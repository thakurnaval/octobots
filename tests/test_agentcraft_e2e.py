"""End-to-end smoke against an in-process fake AgentCraft server.

Exercises the full chain without spawning `npx @idosal/agentcraft`:

  1. Bridge probes /health, GETs /settings, sees analyticsEnabled:false.
  2. WorldState pre-populated with one agent — sink replays it as
     team_member_detected + agent_start + hero_activity_update.
  3. We feed the sink a MessageSentEvent and verify the
     notice_board_message body lands at /event.
  4. The fake server pushes a user_prompt over WS; we verify the row
     appears in relay.db with the expected sender/recipient/content.

The fake AC uses aiohttp (already a transitive dep). HTTP and WS share
one port, mirroring AgentCraft's own express+ws topology.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web

from monitor.bridge import config as bridge_config
from monitor.bridge.agentcraft import config as ac_config
from monitor.bridge.agentcraft.client import AgentCraftClient
from monitor.bridge.agentcraft.sink import AgentCraftSink
from monitor.bridge.events import (
    AgentSnapshot,
    AgentStateEvent,
    MessageSentEvent,
    Space,
)
from monitor.bridge.state import WorldState


class FakeAC:
    def __init__(self) -> None:
        self.captures: list[tuple[str, dict]] = []
        self.subscribed: list[str] = []
        self.ws_clients: list[web.WebSocketResponse] = []
        self.app = web.Application()
        self.app.router.add_get("/health", self._health)
        self.app.router.add_get("/settings", self._settings)
        self.app.router.add_post("/event", self._event)
        self.app.router.add_post("/notice-board", self._notice_board)
        self.app.router.add_get("/", self._ws_or_root)
        self.runner: web.AppRunner | None = None
        self.port: int = 0
        self._subscribed_evt = asyncio.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        sock = next(iter(site._server.sockets or []))
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        for ws in list(self.ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        if self.runner is not None:
            await self.runner.cleanup()

    async def push_user_prompt(self, session_id: str, prompt: str) -> None:
        # Wait for at least one subscribe before pushing — otherwise the
        # bridge may not yet be listening for this session.
        await self._subscribed_evt.wait()
        for ws in list(self.ws_clients):
            if not ws.closed:
                await ws.send_str(json.dumps({
                    "type": "user_prompt",
                    "sessionId": session_id,
                    "prompt": prompt,
                }))

    # --- handlers ---

    async def _health(self, request):
        return web.Response(text="OK")

    async def _settings(self, request):
        return web.json_response({"analyticsEnabled": False})

    async def _event(self, request):
        body = await request.json()
        self.captures.append(("/event", body))
        return web.json_response({"ok": True})

    async def _notice_board(self, request):
        body = await request.json()
        self.captures.append(("/notice-board", body))
        return web.json_response({"ok": True})

    async def _ws_or_root(self, request):
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return web.Response(text="fake-agentcraft")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.append(ws)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "subscribe":
                    self.subscribed.append(data.get("sessionId", ""))
                    self._subscribed_evt.set()
        finally:
            if ws in self.ws_clients:
                self.ws_clients.remove(ws)
        return ws


def _init_relay_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
            content TEXT NOT NULL, response TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.05):
    """Poll `predicate()` until it returns truthy or timeout."""
    async def go():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(interval)
        return False
    return go()


@pytest.fixture
async def fake_ac():
    ac = FakeAC()
    await ac.start()
    yield ac
    await ac.stop()


@pytest.fixture
def relay_db(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "relay.db"
    _init_relay_db(p)
    monkeypatch.setattr(bridge_config, "RELAY_DB", p)
    return p


@pytest.fixture
def settings_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "agentcraft-settings.json"
    monkeypatch.setattr(ac_config, "SETTINGS_PATH", p)
    return p


async def test_e2e_replay_and_inbound(
    fake_ac: FakeAC, relay_db: Path, settings_path: Path,
):
    space = Space(id="s1", name="proj", path="/work/proj", started_at=0.0)
    world = WorldState()
    world.put_agent(AgentSnapshot(id="python-dev", role="python-dev", state="idle"))

    sink = AgentCraftSink(world=world, space=space, team_name="proj")
    sink.client = AgentCraftClient(
        url=fake_ac.url, on_inbound=sink._handle_inbound,
    )
    await sink.start()

    run_task = asyncio.create_task(sink.run())
    push_task = asyncio.create_task(
        fake_ac.push_user_prompt("python-dev", "fix issue #42"),
    )

    try:
        # Replay should land team_member_detected + agent_start +
        # hero_activity_update (idle).
        ok = await _wait_for(
            lambda: any(
                b.get("type") == "team_member_detected"
                for _, b in fake_ac.captures
            )
            and any(
                b.get("type") == "agent_start"
                for _, b in fake_ac.captures
            )
        )
        assert ok, f"replay never landed; captures={fake_ac.captures}"

        # Subscribe should have been sent over WS for the existing agent.
        ok = await _wait_for(lambda: "python-dev" in fake_ac.subscribed)
        assert ok, f"subscribe missing; subscribed={fake_ac.subscribed}"

        # Feed a MessageSentEvent — should produce a notice_board_message.
        await sink.handle(MessageSentEvent(
            msg_id="m1", sender="pm", recipient="python-dev",
            preview="please review", created_at=time.time(),
        ))
        ok = await _wait_for(
            lambda: any(
                b.get("type") == "notice_board_message"
                and b.get("noticeId") == "m1"
                for _, b in fake_ac.captures
            )
        )
        assert ok, f"notice_board_message missing; captures={fake_ac.captures}"

        # State change: idle -> typing -> awaiting_reply
        await sink.handle(AgentStateEvent(
            id="python-dev", state="awaiting_reply", since=time.time(),
        ))
        ok = await _wait_for(
            lambda: any(
                b.get("type") == "awaiting_input"
                and b.get("sessionId") == "python-dev"
                for _, b in fake_ac.captures
            )
        )
        assert ok, "awaiting_input not captured"

        # Inbound path: fake AC pushed user_prompt — should land in relay.db.
        await push_task
        ok = await _wait_for(lambda: _row_count(relay_db) >= 1, timeout=3.0)
        assert ok, "user_prompt did not insert into relay.db"
        rows = _rows(relay_db)
        assert rows == [
            ("user@agentcraft", "python-dev", "fix issue #42", "pending"),
        ]
    finally:
        run_task.cancel()
        push_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, BaseException):
            pass


def _rows(db: Path):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT sender, recipient, content, status FROM messages "
            "ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()


def _row_count(db: Path) -> int:
    return len(_rows(db))
