"""In-memory world state — agents + recent message buffer.

Single mutator (the source pollers), single consumer (the AgentCraft sink
on startup, for replaying current agents as team members). Sources update
it as they observe real events.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from . import config
from .events import AgentSnapshot, AgentStateLiteral, MessageSnapshot


@dataclass
class WorldState:
    agents: dict[str, AgentSnapshot] = field(default_factory=dict)
    messages: deque = field(
        default_factory=lambda: deque(maxlen=config.REPLAY_BUFFER_SIZE)
    )

    def put_agent(self, agent: AgentSnapshot) -> None:
        self.agents[agent.id] = agent

    def remove_agent(self, agent_id: str) -> None:
        self.agents.pop(agent_id, None)

    def set_agent_state(self, agent_id: str, state: AgentStateLiteral) -> bool:
        a = self.agents.get(agent_id)
        if a is None or a.state == state:
            return False
        a.state = state
        return True

    def upsert_message(self, msg: MessageSnapshot) -> None:
        for i, existing in enumerate(self.messages):
            if existing.msg_id == msg.msg_id:
                self.messages[i] = msg
                return
        self.messages.append(msg)

    def snapshot_agents(self) -> list[AgentSnapshot]:
        return list(self.agents.values())
