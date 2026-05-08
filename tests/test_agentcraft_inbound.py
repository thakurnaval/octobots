"""Inbound routing — verifies user_prompt frames land in relay.db,
plus enforce.ensure_settings semantics."""
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


def test_ensure_settings_writes_both_keys(tmp_path: Path):
    import json
    from monitor.bridge.agentcraft import enforce

    p = tmp_path / "settings.json"
    enforce.ensure_settings(p)
    data = json.loads(p.read_text())
    assert data == {"analyticsEnabled": False, "projectFilter": False}
    assert enforce.is_satisfied(p)


def test_ensure_settings_preserves_other_keys(tmp_path: Path):
    import json
    from monitor.bridge.agentcraft import enforce

    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "analyticsEnabled": True,  # will be overwritten
        "projectFilter": True,     # will be overwritten
        "mapTheme": "dark",        # preserved
        "masterVolume": 0.7,       # preserved
    }))
    enforce.ensure_settings(p)
    data = json.loads(p.read_text())
    assert data == {
        "analyticsEnabled": False,
        "projectFilter": False,
        "mapTheme": "dark",
        "masterVolume": 0.7,
    }


def test_is_satisfied_requires_both_keys(tmp_path: Path):
    import json
    from monitor.bridge.agentcraft import enforce

    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"analyticsEnabled": False}))  # missing projectFilter
    assert not enforce.is_satisfied(p)
    p.write_text(json.dumps({"analyticsEnabled": False, "projectFilter": False}))
    assert enforce.is_satisfied(p)
