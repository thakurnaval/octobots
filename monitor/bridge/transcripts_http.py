"""Bidirectional HTTP+WS bridge over `.agents/transcripts/`.

Runs alongside the bridge's other sinks. Exposes:

  GET  /healthz                        → {"ok": true}
  GET  /transcripts                    → {roles: [summary, ...]}
  GET  /transcripts/<role>             → session.json contents
  GET  /transcripts/<role>/<task_id>   → full per-task record (current or archived)
  GET  /events                         → WebSocket — snapshot frame on connect,
                                          live frames for task_claimed / task_done /
                                          task_notify / task_abandoned
  POST /messages/<role>                → insert a pending row in relay.db addressed
                                          to <role>; supervisor's main-loop poll then
                                          dispatches to the role's tmux pane via
                                          send-keys. Body: {"content": str,
                                          "sender"?: str}. Returns {"task_id": str}.

  GET  /supervisor/workers             → roles the supervisor is actively managing
                                          (parsed from .octobots/.pane-map).
  GET  /supervisor/agents              → agents available under .claude/agents/.
  GET  /supervisor/skills              → skills available under .claude/skills/.
  GET  /supervisor/mcp                 → MCP server names from .mcp.json.
  GET  /supervisor/board               → contents of .octobots/board.md as text.
  GET  /supervisor/tasks               → pending taskbox rows from relay.db.
  GET  /supervisor/jobs                → .octobots/schedule.json contents.
  POST /supervisor/command             → enqueue a REPL slash command for the
                                          supervisor to run (e.g. "/role add scout").
                                          Body: {"command": str, "sender"?: str}.
                                          Inserts a row in relay.db with recipient
                                          "@supervisor"; the supervisor poll loop
                                          picks it up and runs handle_command().

Bound to 127.0.0.1 by default — the POST endpoint can insert work for any role.
Don't expose to the network without adding auth.

CORS: `Access-Control-Allow-Origin: *` so a same-machine browser UI can fetch.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from . import config
from .relay_db import insert_taskbox_message
from .transcripts import CURRENT_FILE, SESSION_FILE

log = logging.getLogger(__name__)

INBOUND_SENDER_DEFAULT = "user@transcripts"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.debug("transcripts_http: read %s failed: %s", path, e)
        return None


def _list_roles(root: Path) -> list[str]:
    if not root.exists():
        return []
    try:
        return sorted(
            p.name for p in root.iterdir()
            if p.is_dir() and (p / SESSION_FILE).exists()
        )
    except OSError:
        return []


def _read_pane_map(path: Path) -> list[dict[str, str]]:
    """Parse `.octobots/.pane-map`. Each line is `role=pane_target`.
    Returns [{role, pane}, ...] sorted by role. Missing file → []."""
    if not path.exists():
        return []
    out: list[dict[str, str]] = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        if "=" not in line:
            continue
        role, _, target = line.partition("=")
        role = role.strip()
        target = target.strip()
        if role and target:
            out.append({"role": role, "pane": target})
    out.sort(key=lambda r: r["role"])
    return out


def _list_dir_with_marker(root: Path, marker: str) -> list[dict[str, Any]]:
    """List subdirs of `root` that contain a file named `marker`. Returns
    [{name, has_marker: True}, ...]. Used to enumerate agents/skills."""
    if not root.exists() or not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    try:
        for p in sorted(root.iterdir()):
            if not p.is_dir():
                continue
            if not (p / marker).exists():
                continue
            out.append({"name": p.name})
    except OSError:
        return []
    return out


def _parse_skills_frontmatter(agent_md: Path) -> list[str]:
    """Pull the `skills: [a, b, c]` line out of an AGENT.md front-matter
    block. Returns [] on any parse trouble — this is best-effort, the
    bridge is read-only and we don't want to crash on a malformed file.

    We deliberately avoid pulling in PyYAML: the front-matter line is a
    single inline-list and a regex covers it with no extra dep.
    """
    if not agent_md.is_file():
        return []
    try:
        text = agent_md.read_text(errors="replace")
    except OSError:
        return []
    # Front-matter is between the first two `---` lines.
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end < 0:
        return []
    fm = text[3:end]
    # Match `skills: [a, b, c]` — case-insensitive; allow leading whitespace.
    m = re.search(r"(?im)^\s*skills\s*:\s*\[([^\]]*)\]\s*$", fm)
    if not m:
        return []
    items = [s.strip().strip('"').strip("'") for s in m.group(1).split(",")]
    return [s for s in items if s]


def _list_agents_with_skills(agents_root: Path) -> list[dict[str, Any]]:
    """Like _list_dir_with_marker but also pulls the attached-skills list
    out of each agent's AGENT.md front-matter, so the UI can show which
    skills a role already has."""
    if not agents_root.exists() or not agents_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    try:
        for p in sorted(agents_root.iterdir()):
            if not p.is_dir():
                continue
            agent_md = p / "AGENT.md"
            if not agent_md.exists():
                continue
            out.append({
                "name": p.name,
                "skills": _parse_skills_frontmatter(agent_md),
            })
    except OSError:
        return []
    return out


def _read_mcp_servers(path: Path) -> list[dict[str, Any]]:
    """Parse `.mcp.json`. Returns [{name, command?}, ...] sorted by name.
    Missing file → []."""
    if not path.exists():
        return []
    data = _read_json(path)
    if not isinstance(data, dict):
        return []
    servers = data.get("mcpServers") or {}
    if not isinstance(servers, dict):
        return []
    out: list[dict[str, Any]] = []
    for name, cfg in servers.items():
        entry: dict[str, Any] = {"name": str(name)}
        if isinstance(cfg, dict):
            cmd = cfg.get("command")
            if isinstance(cmd, str):
                entry["command"] = cmd
            url = cfg.get("url")
            if isinstance(url, str):
                entry["url"] = url
        out.append(entry)
    out.sort(key=lambda r: r["name"])
    return out


def _find_task_file(role_dir: Path, task_id: str) -> Path | None:
    current = role_dir / CURRENT_FILE
    if current.exists():
        rec = _read_json(current)
        if rec and rec.get("task_id") == task_id:
            return current
    try:
        for p in role_dir.iterdir():
            if p.is_file() and p.suffix == ".json" and p.name.endswith(f"-{task_id}.json"):
                return p
    except OSError:
        return None
    return None


def _safe_name(s: str) -> bool:
    """Reject path-traversal-y role/task ids."""
    return bool(s) and "/" not in s and "\\" not in s and not s.startswith(".")


def _cors(resp: web.StreamResponse) -> web.StreamResponse:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _json_response(payload: Any, status: int = 200) -> web.Response:
    r = web.json_response(payload, status=status)
    _cors(r)
    return r


def _snapshot(root: Path) -> dict[str, Any]:
    """Full state seed sent over WS on connect."""
    roles = []
    for r in _list_roles(root):
        sess = _read_json(root / r / SESSION_FILE)
        if sess is not None:
            roles.append(sess)
    return {"type": "snapshot", "roles": roles}


STATE_KEY: web.AppKey["_ServerState"] = web.AppKey("transcripts_state", object)


def build_app(
    root: Path = config.TRANSCRIPTS_ROOT,
    relay_db: Path = config.RELAY_DB,
) -> web.Application:
    """Construct the aiohttp app.

    The runtime state (WS client set + broadcast helper) is attached to
    the app at `app[STATE_KEY]`.
    """
    root = Path(root)
    relay_db = Path(relay_db)
    state = _ServerState(root)

    async def healthz(_: web.Request) -> web.Response:
        return _json_response({"ok": True})

    async def list_index(_: web.Request) -> web.Response:
        roles = _list_roles(root)
        out: list[dict[str, Any]] = []
        for role in roles:
            sess = _read_json(root / role / SESSION_FILE)
            if sess is None:
                continue
            out.append({
                "role": role,
                "session_id": sess.get("session_id"),
                "started_at": sess.get("started_at"),
                "current_task_id": sess.get("current_task_id"),
                "task_count": len(sess.get("tasks", [])),
            })
        return _json_response({"roles": out})

    async def get_role(req: web.Request) -> web.Response:
        role = req.match_info["role"]
        if not _safe_name(role):
            return _json_response({"error": "invalid role"}, status=400)
        sess = _read_json(root / role / SESSION_FILE)
        if sess is None:
            return _json_response({"error": "not found"}, status=404)
        return _json_response(sess)

    async def get_task(req: web.Request) -> web.Response:
        role = req.match_info["role"]
        task_id = req.match_info["task_id"]
        if not (_safe_name(role) and _safe_name(task_id)):
            return _json_response({"error": "invalid path"}, status=400)
        path = _find_task_file(root / role, task_id)
        if path is None:
            return _json_response({"error": "not found"}, status=404)
        rec = _read_json(path)
        if rec is None:
            return _json_response({"error": "unreadable"}, status=500)
        return _json_response(rec)

    async def post_message(req: web.Request) -> web.Response:
        role = req.match_info["role"]
        if not _safe_name(role):
            return _json_response({"error": "invalid role"}, status=400)
        try:
            body = await req.json()
        except (json.JSONDecodeError, ValueError):
            return _json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return _json_response({"error": "body must be a JSON object"}, status=400)
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return _json_response(
                {"error": "content must be a non-empty string"}, status=400,
            )
        sender = body.get("sender") or INBOUND_SENDER_DEFAULT
        if not isinstance(sender, str) or not sender.strip():
            return _json_response({"error": "sender must be a string"}, status=400)
        try:
            msg_id = insert_taskbox_message(
                db_path=relay_db,
                sender=sender,
                recipient=role,
                content=content,
            )
        except Exception as e:
            log.warning("transcripts_http: relay.db insert failed: %s", e)
            return _json_response({"error": "insert failed"}, status=500)
        log.info(
            "transcripts_http: inbound message → relay.db: id=%s recipient=%s sender=%s len=%d",
            msg_id, role, sender, len(content),
        )
        return _json_response({"task_id": msg_id, "recipient": role}, status=201)

    async def ws_events(req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(req)
        state.clients.add(ws)
        try:
            # Snapshot on connect so a late joiner has the full picture.
            await ws.send_json(_snapshot(root))
            async for msg in ws:
                # We don't expect inbound frames; drain pings/text to keep the
                # connection alive and ignore anything else.
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            state.clients.discard(ws)
        return ws

    # ─── /supervisor/* — read-only views of the project layout ─────────────
    # The bridge process already runs in PROJECT_ROOT, so .octobots/,
    # .claude/, .mcp.json are all reachable via config.PROJECT_ROOT. In
    # sandbox mode (when /sim is active) most of these don't exist and
    # the endpoints just return empty lists.

    async def get_workers(_: web.Request) -> web.Response:
        pane_map_path = config.PANE_MAP
        return _json_response({"workers": _read_pane_map(pane_map_path)})

    async def get_agents(_: web.Request) -> web.Response:
        # Resources (agents/skills/mcp) live with the installed project, not
        # the sandbox — see config.RESOURCES_ROOT.
        agents_dir = config.RESOURCES_ROOT / ".claude" / "agents"
        return _json_response({"agents": _list_agents_with_skills(agents_dir)})

    async def get_skills(_: web.Request) -> web.Response:
        skills_dir = config.RESOURCES_ROOT / ".claude" / "skills"
        return _json_response({"skills": _list_dir_with_marker(skills_dir, "SKILL.md")})

    async def get_mcp(_: web.Request) -> web.Response:
        mcp_path = config.RESOURCES_ROOT / ".mcp.json"
        return _json_response({"servers": _read_mcp_servers(mcp_path)})

    async def get_board(_: web.Request) -> web.Response:
        path = config.PROJECT_ROOT / ".octobots" / "board.md"
        if not path.exists():
            return _json_response({"text": "", "exists": False})
        try:
            return _json_response({"text": path.read_text(errors="replace"), "exists": True})
        except OSError as e:
            log.warning("read board.md failed: %s", e)
            return _json_response({"error": "unreadable"}, status=500)

    async def get_tasks(_: web.Request) -> web.Response:
        import sqlite3
        db_path = config.RELAY_DB
        if not db_path.exists():
            return _json_response({"tasks": []})
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute(
                    "SELECT id, sender, recipient, content, status, "
                    "       created_at, updated_at "
                    "FROM messages "
                    "WHERE status IN ('pending', 'processing') "
                    "ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.warning("tasks query failed: %s", e)
            return _json_response({"error": "unreadable"}, status=500)
        tasks = [
            {
                "id": r[0], "sender": r[1], "recipient": r[2],
                "content_preview": (r[3] or "")[:200],
                "status": r[4],
                "created_at": r[5], "updated_at": r[6],
            } for r in rows
        ]
        return _json_response({"tasks": tasks})

    async def get_houses(_: web.Request) -> web.Response:
        """Top-level project folders, one per "house" on the map.

        Reads from PROJECT_ROOT (not RESOURCES_ROOT) on purpose — houses
        represent the *project the supervisor is operating on*, which in
        /sim mode is the sandbox with the user's seeded files. Skills and
        agents stay on RESOURCES_ROOT because the installed catalogue
        lives with the supervisor workspace.

        Skips hidden dirs (start with `.`) and runtime/build artifacts
        that carry no semantic meaning. The remaining list IS the
        project's shape so the UI just renders one labelled house per
        entry — no per-project config needed.
        """
        root = config.PROJECT_ROOT
        DENY = {
            "cache", "log", "logs", "tmp", "dist", "build", "out", "target",
            "node_modules", "venv", ".venv", "env", "__pycache__",
            "vendor",  # PHP composer
            "octobots",  # framework injection, has its own keep-out
            "coverage", ".pytest_cache", ".mypy_cache",
        }
        houses: list[dict[str, Any]] = []
        try:
            for p in sorted(root.iterdir()):
                if not p.is_dir():
                    continue
                if p.name.startswith("."):
                    continue
                if p.name.lower() in DENY:
                    continue
                houses.append({"name": p.name})
        except OSError as e:
            log.warning("houses scan failed: %s", e)
        return _json_response({"houses": houses})

    async def get_jobs(_: web.Request) -> web.Response:
        path = config.PROJECT_ROOT / ".octobots" / "schedule.json"
        if not path.exists():
            return _json_response({"jobs": [], "exists": False})
        data = _read_json(path)
        if data is None:
            return _json_response({"error": "unreadable"}, status=500)
        # schedule.json is typically a list of job dicts but could vary.
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        return _json_response({"jobs": jobs, "exists": True})

    async def post_command(req: web.Request) -> web.Response:
        try:
            body = await req.json()
        except (json.JSONDecodeError, ValueError):
            return _json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return _json_response({"error": "body must be a JSON object"}, status=400)
        command = body.get("command")
        if not isinstance(command, str) or not command.strip():
            return _json_response(
                {"error": "command must be a non-empty string"}, status=400,
            )
        sender = body.get("sender") or "user@monitor"
        if not isinstance(sender, str) or not sender.strip():
            return _json_response({"error": "sender must be a string"}, status=400)
        try:
            msg_id = insert_taskbox_message(
                db_path=relay_db,
                sender=sender,
                recipient="@supervisor",
                content=command,
            )
        except Exception as e:
            log.warning("relay.db insert failed for /supervisor/command: %s", e)
            return _json_response({"error": "insert failed"}, status=500)
        log.info(
            "supervisor command queued: id=%s sender=%s command=%r",
            msg_id, sender, command[:120],
        )
        return _json_response(
            {"task_id": msg_id, "recipient": "@supervisor"}, status=201,
        )

    # CORS preflight (OPTIONS) — browsers send these before POSTs that
    # carry `Content-Type: application/json`. Without an explicit handler
    # aiohttp's router returns 405 with no CORS headers and the browser
    # rejects the actual request. A catch-all `{path:.*}` route + Allow
    # headers covers every endpoint at once.
    async def cors_preflight(_: web.Request) -> web.Response:
        r = web.Response(status=204)
        _cors(r)
        r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type"
        r.headers["Access-Control-Max-Age"] = "600"
        return r

    app = web.Application()
    app[STATE_KEY] = state
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/transcripts", list_index)
    app.router.add_get("/transcripts/{role}", get_role)
    app.router.add_get("/transcripts/{role}/{task_id}", get_task)
    app.router.add_get("/events", ws_events)
    app.router.add_post("/messages/{role}", post_message)
    app.router.add_get("/supervisor/workers", get_workers)
    app.router.add_get("/supervisor/agents", get_agents)
    app.router.add_get("/supervisor/skills", get_skills)
    app.router.add_get("/supervisor/mcp", get_mcp)
    app.router.add_get("/supervisor/board", get_board)
    app.router.add_get("/supervisor/tasks", get_tasks)
    app.router.add_get("/supervisor/jobs", get_jobs)
    app.router.add_get("/supervisor/houses", get_houses)
    app.router.add_post("/supervisor/command", post_command)
    app.router.add_route("OPTIONS", "/{path:.*}", cors_preflight)
    return app


class _ServerState:
    """Holds runtime state (WS clients, root path) shared between routes
    and the broadcaster used by the TranscriptSink."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.clients: set[web.WebSocketResponse] = set()

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Fan out a JSON frame to every connected WS client.

        Safe to call from the TranscriptSink's event handlers. Per-client
        send failures are swallowed; broken connections are removed by the
        WS handler on the next iteration.
        """
        if not self.clients:
            return
        dead: list[web.WebSocketResponse] = []
        for ws in list(self.clients):
            try:
                if ws.closed:
                    dead.append(ws)
                    continue
                await ws.send_json(payload)
            except (ConnectionResetError, RuntimeError):
                dead.append(ws)
            except Exception as e:
                log.debug("transcripts_http: ws send failed: %s", e)
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


class TranscriptsHttpServer:
    """Run-loop wrapper that lives alongside the sinks in __main__."""

    def __init__(
        self,
        root: Path = config.TRANSCRIPTS_ROOT,
        host: str = config.TRANSCRIPTS_HTTP_HOST,
        port: int = config.TRANSCRIPTS_HTTP_PORT,
        relay_db: Path = config.RELAY_DB,
    ) -> None:
        self.root = Path(root)
        self.host = host
        self.port = port
        self.relay_db = Path(relay_db)
        self._runner: web.AppRunner | None = None
        self._state: _ServerState | None = None

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """`on_event` callback wired into TranscriptSink.

        Bound method, so it's safe to capture the reference before start()
        — calls before start (or after stop / failed bind) silently no-op.
        """
        if self._state is None:
            return
        await self._state.broadcast(payload)

    async def start(self) -> None:
        app = build_app(self.root, self.relay_db)
        self._state = app[STATE_KEY]
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await site.start()
            log.info(
                "transcripts http listening on http://%s:%d (root=%s, relay_db=%s)",
                self.host, self.port, self.root, self.relay_db,
            )
        except OSError as e:
            log.warning(
                "transcripts http failed to bind %s:%d (%s) — endpoint disabled",
                self.host, self.port, e,
            )
            await self._runner.cleanup()
            self._runner = None
            self._state = None

    async def run(self) -> None:
        import asyncio
        await asyncio.Event().wait()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._state = None
