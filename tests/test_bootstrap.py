"""bootstrap.py: BOOTSTRAP_NODES parsing and the fetched/cached/default
precedence that makes moduloinfo.ca reachable "at all times" even with
GitHub (and the on-disk cache) both unavailable. No real network calls --
fetch_rendezvous_hosts is exercised in test_wan_discovery.py via a fake
transport at the level that matters (run_wan_discovery), not here.
"""
from __future__ import annotations

from pathlib import Path

from roastmesh.bootstrap import (
    DEFAULT_RENDEZVOUS_HOSTS,
    RendezvousHost,
    default_rendezvous_cache_path,
    effective_rendezvous_hosts,
    load_cached_rendezvous_hosts,
    parse_rendezvous_file,
    save_cached_rendezvous_hosts,
)


def test_parse_rendezvous_file_well_formed() -> None:
    text = "moduloinfo.ca\nfoo.example.com,1.2.3.4\nbar.example.com,,26513\n"
    hosts = parse_rendezvous_file(text)
    assert hosts == [
        RendezvousHost(host="moduloinfo.ca", ip=None, port=41890),
        RendezvousHost(host="foo.example.com", ip="1.2.3.4", port=41890),
        RendezvousHost(host="bar.example.com", ip=None, port=26513),
    ]


def test_parse_rendezvous_file_ignores_comments_and_blank_lines() -> None:
    text = "# a comment\n\n   \nmoduloinfo.ca\n# another\n"
    assert parse_rendezvous_file(text) == [RendezvousHost(host="moduloinfo.ca", ip=None, port=41890)]


def test_parse_rendezvous_file_skips_malformed_lines_without_raising() -> None:
    text = "moduloinfo.ca\n,no-host-before-comma\nbad.example.com,,not-a-port\ngood.example.com\n"
    hosts = parse_rendezvous_file(text)
    assert [h.host for h in hosts] == ["moduloinfo.ca", "good.example.com"]


def test_parse_rendezvous_file_empty_text_yields_no_hosts() -> None:
    assert parse_rendezvous_file("") == []
    assert parse_rendezvous_file("   \n\n") == []


def test_effective_rendezvous_hosts_prefers_fetched_over_cached() -> None:
    cached = [RendezvousHost(host="cached.example.com", ip=None, port=41890)]
    fetched = [RendezvousHost(host="fetched.example.com", ip=None, port=41890)]
    assert effective_rendezvous_hosts(cached, fetched) == fetched


def test_effective_rendezvous_hosts_falls_back_to_cached_when_fetch_failed() -> None:
    cached = [RendezvousHost(host="cached.example.com", ip=None, port=41890)]
    assert effective_rendezvous_hosts(cached, None) == cached


def test_effective_rendezvous_hosts_falls_back_to_default_when_both_empty() -> None:
    assert effective_rendezvous_hosts([], None) == DEFAULT_RENDEZVOUS_HOSTS


def test_moduloinfo_ca_present_when_fetch_and_cache_both_fail() -> None:
    hosts = effective_rendezvous_hosts([], None)
    assert any(h.host == "moduloinfo.ca" for h in hosts)


def test_cache_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "rendezvous_hosts.json"
    hosts = [
        RendezvousHost(host="moduloinfo.ca", ip=None, port=41890),
        RendezvousHost(host="second.example.com", ip="9.9.9.9", port=26513),
    ]
    save_cached_rendezvous_hosts(hosts, path)
    assert load_cached_rendezvous_hosts(path) == hosts


def test_load_cached_rendezvous_hosts_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_cached_rendezvous_hosts(tmp_path / "does-not-exist.json") == []


def test_load_cached_rendezvous_hosts_corrupt_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "rendezvous_hosts.json"
    path.write_text("not json", encoding="utf-8")
    assert load_cached_rendezvous_hosts(path) == []


def test_default_rendezvous_cache_path_honors_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert str(default_rendezvous_cache_path()).startswith(str(tmp_path))
