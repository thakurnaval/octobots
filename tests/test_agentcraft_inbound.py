"""Inbound routing — verifies user_prompt frames land in relay.db."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from monitor.bridge.agentcraft import inbound


def _init_relay_db(path: Path) -> None:
    """Mirror relay.py's schema. Kept duplicated on purpose so tests
    don't depend on the relay module's import side-effects."""
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


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "relay.db"
    _init_relay_db(p)
    return p


def _rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT sender, recipient, content, status FROM messages "
            "ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()


async def test_user_prompt_inserts_pending_row(db_path: Path):
    await inbound.route(
        {"type": "user_prompt", "sessionId": "python-dev",
         "prompt": "fix issue #42"},
        db_path=db_path,
        known_agents={"python-dev"},
    )
    assert _rows(db_path) == [
        ("user@agentcraft", "python-dev", "fix issue #42", "pending"),
    ]


async def test_user_prompt_for_unknown_agent_still_inserts(
    db_path: Path, caplog,
):
    with caplog.at_level("WARNING"):
        await inbound.route(
            {"type": "user_prompt", "sessionId": "ghost", "prompt": "hello"},
            db_path=db_path,
            known_agents={"python-dev"},
        )
    assert any("ghost" in r.message for r in caplog.records)
    assert _rows(db_path) == [
        ("user@agentcraft", "ghost", "hello", "pending"),
    ]


async def test_user_prompt_missing_fields_is_dropped(db_path: Path):
    await inbound.route(
        {"type": "user_prompt", "sessionId": "python-dev"},  # no prompt
        db_path=db_path, known_agents=None,
    )
    await inbound.route(
        {"type": "user_prompt", "prompt": "hi"},  # no sessionId
        db_path=db_path, known_agents=None,
    )
    assert _rows(db_path) == []


async def test_permission_response_ignored(db_path: Path):
    await inbound.route(
        {"type": "permission_response", "sessionId": "x",
         "requestId": "r1", "approved": True},
        db_path=db_path, known_agents=None,
    )
    assert _rows(db_path) == []


async def test_unknown_type_ignored(db_path: Path):
    await inbound.route(
        {"type": "internal_hero_active", "sessionId": "x"},
        db_path=db_path, known_agents=None,
    )
    assert _rows(db_path) == []


def test_insert_returns_unique_ids(db_path: Path):
    a = inbound.insert_taskbox_message(
        db_path, "user@agentcraft", "py", "first",
    )
    b = inbound.insert_taskbox_message(
        db_path, "user@agentcraft", "py", "second",
    )
    assert a != b
    assert len(_rows(db_path)) == 2
