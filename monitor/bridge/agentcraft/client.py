"""Async HTTP/WS client for the AgentCraft sink.

HTTP:  stdlib `urllib.request` wrapped in `asyncio.to_thread`. Posts are
       fed by an `asyncio.Queue`; a small worker pool drains it. New
       events are dropped when the queue is full (with a periodic
       summary log) so source pollers stay responsive even if AC stalls.

WS:    `websockets.connect` with exponential-backoff reconnect. The
       inbound loop forwards every JSON frame to `on_inbound(msg)` after
       parsing. Subscriptions are tracked locally and replayed on every
       (re)connect.

Health: cheap GET /health; the sink probes this before sending anything.

No AgentCraft-specific event shapes leak in here — translate.py owns
that. This module is a transport.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import websockets

from . import config

log = logging.getLogger(__name__)

InboundHandler = Callable[[dict[str, Any]], Awaitable[None]]


def _ws_url(http_url: str) -> str:
    parsed = urlparse(http_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    netloc = parsed.netloc or parsed.path
    return f"{scheme}://{netloc}"


class AgentCraftClient:
    def __init__(
        self,
        url: str = config.AC_URL,
        on_inbound: InboundHandler | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.on_inbound = on_inbound
        self._queue: asyncio.Queue[tuple[str, str, dict]] = asyncio.Queue(
            maxsize=config.QUEUE_SIZE,
        )
        self._dropped_since: float = time.time()
        self._dropped_count: int = 0
        self._subscribed: set[str] = set()
        self._ws: websockets.WebSocketClientProtocol | None = None  # type: ignore[name-defined]

    # --------------- HTTP ---------------

    async def health(self) -> bool:
        return await asyncio.to_thread(self._sync_get_ok, "/health")

    async def get_settings(self) -> dict | None:
        try:
            data = await asyncio.to_thread(self._sync_get_json, "/settings")
            return data if isinstance(data, dict) else None
        except Exception as e:
            log.debug("get_settings failed: %s", e)
            return None

    async def disable_project_filter(self) -> bool:
        """Flip projectFilter off at runtime (no AC restart needed).

        Without this, AC filters our subscribed sessions out — its
        `sessionBelongsToProject` check looks for a JSONL transcript
        file at ~/.claude/projects/<slug>/<sessionId>.jsonl, which
        doesn't exist for our synthetic sessionId=role_id values.

        Returns True on 2xx, False otherwise.
        """
        try:
            status = await asyncio.to_thread(
                self._sync_post, "/settings/project-filter", {"enabled": False},
            )
            return 200 <= status < 300
        except Exception as e:
            log.warning("disable_project_filter failed: %s", e)
            return False

    def _sync_get_ok(self, path: str) -> bool:
        try:
            with urllib.request.urlopen(
                self.url + path, timeout=config.HTTP_TIMEOUT,
            ) as r:
                return 200 <= r.status < 300
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            return False

    def _sync_get_json(self, path: str) -> Any:
        with urllib.request.urlopen(
            self.url + path, timeout=config.HTTP_TIMEOUT,
        ) as r:
            return json.loads(r.read().decode("utf-8"))

    def _sync_post(self, path: str, body: dict) -> int:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
            return r.status

    async def post(self, method: str, path: str, body: dict) -> None:
        """Enqueue a call. Drops new events if the queue is full."""
        try:
            self._queue.put_nowait((method, path, body))
        except asyncio.QueueFull:
            self._dropped_count += 1
            now = time.time()
            if now - self._dropped_since >= 30.0:
                log.warning(
                    "agentcraft outbound queue full — dropped %d events in last %.0fs",
                    self._dropped_count, now - self._dropped_since,
                )
                self._dropped_since = now
                self._dropped_count = 0

    async def run_outbound(self, *, workers: int = 4) -> None:
        await asyncio.gather(*(self._outbound_worker(i) for i in range(workers)))

    async def _outbound_worker(self, n: int) -> None:
        while True:
            method, path, body = await self._queue.get()
            try:
                await asyncio.to_thread(self._sync_post, path, body)
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                # One retry after a short delay; second failure -> drop.
                await asyncio.sleep(1.0)
                try:
                    await asyncio.to_thread(self._sync_post, path, body)
                except Exception as e2:
                    log.debug(
                        "outbound[%d] dropped %s %s: %s", n, method, path, e2,
                    )
            except Exception as e:
                log.warning("outbound[%d] unexpected error: %s", n, e)

    # --------------- WS ---------------

    async def subscribe(self, session_id: str) -> None:
        """Subscribe to inbound messages for a session id. Idempotent.

        If WS is connected, sends now; otherwise, the subscribe is replayed
        on the next successful reconnect.
        """
        self._subscribed.add(session_id)
        await self._ws_send({
            "type": "subscribe",
            "sessionId": session_id,
            "terminal": config.TERMINAL_LABEL,
        })

    async def _ws_send(self, msg: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(msg))
        except Exception as e:
            log.debug("ws send failed (%s); will resync on reconnect", e)

    async def run_inbound(self) -> None:
        """Connect to ws://, replay subscriptions, dispatch frames.

        Reconnects with exponential backoff on disconnect. Per-message
        handler errors are caught so one bad frame doesn't kill the loop.
        """
        ws_url = _ws_url(self.url)
        attempt = 0
        while True:
            try:
                async with websockets.connect(ws_url) as ws:
                    self._ws = ws
                    log.info("agentcraft ws connected: %s", ws_url)
                    attempt = 0
                    # Replay subscriptions on each (re)connect.
                    for sid in list(self._subscribed):
                        try:
                            await ws.send(json.dumps({
                                "type": "subscribe",
                                "sessionId": sid,
                                "terminal": config.TERMINAL_LABEL,
                            }))
                        except Exception:
                            break
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            log.debug("ws non-json frame ignored")
                            continue
                        if not isinstance(msg, dict):
                            continue
                        if self.on_inbound is not None:
                            try:
                                await self.on_inbound(msg)
                            except Exception as e:
                                log.warning(
                                    "ws inbound handler raised on %s: %s",
                                    msg.get("type"), e,
                                )
            except Exception as e:
                log.info("agentcraft ws disconnected: %s", e)
            finally:
                self._ws = None
            backoff = config.WS_BACKOFF_SCHEDULE[
                min(attempt, len(config.WS_BACKOFF_SCHEDULE) - 1)
            ]
            attempt += 1
            await asyncio.sleep(backoff)
