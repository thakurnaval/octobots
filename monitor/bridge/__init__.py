"""Octobots supervisor monitor — AgentCraft bridge.

Tails relay.db, tmux panes, and notify.log, translates supervisor events
into AgentCraft's wire protocol, and forwards them to a locally-running
`npx @idosal/agentcraft` server (default http://localhost:2468). Inbound
prompts from the AgentCraft UI are routed back into the taskbox so the
target role's normal listener picks them up.

Run: `python3 -m monitor.bridge` from the supervisor/ directory, after
launching AgentCraft via `monitor/bridge/agentcraft/launch.sh`.
"""

__version__ = "0.1.0"
