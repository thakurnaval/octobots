"""AgentCraft sink — translates supervisor events to AgentCraft's wire
protocol and forwards them to a locally-running `npx @idosal/agentcraft`
server. Inbound user prompts from the AgentCraft UI are routed back into
the supervisor via the taskbox (relay.db).

Default endpoint: http://localhost:2468 (override via
OCTOBOTS_AGENTCRAFT_URL).
"""
