#!/usr/bin/env bash
# Launcher for the supervisor monitor bridge.
#
# Tails .octobots/relay.db, .octobots/.pane-map (tmux), and
# .octobots/notify.log; mirrors per-task activity into
# .agents/transcripts/<role>/; and serves a small HTTP+WS endpoint at
# http://127.0.0.1:2469 that the monitor UI (and any other consumer)
# talks to.
#
# Poll intervals, transcripts root, HTTP bind, and other knobs are
# overridable via OCTOBOTS_* env vars — see
# supervisor/monitor/bridge/config.py and supervisor/docs/bridge.md.
#
# Usage (from a target project root, with .octobots/ initialized):
#   <path-to-supervisor>/scripts/monitor-bridge.sh
#   OCTOBOTS_TRANSCRIPTS_HTTP_PORT=9000 <...>/scripts/monitor-bridge.sh
#
# Normal users don't invoke this directly — the supervisor's `/monitor`
# slash command spawns it as a background subprocess.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPERVISOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

exec env PYTHONPATH="$SUPERVISOR_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m monitor.bridge "$@"
