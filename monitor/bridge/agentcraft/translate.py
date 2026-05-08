"""Pure translation: supervisor event dataclass -> AgentCraft HTTP calls.

Each input event yields zero or more `Call` tuples. The sink consumes the
list and pushes them onto its outbound queue. No I/O here — this module
exists so the wire-format mapping is unit-testable without a live AC
server.

Wire-protocol notes (extracted from `@idosal/agentcraft@0.4.1` server
bundle, not officially documented):

  - Generic sink: `POST /event` with `{type: <eventType>, ...payload}`.
  - Sessions are identified by `sessionId`. We use the role id (e.g.
    "python-dev").
  - Team primitive: `team_member_detected` event +
    `/team-messages/:teamName/:memberName` route group all roles under
    one team. Team appearance is set via
    `POST /settings/team-customization` (see _team_customization_call).
  - Hero appearance: `POST /settings/hero/:heroId` with
    {customName ≤50, title ≤50, colorIndex 0..13}. We map
    AGENT.md's top-level `color` word to colorIndex via _color_index().
  - Notice board (`notice_board_message`, `notice_resolved`) is the
    closest analogue to taskbox messages between roles. We re-broadcast
    notice_board_message with a `status` field on MessageClaimedEvent so
    the UI can show progress between sent and resolved.

If AC 0.5+ renames event types, restructures payloads, or changes the
allowed enum values, this module is the only thing that has to change.
"""
from __future__ import annotations

from typing import Any

from ..events import (
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

Call = tuple[str, str, dict[str, Any]]  # (method, path, body)

CLIENT = "claude-code"

# Magic recipient string for taskbox messages addressed to the user
# rather than another role. PM is the canonical user-facing role; any
# role is allowed to address the user this way. The bridge surfaces
# these as `awaiting_input` + `notification` on the sender hero so the
# UI shows it as paused. The user replies via the standard
# user_prompt → taskbox path.
USER_RECIPIENT = "user"

# Mapping AGENT.md top-level `color` word → AC hero colorIndex (0..13).
# AC's hero palette has 14 slots; we map the 10 word-colors used in
# sdlc-skills frontmatter to the closest match. Unknown words → no
# colorIndex (the customization call simply omits the field).
_COLOR_INDEX = {
    "red": 0,
    "orange": 1,
    "yellow": 2,
    "green": 3,
    "blue": 4,
    "purple": 5,
    "magenta": 6,
    "pink": 6,
    "cyan": 7,
    "brown": 8,
    "gray": 9,
    "grey": 9,
    "white": 9,
    "black": 9,
}

# Allowed icons for AC's team customization. Simple icon mapping from
# common emojis to AC's heraldic-glyph vocab. Unknown emojis → no icon.
_TEAM_ICON_FROM_EMOJI = {
    "🛡": "shield", "🛡️": "shield", "⚔": "sword", "⚔️": "sword",
    "👑": "crown", "💀": "skull", "⭐": "star", "🌟": "star",
    "🔥": "flame",
}


def _color_index(theme: dict[str, Any] | None) -> int | None:
    if not theme:
        return None
    word = theme.get("color_word")
    if not isinstance(word, str):
        return None
    return _COLOR_INDEX.get(word.lower())


def _hero_customization_call(
    *,
    session_id: str,
    role: str,
    alias: str | None,
    theme: dict[str, Any] | None,
) -> Call | None:
    body: dict[str, Any] = {}

    custom_name = None
    if theme and isinstance(theme.get("short_name"), str):
        custom_name = theme["short_name"]
    elif alias:
        custom_name = alias
    if custom_name:
        body["customName"] = custom_name[:50]

    if role:
        body["title"] = role[:50]

    idx = _color_index(theme)
    if idx is not None:
        body["colorIndex"] = idx

    if not body:
        return None
    return ("POST", f"/settings/hero/{session_id}", body)


def _spawn_calls(
    *,
    session_id: str,
    role: str,
    alias: str | None,
    theme: dict[str, Any] | None,
    team_name: str,
    cwd: str,
    now: float,
) -> list[Call]:
    calls: list[Call] = [
        ("POST", "/event", {
            "type": "team_member_detected",
            "sessionId": session_id,
            "teamInfo": {
                "teamName": team_name,
                "agentId": session_id,
                "agentType": role,
            },
        }),
        ("POST", "/event", {
            "type": "agent_start",
            "sessionId": session_id,
            "cwd": cwd,
            "client": CLIENT,
            "timestamp": now,
        }),
    ]
    custom = _hero_customization_call(
        session_id=session_id, role=role, alias=alias, theme=theme,
    )
    if custom is not None:
        calls.append(custom)
    return calls


def translate(event: Any, space: Space, team_name: str, now: float) -> list[Call]:
    if isinstance(event, AgentSpawnEvent):
        return _spawn_calls(
            session_id=event.id,
            role=event.role,
            alias=event.alias,
            theme=event.theme,
            team_name=team_name,
            cwd=space.path,
            now=now,
        )

    if isinstance(event, AgentDespawnEvent):
        return [("POST", "/event", {
            "type": "agent_complete",
            "sessionId": event.id,
            "timestamp": now,
            "exitCode": 0,
        })]

    if isinstance(event, AgentStateEvent):
        s = event.state
        if s == "awaiting_reply":
            return [("POST", "/event", {
                "type": "awaiting_input",
                "sessionId": event.id,
                "timestamp": event.since,
            })]
        if s == "calling_user":
            return [
                ("POST", "/event", {
                    "type": "awaiting_input",
                    "sessionId": event.id,
                    "timestamp": event.since,
                }),
                ("POST", "/event", {
                    "type": "notification",
                    "sessionId": event.id,
                    "message": f"{event.id} is calling the user",
                    "timestamp": event.since,
                }),
            ]
        # idle | typing | sending | blocked → hero_activity_update
        return [("POST", "/event", {
            "type": "hero_activity_update",
            "sessionId": event.id,
            "activity": s,
            "timestamp": event.since,
        })]

    if isinstance(event, MessageSentEvent):
        notice = ("POST", "/event", {
            "type": "notice_board_message",
            "noticeId": event.msg_id,
            "sender": event.sender,
            "recipient": event.recipient,
            "content": event.preview,
            "status": "pending",
            "teamName": team_name,
            "timestamp": event.created_at,
        })
        if event.recipient == USER_RECIPIENT:
            # User-facing message: surface on the sender's hero as a
            # notification + paused state so the user knows to reply.
            # Keep the notice for chat history.
            return [
                ("POST", "/event", {
                    "type": "notification",
                    "sessionId": event.sender,
                    "message": event.preview,
                    "timestamp": event.created_at,
                }),
                ("POST", "/event", {
                    "type": "awaiting_input",
                    "sessionId": event.sender,
                    "timestamp": event.created_at,
                }),
                notice,
            ]
        return [notice]

    if isinstance(event, MessageClaimedEvent):
        # Re-broadcast the notice with status:"processing" so the UI has
        # a non-static intermediate step between sent and resolved. Same
        # noticeId so any noticeId-keyed renderer merges in place; full
        # payload (sender, recipient, content) repeated so renderers
        # that expect a complete message don't choke on a partial one.
        return [("POST", "/event", {
            "type": "notice_board_message",
            "noticeId": event.msg_id,
            "sender": event.sender,
            "recipient": event.recipient,
            "content": event.preview,
            "status": "processing",
            "teamName": team_name,
            "timestamp": now,
        })]

    if isinstance(event, MessageDoneEvent):
        return [("POST", "/event", {
            "type": "notice_resolved",
            "noticeId": event.msg_id,
            "recipient": event.recipient,
            "response": event.response_preview,
            "timestamp": now,
        })]

    if isinstance(event, NotifyEvent):
        return [("POST", "/event", {
            "type": "notification",
            "sessionId": event.sender,
            "channel": event.channel,
            "message": event.preview,
            "timestamp": event.ts,
        })]

    return []


def initial_snapshot_calls(
    agents: list[AgentSnapshot], space: Space, team_name: str, now: float,
) -> list[Call]:
    """Burst emitted once at sink startup so AC sees the existing team.

    For each agent already in WorldState, replay its spawn calls (which
    now include hero customization) plus a state event reflecting its
    current state.
    """
    calls: list[Call] = []
    for a in agents:
        calls.extend(_spawn_calls(
            session_id=a.id,
            role=a.role,
            alias=a.alias,
            theme=a.theme,
            team_name=team_name,
            cwd=space.path,
            now=now,
        ))
        calls.extend(translate(
            AgentStateEvent(id=a.id, state=a.state, since=now),
            space, team_name, now,
        ))
    return calls
