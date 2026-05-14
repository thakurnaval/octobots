"""Event dataclasses produced by the source pollers.

These are the bridge's *internal* event vocabulary — what the sources emit
and what sinks consume. Each sink decides how to translate them onto its
target wire protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

AgentStateLiteral = Literal[
    "idle", "typing", "sending", "awaiting_reply", "blocked", "calling_user"
]


@dataclass
class AgentSnapshot:
    id: str
    role: str
    alias: str | None = None
    theme: dict[str, Any] | None = None
    state: AgentStateLiteral = "idle"


@dataclass
class MessageSnapshot:
    msg_id: str
    sender: str
    recipient: str
    status: str
    preview: str
    created_at: float
    updated_at: float


@dataclass
class AgentSpawnEvent:
    id: str
    role: str
    alias: str | None
    theme: dict[str, Any] | None


@dataclass
class AgentDespawnEvent:
    id: str


@dataclass
class AgentStateEvent:
    id: str
    state: AgentStateLiteral
    since: float


@dataclass
class MessageSentEvent:
    msg_id: str
    sender: str
    recipient: str
    preview: str
    created_at: float
    content: str | None = None  # full body; sinks needing fidelity (transcripts) read this


@dataclass
class MessageClaimedEvent:
    msg_id: str
    sender: str
    recipient: str
    preview: str
    content: str | None = None


@dataclass
class MessageDoneEvent:
    msg_id: str
    recipient: str
    response_preview: str
    response: str | None = None  # full response body; preview-only sinks ignore this


@dataclass
class NotifyEvent:
    sender: str
    channel: str
    preview: str
    ts: float
