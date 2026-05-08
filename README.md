# Octobots

![Octobots Hero](docs/assets/hero.jpg)

AI development team powered by Claude Code. Each role runs as a separate Claude Code instance in tmux, communicating via SQLite queue, with sessions mapped to GitHub issues and Telegram as the user interface.

## Installation

Run this from your **project root**:

```bash
curl -fsSL https://raw.githubusercontent.com/arozumenko/octobots/main/install.sh | bash
```

Downloads a tarball from GitHub, extracts to `/tmp`, copies the framework to `octobots/` in your project, installs Python deps, initializes `.octobots/`, and seeds `.claude/`. No nested git repo — `octobots/` is just files (gitignored automatically).

Re-run at any time to update to the latest version.

## Quick Start

```bash
# 1. Configure Telegram (optional but recommended)
echo 'OCTOBOTS_TG_TOKEN=your-bot-token' >> .env.octobots
echo 'OCTOBOTS_TG_OWNER=your-telegram-user-id' >> .env.octobots

# 2. Explore the project and generate config
octobots/start.sh scout

# 3. Start the team (all roles in tmux)
python3 octobots/scripts/supervisor.py

# 4. Watch the dashboard
tmux attach -t octobots
```

## Team

| Role | Name | Does | Doesn't |
|------|------|------|---------|
| **Scout** | Kit | Explores codebase, seeds config | Write code |
| **BA** | Alex | Goals → epics → user stories | Prescribe implementation |
| **Tech Lead** | Rio | Stories → technical tasks + deps | Distribute work |
| **PM** | Max | Distributes, tracks, unblocks | Implement or test |
| **Python Dev** | Py | Backend code, APIs, data | Frontend |
| **JS Dev** | Jay | Frontend, React, Node | Backend |
| **QA Engineer** | Sage | Tests, reproduces, verifies | Fix bugs |

## Architecture

```
User (Telegram)
  │
  ▼ send-keys
tmux "octobots"
├── project-manager ← Max distributes via taskbox (reads board for live roster)
├── python-dev      ← Py picks up tasks, works in isolated repo clone
├── js-dev          ← Jay picks up tasks, works in isolated repo clone
├── qa-engineer     ← Sage tests from project root (staging env)
├── ba              ← Alex writes user stories
├── tech-lead       ← Rio decomposes stories into tasks
└── [roles are dynamic — add/remove/clone at runtime without restart]

Any role → notify MCP tool → Telegram (direct notifications)
```

### Communication — Three Channels

| Channel | Purpose | Persistence |
|---------|---------|-------------|
| **board.md** | Team state — supervisor writes `## Team` (roster) and `## Active Work` (taskbox queue); agents write decisions, blockers, findings | In .octobots/ |
| **Taskbox** | Inter-role task assignment and coordination | SQLite, ephemeral |
| **GitHub Issues** | Permanent audit trail (every action gets a comment) | Forever |

The board is the single shared state file. PM reads it before routing any task — the `## Team` section tells it who is actually running and which Worker ID to use in taskbox.

### Session Management

Each GitHub issue maps to a Claude Code named session:

```
Issue #103 → session "python-dev-issue-103" → full context preserved
Issue #107 → session "python-dev-issue-107" → separate context
Back to #103 → /resume python-dev-issue-103 → context restored
```

No context blowup. Each task has its own session. Fully resumable.

### Worker Isolation

Workers with `workspace: clone` in their `AGENT.md` frontmatter get isolated repo clones. Other workers share the project root via symlinks.

```
.octobots/workers/
├── python-dev/    ← own repo clones, own branch, own .env  (workspace: clone)
└── js-dev/        ← own repo clones                         (workspace: clone)
```

`qa-engineer` runs from the project root — it reads staging state and doesn't write code, so no clone is needed.

Each role also declares which skills it uses via `skills:` frontmatter — workers only get symlinks for those skills, not all skills.

## Scheduling & Loops

The supervisor runs jobs on a schedule — same `@role` syntax as Telegram. No LLM involved.

```bash
/schedule every 30m @pm Check status of all tasks
/schedule at 15:00 @py Review PR #42
/schedule cron 0 9 * * MON-FRI @ba Daily standup report
/schedule every 1h run git fetch --all
/schedule every 30m agent rca-investigator Check flaky tests

/loop 30m @qa Run regression tests       # shortcut for /schedule every

/jobs                                     # list all
/jobs cancel <id>                         # remove
/jobs pause <id>                          # disable temporarily
```

Workers can self-restart to pick up new skills/agents:
```bash
relay.py send --from $OCTOBOTS_ID --to supervisor "restart"
```

The supervisor also holds pending taskbox messages until a worker's current task is acked — no message pile-up on busy workers.

## Structure

```
octobots/                            ← FRAMEWORK (git pull, read-only)
├── supervisor.sh                      Thin wrapper → scripts/supervisor.py
├── start.sh                           Launch a role interactively
├── roles/<role>/                      Base role templates
│   ├── AGENT.md                         Identity (frontmatter) + technical instructions
│   ├── SOUL.md                          Personality, voice, quirks
│   └── .claude/{skills,agents}/ →       Symlinks to shared
├── shared/
│   ├── agents/                        Shared agents (rca-investigator, etc.)
│   └── conventions/                   Teamwork, audit trail, sessions
├── skills/                            10 shared skills
└── scripts/
    ├── supervisor.py                  Rich TUI supervisor + scheduler
    ├── telegram-bridge.py             Telegram ↔ tmux bridge
    ├── scheduler.py                   Schedule/loop engine
    ├── roles.py                       Shared role aliases (@pm, @qa, etc.)
    ├── notify_lib.py                  Telegram transport (used by notify MCP + supervisor)
    ├── init-project.sh                Initialize .octobots/ for a project
    └── requirements.txt               Python deps (rich, telegram, dotenv)

.octobots/                           ← RUNTIME (project-specific, read/write)
├── board.md                           Team board — Team + Active Work (supervisor); rest (agents)
├── memory/<role>.md                   Per-role persistent learnings
├── roles/                             Project role overrides
├── skills/                            Project-specific skills
├── agents/                            Project-specific agents
├── workers/                           Isolated worker environments
│   ├── python-dev/                      Own repo clones + own .claude/ (filtered skills)
│   ├── js-dev/                          Own repo clones + own .claude/
│   └── <role>-2/                        Clone of a role, own workspace
├── relay.db                           Taskbox database
├── schedule.json                      Scheduled jobs (persistent)
└── profile.md, conventions.md, ...    Scout output
```

## Configuration

All config in `.env.octobots` (project root or octobots/):

```bash
# Telegram
OCTOBOTS_TG_TOKEN=your-bot-token
OCTOBOTS_TG_OWNER=your-telegram-user-id

# Workers (optional — auto-discovers from roles/ if not set)
OCTOBOTS_WORKERS=project-manager python-dev js-dev qa-engineer
OCTOBOTS_EXCLUDED_ROLES=scout

# Worktree roles (which roles get isolated git worktrees)
# Default: python-dev js-dev qa-engineer
```

## Watching the Team

```bash
# Dashboard — all workers tiled, auto-refreshing
tmux attach -t octobots:dashboard

# Individual worker — full interactive access
tmux attach -t octobots:python-dev

# Inside tmux:
# Ctrl+B, w — pick any window
# Ctrl+B, n/p — next/previous window
# Ctrl+B, d — detach (everything keeps running)
```

## Pipeline Flow

```
1. User → Max (Telegram): "We need user authentication"
2. Max → Alex (taskbox): "Analyze auth requirements"
3. Alex → Rio (taskbox): Epic + user stories with ACs
4. Rio → Max (taskbox): Technical tasks with dependencies
5. Max → Py/Jay (taskbox): Individual task assignments
6. Py/Jay work in worktrees, commit, create PRs
7. Max → Sage (taskbox): "Verify #103"
8. Sage tests, reports findings on GitHub issue
9. Any role → User (notify MCP tool): status updates via Telegram
```

## Dynamic Team Management

Roles and skills can be added, removed, and cloned at runtime — no supervisor restart needed.

### Adding / removing roles

```bash
# In the supervisor prompt:
/role list                                  # see available roles and which are active
/role add python-dev                        # install from agents.json (repo + pinned ref)
/role add arozumenko/my-agent@main          # install directly from a GitHub repo
/role add my-existing-role                  # use an agent already in .claude/agents/
/role remove my-role                        # stop + clear .octobots/workers/<role>/
/role clone python-dev                      # spawn python-dev-2 with own isolated workspace
/role clone python-dev py-auth              # explicit alias
```

`/role add` never moves files. It installs agents into `.claude/agents/` (via `npx github:<repo> init`) and launches them in place. `/role remove` leaves `.claude/agents/<role>/` alone — uninstall the agent separately if you want it gone.

### Adding skills to a role

```bash
/skill python-dev tdd           # add skill live: symlink + update AGENT.md
/skill all taskbox              # add to every worker at once
```

### Defining a new role from scratch

Two options:

**Option A — publish as an agent repo** (survivable across updates, shareable):

```bash
# Add agents/<name>/{AGENT.md,SOUL.md} to arozumenko/sdlc-skills (or your fork),
# register it in agents.json, then install:
npx github:arozumenko/sdlc-skills init --agents <name> --target claude
/role add <name>
```

**Option B — project-local role** (lives in `.octobots/roles/`, not shared):

```bash
mkdir -p .octobots/roles/my-role
# Create .octobots/roles/my-role/AGENT.md with frontmatter:
```

```yaml
---
name: my-role
description: One-line description of this role
model: sonnet
color: cyan
skills: [tdd, code-review]   # only skills this role needs
# workspace: clone           # uncomment if this role writes code
---
```

Add `SOUL.md` for personality, then `/role add my-role` to start it live.

## Documentation

- [Setup Guide](docs/setup.md) — Installation, first run, Telegram, troubleshooting
- [Architecture](docs/architecture.md) — Design principles, components, session management
- [Skill Spec](docs/skill-spec.md) — How to create new skills (agentskills.io standard)

## License

Apache-2.0
