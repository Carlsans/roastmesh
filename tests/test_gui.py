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
import queue
import shutil
import signal
import subprocess
import sys
import threading
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


def test_all_tabs_construct_and_can_be_selected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # Isolated HOME: constructing RoastnetApp auto-starts a real `node
    # serve` (Network tab is always-on) and reads/writes gui/config.py's
    # settings file -- without this it would touch the real user's actual
    # ~/.local/share/roastnet and create a real ~/RoastNetShare folder.
    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
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
    assert "TABS 4" in r.stdout, r.stdout


def test_search_tab_runs_a_real_search_and_populates_the_table(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "gui.sqlite3"
    conn = connect(db_path)
    results = ingest_path(conn, FIXTURES_DIR)
    conn.close()
    assert all(r.error is None for r in results)

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastnet.gui.app import RoastnetApp
app = RoastnetApp()
app.db_path.set({str(db_path)!r})
app.update()
tab = app.tabs[0]
tab._on_run()
for _ in range(200):
    app.update()
    # wait for the actual side effect, not just the thread's running flag --
    # that flag can flip False slightly before the scheduled stream_into
    # callback (which is what actually populates the table) has run.
    if tab.task is not None and not tab.task.running and tab.table.count_var.get() != "running...":
        break
    import time; time.sleep(0.05)
rows = tab.table.tree.get_children()
print("ROWS", len(rows))
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert f"ROWS {len(results)}" in r.stdout, r.stdout


def test_search_tab_columns_show_title_and_filename_not_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "gui.sqlite3"
    conn = connect(db_path)
    ingest_path(conn, FIXTURES_DIR / "kaleido_1.alog")
    conn.close()

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastnet.gui.app import RoastnetApp
app = RoastnetApp()
app.db_path.set({str(db_path)!r})
app.update()
tab = app.tabs[0]
print("COLUMNS", tab.table.tree["columns"])
tab._on_run()
for _ in range(200):
    app.update()
    if tab.task is not None and not tab.task.running and tab.table.count_var.get() != "running...":
        break
    import time; time.sleep(0.05)
row_id = tab.table.tree.get_children()[0]
print("VALUES", tab.table.tree.item(row_id)["values"])
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "'roast_id'" not in r.stdout
    columns_line = [line for line in r.stdout.splitlines() if line.startswith("COLUMNS")][0]
    assert "title" in columns_line and "filename" in columns_line
    values_line = [line for line in r.stdout.splitlines() if line.startswith("VALUES")][0]
    assert "kaleido_1.alog" in values_line  # the real filename, not a content hash or an id


def test_search_tab_lan_only_checkbox_is_checked_by_default_and_toggles_the_flag(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastnet.gui.app import RoastnetApp
app = RoastnetApp()
app.update()
tab = app.tabs[0]
print("DEFAULT_CHECKED", tab.lan_only.get())
print("DEFAULT_ARGS", tab._build_args())
tab.lan_only.set(False)
print("UNCHECKED_ARGS", tab._build_args())
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "DEFAULT_CHECKED True" in r.stdout, r.stdout
    assert "--all-peers" not in [line for line in r.stdout.splitlines() if line.startswith("DEFAULT_ARGS")][0]
    assert "--all-peers" in [line for line in r.stdout.splitlines() if line.startswith("UNCHECKED_ARGS")][0]


def test_search_tab_own_only_checkbox_is_unchecked_by_default_and_toggles_the_flag(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastnet.gui.app import RoastnetApp
app = RoastnetApp()
app.update()
tab = app.tabs[0]
print("DEFAULT_CHECKED", tab.own_only.get())
print("DEFAULT_ARGS", tab._build_args())
tab.own_only.set(True)
print("CHECKED_ARGS", tab._build_args())
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "DEFAULT_CHECKED False" in r.stdout, r.stdout
    assert "--own-only" not in [line for line in r.stdout.splitlines() if line.startswith("DEFAULT_ARGS")][0]
    assert "--own-only" in [line for line in r.stdout.splitlines() if line.startswith("CHECKED_ARGS")][0]


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
    # wait for the console to actually have content, not just the thread's
    # running flag -- that can flip False before the scheduled stream_into
    # callback that writes to the console has run.
    if tab.task is not None and not tab.task.running and tab.console.get_text().strip():
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


def test_copy_to_clipboard_puts_the_text_on_the_real_clipboard() -> None:
    # A manual fallback that must keep working regardless of whether the
    # desktop has anything registered to auto-open a file/folder with.
    r = _run_headless("""
import tkinter as tk
from roastnet.gui.app import _copy_to_clipboard
root = tk.Tk()
root.withdraw()
_copy_to_clipboard(root, '/tmp/some/path/roast.alog')
root.update()
print('CLIPBOARD', repr(root.clipboard_get()))
root.destroy()
print('OK')
""")
    assert "OK" in r.stdout, r.stderr
    assert "CLIPBOARD '/tmp/some/path/roast.alog'" in r.stdout, r.stdout


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


def test_network_tab_start_stop_serving_produces_and_clears_a_ticket(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastnet.gui.app import RoastnetApp
app = RoastnetApp()
app.update()
tab = app.tabs[2]
# serving starts automatically on tab construction -- no _on_start_serve()
# call here, just wait for the ticket that auto-start produces
for _ in range(200):
    app.update()
    if tab.ticket_var.get():
        break
    import time; time.sleep(0.05)
ticket = tab.ticket_var.get()
print("TICKET_LEN", len(ticket))
assert ticket.startswith("endpoint"), ticket

tab._on_stop_serve()
for _ in range(100):
    app.update()
    # wait for both the task to actually stop AND the scheduled
    # stream_into callback that clears the ticket to have run -- the
    # thread's running flag can flip slightly before that callback fires
    # on a later app.update() cycle.
    if tab.serve_task is not None and not tab.serve_task.running and not tab.ticket_var.get():
        break
    import time; time.sleep(0.05)
print("STILL_RUNNING", tab.serve_task.running if tab.serve_task else None)
print("TICKET_AFTER_STOP", repr(tab.ticket_var.get()))
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "STILL_RUNNING False" in r.stdout, r.stdout
    assert "TICKET_AFTER_STOP ''" in r.stdout, r.stdout
    ticket_len_line = [l for l in r.stdout.splitlines() if l.startswith("TICKET_LEN")][0]
    assert int(ticket_len_line.split()[1]) > 0


def _start_server_process(env: dict, feed_fixture: Path) -> tuple[subprocess.Popen, str]:
    """Publish one entry and start a real `roastnet node serve` for a test
    to sync against. Reads stdout in a background thread into a queue with
    a bounded wait, rather than a plain blocking readline() loop, so a
    server that never prints a ticket fails the test cleanly instead of
    hanging it."""
    subprocess.run(
        [sys.executable, "-m", "roastnet.cli", "feed", "publish", str(feed_fixture)],
        # cwd=env["HOME"]: `feed publish` now also ingests into --db, which
        # defaults to a cwd-relative path -- without pinning cwd here, that
        # lands as a stray roastnet.sqlite3 in whatever directory pytest
        # itself was invoked from, not this test's isolated tmp_path.
        env=env, cwd=env["HOME"], check=True, capture_output=True, text=True, timeout=30,
    )
    proc = subprocess.Popen(
        # --no-lan-discovery: the GUI's own NetworkTab now auto-starts a
        # local server with LAN discovery on (that's the feature), which
        # would otherwise race with this test's explicit sync call over
        # the real network this sandbox is connected to -- this test is
        # about manual sync through the GUI, not LAN discovery (that has
        # its own dedicated test in test_net.py).
        [sys.executable, "-m", "roastnet.cli", "node", "serve", "--no-lan-discovery"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    ticket_queue: queue.Queue[str] = queue.Queue()

    def _reader() -> None:
        for line in proc.stdout:
            if line.startswith("ticket: "):
                ticket_queue.put(line[len("ticket: "):].strip())
                return

    threading.Thread(target=_reader, daemon=True).start()
    ticket = ticket_queue.get(timeout=15)
    return proc, ticket


def test_network_tab_syncs_with_a_real_peer_end_to_end(tmp_path: Path) -> None:
    server_env = {**os.environ, "HOME": str(tmp_path / "server_home")}
    (tmp_path / "server_home").mkdir()
    proc, ticket = _start_server_process(server_env, FIXTURES_DIR / "kaleido_1.alog")

    try:
        client_home = tmp_path / "client_home"
        client_home.mkdir()
        db_path = tmp_path / "client.sqlite3"

        r = _run_headless(f"""
import os
os.environ["HOME"] = {str(client_home)!r}
from roastnet.gui.app import RoastnetApp
app = RoastnetApp()
app.db_path.set({str(db_path)!r})
app.update()
tab = app.tabs[2]
tab.peer_ticket_field.set({ticket!r})
tab._on_sync()
for _ in range(200):
    app.update()
    if tab.sync_task is not None and not tab.sync_task.running and tab.sync_console.get_text().strip():
        break
    import time; time.sleep(0.05)
print(tab.sync_console.get_text())
for _ in range(50):
    app.update()
    if tab.peers_table.tree.get_children():
        break
    import time; time.sleep(0.05)
print("PEER_ROWS", len(tab.peers_table.tree.get_children()))
app._on_close()
print("OK")
""", timeout=60)
        assert "OK" in r.stdout, r.stderr
        assert "1 new entries" in r.stdout, r.stdout
        assert "PEER_ROWS 1" in r.stdout, r.stdout

        conn = connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
        conn.close()
        assert count == 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _gui_launch_argv(port: int) -> list[str]:
    body = (
        "from roastnet.gui.app import main\n"
        f"main(single_instance_port={port})\n"
    )
    cmd = [sys.executable, "-c", body]
    if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", *cmd]
    return cmd


def test_second_launch_focuses_the_first_instead_of_opening_a_second_window(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    port = 41998  # dedicated test port, distinct from the real single_instance.PORT

    first = subprocess.Popen(_gui_launch_argv(port), env=env)
    try:
        # wait for the first instance to actually bind its focus listener,
        # not just for the process to start (Tk/Xvfb startup isn't instant)
        from roastnet.gui import single_instance
        bound = False
        for _ in range(100):
            if single_instance.another_instance_is_running(port=port, timeout=0.2):
                bound = True
                break
            time.sleep(0.2)
        assert bound, "first instance never started listening for focus requests"
        assert first.poll() is None, "first instance exited unexpectedly"

        second = subprocess.run(_gui_launch_argv(port), env=env, timeout=20)
        assert second.returncode == 0
        # the first instance is still the one and only instance running
        assert first.poll() is None
    finally:
        first.terminate()
        try:
            first.wait(timeout=5)
        except subprocess.TimeoutExpired:
            first.kill()


def test_sigterm_cleans_up_the_background_node_serve_process(tmp_path: Path) -> None:
    """A plain SIGTERM (e.g. `kill`, a session manager logging the user
    out -- anything that isn't the window's own close button) must still
    clean up the background `node serve` process the Network tab
    auto-starts. Otherwise it's a permanent orphan: still serving,
    discovering, and auto-publishing with no visible app left. This was a
    real bug found during development -- repeated manual test runs (a
    plain subprocess.Popen(...).terminate(), same as this test does) left
    real orphaned node serve processes running indefinitely on a real
    machine, because WM_DELETE_WINDOW (and the os.killpg cleanup wired to
    it) never fires for a signal that isn't a window-manager close event.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    port = 41995
    marker = f"node serve --publish-watch-dir {home}"

    def _node_serve_running() -> bool:
        check = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        return bool(check.stdout.strip())

    proc = subprocess.Popen(_gui_launch_argv(port), env=env)
    try:
        from roastnet.gui import single_instance
        bound = False
        for _ in range(100):
            if single_instance.another_instance_is_running(port=port, timeout=0.2):
                bound = True
                break
            time.sleep(0.2)
        assert bound, "instance never started listening for focus requests"

        running = False
        for _ in range(50):
            if _node_serve_running():
                running = True
                break
            time.sleep(0.2)
        assert running, "node serve never started under the GUI instance"

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

        time.sleep(1)  # give a leaked orphan, if any, a moment to still be visible
        assert not _node_serve_running(), "node serve leaked as an orphan after SIGTERM"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        subprocess.run(["pkill", "-f", marker])  # belt and braces if the assertion above failed


def test_hide_button_removes_the_roast_and_unhide_restores_it(tmp_path: Path) -> None:
    """Real end-to-end drive of the actual double-click-to-detail-window
    path (via a real computed row bbox under Xvfb, not a shortcut around
    it), then the Hide button, confirming the roast actually disappears
    from a re-run search, and Unhide brings it back."""
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "gui.sqlite3"
    conn = connect(db_path)
    ingest_path(conn, FIXTURES_DIR / "kaleido_1.alog")
    conn.close()

    r = _run_headless(f"""
import os, time
os.environ["HOME"] = {str(home)!r}
from roastnet.gui.app import RoastnetApp
app = RoastnetApp()
app.db_path.set({str(db_path)!r})
app.update()
tab = app.tabs[0]

def run_search():
    tab._on_run()
    for _ in range(200):
        app.update()
        if tab.task is not None and not tab.task.running and tab.table.count_var.get() != "running...":
            break
        time.sleep(0.05)

run_search()
print("COUNT_BEFORE_HIDE", tab.table.count_var.get())

row_id = tab.table.tree.get_children()[0]
x, y, w, h = tab.table.tree.bbox(row_id)

class FakeEvent:
    pass

event = FakeEvent()
event.y = y + h // 2
tab._on_open_row(event)
for _ in range(100):
    app.update()
    if getattr(tab, "_last_detail_window", None) is not None:
        break
    time.sleep(0.05)
detail = tab._last_detail_window
print("INITIAL_BUTTON_TEXT", detail.hide_button.cget("text"))

detail._on_toggle_hidden()
for _ in range(100):
    app.update()
    if detail.hide_button.cget("text") == "Unhide":
        break
    time.sleep(0.05)
print("BUTTON_TEXT_AFTER_HIDE", detail.hide_button.cget("text"))

run_search()
print("COUNT_AFTER_HIDE", tab.table.count_var.get())

tab.show_hidden.set(True)
run_search()
print("COUNT_WITH_SHOW_HIDDEN", tab.table.count_var.get())

detail._on_toggle_hidden()
for _ in range(100):
    app.update()
    if detail.hide_button.cget("text") == "Hide":
        break
    time.sleep(0.05)

tab.show_hidden.set(False)
run_search()
print("COUNT_AFTER_UNHIDE", tab.table.count_var.get())

app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "COUNT_BEFORE_HIDE 1 result" in r.stdout, r.stdout
    assert "INITIAL_BUTTON_TEXT Hide" in r.stdout, r.stdout
    assert "BUTTON_TEXT_AFTER_HIDE Unhide" in r.stdout, r.stdout
    assert "COUNT_AFTER_HIDE 0 result" in r.stdout, r.stdout
    assert "COUNT_WITH_SHOW_HIDDEN 1 result" in r.stdout, r.stdout
    assert "COUNT_AFTER_UNHIDE 1 result" in r.stdout, r.stdout
