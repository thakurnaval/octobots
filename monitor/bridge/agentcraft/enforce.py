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


def ensure_analytics_disabled(path: Path = config.SETTINGS_PATH) -> Path:
    """Write `analyticsEnabled:false` to AgentCraft's settings.json,
    preserving any other keys.

    Returns the path written. Idempotent: if analytics is already off,
    rewrites with the same value (cheap, keeps the file canonical).
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
    path.write_text(json.dumps(existing, indent=2) + "\n")
    log.info("agentcraft analytics disabled in %s", path)
    return path


def is_satisfied(path: Path = config.SETTINGS_PATH) -> bool:
    """Returns True iff `analyticsEnabled` is explicitly False on disk."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict) and data.get("analyticsEnabled") is False
