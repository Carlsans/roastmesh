"""GUI tests.

These run headless under Xvfb when available and skip otherwise, so the
suite still passes on a machine with no display -- ported from roastlab's
tests/test_gui.py (same author's sibling project), which established this
pattern: test the things that actually break a GUI (does every tab
construct, does a real command run end to end, does cancel work) rather
than pixel layout.
"""
from __future__ import annotations

import ast
import json
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

from roastmesh.feed import read_entries
from roastmesh.gui.runner import Task
from roastmesh.index.db import connect
from roastmesh.index.ingest import ingest_path

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
        # This system's xvfb-run defaults to a 640x480 virtual screen --
        # confirmed as the root cause of a real intermittent failure: the
        # search results Treeview's requested size (~850x400) didn't fit
        # inside the app's own default 900x680 window once packed under a
        # tab bar/heading at that resolution, leaving it unmapped
        # (winfo_ismapped() == 0) so Treeview.bbox() returned nothing for
        # an otherwise perfectly real, populated row. 1920x1080 comfortably
        # fits this app's window at any of its resolution-based UI scales
        # (gui/widgets.py's detect_ui_scale) -- a laptop-sized screen is
        # exactly the scenario this whole feature is about, not a corner
        # case to shrink away.
        cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1920x1080x24", *cmd]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def test_all_tabs_construct_and_can_be_selected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # Isolated HOME: constructing RoastmeshApp auto-starts a real `node
    # serve` (Network tab is always-on) and reads/writes gui/config.py's
    # settings file -- without this it would touch the real user's actual
    # ~/.local/share/roastmesh and create a real ~/RoastMeshShare folder.
    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
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


def test_ui_scale_env_var_overrides_a_persisted_config_value(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".local" / "share" / "roastmesh").mkdir(parents=True)
    (home / ".local" / "share" / "roastmesh" / "gui_config.json").write_text('{"ui_scale": 1.6}')
    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
os.environ["ROASTMESH_UI_SCALE"] = "2.0"
from roastmesh.gui import widgets
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.update()
print("UI_SCALE", widgets.UI_SCALE)
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "UI_SCALE 2.0" in r.stdout, r.stdout


def test_relaunch_for_scale_brings_the_app_back_up(tmp_path: Path) -> None:
    """Changing the interface scale must actually restart the app -- through
    main(), which is where the restart really happens.

    This goes via main() rather than calling _relaunch_with_scale on a bare
    app, because everything that broke on Windows lived in the parts a bare
    app skips. The restart used to be performed from inside the Tk callback:
    sys.exit() there raises SystemExit through the Tcl stack, where Tkinter
    reports a traceback instead of exiting, so the window was destroyed, the
    process stayed alive, and the replacement it had just spawned quit again
    immediately because the single-instance guard found the old one still
    holding the port. POSIX hid all of it -- execv replaces the process, so
    there is no callback to return to and no overlap.

    The proof is that the second instance gets far enough to write the file:
    if the guard turns it away, nothing is written.
    """
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "relaunched.txt"
    port = 41977
    script = tmp_path / "relaunch_probe.py"
    script.write_text(f"""
import os, sys
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui import config as gui_config
from roastmesh.gui import widgets
from roastmesh.gui import app as appmod

MARKER = {str(marker)!r}
_orig = appmod.RoastmeshApp.__init__

def _patched(self):
    _orig(self)
    if gui_config.load_config().ui_scale == 2.5:
        with open(MARKER, "w", encoding="utf-8") as fh:
            fh.write(f"scale={{widgets.UI_SCALE}} tabs={{len(self.tabs)}}")
        self.after(200, self._on_close)
    else:
        self.after(700, lambda: self._relaunch_with_scale(2.5))

appmod.RoastmeshApp.__init__ = _patched
appmod.main(single_instance_port={port})
""")
    cmd = [sys.executable, str(script)]
    if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1920x1080x24", *cmd]
    subprocess.run(cmd, capture_output=True, text=True, timeout=90)

    # The relaunched instance is a separate process on Windows, so it can
    # outlive the one we waited on -- poll rather than assume it has finished.
    for _ in range(60):
        if marker.exists():
            break
        time.sleep(0.5)

    assert marker.exists(), (
        "the relaunched instance never started -- on Windows this is the "
        "single-instance guard seeing the old process still holding the port"
    )
    assert "scale=2.5" in marker.read_text(encoding="utf-8")
    assert "tabs=4" in marker.read_text(encoding="utf-8")

    saved = json.loads((home / ".local" / "share" / "roastmesh" / "gui_config.json").read_text())
    assert saved["ui_scale"] == 2.5


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
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
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


def test_search_tab_columns_show_title_and_roast_date_not_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "gui.sqlite3"
    conn = connect(db_path)
    ingest_path(conn, FIXTURES_DIR / "kaleido_1.alog")
    conn.close()

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
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
    assert "title" in columns_line and "roast_date" in columns_line
    assert "filename" not in columns_line
    values_line = [line for line in r.stdout.splitlines() if line.startswith("VALUES")][0]
    assert "2024-05-02" in values_line  # kaleido_1.alog's roastisodate


def test_search_tab_results_sort_by_clicking_a_column_header(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "gui.sqlite3"
    conn = connect(db_path)
    ingest_path(conn, FIXTURES_DIR)  # real fixtures: machine_key values hottop/kaleido_serial/unknown
    conn.close()

    r = _run_headless(f"""
import os, time
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.db_path.set({str(db_path)!r})
app.update()
tab = app.tabs[0]
tab._on_run()
for _ in range(200):
    app.update()
    if tab.task is not None and not tab.task.running and tab.table.count_var.get() != "running...":
        break
    time.sleep(0.05)

def machine_values():
    return [tab.table.tree.set(iid, "machine_key") for iid in tab.table.tree.get_children()]

print("UNSORTED_HEADING", tab.table.tree.heading("machine_key")["text"])

tab.table._on_heading_click("machine_key")
print("ASCENDING", machine_values())
print("ASCENDING_HEADING", tab.table.tree.heading("machine_key")["text"])

tab.table._on_heading_click("machine_key")
print("DESCENDING", machine_values())
print("DESCENDING_HEADING", tab.table.tree.heading("machine_key")["text"])

app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "UNSORTED_HEADING Machine" in r.stdout, r.stdout  # no arrow before any click
    lines = {line.split(" ", 1)[0]: line for line in r.stdout.splitlines()}
    ascending = ast.literal_eval(lines["ASCENDING"].split(" ", 1)[1])
    descending = ast.literal_eval(lines["DESCENDING"].split(" ", 1)[1])
    assert ascending == sorted(ascending)
    assert descending == sorted(descending, reverse=True)
    assert "▲" in lines["ASCENDING_HEADING"]
    assert "▼" in lines["DESCENDING_HEADING"]


def test_search_tab_lan_only_checkbox_is_unchecked_by_default_and_toggles_the_flag(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.update()
tab = app.tabs[0]
print("DEFAULT_CHECKED", tab.lan_only.get())
print("DEFAULT_ARGS", tab._build_args())
tab.lan_only.set(True)
print("CHECKED_ARGS", tab._build_args())
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "DEFAULT_CHECKED False" in r.stdout, r.stdout
    # The CLI now defaults to --all-peers, so the GUI passes the *restricting*
    # flag: nothing by default, --lan-only only when the box is ticked.
    assert "--lan-only" not in [line for line in r.stdout.splitlines() if line.startswith("DEFAULT_ARGS")][0]
    assert "--lan-only" in [line for line in r.stdout.splitlines() if line.startswith("CHECKED_ARGS")][0]


def test_search_tab_own_only_checkbox_is_unchecked_by_default_and_toggles_the_flag(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
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


def test_search_tab_favorites_only_checkbox_is_unchecked_by_default_and_toggles_the_flag(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.update()
tab = app.tabs[0]
print("DEFAULT_CHECKED", tab.favorites_only.get())
print("DEFAULT_ARGS", tab._build_args())
tab.favorites_only.set(True)
print("CHECKED_ARGS", tab._build_args())
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "DEFAULT_CHECKED False" in r.stdout, r.stdout
    assert "--favorites-only" not in [line for line in r.stdout.splitlines() if line.startswith("DEFAULT_ARGS")][0]
    assert "--favorites-only" in [line for line in r.stdout.splitlines() if line.startswith("CHECKED_ARGS")][0]


def test_search_tab_mode_selector_changes_build_args(tmp_path: Path) -> None:
    """Default mode is "all users" (existing tests above rely on this).
    Switching to "one user" and selecting a pubkey swaps --machine/
    --favorites-only (the all-users filters) for --user."""
    home = tmp_path / "home"
    home.mkdir()
    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.update()
tab = app.tabs[0]
print("MODE_ALL", tab.MODE_ALL)
print("MODE_ONE", tab.MODE_ONE)
print("DEFAULT_MODE", tab.mode.get())
tab.machine.set("kaleido_serial")
tab.favorites_only.set(True)
print("ALL_ARGS", tab._build_args())
tab.mode.set(tab.MODE_ONE)
tab.selected_user_pubkey = "deadbeef" * 8
print("ONE_ARGS", tab._build_args())
tab.selected_user_pubkey = None
print("ONE_ARGS_NO_SELECTION", tab._build_args())
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    lines = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in r.stdout.splitlines() if " " in line}
    assert lines["DEFAULT_MODE"] == lines["MODE_ALL"]
    assert "'--machine', 'kaleido_serial'" in lines["ALL_ARGS"]
    assert "--favorites-only" in lines["ALL_ARGS"]
    # In "one user" mode the all-users filters (machine/favorites-only) are
    # replaced by --user -- they must not leak through.
    assert "'--user', 'deadbeef" in lines["ONE_ARGS"]
    assert "--machine" not in lines["ONE_ARGS"]
    assert "--favorites-only" not in lines["ONE_ARGS"]
    # With no user selected yet, --user must not appear (there's nothing to
    # search for -- _on_run guards this case separately).
    assert "--user" not in lines["ONE_ARGS_NO_SELECTION"]


def test_search_tab_selecting_a_user_emits_user_flag_and_lists_their_roast(tmp_path: Path) -> None:
    """Real end-to-end drive of "one user" mode: ingest a roast under a real
    identity, list users, select the row, and confirm both _build_args()
    and the actual search results reflect that one user."""
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "gui.sqlite3"
    fixture = FIXTURES_DIR / "kaleido_1.alog"
    env = {**os.environ, "HOME": str(home)}
    subprocess.run(
        [sys.executable, "-m", "roastmesh.cli", "--db", str(db_path), "ingest", str(fixture), "--user-log"],
        env=env, capture_output=True, text=True, check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "roastmesh.cli", "--db", str(db_path), "profile", "set", "--name", "Test Roaster"],
        env=env, capture_output=True, text=True, check=True,
    )
    show = subprocess.run(
        [sys.executable, "-m", "roastmesh.cli", "--db", str(db_path), "profile", "show", "--json"],
        env=env, capture_output=True, text=True, check=True,
    )
    pubkey = json.loads(show.stdout)["pubkey"]

    r = _run_headless(f"""
import os, time
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.db_path.set({str(db_path)!r})
app.update()
tab = app.tabs[0]
tab.mode.set(tab.MODE_ONE)
tab._on_mode_changed()
for _ in range(150):
    app.update()
    if tab.user_table.tree.get_children():
        break
    time.sleep(0.02)
print("USER_ROWS", len(tab.user_table.tree.get_children()))

tab.user_table.tree.selection_set({pubkey!r})
for _ in range(150):
    app.update()
    if tab.selected_user_pubkey == {pubkey!r}:
        break
    time.sleep(0.02)
print("SELECTED", tab.selected_user_pubkey)
print("BUILD_ARGS", tab._build_args())

for _ in range(200):
    app.update()
    if tab.task is not None and not tab.task.running and tab.table.count_var.get() not in ("", "running..."):
        break
    time.sleep(0.02)
print("RESULT_ROWS", len(tab.table.tree.get_children()))
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "USER_ROWS 1" in r.stdout, r.stdout
    assert f"SELECTED {pubkey}" in r.stdout, r.stdout
    build_args_line = [line for line in r.stdout.splitlines() if line.startswith("BUILD_ARGS")][0]
    assert "'--user'" in build_args_line and pubkey in build_args_line
    assert "RESULT_ROWS 1" in r.stdout, r.stdout


def test_settings_tab_name_and_machine_round_trip_through_profile(tmp_path: Path) -> None:
    """Settings' "You" fields save on focus-out/Return (via `profile set`),
    never through the per-keystroke trace_add auto-save the other Settings
    fields use -- and the value shown is seeded from `profile show`."""
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "gui.sqlite3"
    env = {**os.environ, "HOME": str(home)}

    r = _run_headless(f"""
import os, time
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.db_path.set({str(db_path)!r})
app.update()
tab = app.tabs[3]
# Poll for the value rather than sleeping a fixed 3s: the field is seeded
# by a background `profile show` subprocess, and on a loaded machine that
# does not always finish inside a fixed window -- which made this test fail
# about one run in six. The loop exits as soon as the value lands, so a
# generous ceiling costs nothing when things are healthy.
#
# The ceiling must stay comfortably under _run_headless's own subprocess
# timeout, or a slow machine trades one failure for a worse one: the whole
# interpreter is killed mid-run and the assertion that reports *why* never
# gets to speak. Polling ~20s inside a 90s harness leaves that margin.
for _ in range(1000):
    app.update()
    if tab.app.display_name.get():
        break
    time.sleep(0.02)
print("LOADED_NAME", repr(tab.app.display_name.get()))
tab.app.display_name.set("Amber Chaff")
tab.app.own_machine.set("Some Custom Rig")
tab._on_profile_field_changed()
for _ in range(100):
    app.update()
    if tab.you_status.get():
        break
    time.sleep(0.02)
print("STATUS", tab.you_status.get())
app._on_close()
print("OK")
""", timeout=90)
    assert "OK" in r.stdout, r.stderr
    # A default (deterministic) name was already there before we typed
    # anything -- confirms the field is seeded from `profile show`, not left
    # blank.
    loaded_line = [line for line in r.stdout.splitlines() if line.startswith("LOADED_NAME")][0]
    assert loaded_line != "LOADED_NAME ''"
    assert "STATUS Saved." in r.stdout, r.stdout

    show = subprocess.run(
        [sys.executable, "-m", "roastmesh.cli", "--db", str(db_path), "profile", "show", "--json"],
        env=env, capture_output=True, text=True, check=True,
    )
    profile = json.loads(show.stdout)
    assert profile["name"] == "Amber Chaff"
    assert profile["machine_display"] == "Some Custom Rig"


def test_publish_tab_publishes_a_real_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fixture = FIXTURES_DIR / "kaleido_1.alog"

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
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

    feed_dir = home / ".local" / "share" / "roastmesh" / "feed"
    entries = read_entries(feed_dir)
    assert len(entries) == 1
    assert entries[0].content_sha256


def test_copy_to_clipboard_puts_the_text_on_the_real_clipboard() -> None:
    # A manual fallback that must keep working regardless of whether the
    # desktop has anything registered to auto-open a file/folder with.
    r = _run_headless("""
import tkinter as tk
from roastmesh.gui.app import _copy_to_clipboard
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
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
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
    """Publish one entry and start a real `roastmesh node serve` for a test
    to sync against. Reads stdout in a background thread into a queue with
    a bounded wait, rather than a plain blocking readline() loop, so a
    server that never prints a ticket fails the test cleanly instead of
    hanging it."""
    subprocess.run(
        [sys.executable, "-m", "roastmesh.cli", "feed", "publish", str(feed_fixture)],
        # cwd=env["HOME"]: `feed publish` now also ingests into --db, which
        # defaults to a cwd-relative path -- without pinning cwd here, that
        # lands as a stray roastmesh.sqlite3 in whatever directory pytest
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
        [sys.executable, "-m", "roastmesh.cli", "node", "serve", "--no-lan-discovery"],
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
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
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
        "from roastmesh.gui.app import main\n"
        f"main(single_instance_port={port})\n"
    )
    cmd = [sys.executable, "-c", body]
    if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        # This system's xvfb-run defaults to a 640x480 virtual screen --
        # confirmed as the root cause of a real intermittent failure: the
        # search results Treeview's requested size (~850x400) didn't fit
        # inside the app's own default 900x680 window once packed under a
        # tab bar/heading at that resolution, leaving it unmapped
        # (winfo_ismapped() == 0) so Treeview.bbox() returned nothing for
        # an otherwise perfectly real, populated row. 1920x1080 comfortably
        # fits this app's window at any of its resolution-based UI scales
        # (gui/widgets.py's detect_ui_scale) -- a laptop-sized screen is
        # exactly the scenario this whole feature is about, not a corner
        # case to shrink away.
        cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1920x1080x24", *cmd]
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
        from roastmesh.gui import single_instance
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
        from roastmesh.gui import single_instance
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
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
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


def test_configured_language_applies_before_any_tab_is_built(tmp_path: Path) -> None:
    """The language must be resolved from config and set (gui/i18n.py)
    before RoastmeshApp builds its notebook -- every tab label is baked in
    at construction time, so setting the language any later would leave
    the first-opened window in English regardless of Settings."""
    home = tmp_path / "home"
    home.mkdir()
    config_dir = home / ".local" / "share" / "roastmesh"
    config_dir.mkdir(parents=True)
    (config_dir / "gui_config.json").write_text('{"language": "fr"}')

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
from roastmesh.gui import i18n
app = RoastmeshApp()
app.update()
nb = [c for c in app.winfo_children() if c.winfo_class() == "TNotebook"][0]
print("CURRENT_LANGUAGE", i18n.current_language())
print("TAB0_TEXT", nb.tab(0, "text"))
print("RUN_BUTTON_TEXT", app.tabs[0].runbar.run_btn.cget("text"))
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "CURRENT_LANGUAGE fr" in r.stdout, r.stdout
    assert "TAB0_TEXT Rechercher" in r.stdout, r.stdout
    assert "RUN_BUTTON_TEXT Rechercher" in r.stdout, r.stdout


def test_unconfigured_language_defaults_to_english(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
from roastmesh.gui import i18n
app = RoastmeshApp()
app.update()
nb = [c for c in app.winfo_children() if c.winfo_class() == "TNotebook"][0]
print("CURRENT_LANGUAGE", i18n.current_language())
print("TAB0_TEXT", nb.tab(0, "text"))
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "CURRENT_LANGUAGE en" in r.stdout, r.stdout
    assert "TAB0_TEXT Search" in r.stdout, r.stdout


def test_toggling_internet_discovery_restarts_serving_with_the_new_flag(tmp_path: Path) -> None:
    """Ticking the Settings checkbox has to actually turn discovery on.

    Serving auto-starts at launch and `--wan-discovery` is decided once, right
    then, so before this the checkbox silently changed nothing until the user
    also found Stop-then-Start on the Network tab. That cost a real user an
    evening of "it's supposed to be on" while their node never announced
    itself -- exactly the kind of failure that looks like a network problem
    and isn't.
    """
    home = tmp_path / "home"
    (home / ".local" / "share" / "roastmesh").mkdir(parents=True)
    (home / ".local" / "share" / "roastmesh" / "gui_config.json").write_text(
        '{"wan_discovery_enabled": false}')
    r = _run_headless(f"""
import os, time
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.update()
net_tab = app.network_tab
print("BEFORE", "--wan-discovery" in net_tab.serve_task.argv)

app.wan_discovery_enabled.set(True)
for _ in range(60):          # the restart is deferred via after(); let it land
    app.update()
    time.sleep(0.05)
    if "--wan-discovery" in net_tab.serve_task.argv:
        break
print("AFTER", "--wan-discovery" in net_tab.serve_task.argv)
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    assert "BEFORE False" in r.stdout, r.stdout
    assert "AFTER True" in r.stdout, r.stdout


def test_scale_keyboard_shortcuts_are_bound_at_startup(tmp_path: Path) -> None:
    """The Ctrl+scroll / Ctrl+plus / Ctrl+0 bindings must exist on a plain
    launch, with nothing else touched first.

    They spent two releases misplaced inside _apply_discovery_change, so they
    were only ever bound if the user toggled internet discovery while serving
    -- meaning the documented way to resize the interface silently did nothing.
    The existing scale tests all called _relaunch_with_scale directly, which
    exercised the handler while proving nothing about whether any key could
    reach it. This asserts the binding itself.
    """
    home = tmp_path / "home"
    home.mkdir()
    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.update()
for seq in ("<Control-MouseWheel>", "<Control-Button-4>", "<Control-Button-5>",
            "<Control-plus>", "<Control-minus>", "<Control-0>"):
    print("BOUND", seq, bool(app.bind_all(seq)))
app._on_close()
print("OK")
""")
    assert "OK" in r.stdout, r.stderr
    unbound = [line for line in r.stdout.splitlines()
               if line.startswith("BOUND") and line.endswith("False")]
    assert not unbound, f"scale shortcuts not bound at startup: {unbound}"


def test_network_tab_renders_a_wan_stats_line_from_the_serve_stream(tmp_path: Path) -> None:
    """The live half of the diagnostics panel.

    `wan-stats:` is the second CLI->GUI text contract after `ticket: `, and it
    is what makes the panel free: the serving process has already computed
    these numbers every round, so the GUI reads them off its output instead of
    spawning a process per refresh. If the prefix or the key names drift, the
    panel silently shows "waiting..." forever -- which is precisely the kind of
    quiet nothing this whole feature exists to eliminate.
    """
    home = tmp_path / "home"
    home.mkdir()

    payload = json.dumps({
        "external_ip": "209.227.189.65", "external_port": 48973,
        "nat": "symmetric", "ip_votes": 17, "node_id": "cd" * 20,
        "node_id_bep42": True,
        "routing_table": {"total": 40, "good": 30, "verified": 21},
        "warm": True,
        "lookup": {"rounds": 14, "queried": 47, "replied": 15, "closest_bits": 140,
                   "announced": 8, "no_token": 0, "peers_found": 1,
                   "rejected_martian": 0, "rejected_impossible_proximity": 3,
                   "rejected_bep42": 29},
        "announce_set": [{"addr": "1.2.3.4:6881", "bits": 140, "bep42": True}],
        "readback": False, "peers": ["5.6.7.8:41890"],
        "swarm_info_hash": "22" * 20,
    })

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
os.environ["USERPROFILE"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.update()
tab = app.network_tab
assert tab.diag_vars["status"].get(), "no placeholder text"
tab._on_serve_output("wan-stats: " + {payload!r} + chr(10))
app.update()
print("STATUS", tab.diag_vars["status"].get())
print("EXTERNAL", tab.diag_vars["external"].get())
print("NAT", tab.diag_vars["nat"].get())
print("REJECTED", tab.diag_vars["rejected"].get())
print("FINDABLE", tab.diag_vars["findable"].get())
print("PEERS", tab.diag_vars["peers"].get())
app.destroy()
""")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "209.227.189.65:48973" in r.stdout
    assert "17" in r.stdout
    # A symmetric NAT outranks everything else in the status line: it is the
    # one condition no DHT fix can rescue.
    assert "blocked by this network" in r.stdout
    assert "carrier-grade" in r.stdout
    assert "3" in r.stdout and "29" in r.stdout
    assert "could not find us" in r.stdout
    assert "5.6.7.8:41890" in r.stdout


def test_a_forwarded_port_setting_reaches_the_serve_command(tmp_path: Path) -> None:
    """The setting is worthless unless it lands on both flags: --wan-port is
    the socket we listen on, --public-port is what we tell other nodes. A
    forward only helps if the port it delivers to is the one we are on."""
    home = tmp_path / "home"
    home.mkdir()

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
os.environ["USERPROFILE"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
from roastmesh.gui.runner import describe
app = RoastmeshApp()
app.update()
tab = app.network_tab
app.wan_discovery_enabled.set(True)

def restart(label, value):
    # _on_start_serve refuses while a task is still running, and a just-stopped
    # one lingers -- without clearing it the second call is a no-op and the
    # test reads the *previous* argv, passing for the wrong reason.
    tab._on_stop_serve()
    tab.serve_task = None
    app.public_port.set(value)
    tab._on_start_serve()
    print(label, describe(tab.serve_task.argv))

restart("NOPORT", "")
restart("PORT", "26513")
restart("AUTO", "auto")
restart("JUNK", "not-a-port")
tab._on_stop_serve()
app.destroy()
""")
    assert r.returncode == 0, r.stdout + r.stderr
    noport = next(l for l in r.stdout.splitlines() if l.startswith("NOPORT"))
    port = next(l for l in r.stdout.splitlines() if l.startswith("PORT"))
    junk = next(l for l in r.stdout.splitlines() if l.startswith("JUNK"))

    auto = next(l for l in r.stdout.splitlines() if l.startswith("AUTO"))
    assert "--public-port" not in noport      # empty means "no forward", not a flag
    assert "--wan-port 26513" in port and "--public-port 26513" in port
    # `auto` deliberately does not pin --wan-port: the router chooses the
    # external number, and guessing it in advance is the one thing that cannot
    # work.
    assert "--public-port auto" in auto and "--wan-port" not in auto
    assert "--public-port" not in junk        # junk must not produce a serve that won't start


def test_the_network_panel_says_what_to_do_about_an_unreachable_node(tmp_path: Path) -> None:
    """"Not findable" with no next step is where a user gives up. When the
    diagnosis is settled -- symmetric NAT, or an announce we could not find
    afterwards -- the panel has to name the fix."""
    home = tmp_path / "home"
    home.mkdir()

    base = {
        "external_ip": "1.2.3.4", "external_port": 5, "nat": "symmetric", "ip_votes": 9,
        "node_id": "cd" * 20, "node_id_bep42": True,
        "routing_table": {"total": 40, "good": 30, "verified": 21}, "warm": True,
        "lookup": {"rounds": 9, "queried": 30, "replied": 12, "closest_bits": 140,
                   "announced": 8, "no_token": 0, "peers_found": 0,
                   "rejected_martian": 0, "rejected_impossible_proximity": 0,
                   "rejected_bep42": 12},
        "announce_set": [], "readback": False, "peers": [],
        "swarm_info_hash": "22" * 20,
    }
    needs = json.dumps({**base, "public_port": None, "needs_public_port": True})
    configured = json.dumps({**base, "public_port": 26513, "needs_public_port": True})
    healthy = json.dumps({**base, "nat": "consistent", "readback": True,
                          "public_port": 26513, "needs_public_port": False})

    r = _run_headless(f"""
import os
os.environ["HOME"] = {str(home)!r}
os.environ["USERPROFILE"] = {str(home)!r}
from roastmesh.gui.app import RoastmeshApp
app = RoastmeshApp()
app.update()
tab = app.network_tab
tab._on_serve_output("wan-stats: " + {needs!r} + chr(10))
print("NEEDS", tab.diag_vars["advice"].get())
tab._on_serve_output("wan-stats: " + {configured!r} + chr(10))
print("CONFIGURED", tab.diag_vars["advice"].get())
tab._on_serve_output("wan-stats: " + {healthy!r} + chr(10))
print("HEALTHY", repr(tab.diag_vars["advice"].get()))
app.destroy()
""")
    assert r.returncode == 0, r.stdout + r.stderr
    needs_line = next(l for l in r.stdout.splitlines() if l.startswith("NEEDS"))
    conf_line = next(l for l in r.stdout.splitlines() if l.startswith("CONFIGURED"))
    healthy_line = next(l for l in r.stdout.splitlines() if l.startswith("HEALTHY"))

    assert "port is forwarded" in needs_line and "Settings" in needs_line
    assert "26513 is set" in conf_line          # a different problem, a different sentence
    assert healthy_line == "HEALTHY ''"         # nothing to say when it works
