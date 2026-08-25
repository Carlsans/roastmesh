import http.client
import threading
from pathlib import Path

import pytest

from roastnet.gateway import make_server
from roastnet.index import repository as repo
from roastnet.index.db import connect
from roastnet.index.ingest import ingest_file, ingest_path

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))


@pytest.fixture
def gateway(tmp_path: Path):
    db_path = tmp_path / "gateway.sqlite3"
    conn = connect(db_path)
    results = ingest_path(conn, FIXTURES_DIR)
    assert all(r.error is None for r in results)
    conn.close()

    server = make_server(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, db_path
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _get(port: int, path: str) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    return conn.getresponse()


def test_index_lists_all_fixtures(gateway) -> None:
    port, _ = gateway
    resp = _get(port, "/")
    body = resp.read().decode("utf-8")
    assert resp.status == 200
    assert f"{len(FIXTURES)} result(s)" in body


def test_index_filters_by_machine(gateway) -> None:
    port, _ = gateway
    resp = _get(port, "/?machine=hottop")
    body = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "2 result(s)" in body  # hottop_1.alog, hottop_2.alog


def test_roast_detail_page_shows_milestones_and_dtr(gateway) -> None:
    port, db_path = gateway
    conn = connect(db_path)
    roast_id = conn.execute(
        "SELECT roast_id FROM roasts WHERE machine_key = 'kaleido_serial' LIMIT 1"
    ).fetchone()["roast_id"]
    conn.close()

    resp = _get(port, f"/roast/{roast_id}")
    body = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "Milestones" in body
    assert "Phase profile" in body
    assert "Download original .alog" in body


def test_roast_detail_page_404s_for_unknown_id(gateway) -> None:
    port, _ = gateway
    resp = _get(port, "/roast/does-not-exist")
    resp.read()
    assert resp.status == 404


def test_download_returns_identical_bytes(gateway) -> None:
    port, db_path = gateway
    conn = connect(db_path)
    roast_id = conn.execute("SELECT roast_id FROM roasts LIMIT 1").fetchone()["roast_id"]
    raw_path = repo.find_raw_path(conn, roast_id)
    conn.close()

    resp = _get(port, f"/roast/{roast_id}/download")
    downloaded = resp.read()

    assert resp.status == 200
    assert resp.getheader("Content-Disposition") is not None
    assert downloaded == Path(raw_path).read_bytes()


def test_download_404s_for_unknown_id(gateway) -> None:
    port, _ = gateway
    resp = _get(port, "/roast/does-not-exist/download")
    resp.read()
    assert resp.status == 404


def test_post_is_rejected(gateway) -> None:
    port, _ = gateway
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/", body=b"x=1")
    resp = conn.getresponse()
    resp.read()
    assert resp.status != 200


def test_xss_in_notes_is_escaped_not_executed(tmp_path: Path) -> None:
    malicious = {
        "roastertype": "TestRoaster",
        "mode": "C",
        "weight": [300.0, 250.0, "g"],
        "beans": "Test Beans",
        "roastingnotes": "<script>alert(1)</script>",
        "cuppingnotes": "",
        "timex": [0, 60, 120],
        "temp1": [20.0, 150.0, 200.0],
        "temp2": [20.0, 140.0, 195.0],
        "computed": {
            "CHARGE_time": 0.0, "CHARGE_BT": 20.0, "CHARGE_ET": 20.0,
            "DROP_time": 120.0, "DROP_BT": 195.0, "DROP_ET": 200.0,
        },
        "timeindex": [0, 0, 0, 0, 0, 0, 2, 0],
    }
    alog_path = tmp_path / "malicious.alog"
    alog_path.write_text(repr(malicious))

    db_path = tmp_path / "gateway.sqlite3"
    conn = connect(db_path)
    result = ingest_file(conn, alog_path)
    assert result.error is None
    roast_id = result.record.roast_id
    conn.close()

    server = make_server(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        resp = _get(port, f"/roast/{roast_id}")
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    finally:
        server.shutdown()
        thread.join(timeout=5)
