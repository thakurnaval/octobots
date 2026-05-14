"""Transcripts HTTP+WS endpoint — verify route shapes, safety, and live push."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from monitor.bridge.events import (
    MessageClaimedEvent,
    MessageDoneEvent,
    NotifyEvent,
)
from monitor.bridge.transcripts import TranscriptSink
from monitor.bridge.transcripts_http import STATE_KEY, build_app


def _claim(role: str, msg_id: str = "m1", sender: str = "pm",
           content: str = "verify PR #42") -> MessageClaimedEvent:
    return MessageClaimedEvent(
        msg_id=msg_id, sender=sender, recipient=role,
        preview=content[:200], content=content,
    )


def _done(role: str, msg_id: str = "m1", response: str = "ok") -> MessageDoneEvent:
    return MessageDoneEvent(
        msg_id=msg_id, recipient=role,
        response_preview=response[:200], response=response,
    )


@pytest.fixture
async def populated_root(tmp_path: Path):
    """Set up two roles, each with a finished task + qa in-flight on a second."""
    sink = TranscriptSink(root=tmp_path, session_id="20260513T153000Z")
    await sink.start()
    # qa: one done task, one in-flight
    await sink.handle(_claim("qa", msg_id="m1", content="task one"))
    await sink.handle(NotifyEvent(
        sender="qa", channel="telegram", preview="ping", ts=time.time() + 0.01,
    ))
    await sink.handle(_done("qa", msg_id="m1", response="task one done"))
    await sink.handle(_claim("qa", msg_id="m2", content="task two (in flight)"))
    # pm: one done task only
    await sink.handle(_claim("pm", msg_id="p1", content="pm task"))
    await sink.handle(_done("pm", msg_id="p1", response="pm reply"))
    return tmp_path


async def _client(root: Path, relay_db: Path | None = None) -> TestClient:
    server = TestServer(build_app(root, relay_db or (root / "relay.db")))
    client = TestClient(server)
    await client.start_server()
    return client


def _init_relay_db(path: Path) -> None:
    """Mirror relay.py's schema (kept duplicated on purpose — tests don't
    depend on import side effects)."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          TEXT PRIMARY KEY,
            sender      TEXT NOT NULL,
            recipient   TEXT NOT NULL,
            content     TEXT NOT NULL,
            response    TEXT DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'pending',
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _db_rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT id, sender, recipient, content, status FROM messages "
            "ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()


# -------------------------- healthz --------------------------

async def test_healthz(tmp_path: Path):
    client = await _client(tmp_path)
    try:
        resp = await client.get("/healthz")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True}
    finally:
        await client.close()


# -------------------------- index --------------------------

async def test_index_empty_when_no_roles(tmp_path: Path):
    client = await _client(tmp_path)
    try:
        resp = await client.get("/transcripts")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"roles": []}
    finally:
        await client.close()


async def test_index_lists_known_roles_with_summary(populated_root: Path):
    client = await _client(populated_root)
    try:
        resp = await client.get("/transcripts")
        assert resp.status == 200
        body = await resp.json()
        roles = {r["role"]: r for r in body["roles"]}
        assert set(roles) == {"qa", "pm"}

        assert roles["qa"]["current_task_id"] == "m2"        # in-flight
        assert roles["qa"]["session_id"] == "20260513T153000Z"
        assert roles["qa"]["task_count"] == 2

        assert roles["pm"]["current_task_id"] is None        # idle
        assert roles["pm"]["task_count"] == 1
    finally:
        await client.close()


# -------------------------- per-role --------------------------

async def test_get_role_returns_session(populated_root: Path):
    client = await _client(populated_root)
    try:
        resp = await client.get("/transcripts/qa")
        assert resp.status == 200
        body = await resp.json()
        assert body["role"] == "qa"
        assert body["session_id"] == "20260513T153000Z"
        # tasks newest-first; current is m2
        assert body["tasks"][0]["task_id"] == "m2"
        assert body["tasks"][1]["task_id"] == "m1"
        # done task has notify count from the one notify we sent
        m1 = body["tasks"][1]
        assert m1["notify_count"] == 1
    finally:
        await client.close()


async def test_get_unknown_role_is_404(tmp_path: Path):
    client = await _client(tmp_path)
    try:
        resp = await client.get("/transcripts/nonesuch")
        assert resp.status == 404
    finally:
        await client.close()


# -------------------------- per-task --------------------------

async def test_get_archived_task_returns_full_record(populated_root: Path):
    client = await _client(populated_root)
    try:
        resp = await client.get("/transcripts/qa/m1")
        assert resp.status == 200
        body = await resp.json()
        assert body["task_id"] == "m1"
        assert body["status"] == "done"
        assert body["prompt"] == "task one"
        assert body["response"] == "task one done"
        assert len(body["activity"]) == 1
        assert body["activity"][0]["kind"] == "notify"
    finally:
        await client.close()


async def test_get_current_task_returns_live_record(populated_root: Path):
    """Asking for the in-flight task id should serve current.json."""
    client = await _client(populated_root)
    try:
        resp = await client.get("/transcripts/qa/m2")
        assert resp.status == 200
        body = await resp.json()
        assert body["task_id"] == "m2"
        assert body["status"] == "processing"
        assert body["ended_at"] is None
        assert body["prompt"] == "task two (in flight)"
    finally:
        await client.close()


async def test_get_unknown_task_is_404(populated_root: Path):
    client = await _client(populated_root)
    try:
        resp = await client.get("/transcripts/qa/nonesuch")
        assert resp.status == 404
    finally:
        await client.close()


# -------------------------- safety --------------------------

async def test_path_traversal_rejected(populated_root: Path):
    """A maliciously-shaped role or task id should not escape the root."""
    client = await _client(populated_root)
    try:
        # Path-y values get URL-encoded; aiohttp routes them through {role},
        # but our handler rejects entries starting with '.' or containing '/'.
        for url in (
            "/transcripts/..",
            "/transcripts/.hidden",
            "/transcripts/qa/..",
            "/transcripts/qa/.session",
        ):
            resp = await client.get(url)
            assert resp.status in (400, 404), f"{url} → {resp.status}"
    finally:
        await client.close()


async def test_cors_header_present(populated_root: Path):
    client = await _client(populated_root)
    try:
        resp = await client.get("/transcripts/qa")
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
        assert resp.headers.get("Cache-Control") == "no-store"
    finally:
        await client.close()


async def test_role_without_session_json_skipped_in_index(tmp_path: Path):
    """A stray dir without session.json shouldn't appear in the index."""
    (tmp_path / "ghost").mkdir()
    (tmp_path / "ghost" / "stray.txt").write_text("not a transcript")
    client = await _client(tmp_path)
    try:
        resp = await client.get("/transcripts")
        body = await resp.json()
        assert body["roles"] == []
    finally:
        await client.close()


# -------------------------- POST /messages/<role> --------------------------

async def test_post_message_inserts_pending_row(tmp_path: Path):
    relay = tmp_path / "relay.db"
    _init_relay_db(relay)
    client = await _client(tmp_path, relay_db=relay)
    try:
        resp = await client.post(
            "/messages/python-dev",
            json={"content": "fix issue #42", "sender": "user@ui"},
        )
        assert resp.status == 201
        body = await resp.json()
        assert body["recipient"] == "python-dev"
        assert body["task_id"]  # some msg id

        rows = _db_rows(relay)
        assert len(rows) == 1
        msg_id, sender, recipient, content, status = rows[0]
        assert msg_id == body["task_id"]
        assert sender == "user@ui"
        assert recipient == "python-dev"
        assert content == "fix issue #42"
        assert status == "pending"
    finally:
        await client.close()


async def test_post_message_defaults_sender(tmp_path: Path):
    relay = tmp_path / "relay.db"
    _init_relay_db(relay)
    client = await _client(tmp_path, relay_db=relay)
    try:
        resp = await client.post("/messages/qa", json={"content": "do X"})
        assert resp.status == 201
        rows = _db_rows(relay)
        assert rows[0][1] == "user@transcripts"
    finally:
        await client.close()


async def test_post_message_validation(tmp_path: Path):
    relay = tmp_path / "relay.db"
    _init_relay_db(relay)
    client = await _client(tmp_path, relay_db=relay)
    try:
        # Empty content
        r = await client.post("/messages/qa", json={"content": ""})
        assert r.status == 400
        # Non-string content
        r = await client.post("/messages/qa", json={"content": 123})
        assert r.status == 400
        # Missing content
        r = await client.post("/messages/qa", json={})
        assert r.status == 400
        # Invalid role name
        r = await client.post("/messages/.hidden", json={"content": "x"})
        assert r.status == 400
        # Body must be JSON
        r = await client.post(
            "/messages/qa", data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status == 400
        # Nothing was inserted by any of those failed attempts
        assert _db_rows(relay) == []
    finally:
        await client.close()


# -------------------------- WS /events --------------------------

async def test_ws_sends_snapshot_on_connect(populated_root: Path):
    client = await _client(populated_root)
    try:
        async with client.ws_connect("/events") as ws:
            msg = await ws.receive_json(timeout=2.0)
            assert msg["type"] == "snapshot"
            roles = {r["role"]: r for r in msg["roles"]}
            assert set(roles) == {"qa", "pm"}
            assert roles["qa"]["current_task_id"] == "m2"
    finally:
        await client.close()


async def test_ws_pushes_task_claimed_frame(tmp_path: Path):
    """Sink emits via on_event → server broadcasts → client receives frame."""
    app = build_app(tmp_path)
    state = app[STATE_KEY]
    sink = TranscriptSink(root=tmp_path, on_event=state.broadcast)
    await sink.start()

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.ws_connect("/events") as ws:
            # Drain snapshot first
            snap = await ws.receive_json(timeout=2.0)
            assert snap["type"] == "snapshot"
            # Trigger a claim
            await sink.handle(_claim("qa", msg_id="m1"))
            frame = await ws.receive_json(timeout=2.0)
            assert frame["type"] == "task_claimed"
            assert frame["role"] == "qa"
            assert frame["task"]["task_id"] == "m1"
            assert frame["task"]["status"] == "processing"
    finally:
        await client.close()


async def test_ws_pushes_done_and_notify_and_abandoned(tmp_path: Path):
    app = build_app(tmp_path)
    state = app[STATE_KEY]
    sink = TranscriptSink(root=tmp_path, on_event=state.broadcast)
    await sink.start()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.ws_connect("/events") as ws:
            await ws.receive_json(timeout=2.0)  # snapshot

            await sink.handle(_claim("qa", msg_id="m1"))
            await ws.receive_json(timeout=2.0)  # task_claimed

            await sink.handle(NotifyEvent(
                sender="qa", channel="telegram",
                preview="ping", ts=time.time() + 0.01,
            ))
            frame = await ws.receive_json(timeout=2.0)
            assert frame["type"] == "task_notify"
            assert frame["role"] == "qa"
            assert frame["task_id"] == "m1"
            assert frame["notify_count"] == 1
            assert frame["activity"]["preview"] == "ping"

            # Overlapping claim → previous abandoned + new task_claimed
            await sink.handle(_claim("qa", msg_id="m2"))
            f1 = await ws.receive_json(timeout=2.0)
            f2 = await ws.receive_json(timeout=2.0)
            kinds = {f1["type"], f2["type"]}
            assert kinds == {"task_abandoned", "task_claimed"}

            await sink.handle(_done("qa", msg_id="m2"))
            frame = await ws.receive_json(timeout=2.0)
            assert frame["type"] == "task_done"
            assert frame["role"] == "qa"
            assert frame["task"]["task_id"] == "m2"
            assert frame["task"]["status"] == "done"
    finally:
        await client.close()


async def test_ws_broadcast_with_no_clients_is_safe(tmp_path: Path):
    """Sink emitting before any client connects must not crash."""
    app = build_app(tmp_path)
    state = app[STATE_KEY]
    sink = TranscriptSink(root=tmp_path, on_event=state.broadcast)
    await sink.start()
    # No WS clients connected; handle should still succeed.
    await sink.handle(_claim("qa"))
    await sink.handle(_done("qa"))
