"""Telemetry enforcement.

AgentCraft 0.4.x ships with a live PostHog API key + Supabase URL baked
into `server/dist/build-env.json`. The npm package's
`@idosal/agentcraft@0.4.1` LICENSE is "All Rights Reserved", so we can
neither vendor nor patch it. We disable telemetry two ways:

  1. `launch.sh` overrides POSTHOG_API_KEY / POSTHOG_HOST / SUPABASE_URL
     / SUPABASE_ANON_KEY env vars before invoking npx, short-circuiting
     the build-env fallback (the `process.env.X || build_env.X` chain
     in posthog.js).
  2. We write `analyticsEnabled:false` to ~/.agentcraft/settings.json so
     even if the user runs AC their own way, capture() calls are gated.

This module owns step 2. `ensure_analytics_disabled()` is idempotent and
preserves any other keys the user has set.

We also probe `/settings` after AC comes up and log a loud warning if
analytics is still on (user started AC before we wrote the file, or
something else is overriding it).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


def ensure_settings(path: Path = config.SETTINGS_PATH) -> Path:
    """Write the settings octobots needs to AgentCraft's settings.json,
    preserving any other keys the user has set.

    Settings written:
      - `analyticsEnabled: false` — gates PostHog capture() inside AC.
      - `projectFilter: false` — without this, AC filters our subscribed
        sessions out (`sessionBelongsToProject` returns false for our
        synthetic sessionId=role_id values, so AC won't create the
        placeholder hero on the WS subscribe path).

    Idempotent. Returns the path written. AC reads settings on startup,
    so a runtime change requires AC restart OR the matching `POST
    /settings/<key>` runtime endpoints (we use both — see sink.py).
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if not isinstance(existing, dict):
                log.warning(
                    "agentcraft settings at %s wasn't a JSON object; overwriting",
                    path,
                )
                existing = {}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("could not read %s (%s); overwriting", path, e)
            existing = {}

    existing["analyticsEnabled"] = False
    existing["projectFilter"] = False
    path.write_text(json.dumps(existing, indent=2) + "\n")
    log.info(
        "agentcraft settings written: analyticsEnabled=False, "
        "projectFilter=False (path=%s)", path,
    )
    return path


# Backwards-compatible alias — older callers used the analytics-only name.
ensure_analytics_disabled = ensure_settings


def is_satisfied(path: Path = config.SETTINGS_PATH) -> bool:
    """Returns True iff both required settings are correct on disk."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return (
        isinstance(data, dict)
        and data.get("analyticsEnabled") is False
        and data.get("projectFilter") is False
    )
