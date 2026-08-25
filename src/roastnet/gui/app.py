"""roastnet desktop GUI.

Four tabs: Search and Publish, in the order ARCHITECTURE.md's build order
names them ("search first, publish second"), then Network -- start serving,
sync with a peer, see who you know -- which makes the actual point of the
project (talking to another machine) fully driveable from the GUI instead
of needing the CLI for it -- then Settings, where the database file, the
publish watch folder, and internet-wide discovery live. Those three used to
be a bar repeated atop every tab (just the database file) or not exposed in
the GUI at all; Settings exists so they're set once instead of nagging every
screen. Every action shells out to the same `roastnet` CLI a terminal user
would run -- see gui/runner.py for why -- and the exact command is shown
above its output.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, ttk

from roastnet.gui import config as gui_config
from roastnet.gui import single_instance
from roastnet.gui.runner import Task, describe, roastnet_argv, stream_into
from roastnet.gui.widgets import (
    BG,
    FG,
    FONT_BOLD,
    FONT_H2,
    FONT_MONO,
    MUTED,
    Choice,
    Console,
    Field,
    PeerTable,
    ResultsTable,
    RunBar,
    explain,
    heading,
    section,
)


def _external_subprocess_env() -> dict[str, str] | None:
    """The environment to launch an external (non-roastnet) program with.

    A PyInstaller-frozen roastnet-gui sets LD_LIBRARY_PATH to point at its
    own self-extracted temp directory, so its bundled .so files (built
    for roastnet's own Python/cryptography/etc.) are what get found first
    -- confirmed as the cause of a real bug: opening a roast crashed with
    "openssl not found" / libcrypto.so errors, because the external
    program's dynamic linker picked up roastnet's *bundled* libcrypto
    over the system's own, and the bundled one isn't a complete, ABI-
    compatible OpenSSL install for anything but roastnet itself.
    subprocess.Popen inherits the parent's environment by default, so
    every external program launched (xdg-open, flatpak, a plain Artisan
    binary) picked this up. PyInstaller's bootloader preserves whatever
    LD_LIBRARY_PATH existed before it overrode it as LD_LIBRARY_PATH_ORIG
    (only set if one existed at all) -- restore that, or drop the
    variable entirely if there was none. Returns None (meaning "use the
    parent's environment, unmodified") when not running frozen, so this
    has zero effect on a from-source run."""
    if not getattr(sys, "frozen", False):
        return None
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def _run_opener(cmd: list[str]) -> str | None:
    """Run an opener command and report what happened. Returns None if it
    looks like it worked, or a short message to show the user if it
    clearly didn't -- silently doing nothing on failure (the previous
    behavior here) is exactly the kind of black box this project's GUI
    otherwise avoids everywhere else."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 env=_external_subprocess_env())
    except OSError as exc:
        return f"could not run {cmd[0]}: {exc}"
    try:
        # Most openers (xdg-open, a real Artisan launch) either hand off
        # to a long-running app (still running after this) or return with
        # a nonzero code when nothing could open the target -- but some
        # desktop environments route xdg-open through a D-Bus portal
        # (org.freedesktop.portal.OpenURI) that can take a couple of
        # seconds to resolve, so this waits a bit longer than the launched
        # app's own startup time would suggest, to avoid mistaking "still
        # working on it" for "launched successfully" and reporting no
        # error when the portal call is actually about to fail.
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        output = (proc.stdout.read() if proc.stdout else "").strip()
        return output or f"{cmd[0]} exited with code {proc.returncode}"
    return None


def _open_with_default_app(path: str) -> str | None:
    """Open `path` (file or folder) with whatever the OS would normally
    use. Returns None on apparent success, or a short error message."""
    if sys.platform == "win32":
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return None
        except OSError as exc:
            return f"could not open: {exc}"
    if sys.platform == "darwin":
        return _run_opener(["open", path])
    return _run_opener(["xdg-open", path])


ARTISAN_FLATPAK_ID = "org.artisan_scope.artisan"


def _stage_for_artisan(path: str) -> str:
    """Copy `path` somewhere a sandboxed Artisan install can actually see.

    `flatpak info --show-permissions org.artisan_scope.artisan` shows its
    sandbox's filesystem access is just `xdg-documents` (plus a read-only
    KDE config file) -- not the rest of $HOME. Handing a sandboxed install
    a raw roastnet path (under ~/.local/share/roastnet or the watch
    folder) launches Artisan fine but fails to actually read the file
    (IOError, confirmed on a real machine) because that path is invisible
    from inside its sandbox. Routing through the desktop's file-open
    portal instead would be the "proper", packaging-agnostic fix, but
    needs a D-Bus round trip and only works if the desktop's portal
    backend supports it; copying into ~/Documents -- the one location
    every packaging of Artisan that sandboxes at all can be expected to
    grant (it's the standard XDG "documents" portal directory) -- is
    simpler, needs no extra dependency, and works everywhere Artisan
    itself already works. A single fixed staging filename (not one per
    file) means nothing accumulates there across opens.

    Only called for launch methods _find_artisan_launcher has flagged as
    sandboxed (see `needs_staging` there) -- a plain, unsandboxed install
    skips this, since copying would silently disconnect "save" inside
    Artisan from the real file roastnet (or the user) actually manages."""
    staging_dir = Path.home() / "Documents" / ".roastnet-open"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_path = staging_dir / f"roast{Path(path).suffix}"
    shutil.copy(path, staged_path)
    return str(staged_path)


def _is_snap_wrapper(binary_path: str) -> bool:
    """True if a `shutil.which`-resolved binary is Snap's exported
    wrapper rather than a plain native install. Unlike Flatpak (which
    exports under the app's full reverse-DNS id, so it's caught by name
    before this is ever consulted), Snap exports its wrapper under the
    app's own plain command name -- e.g. still literally `artisan` -- so
    a Snap-confined install is indistinguishable from a native one by
    name alone; the resolved path (always under /snap/) is what actually
    tells them apart. Not confirmed against a real Snap-packaged Artisan
    (unlike the Flatpak case, which was) -- this is a defensive guess at
    the same class of bug applying there too, since strict Snap
    confinement can restrict filesystem visibility the same way Flatpak's
    did."""
    return binary_path.startswith("/snap/") or binary_path.startswith("/var/lib/snapd/")


def _find_artisan_launcher(path: str) -> tuple[list[str], bool] | None:
    """Locate a real Artisan install across the ways it commonly ends up
    on a machine. Returns (argv_prefix, needs_staging) -- the caller
    appends the actual file path (staged first via _stage_for_artisan if
    `needs_staging`) to get the full command -- or None if no install is
    found at all.

    A plain PATH binary (the AUR package `artisan-roaster-scope` installs
    one literally named `artisan`) is tried first. Flatpak -- the Flathub
    package `org.artisan_scope.artisan` -- needs its own handling:
    Flatpak exports a PATH wrapper under the app's full reverse-DNS id,
    not under `artisan`, so `shutil.which("artisan")` never finds it even
    though it's right there on PATH. Confirmed on a real machine during
    development: this exact gap was why "open original file" silently
    fell back to a text editor instead of the Artisan the user actually
    had installed, and once found, why it then failed with an IOError
    until its sandbox's restricted filesystem view was worked around too
    (see _stage_for_artisan). Snap is handled defensively on the same
    reasoning (see _is_snap_wrapper) even though it hasn't been confirmed
    against a real Snap-packaged Artisan the way Flatpak was."""
    for name in ("artisan", "Artisan"):
        found = shutil.which(name)
        if found:
            return [found], _is_snap_wrapper(found)

    flatpak_wrapper = shutil.which(ARTISAN_FLATPAK_ID)
    if flatpak_wrapper:
        return [flatpak_wrapper], True

    if shutil.which("flatpak"):
        # Covers a Flatpak install whose exports/bin wrapper isn't on
        # PATH in this process specifically (e.g. launched from a
        # desktop entry with a trimmed PATH) but the flatpak command
        # itself is -- `flatpak run` finds the app by id regardless.
        try:
            check = subprocess.run(["flatpak", "info", ARTISAN_FLATPAK_ID],
                                    capture_output=True, timeout=3, env=_external_subprocess_env())
        except (OSError, subprocess.TimeoutExpired):
            check = None
        if check is not None and check.returncode == 0:
            return ["flatpak", "run", ARTISAN_FLATPAK_ID], True

    if sys.platform == "darwin" and Path("/Applications/Artisan.app").exists():
        # Direct-download macOS distribution isn't sandboxed; if that
        # ever changes (e.g. a Mac App Store build with App Sandbox
        # entitlements), this would need the same needs_staging=True
        # treatment as Flatpak/Snap above.
        return ["open", "-a", "Artisan"], False

    return None


def _open_alog_file(path: str) -> str | None:
    """Same contract as _open_with_default_app, but for a .alog file
    specifically: tries a real Artisan install first, since a .alog file
    is, on disk, just a Python dict literal with no MIME type registered
    on most systems -- the OS's generic handler for it very often turns
    out to be a text editor, not the roasting software the file is
    actually for (confirmed during development: xdg-open opened one in
    Kate, not Artisan, on a machine with no Artisan install). Not used for
    opening a folder -- Artisan isn't a file manager."""
    found = _find_artisan_launcher(path)
    if found is None:
        return _open_with_default_app(path)
    launcher, needs_staging = found
    target_path = _stage_for_artisan(path) if needs_staging else path
    return _run_opener([*launcher, target_path])


def _copy_to_clipboard(widget: tk.Widget, text: str) -> None:
    """A manual fallback that always works, regardless of whether this
    desktop has anything registered to auto-open a file or folder with --
    paste the path into a file manager or terminal yourself."""
    widget.clipboard_clear()
    widget.clipboard_append(text)


class Tab(ttk.Frame):
    """Base tab: owns one background task, for cancellation on close."""

    def __init__(self, parent: tk.Widget, app: "RoastnetApp") -> None:
        super().__init__(parent)
        self.app = app
        self.task: Task | None = None

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()


class SearchTab(Tab):
    """Find roast profiles in the local index -- own roasts plus anything
    replicated from peers. First tab, per ARCHITECTURE.md's "search first"."""

    def __init__(self, parent: tk.Widget, app: "RoastnetApp") -> None:
        super().__init__(parent, app)
        heading(self, "Search", "Find roast profiles in your local index.")
        explain(self, "Text (optional) is matched against bean/process notes and roast type. "
                       "The filters below narrow further -- leave any blank to not filter on it. "
                       "\"LAN only\" is on by default, so a stranger found through internet-wide "
                       "discovery doesn't show up in results just for being nearby on the network.")

        self.query = Field(self, "Text", help_text="Free-text search, e.g. 'washed ethiopian'.")
        self.machine = Field(self, "Machine", help_text="Exact machine_key, e.g. kaleido_m2.")
        self.roast_type = Field(self, "Roast type", help_text="e.g. 'full city', 'vienna'.")
        self.dtr_min = Field(self, "DTR min %", width=8)
        self.dtr_max = Field(self, "DTR max %", width=8)
        self.drop_after = Field(self, "Drop after (°C)", width=8)
        self.second_crack = Choice(self, "After second crack?", ["any", "yes", "no"], default="any")

        self.lan_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self, text="LAN only (hide results from internet-wide or manually-added peers)",
            variable=self.lan_only,
        ).pack(anchor="w", padx=10, pady=(6, 2))

        self.own_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self, text="Only my own roasts (hide everything synced from any peer)",
            variable=self.own_only,
        ).pack(anchor="w", padx=10, pady=(0, 2))

        self.show_hidden = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self, text="Show hidden roasts too",
            variable=self.show_hidden,
        ).pack(anchor="w", padx=10, pady=(0, 2))

        self._output: list[str] = []
        self.runbar = RunBar(self, "Search", self._on_run, self.cancel)
        self.table = ResultsTable(self)
        self.table.tree.bind("<Double-1>", self._on_open_row)
        explain(self, "Double-click a result to see its full detail and open the original .alog file.")

    def _build_args(self) -> list[str]:
        args = ["search"]
        text = self.query.get()
        if text:
            args.append(text)
        if self.machine.get():
            args += ["--machine", self.machine.get()]
        if self.roast_type.get():
            args += ["--roast-type", self.roast_type.get()]
        if self.dtr_min.get():
            args += ["--dtr-min", self.dtr_min.get()]
        if self.dtr_max.get():
            args += ["--dtr-max", self.dtr_max.get()]
        if self.drop_after.get():
            args += ["--drop-after", self.drop_after.get()]
        choice = self.second_crack.get()
        if choice == "yes":
            args.append("--after-second-crack")
        elif choice == "no":
            args.append("--not-after-second-crack")
        if not self.lan_only.get():
            args.append("--all-peers")
        if self.own_only.get():
            args.append("--own-only")
        if self.show_hidden.get():
            args.append("--show-hidden")
        args.append("--json")
        return args

    def _on_run(self) -> None:
        if self.task is not None and self.task.running:
            return
        argv = roastnet_argv("--db", self.app.db_path.get(), *self._build_args())
        self._output = []
        self.table.set_error("running...")
        self.runbar.set_running(True, "running...")
        self.task = Task(argv=argv)
        self.task.start()
        stream_into(self.task, self._output.append, self._on_finished, lambda ms, fn: self.after(ms, fn))

    def _on_finished(self, code: int) -> None:
        self.runbar.set_running(False, "done" if code == 0 else f"exited with code {code}")
        text = "".join(self._output)
        if code != 0:
            self.table.set_error(text.strip() or f"exited with code {code}")
            return
        try:
            rows = json.loads(text)
        except json.JSONDecodeError:
            self.table.set_error("could not parse results")
            return
        self.table.set_rows(rows)

    def _on_open_row(self, event: tk.Event) -> None:
        roast_id = self.table.tree.identify_row(event.y)
        if not roast_id:
            return
        argv = roastnet_argv("--db", self.app.db_path.get(), "show", roast_id, "--json")
        buf: list[str] = []
        task = Task(argv=argv)
        task.start()
        stream_into(task, buf.append, lambda code: self._open_row_loaded(code, buf, roast_id),
                    lambda ms, fn: self.after(ms, fn))

    def _open_row_loaded(self, code: int, buf: list[str], roast_id: str) -> None:
        if code != 0:
            return
        try:
            payload = json.loads("".join(buf))
        except json.JSONDecodeError:
            return
        # kept as an attribute (not just a local) so tests can reach the
        # window that opened without needing to walk winfo_children()
        self._last_detail_window = RoastDetailWindow(
            self, self.app, roast_id, payload.get("record") or {}, payload.get("raw_path"),
            bool(payload.get("hidden")), on_change=self._on_run,
        )


class RoastDetailWindow(tk.Toplevel):
    """A search result, opened: full metadata, milestones, notes, and
    buttons to open the original .alog file -- with a real Artisan install
    if one is found on PATH, since the OS's generic file-type handler for
    .alog is very often a text editor instead (see _open_alog_file) -- and
    to hide/unhide it from this machine's own search results. Whatever
    happens (opening the file, or hide/unhide), success or failure is
    reported in the status line below the buttons -- never silent."""

    def __init__(
        self, parent: tk.Widget, app: "RoastnetApp", roast_id: str, record: dict,
        raw_path: str | None, hidden: bool, *, on_change: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.roast_id = roast_id
        self.hidden = hidden
        self.on_change = on_change
        self.configure(bg=BG)
        beans_lines = (record.get("beans_text") or "").splitlines()
        title = record.get("title") or (beans_lines[0] if beans_lines else "Roast detail")
        self.title(title)

        heading(self, title)

        info = ttk.Frame(self)
        info.pack(fill="x", padx=14, pady=(0, 6))

        def row(label: str, value) -> None:
            r = ttk.Frame(info)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=label, font=FONT_BOLD, bg=BG, fg=FG, width=18,
                     anchor="w").pack(side="left")
            tk.Label(r, text=str(value) if value not in (None, "") else "?", font=FONT_MONO,
                     bg=BG, fg=MUTED, anchor="w", wraplength=600, justify="left").pack(side="left")

        row("Machine", f"{record.get('machine_key')} ({record.get('roaster_type_raw')})")
        roast_type_value = (
            f"{record['roast_type']} (estimated from peak temperature -- "
            "may not hold for every machine's probe)"
        ) if record.get("roast_type") else None
        row("Roast type", roast_type_value)
        row("Batch in / out", f"{record.get('batch_weight_in_g')}g / {record.get('batch_weight_out_g')}g")
        row("Roast date", record.get("roast_date"))

        milestones = record.get("milestones") or []
        if milestones:
            tk.Label(self, text="Milestones", font=FONT_H2, fg=FG, bg=BG, anchor="w").pack(
                fill="x", padx=14, pady=(10, 2))
            for m in milestones:
                row(m.get("name") or "?", f"t={m.get('time_s')}  BT={m.get('bt_c')}  ET={m.get('et_c')}")

        notes = record.get("roasting_notes") or record.get("cupping_notes")
        if notes:
            tk.Label(self, text="Notes", font=FONT_H2, fg=FG, bg=BG, anchor="w").pack(
                fill="x", padx=14, pady=(10, 2))
            explain(self, notes)

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=14, pady=(12, 2))
        if raw_path:
            ttk.Button(btn_row, text="Open original file",
                       command=lambda: self._on_open_file(raw_path)).pack(side="left")
            ttk.Button(btn_row, text="Copy path",
                       command=lambda: _copy_to_clipboard(self, raw_path)).pack(side="left", padx=(6, 0))
        self.hide_button = ttk.Button(
            btn_row, text=("Unhide" if hidden else "Hide"), command=self._on_toggle_hidden,
        )
        self.hide_button.pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Close", command=self.destroy).pack(side="right")

        if raw_path:
            tk.Label(self, text=raw_path, font=FONT_MONO, fg=MUTED, bg=BG, anchor="w",
                     wraplength=840, justify="left").pack(fill="x", padx=14, pady=(4, 0))

        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, font=("TkDefaultFont", 9), fg=MUTED,
                 bg=BG, anchor="w", wraplength=840, justify="left").pack(fill="x", padx=14, pady=(2, 12))

    def _on_open_file(self, path: str) -> None:
        error = _open_alog_file(path)
        self.status_var.set(
            f"Couldn't open it: {error}. The file itself is at the path shown above." if error else ""
        )

    def _on_toggle_hidden(self) -> None:
        command = "unhide" if self.hidden else "hide"
        argv = roastnet_argv("--db", self.app.db_path.get(), command, self.roast_id)
        buf: list[str] = []
        task = Task(argv=argv)
        task.start()
        stream_into(task, buf.append, lambda code: self._on_hide_toggled(code, buf),
                    lambda ms, fn: self.after(ms, fn))

    def _on_hide_toggled(self, code: int, buf: list[str]) -> None:
        if code != 0:
            self.status_var.set(f"Couldn't change hidden status: {''.join(buf).strip()}")
            return
        self.hidden = not self.hidden
        self.hide_button.configure(text="Unhide" if self.hidden else "Hide")
        self.status_var.set("Hidden from your own search results." if self.hidden else "Unhidden.")
        if self.on_change:
            self.on_change()


class PublishTab(Tab):
    """Append one of your own roasts to your signed feed. Second tab, per
    ARCHITECTURE.md's "publish second"."""

    def __init__(self, parent: tk.Widget, app: "RoastnetApp") -> None:
        super().__init__(parent, app)
        heading(self, "Publish", "Add one of your own roasts to your signed feed.")
        explain(self, "Publishing appends a signed entry to your local feed -- your identity is "
                       "created silently the first time you publish, if you don't have one yet. "
                       "A peer only receives it once they sync with you, which the Network tab "
                       "now does on its own once you're serving.")

        identity_row = ttk.Frame(self)
        identity_row.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(identity_row, text="Feed address:", font=FONT_BOLD, bg=BG, fg=FG).pack(side="left")
        self.identity_var = tk.StringVar(value="(loading...)")
        tk.Label(identity_row, textvariable=self.identity_var, font=FONT_MONO, bg=BG,
                 fg=MUTED).pack(side="left", padx=(6, 0))

        folder_section = section(self, "Shared folder (recommended)")
        tk.Label(folder_section, text="Drop .alog files here and they're published automatically, "
                 "as long as the Network tab is serving -- no button to click per file:",
                 font=("TkDefaultFont", 9), fg=MUTED, bg=BG, wraplength=840, justify="left",
                 anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        folder_row = ttk.Frame(folder_section)
        folder_row.pack(fill="x", padx=10, pady=(0, 2))
        tk.Label(folder_row, textvariable=self.app.watch_dir, font=FONT_MONO, bg=BG, fg=FG).pack(
            side="left")
        ttk.Button(folder_row, text="Open folder", command=self._open_watch_folder).pack(
            side="left", padx=(8, 0))
        ttk.Button(folder_row, text="Copy path",
                   command=lambda: _copy_to_clipboard(self, self.app.watch_dir.get())).pack(
            side="left", padx=(6, 0))
        self.folder_status_var = tk.StringVar(value="")
        tk.Label(folder_section, textvariable=self.folder_status_var, font=("TkDefaultFont", 9),
                 fg=MUTED, bg=BG, anchor="w", wraplength=840, justify="left").pack(
            fill="x", padx=10, pady=(0, 8))

        single_file_section = section(self, "Publish a single file")
        self.path_field = Field(single_file_section, "File to publish", help_text="An Artisan .alog file.")
        ttk.Button(single_file_section, text="Browse...", command=self._browse).pack(
            padx=10, pady=(0, 6), anchor="w")

        self.runbar = RunBar(single_file_section, "Publish", self._on_run, self.cancel)
        self.console = Console(single_file_section)

        self._load_identity()

    def _open_watch_folder(self) -> None:
        path = self.app.watch_dir.get()
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.folder_status_var.set(f"couldn't create {path}: {exc}")
            return
        error = _open_with_default_app(path)
        self.folder_status_var.set(f"couldn't open it: {error}. The folder itself is at: {path}" if error else "")

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a .alog file", filetypes=[("Artisan roast log", "*.alog"), ("All files", "*.*")],
        )
        if path:
            self.path_field.set(path)

    def _load_identity(self) -> None:
        buf: list[str] = []
        task = Task(argv=roastnet_argv("identity", "show"))
        task.start()
        stream_into(task, buf.append, lambda code: self._identity_loaded(buf), lambda ms, fn: self.after(ms, fn))

    def _identity_loaded(self, buf: list[str]) -> None:
        lines = [line for line in "".join(buf).splitlines() if line.strip()]
        self.identity_var.set(lines[-1] if lines else "(unknown)")

    def _on_run(self) -> None:
        if self.task is not None and self.task.running:
            return
        path = self.path_field.get()
        if not path:
            self.console.clear()
            self.console.append("choose a .alog file first\n")
            return
        argv = roastnet_argv("--db", self.app.db_path.get(), "feed", "publish", path)
        self.console.clear()
        self.console.set_command(describe(argv))
        self.task = Task(argv=argv)
        self.runbar.set_running(True, "running...")
        self.task.start()
        stream_into(self.task, self.console.append, self._on_finished, lambda ms, fn: self.after(ms, fn))

    def _on_finished(self, code: int) -> None:
        self.runbar.set_running(False, "done" if code == 0 else f"exited with code {code}")
        if code == 0:
            self._load_identity()


class NetworkTab(Tab):
    """Serve your feed to peers, and sync with theirs. Third tab -- the
    piece that makes cross-machine testing fully GUI-driven.

    Tracks two independent background processes (you can be serving and
    syncing at once), so it doesn't use the base Tab's single self.task --
    it overrides cancel() to stop both instead.
    """

    def __init__(self, parent: tk.Widget, app: "RoastnetApp") -> None:
        super().__init__(parent, app)
        self.serve_task: Task | None = None
        self.sync_task: Task | None = None
        self._closed = False

        heading(self, "Network", "Serve your feed to peers, and pull theirs.")
        explain(self, "The network is on automatically while this app is running -- peers on your "
                       "local network are found and synced with on their own, no clicking needed. "
                       "Internet-wide discovery (Settings tab) finds peers beyond your LAN the same "
                       "way, if you've turned it on. For a peer discovery won't reach, share the "
                       "ticket shown below with them, or paste theirs under 'Sync with a peer'. "
                       "Changes made in Settings apply the next time you Stop then Start serving.")

        serve_section = section(self, "Serve your feed")
        tk.Label(serve_section, text="Your ticket:", font=FONT_BOLD, bg=BG, fg=FG).pack(
            anchor="w", padx=10, pady=(6, 0))
        ticket_row = ttk.Frame(serve_section)
        ticket_row.pack(fill="x", padx=10, pady=(0, 6))
        self.ticket_var = tk.StringVar(value="")
        self.ticket_entry = ttk.Entry(ticket_row, textvariable=self.ticket_var, state="readonly")
        self.ticket_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(ticket_row, text="Copy", command=self._copy_ticket).pack(side="left", padx=(6, 0))
        self.serve_runbar = RunBar(serve_section, "Start serving", self._on_start_serve, self._on_stop_serve)
        self.serve_console = Console(serve_section, height=4)

        sync_section = section(self, "Sync with a peer")
        self.peer_ticket_field = Field(sync_section, "Peer's ticket",
                                        help_text="Paste the ticket they shared with you.")
        self.sync_runbar = RunBar(sync_section, "Sync", self._on_sync, self._on_cancel_sync)
        self.sync_console = Console(sync_section, height=4)

        peers_section = section(self, "Known peers")
        self.peers_table = PeerTable(peers_section)

        self._on_start_serve()
        self._schedule_peer_refresh()

    def cancel(self) -> None:
        self._closed = True
        if self.serve_task is not None:
            self.serve_task.cancel()
        if self.sync_task is not None:
            self.sync_task.cancel()

    def _schedule_peer_refresh(self) -> None:
        # keeps "Known peers" (and thus visibility into LAN auto-discovery)
        # live without the user ever clicking anything -- self-reschedules
        # until the tab/app is torn down.
        if self._closed:
            return
        self._refresh_peers()
        self.after(5000, self._schedule_peer_refresh)

    def _copy_ticket(self) -> None:
        ticket = self.ticket_var.get()
        if not ticket:
            return
        self.clipboard_clear()
        self.clipboard_append(ticket)

    def _on_start_serve(self) -> None:
        if self.serve_task is not None and self.serve_task.running:
            return
        argv = roastnet_argv("--db", self.app.db_path.get(), "node", "serve",
                              "--publish-watch-dir", self.app.watch_dir.get())
        if self.app.wan_discovery_enabled.get():
            argv.append("--wan-discovery")
        self.serve_console.clear()
        self.serve_console.set_command(describe(argv))
        self.ticket_var.set("")
        self.serve_task = Task(argv=argv)
        self.serve_runbar.set_running(True, "starting...")
        self.serve_task.start()
        stream_into(self.serve_task, self._on_serve_output, self._on_serve_finished,
                    lambda ms, fn: self.after(ms, fn))

    def _on_serve_output(self, text: str) -> None:
        self.serve_console.append(text)
        for line in text.splitlines():
            if line.startswith("ticket: "):
                self.ticket_var.set(line[len("ticket: "):].strip())
                self.serve_runbar.set_running(True, "serving")

    def _on_stop_serve(self) -> None:
        if self.serve_task is not None:
            self.serve_task.cancel()

    def _on_serve_finished(self, code: int) -> None:
        self.serve_runbar.set_running(False, "stopped" if code == 0 else f"stopped (exit {code})")
        self.ticket_var.set("")

    def _on_sync(self) -> None:
        if self.sync_task is not None and self.sync_task.running:
            return
        ticket = self.peer_ticket_field.get()
        if not ticket:
            self.sync_console.clear()
            self.sync_console.append("paste a peer's ticket first\n")
            return
        argv = roastnet_argv("--db", self.app.db_path.get(), "peer", "sync", ticket)
        self.sync_console.clear()
        self.sync_console.set_command(describe(argv))
        self.sync_task = Task(argv=argv)
        self.sync_runbar.set_running(True, "syncing...")
        self.sync_task.start()
        stream_into(self.sync_task, self.sync_console.append, self._on_sync_finished,
                    lambda ms, fn: self.after(ms, fn))

    def _on_cancel_sync(self) -> None:
        if self.sync_task is not None:
            self.sync_task.cancel()

    def _on_sync_finished(self, code: int) -> None:
        self.sync_runbar.set_running(False, "done" if code == 0 else f"exited with code {code}")
        self._refresh_peers()

    def _refresh_peers(self) -> None:
        buf: list[str] = []
        task = Task(argv=roastnet_argv("peer", "list", "--json"))
        task.start()
        stream_into(task, buf.append, lambda code: self._peers_loaded(buf), lambda ms, fn: self.after(ms, fn))

    def _peers_loaded(self, buf: list[str]) -> None:
        try:
            peers = json.loads("".join(buf))
        except json.JSONDecodeError:
            return
        self.peers_table.set_rows(peers)


class SettingsTab(Tab):
    """Where things live, and how far discovery reaches. Its own tab
    rather than a bar repeated atop every screen (the database file used
    to be exactly that) because these are set-once choices, not something
    to reconsider on every search or publish. Every field here writes
    through to gui/config.py immediately, so a choice made here survives
    closing and reopening the app."""

    def __init__(self, parent: tk.Widget, app: "RoastnetApp") -> None:
        super().__init__(parent, app)
        heading(self, "Settings", "Where things live, and how far discovery reaches.")

        db_section = section(self, "Database file")
        explain(db_section, "Where your local search index lives. Search, Publish, and Network "
                             "all use this. Existing tabs pick up a change the next time they run.")
        self.db_field = Field(db_section, "Path", variable=self.app.db_path, width=60)
        ttk.Button(db_section, text="Browse...", command=self._browse_db).pack(
            padx=10, pady=(0, 8), anchor="w")

        watch_section = section(self, "Shared publish folder")
        explain(watch_section, "Any .alog file dropped here is published automatically while "
                                "the Network tab is serving -- see the Publish tab.")
        self.watch_field = Field(watch_section, "Path", variable=self.app.watch_dir, width=60)
        ttk.Button(watch_section, text="Browse...", command=self._browse_watch_dir).pack(
            padx=10, pady=(0, 8), anchor="w")

        wan_section = section(self, "Internet-wide discovery")
        explain(wan_section,
                "Off by default. LAN discovery only ever broadcasts on your local network. "
                "Turning this on also finds and syncs with roastnet peers anywhere on the "
                "internet, the same way a BitTorrent client finds peers with no tracker of its "
                "own: by announcing on the public BitTorrent DHT, a huge, already-running "
                "public network -- no server of roastnet's own involved. The trade-off: your "
                "public IP address (and the fact that it's running roastnet) becomes visible to "
                "anyone else looking at that same swarm, which a LAN broadcast never exposes. "
                "Restart serving (Network tab: Stop, then Start) after changing this.")
        ttk.Checkbutton(wan_section, text="Find peers over the whole internet, not just my LAN",
                         variable=self.app.wan_discovery_enabled).pack(anchor="w", padx=10, pady=(0, 8))

    def _browse_db(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Choose a database file", defaultextension=".sqlite3",
            filetypes=[("SQLite database", "*.sqlite3"), ("All files", "*.*")],
        )
        if path:
            self.app.db_path.set(path)

    def _browse_watch_dir(self) -> None:
        path = filedialog.askdirectory(title="Choose a folder to auto-publish from")
        if path:
            self.app.watch_dir.set(path)


class RoastnetApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("roastnet")
        self.geometry("900x680")
        self.configure(bg=BG)
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass

        cfg = gui_config.load_config()
        self.db_path = tk.StringVar(value=cfg.db_path)
        self.watch_dir = tk.StringVar(value=cfg.watch_dir)
        self.wan_discovery_enabled = tk.BooleanVar(value=cfg.wan_discovery_enabled)
        for var in (self.db_path, self.watch_dir, self.wan_discovery_enabled):
            var.trace_add("write", lambda *_args: self._save_config())

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=6, pady=6)

        search_tab = SearchTab(notebook, self)
        publish_tab = PublishTab(notebook, self)
        network_tab = NetworkTab(notebook, self)
        settings_tab = SettingsTab(notebook, self)
        notebook.add(search_tab, text="Search")
        notebook.add(publish_tab, text="Publish")
        notebook.add(network_tab, text="Network")
        notebook.add(settings_tab, text="Settings")
        self.tabs: list[Tab] = [search_tab, publish_tab, network_tab, settings_tab]

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _save_config(self) -> None:
        gui_config.save_config(gui_config.GuiConfig(
            db_path=self.db_path.get(), watch_dir=self.watch_dir.get(),
            wan_discovery_enabled=self.wan_discovery_enabled.get(),
        ))

    def _on_close(self) -> None:
        for tab in self.tabs:
            tab.cancel()
        self.destroy()


def main(*, single_instance_port: int = single_instance.PORT) -> None:
    if single_instance.another_instance_is_running(port=single_instance_port):
        return  # asked it to focus itself instead -- nothing else to do here

    app = RoastnetApp()

    # A plain SIGTERM (killed via `kill`/`pkill`, a session manager logging
    # the user out, systemd stopping the unit -- anything that isn't the
    # window's own close button) never reaches WM_DELETE_WINDOW, so without
    # this, app._on_close() -- and with it, Task.cancel()'s os.killpg on
    # the detached `node serve` child (gui/runner.py deliberately puts it
    # in its own process group so Cancel can kill it and its children) --
    # never runs. The result: node serve outlives the window that started
    # it, as a permanent orphan still serving/discovering/auto-publishing
    # with no visible app left. Confirmed during development: exactly this
    # leaked real orphaned node serve processes on a real machine.
    def _handle_terminate(signum, frame) -> None:
        app._on_close()

    signal.signal(signal.SIGTERM, _handle_terminate)

    focus_requests: queue.Queue = queue.Queue()
    # start_focus_listener's callback runs on a background thread -- Tk
    # widgets may only be touched from the thread that created them, so
    # it just drops a marker in the queue and _poll_focus_requests (on
    # the Tk main thread, via app.after, same pattern gui/runner.py's
    # stream_into uses for task output) is what actually raises the
    # window.
    single_instance.start_focus_listener(lambda: focus_requests.put(None), port=single_instance_port)

    def _poll_focus_requests() -> None:
        try:
            while True:
                focus_requests.get_nowait()
                app.deiconify()
                app.lift()
                app.focus_force()
        except queue.Empty:
            pass
        app.after(300, _poll_focus_requests)

    _poll_focus_requests()
    app.mainloop()


if __name__ == "__main__":
    main()
