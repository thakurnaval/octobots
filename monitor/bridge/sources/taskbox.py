"""Taskbox poller — emits message_* events from relay.db.

Polls the messages table on a short interval, emits events for new messages
and status transitions, and keeps WorldState in sync so snapshot replies
have history.

State derivation rules implemented here:
  - INSERT (new msg_id):    emit MessageSentEvent.
  - pending -> processing:  emit MessageClaimedEvent.
  - processing -> done:     emit MessageDoneEvent.

We do NOT flip agent.state from taskbox. Idle/typing is owned exclusively
by tmux_panes (the freshest, truest signal). Letting both sources write
the same state caused flicker: taskbox flipped recipient -> typing on
claim, then tmux's next poll saw no pane activity and immediately flipped
it back to idle. The UI gets enough animation cues from the message
events themselves (envelope flying, arriving, poofing) without needing a
state event tied to claim/ack.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import config
from ..events import (
    MessageClaimedEvent,
    MessageDoneEvent,
    MessageSentEvent,
    MessageSnapshot,
)
from ..state import WorldState

log = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]


def _preview(text: str | None, n: int = config.PREVIEW_LEN) -> str:
    if not text:
        return ""
    return text if len(text) <= n else text[:n] + "…"


class TaskboxPoller:
    def __init__(
        self,
        emit: EmitFn,
        world: WorldState,
        db_path: Path = config.RELAY_DB,
    ) -> None:
        self.emit = emit
        self.world = world
        self.db_path = db_path
        self._last_seen: float = 0.0
        self._status: dict[str, str] = {}
        self._bootstrapped = False

    def _read_rows(self, where_sql: str, params: tuple) -> list[dict[str, Any]]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, sender, recipient, content, response, status, "
                "       created_at, updated_at FROM messages " + where_sql,
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _read_bootstrap(self) -> list[dict[str, Any]]:
        return self._read_rows(
            "ORDER BY updated_at DESC LIMIT ?",
            (config.REPLAY_BUFFER_SIZE,),
        )

    def _read_deltas(self) -> list[dict[str, Any]]:
        return self._read_rows(
            "WHERE updated_at > ? ORDER BY updated_at ASC",
            (self._last_seen,),
        )

    def _apply_bootstrap(self, rows: list[dict[str, Any]]) -> None:
        # Rows are DESC by updated_at; reverse so the deque ends up chronological.
        for r in reversed(rows):
            self.world.upsert_message(MessageSnapshot(
                msg_id=r["id"],
                sender=r["sender"],
                recipient=r["recipient"],
                status=r["status"],
                preview=_preview(r["content"]),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            ))
            self._status[r["id"]] = r["status"]
            if r["updated_at"] > self._last_seen:
                self._last_seen = r["updated_at"]
        log.info(
            "taskbox bootstrap: loaded %d messages, last_seen=%.3f",
            len(rows), self._last_seen,
        )

    async def _apply_delta(self, r: dict[str, Any]) -> None:
        msg_id = r["id"]
        new_status = r["status"]
        old_status = self._status.get(msg_id)

        snap = MessageSnapshot(
            msg_id=msg_id,
            sender=r["sender"],
            recipient=r["recipient"],
            status=new_status,
            preview=_preview(r["content"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        self.world.upsert_message(snap)

        if old_status is None:
            await self.emit(MessageSentEvent(
                msg_id=msg_id,
                sender=r["sender"],
                recipient=r["recipient"],
                preview=snap.preview,
                created_at=r["created_at"],
            ))
        elif old_status != new_status:
            if old_status == "pending" and new_status == "processing":
                await self.emit(MessageClaimedEvent(
                    msg_id=msg_id,
                    sender=r["sender"],
                    recipient=r["recipient"],
                    preview=snap.preview,
                ))
            elif old_status == "processing" and new_status == "done":
                await self.emit(MessageDoneEvent(
                    msg_id=msg_id, recipient=r["recipient"],
                    response_preview=_preview(r["response"]),
                ))

        self._status[msg_id] = new_status
        if r["updated_at"] > self._last_seen:
            self._last_seen = r["updated_at"]

    async def run(self) -> None:
        log.info(
            "taskbox poller started (db=%s, interval=%.2fs)",
            self.db_path, config.TASKBOX_POLL_INTERVAL,
        )
        while True:
            if not self.db_path.exists():
                await asyncio.sleep(config.TASKBOX_POLL_INTERVAL)
                continue
            if not self._bootstrapped:
                try:
                    rows = await asyncio.to_thread(self._read_bootstrap)
                except sqlite3.Error as e:
                    log.warning("taskbox bootstrap failed: %s", e)
                    await asyncio.sleep(config.TASKBOX_POLL_INTERVAL)
                    continue
                self._apply_bootstrap(rows)
                self._bootstrapped = True
            try:
                deltas = await asyncio.to_thread(self._read_deltas)
            except sqlite3.Error as e:
                log.warning("taskbox poll failed: %s", e)
                await asyncio.sleep(config.TASKBOX_POLL_INTERVAL)
                continue
            for r in deltas:
                await self._apply_delta(r)
            await asyncio.sleep(config.TASKBOX_POLL_INTERVAL)
