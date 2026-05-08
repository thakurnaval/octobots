"""AgentCraftSink — translates source events into AgentCraft HTTP/WS calls.

Lifecycle:

  1. `start()` — synchronous prep: write `analyticsEnabled:false` to AC's
     settings.json, log endpoint + team name. No network yet.

  2. `run()` — long-running. Probes /health until AC is reachable, then:
       a. Verify settings.analyticsEnabled is False on the live server.
          (Loud warning if not — the user must restart AC for our
           settings-file write to take effect.)
       b. Replay current WorldState as `team_member_detected` +
          `agent_start` + state events so AC sees the existing team.
       c. Subscribe to inbound prompts for every current agent.
       d. Run the outbound POST workers and the inbound WS loop forever.

  3. `handle(event)` — called by every source poller. Subscribes the WS
     to new agents on AgentSpawnEvent, translates the event, enqueues
     the resulting HTTP calls. Dispatch is non-blocking; the client's
     queue absorbs short bursts and drops oldest on overflow.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from .. import config as bridge_config
from ..events import AgentSpawnEvent, Space
from ..state import WorldState
from . import config, enforce, inbound
from .client import AgentCraftClient
from .translate import initial_snapshot_calls, translate

log = logging.getLogger(__name__)


class AgentCraftSink:
    def __init__(
        self,
        world: WorldState,
        space: Space,
        team_name: str | None = None,
    ) -> None:
        self.world = world
        self.space = space
        self.team_name = (
            team_name
            or config.TEAM_NAME
            or os.environ.get("OCTOBOTS_AGENTCRAFT_TEAM_NAME", "")
            or space.name
        )
        self.client = AgentCraftClient(on_inbound=self._handle_inbound)

    # --------------- lifecycle ---------------

    async def start(self) -> None:
        try:
            enforce.ensure_settings()
        except OSError as e:
            log.warning(
                "could not write agentcraft settings.json (%s); "
                "telemetry will only be off if launch.sh is used", e,
            )
        log.info(
            "agentcraft sink configured: url=%s team=%s settings=%s",
            self.client.url, self.team_name, config.SETTINGS_PATH,
        )

    async def run(self) -> None:
        await self._wait_for_ac()
        await self._verify_analytics_off()
        await self._disable_project_filter_runtime()
        await self._replay_snapshot()
        await asyncio.gather(
            self.client.run_outbound(),
            self.client.run_inbound(),
        )

    # --------------- per-event ---------------

    async def handle(self, event: Any) -> None:
        # New agents need a WS subscription so user_prompt frames from
        # the AC UI find their way back to us.
        if isinstance(event, AgentSpawnEvent):
            await self.client.subscribe(event.id)

        for method, path, body in translate(
            event, self.space, self.team_name, time.time(),
        ):
            await self.client.post(method, path, body)

    async def _handle_inbound(self, msg: dict[str, Any]) -> None:
        await inbound.route(
            msg,
            db_path=bridge_config.RELAY_DB,
            known_agents=set(self.world.agents.keys()),
        )

    # --------------- internals ---------------

    async def _wait_for_ac(self) -> None:
        attempts = 0
        while True:
            if await self.client.health():
                log.info("agentcraft reachable at %s", self.client.url)
                return
            attempts += 1
            if attempts == 1 or attempts % 12 == 0:
                # Log on first miss, then every minute (12 * 5s).
                log.info(
                    "waiting for agentcraft at %s (every %.0fs)",
                    self.client.url, config.PROBE_INTERVAL,
                )
            await asyncio.sleep(config.PROBE_INTERVAL)

    async def _verify_analytics_off(self) -> None:
        settings = await self.client.get_settings()
        if settings is None:
            log.info(
                "could not GET %s/settings — proceeding; relying on "
                "settings-file write + launch.sh env overrides",
                self.client.url,
            )
            return
        if settings.get("analyticsEnabled") is False:
            log.info("agentcraft analytics confirmed off")
            return
        log.warning(
            "AGENTCRAFT TELEMETRY MAY BE ON: server reports "
            "analyticsEnabled=%s. Restart agentcraft to pick up the "
            "settings.json we wrote (%s), or use launch.sh.",
            settings.get("analyticsEnabled"),
            config.SETTINGS_PATH,
        )

    async def _disable_project_filter_runtime(self) -> None:
        """Flip AC's projectFilter off in-memory so heroes appear.

        Without this, AC filters our WS subscribes (synthetic sessionIds
        don't match its ~/.claude/projects/<slug>/ scan results) and no
        placeholder heroes get created.

        We also write projectFilter:false to settings.json (in start())
        so the next AC restart picks it up automatically.
        """
        ok = await self.client.disable_project_filter()
        if ok:
            log.info("agentcraft projectFilter disabled at runtime")
        else:
            log.warning(
                "could not POST /settings/project-filter at runtime — "
                "heroes may not appear until agentcraft restarts and "
                "reads the settings.json we wrote",
            )

    async def _replay_snapshot(self) -> None:
        agents = self.world.snapshot_agents()
        if not agents:
            log.info("agentcraft replay: no agents in WorldState yet")
            return
        log.info("agentcraft replay: %d agents", len(agents))
        for a in agents:
            await self.client.subscribe(a.id)
        for method, path, body in initial_snapshot_calls(
            agents, self.space, self.team_name, time.time(),
        ):
            await self.client.post(method, path, body)
