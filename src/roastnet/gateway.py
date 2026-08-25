"""Read-only HTTP gateway over the local search index.

ARCHITECTURE.md's Distribution section: "static page against a gateway any
full node can expose... browse and download, contributes nothing." Plain
stdlib http.server, not a framework -- three GET routes over data
`repository.py` already knows how to query is exactly what "static page"
describes, not a rich web app.

GET-only by construction: only do_GET is implemented, so nothing here can
ever accept a write ("contributes nothing" enforced by construction, not
convention). The corpus this serves is untrusted P2P content from strangers
-- the same content ARCHITECTURE.md's SECURITY section warns about for the
parser -- so every corpus-derived string is passed through _esc() before it
lands in HTML. That is the one rule this file cannot violate.
"""
from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from roastnet.index import repository as repo
from roastnet.index.db import connect

PAGE_HEAD = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: sans-serif; max-width: 900px; margin: 2em auto; padding: 0 1em; color: #222; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
th, td {{ text-align: left; padding: 4px 10px 4px 0; border-bottom: 1px solid #ddd; vertical-align: top; }}
a {{ color: #7a4a2b; }}
input {{ padding: 4px; margin: 2px 4px 2px 0; }}
.warn {{ background: #fdf6e3; border: 1px solid #e6d9a8; padding: 8px 12px; margin: 8px 0; }}
</style></head><body>
"""
PAGE_TAIL = "</body></html>"


def _esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def _safe_filename(name: str) -> str:
    cleaned = "".join(c for c in name if c not in '"\r\n')
    return cleaned or "roast.alog"


def _page(title: str, body: str) -> bytes:
    return (PAGE_HEAD.format(title=_esc(title)) + body + PAGE_TAIL).encode("utf-8")


def _render_search_page(rows: list[repo.RoastSearchRow], params: dict[str, list[str]]) -> bytes:
    def field(name: str, label: str) -> str:
        value = _esc((params.get(name) or [""])[0])
        return f'<label>{_esc(label)} <input name="{_esc(name)}" value="{value}"></label>'

    form = f"""
    <h1>roastnet</h1>
    <p>Local search index -- your own roasts plus anything replicated from peers.</p>
    <form method="get" action="/">
      {field("q", "Text")}
      {field("machine", "Machine")}
      {field("roast_type", "Roast type")}
      {field("dtr_min", "DTR min %")}
      {field("dtr_max", "DTR max %")}
      {field("drop_after", "Drop after (C)")}
      <button type="submit">Search</button>
    </form>
    """
    if not rows:
        return _page("roastnet search", form + "<p>No matches.</p>")

    table_rows = []
    for row in rows:
        beans = (row.beans_text or "").splitlines()[0][:80] if row.beans_text else ""
        dtr = f"{row.dtr_pct:.1f}%" if row.dtr_pct is not None else ""
        drop = f"{row.drop_bt_c:.0f}C" if row.drop_bt_c is not None else ""
        table_rows.append(
            "<tr>"
            f'<td><a href="/roast/{_esc(row.roast_id)}">{_esc(row.roast_id[:8])}</a></td>'
            f"<td>{_esc(row.machine_key)}</td>"
            f"<td>{_esc(row.roast_type or '')}</td>"
            f"<td>{_esc(dtr)}</td>"
            f"<td>{_esc(drop)}</td>"
            f"<td>{_esc(beans)}</td>"
            "</tr>"
        )

    table = (
        "<table><tr><th>ID</th><th>Machine</th><th>Roast type</th><th>DTR</th>"
        f"<th>Drop</th><th>Beans</th></tr>{''.join(table_rows)}</table>"
        f"<p>{len(rows)} result(s)</p>"
    )
    return _page("roastnet search", form + table)


def _render_roast_page(record: dict, roast_id: str) -> bytes | None:
    if record is None:
        return None

    milestones = record.get("milestones") or []
    milestone_rows = "".join(
        f"<tr><td>{_esc(m.get('name'))}</td><td>{_esc(m.get('time_s'))}</td>"
        f"<td>{_esc(m.get('bt_c'))}</td><td>{_esc(m.get('et_c'))}</td></tr>"
        for m in milestones
    )
    phase = record.get("phase_profile") or {}
    phase_rows = "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in phase.items())
    tags = ", ".join(_esc(t) for t in (record.get("note_tags") or []))
    warnings = record.get("parse_warnings") or []
    warn_html = (
        '<div class="warn">Parse warnings: ' + "; ".join(_esc(w) for w in warnings) + "</div>"
        if warnings else ""
    )
    beans_text = record.get("beans_text")
    title = beans_text.splitlines()[0] if beans_text else roast_id[:8]

    body = f"""
    <p><a href="/">&larr; back to search</a></p>
    <h1>{_esc(title)}</h1>
    {warn_html}
    <table>
      <tr><td>Machine</td><td>{_esc(record.get('machine_key'))} ({_esc(record.get('roaster_type_raw'))})</td></tr>
      <tr><td>Roast type</td><td>{_esc(record.get('roast_type'))}</td></tr>
      <tr><td>Batch weight in / out</td>
          <td>{_esc(record.get('batch_weight_in_g'))}g / {_esc(record.get('batch_weight_out_g'))}g</td></tr>
      <tr><td>Density</td><td>{_esc(record.get('density_g_per_l'))}</td></tr>
      <tr><td>Roast date</td><td>{_esc(record.get('roast_date'))}</td></tr>
      <tr><td>Tags</td><td>{tags}</td></tr>
    </table>
    <h2>Milestones</h2>
    <table><tr><th>Name</th><th>Time (s)</th><th>BT (C)</th><th>ET (C)</th></tr>{milestone_rows}</table>
    <h2>Phase profile</h2>
    <table>{phase_rows}</table>
    <h2>Notes</h2>
    <p>{_esc(record.get('roasting_notes'))}</p>
    <p>{_esc(record.get('cupping_notes'))}</p>
    <p><a href="/roast/{_esc(roast_id)}/download">Download original .alog</a></p>
    """
    return _page(f"roastnet: {title}", body)


class GatewayHandler(BaseHTTPRequestHandler):
    db_path: str = ""  # set per-instance by make_server() before the server starts

    def log_message(self, format: str, *args) -> None:
        pass  # keep test/CLI output quiet; errors still surface via response codes

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        segments = [s for s in parts.path.split("/") if s]
        conn = connect(self.db_path)
        try:
            if not segments:
                self._handle_search(conn, parse_qs(parts.query))
            elif segments[0] == "roast" and len(segments) == 2:
                self._handle_roast(conn, segments[1])
            elif segments[0] == "roast" and len(segments) == 3 and segments[2] == "download":
                self._handle_download(conn, segments[1])
            else:
                self._not_found()
        finally:
            conn.close()

    def _handle_search(self, conn, params: dict[str, list[str]]) -> None:
        def first(name: str) -> str | None:
            values = params.get(name)
            return values[0].strip() if values and values[0].strip() else None

        def first_float(name: str) -> float | None:
            value = first(name)
            if value is None:
                return None
            try:
                return float(value)
            except ValueError:
                return None

        after_second_crack = None
        raw_asc = first("after_second_crack")
        if raw_asc == "yes":
            after_second_crack = True
        elif raw_asc == "no":
            after_second_crack = False

        rows = repo.search_roasts(
            conn, text=first("q"), machine_key=first("machine"), roast_type=first("roast_type"),
            dtr_min=first_float("dtr_min"), dtr_max=first_float("dtr_max"),
            drop_bt_min=first_float("drop_after"), after_second_crack=after_second_crack,
        )
        self._respond_html(_render_search_page(rows, params))

    def _handle_roast(self, conn, roast_id: str) -> None:
        record = repo.load_full_record(conn, roast_id)
        page = _render_roast_page(record, roast_id)
        if page is None:
            self._not_found()
            return
        self._respond_html(page)

    def _handle_download(self, conn, roast_id: str) -> None:
        raw_path = repo.find_raw_path(conn, roast_id)
        if raw_path is None:
            self._not_found()
            return
        path = Path(raw_path)
        if not path.exists():
            self._not_found()
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{_safe_filename(path.name)}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _respond_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self) -> None:
        body = _page("not found", "<p>Not found.</p>")
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(db_path, host: str = "127.0.0.1", port: int = 8420) -> ThreadingHTTPServer:
    handler_cls = type("BoundGatewayHandler", (GatewayHandler,), {"db_path": str(db_path)})
    return ThreadingHTTPServer((host, port), handler_cls)
