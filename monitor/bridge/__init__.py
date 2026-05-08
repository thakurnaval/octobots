"""Octobots supervisor monitor — AgentCraft bridge.

Tails relay.db, tmux panes, and notify.log, translates supervisor events
into AgentCraft's wire protocol, and forwards them to a locally-running
`npx @idosal/agentcraft` server (default http://localhost:2468). Inbound
prompts from the AgentCraft UI are routed back into the taskbox; the
supervisor's main-loop poll then dispatches them to the role's tmux pane
via send-keys.

Run: `python3 -m monitor.bridge` from the supervisor/ directory, after
launching AgentCraft via `monitor/bridge/agentcraft/launch.sh`.
"""

__version__ = "0.1.0"
