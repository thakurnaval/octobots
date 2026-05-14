"""Bridge config — defaults with env-var overrides.

Source paths + poll intervals + transcripts root + HTTP server bind.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("OCTOBOTS_PROJECT_ROOT", Path.cwd())).resolve()
# Installed project resources — `.claude/skills/`, `.claude/agents/`,
# `.mcp.json`. In live mode this is the same as PROJECT_ROOT, but in /sim
# mode the bridge runs against a sandbox directory and we still want the
# UI to see the real installed agents and skills. Sim launches set this
# explicitly to the original project root.
RESOURCES_ROOT = Path(
    os.environ.get("OCTOBOTS_RESOURCES_ROOT", PROJECT_ROOT)
).resolve()
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

# Per-role task transcript mirror (TranscriptSink).
TRANSCRIPTS_ROOT = Path(
    os.environ.get(
        "OCTOBOTS_TRANSCRIPTS_ROOT", PROJECT_ROOT / ".agents" / "transcripts"
    )
).resolve()
TRANSCRIPTS_RETENTION = int(os.environ.get("OCTOBOTS_TRANSCRIPTS_RETENTION", "20"))

# Read-only HTTP endpoint that exposes the transcripts to UI consumers.
TRANSCRIPTS_HTTP_HOST = os.environ.get("OCTOBOTS_TRANSCRIPTS_HTTP_HOST", "127.0.0.1")
TRANSCRIPTS_HTTP_PORT = int(os.environ.get("OCTOBOTS_TRANSCRIPTS_HTTP_PORT", "2469"))
