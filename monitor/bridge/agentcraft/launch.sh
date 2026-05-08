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
# override, plus point POSTHOG_HOST and SUPABASE_URL at hosts that
# fail at DNS resolution (`*.invalid` is reserved by RFC 2606 — never
# resolves) so any latent client init can't reach a real server. The
# Octobots bridge additionally writes ~/.agentcraft/settings.json with
# `analyticsEnabled:false`; this script is belt-and-braces.
#
# IMPORTANT: don't use port 1 (or 7, 9, 11, 13, 15, 17, 19, 21, 22, 23,
# 25, 53, 110, 143, 6000, etc.) — they're on undici's hard-coded fetch
# block-list and produce a synchronous "Error: bad port" on every
# attempt, which spams the AC log on each WebSocket connect. A
# `.invalid` host fails as a normal network error, which the AC auth
# manager handles silently.
#
# If you still see token-validation spam after switching to this
# launcher, AC's browser tab has a cached Supabase session from an
# earlier run. Open AC in the browser, sign out, refresh — the spam
# stops because the browser no longer sends a token to validate.
#
# Usage:
#   octobots/monitor/bridge/agentcraft/launch.sh
#   octobots/monitor/bridge/agentcraft/launch.sh start -d
#   octobots/monitor/bridge/agentcraft/launch.sh stop

set -euo pipefail

POSTHOG_API_KEY="${POSTHOG_API_KEY:-disabled-by-octobots}" \
POSTHOG_HOST="${POSTHOG_HOST:-https://posthog.disabled-by-octobots.invalid}" \
SUPABASE_URL="${SUPABASE_URL:-https://supabase.disabled-by-octobots.invalid}" \
SUPABASE_ANON_KEY="${SUPABASE_ANON_KEY:-disabled-by-octobots}" \
exec npx -y @idosal/agentcraft "$@"
