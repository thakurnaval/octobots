"""Inbound message routing — AgentCraft UI -> supervisor taskbox.

Frame shapes the AC server sends to subscribed external listeners:

  - {type:"user_prompt", sessionId, prompt}
        UI typed a prompt aimed at a session. We insert a pending row
        into relay.db with sender="user@agentcraft" and
        recipient=sessionId. The supervisor's main-loop poll
        (scripts/supervisor.py, default 15s interval) sees the row and
        dispatches it to the role's tmux pane via send-keys — same
        code path as a user message from any other source.

  - {type:"permission_response", sessionId, requestId, approved}
        UI clicked approve/deny. Supervisor doesn't yet have a
        permission gate at the role level, so v1 logs and drops.

Anything else (`internal_hero_*`, broadcast types intended for AC's own
spawned agents) is ignored.

The SQLite write here is intentionally a duplicate of `relay.py
cmd_send` rather than a subprocess call: same `INSERT` shape, same
column names, same status="pending". Keeping it inline avoids the
per-prompt fork+exec latency.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

INBOUND_SENDER = "user@agentcraft"


def insert_taskbox_message(
    db_path: Path, sender: str, recipient: str, content: str,
) -> str:
    """Insert a pending message into relay.db. Returns msg_id.

    Uses WAL + busy_timeout to match relay.py's connection profile.
    """
    msg_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "INSERT INTO messages "
            "(id, sender, recipient, content, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (msg_id, sender, recipient, content, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return msg_id


async def route(
    msg: dict[str, Any],
    db_path: Path,
    known_agents: set[str] | None = None,
) -> None:
    """Dispatch one inbound WS frame from AgentCraft.

    `known_agents` is consulted only to log a warning when prompts
    arrive for unknown roles; we still insert so the user's input
    isn't lost if the role is briefly missing from WorldState.
    """
    msg_type = msg.get("type")
    if msg_type == "user_prompt":
        session_id = msg.get("sessionId")
        prompt = msg.get("prompt")
        if not session_id or not prompt:
            log.warning("inbound user_prompt missing sessionId or prompt: %r", msg)
            return
        if known_agents is not None and session_id not in known_agents:
            log.warning(
                "inbound prompt for unknown role %r — inserting anyway",
                session_id,
            )
        msg_id = insert_taskbox_message(
            db_path,
            sender=INBOUND_SENDER,
            recipient=str(session_id),
            content=str(prompt),
        )
        log.info(
            "inbound prompt -> taskbox: id=%s recipient=%s len=%d",
            msg_id, session_id, len(prompt),
        )
        return

    if msg_type == "permission_response":
        # No supervisor-side gate yet — drop with a debug log so v2 has
        # a breadcrumb if/when we add one.
        log.debug(
            "permission_response ignored (no gate in v1): sessionId=%s approved=%s",
            msg.get("sessionId"), msg.get("approved"),
        )
        return

    log.debug("inbound %s ignored", msg_type)
