"""Entry point — `python3 -m monitor.bridge` from the supervisor/ directory.

Tails `.octobots/relay.db`, `.octobots/.pane-map` (tmux), and
`.octobots/notify.log`; mirrors per-task activity into
`.agents/transcripts/<role>/` and serves the HTTP+WS endpoint at
http://127.0.0.1:2469 that the monitor UI (and any other consumer)
talks to.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from .sources.notify_log import NotifyLogTailer
from .sources.taskbox import TaskboxPoller
from .sources.tmux_panes import TmuxPanePoller
from .state import WorldState
from .transcripts import TranscriptSink
from .transcripts_http import TranscriptsHttpServer


def _session_id(started_at: float) -> str:
    """ISO-compact UTC stamp; matches the iso_compact prefix on archive files."""
    return datetime.fromtimestamp(started_at, tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ",
    )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("bridge")

    started_at = time.time()
    world = WorldState()

    # HTTP+WS server fronts the transcript files. Constructed first so we
    # can wire its `broadcast` method into the sink as the on_event callback
    # — captured as a bound method, so it stays valid across start/stop.
    http_server = TranscriptsHttpServer()

    # session_id identifies this bridge process's run. Same value lands in
    # every role's session.json so a consumer can tell which transcripts
    # belong to the same supervisor session.
    sinks = [
        TranscriptSink(
            session_id=_session_id(started_at),
            on_event=http_server.broadcast,
        ),
    ]

    for s in sinks:
        await s.start()
    await http_server.start()

    async def emit(event):
        # Fan-out: each event reaches every sink. Errors in one sink
        # don't poison the others — they're isolated per gather slot.
        await asyncio.gather(
            *(s.handle(event) for s in sinks),
            return_exceptions=True,
        )

    sources = [
        TaskboxPoller(emit, world),
        TmuxPanePoller(emit, world),
        NotifyLogTailer(emit, world),
    ]

    log.info("bridge started: session=%s", _session_id(started_at))
    await asyncio.gather(
        *(s.run() for s in sinks),
        http_server.run(),
        *(s.run() for s in sources),
    )


if __name__ == "__main__":
    asyncio.run(main())
