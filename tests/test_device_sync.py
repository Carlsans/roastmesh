"""device_sync.py: the private folder mirror's reconcile engine and its wire
protocol. `_safe_relpath` and `reconcile` are pure -- tested with plain data,
no filesystem, no network. `_build_sync_response` does real filesystem I/O
but no network -- tested by calling it directly, the same "transport-agnostic
where it matters" posture as pairing.py. The end-to-end test wires
scan_folder + reconcile + _build_sync_response together over two real temp
directories with no socket at all -- an in-memory transport in the sense
that matters: nothing here ever opens a connection.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

from roastmesh.device_sync import (
    Action,
    _build_sync_response,
    _safe_relpath,
    load_state,
    reconcile,
    save_state,
    scan_folder,
)

# --------------------------------------------------------------------------
# _safe_relpath
# --------------------------------------------------------------------------

def test_safe_relpath_accepts_an_ordinary_nested_path() -> None:
    assert _safe_relpath("a/b.alog") == "a/b.alog"


def test_safe_relpath_accepts_a_bare_filename() -> None:
    assert _safe_relpath("roast.alog") == "roast.alog"


def test_safe_relpath_rejects_traversal() -> None:
    for hostile in ("..", "../x", "a/../b", "a/b/..", "../../etc/passwd"):
        assert _safe_relpath(hostile) is None, hostile


def test_safe_relpath_rejects_absolute_paths() -> None:
    for hostile in ("/etc/passwd", "/a/b"):
        assert _safe_relpath(hostile) is None, hostile


def test_safe_relpath_rejects_backslashes() -> None:
    for hostile in ("a\\b", "..\\..\\x", "C:\\Windows"):
        assert _safe_relpath(hostile) is None, hostile


def test_safe_relpath_rejects_empty_and_nul() -> None:
    assert _safe_relpath("") is None
    assert _safe_relpath("a\x00b") is None


def test_safe_relpath_rejects_non_string_input() -> None:
    assert _safe_relpath(None) is None
    assert _safe_relpath(123) is None
    assert _safe_relpath(["a"]) is None


# --------------------------------------------------------------------------
# reconcile -- pure, per the plan's exact scenarios
# --------------------------------------------------------------------------

def _rec(sha: str, updated_at: float, *, deleted: bool = False, size: int = 10) -> dict:
    return {"sha256": sha, "size": size, "mtime_ns": 0, "deleted": deleted, "updated_at": updated_at}


def test_reconcile_add_propagates_in_both_directions() -> None:
    local = {"only_local.txt": _rec("aaa", 100)}
    remote = {"only_remote.txt": _rec("bbb", 100)}
    local_actions, remote_actions = reconcile(local, remote)
    assert local_actions == [Action("pull", "only_remote.txt", remote["only_remote.txt"])]
    assert remote_actions == [Action("pull", "only_local.txt", local["only_local.txt"])]


def test_reconcile_edit_newest_wins_local_ahead() -> None:
    local = {"f.txt": _rec("new-sha", 200)}
    remote = {"f.txt": _rec("old-sha", 100)}
    local_actions, remote_actions = reconcile(local, remote)
    assert local_actions == []
    assert remote_actions == [Action("pull", "f.txt", local["f.txt"])]


def test_reconcile_edit_newest_wins_remote_ahead() -> None:
    local = {"f.txt": _rec("old-sha", 100)}
    remote = {"f.txt": _rec("new-sha", 200)}
    local_actions, remote_actions = reconcile(local, remote)
    assert local_actions == [Action("pull", "f.txt", remote["f.txt"])]
    assert remote_actions == []


def test_reconcile_delete_propagates_tombstone_beats_older_present_record() -> None:
    local = {"gone.txt": _rec(None, 200, deleted=True, size=0)}
    remote = {"gone.txt": _rec("still-here", 100)}
    local_actions, remote_actions = reconcile(local, remote)
    assert local_actions == []
    assert remote_actions == [Action("delete", "gone.txt", local["gone.txt"])]


def test_reconcile_equal_sha_produces_no_action_even_with_different_timestamps() -> None:
    local = {"f.txt": _rec("same-sha", 200)}
    remote = {"f.txt": _rec("same-sha", 100)}
    local_actions, remote_actions = reconcile(local, remote)
    assert local_actions == []
    assert remote_actions == []


def test_reconcile_exact_tie_produces_no_action() -> None:
    local = {"f.txt": _rec("sha-a", 150)}
    remote = {"f.txt": _rec("sha-b", 150)}
    local_actions, remote_actions = reconcile(local, remote)
    assert local_actions == []
    assert remote_actions == []


def test_reconcile_a_remote_present_record_older_than_a_local_tombstone_does_not_resurrect_the_file() -> None:
    """The exact scenario the plan calls out: local deleted the file (a
    newer tombstone); remote's copy of it is older and still "present".
    The file must not come back on local, and remote must be told to
    delete its stale copy."""
    local = {"f.txt": _rec(None, 300, deleted=True, size=0)}
    remote = {"f.txt": _rec("old-sha", 100)}
    local_actions, remote_actions = reconcile(local, remote)
    assert local_actions == [], "a tombstone must never be undone by an older present record"
    assert remote_actions == [Action("delete", "f.txt", local["f.txt"])]


def test_reconcile_both_tombstoned_produces_no_action() -> None:
    local = {"f.txt": _rec(None, 100, deleted=True, size=0)}
    remote = {"f.txt": _rec(None, 200, deleted=True, size=0)}
    local_actions, remote_actions = reconcile(local, remote)
    assert local_actions == []
    assert remote_actions == []


def test_reconcile_is_ordered_by_relpath() -> None:
    local = {"b.txt": _rec("b", 100), "a.txt": _rec("a", 100)}
    remote: dict = {}
    local_actions, _ = reconcile(local, remote)
    assert [a.relpath for a in local_actions] == []
    _, remote_actions = reconcile(local, remote)
    assert [a.relpath for a in remote_actions] == ["a.txt", "b.txt"]


# --------------------------------------------------------------------------
# scan_folder
# --------------------------------------------------------------------------

def test_scan_folder_finds_a_new_file(tmp_path: Path) -> None:
    (tmp_path / "roast.alog").write_bytes(b"hello")
    manifest = scan_folder(tmp_path, {})
    assert "roast.alog" in manifest
    assert manifest["roast.alog"]["deleted"] is False
    assert manifest["roast.alog"]["size"] == 5


def test_scan_folder_ignores_the_reserved_versions_directory(tmp_path: Path) -> None:
    versions = tmp_path / ".roastmesh-versions"
    versions.mkdir()
    (versions / "old.alog").write_bytes(b"old")
    (tmp_path / "current.alog").write_bytes(b"current")
    manifest = scan_folder(tmp_path, {})
    assert set(manifest) == {"current.alog"}


def test_scan_folder_marks_a_removed_file_as_a_tombstone(tmp_path: Path) -> None:
    path = tmp_path / "gone.alog"
    path.write_bytes(b"bye")
    first = scan_folder(tmp_path, {})
    path.unlink()
    second = scan_folder(tmp_path, first)
    assert second["gone.alog"]["deleted"] is True


def test_scan_folder_keeps_an_unchanged_files_updated_at(tmp_path: Path) -> None:
    path = tmp_path / "stable.alog"
    path.write_bytes(b"content")
    first = scan_folder(tmp_path, {})
    second = scan_folder(tmp_path, first)
    assert second["stable.alog"]["updated_at"] == first["stable.alog"]["updated_at"]


def test_scan_folder_carries_forward_a_settled_tombstone_unchanged(tmp_path: Path) -> None:
    prev = {"long_gone.alog": {"sha256": None, "size": 0, "mtime_ns": 0,
                                "deleted": True, "updated_at": 12345.0}}
    manifest = scan_folder(tmp_path, prev)
    assert manifest["long_gone.alog"] == prev["long_gone.alog"]


def test_scan_folder_nested_subdirectory(tmp_path: Path) -> None:
    nested = tmp_path / "sub" / "dir"
    nested.mkdir(parents=True)
    (nested / "f.bin").write_bytes(b"x")
    manifest = scan_folder(tmp_path, {})
    assert "sub/dir/f.bin" in manifest


# --------------------------------------------------------------------------
# _build_sync_response -- server-side ops, path-traversal rejection
# --------------------------------------------------------------------------

def test_build_sync_response_manifest_reflects_disk(tmp_path: Path) -> None:
    devices_dir = tmp_path / "devices"
    devices_dir.mkdir()
    state_path = tmp_path / "state.json"
    (devices_dir / "a.alog").write_bytes(b"content")
    response = _build_sync_response({"op": "manifest"}, devices_dir, state_path)
    assert "a.alog" in response["records"]


def test_build_sync_response_get_file_rejects_a_traversing_path(tmp_path: Path) -> None:
    devices_dir = tmp_path / "devices"
    devices_dir.mkdir()
    state_path = tmp_path / "state.json"
    response = _build_sync_response({"op": "get_file", "path": "../../etc/passwd"}, devices_dir, state_path)
    assert "error" in response


def test_build_sync_response_put_file_rejects_a_traversing_path(tmp_path: Path) -> None:
    devices_dir = tmp_path / "devices"
    devices_dir.mkdir()
    state_path = tmp_path / "state.json"
    response = _build_sync_response({
        "op": "put_file", "path": "../escape.txt",
        "content_base64": base64.b64encode(b"evil").decode("ascii"),
        "record": {"sha256": "x", "size": 4, "mtime_ns": 0, "deleted": False, "updated_at": 1.0},
    }, devices_dir, state_path)
    assert "error" in response
    assert not (tmp_path / "escape.txt").exists()


def test_build_sync_response_delete_file_rejects_a_traversing_path(tmp_path: Path) -> None:
    devices_dir = tmp_path / "devices"
    devices_dir.mkdir()
    state_path = tmp_path / "state.json"
    response = _build_sync_response({
        "op": "delete_file", "path": "../../somewhere.txt",
        "record": {"sha256": None, "size": 0, "mtime_ns": 0, "deleted": True, "updated_at": 1.0},
    }, devices_dir, state_path)
    assert "error" in response


def test_build_sync_response_get_file_reports_not_found(tmp_path: Path) -> None:
    devices_dir = tmp_path / "devices"
    devices_dir.mkdir()
    state_path = tmp_path / "state.json"
    response = _build_sync_response({"op": "get_file", "path": "nope.txt"}, devices_dir, state_path)
    assert "error" in response


def test_build_sync_response_put_file_then_get_file_round_trips_content(tmp_path: Path) -> None:
    devices_dir = tmp_path / "devices"
    devices_dir.mkdir()
    state_path = tmp_path / "state.json"
    put_response = _build_sync_response({
        "op": "put_file", "path": "nested/new.txt",
        "content_base64": base64.b64encode(b"round trip me").decode("ascii"),
        "record": {"sha256": "whatever", "size": 999, "mtime_ns": 0, "deleted": False, "updated_at": 5.0},
    }, devices_dir, state_path)
    assert put_response == {"ok": True}
    get_response = _build_sync_response({"op": "get_file", "path": "nested/new.txt"}, devices_dir, state_path)
    assert base64.b64decode(get_response["content_base64"]) == b"round trip me"


def test_build_sync_response_delete_file_removes_the_file_and_records_a_tombstone(tmp_path: Path) -> None:
    devices_dir = tmp_path / "devices"
    devices_dir.mkdir()
    state_path = tmp_path / "state.json"
    (devices_dir / "to_delete.txt").write_bytes(b"gone soon")
    response = _build_sync_response({
        "op": "delete_file", "path": "to_delete.txt",
        "record": {"sha256": None, "size": 0, "mtime_ns": 0, "deleted": True, "updated_at": time.time()},
    }, devices_dir, state_path)
    assert response == {"ok": True}
    assert not (devices_dir / "to_delete.txt").exists()
    assert load_state(state_path)["to_delete.txt"]["deleted"] is True


def test_build_sync_response_unknown_op(tmp_path: Path) -> None:
    response = _build_sync_response({"op": "nonsense"}, tmp_path, tmp_path / "state.json")
    assert "error" in response


# --------------------------------------------------------------------------
# End-to-end: two temp dirs, no network -- converge to identical, including
# a deletion, driving scan_folder + reconcile + _build_sync_response exactly
# the way reconcile_with_device does over a real connection.
# --------------------------------------------------------------------------

def _apply(local_dir: Path, local_state_path: Path, local_manifest: dict,
           remote_dir: Path, remote_state_path: Path) -> dict:
    """One reconcile round, driving both sides through the real wire-format
    functions (base64 content, `_build_sync_response`) with no socket at
    all -- `local_*` is played by hand (like reconcile_with_device would
    over its own disk), `remote_*` only ever through `_build_sync_response`,
    exactly as if it were answering real get_file/put_file/delete_file
    requests."""
    remote_manifest = _build_sync_response({"op": "manifest"}, remote_dir, remote_state_path)["records"]
    local_actions, remote_actions = reconcile(local_manifest, remote_manifest)

    for action in local_actions:
        target = local_dir / action.relpath
        if action.op == "pull":
            resp = _build_sync_response({"op": "get_file", "path": action.relpath}, remote_dir, remote_state_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(resp["content_base64"]))
        else:
            target.unlink(missing_ok=True)
        local_manifest[action.relpath] = action.record

    for action in remote_actions:
        if action.op == "pull":
            content = (local_dir / action.relpath).read_bytes()
            _build_sync_response({
                "op": "put_file", "path": action.relpath,
                "content_base64": base64.b64encode(content).decode("ascii"),
                "record": action.record,
            }, remote_dir, remote_state_path)
        else:
            _build_sync_response({"op": "delete_file", "path": action.relpath, "record": action.record},
                                 remote_dir, remote_state_path)

    save_state(local_manifest, local_state_path)
    return local_manifest


def test_end_to_end_reconcile_converges_two_folders_including_a_deletion(tmp_path: Path) -> None:
    local_dir = tmp_path / "local"
    remote_dir = tmp_path / "remote"
    local_dir.mkdir()
    remote_dir.mkdir()
    local_state_path = tmp_path / "local_state.json"
    remote_state_path = tmp_path / "remote_state.json"

    # A file both sides already have, byte-identical -- must stay untouched
    # (equal sha -> nothing).
    (local_dir / "shared.alog").write_text("shared content")
    (remote_dir / "shared.alog").write_text("shared content")

    # A file only local has -- must end up on remote.
    (local_dir / "only_local.txt").write_bytes(b"from local")

    # A file only remote has, in a subdirectory -- must end up on local.
    (remote_dir / "sub").mkdir()
    (remote_dir / "sub" / "only_remote.txt").write_bytes(b"from remote")

    # A file both sides start with; local deletes it after an initial sync
    # round -- the deletion must propagate to remote.
    (local_dir / "deleteme.txt").write_text("bye")
    (remote_dir / "deleteme.txt").write_text("bye")

    local_manifest = scan_folder(local_dir, {})
    save_state(local_manifest, local_state_path)
    remote_manifest = scan_folder(remote_dir, {})
    save_state(remote_manifest, remote_state_path)

    # Delete locally, then rescan -- the resulting tombstone is necessarily
    # newer than remote's still-present record for the same relpath.
    (local_dir / "deleteme.txt").unlink()
    local_manifest = scan_folder(local_dir, load_state(local_state_path))
    save_state(local_manifest, local_state_path)

    local_manifest = _apply(local_dir, local_state_path, local_manifest, remote_dir, remote_state_path)

    assert (remote_dir / "only_local.txt").read_bytes() == b"from local"
    assert (local_dir / "sub" / "only_remote.txt").read_bytes() == b"from remote"
    assert not (remote_dir / "deleteme.txt").exists()
    assert not (local_dir / "deleteme.txt").exists()
    assert (local_dir / "shared.alog").exists()
    assert (remote_dir / "shared.alog").exists()

    # A second round with nothing new on either side converges to no
    # further actions at all -- the fixed point a real periodic sync relies on.
    local_manifest2 = scan_folder(local_dir, local_manifest)
    remote_manifest2 = load_state(remote_state_path)
    local_actions2, remote_actions2 = reconcile(local_manifest2, remote_manifest2)
    assert local_actions2 == []
    assert remote_actions2 == []
