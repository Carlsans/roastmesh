"""GUI tests.

These run headless under Xvfb when available and skip otherwise, so the
suite still passes on a machine with no display -- ported from roastlab's
tests/test_gui.py (same author's sibling project), which established this
pattern: test the things that actually break a GUI (does every tab
construct, does a real command run end to end, does cancel work) rather
than pixel layout.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

from roastnet.feed import read_entries
from roastnet.gui.runner import Task
from roastnet.index.db import connect
from roastnet.index.ingest import ingest_path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY")) or shutil.which("Xvfb") is not None


pytestmark = pytest.mark.skipif(not _has_display(), reason="no X display and no Xvfb")

HEADLESS = """
import sys
sys.argv = ["test"]
{body}
"""


def _run_headless(body: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a snippet in a real (virtual) X session.

    A subprocess is used rather than creating Tk in-process because tkinter
    does not reliably tolerate repeated create/destroy cycles of the root
    window inside one interpreter, which would make test ORDER affect
    results.
    """
    cmd = [sys.executable, "-c", HEADLESS.format(body=body)]
    if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", *cmd]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def test_both_tabs_construct_and_can_be_selected() -> None:
    r = _run_headless("""
from roastnet.gui.app import RoastnetApp
app = RoastnetApp()
app.update()
nb = [c for c in app.winfo_children() if c.winfo_class() == "TNotebook"][0]
for i in range(len(app.tabs)):
    nb.select(i)
    app.update()
print("TABS", len(app.tabs))
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "TABS 2" in r.stdout, r.stdout


def test_search_tab_runs_a_real_search_and_populates_the_table(tmp_path: Path) -> None:
    db_path = tmp_path / "gui.sqlite3"
    conn = connect(db_path)
    results = ingest_path(conn, FIXTURES_DIR)
    conn.close()
    assert all(r.error is None for r in results)

    r = _run_headless(f"""
from roastnet.gui.app import RoastnetApp
app = RoastnetApp()
app.db_path.set({str(db_path)!r})
app.update()
tab = app.tabs[0]
tab._on_run()
for _ in range(200):
    app.update()
    if tab.task is not None and not tab.task.running:
        break
    import time; time.sleep(0.05)
rows = tab.table.tree.get_children()
print("ROWS", len(rows))
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert f"ROWS {len(results)}" in r.stdout, r.stdout


def test_publish_tab_publishes_a_real_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fixture = FIXTURES_DIR / "kaleido_1.alog"

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastnet.gui.app import RoastnetApp
app = RoastnetApp()
app.update()
tab = app.tabs[1]
for _ in range(100):
    app.update()
    if tab.identity_var.get() != "(loading...)":
        break
    import time; time.sleep(0.05)
tab.path_field.set({str(fixture)!r})
tab._on_run()
for _ in range(200):
    app.update()
    if tab.task is not None and not tab.task.running:
        break
    import time; time.sleep(0.05)
print(tab.console.get_text())
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "published entry 0" in r.stdout, r.stdout

    feed_dir = home / ".local" / "share" / "roastnet" / "feed"
    entries = read_entries(feed_dir)
    assert len(entries) == 1
    assert entries[0].content_sha256


def test_cancel_stops_a_running_task_quickly() -> None:
    task = Task(argv=[sys.executable, "-c", "import time; time.sleep(30)"])
    task.start()
    time.sleep(0.3)
    assert task.running
    start = time.monotonic()
    task.cancel()
    while task.running and time.monotonic() - start < 5:
        time.sleep(0.05)
    assert not task.running
    assert time.monotonic() - start < 5
