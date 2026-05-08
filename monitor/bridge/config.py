"""Bridge config — defaults with env-var overrides.

Producer-side knobs only (paths to sources, poll intervals). Sink-specific
configuration lives next to each sink (e.g. agentcraft/config.py).
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("OCTOBOTS_PROJECT_ROOT", Path.cwd())).resolve()
RELAY_DB = Path(
    os.environ.get("OCTOBOTS_RELAY_DB", PROJECT_ROOT / ".octobots" / "relay.db")
).resolve()
NOTIFY_LOG = Path(
    os.environ.get("OCTOBOTS_NOTIFY_LOG", PROJECT_ROOT / ".octobots" / "notify.log")
).resolve()
PANE_MAP = Path(
    os.environ.get("OCTOBOTS_PANE_MAP", PROJECT_ROOT / ".octobots" / ".pane-map")
).resolve()

TASKBOX_POLL_INTERVAL = float(os.environ.get("OCTOBOTS_TASKBOX_INTERVAL", "0.2"))
TMUX_POLL_INTERVAL = float(os.environ.get("OCTOBOTS_TMUX_INTERVAL", "0.5"))
NOTIFY_POLL_INTERVAL = float(os.environ.get("OCTOBOTS_NOTIFY_INTERVAL", "0.5"))

REPLAY_BUFFER_SIZE = int(os.environ.get("OCTOBOTS_REPLAY_BUFFER", "100"))
TMUX_CAPTURE_LINES = int(os.environ.get("OCTOBOTS_TMUX_CAPTURE_LINES", "10"))

PREVIEW_LEN = 200
