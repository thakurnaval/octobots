# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Octobots is a framework for orchestrating multiple Claude Code instances as an AI development team. It's installed into target projects as a sibling directory (`octobots/`), not run standalone. Roles (PM, dev, QA, BA, tech lead, scout) and most published skills now live in the single `arozumenko/sdlc-skills` monorepo and are installed via `npx github:arozumenko/sdlc-skills init --agents <name> --skills <name> --target claude`. Roles communicate through a shared SQLite queue (taskbox).

## Commands

```bash
# Validate infrastructure readiness
python3 octobots/scripts/check-spawn-ready.py
python3 octobots/scripts/check-spawn-ready.py --check infra-only
python3 octobots/scripts/check-spawn-ready.py --check files-only

# Check shell script syntax
bash -n octobots/scripts/*.sh

# Install Python dependencies
pip install -r octobots/requirements.txt

# Start a single role (interactive, from project root)
octobots/start.sh scout
octobots/start.sh python-dev

# Start full team (Rich TUI supervisor)
python3 octobots/scripts/supervisor.py

# Taskbox CLI
python octobots/skills/taskbox/scripts/relay.py send --from py --to pm "message"
python octobots/skills/taskbox/scripts/relay.py inbox --id pm
python octobots/skills/taskbox/scripts/relay.py ack MSG_ID "acked"

# Agent registry / team selector (non-interactive)
python3 octobots/scripts/select-agents.py --preset 0   # first preset
python3 octobots/scripts/select-agents.py --all        # all agents
```

There is no test suite for the framework itself — tests live in target projects.

## Architecture

### Role System (Decoupled)

Roles live in `arozumenko/sdlc-skills/agents/<name>/` (a few third-party agents like `onetest-ai/qa-agent` remain on their own). Each agent dir has:
- `AGENT.md` — YAML frontmatter (name, model, color, `workspace`, skills list) + technical instructions
- `SOUL.md` — Personality, voice, working style

Install a role: `npx github:arozumenko/sdlc-skills init --agents <name> --target claude`
Install several at once: `npx github:arozumenko/sdlc-skills init --agents ba,tech-lead,pm --target claude`

Role resolution order (both `start.sh` and `supervisor.py`):
1. `.octobots/roles/<role>/` (project overrides)
2. `.claude/agents/<role>/` (installed via `npx github:<repo> init`)

`octobots/roles/` is no longer a role source. The supervisor reads from
`.claude/agents/` directly — no promotion, no moving files out of user-owned
directories. `/role add <id>` / `/role add owner/repo[@ref]` installs into
`.claude/agents/` via `registry-fetch.sh` and then launches the worker in place.
`/role remove` only tears down `.octobots/workers/<role>/` and leaves
`.claude/agents/<role>/` intact — uninstall the agent separately if you want it
gone.

Roles with `workspace: clone` get isolated repo clones under `.octobots/workers/<role>/`.

### Agent Registry

`agents.json` lists all published agents and team presets. Used by:
- `install.sh` via `scripts/select-agents.py` for cookiecutter team setup
- Scout during project onboarding to propose team adjustments

```json
{
  "monorepo": { "id": "sdlc-skills", "repo": "arozumenko/sdlc-skills", "ref": "main" },
  "agents": [{
    "id": "scout", "monorepo": "sdlc-skills", "name": "scout", "required": true,
    "group": "core"
  }, ...],
  "presets": [{ "name": "iOS development", "agents": [...], "qa": "qa-sage" }, ...]
}
```

`agents.json` is **install-time only** — it tells `select-agents.py` how to
install each agent (monorepo + name, or third-party repo) and which group
to show it under in the Custom flow (`core` | `dev` | `qa`). Runtime
metadata (theme for tmux panes, aliases for @shorthands) lives in each
agent's AGENT.md frontmatter and is loaded by `scripts/agent_registry.py`
at supervisor startup. For third-party agents that don't ship those
fields, `octobots/agent-overrides.json` provides an overlay.

Adding a new agent: register in `agents.json` for the picker, ensure the
agent's own AGENT.md carries `group`/`theme`/`aliases`/`required` fields
(for sdlc-skills agents this is just frontmatter), and — if it's a
third-party agent you can't modify upstream — drop an override entry in
`agent-overrides.json`.

### Taskbox (Inter-Role Messaging)

All inter-role communication flows through a single SQLite database (`.octobots/relay.db`). No REST APIs, no MCP servers for messaging — just Python stdlib + Bash. The supervisor holds incoming messages until the worker acknowledges the current task, preventing pile-up.

### Session-Per-Issue Pattern

Each GitHub issue maps to a named Claude Code session (e.g., `python-dev-issue-103`). Roles use `/resume <session-name>` to restore full context when returning to a task. This keeps context isolated and prevents bloat.

### Three Communication Channels

| Channel | Purpose | Persistence |
|---|---|---|
| `board.md` | Team state — decisions, blockers, active work | Git-versioned |
| Taskbox | Task assignment, async nudges | SQLite, ephemeral |
| GitHub Issues | Permanent audit trail, every action commented | Permanent |

### Pipeline Flow

```
User → PM (Max) → BA (Alex): analyze requirements
BA → Tech Lead (Rio): epic + user stories
Tech Lead → PM: technical tasks
PM → Dev (Py/Jay): assignments
Dev → PM: PR created
PM → QA (Sage): verify PR
Any role → User: `notify` MCP tool → Telegram
```

### Supervisor (`scripts/supervisor.py`)

Rich TUI that manages tmux panes, polls taskbox, runs scheduled jobs, and maintains the team board. Key REPL commands:

```
/role list|add|remove|clone       # dynamic team management
/skill <role> <skill>             # add skill to running role
/schedule every 30m @pm <task>    # cron-style scheduling
/loop 10m @qa <task>              # recurring loop
```

### Skills System

Bundled skills in `skills/<name>/` are reusable capabilities symlinked into each role's `.claude/skills/`. Published skills (code-review, git-workflow, tdd, memory, etc.) live in `arozumenko/sdlc-skills/skills/<name>/` and are installed via `npx github:arozumenko/sdlc-skills init --skills <name> --target claude`. The `SKILL.md` file defines the skill per the agentskills.io spec.

Bundled skills (still in this repo):
- `taskbox` — inter-role messaging relay
- `memory` — per-role persistent memory; supervisor invokes `memory.py snapshot` at every role launch
- `bugfix-workflow` — structured bug investigation
- `implement-feature` — feature implementation workflow
- `plan-feature` — feature planning workflow
- `project-seeder` — scout's project configuration skill

Shared agents in `shared/agents/`:
- `issue-reproducer` — reproduces GitHub issues
- `rca-investigator` — root cause analysis

Note: the supervisor's main loop polls the taskbox directly (default 15 s)
and dispatches via tmux send-keys, so no per-role inbox-listener subagent
is needed. Operations on `relay.py` are exposed via the `taskbox` skill,
not a separate agent.

### Worker Environments

`scripts/init-project.sh` sets up `.octobots/workers/<role>/` for each discovered role. Each worker gets:
- Symlinks to shared resources (`octobots/`, `.octobots/`, `.env`, etc.)
- `.claude/agents/<role>/` — symlink to the role's agent dir
- `.claude/skills/` — symlinks to allowed skills (filtered by `skills:` in AGENT.md)
- `OCTOBOTS.md` — generated per-worker config (Worker ID, taskbox commands, memory path)
- `CLAUDE.md` — imports `@shared/conventions.md` + `@OCTOBOTS.md` (written once, user-editable)

Clone workers (`workspace: clone`) additionally get isolated git clones for each repo.

### Runtime directories (`.agents/` + `.octobots/`)

Created by `scripts/init-project.sh` in the target project, not in the octobots repo itself. The split mirrors the content / orchestration architecture:

- **`.agents/`** — IDE-neutral content read by every agent on every IDE (scout writes it):
  - `profile.md`, `architecture.md`, `conventions.md`, `testing.md`, `team-comms.md`, `onboarding.md`
  - `memory/<role>/` — memory-skill-spec directory (MEMORY.md index, curated entries, daily/ logs, snapshot.md)

- **`.octobots/`** — supervisor runtime state (only meaningful when the supervisor is running):
  - `board.md` — Team board (auto-created if missing)
  - `relay.db` — SQLite taskbox
  - `workers/` — Isolated worker environments
  - `roles/` — project role overrides
  - `schedule.json` — Persistent scheduled jobs
  - `registry/` — cached clones of third-party agent/skill repos
  - `roles-manifest.yaml` — check-spawn-ready.py input (scout generates)

## Key Conventions

**Terminal Rules (Critical):** Roles run in unattended tmux panes. They must never ask questions to stdout or present options and wait. All user communication goes through the `notify` MCP tool (`mcp__notify__notify`, defined in `mcp/notify/server.py` and registered in `.mcp.json`). The transport logic lives in `scripts/notify_lib.py` and is shared with the supervisor's own internal warnings. All teammate communication goes through taskbox.

**Three-Step Task Completion (mandatory for all roles):**
1. Comment on GitHub issue with results
2. Ack the taskbox message
3. Notify user via the `notify` MCP tool

Skipping any step breaks the pipeline.

**GitHub Labels Track Status:** `ready` → `in-progress` → `review` → `testing` → `done`. Before starting a task, check for `in-progress` label to avoid duplicate work.

## Configuration

`.env.octobots` (in target project root, not in this repo):
```bash
OCTOBOTS_TG_TOKEN=...         # Telegram bot token
OCTOBOTS_TG_OWNER=...         # Telegram user ID for notifications
OCTOBOTS_WORKERS=project-manager python-dev js-dev qa-engineer ba tech-lead
OCTOBOTS_EXCLUDED_ROLES=scout
```

`.mcp.json` configures MCP servers available to all roles: Playwright, GitHub, Context7, Tavily (web search), Accessibility Scanner, Lighthouse.

## Adding a New Role

1. Add `agents/<name>/AGENT.md` (with YAML frontmatter) and `agents/<name>/SOUL.md` to `arozumenko/sdlc-skills`.
   The AGENT.md frontmatter carries all runtime metadata:
   - `group: core | dev | qa` — where it appears in the Custom selector
   - `theme: {color: colour<N>, icon: "<emoji>", short_name: <short>}` — tmux pane styling
   - `aliases: [short, nickname, ...]` — @shorthand resolution (e.g. `["io", "ios"]` for ios-dev)
   - `required: true` — only for scout-style always-on agents
   - `workspace: clone` — if the role needs an isolated repo clone
2. Register in `agents.json` for the install-time picker (id, monorepo+name or repo, role, description, group).
3. No Python changes required — `supervisor.py`, `scripts/roles.py`, and `scripts/select-agents.py`
   all read from installed AGENT.md frontmatter (via `scripts/agent_registry.py`) or from `agents.json` directly.
4. For third-party agents you can't modify upstream, add an overlay entry in `agent-overrides.json` instead.

## Adding a New Skill

Follow the agentskills.io spec documented in `docs/skill-spec.md`. The `SKILL.md` file is the definition. Scripts go in `skills/<name>/scripts/`. The skill is activated by symlinking into a role's `.claude/skills/`.

Skill resolution now lives in **sdlc-skills**:
- Published skills (monorepo): add to `arozumenko/sdlc-skills/skills/<name>/` and register in `arozumenko/sdlc-skills/skills.json` with `monorepo: sdlc-skills`.
- External skills (third-party repos): register in `arozumenko/sdlc-skills/skills.json` with `repo: owner/repo` (+ optional `ref`, + optional `subdir` for multi-skill repos). The sdlc-skills installer clones + symlinks them automatically.
- Bundled framework-internal skills (e.g. taskbox, memory): add to `skills/<name>/` in this repo.

The supervisor's `install.sh` delegates all content installation to `npx github:arozumenko/sdlc-skills init`, which auto-resolves each agent's declared skills — monorepo and external alike.
