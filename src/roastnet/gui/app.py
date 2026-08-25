"""roastnet desktop GUI.

Two tabs, in the order ARCHITECTURE.md's build order names them: Search
first, Publish second. Every action shells out to the same `roastnet` CLI a
terminal user would run -- see gui/runner.py for why -- and the exact
command is shown above its output.

Peer/node management (roastnet peer/node) stays CLI-only for this step --
not named by "search first, publish second," and adding it here would be
scope creep.
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
    ResultsTable,
    RunBar,
    explain,
    heading,
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
        notebook.add(search_tab, text="Search")
        notebook.add(publish_tab, text="Publish")
        self.tabs: list[Tab] = [search_tab, publish_tab]

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
