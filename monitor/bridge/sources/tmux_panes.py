"""Tmux pane poller — derives idle/typing from pane content, manages
agent spawn/despawn, surfaces role metadata to the UI.

Pane map: supervisor writes `.octobots/.pane-map` (one `role=pane_target`
line per role). We reload it every poll cycle and reconcile WorldState:
new roles -> AgentSpawnEvent, removed roles -> AgentDespawnEvent.

State derivation: for each pane, capture the last N lines via
`tmux capture-pane -p`, hash the result, and compare with the previous
hash to detect activity. Typing recency < TYPING_RECENCY -> typing,
otherwise -> idle. We only flip between idle and typing — non-derived
states (calling_user, blocked, awaiting_reply) set by other sources are
respected and not overridden.

Role metadata (theme/alias) comes from AGENT.md frontmatter:
  1. .octobots/roles/<role>/AGENT.md   (project overrides)
  2. .claude/agents/<role>/AGENT.md    (installed)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from .. import config
from ..events import (
    AgentDespawnEvent,
    AgentSnapshot,
    AgentSpawnEvent,
    AgentStateEvent,
)
from ..state import WorldState

log = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]

TYPING_RECENCY = 1.5  # seconds — pane changed within this window -> typing


def _read_pane_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        role, _, target = line.partition("=")
        role = role.strip()
        target = target.strip()
        if role and target:
            out[role] = target
    return out


def _load_agent_meta(role: str) -> dict[str, Any]:
    candidates = [
        config.PROJECT_ROOT / ".octobots" / "roles" / role / "AGENT.md",
        config.PROJECT_ROOT / ".claude" / "agents" / role / "AGENT.md",
    ]
    for p in candidates:
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if not text.startswith("---"):
            return {}
        try:
            _, fm, _ = text.split("---", 2)
            return yaml.safe_load(fm) or {}
        except Exception:
            return {}
    return {}


def _theme_for(meta: dict[str, Any]) -> dict[str, Any] | None:
    theme = meta.get("theme")
    if not isinstance(theme, dict):
        # Even without a `theme:` block, surface the top-level `color`
        # word (e.g. "blue", "magenta") so downstream sinks have
        # something to map to their own palette.
        color_word = meta.get("color")
        if color_word:
            return {"color_word": color_word}
        return None
    return {
        "color": theme.get("color"),
        "color_word": meta.get("color"),
        "icon": theme.get("icon"),
        "short_name": theme.get("short_name"),
    }


def _first_alias(meta: dict[str, Any]) -> str | None:
    aliases = meta.get("aliases")
    if isinstance(aliases, list) and aliases:
        first = aliases[0]
        return str(first) if first else None
    return None


def _capture_pane(target: str, lines: int) -> str | None:
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
            capture_output=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class TmuxPanePoller:
    def __init__(
        self,
        emit: EmitFn,
        world: WorldState,
        pane_map_path: Path = config.PANE_MAP,
    ) -> None:
        self.emit = emit
        self.world = world
        self.pane_map_path = pane_map_path
        self._hashes: dict[str, str] = {}
        self._last_change: dict[str, float] = {}

    async def _ensure_agents(self, pane_map: dict[str, str]) -> None:
        active = set(pane_map.keys())
        current = set(self.world.agents.keys())

        for role in active - current:
            meta = await asyncio.to_thread(_load_agent_meta, role)
            theme = _theme_for(meta)
            alias = _first_alias(meta)
            display_role = str(meta.get("name") or role)
            self.world.put_agent(AgentSnapshot(
                id=role,
                role=display_role,
                alias=alias,
                theme=theme,
                state="idle",
            ))
            await self.emit(AgentSpawnEvent(
                id=role, role=display_role, alias=alias, theme=theme,
            ))

        for role in current - active:
            self.world.remove_agent(role)
            self._hashes.pop(role, None)
            self._last_change.pop(role, None)
            await self.emit(AgentDespawnEvent(id=role))

    async def _update_state(self, role: str, target: str) -> None:
        text = await asyncio.to_thread(
            _capture_pane, target, config.TMUX_CAPTURE_LINES
        )
        if text is None:
            return
        h = _hash_text(text)
        now = time.time()
        prev = self._hashes.get(role)
        if prev is None:
            # First capture — set baseline, don't fire a state event yet.
            self._hashes[role] = h
            return
        if h != prev:
            self._last_change[role] = now
        self._hashes[role] = h

        agent = self.world.agents.get(role)
        if agent is None or agent.state not in ("idle", "typing"):
            return  # respect non-derived states (calling_user, blocked, etc.)

        last = self._last_change.get(role, 0.0)
        new_state = "typing" if (now - last) < TYPING_RECENCY else "idle"
        if self.world.set_agent_state(role, new_state):
            since = last if new_state == "typing" else now
            await self.emit(AgentStateEvent(
                id=role, state=new_state, since=since,
            ))

    async def run(self) -> None:
        log.info(
            "tmux poller started (interval=%.2fs, pane_map=%s)",
            config.TMUX_POLL_INTERVAL, self.pane_map_path,
        )
        while True:
            pane_map = await asyncio.to_thread(_read_pane_map, self.pane_map_path)
            await self._ensure_agents(pane_map)
            for role, target in pane_map.items():
                await self._update_state(role, target)
            await asyncio.sleep(config.TMUX_POLL_INTERVAL)
