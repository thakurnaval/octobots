"""TranscriptSink — mirrors per-role task activity to `.agents/transcripts/`.

For each role, maintains:

  .agents/transcripts/<role>/
    current.json                       (live: in-flight task, updated on each event)
    <iso_compact>-<task_id>.json       (immutable: archived completed tasks)

Retention: keep the N most recent archived files per role (default 20),
delete older. `current.json` is removed once the task it described
finalizes; it's a transient marker for "what's this role doing right now".

Per-task file shape:

  {
    "task_id":    str,        # relay.db message id
    "role":       str,        # recipient role (this transcript's owner)
    "sender":     str,        # whoever sent the task into the role
    "prompt":     str,        # full content of the task message
    "started_at": float,      # epoch seconds — when role claimed (pending → processing)
    "ended_at":   float|null, # epoch seconds — when role acked (processing → done)
    "status":     str,        # "processing" while live, "done" once finalized
    "response":   str|null,   # full response, populated on finalize
    "activity":   [           # tier 1+2: bounded chronological list of events
      {"ts": float, "kind": "notify", "channel": str, "preview": str},
      ...
    ],
  }

Activity sources (current):
  - NotifyEvent — appended when notify.sender == this.role and the event's
    timestamp falls inside the task's [started_at, ended_at] window. Captures
    `mcp__notify__notify` calls during the task.

Activity is intentionally NOT recoverable from later sources (tool-use,
file-edit) in this version — that's tier 3 work and requires Claude Code
hooks. Adding fields to `activity[]` is additive and won't break readers.

Robustness notes:
  - If the bridge starts mid-task (MessageClaimed was missed during the
    taskbox bootstrap), MessageDone for that task is silently dropped —
    we have no `started_at` for it. The supervisor will still ack the
    message normally; only the transcript is missing.
  - If the bridge crashes between MessageClaimed and MessageDone, the
    `current.json` for that role is left on disk until the next claim
    overwrites it or it's cleaned up manually. Stale `current.json` is
    diagnostic, not load-bearing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import config
from .events import (
    MessageClaimedEvent,
    MessageDoneEvent,
    MessageSentEvent,
    NotifyEvent,
)

OnEventCb = Callable[[dict[str, Any]], Awaitable[None]]

log = logging.getLogger(__name__)

CURRENT_FILE = "current.json"
SESSION_FILE = "session.json"


def _iso_compact(ts: float) -> str:
    """Filename-safe UTC stamp: 20260513T153022Z (sortable)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _preview(text: str | None, n: int = 200) -> str:
    """Truncate for session.json index entries (full body lives in per-task file)."""
    if not text:
        return ""
    return text if len(text) <= n else text[:n] + "…"


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON via temp + rename — never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


class TranscriptSink:
    def __init__(
        self,
        root: Path = config.TRANSCRIPTS_ROOT,
        retention: int = config.TRANSCRIPTS_RETENTION,
        session_id: str | None = None,
        on_event: OnEventCb | None = None,
    ) -> None:
        self.root = Path(root)
        self.retention = max(1, retention)
        # Session id identifies this bridge process's run. Same value used in
        # every role's session.json so a consumer can tell whether two roles
        # are in the same bridge lifetime.
        self.session_id = session_id or _iso_compact(time.time())
        # Optional async hook invoked after each state change. Used by the
        # HTTP server to broadcast WS frames; tests pass a list-appender.
        # Failures in the callback are caught and logged — they never
        # abort the sink's own bookkeeping.
        self.on_event = on_event
        # role -> in-flight task record (full per-task data, written to current.json)
        self._current: dict[str, dict[str, Any]] = {}
        # role -> session index (denormalized, written to session.json)
        self._sessions: dict[str, dict[str, Any]] = {}

    # ----- public lifecycle -----

    async def start(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("transcripts root not writable: %s (%s)", self.root, e)
            return
        log.info(
            "transcripts sink configured: root=%s retention=%d",
            self.root, self.retention,
        )

    async def run(self) -> None:
        # No background loop needed — this sink is purely event-driven.
        # Returning here would close the asyncio.gather in __main__; instead,
        # block forever so the gather stays alive.
        await asyncio.Event().wait()

    # ----- event dispatch -----

    async def handle(self, event: Any) -> None:
        if isinstance(event, MessageSentEvent):
            await self._on_sent(event)
        elif isinstance(event, MessageClaimedEvent):
            await self._on_claimed(event)
        elif isinstance(event, MessageDoneEvent):
            await self._on_done(event)
        elif isinstance(event, NotifyEvent):
            await self._on_notify(event)
        # Other events (Agent*) are not transcript-relevant.

    async def _safe_emit(self, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            await self.on_event(payload)
        except Exception as e:  # never let consumer errors poison the sink
            log.debug("transcripts: on_event raised on %s: %s", payload.get("type"), e)

    # ----- handlers -----

    async def _on_sent(self, ev: MessageSentEvent) -> None:
        """A new pending message has appeared in relay.db.

        Could be from `POST /messages/<role>` (user typed in UI), from
        the supervisor's main-loop dispatch, or from sim-traffic. We
        give it a session.json entry with status="pending" so the UI
        can show it immediately, even before any worker claims it.
        Doesn't touch self._current — pending tasks are queued, not
        in-flight.
        """
        sess = self._session_for(ev.recipient, started_at=ev.created_at)
        # Skip if we already know about this task_id — claim/done may
        # have arrived first (out-of-order delivery from the bus is
        # rare but legal).
        if any(t["task_id"] == ev.msg_id for t in sess["tasks"]):
            return
        entry = {
            "task_id": ev.msg_id,
            "sender": ev.sender,
            "status": "pending",
            "started_at": ev.created_at,
            "ended_at": None,
            "prompt_preview": _preview(
                ev.content if ev.content is not None else ev.preview
            ),
            "response_preview": "",
            "notify_count": 0,
        }
        sess["tasks"].insert(0, entry)
        del sess["tasks"][self.retention:]
        self._write_session(ev.recipient)
        await self._safe_emit({
            "type": "task_pending",
            "role": ev.recipient,
            "task": dict(entry),
        })

    async def _on_claimed(self, ev: MessageClaimedEvent) -> None:
        now = time.time()
        prev = self._current.get(ev.recipient)
        if prev is not None:
            # Role somehow claimed a new task while one is still in flight.
            # Should not happen given the supervisor's one-at-a-time guarantee,
            # but if it does, abandon the previous as a transcript safety net.
            log.warning(
                "transcripts: %s claimed %s while %s still in-flight — "
                "abandoning previous transcript",
                ev.recipient, ev.msg_id, prev.get("task_id"),
            )
            prev["status"] = "abandoned"
            prev["ended_at"] = now
            self._archive(ev.recipient, prev)
            self._session_update_entry(ev.recipient, prev["task_id"], {
                "status": "abandoned", "ended_at": prev["ended_at"],
            })
            await self._safe_emit({
                "type": "task_abandoned",
                "role": ev.recipient,
                "task_id": prev["task_id"],
                "ended_at": prev["ended_at"],
            })

        rec = {
            "task_id": ev.msg_id,
            "role": ev.recipient,
            "sender": ev.sender,
            "prompt": ev.content if ev.content is not None else ev.preview,
            "started_at": now,
            "ended_at": None,
            "status": "processing",
            "response": None,
            "activity": [],
        }
        self._current[ev.recipient] = rec
        self._write_current(ev.recipient, rec)
        self._session_on_claim(ev, now)
        await self._safe_emit({
            "type": "task_claimed",
            "role": ev.recipient,
            "task": self._session_entry(ev.recipient, ev.msg_id),
        })

    async def _on_done(self, ev: MessageDoneEvent) -> None:
        rec = self._current.pop(ev.recipient, None)
        if rec is None:
            log.debug(
                "transcripts: done event for %s/%s with no current task — "
                "claim was missed (bridge started mid-task)",
                ev.recipient, ev.msg_id,
            )
            return
        now = time.time()
        rec["ended_at"] = now
        rec["status"] = "done"
        rec["response"] = (
            ev.response if ev.response is not None else ev.response_preview
        )
        self._archive(ev.recipient, rec)
        self._session_on_done(ev, rec, now)
        await self._safe_emit({
            "type": "task_done",
            "role": ev.recipient,
            "task": self._session_entry(ev.recipient, ev.msg_id),
        })

    async def _on_notify(self, ev: NotifyEvent) -> None:
        rec = self._current.get(ev.sender)
        if rec is None:
            return  # notify outside any task — drop quietly
        # Window check: started_at <= ts. ended_at is None while live.
        if ev.ts < rec["started_at"]:
            return
        rec["activity"].append({
            "ts": ev.ts,
            "kind": "notify",
            "channel": ev.channel,
            "preview": ev.preview,
        })
        self._write_current(ev.sender, rec)
        # Bump notify_count on the matching session.json entry.
        self._session_on_notify(ev.sender, rec["task_id"])
        await self._safe_emit({
            "type": "task_notify",
            "role": ev.sender,
            "task_id": rec["task_id"],
            "notify_count": self._notify_count(ev.sender, rec["task_id"]),
            "activity": {
                "ts": ev.ts, "kind": "notify",
                "channel": ev.channel, "preview": ev.preview,
            },
        })

    # ----- filesystem helpers -----

    def _role_dir(self, role: str) -> Path:
        return self.root / role

    def _write_current(self, role: str, rec: dict[str, Any]) -> None:
        path = self._role_dir(role) / CURRENT_FILE
        try:
            _atomic_write_json(path, rec)
        except OSError as e:
            log.warning("transcripts: write current.json failed (%s): %s", path, e)

    def _archive(self, role: str, rec: dict[str, Any]) -> None:
        role_dir = self._role_dir(role)
        ts = rec.get("started_at") or time.time()
        fname = f"{_iso_compact(ts)}-{rec['task_id']}.json"
        archive_path = role_dir / fname
        try:
            _atomic_write_json(archive_path, rec)
        except OSError as e:
            log.warning(
                "transcripts: archive write failed (%s): %s", archive_path, e
            )
            return
        # Remove current.json for this role — task is no longer in flight.
        try:
            (role_dir / CURRENT_FILE).unlink(missing_ok=True)
        except OSError as e:
            log.debug("transcripts: removing current.json: %s", e)
        self._prune(role_dir)

    def _prune(self, role_dir: Path) -> None:
        # Keep the N newest archived files (current.json + session.json not counted).
        try:
            archives = sorted(
                (p for p in role_dir.iterdir()
                 if p.is_file()
                 and p.name not in (CURRENT_FILE, SESSION_FILE)
                 and p.suffix == ".json"),
                key=lambda p: p.name,  # ISO-compact prefix sorts chronologically
                reverse=True,
            )
        except OSError:
            return
        for old in archives[self.retention:]:
            try:
                old.unlink()
            except OSError as e:
                log.debug("transcripts: prune failed for %s: %s", old, e)

    # ----- session.json (per-role index for fast UI consumer reads) -----

    def _session_for(self, role: str, started_at: float | None = None) -> dict[str, Any]:
        """Lazy-init the session record for this role."""
        sess = self._sessions.get(role)
        if sess is None:
            sess = {
                "session_id": self.session_id,
                "role": role,
                "started_at": started_at or time.time(),
                "current_task_id": None,
                "tasks": [],  # newest first
            }
            self._sessions[role] = sess
        return sess

    def _write_session(self, role: str) -> None:
        sess = self._sessions.get(role)
        if sess is None:
            return
        path = self._role_dir(role) / SESSION_FILE
        try:
            _atomic_write_json(path, sess)
        except OSError as e:
            log.warning("transcripts: write session.json failed (%s): %s", path, e)

    def _session_on_claim(self, ev: MessageClaimedEvent, now: float) -> None:
        sess = self._session_for(ev.recipient, started_at=now)
        entry = {
            "task_id": ev.msg_id,
            "sender": ev.sender,
            "status": "processing",
            "started_at": now,
            "ended_at": None,
            "prompt_preview": _preview(
                ev.content if ev.content is not None else ev.preview
            ),
            "response_preview": "",
            "notify_count": 0,
        }
        # Prepend (newest first), de-dup by task_id, bound by retention.
        sess["tasks"] = [t for t in sess["tasks"] if t["task_id"] != ev.msg_id]
        sess["tasks"].insert(0, entry)
        del sess["tasks"][self.retention:]
        sess["current_task_id"] = ev.msg_id
        self._write_session(ev.recipient)

    def _session_on_done(
        self, ev: MessageDoneEvent, rec: dict[str, Any], now: float,
    ) -> None:
        sess = self._sessions.get(ev.recipient)
        if sess is None:
            # done without a session record — shouldn't happen if claim was seen
            return
        self._session_update_entry(ev.recipient, ev.msg_id, {
            "status": "done",
            "ended_at": now,
            "response_preview": _preview(
                ev.response if ev.response is not None else ev.response_preview
            ),
        })
        if sess.get("current_task_id") == ev.msg_id:
            sess["current_task_id"] = None
            self._write_session(ev.recipient)

    def _session_on_notify(self, role: str, task_id: str) -> None:
        sess = self._sessions.get(role)
        if sess is None:
            return
        for entry in sess["tasks"]:
            if entry["task_id"] == task_id:
                entry["notify_count"] = int(entry.get("notify_count", 0)) + 1
                self._write_session(role)
                return

    def _session_update_entry(
        self, role: str, task_id: str, patch: dict[str, Any],
    ) -> None:
        sess = self._sessions.get(role)
        if sess is None:
            return
        for entry in sess["tasks"]:
            if entry["task_id"] == task_id:
                entry.update(patch)
                self._write_session(role)
                return

    def _session_entry(self, role: str, task_id: str) -> dict[str, Any] | None:
        sess = self._sessions.get(role)
        if sess is None:
            return None
        for entry in sess["tasks"]:
            if entry["task_id"] == task_id:
                # Return a copy — consumers shouldn't mutate the session record.
                return dict(entry)
        return None

    def _notify_count(self, role: str, task_id: str) -> int:
        entry = self._session_entry(role, task_id)
        return int(entry.get("notify_count", 0)) if entry else 0
