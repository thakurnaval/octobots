#!/usr/bin/env bash
# Launcher for the supervisor monitor → AgentCraft bridge.
#
# Tails .octobots/relay.db, .octobots/.pane-map (tmux), and
# .octobots/notify.log; translates supervisor events into AgentCraft's
# wire protocol and forwards them to a locally-running
# `npx @idosal/agentcraft` server (default http://localhost:2468).
# Inbound prompts from the AC UI are routed back into the taskbox.
#
# Poll intervals, AC URL, and other knobs are overridable via OCTOBOTS_*
# env vars — see supervisor/monitor/bridge/config.py and
# supervisor/monitor/bridge/agentcraft/config.py.
#
# Usage (from a target project root, with .octobots/ initialized):
#   <path-to-supervisor>/scripts/monitor-bridge.sh
#   OCTOBOTS_AGENTCRAFT_URL=http://localhost:9000 <...>/scripts/monitor-bridge.sh
#
# Start AgentCraft itself with:
#   <path-to-supervisor>/monitor/bridge/agentcraft/launch.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPERVISOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

exec env PYTHONPATH="$SUPERVISOR_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m monitor.bridge "$@"
