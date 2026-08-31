"""roastmesh desktop GUI.

Four tabs: Search and Publish, in the order ARCHITECTURE.md's build order
names them ("search first, publish second"), then Network -- start serving,
sync with a peer, see who you know -- which makes the actual point of the
project (talking to another machine) fully driveable from the GUI instead
of needing the CLI for it -- then Settings, where the database file, the
publish watch folder, and internet-wide discovery live. Those three used to
be a bar repeated atop every tab (just the database file) or not exposed in
the GUI at all; Settings exists so they're set once instead of nagging every
screen. Every action shells out to the same `roastmesh` CLI a terminal user
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
import tkinter.font as tkfont
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, ttk

from roastmesh.alog.curves import format_mmss
from roastmesh.gui import config as gui_config
from roastmesh.gui import i18n
from roastmesh.gui import single_instance
from roastmesh.gui import units
from roastmesh.gui import widgets
from roastmesh.gui.chart import RoastChart
from roastmesh.gui.i18n import t, tn
from roastmesh.gui.runner import Task, describe, parse_json_output, roastmesh_argv, stream_into
from roastmesh.models import weight_loss_pct
from roastmesh.gui.widgets import (
    BG,
    FG,
    FONT_BOLD,
    FONT_H2,
    FONT_MONO,
    MUTED,
    AutocompleteField,
    Choice,
    Console,
    Field,
    PeerTable,
    ResultsTable,
    RunBar,
    UserTable,
    explain,
    heading,
    screen_geometry,
    scrollable,
    section,
    sp,
)


def _external_subprocess_env() -> dict[str, str] | None:
    """The environment to launch an external (non-roastmesh) program with.

    A PyInstaller-frozen roastmesh-gui sets LD_LIBRARY_PATH to point at its
    own self-extracted temp directory, so its bundled .so files (built
    for roastmesh's own Python/cryptography/etc.) are what get found first
    -- confirmed as the cause of a real bug: opening a roast crashed with
    "openssl not found" / libcrypto.so errors, because the external
    program's dynamic linker picked up roastmesh's *bundled* libcrypto
    over the system's own, and the bundled one isn't a complete, ABI-
    compatible OpenSSL install for anything but roastmesh itself.
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
        return t("could not run {command}: {error}", command=cmd[0], error=exc)
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
        return output or t("{command} exited with code {code}", command=cmd[0], code=proc.returncode)
    return None


def _open_with_default_app(path: str) -> str | None:
    """Open `path` (file or folder) with whatever the OS would normally
    use. Returns None on apparent success, or a short error message."""
    if sys.platform == "win32":
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return None
        except OSError as exc:
            return t("could not open: {error}", error=exc)
    if sys.platform == "darwin":
        return _run_opener(["open", path])
    return _run_opener(["xdg-open", path])


ARTISAN_FLATPAK_ID = "org.artisan_scope.artisan"


def _stage_for_artisan(path: str) -> str:
    """Copy `path` somewhere a sandboxed Artisan install can actually see.

    `flatpak info --show-permissions org.artisan_scope.artisan` shows its
    sandbox's filesystem access is just `xdg-documents` (plus a read-only
    KDE config file) -- not the rest of $HOME. Handing a sandboxed install
    a raw roastmesh path (under ~/.local/share/roastmesh or the watch
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
    Artisan from the real file roastmesh (or the user) actually manages."""
    staging_dir = Path.home() / "Documents" / ".roastmesh-open"
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


def _exited_with_code(code: int) -> str:
    """Shared wording for a subprocess that finished with a nonzero exit
    code -- consolidated from four near-identical f-strings so there's one
    catalog entry to translate instead of four."""
    return t("exited with code {code}", code=code)


class Tab(ttk.Frame):
    """Base tab: owns one background task, for cancellation on close."""

    def __init__(self, parent: tk.Widget, app: "RoastmeshApp") -> None:
        super().__init__(parent)
        self.app = app
        self.task: Task | None = None

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()


class SearchTab(Tab):
    """Find roast profiles in the local index -- own roasts plus anything
    replicated from peers. First tab, per ARCHITECTURE.md's "search first".

    Two modes, chosen by the radio buttons at the top: *All users* (the
    original form -- search everything, optionally narrowed by machine or
    to favorited users' roasts) and *One user* (browse a single person's
    roasts: filter/select from a user list, then their roasts land in the
    same results table below). Both modes share the free-text box, the
    roast-shape filters (roast type/DTR/drop temp/second crack), and the
    lan-only/own-only/show-hidden checkboxes -- only what "Machine" and
    "favorites" mean differs, since in One user mode those filter the user
    list, not the roasts directly.
    """

    MODE_ALL = "all_users"
    MODE_ONE = "one_user"

    def __init__(self, parent: tk.Widget, app: "RoastmeshApp") -> None:
        super().__init__(parent, app)
        heading(self, t("Search"), t("Find roast profiles in your local index."))
        explain(self, t("Text (optional) is matched against bean/process notes and roast type. "
                         "The filters below narrow further -- leave any blank to not filter on it. "
                         "Peers found through internet-wide discovery show up in results by default, "
                         "same as LAN peers -- check \"LAN only\" to hide anyone not on your local network."))

        self.selected_user_pubkey: str | None = None
        self._user_rows: dict[str, dict] = {}

        mode_row = ttk.Frame(self)
        mode_row.pack(fill="x", padx=10, pady=(0, 2))
        tk.Label(mode_row, text=t("Show:"), font=FONT_BOLD, bg=BG, fg=FG).pack(side="left")
        self.mode = tk.StringVar(value=self.MODE_ALL)
        ttk.Radiobutton(mode_row, text=t("All users"), value=self.MODE_ALL, variable=self.mode,
                        command=self._on_mode_changed).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(mode_row, text=t("One user"), value=self.MODE_ONE, variable=self.mode,
                        command=self._on_mode_changed).pack(side="left", padx=(8, 0))

        self.query = Field(self, t("Text"), help_text=t("Free-text search, e.g. 'washed ethiopian'."))

        # Only one of these two frames is packed at a time (see
        # _on_mode_changed) -- both live inside a fixed-position container so
        # swapping between them doesn't disturb the rest of the tab's layout.
        self.filter_container = ttk.Frame(self)
        self.filter_container.pack(fill="x")

        self.all_users_frame = ttk.Frame(self.filter_container)
        self.machine = AutocompleteField(self.all_users_frame, t("Machine"),
                                          help_text=t("Exact machine_key, e.g. kaleido_m2."))
        self.favorites_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.all_users_frame,
            text=t("Favorites only (hide roasts from anyone you haven't favorited)"),
            variable=self.favorites_only,
        ).pack(anchor="w", padx=10, pady=(0, 2))

        self.one_user_frame = ttk.Frame(self.filter_container)
        explain(self.one_user_frame, t("Filter the list below, then select someone to see their roasts "
                                        "in the results table below."))
        self.user_machine = AutocompleteField(self.one_user_frame, t("Machine"),
                                               help_text=t("Only list users who declared this machine."))
        self.user_favorites_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.one_user_frame, text=t("Favorites only"),
                         variable=self.user_favorites_only).pack(anchor="w", padx=10, pady=(0, 2))
        ttk.Button(self.one_user_frame, text=t("Refresh list"),
                   command=self._refresh_user_list).pack(anchor="w", padx=10, pady=(0, 4))
        self.user_table = UserTable(self.one_user_frame)
        self.user_table.tree.bind("<<TreeviewSelect>>", self._on_user_selected)
        user_btn_row = ttk.Frame(self.one_user_frame)
        user_btn_row.pack(fill="x", padx=10, pady=(0, 2))
        self.favorite_btn = ttk.Button(user_btn_row, text=t("Favorite"),
                                        command=self._on_toggle_user_favorite, state="disabled")
        self.favorite_btn.pack(side="left")
        self.like_btn = ttk.Button(user_btn_row, text=t("Like"),
                                    command=self._on_toggle_user_like, state="disabled")
        self.like_btn.pack(side="left", padx=(6, 0))
        self.user_status = tk.StringVar(value="")
        tk.Label(self.one_user_frame, textvariable=self.user_status, font=("TkDefaultFont", 9),
                 fg=MUTED, bg=BG, anchor="w").pack(fill="x", padx=10, pady=(0, 4))

        self.all_users_frame.pack(fill="x")  # default mode is MODE_ALL

        self.roast_type = Field(self, t("Roast type"), help_text=t("e.g. 'full city', 'vienna'."))
        self.dtr_min = Field(self, t("DTR min %"), width=8)
        self.dtr_max = Field(self, t("DTR max %"), width=8)
        self.drop_after = Field(self, t("Drop after (°C)"), width=8)
        # Options are logic values compared in _build_args below, not
        # display text -- Choice has no separate display/value split, so
        # the dropdown itself stays in English ("any"/"yes"/"no") while the
        # label above it is translated. A user typing a search filter
        # dropdown is a smaller translation gap than breaking the filter.
        self.second_crack = Choice(self, t("After second crack?"), ["any", "yes", "no"], default="any")

        self.lan_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self, text=t("LAN only (hide results from internet-wide or manually-added peers)"),
            variable=self.lan_only,
        ).pack(anchor="w", padx=10, pady=(6, 2))

        self.own_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self, text=t("Only my own roasts (hide everything synced from any peer)"),
            variable=self.own_only,
        ).pack(anchor="w", padx=10, pady=(0, 2))

        self.show_hidden = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self, text=t("Show hidden roasts too"),
            variable=self.show_hidden,
        ).pack(anchor="w", padx=10, pady=(0, 2))

        self._output: list[str] = []
        self.runbar = RunBar(self, t("Search"), self._on_run, self.cancel)
        self.table = ResultsTable(self)
        self.table.tree.bind("<Double-1>", self._on_open_row)
        explain(self, t("Double-click a result to see its full detail and open the original .alog file."))

        self._load_used_machines()

    def _on_mode_changed(self) -> None:
        if self.mode.get() == self.MODE_ONE:
            self.all_users_frame.pack_forget()
            self.one_user_frame.pack(fill="x")
            self._refresh_user_list()
        else:
            self.one_user_frame.pack_forget()
            self.all_users_frame.pack(fill="x")

    def _load_used_machines(self) -> None:
        """Populate both machine autocompletes from the machine_keys already
        present in this index (`machines list --used`) -- the catalogue used
        for the Settings picker is a different, much larger list of machines
        this index has never actually seen a roast from."""
        buf: list[str] = []
        task = Task(argv=roastmesh_argv("--db", self.app.db_path.get(), "machines", "list", "--used", "--json"))
        task.start()
        stream_into(task, buf.append, lambda code: self._used_machines_loaded(code, buf),
                    lambda ms, fn: self.after(ms, fn))

    def _used_machines_loaded(self, code: int, buf: list[str]) -> None:
        if code != 0:
            return
        try:
            keys = parse_json_output("".join(buf))
        except json.JSONDecodeError:
            return
        self.machine.set_values(keys)
        self.user_machine.set_values(keys)

    def _build_args(self) -> list[str]:
        args = ["search"]
        text = self.query.get()
        if text:
            args.append(text)
        if self.mode.get() == self.MODE_ONE:
            if self.selected_user_pubkey:
                args += ["--user", self.selected_user_pubkey]
        else:
            if self.machine.get():
                args += ["--machine", self.machine.get()]
            if self.favorites_only.get():
                args.append("--favorites-only")
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
        # Pass the *restricting* flag explicitly. The CLI now defaults to
        # --all-peers, so sending nothing when this box is ticked would
        # silently turn the checkbox into a no-op.
        if self.lan_only.get():
            args.append("--lan-only")
        if self.own_only.get():
            args.append("--own-only")
        if self.show_hidden.get():
            args.append("--show-hidden")
        args.append("--json")
        return args

    def _on_run(self) -> None:
        if self.task is not None and self.task.running:
            return
        if self.mode.get() == self.MODE_ONE and not self.selected_user_pubkey:
            self.table.set_error(t("select a user above first"))
            return
        argv = roastmesh_argv("--db", self.app.db_path.get(), *self._build_args())
        self._output = []
        self.table.set_error(t("running..."))
        self.runbar.set_running(True, t("running..."))
        self.task = Task(argv=argv)
        self.task.start()
        stream_into(self.task, self._output.append, self._on_finished, lambda ms, fn: self.after(ms, fn))

    def _on_finished(self, code: int) -> None:
        self.runbar.set_running(False, t("done") if code == 0 else _exited_with_code(code))
        text = "".join(self._output)
        if code != 0:
            self.table.set_error(text.strip() or _exited_with_code(code))
            return
        try:
            rows = parse_json_output(text)
        except json.JSONDecodeError:
            self.table.set_error(t("could not parse results"))
            return
        self.table.set_rows(rows, unit=self.app.temp_unit.get())

    def _refresh_user_list(self) -> None:
        args = ["user", "list", "--json"]
        if self.user_machine.get():
            args += ["--machine", self.user_machine.get()]
        if self.user_favorites_only.get():
            args.append("--favorites")
        argv = roastmesh_argv("--db", self.app.db_path.get(), *args)
        buf: list[str] = []
        task = Task(argv=argv)
        task.start()
        stream_into(task, buf.append, lambda code: self._users_loaded(code, buf),
                    lambda ms, fn: self.after(ms, fn))

    def _users_loaded(self, code: int, buf: list[str]) -> None:
        if code != 0:
            self.user_status.set(t("Couldn't load users: {error}", error="".join(buf).strip()))
            return
        try:
            rows = parse_json_output("".join(buf))
        except json.JSONDecodeError:
            self.user_status.set(t("could not parse results"))
            return
        self._user_rows = {row["pubkey_hex"]: row for row in rows}
        previously_selected = self.selected_user_pubkey
        self.user_table.set_rows(rows)
        if previously_selected in self._user_rows:
            self.user_table.tree.selection_set(previously_selected)
        else:
            self.selected_user_pubkey = None
            self.favorite_btn.configure(state="disabled")
            self.like_btn.configure(state="disabled")

    def _on_user_selected(self, event: tk.Event | None = None) -> None:
        selection = self.user_table.tree.selection()
        if not selection:
            self.selected_user_pubkey = None
            self.favorite_btn.configure(state="disabled")
            self.like_btn.configure(state="disabled")
            return
        pubkey = selection[0]
        self.selected_user_pubkey = pubkey
        row = self._user_rows.get(pubkey, {})
        self.favorite_btn.configure(
            state="normal", text=t("Unfavorite") if row.get("is_favorite") else t("Favorite"),
        )
        self.like_btn.configure(state="normal", text=t("Like"))
        self._refresh_like_button_label(pubkey)
        self._on_run()

    def _refresh_like_button_label(self, pubkey: str) -> None:
        buf: list[str] = []
        task = Task(argv=roastmesh_argv("profile", "show", "--json"))
        task.start()
        stream_into(task, buf.append, lambda code: self._own_profile_loaded(code, buf, pubkey),
                    lambda ms, fn: self.after(ms, fn))

    def _own_profile_loaded(self, code: int, buf: list[str], pubkey: str) -> None:
        if code != 0 or self.selected_user_pubkey != pubkey:
            return  # a different row was selected before this landed
        try:
            profile = parse_json_output("".join(buf))
        except json.JSONDecodeError:
            return
        liked = pubkey in (profile.get("likes") or [])
        self.like_btn.configure(text=t("Unlike") if liked else t("Like"))

    def _on_toggle_user_favorite(self) -> None:
        pubkey = self.selected_user_pubkey
        if not pubkey:
            return
        now_favorite = not self._user_rows.get(pubkey, {}).get("is_favorite")
        command = "favorite" if now_favorite else "unfavorite"
        argv = roastmesh_argv("--db", self.app.db_path.get(), "user", command, pubkey)
        buf: list[str] = []
        task = Task(argv=argv)
        task.start()
        stream_into(task, buf.append, lambda code: self._on_user_favorite_toggled(code, buf, now_favorite),
                    lambda ms, fn: self.after(ms, fn))

    def _on_user_favorite_toggled(self, code: int, buf: list[str], now_favorite: bool) -> None:
        if code != 0:
            self.user_status.set(t("Couldn't change favorite status: {error}", error="".join(buf).strip()))
            return
        self.user_status.set(t("Favorited.") if now_favorite else t("Unfavorited."))
        self._refresh_user_list()

    def _on_toggle_user_like(self) -> None:
        pubkey = self.selected_user_pubkey
        if not pubkey:
            return
        now_liked = self.like_btn.cget("text") != t("Unlike")
        command = "like" if now_liked else "unlike"
        argv = roastmesh_argv("--db", self.app.db_path.get(), "user", command, pubkey)
        buf: list[str] = []
        task = Task(argv=argv)
        task.start()
        stream_into(task, buf.append, lambda code: self._on_user_like_toggled(code, buf, pubkey, now_liked),
                    lambda ms, fn: self.after(ms, fn))

    def _on_user_like_toggled(self, code: int, buf: list[str], pubkey: str, now_liked: bool) -> None:
        if code != 0:
            self.user_status.set(t("Couldn't change like status: {error}", error="".join(buf).strip()))
            return
        self.user_status.set(t("Liked.") if now_liked else t("Unliked."))
        self._refresh_user_list()
        self._refresh_like_button_label(pubkey)

    def _on_open_row(self, event: tk.Event) -> None:
        roast_id = self.table.tree.identify_row(event.y)
        if not roast_id:
            return
        argv = roastmesh_argv("--db", self.app.db_path.get(), "show", roast_id, "--json")
        buf: list[str] = []
        task = Task(argv=argv)
        task.start()
        stream_into(task, buf.append, lambda code: self._open_row_loaded(code, buf, roast_id),
                    lambda ms, fn: self.after(ms, fn))

    def _open_row_loaded(self, code: int, buf: list[str], roast_id: str) -> None:
        if code != 0:
            return
        try:
            payload = parse_json_output("".join(buf))
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
        self, parent: tk.Widget, app: "RoastmeshApp", roast_id: str, record: dict,
        raw_path: str | None, hidden: bool, *, on_change: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.roast_id = roast_id
        self.hidden = hidden
        self.on_change = on_change
        self.configure(bg=BG)
        self.geometry(screen_geometry(self, 1040, 820))
        # Same on Windows: the chart is the point of this window, and it reads
        # far better with the whole screen than in a 1040px box.
        widgets.maximize(self)
        beans_lines = (record.get("beans_text") or "").splitlines()
        title = record.get("title") or (beans_lines[0] if beans_lines else t("Roast detail"))
        self.title(title)

        unit = app.temp_unit.get()

        heading(self, title)
        RoastChart(self, record, unit=unit)

        info = ttk.Frame(self)
        info.pack(fill="x", padx=14, pady=(0, 6))

        def row(label: str, value) -> None:
            r = ttk.Frame(info)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=label, font=FONT_BOLD, bg=BG, fg=FG, width=18,
                     anchor="w").pack(side="left")
            tk.Label(r, text=str(value) if value not in (None, "") else t("?"), font=FONT_MONO,
                     bg=BG, fg=MUTED, anchor="w", wraplength=sp(600), justify="left").pack(side="left")

        row(t("Machine"), f"{record.get('machine_key')} ({record.get('roaster_type_raw')})")
        roast_type_value = (
            t("{roast_type} (estimated from peak temperature -- may not hold for every machine's probe)",
              roast_type=record["roast_type"])
        ) if record.get("roast_type") else None
        row(t("Roast type"), roast_type_value)

        batch_in = record.get("batch_weight_in_g")
        batch_out = record.get("batch_weight_out_g")
        batch_text = t("{in_g}g / {out_g}g", in_g=batch_in, out_g=batch_out)
        loss_pct = weight_loss_pct(batch_in, batch_out)
        if loss_pct is not None:
            batch_text += "   " + t("Weight loss: {pct:.1f}%", pct=loss_pct)
        row(t("Batch in / out"), batch_text)
        row(t("Roast date"), record.get("roast_date"))

        milestones = record.get("milestones") or []
        if milestones:
            tk.Label(self, text=t("Milestones"), font=FONT_H2, fg=FG, bg=BG, anchor="w").pack(
                fill="x", padx=14, pady=(10, 2))
            for m in milestones:
                time_text = format_mmss(m["time_s"]) if m.get("time_s") is not None else t("?")
                bt = units.convert_temp(m.get("bt_c"), unit)
                et = units.convert_temp(m.get("et_c"), unit)
                bt_text = f"{bt:.1f}°{unit}" if bt is not None else t("?")
                et_text = f"{et:.1f}°{unit}" if et is not None else t("?")
                # BT/ET are universal roasting notation, left untranslated
                # (same reasoning as gui/chart.py's legend).
                row(m.get("name") or t("?"), t("t={time}  BT={bt}  ET={et}", time=time_text, bt=bt_text, et=et_text))

        notes = record.get("roasting_notes") or record.get("cupping_notes")
        if notes:
            tk.Label(self, text=t("Notes"), font=FONT_H2, fg=FG, bg=BG, anchor="w").pack(
                fill="x", padx=14, pady=(10, 2))
            explain(self, notes)

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=14, pady=(12, 2))
        if raw_path:
            ttk.Button(btn_row, text=t("Open original file"),
                       command=lambda: self._on_open_file(raw_path)).pack(side="left")
            ttk.Button(btn_row, text=t("Copy path"),
                       command=lambda: _copy_to_clipboard(self, raw_path)).pack(side="left", padx=(6, 0))
        self.hide_button = ttk.Button(
            btn_row, text=(t("Unhide") if hidden else t("Hide")), command=self._on_toggle_hidden,
        )
        self.hide_button.pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text=t("Close"), command=self.destroy).pack(side="right")

        if raw_path:
            tk.Label(self, text=raw_path, font=FONT_MONO, fg=MUTED, bg=BG, anchor="w",
                     wraplength=sp(840), justify="left").pack(fill="x", padx=14, pady=(4, 0))

        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, font=("TkDefaultFont", 9), fg=MUTED,
                 bg=BG, anchor="w", wraplength=sp(840), justify="left").pack(fill="x", padx=14, pady=(2, 12))

    def _on_open_file(self, path: str) -> None:
        error = _open_alog_file(path)
        self.status_var.set(
            t("Couldn't open it: {error}. The file itself is at the path shown above.", error=error)
            if error else ""
        )

    def _on_toggle_hidden(self) -> None:
        command = "unhide" if self.hidden else "hide"
        argv = roastmesh_argv("--db", self.app.db_path.get(), command, self.roast_id)
        buf: list[str] = []
        task = Task(argv=argv)
        task.start()
        stream_into(task, buf.append, lambda code: self._on_hide_toggled(code, buf),
                    lambda ms, fn: self.after(ms, fn))

    def _on_hide_toggled(self, code: int, buf: list[str]) -> None:
        if code != 0:
            self.status_var.set(t("Couldn't change hidden status: {error}", error="".join(buf).strip()))
            return
        self.hidden = not self.hidden
        self.hide_button.configure(text=t("Unhide") if self.hidden else t("Hide"))
        self.status_var.set(t("Hidden from your own search results.") if self.hidden else t("Unhidden."))
        if self.on_change:
            self.on_change()


class PublishTab(Tab):
    """Append one of your own roasts to your signed feed. Second tab, per
    ARCHITECTURE.md's "publish second"."""

    def __init__(self, parent: tk.Widget, app: "RoastmeshApp") -> None:
        super().__init__(parent, app)
        heading(self, t("Publish"), t("Add one of your own roasts to your signed feed."))
        explain(self, t("Publishing appends a signed entry to your local feed -- your identity is "
                         "created silently the first time you publish, if you don't have one yet. "
                         "A peer only receives it once they sync with you, which the Network tab "
                         "now does on its own once you're serving."))

        identity_row = ttk.Frame(self)
        identity_row.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(identity_row, text=t("Feed address:"), font=FONT_BOLD, bg=BG, fg=FG).pack(side="left")
        self.identity_var = tk.StringVar(value=t("(loading...)"))
        tk.Label(identity_row, textvariable=self.identity_var, font=FONT_MONO, bg=BG,
                 fg=MUTED).pack(side="left", padx=(6, 0))

        folder_section = section(self, t("Shared folder (recommended)"))
        tk.Label(folder_section, text=t("Drop .alog files here and they're published automatically, "
                 "as long as the Network tab is serving -- no button to click per file:"),
                 font=("TkDefaultFont", 9), fg=MUTED, bg=BG, wraplength=sp(840), justify="left",
                 anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        folder_row = ttk.Frame(folder_section)
        folder_row.pack(fill="x", padx=10, pady=(0, 2))
        tk.Label(folder_row, textvariable=self.app.watch_dir, font=FONT_MONO, bg=BG, fg=FG).pack(
            side="left")
        ttk.Button(folder_row, text=t("Open folder"), command=self._open_watch_folder).pack(
            side="left", padx=(8, 0))
        ttk.Button(folder_row, text=t("Copy path"),
                   command=lambda: _copy_to_clipboard(self, self.app.watch_dir.get())).pack(
            side="left", padx=(6, 0))
        self.folder_status_var = tk.StringVar(value="")
        tk.Label(folder_section, textvariable=self.folder_status_var, font=("TkDefaultFont", 9),
                 fg=MUTED, bg=BG, anchor="w", wraplength=sp(840), justify="left").pack(
            fill="x", padx=10, pady=(0, 8))

        single_file_section = section(self, t("Publish a single file"))
        self.path_field = Field(single_file_section, t("File to publish"), help_text=t("An Artisan .alog file."))
        ttk.Button(single_file_section, text=t("Browse..."), command=self._browse).pack(
            padx=10, pady=(0, 6), anchor="w")

        self.runbar = RunBar(single_file_section, t("Publish"), self._on_run, self.cancel)
        self.console = Console(single_file_section)

        self._load_identity()

    def _open_watch_folder(self) -> None:
        path = self.app.watch_dir.get()
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.folder_status_var.set(t("Couldn't create {path}: {error}", path=path, error=exc))
            return
        error = _open_with_default_app(path)
        self.folder_status_var.set(
            t("Couldn't open it: {error}. The folder itself is at: {path}", error=error, path=path)
            if error else ""
        )

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            title=t("Choose a .alog file"),
            filetypes=[(t("Artisan roast log"), "*.alog"), (t("All files"), "*.*")],
        )
        if path:
            self.path_field.set(path)

    def _load_identity(self) -> None:
        buf: list[str] = []
        task = Task(argv=roastmesh_argv("identity", "show"))
        task.start()
        stream_into(task, buf.append, lambda code: self._identity_loaded(buf), lambda ms, fn: self.after(ms, fn))

    def _identity_loaded(self, buf: list[str]) -> None:
        lines = [line for line in "".join(buf).splitlines() if line.strip()]
        self.identity_var.set(lines[-1] if lines else t("(unknown)"))

    def _on_run(self) -> None:
        if self.task is not None and self.task.running:
            return
        path = self.path_field.get()
        if not path:
            self.console.clear()
            self.console.append(t("choose a .alog file first") + "\n")
            return
        argv = roastmesh_argv("--db", self.app.db_path.get(), "feed", "publish", path)
        self.console.clear()
        self.console.set_command(describe(argv))
        self.task = Task(argv=argv)
        self.runbar.set_running(True, t("running..."))
        self.task.start()
        stream_into(self.task, self.console.append, self._on_finished, lambda ms, fn: self.after(ms, fn))

    def _on_finished(self, code: int) -> None:
        self.runbar.set_running(False, t("done") if code == 0 else _exited_with_code(code))
        if code == 0:
            self._load_identity()


class NetworkTab(Tab):
    """Serve your feed to peers, and sync with theirs. Third tab -- the
    piece that makes cross-machine testing fully GUI-driven.

    Tracks two independent background processes (you can be serving and
    syncing at once), so it doesn't use the base Tab's single self.task --
    it overrides cancel() to stop both instead.
    """

    def __init__(self, parent: tk.Widget, app: "RoastmeshApp") -> None:
        super().__init__(parent, app)
        self.serve_task: Task | None = None
        self.sync_task: Task | None = None
        self.diag_task: Task | None = None
        self._closed = False

        heading(self, t("Network"), t("Serve your feed to peers, and pull theirs."))
        explain(self, t("The network is on automatically while this app is running -- peers on your "
                         "local network are found and synced with on their own, no clicking needed. "
                         "Internet-wide discovery (Settings tab) finds peers beyond your LAN the same "
                         "way, if you've turned it on. For a peer discovery won't reach, share the "
                         "ticket shown below with them, or paste theirs under 'Sync with a peer'. "
                         "Changes made in Settings apply the next time you Stop then Start serving."))

        serve_section = section(self, t("Serve your feed"))
        tk.Label(serve_section, text=t("Your ticket:"), font=FONT_BOLD, bg=BG, fg=FG).pack(
            anchor="w", padx=10, pady=(6, 0))
        ticket_row = ttk.Frame(serve_section)
        ticket_row.pack(fill="x", padx=10, pady=(0, 6))
        self.ticket_var = tk.StringVar(value="")
        self.ticket_entry = ttk.Entry(ticket_row, textvariable=self.ticket_var, state="readonly")
        self.ticket_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(ticket_row, text=t("Copy"), command=self._copy_ticket).pack(side="left", padx=(6, 0))
        self.serve_runbar = RunBar(serve_section, t("Start serving"), self._on_start_serve, self._on_stop_serve)
        self.serve_console = Console(serve_section, height=4)

        sync_section = section(self, t("Sync with a peer"))
        self.peer_ticket_field = Field(sync_section, t("Peer's ticket"),
                                        help_text=t("Paste the ticket they shared with you."))
        self.sync_runbar = RunBar(sync_section, t("Sync"), self._on_sync, self._on_cancel_sync)
        self.sync_console = Console(sync_section, height=4)

        diag_section = section(self, t("Internet discovery"))
        explain(diag_section, t(
            "Finding peers beyond your local network relies on the public BitTorrent DHT, "
            "and it is the one part of roastmesh that can fail completely while looking "
            "like it is working. These numbers update on their own while serving; "
            "\"Run a full check\" does a deeper one-off test that takes about a minute."))
        grid = ttk.Frame(diag_section)
        grid.pack(fill="x", padx=10, pady=(0, 6))
        self.diag_vars: dict[str, tk.StringVar] = {}
        for row, (key, label) in enumerate((
            ("status", t("Status")),
            ("external", t("This node, from outside")),
            ("nat", t("Your network")),
            ("table", t("Known DHT nodes")),
            ("lookup", t("Last lookup")),
            ("rejected", t("Forged nodes turned away")),
            ("announce", t("Published to")),
            ("findable", t("Findable by others")),
            ("peers", t("Peers on the swarm")),
            ("advice", t("What to do")),
        )):
            tk.Label(grid, text=label + ":", font=FONT_BOLD, bg=BG, fg=FG).grid(
                row=row, column=0, sticky="nw", padx=(0, 8), pady=1)
            var = tk.StringVar(value=t("waiting..."))
            self.diag_vars[key] = var
            tk.Label(grid, textvariable=var, bg=BG, fg=MUTED, anchor="w",
                     justify="left", wraplength=520).grid(row=row, column=1, sticky="w", pady=1)
        self.diag_runbar = RunBar(diag_section, t("Run a full check"),
                                  self._on_diag, self._on_cancel_diag)
        self.diag_console = Console(diag_section, height=3)

        peers_section = section(self, t("Known peers"))
        self.peers_table = PeerTable(peers_section)

        self._on_start_serve()
        self._schedule_peer_refresh()

    def cancel(self) -> None:
        self._closed = True
        if self.serve_task is not None:
            self.serve_task.cancel()
        if self.sync_task is not None:
            self.sync_task.cancel()
        if self.diag_task is not None:
            self.diag_task.cancel()

    # Every tick shells out to a whole new `roastmesh peer list` process (see
    # gui/runner.py's docstring for why the GUI shells out at all) -- cheap
    # as a one-off, but measured at ~0.12s of CPU from source and ~0.30s
    # under a packaged PyInstaller binary (onefile self-extraction on every
    # launch) on a fast desktop. A real bug: at the previous 5s interval
    # that's a genuinely continuous background load (~6-8% of a core on a
    # packaged build) for a "Known peers" table nobody is watching in real
    # time -- exactly the kind of small, constant, periodic CPU burst that
    # can keep a laptop's fan cycling even while the app just sits open.
    # 30s keeps the table reasonably live (LAN/WAN discovery still shows up
    # within half a minute with zero clicks) at roughly 1/6th the cost.
    _PEER_REFRESH_INTERVAL_MS = 30_000

    def _schedule_peer_refresh(self) -> None:
        # keeps "Known peers" (and thus visibility into LAN auto-discovery)
        # live without the user ever clicking anything -- self-reschedules
        # until the tab/app is torn down.
        if self._closed:
            return
        self._refresh_peers()
        self.after(self._PEER_REFRESH_INTERVAL_MS, self._schedule_peer_refresh)

    def _copy_ticket(self) -> None:
        ticket = self.ticket_var.get()
        if not ticket:
            return
        self.clipboard_clear()
        self.clipboard_append(ticket)

    def _on_start_serve(self) -> None:
        if self.serve_task is not None and self.serve_task.running:
            return
        argv = roastmesh_argv("--db", self.app.db_path.get(), "node", "serve",
                              "--publish-watch-dir", self.app.watch_dir.get())
        if self.app.wan_discovery_enabled.get():
            argv.append("--wan-discovery")
            # Both, and the same number: --wan-port is the socket we listen on,
            # --public-port is what we tell other nodes to use. A forward is
            # only useful if the port it delivers to is the one we are on.
            port = self.app.public_port.get().strip()
            if port.isdigit():
                argv += ["--wan-port", port, "--public-port", port]
        self.serve_console.clear()
        self.serve_console.set_command(describe(argv))
        self.ticket_var.set("")
        self.serve_task = Task(argv=argv)
        self.serve_runbar.set_running(True, t("starting..."))
        self.serve_task.start()
        # `node serve` runs for the app's whole lifetime now that serving
        # auto-starts (unlike a short one-off search/publish/sync), so
        # stream_into's default 120ms poll would fire roughly 30,000 times
        # an hour just to check an almost-always-empty output queue --
        # each firing is individually cheap, but that many small, constant
        # wakeups is exactly the pattern that can keep a CPU from ever
        # reaching a deeper idle state. A second's delay in an occasional
        # "watch: published..." log line showing up is imperceptible.
        stream_into(self.serve_task, self._on_serve_output, self._on_serve_finished,
                    lambda ms, fn: self.after(ms, fn), interval_ms=1000)

    def _on_serve_output(self, text: str) -> None:
        self.serve_console.append(text)
        for line in text.splitlines():
            # "ticket: " is the one text contract between the CLI and the
            # GUI (net.py's serve() prints it, in English, always) -- never
            # translate this literal, or the ticket box stays empty forever.
            if line.startswith("ticket: "):
                self.ticket_var.set(line[len("ticket: "):].strip())
                self.serve_runbar.set_running(True, t("serving"))
            # The second CLI->GUI text contract, same shape as "ticket: " and
            # for the same reason: the diagnostics are already computed inside
            # the serving process every round, so reading them off its output
            # costs nothing, where polling would mean a whole extra process
            # per refresh (see _refresh_peers' note on that cost).
            elif line.startswith("wan-stats: "):
                try:
                    self._apply_diagnostics(json.loads(line[len("wan-stats: "):]))
                except (ValueError, TypeError, KeyError):
                    pass

    def _on_stop_serve(self) -> None:
        if self.serve_task is not None:
            self.serve_task.cancel()

    def _on_serve_finished(self, code: int) -> None:
        self.serve_runbar.set_running(False, t("stopped") if code == 0 else t("stopped (exit {code})", code=code))
        self.ticket_var.set("")

    def _on_sync(self) -> None:
        if self.sync_task is not None and self.sync_task.running:
            return
        ticket = self.peer_ticket_field.get()
        if not ticket:
            self.sync_console.clear()
            self.sync_console.append(t("paste a peer's ticket first") + "\n")
            return
        argv = roastmesh_argv("--db", self.app.db_path.get(), "peer", "sync", ticket)
        self.sync_console.clear()
        self.sync_console.set_command(describe(argv))
        self.sync_task = Task(argv=argv)
        self.sync_runbar.set_running(True, t("syncing..."))
        self.sync_task.start()
        stream_into(self.sync_task, self.sync_console.append, self._on_sync_finished,
                    lambda ms, fn: self.after(ms, fn))

    def _on_cancel_sync(self) -> None:
        if self.sync_task is not None:
            self.sync_task.cancel()

    def _on_sync_finished(self, code: int) -> None:
        self.sync_runbar.set_running(False, t("done") if code == 0 else _exited_with_code(code))
        self._refresh_peers()

    def _on_diag(self) -> None:
        if self.diag_task is not None and self.diag_task.running:
            return
        argv = roastmesh_argv("node", "doctor", "--json")
        self.diag_console.clear()
        self.diag_console.set_command(describe(argv))
        buf: list[str] = []
        self.diag_task = Task(argv=argv)
        self.diag_runbar.set_running(True, t("checking..."))
        self.diag_task.start()
        stream_into(self.diag_task, buf.append,
                    lambda code: self._diag_finished(code, buf),
                    lambda ms, fn: self.after(ms, fn))

    def _on_cancel_diag(self) -> None:
        if self.diag_task is not None:
            self.diag_task.cancel()

    def _diag_finished(self, code: int, buf: list[str]) -> None:
        self.diag_runbar.set_running(False, t("done") if code == 0 else _exited_with_code(code))
        try:
            self._apply_diagnostics(parse_json_output("".join(buf)))
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            self.diag_console.append(t("could not read the diagnostic report") + "\n")

    def _apply_diagnostics(self, r: dict) -> None:
        """Render one diagnostics payload -- from the live `wan-stats:` line or
        from `node doctor --json`, which emit the same keys by construction
        (wan_discovery.diagnostics_payload)."""
        v = self.diag_vars
        lookup = r.get("lookup") or {}
        closest = lookup.get("closest_bits")
        readback = r.get("readback")
        nat = r.get("nat")

        if r.get("external_ip"):
            v["external"].set("{}:{}  {}".format(
                r["external_ip"], r.get("external_port"),
                t("({n} independent reports agree)", n=r.get("ip_votes", 0))))
        else:
            v["external"].set(t("unknown -- not enough nodes have reported it back yet"))

        if nat == "symmetric":
            v["nat"].set(t("symmetric or carrier-grade NAT -- other nodes cannot open a "
                            "connection to you, so internet discovery cannot work here. "
                            "LAN peers and pasted tickets still work."))
        elif nat == "consistent":
            v["nat"].set(t("stable address. Whether a stranger's first packet gets through "
                            "depends on your router's filtering, which this cannot measure -- "
                            "a stable address that still drops unsolicited packets is common."))
        else:
            v["nat"].set(t("not known yet"))

        table = r.get("routing_table") or {}
        v["table"].set(t("{good} good of {total}, {verified} identity-verified",
                          good=table.get("good", 0), total=table.get("total", 0),
                          verified=table.get("verified", 0)))

        v["lookup"].set(t("{rounds} rounds, {replied} of {queried} answered, closest {closest}",
                           rounds=lookup.get("rounds", 0), replied=lookup.get("replied", 0),
                           queried=lookup.get("queried", 0),
                           closest=("-" if closest is None else "2^{}".format(closest))))

        v["rejected"].set(t("{forged} forging closeness, {bep42} unverifiable, {martian} unroutable",
                             forged=lookup.get("rejected_impossible_proximity", 0),
                             bep42=lookup.get("rejected_bep42", 0),
                             martian=lookup.get("rejected_martian", 0)))

        announce_set = r.get("announce_set") or []
        verified = sum(1 for a in announce_set if a.get("bep42") is True)
        v["announce"].set(t("{announced} node(s); {verified} of the {total} closest are verified",
                             announced=lookup.get("announced", 0), verified=verified,
                             total=len(announce_set)))

        if readback is True:
            v["findable"].set(t("yes -- a fresh lookup found this node's own address"))
        elif readback is False:
            v["findable"].set(t("NO -- we published, but a fresh lookup could not find us"))
        else:
            v["findable"].set(t("not checked yet"))

        peers = r.get("peers") or []
        v["peers"].set(", ".join(peers) if peers else t("none advertised right now"))

        if r.get("needs_public_port"):
            port = r.get("public_port")
            if port:
                v["advice"].set(t("Port {port} is set, but a fresh lookup still could not "
                                   "find this machine -- so it is not actually open. Check "
                                   "the forward really points here.", port=port))
            else:
                v["advice"].set(t("This machine cannot be found by others until a port is "
                                   "forwarded to it. Put that port in Settings under "
                                   "\"Forwarded port\". Your router's port-forwarding page "
                                   "has it, or your VPN if it offers one. You can still find "
                                   "and sync with other people meanwhile."))
        else:
            v["advice"].set("")

        if nat == "symmetric":
            status = t("blocked by this network")
        elif readback is True:
            status = t("healthy -- reachable and findable")
        elif readback is False:
            status = t("published, but not findable")
        elif closest is not None and closest > 150:
            status = t("not converging -- cannot reach the swarm")
        elif not r.get("warm", True):
            status = t("warming up")
        else:
            status = t("running")
        v["status"].set(status)

    def _refresh_peers(self) -> None:
        buf: list[str] = []
        task = Task(argv=roastmesh_argv("peer", "list", "--json"))
        task.start()
        stream_into(task, buf.append, lambda code: self._peers_loaded(buf), lambda ms, fn: self.after(ms, fn))

    def _peers_loaded(self, buf: list[str]) -> None:
        try:
            peers = parse_json_output("".join(buf))
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

    def __init__(self, parent: tk.Widget, app: "RoastmeshApp") -> None:
        super().__init__(parent, app)
        # Scrollable: this tab has grown a section at a time (watch folder,
        # WAN discovery, temperature unit, now language) and, confirmed on
        # a real screenshot, the bottom section is now clipped with no way
        # to reach it even on a large display -- scrollable() (widgets.py)
        # already existed for exactly this, just unused until now.
        container = scrollable(self)
        heading(container, t("Settings"), t("Where things live, and how far discovery reaches."))

        you_section = section(container, t("You"))
        explain(you_section, t("Your name and declared machine are shown to peers once they sync with "
                                "you -- cosmetic only, never required, and never trusted for uniqueness. "
                                "Saved when you leave the field or press Enter, not on every keystroke, "
                                "since each save re-signs your profile."))
        self.name_field = Field(you_section, t("Display name"), variable=self.app.display_name,
                                 help_text=t("Shown to peers who sync with you."))
        self.name_field.entry.bind("<FocusOut>", self._on_profile_field_changed)
        self.name_field.entry.bind("<Return>", self._on_profile_field_changed)
        self.machine_field = AutocompleteField(
            you_section, t("Your machine"), variable=self.app.own_machine,
            help_text=t("Your declared roaster -- also used as a fallback machine filter for your own "
                        "roasts that have none of their own. Type a machine not in the list to use a "
                        "custom one."))
        self.machine_field.combo.bind("<FocusOut>", self._on_profile_field_changed)
        self.machine_field.combo.bind("<Return>", self._on_profile_field_changed)
        self.machine_field.combo.bind("<<ComboboxSelected>>", self._on_profile_field_changed)
        self.you_status = tk.StringVar(value="")
        tk.Label(you_section, textvariable=self.you_status, font=("TkDefaultFont", 9), fg=MUTED,
                 bg=BG, anchor="w").pack(fill="x", padx=10, pady=(0, 8))
        self._machine_by_display: dict[str, str] = {}
        self._load_profile()
        self._load_machine_catalogue()

        db_section = section(container, t("Database file"))
        explain(db_section, t("Where your local search index lives. Search, Publish, and Network "
                               "all use this. Existing tabs pick up a change the next time they run."))
        self.db_field = Field(db_section, t("Path"), variable=self.app.db_path, width=60)
        ttk.Button(db_section, text=t("Browse..."), command=self._browse_db).pack(
            padx=10, pady=(0, 8), anchor="w")

        watch_section = section(container, t("Shared publish folder"))
        explain(watch_section, t("Any .alog file dropped here is published automatically while "
                                  "the Network tab is serving -- see the Publish tab."))
        self.watch_field = Field(watch_section, t("Path"), variable=self.app.watch_dir, width=60)
        ttk.Button(watch_section, text=t("Browse..."), command=self._browse_watch_dir).pack(
            padx=10, pady=(0, 8), anchor="w")

        wan_section = section(container, t("Internet-wide discovery"))
        explain(wan_section,
                t("On by default. LAN discovery only ever broadcasts on your local network; this "
                  "also finds and syncs with roastmesh peers anywhere on the internet, the same way "
                  "a BitTorrent client finds peers with no tracker of its own: by announcing on the "
                  "public BitTorrent DHT, a huge, already-running public network -- no server of "
                  "roastmesh's own involved. The trade-off: your public IP address (and the fact "
                  "that it's running roastmesh) becomes visible to anyone else looking at that same "
                  "swarm, which a LAN broadcast never exposes. Uncheck this if you'd rather only "
                  "ever be found on your local network. Takes effect immediately -- serving "
                  "restarts on its own when you change this, so the ticket on the Network "
                  "tab will change."))
        ttk.Checkbutton(wan_section, text=t("Find peers over the whole internet, not just my LAN"),
                         variable=self.app.wan_discovery_enabled).pack(anchor="w", padx=10, pady=(0, 8))
        port_field = Field(wan_section, t("Forwarded port (optional)"), variable=self.app.public_port,
              help_text=t("Leave empty unless your router or VPN forwards a port to this "
                          "machine. Some networks give every outgoing connection a different "
                          "port, so the address other people see is not one they can reach -- "
                          "then you need a forwarded port to be findable at all. The Network "
                          "tab says so outright when that is the case, and 'Run a full check' "
                          "will tell you whether the port you entered actually works."))
        # On commit, not on every keystroke: this restarts the serving process,
        # and the auto-save trace every other Settings field uses fires per
        # character -- typing "26513" would have restarted the node five times.
        for event in ("<FocusOut>", "<Return>"):
            port_field.entry.bind(event, lambda *_e: self.app._apply_discovery_change())

        unit_section = section(container, t("Temperature unit"))
        explain(unit_section,
                t("Controls how temperatures are shown everywhere in the app -- search results, "
                  "the roast detail chart, and its stats. Roasts are always parsed, stored, and "
                  "searched in Celsius internally, no matter what's picked here -- this only "
                  "changes what you see on screen. An open search or detail window picks up a "
                  "change the next time it re-runs (a new search, reopening a roast)."))
        unit_row = ttk.Frame(unit_section)
        unit_row.pack(anchor="w", padx=10, pady=(0, 8))
        ttk.Radiobutton(unit_row, text=t("Celsius (°C)"), value=units.CELSIUS,
                        variable=self.app.temp_unit).pack(side="left")
        ttk.Radiobutton(unit_row, text=t("Fahrenheit (°F)"), value=units.FAHRENHEIT,
                        variable=self.app.temp_unit).pack(side="left", padx=(12, 0))

        language_section = section(container, t("Language"))
        explain(language_section,
                t("Changes what every label, button, and help text in this app is shown in. "
                  "Takes effect the next time you open roastmesh -- an already-open window keeps "
                  "its current language."))
        language_row = ttk.Frame(language_section)
        language_row.pack(anchor="w", padx=10, pady=(0, 8))
        # Native names, never translated -- a user who picks the wrong
        # language by mistake must still be able to read their way back.
        for code, (native_name, _is_plural) in i18n.LANGUAGES.items():
            ttk.Radiobutton(language_row, text=native_name, value=code,
                            variable=self.app.language).pack(side="left", padx=(0, 12))

        scale_section = section(container, t("Display size"))
        explain(scale_section,
                t("Currently {pct}% -- scales every label, button, and chart together. "
                  "Detected from this screen's resolution by default; Ctrl+scroll (or "
                  "Ctrl+plus/Ctrl+minus) to adjust it, Ctrl+0 to go back to auto-detect. "
                  "Restarts roastmesh to apply, the same as Stop-then-Start serving does.",
                  pct=round(widgets.UI_SCALE * 100)))

    def _browse_db(self) -> None:
        path = filedialog.asksaveasfilename(
            title=t("Choose a database file"), defaultextension=".sqlite3",
            filetypes=[(t("SQLite database"), "*.sqlite3"), (t("All files"), "*.*")],
        )
        if path:
            self.app.db_path.set(path)

    def _browse_watch_dir(self) -> None:
        path = filedialog.askdirectory(title=t("Choose a folder to auto-publish from"))
        if path:
            self.app.watch_dir.set(path)

    def _load_profile(self) -> None:
        buf: list[str] = []
        task = Task(argv=roastmesh_argv("--db", self.app.db_path.get(), "profile", "show", "--json"))
        task.start()
        stream_into(task, buf.append, lambda code: self._profile_loaded(code, buf),
                    lambda ms, fn: self.after(ms, fn))

    def _profile_loaded(self, code: int, buf: list[str]) -> None:
        if code != 0:
            return
        try:
            profile = parse_json_output("".join(buf))
        except json.JSONDecodeError:
            return
        self.app.display_name.set(profile.get("name") or "")
        self.app.own_machine.set(profile.get("machine_display") or profile.get("machine_key") or "")

    def _load_machine_catalogue(self) -> None:
        buf: list[str] = []
        task = Task(argv=roastmesh_argv("--db", self.app.db_path.get(), "machines", "list", "--json"))
        task.start()
        stream_into(task, buf.append, lambda code: self._catalogue_loaded(code, buf),
                    lambda ms, fn: self.after(ms, fn))

    def _catalogue_loaded(self, code: int, buf: list[str]) -> None:
        if code != 0:
            return
        try:
            catalogue = parse_json_output("".join(buf))
        except json.JSONDecodeError:
            return
        # Several catalogue entries share the same machine_key (e.g. three
        # Aillio Bullet variants all collapse to "aillio_bullet") -- show
        # the human-readable display_name in the dropdown, and remember
        # only the first key for each so a selection resolves unambiguously.
        values: list[str] = []
        for m in catalogue:
            display = m.get("display_name") or m.get("key")
            if display and display not in self._machine_by_display:
                self._machine_by_display[display] = m.get("key")
                values.append(display)
        self.machine_field.set_values(values)

    def _on_profile_field_changed(self, event: tk.Event | None = None) -> None:
        """Save name/machine on focus-out or Return -- deliberately NOT
        wired to the trace_add auto-save every other Settings field uses
        (see RoastmeshApp.__init__): that fires on every keystroke, and
        each write here shells out to `profile set`, which re-signs
        profile.json."""
        name = self.app.display_name.get().strip()
        machine_text = self.app.own_machine.get().strip()
        args = ["--db", self.app.db_path.get(), "profile", "set"]
        if name:
            args += ["--name", name]
        if machine_text:
            key = self._machine_by_display.get(machine_text)
            if key:
                args += ["--machine", key]
            else:
                args += ["--machine-custom", machine_text]
        if not name and not machine_text:
            return  # nothing to save yet
        argv = roastmesh_argv(*args)
        buf: list[str] = []
        task = Task(argv=argv)
        task.start()
        stream_into(task, buf.append, lambda code: self._on_profile_saved(code, buf),
                    lambda ms, fn: self.after(ms, fn))

    def _on_profile_saved(self, code: int, buf: list[str]) -> None:
        if code != 0:
            self.you_status.set(t("Couldn't save: {error}", error="".join(buf).strip()))
            return
        self.you_status.set(t("Saved."))


class RoastmeshApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        cfg = gui_config.load_config()
        # None until a user overrides it via Ctrl+scroll/Ctrl+plus/minus --
        # kept separately from widgets.UI_SCALE (the resolved, in-effect
        # number) so _save_config can round-trip "auto-detect" (None)
        # rather than freezing in whatever this screen happened to resolve
        # to. See gui/config.py's ui_scale field.
        self._ui_scale_override = cfg.ui_scale

        # Every font in this app (and gui/chart.py's Canvas text) is
        # specified in points, so this one call is what actually makes
        # them all bigger together -- see UI_SCALE's docstring in
        # widgets.py for why this can't just be auto-detected from the
        # display's reported DPI, and must instead be resolved (env var >
        # persisted override > this screen's resolution) before any widget
        # is created, or already-built widgets keep their original size --
        # this includes gui/chart.py, whose margins are computed from
        # sp() at each chart's own construction time specifically so they
        # pick up whatever is resolved here.
        widgets.set_scale(widgets.resolve_ui_scale(self, self._ui_scale_override))
        try:
            self.tk.call("tk", "scaling", self.tk.call("tk", "scaling") * widgets.UI_SCALE)
        except tk.TclError:
            pass
        self.title("roastmesh")
        self.geometry(screen_geometry(self, 900, 680))
        # Maximized on Windows (no-op elsewhere): the geometry above is a
        # sensible size, but Windows users expect a desktop app to open filling
        # the screen, and the search results table has more columns than a
        # 900px window shows comfortably.
        widgets.maximize(self)
        self.configure(bg=BG)
        try:
            style = ttk.Style(self)
            style.theme_use("clam")
            # "clam"'s Treeview row height is a fixed pixel value baked
            # into the theme, not derived from the active font -- confirmed
            # on a real 4K display: it stayed at 20px after the 3x
            # font-scaling bump above (which needs ~55px of linespace),
            # clipping almost every row's text down to unreadable
            # fragments (only the outer edges of each glyph fit). Recompute
            # it from the font actually in use, now that scaling is set.
            row_font = tkfont.nametofont("TkDefaultFont")
            style.configure("Treeview", rowheight=round(row_font.metrics("linespace") * 1.3))
        except tk.TclError:
            pass

        # Must happen before any tab is built below -- every widget label is
        # baked in at construction time (see gui/i18n.py's module docstring
        # for why a language switch applies on next launch rather than
        # rebuilding live).
        i18n.set_language(i18n.resolve_language(cfg.language))

        self.db_path = tk.StringVar(value=cfg.db_path)
        self.watch_dir = tk.StringVar(value=cfg.watch_dir)
        self.wan_discovery_enabled = tk.BooleanVar(value=cfg.wan_discovery_enabled)
        self.public_port = tk.StringVar(value=cfg.public_port)
        self.temp_unit = tk.StringVar(value=cfg.temp_unit)
        self.language = tk.StringVar(value=i18n.current_language())
        for var in (self.db_path, self.watch_dir, self.wan_discovery_enabled, self.public_port,
                    self.temp_unit, self.language):
            var.trace_add("write", lambda *_args: self._save_config())

        # Your own profile (display name, declared machine) -- NOT part of
        # the trace_add loop above. Those write to gui_config.json on every
        # keystroke; these two are backed by `profile set` instead (see
        # SettingsTab._on_profile_field_changed), which re-signs
        # profile.json on every call, so they save on focus-out/Return only.
        self.display_name = tk.StringVar(value="")
        self.own_machine = tk.StringVar(value="")

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=6, pady=6)

        search_tab = SearchTab(notebook, self)
        publish_tab = PublishTab(notebook, self)
        network_tab = NetworkTab(notebook, self)
        settings_tab = SettingsTab(notebook, self)
        notebook.add(search_tab, text=t("Search"))
        notebook.add(publish_tab, text=t("Publish"))
        notebook.add(network_tab, text=t("Network"))
        notebook.add(settings_tab, text=t("Settings"))
        self.tabs: list[Tab] = [search_tab, publish_tab, network_tab, settings_tab]
        self.network_tab = network_tab
        # Ticking "find peers over the whole internet" has to actually turn it
        # on. Serving auto-starts at launch and `--wan-discovery` is decided
        # once, right then (NetworkTab._on_start_serve), so until now the
        # checkbox changed nothing until the user also found Stop-then-Start on
        # another tab. Nobody reads a checkbox that way -- it cost a real user
        # an evening of "it's supposed to be on" while their node never
        # announced itself.
        self.wan_discovery_enabled.trace_add(
            "write", lambda *_args: self._apply_discovery_change())

        # Resizing (Ctrl+scroll or Ctrl+plus/minus, Ctrl+0 to go back to
        # auto-detect) restarts the whole app rather than re-laying-out live
        # -- see _relaunch_with_scale's docstring for why. bind_all so it
        # works no matter which tab/widget has focus. Both wheel conventions
        # are bound: X11 sends Button-4/5, while Windows, macOS and some
        # Wayland/XWayland Tk builds send MouseWheel.
        #
        # These belong here, in __init__, and nowhere else. They spent two
        # releases misplaced inside _apply_discovery_change, which meant they
        # were bound only if the user toggled internet discovery *while
        # serving* -- so Ctrl+scroll silently did nothing on a normal launch,
        # and the tests missed it by calling _relaunch_with_scale directly
        # instead of checking that anything was bound to reach it.
        self.bind_all("<Control-MouseWheel>", self._on_scale_wheel)
        self.bind_all("<Control-Button-4>", lambda _e: self._nudge_scale(widgets.SCALE_STEP))
        self.bind_all("<Control-Button-5>", lambda _e: self._nudge_scale(-widgets.SCALE_STEP))
        for seq in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            self.bind_all(seq, lambda _e: self._nudge_scale(widgets.SCALE_STEP))
        for seq in ("<Control-minus>", "<Control-KP_Subtract>"):
            self.bind_all(seq, lambda _e: self._nudge_scale(-widgets.SCALE_STEP))
        for seq in ("<Control-0>", "<Control-KP_0>"):
            self.bind_all(seq, lambda _e: self._relaunch_with_scale(None))

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_discovery_change(self) -> None:
        """Restart serving so the new discovery setting takes effect now."""
        tab = getattr(self, "network_tab", None)
        if tab is None or tab.serve_task is None or not tab.serve_task.running:
            return  # not serving yet; the next start picks it up anyway
        tab._on_stop_serve()
        # Give the old process a moment to release its ports before rebinding.
        self.after(800, tab._on_start_serve)

    def _on_scale_wheel(self, event: tk.Event) -> None:
        self._nudge_scale(widgets.SCALE_STEP if event.delta > 0 else -widgets.SCALE_STEP)

    def _nudge_scale(self, delta: float) -> None:
        self._relaunch_with_scale(widgets.UI_SCALE + delta)

    def _relaunch_with_scale(self, new_scale: float | None) -> None:
        """Persist `new_scale` (None = go back to auto-detecting from this
        screen's resolution) and restart the whole process so it takes
        effect.

        Not applied live: UI_SCALE/LINE_SCALE feed sp()/lw() calls scattered
        across every tab (Field widths, Treeview column widths, wraplength,
        gui/chart.py's hand-drawn margins/line widths), almost all baked
        into widget configuration once at construction time -- there's no
        single place to re-apply a new value to everything already built
        short of re-running that construction from scratch. A full restart
        does exactly that for free, correctly, using the same code path
        every other launch already goes through -- no separate "live
        rescale" logic to maintain and independently verify. The cost is a
        brief blip (a couple hundred ms) and, if Network was serving, a
        fresh ticket -- the same trade-off Stop-then-Start already asks of
        a user changing Settings, just automatic here."""
        if new_scale is not None:
            new_scale = max(widgets.MIN_UI_SCALE, min(widgets.MAX_UI_SCALE, new_scale))
        self._ui_scale_override = new_scale
        self._save_config()
        # Only *request* the restart here. Actually doing it from inside a Tk
        # callback is what made this crash on Windows: `sys.exit()` raises
        # SystemExit through the Tcl call stack, where Tkinter catches it and
        # reports a traceback instead of exiting -- so the window was already
        # destroyed, the process stayed alive, and the replacement it had just
        # spawned immediately quit again because the single-instance guard saw
        # the old process still holding the port. POSIX never showed this: execv
        # replaces the process outright, so there is no callback to return to
        # and no overlap. main() performs the restart after mainloop() ends.
        self._relaunch_requested = True
        self._on_close()

    def _save_config(self) -> None:
        gui_config.save_config(gui_config.GuiConfig(
            db_path=self.db_path.get(), watch_dir=self.watch_dir.get(),
            wan_discovery_enabled=self.wan_discovery_enabled.get(),
            public_port=self.public_port.get().strip(),
            temp_unit=self.temp_unit.get(),
            language=self.language.get(),
            ui_scale=self._ui_scale_override,
        ))

    def _on_close(self) -> None:
        for tab in self.tabs:
            tab.cancel()
        self.destroy()


def main(*, single_instance_port: int = single_instance.PORT) -> None:
    if single_instance.another_instance_is_running(port=single_instance_port):
        return  # asked it to focus itself instead -- nothing else to do here

    app = RoastmeshApp()

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

    # Windows accepts this call but never actually delivers SIGTERM (Task
    # Manager "End task" is a TerminateProcess, which gives no notification at
    # all), so the orphaned-`node serve` protection described above is POSIX
    # only. On Windows the equivalent guarantee comes from cancel()'s
    # `taskkill /T`, which reaches the child tree when the window closes
    # normally. Guarded rather than assumed, so a future reader isn't misled.
    if hasattr(signal, "SIGTERM") and sys.platform != "win32":
        signal.signal(signal.SIGTERM, _handle_terminate)

    focus_requests: queue.Queue = queue.Queue()
    # start_focus_listener's callback runs on a background thread -- Tk
    # widgets may only be touched from the thread that created them, so
    # it just drops a marker in the queue and _poll_focus_requests (on
    # the Tk main thread, via app.after, same pattern gui/runner.py's
    # stream_into uses for task output) is what actually raises the
    # window.
    listener = single_instance.start_focus_listener(
        lambda: focus_requests.put(None), port=single_instance_port)

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

    if getattr(app, "_relaunch_requested", False):
        # Restart to apply a new interface scale (see _relaunch_with_scale).
        # Done here, after mainloop has ended, rather than from the callback
        # that requested it.
        #
        # Releasing the single-instance port first is essential on Windows:
        # there the replacement runs *alongside* this process for a moment,
        # and its startup probe would find this one still listening and exit
        # immediately -- the app would simply disappear on Ctrl+scroll. POSIX
        # never hit this because execv replaces the process in place.
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if sys.platform == "win32":
            subprocess.Popen(
                [sys.executable, *sys.argv[1:]],
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            return
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
