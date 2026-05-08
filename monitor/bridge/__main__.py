"""Entry point — `python3 -m monitor.bridge` from the supervisor/ directory.

Wires the three source pollers into a single AgentCraftSink. Sources call
`sink.handle(event)` on every change; the sink fans out to
http://localhost:2468 (the locally-running `npx @idosal/agentcraft`).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path

from . import config
from .agentcraft.sink import AgentCraftSink
from .events import Space
from .sources.notify_log import NotifyLogTailer
from .sources.taskbox import TaskboxPoller
from .sources.tmux_panes import TmuxPanePoller
from .state import WorldState


def _space_id(db_path: Path) -> str:
    return hashlib.sha256(str(db_path).encode()).hexdigest()[:12]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("bridge")

    space = Space(
        id=_space_id(config.RELAY_DB),
        name=config.PROJECT_ROOT.name,
        path=str(config.PROJECT_ROOT),
        started_at=time.time(),
    )
    world = WorldState()
    sink = AgentCraftSink(world=world, space=space)
    await sink.start()

    sources = [
        TaskboxPoller(sink.handle, world),
        TmuxPanePoller(sink.handle, world),
        NotifyLogTailer(sink.handle, world),
    ]

    log.info("space: id=%s name=%s path=%s", space.id, space.name, space.path)
    await asyncio.gather(
        sink.run(),
        *(s.run() for s in sources),
    )


if __name__ == "__main__":
    asyncio.run(main())
