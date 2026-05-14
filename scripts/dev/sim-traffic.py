#!/usr/bin/env python3
"""Drive synthetic taskbox traffic for monitor-ui development + demos.

Inserts pending rows into a relay.db at the path the bridge is watching,
cycles them pending → processing → done with realistic delays, and
optionally writes notify.log entries during a task's processing window.

Intended use: spin up the bridge + monitor-ui without a real supervisor,
then run this against the same `.octobots/relay.db` to see the UI animate.

Example:

    # Terminal 1: bridge (the supervisor invokes this; here we run it raw)
    cd supervisor
    OCTOBOTS_PROJECT_ROOT=/tmp/octobots-demo python3 -m monitor.bridge

    # Terminal 2: monitor-ui dev server
    cd monitor-ui && npm run dev

    # Terminal 3: drive synthetic traffic
    python3 supervisor/scripts/dev/sim-traffic.py /tmp/octobots-demo

The script creates the dir + .octobots/relay.db + .agents/transcripts/
if they don't exist. Ctrl-C stops it; nothing else is persistent beyond
the demo dir.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROLES = ["pm", "tech-lead", "ba", "qa", "python-dev", "js-dev"]

PROMPTS = [
    ("pm", "qa", "Verify the regression fix on PR #103 against staging."),
    ("tech-lead", "python-dev", "Refactor the auth middleware to remove the legacy session-token storage."),
    ("ba", "tech-lead", "Stakeholder asked: can we ship the analytics dashboard before EOM?"),
    ("pm", "ba", "Translate the customer feedback from the call into user stories."),
    ("qa", "pm", "Three flaky tests in the nightly run — escalating."),
    ("tech-lead", "js-dev", "Bump React to 19 and audit the deprecated lifecycle calls."),
    ("pm", "tech-lead", "Estimate effort for the OAuth migration epic."),
]

RESPONSES = [
    "Tests pass on staging — green light.",
    "Done. Pushed branch + opened PR.",
    "Investigated, root cause was X; mitigated.",
    "Draft stories created in board.md; awaiting review.",
    "Escalated to the on-call. Will follow up.",
]

# Replies for externally-injected messages (e.g. user typed into the UI).
EXTERNAL_RESPONSES = [
    "Got it — on it.",
    "Acknowledged. Will report back shortly.",
    "Looking into it now.",
    "Noted. Picking this up.",
    "Investigating — will follow up.",
]


def _init_relay_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                sender      TEXT NOT NULL,
                recipient   TEXT NOT NULL,
                content     TEXT NOT NULL,
                response    TEXT DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _msg_id() -> str:
    return uuid.uuid4().hex[:12]


def _send(db: Path, sender: str, recipient: str, content: str) -> str:
    msg_id = _msg_id()
    now = time.time()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO messages (id, sender, recipient, content, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (msg_id, sender, recipient, content, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return msg_id


def _claim(db: Path, msg_id: str) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE messages SET status='processing', updated_at=? WHERE id=?",
            (time.time(), msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def _done(db: Path, msg_id: str, response: str) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE messages SET status='done', response=?, updated_at=? WHERE id=?",
            (response, time.time(), msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def _existing_message_ids(db: Path) -> set[str]:
    """All ids currently in the messages table — used at startup to skip
    rows that existed before the sim started, so we don't re-claim
    historical data."""
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT id FROM messages")}
    finally:
        conn.close()


def _pending_rows(db: Path) -> list[tuple[str, str, str, str]]:
    """Read all currently-pending rows. Returns (id, sender, recipient, content)."""
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT id, sender, recipient, content FROM messages WHERE status='pending'"
        ).fetchall()
    finally:
        conn.close()


def _drain_externals(
    db: Path,
    processed: set[str],
    duration_range: tuple[float, float] = (1.0, 2.5),
) -> int:
    """Claim + ack any pending rows we haven't processed yet (e.g. typed
    by the user into the monitor UI). Returns the count drained.

    Pretends to be a worker: short delay before claim, scripted reply on done.
    """
    drained = 0
    for msg_id, sender, recipient, content in _pending_rows(db):
        if msg_id in processed:
            continue
        print(f"[sim] external {msg_id[:6]} {sender:>10s} → {recipient:<10s}: {content[:60]}")
        time.sleep(0.4)
        _claim(db, msg_id)
        time.sleep(random.uniform(*duration_range))
        _done(db, msg_id, random.choice(EXTERNAL_RESPONSES))
        processed.add(msg_id)
        drained += 1
    return drained


def _write_notify_line(notify_log: Path, role: str, preview: str) -> None:
    notify_log.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "from": role,
        "channel": "telegram",
        "method": "sendMessage",
        "preview": preview,
    }
    with notify_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _write_pane_map(pane_map: Path) -> None:
    """Pretend tmux panes exist so the bridge's tmux_panes poller registers
    the roles as agents in WorldState.

    The targets are fake; tmux capture-pane against them fails silently
    and the bridge keeps each role in 'idle' state forever. That's fine
    for a monitor-ui demo — we drive activity via the taskbox path.
    """
    pane_map.parent.mkdir(parents=True, exist_ok=True)
    pane_map.write_text("\n".join(f"{r}=demo:fake.{i}" for i, r in enumerate(ROLES)) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_dir", type=Path,
                    help="Directory the bridge polls (sets OCTOBOTS_PROJECT_ROOT).")
    ap.add_argument("--interval", type=float, default=4.0,
                    help="Average seconds between task arrivals (default 4).")
    ap.add_argument("--task-duration", type=float, default=6.0,
                    help="Average seconds a task spends in 'processing' (default 6).")
    args = ap.parse_args()

    root: Path = args.project_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    relay = root / ".octobots" / "relay.db"
    notify_log = root / ".octobots" / "notify.log"
    pane_map = root / ".octobots" / ".pane-map"
    _init_relay_db(relay)
    _write_pane_map(pane_map)

    print(f"[sim] driving traffic into {root}")
    print(f"[sim] relay.db at {relay}")
    print(f"[sim] start the bridge with: OCTOBOTS_PROJECT_ROOT={root} python3 -m monitor.bridge")
    print(f"[sim] open the UI at: http://127.0.0.1:5173/")
    print("[sim] Ctrl-C to stop.\n")

    # Skip historical pending rows on startup — we only auto-respond to
    # things that arrive after we're running.
    processed: set[str] = _existing_message_ids(relay)

    try:
        while True:
            # 1. Drain any externally-sent pending messages first (e.g. user
            # typed into the monitor UI). Gives the demo a "the bot replies
            # when you send something" feel.
            _drain_externals(relay, processed)

            # 2. Scripted task.
            sender, recipient, prompt = random.choice(PROMPTS)
            msg_id = _send(relay, sender, recipient, prompt)
            processed.add(msg_id)
            print(f"[sim] sent {msg_id[:6]} {sender:>10s} → {recipient:<10s}: {prompt[:60]}")
            time.sleep(0.6)  # let the bridge observe the pending row first
            _claim(relay, msg_id)
            duration = max(0.5, random.gauss(args.task_duration, 1.5))
            if random.random() < 0.4:
                time.sleep(duration / 2)
                _write_notify_line(notify_log, recipient,
                                   f"pinging user about {msg_id[:6]}")
                time.sleep(duration / 2)
            else:
                time.sleep(duration)
            _done(relay, msg_id, random.choice(RESPONSES))
            print(f"[sim] done {msg_id[:6]}")
            time.sleep(max(0.5, random.gauss(args.interval, 1.5)))
    except KeyboardInterrupt:
        print("\n[sim] stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
