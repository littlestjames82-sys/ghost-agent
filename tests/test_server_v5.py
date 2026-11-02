"""v0.5.0 reskin + brand icon tests (landmarks, asset route, fallback).

The behavioral contract is covered by the v1-v4 suites and gate v5;
these tests pin the new presentation surface: version bump, Trust
Ladder landmarks, and Ryan's Ghost Agent icon served token-free.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import ghost_agent.server as server_mod
from ghost_agent.agent import Agent
from ghost_agent.models import Router
from ghost_agent.server import make_server
from ghost_agent.store import Store


def _start(tmp_path, tok=None):
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    server = make_server("127.0.0.1", 0, store, agent, tok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_v5_version_in_export(tmp_path):
    server, base = _start(tmp_path)
    try:
        status, _, body = _get(base, "/api/export")
        assert status == 200
        assert json.loads(body)["version"] == "0.10.0"
        status, _, body = _get(base, "/api/system")
        assert status == 200
        assert json.loads(body)["version"] == "0.10.0"
    finally:
        server.shutdown(); server.server_close()


def test_v5_page_landmarks(tmp_path):
    server, base = _start(tmp_path)
    try:
        status, _, raw = _get(base, "/")
        assert status == 200
        body = raw.decode()
        # Trust Ladder shelves on the key pages.
        for landmark in (
            'id="ov-attention-shelf"', 'id="ov-recent-shelf"',
            'id="ap-pending-shelf"', 'id="ap-decisions-shelf"',
            'id="mem-candidates-shelf"', 'id="mem-trusted-shelf"',
            "id='sys-health'",  # JS-rendered, single-quoted attribute
        ):
            assert landmark in body, landmark
        # Drawer + bottom nav + header surface label.
        assert 'id="drawer"' in body
        assert 'id="bottomnav"' in body
        assert 'id="hdr-surface"' in body
        assert 'id="apps-btn"' in body
        # The icon is the brand mark and the favicon.
        assert '<link rel="icon" type="image/jpeg" ' \
               'href="/assets/ghost-agent-icon.jpg">' in body
        assert '<img class="brandicon" ' \
               'src="/assets/ghost-agent-icon.jpg"' in body
    finally:
        server.shutdown(); server.server_close()


def test_v5_icon_asset_served_without_token(tmp_path):
    # Even on a token-protected server, the icon route answers without
    # credentials (the login screen renders before any token exists).
    server, base = _start(tmp_path, tok="sekrit")
    try:
        status, headers, data = _get(base, "/assets/ghost-agent-icon.jpg")
        assert status == 200
        assert headers.get_content_type() == "image/jpeg"
        assert len(data) > 100_000  # the real 1254x1254 JPEG (~275KB)
        assert data[:2] == b"\xff\xd8"  # JPEG magic
        # ...while the API gate still refuses the same client.
        status, _, _ = _get(base, "/api/board")
        assert status == 401
    finally:
        server.shutdown(); server.server_close()


def test_v5_brandmark_falls_back_when_icon_missing(tmp_path, monkeypatch):
    missing = tmp_path / "no-such-icon.jpg"
    monkeypatch.setattr(server_mod, "ICON_PATH", missing)
    assert server_mod._brandmark_html() == '<span class="ghostmark">G</span>'
    monkeypatch.undo()
    html = server_mod._brandmark_html()
    assert html.startswith('<img class="brandicon"')
    assert "/assets/ghost-agent-icon.jpg" in html
