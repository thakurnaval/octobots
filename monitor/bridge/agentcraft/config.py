"""AgentCraft sink config — env-var overrides for the wire layer.

Producer-side config (paths to relay.db, poll intervals) lives in the
parent `bridge/config.py`; this module only knobs that affect how the
sink talks to AgentCraft.
"""
from __future__ import annotations

import os
from pathlib import Path

# Where the locally-running `npx @idosal/agentcraft` server is reachable.
AC_URL = os.environ.get("OCTOBOTS_AGENTCRAFT_URL", "http://localhost:2468").rstrip("/")

# Override the team name shown in AgentCraft's UI. Defaults to project dir name.
TEAM_NAME = os.environ.get("OCTOBOTS_AGENTCRAFT_TEAM_NAME", "")

# AgentCraft's persisted settings. We write `analyticsEnabled:false` here
# at sink startup; the path mirrors AC's own `getAgentCraftPath()`.
SETTINGS_PATH = Path(
    os.environ.get(
        "OCTOBOTS_AGENTCRAFT_SETTINGS",
        str(Path.home() / ".agentcraft" / "settings.json"),
    )
)

# How long to wait for an outbound HTTP POST before giving up.
HTTP_TIMEOUT = float(os.environ.get("OCTOBOTS_AGENTCRAFT_HTTP_TIMEOUT", "5.0"))

# Outbound HTTP queue depth. Drop new events when full (sources keep running).
QUEUE_SIZE = int(os.environ.get("OCTOBOTS_AGENTCRAFT_QUEUE_SIZE", "1024"))

# Health-probe cadence while AC server is unreachable.
PROBE_INTERVAL = float(os.environ.get("OCTOBOTS_AGENTCRAFT_PROBE_INTERVAL", "5.0"))

# Inbound WS reconnect backoff (seconds): caps at the last value.
WS_BACKOFF_SCHEDULE = (1.0, 2.0, 5.0, 10.0, 30.0)

# Subscribed-from terminal label sent in `{type:"subscribe", terminal}`.
TERMINAL_LABEL = "octobots-bridge"
