"""Translation table tests — pure function, no network.

Each test pins one event variant and asserts the exact list of
(method, path, body) tuples the sink will dispatch. Frozen-time so
timestamps are deterministic.
"""
from __future__ import annotations

import pytest

from monitor.bridge.agentcraft.translate import (
    _color_index,
    _hero_customization_call,
    initial_snapshot_calls,
    translate,
)
from monitor.bridge.events import (
    AgentDespawnEvent,
    AgentSnapshot,
    AgentSpawnEvent,
    AgentStateEvent,
    MessageClaimedEvent,
    MessageDoneEvent,
    MessageSentEvent,
    NotifyEvent,
    Space,
)

NOW = 1_700_000_000.0
SPACE = Space(id="abc12345", name="myproj", path="/work/myproj", started_at=NOW - 60)
TEAM = "myproj"


def _t(event):
    return translate(event, SPACE, TEAM, NOW)


# ----- core lifecycle -----


def test_agent_spawn_emits_team_start_then_customization():
    calls = _t(AgentSpawnEvent(
        id="python-dev",
        role="python-dev",
        alias="py",
        theme={"color_word": "cyan", "icon": "🐍", "short_name": "py"},
    ))
    assert calls == [
        ("POST", "/event", {
            "type": "team_member_detected",
            "sessionId": "python-dev",
            "teamInfo": {
                "teamName": "myproj",
                "agentId": "python-dev",
                "agentType": "python-dev",
            },
        }),
        ("POST", "/event", {
            "type": "agent_start",
            "sessionId": "python-dev",
            "cwd": "/work/myproj",
            "client": "claude-code",
            "timestamp": NOW,
        }),
        ("POST", "/settings/hero/python-dev", {
            "customName": "py",
            "title": "python-dev",
            "colorIndex": 7,  # cyan
        }),
    ]


def test_agent_spawn_without_theme_falls_back_to_alias_and_role():
    calls = _t(AgentSpawnEvent(
        id="ba", role="ba", alias="alex", theme=None,
    ))
    # No theme → no colorIndex; alias is used for customName, role for title.
    assert calls[-1] == (
        "POST", "/settings/hero/ba", {"customName": "alex", "title": "ba"},
    )


def test_agent_spawn_no_meta_at_all_still_sets_title():
    calls = _t(AgentSpawnEvent(id="x", role="x", alias=None, theme=None))
    assert calls[-1] == ("POST", "/settings/hero/x", {"title": "x"})


def test_agent_spawn_with_unknown_color_word_skips_color_index():
    calls = _t(AgentSpawnEvent(
        id="weird", role="weird", alias=None,
        theme={"color_word": "fuchsia", "short_name": "w"},
    ))
    body = calls[-1][2]
    assert "colorIndex" not in body
    assert body["customName"] == "w"


def test_agent_despawn():
    calls = _t(AgentDespawnEvent(id="python-dev"))
    assert calls == [
        ("POST", "/event", {
            "type": "agent_complete",
            "sessionId": "python-dev",
            "timestamp": NOW,
            "exitCode": 0,
        }),
    ]


# ----- state events -----


@pytest.mark.parametrize("state", ["idle", "typing", "sending", "blocked"])
def test_agent_state_activity_states(state):
    calls = _t(AgentStateEvent(id="qa", state=state, since=NOW - 1))
    assert calls == [
        ("POST", "/event", {
            "type": "hero_activity_update",
            "sessionId": "qa",
            "activity": state,
            "timestamp": NOW - 1,
        }),
    ]


def test_agent_state_awaiting_reply():
    calls = _t(AgentStateEvent(id="pm", state="awaiting_reply", since=NOW - 2))
    assert calls == [
        ("POST", "/event", {
            "type": "awaiting_input",
            "sessionId": "pm",
            "timestamp": NOW - 2,
        }),
    ]


def test_agent_state_calling_user_emits_two():
    calls = _t(AgentStateEvent(id="ba", state="calling_user", since=NOW - 3))
    assert calls == [
        ("POST", "/event", {
            "type": "awaiting_input",
            "sessionId": "ba",
            "timestamp": NOW - 3,
        }),
        ("POST", "/event", {
            "type": "notification",
            "sessionId": "ba",
            "message": "ba is calling the user",
            "timestamp": NOW - 3,
        }),
    ]


# ----- message lifecycle -----


def test_message_sent_to_user_emits_notification_awaiting_and_notice():
    calls = _t(MessageSentEvent(
        msg_id="m9",
        sender="pm",
        recipient="user",
        preview="Plan: ship it. Approve? (yes/no)",
        created_at=NOW - 4,
    ))
    assert calls == [
        ("POST", "/event", {
            "type": "notification",
            "sessionId": "pm",
            "message": "Plan: ship it. Approve? (yes/no)",
            "timestamp": NOW - 4,
        }),
        ("POST", "/event", {
            "type": "awaiting_input",
            "sessionId": "pm",
            "timestamp": NOW - 4,
        }),
        ("POST", "/event", {
            "type": "notice_board_message",
            "noticeId": "m9",
            "sender": "pm",
            "recipient": "user",
            "content": "Plan: ship it. Approve? (yes/no)",
            "status": "pending",
            "teamName": "myproj",
            "timestamp": NOW - 4,
        }),
    ]


def test_message_sent_routes_to_notice_board_with_pending():
    calls = _t(MessageSentEvent(
        msg_id="m1",
        sender="pm",
        recipient="python-dev",
        preview="please implement issue #42",
        created_at=NOW - 5,
    ))
    assert calls == [
        ("POST", "/event", {
            "type": "notice_board_message",
            "noticeId": "m1",
            "sender": "pm",
            "recipient": "python-dev",
            "content": "please implement issue #42",
            "status": "pending",
            "teamName": "myproj",
            "timestamp": NOW - 5,
        }),
    ]


def test_message_claimed_rebroadcasts_with_processing_status():
    calls = _t(MessageClaimedEvent(
        msg_id="m1",
        sender="pm",
        recipient="python-dev",
        preview="please implement issue #42",
    ))
    # Same noticeId + full payload + status:processing so the UI can
    # update in place between pending and resolved.
    assert calls == [
        ("POST", "/event", {
            "type": "notice_board_message",
            "noticeId": "m1",
            "sender": "pm",
            "recipient": "python-dev",
            "content": "please implement issue #42",
            "status": "processing",
            "teamName": "myproj",
            "timestamp": NOW,
        }),
    ]


def test_message_done_resolves_notice():
    calls = _t(MessageDoneEvent(
        msg_id="m1", recipient="python-dev", response_preview="opened PR #88",
    ))
    assert calls == [
        ("POST", "/event", {
            "type": "notice_resolved",
            "noticeId": "m1",
            "recipient": "python-dev",
            "response": "opened PR #88",
            "timestamp": NOW,
        }),
    ]


# ----- notify -----


def test_notify_event():
    calls = _t(NotifyEvent(
        sender="qa", channel="telegram", preview="release blocker found", ts=NOW - 7,
    ))
    assert calls == [
        ("POST", "/event", {
            "type": "notification",
            "sessionId": "qa",
            "channel": "telegram",
            "message": "release blocker found",
            "timestamp": NOW - 7,
        }),
    ]


def test_unknown_event_returns_empty():
    class Bogus:
        pass
    assert translate(Bogus(), SPACE, TEAM, NOW) == []


# ----- helpers -----


@pytest.mark.parametrize("word, idx", [
    ("red", 0), ("orange", 1), ("yellow", 2), ("green", 3),
    ("blue", 4), ("purple", 5), ("magenta", 6), ("pink", 6),
    ("cyan", 7), ("brown", 8), ("gray", 9), ("grey", 9),
])
def test_color_index_known_words(word, idx):
    assert _color_index({"color_word": word}) == idx
    # case-insensitive
    assert _color_index({"color_word": word.upper()}) == idx


def test_color_index_returns_none_for_unknown_or_missing():
    assert _color_index(None) is None
    assert _color_index({}) is None
    assert _color_index({"color_word": "fuchsia"}) is None
    assert _color_index({"color_word": 42}) is None  # not a string


def test_hero_customization_truncates_long_strings():
    long_role = "x" * 100
    long_alias = "a" * 100
    call = _hero_customization_call(
        session_id="x", role=long_role, alias=long_alias, theme=None,
    )
    assert call is not None
    _, _, body = call
    assert len(body["customName"]) == 50
    assert len(body["title"]) == 50


def test_hero_customization_returns_none_when_no_data():
    # Empty role + no alias + no theme = nothing to customize.
    call = _hero_customization_call(
        session_id="x", role="", alias=None, theme=None,
    )
    assert call is None


# ----- snapshot replay -----


def test_initial_snapshot_emits_spawn_customization_state_per_agent():
    agents = [
        AgentSnapshot(
            id="pm", role="pm", alias="max",
            theme={"color_word": "magenta", "short_name": "pm"},
            state="idle",
        ),
        AgentSnapshot(
            id="qa", role="qa", alias=None,
            theme={"color_word": "green", "short_name": "qa"},
            state="awaiting_reply",
        ),
    ]
    calls = initial_snapshot_calls(agents, SPACE, TEAM, NOW)
    # Per agent: team_member_detected, agent_start, /settings/hero, state.
    # 4 per agent × 2 agents = 8 calls.
    assert len(calls) == 8
    types_or_paths = [(p, b.get("type", "")) for _, p, b in calls]
    assert types_or_paths == [
        ("/event", "team_member_detected"),
        ("/event", "agent_start"),
        ("/settings/hero/pm", ""),
        ("/event", "hero_activity_update"),  # idle
        ("/event", "team_member_detected"),
        ("/event", "agent_start"),
        ("/settings/hero/qa", ""),
        ("/event", "awaiting_input"),  # awaiting_reply
    ]
    # PM customization gets colorIndex 6 (magenta), QA gets 3 (green).
    assert calls[2][2]["colorIndex"] == 6
    assert calls[6][2]["colorIndex"] == 3
