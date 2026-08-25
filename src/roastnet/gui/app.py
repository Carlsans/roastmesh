"""roastnet desktop GUI.

Three tabs: Search and Publish, in the order ARCHITECTURE.md's build order
names them ("search first, publish second"), then Network -- start serving,
sync with a peer, see who you know -- which makes the actual point of the
project (talking to another machine) fully driveable from the GUI instead
of needing the CLI for it. Every action shells out to the same `roastnet`
CLI a terminal user would run -- see gui/runner.py for why -- and the exact
command is shown above its output.
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import filedialog, ttk

from roastnet.gui.runner import Task, describe, roastnet_argv, stream_into
from roastnet.gui.widgets import (
    BG,
    FG,
    FONT_BOLD,
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
from roastnet.index.db import default_db_path


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
                       "The filters below narrow further -- leave any blank to not filter on it.")

        self.query = Field(self, "Text", help_text="Free-text search, e.g. 'washed ethiopian'.")
        self.machine = Field(self, "Machine", help_text="Exact machine_key, e.g. kaleido_m2.")
        self.roast_type = Field(self, "Roast type", help_text="e.g. 'full city', 'vienna'.")
        self.dtr_min = Field(self, "DTR min %", width=8)
        self.dtr_max = Field(self, "DTR max %", width=8)
        self.drop_after = Field(self, "Drop after (°C)", width=8)
        self.second_crack = Choice(self, "After second crack?", ["any", "yes", "no"], default="any")

        self._output: list[str] = []
        self.runbar = RunBar(self, "Search", self._on_run, self.cancel)
        self.table = ResultsTable(self)

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


class PublishTab(Tab):
    """Append one of your own roasts to your signed feed. Second tab, per
    ARCHITECTURE.md's "publish second"."""

    def __init__(self, parent: tk.Widget, app: "RoastnetApp") -> None:
        super().__init__(parent, app)
        heading(self, "Publish", "Add one of your own roasts to your signed feed.")
        explain(self, "Publishing appends a signed entry to your local feed -- your identity is "
                       "created silently the first time you publish, if you don't have one yet. "
                       "A peer only receives it once they sync with you (roastnet peer sync), which "
                       "stays a command-line-only operation for now.")

        identity_row = ttk.Frame(self)
        identity_row.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(identity_row, text="Feed address:", font=FONT_BOLD, bg=BG, fg=FG).pack(side="left")
        self.identity_var = tk.StringVar(value="(loading...)")
        tk.Label(identity_row, textvariable=self.identity_var, font=FONT_MONO, bg=BG,
                 fg=MUTED).pack(side="left", padx=(6, 0))

        self.path_field = Field(self, "File to publish", help_text="An Artisan .alog file.")
        ttk.Button(self, text="Browse...", command=self._browse).pack(padx=10, pady=(0, 6), anchor="w")

        self.runbar = RunBar(self, "Publish", self._on_run, self.cancel)
        self.console = Console(self)

        self._load_identity()

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
                       "For a peer that isn't on the same local network, share the ticket shown "
                       "below with them, or paste theirs under 'Sync with a peer' to reach them "
                       "directly.")

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
        argv = roastnet_argv("--db", self.app.db_path.get(), "node", "serve")
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

        top = ttk.Frame(self)
        top.pack(fill="x")
        self.db_path = Field(top, "Database file", default=str(default_db_path()),
                              help_text="Where your local search index lives.", width=60)
        ttk.Button(top, text="Browse...", command=self._browse_db).pack(padx=10, pady=(0, 6), anchor="w")

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=6, pady=6)

        search_tab = SearchTab(notebook, self)
        publish_tab = PublishTab(notebook, self)
        network_tab = NetworkTab(notebook, self)
        notebook.add(search_tab, text="Search")
        notebook.add(publish_tab, text="Publish")
        notebook.add(network_tab, text="Network")
        self.tabs: list[Tab] = [search_tab, publish_tab, network_tab]

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _browse_db(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Choose a database file", defaultextension=".sqlite3",
            filetypes=[("SQLite database", "*.sqlite3"), ("All files", "*.*")],
        )
        if path:
            self.db_path.set(path)

    def _on_close(self) -> None:
        for tab in self.tabs:
            tab.cancel()
        self.destroy()


def main() -> None:
    RoastnetApp().mainloop()


if __name__ == "__main__":
    main()
