# Architecture

## Overview

Octobots is an AI development team where each role runs as a separate Claude Code instance. Roles communicate through a shared SQLite message queue (taskbox) and persist work on GitHub Issues.

## Design Principles

1. **Separation of concerns** — Each role has one job. BA writes stories, tech lead decomposes, PM coordinates, devs implement, QA tests.
2. **Taskbox as universal bus** — All inter-role communication flows through one SQLite queue. No special protocols.
3. **Session-per-ticket** — Each GitHub issue maps to a Claude Code named session. Context preserved per issue, no blowup.
4. **Three-file roles** — SOUL.md (personality), AGENT.md (identity + instructions), MEMORY (learnings). Easy to understand, extend, customize. Project context lives in the project's own `CLAUDE.md`, auto-loaded by Claude Code.
5. **Shared skills** — Common capabilities (git, testing, code review) maintained once, symlinked to all roles.
6. **External state** — Nothing important lives in conversation context. GitHub Issues + MEMORY.md + taskbox = the system of record.

## Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                         User                                 │
│                    (CLI or Telegram)                          │
└───────────────────────────┬─────────────────────────────────┘
                            │
              ┌─────────────┴─────────────────┐
              │                               │
              ▼                               ▼
User (Telegram)
  │ send-keys (direct to pane, no polling)
  ▼
tmux "octobots" ──────────────────────────────────────────
│                                                         │
│  ┌────────────┐  taskbox  ┌──────────┐  ┌──────────┐   │
│  │ Max (PM)   │ ────────→ │ Alex(BA) │  │ Rio(TL)  │   │
│  │ receives   │           │          │  │          │   │
│  │ user msgs  │           └──────────┘  └──────────┘   │
│  └─────┬──────┘                                        │
│        │ taskbox                                        │
│  ┌─────┴──────┐──────────────┐                         │
│  ▼             ▼              ▼                         │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐                 │
│ │ Py       │ │ Jay      │ │ Sage(QA) │                 │
│ │ worktree │ │ worktree │ │ worktree │                 │
│ └──────────┘ └──────────┘ └──────────┘                 │
│                                                         │
│ dashboard: all panes tiled, auto-refresh                │
──────────────────────────────────────────────────────────

Any role → notify MCP tool → Telegram Bot API → User
```

### Three Communication Channels

| Channel | Purpose | Who Writes | Persistence |
|---------|---------|-----------|-------------|
| **BOARD.md** | Team state: decisions, blockers, findings | All roles | Git-versioned |
| **Taskbox** | Task assignment, inter-role coordination | All roles | SQLite, ephemeral |
| **GitHub Issues** | Audit trail: every action commented | All roles | Forever |

### Notifications

Any role can notify the user directly via Telegram by calling the `notify`
MCP tool (`mcp__notify__notify`):

```
notify(message="Task #103 complete. PR #45 ready.")
notify(message="QA report attached", file="/abs/path/to/report.md")
```

No taskbox, no PM intermediary. The MCP server is a thin wrapper around
`octobots/scripts/notify_lib.py` (Python urllib → Telegram Bot API), which
is also used by the supervisor itself for internal "stuck role" warnings.

## Pipeline Flow

```
1. User → Max (Telegram → send-keys): "We need user authentication"
2. Max → Alex (taskbox): "Analyze auth requirements"
3. Alex → Rio (taskbox): Epic + user stories with ACs
4. Rio → Max (taskbox): Technical tasks with dependencies
5. Max → Py/Jay (taskbox): Individual task assignments
6. Py/Jay work in git worktrees, commit, create PRs
7. Max → Sage (taskbox): "Verify #103"
8. Sage tests, reports on GitHub issue
9. Any role → User (notify MCP tool): "Task complete" via Telegram
```

## Session Management

```
                    Supervisor (supervisor.sh)
                    polls taskbox, manages tmux,
                    runs scheduled jobs
                              │
                    tmux session "octobots"
                    ┌─────────┼──────────────┐
                    │         │              │
              ┌─────▼───┐ ┌──▼────┐   ┌─────▼───┐
              │python-dev│ │js-dev │   │qa-eng   │
              │         │ │       │   │         │
              │ Claude  │ │Claude │   │ Claude  │
              │ Code    │ │ Code  │   │  Code   │
              └─────────┘ └───────┘   └─────────┘
                  │
                  ├── /resume python-dev-issue-103  (active)
                  ├── /resume python-dev-issue-107  (swap in)
                  └── python-dev-issue-103 preserved on disk
```

- One Claude process per role
- `/resume <session>` swaps context per GitHub issue
- Previous sessions preserved — resumable anytime
- Supervisor extracts `#NNN` from taskbox messages → session name
- Workers can request restart via taskbox → supervisor relaunches fresh

## Scheduling & Loops

The supervisor includes a built-in scheduler for recurring and one-shot jobs. No LLM involved — jobs are defined and triggered directly from the supervisor.

```
Supervisor poll loop (every 15s)
  ├── Check scheduled jobs (schedule.json)
  │     ├── @role messages → taskbox send
  │     ├── run commands → shell subprocess
  │     └── agent invocations → claude subprocess
  ├── Check restart requests → relaunch workers
  ├── Poll taskbox → route to workers
  └── Poll GitHub issues → route to PM
```

### Job Types

| Type | Spec Examples | Behavior |
|------|--------------|----------|
| `at` | `15:00`, `in 2h` | One-shot, removed after execution |
| `every` | `5m`, `1h`, `1d` | Recurring interval |
| `cron` | `0 9 * * MON-FRI` | Standard 5-field cron expression |

### Job Actions

| Action | Target | What Happens |
|--------|--------|-------------|
| `@role` | Role alias | Sends a taskbox message to the worker |
| `run` | Shell command | Runs as subprocess, captures output |
| `agent` | Agent name | Spawns `claude -p` with agent's AGENT.md |

Jobs persist to `.octobots/schedule.json` across supervisor restarts.

### Worker Self-Restart

Workers can request their own restart by sending a taskbox message to `supervisor`:

```
relay.py send --from $OCTOBOTS_ID --to supervisor "restart"
```

The supervisor picks this up on the next poll cycle and relaunches the worker. This is the mechanism for workers to pick up newly added skills or agents (Claude Code discovers them at session start, not mid-conversation).

## Audit Trail

```
GitHub Issues = system of record

#100 [EPIC] User Auth
  ├── 📋 Stories: US-001..005         (Alex/BA)
  ├── 🔨 Tasks: TASK-001..006        (Rio/TL)
  ├── 📬 Assigned: python-dev, js-dev (Max/PM)
  │
  #103 TASK: Login endpoint
    ├── 🔧 Started                     (Py)
    ├── ✅ Done: PR #45                (Py)
    ├── 🧪 Testing                     (Sage)
    └── ✅ Verified                    (Sage)
```

Every meaningful action → GitHub issue comment. Taskbox for nudges, issues for the record.

## File Anatomy of a Role

```
roles/python-dev/
├── AGENT.md             Who you are (identity frontmatter) + what you do (instructions)
├── SOUL.md              Your personality, voice, and values (referenced from AGENT.md)
├── MEMORY.md            What you remember (grows over time)
└── .claude/
    ├── skills/          Symlinks to shared skills
    │   ├── taskbox → ../../../skills/taskbox
    │   ├── git-workflow → ../../../skills/git-workflow
    │   └── ...
    └── agents/
        └── rca-investigator → ../../../shared/agents/rca-investigator
```

- **AGENT.md** loaded via `claude --agent <role>` — frontmatter sets identity (name shown in prompt), body is the role's technical instructions
- **SOUL.md** loaded on demand by the AGENT.md instruction — personality kept separate from behavior
- **MEMORY.md** read at session start, updated before session end
- **Skills** discovered automatically by Claude Code from `.claude/skills/`
- **Project `CLAUDE.md`** lives at the project root (generated by scout), auto-loaded by Claude Code for project context. Symlinked into isolated worker dirs by the supervisor.

## Taskbox Protocol

```
SQLite database with WAL mode (concurrent access safe)

Messages table:
  id          TEXT PRIMARY KEY
  sender      TEXT     "python-dev"
  recipient   TEXT     "project-manager"
  content     TEXT     "TASK-003 (#103) complete. PR #45."
  response    TEXT     "Acknowledged. Routing to QA."
  status      TEXT     "pending" | "processing" | "done"
  created_at  REAL
  updated_at  REAL

Operations:
  send       → creates message with status "pending"
  inbox      → reads pending messages for a recipient
  claim      → atomically moves pending → processing
  ack        → moves processing → done, attaches response
  responses  → reads done messages where you were the sender
  stats      → counts by recipient and status
  peers      → all known participant IDs
```
