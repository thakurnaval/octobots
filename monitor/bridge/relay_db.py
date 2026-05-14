"""Shared helpers for writing into the supervisor's taskbox (relay.db).

The supervisor's `relay.py` owns the schema; this module duplicates only
the minimal INSERT path so the bridge can write without a subprocess
fork+exec per inbound message. Keep this thin — any schema or status-
machine logic stays in the relay module.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


def insert_taskbox_message(
    db_path: Path, sender: str, recipient: str, content: str,
) -> str:
    """Insert a pending taskbox message. Returns the new msg_id.

    Mirrors `relay.py cmd_send`'s INSERT shape: same column names, same
    status ("pending"), same WAL + busy_timeout connection profile.
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
