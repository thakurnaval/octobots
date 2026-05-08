"""Notify log tailer — emits notify events for `mcp__notify__notify` calls.

Tails `.octobots/notify.log` (JSONL, written by notify_lib.py). Each line:
    {"ts": <float>, "from": <role>, "channel": "telegram",
     "method": "sendMessage|sendPhoto|...", "preview": "..."}

For each new line we emit:
  - NotifyEvent — UI shows wave/preview bubble
  - AgentStateEvent(state="calling_user") if the sender is in WorldState
    Auto-reverts to "idle" after WAVE_DURATION seconds, unless another
    notify for the same role supplanted us.

On startup we seek to end of file so historical entries don't replay as
live events. We track inode and reopen on rotation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

from .. import config
from ..events import AgentStateEvent, NotifyEvent
from ..state import WorldState

log = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]

WAVE_DURATION = 3.0  # seconds calling_user persists before auto-revert


class NotifyLogTailer:
    def __init__(
        self,
        emit: EmitFn,
        world: WorldState,
        log_path: Path = config.NOTIFY_LOG,
    ) -> None:
        self.emit = emit
        self.world = world
        self.log_path = log_path
        self._last_wave_ts: dict[str, float] = {}

    async def _wave(self, role: str, ts: float) -> None:
        self._last_wave_ts[role] = ts
        if self.world.set_agent_state(role, "calling_user"):
            await self.emit(AgentStateEvent(id=role, state="calling_user", since=ts))
        await asyncio.sleep(WAVE_DURATION)
        if self._last_wave_ts.get(role, 0.0) > ts:
            return  # newer wave for this role is responsible for the revert
        a = self.world.agents.get(role)
        if a and a.state == "calling_user":
            self.world.set_agent_state(role, "idle")
            await self.emit(
                AgentStateEvent(id=role, state="idle", since=time.time())
            )

    async def _process_line(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            rec = json.loads(raw)
        except Exception:
            log.warning("notify_log: bad JSON line: %r", raw[:120])
            return
        ts = float(rec.get("ts") or time.time())
        role = str(rec.get("from") or "unknown")
        await self.emit(NotifyEvent(
            sender=role,
            channel=str(rec.get("channel") or ""),
            preview=str(rec.get("preview") or ""),
            ts=ts,
        ))
        asyncio.create_task(self._wave(role, ts))

    async def run(self) -> None:
        log.info("notify tailer started (path=%s)", self.log_path)

        # Capture existence at startup. If the log was created *after* we
        # started watching, we must read from the beginning — the line
        # that triggered creation is the one we want to emit. If the log
        # already existed, seek to end so historical entries don't replay
        # as live events.
        existed_at_start = self.log_path.exists()
        while not self.log_path.exists():
            await asyncio.sleep(config.NOTIFY_POLL_INTERVAL)

        f = self.log_path.open("r", encoding="utf-8")
        if existed_at_start:
            f.seek(0, os.SEEK_END)
        try:
            inode = os.fstat(f.fileno()).st_ino
        except OSError:
            inode = None
        log.info(
            "notify tailer attached (offset=%d inode=%s existed_at_start=%s)",
            f.tell(), inode, existed_at_start,
        )

        try:
            while True:
                while True:
                    pos = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        # Partial write — back off, retry next poll.
                        f.seek(pos)
                        break
                    await self._process_line(line)

                await asyncio.sleep(config.NOTIFY_POLL_INTERVAL)

                # Detect log rotation by inode change.
                try:
                    new_inode = os.stat(self.log_path).st_ino
                except FileNotFoundError:
                    continue
                if inode is not None and new_inode != inode:
                    log.info("notify_log rotated, reopening")
                    try:
                        f.close()
                    except Exception:
                        pass
                    f = self.log_path.open("r", encoding="utf-8")
                    inode = new_inode
        finally:
            try:
                f.close()
            except Exception:
                pass
