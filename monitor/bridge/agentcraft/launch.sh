#!/usr/bin/env bash
# Launch `@idosal/agentcraft` with telemetry endpoints clobbered.
#
# AgentCraft 0.4.x bakes a live PostHog API key + Supabase URL into its
# npm tarball at server/dist/build-env.json. The relevant fallback chain
# in posthog.js is:
#
#   process.env.POSTHOG_API_KEY || build_env.POSTHOG_API_KEY || ""
#
# JS treats empty strings as falsy, so `POSTHOG_API_KEY=""` falls
# through to the baked key. We set non-empty bogus values to actually
# override, plus point POSTHOG_HOST and SUPABASE_URL at unreachable
# loopback addresses so any latent client init can't reach a real
# server. The Octobots bridge additionally writes
# ~/.agentcraft/settings.json with `analyticsEnabled:false`; this
# script is belt-and-braces.
#
# Usage:
#   octobots/monitor/bridge/agentcraft/launch.sh
#   octobots/monitor/bridge/agentcraft/launch.sh start -d
#   octobots/monitor/bridge/agentcraft/launch.sh stop

set -euo pipefail

POSTHOG_API_KEY="${POSTHOG_API_KEY:-disabled-by-octobots}" \
POSTHOG_HOST="${POSTHOG_HOST:-http://127.0.0.1:1}" \
SUPABASE_URL="${SUPABASE_URL:-http://127.0.0.1:1}" \
SUPABASE_ANON_KEY="${SUPABASE_ANON_KEY:-disabled-by-octobots}" \
exec npx -y @idosal/agentcraft "$@"
