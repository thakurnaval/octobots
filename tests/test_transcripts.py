"""TranscriptSink — per-task file lifecycle + notify-window activity."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from monitor.bridge.events import (
    MessageClaimedEvent,
    MessageDoneEvent,
    MessageSentEvent,
    NotifyEvent,
)
from monitor.bridge.transcripts import CURRENT_FILE, SESSION_FILE, TranscriptSink


def _sent(role: str, msg_id: str = "m1", sender: str = "pm",
          content: str = "verify PR #42", created_at: float | None = None) -> MessageSentEvent:
    return MessageSentEvent(
        msg_id=msg_id, sender=sender, recipient=role,
        preview=content[:200], created_at=created_at if created_at is not None else time.time(),
        content=content,
    )


def _claim(role: str, msg_id: str = "m1", sender: str = "pm",
           content: str = "verify PR #42") -> MessageClaimedEvent:
    return MessageClaimedEvent(
        msg_id=msg_id, sender=sender, recipient=role,
        preview=content[:200], content=content,
    )


def _done(role: str, msg_id: str = "m1", response: str = "ok") -> MessageDoneEvent:
    return MessageDoneEvent(
        msg_id=msg_id, recipient=role,
        response_preview=response[:200], response=response,
    )


def _notify(role: str, ts: float, channel: str = "telegram",
            preview: str = "user, please review") -> NotifyEvent:
    return NotifyEvent(sender=role, channel=channel, preview=preview, ts=ts)


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _archives(role_dir: Path) -> list[Path]:
    return sorted(
        p for p in role_dir.iterdir()
        if p.is_file()
        and p.name not in (CURRENT_FILE, SESSION_FILE)
        and p.suffix == ".json"
    )


# -------------------------- core lifecycle --------------------------

async def test_claim_writes_current_with_full_prompt(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa", content="full prompt body, longer than preview"))

    current = tmp_path / "qa" / CURRENT_FILE
    rec = _read(current)
    assert rec["task_id"] == "m1"
    assert rec["role"] == "qa"
    assert rec["sender"] == "pm"
    assert rec["prompt"] == "full prompt body, longer than preview"
    assert rec["status"] == "processing"
    assert rec["ended_at"] is None
    assert rec["response"] is None
    assert rec["activity"] == []


async def test_done_archives_and_removes_current(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa"))
    assert (tmp_path / "qa" / CURRENT_FILE).exists()

    await sink.handle(_done("qa", response="full response body"))

    assert not (tmp_path / "qa" / CURRENT_FILE).exists()
    archives = _archives(tmp_path / "qa")
    assert len(archives) == 1
    rec = _read(archives[0])
    assert rec["status"] == "done"
    assert rec["response"] == "full response body"
    assert rec["ended_at"] is not None
    assert rec["ended_at"] >= rec["started_at"]
    # Filename: <iso_compact>-<task_id>.json
    assert archives[0].name.endswith("-m1.json")


async def test_done_falls_back_to_preview_if_no_full_content(tmp_path: Path):
    """Bridge running against an older taskbox that didn't populate full content
    should still produce a usable record from the preview fields."""
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(MessageClaimedEvent(
        msg_id="m1", sender="pm", recipient="qa", preview="short prompt",
        content=None,
    ))
    await sink.handle(MessageDoneEvent(
        msg_id="m1", recipient="qa", response_preview="short reply", response=None,
    ))
    archives = _archives(tmp_path / "qa")
    rec = _read(archives[0])
    assert rec["prompt"] == "short prompt"
    assert rec["response"] == "short reply"


# -------------------------- notify activity --------------------------

async def test_notify_inside_window_appended_to_activity(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa"))
    # Simulate notify a fraction of a second after claim — clearly inside window.
    ts = time.time() + 0.01
    await sink.handle(_notify("qa", ts=ts, preview="ping the user"))

    rec = _read(tmp_path / "qa" / CURRENT_FILE)
    assert len(rec["activity"]) == 1
    act = rec["activity"][0]
    assert act["kind"] == "notify"
    assert act["channel"] == "telegram"
    assert act["preview"] == "ping the user"
    assert act["ts"] == ts


async def test_notify_before_task_start_is_dropped(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa"))
    rec = _read(tmp_path / "qa" / CURRENT_FILE)
    started_at = rec["started_at"]
    # Notify dated before the claim
    await sink.handle(_notify("qa", ts=started_at - 5.0))
    rec = _read(tmp_path / "qa" / CURRENT_FILE)
    assert rec["activity"] == []


async def test_notify_for_role_with_no_current_task_is_dropped(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_notify("qa", ts=time.time()))
    # No file should have been written at all.
    assert not (tmp_path / "qa").exists() or not any((tmp_path / "qa").iterdir())


async def test_notify_persists_into_archived_file_on_done(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa"))
    await sink.handle(_notify("qa", ts=time.time() + 0.01, preview="one"))
    await sink.handle(_notify("qa", ts=time.time() + 0.02, preview="two"))
    await sink.handle(_done("qa"))

    archives = _archives(tmp_path / "qa")
    rec = _read(archives[0])
    previews = [a["preview"] for a in rec["activity"]]
    assert previews == ["one", "two"]


# -------------------------- edge cases --------------------------

async def test_done_without_prior_claim_is_silent(tmp_path: Path):
    """Bridge starts mid-task: only sees the done transition, not the claim."""
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_done("qa"))
    # No file written, no exception raised.
    assert not (tmp_path / "qa" / CURRENT_FILE).exists()
    assert not (tmp_path / "qa").exists() or not _archives(tmp_path / "qa")


async def test_overlapping_claims_abandon_previous(tmp_path: Path):
    """Supervisor guarantees one-at-a-time, but be defensive: if a new claim
    arrives while one is in flight, the previous is archived as 'abandoned'
    so the transcript isn't silently lost."""
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa", msg_id="m1", content="first"))
    await sink.handle(_claim("qa", msg_id="m2", content="second"))

    archives = _archives(tmp_path / "qa")
    assert len(archives) == 1
    rec = _read(archives[0])
    assert rec["task_id"] == "m1"
    assert rec["status"] == "abandoned"

    # Live file is now the second task.
    current = _read(tmp_path / "qa" / CURRENT_FILE)
    assert current["task_id"] == "m2"
    assert current["prompt"] == "second"


async def test_retention_prunes_to_n_newest(tmp_path: Path):
    """After more than `retention` completed tasks, only the newest N remain."""
    sink = TranscriptSink(root=tmp_path, retention=3)
    await sink.start()

    for i in range(5):
        msg_id = f"m{i}"
        await sink.handle(_claim("qa", msg_id=msg_id, content=f"task {i}"))
        await sink.handle(_done("qa", msg_id=msg_id, response=f"reply {i}"))
        # Ensure strictly monotonic filenames (iso_compact has 1-second resolution).
        # Sleep avoids two tasks getting the same prefix and colliding.
        time.sleep(1.05)

    archives = _archives(tmp_path / "qa")
    assert len(archives) == 3
    # Newest 3 are m2..m4 (ordered by ISO prefix).
    task_ids = sorted(_read(p)["task_id"] for p in archives)
    assert task_ids == ["m2", "m3", "m4"]


async def test_other_roles_are_isolated(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa", msg_id="q1"))
    await sink.handle(_claim("pm", msg_id="p1"))

    qa_current = _read(tmp_path / "qa" / CURRENT_FILE)
    pm_current = _read(tmp_path / "pm" / CURRENT_FILE)
    assert qa_current["task_id"] == "q1"
    assert pm_current["task_id"] == "p1"

    # Notify for qa doesn't bleed into pm.
    await sink.handle(_notify("qa", ts=time.time() + 0.01, preview="for qa"))
    pm_after = _read(tmp_path / "pm" / CURRENT_FILE)
    assert pm_after["activity"] == []


async def test_atomic_write_leaves_no_tmp(tmp_path: Path):
    """No `.tmp` files should remain after handle() returns."""
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa"))
    await sink.handle(_done("qa"))
    stray = list((tmp_path / "qa").glob("*.tmp"))
    assert stray == []


# -------------------------- session.json --------------------------

async def test_session_json_created_on_first_claim(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path, session_id="20260513T153000Z")
    await sink.start()
    # No session.json before any activity
    assert not (tmp_path / "qa" / SESSION_FILE).exists()
    await sink.handle(_claim("qa"))

    sess = _read(tmp_path / "qa" / SESSION_FILE)
    assert sess["session_id"] == "20260513T153000Z"
    assert sess["role"] == "qa"
    assert sess["current_task_id"] == "m1"
    assert len(sess["tasks"]) == 1
    t0 = sess["tasks"][0]
    assert t0["task_id"] == "m1"
    assert t0["sender"] == "pm"
    assert t0["status"] == "processing"
    assert t0["ended_at"] is None
    assert t0["notify_count"] == 0
    assert t0["prompt_preview"] != ""


async def test_session_json_updates_on_done(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa"))
    await sink.handle(_done("qa", response="all green"))

    sess = _read(tmp_path / "qa" / SESSION_FILE)
    assert sess["current_task_id"] is None  # role is idle again
    t0 = sess["tasks"][0]
    assert t0["status"] == "done"
    assert t0["ended_at"] is not None
    assert "all green" in t0["response_preview"]


async def test_session_json_notify_count_increments(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa"))
    now = time.time() + 0.01
    await sink.handle(_notify("qa", ts=now))
    await sink.handle(_notify("qa", ts=now + 0.02))

    sess = _read(tmp_path / "qa" / SESSION_FILE)
    assert sess["tasks"][0]["notify_count"] == 2


async def test_session_json_orders_newest_first_and_caps_at_retention(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path, retention=3)
    await sink.start()
    for i in range(5):
        msg_id = f"m{i}"
        await sink.handle(_claim("qa", msg_id=msg_id, content=f"task {i}"))
        await sink.handle(_done("qa", msg_id=msg_id, response=f"reply {i}"))
        # No sleep needed — session index is in-memory, isn't ISO-named.

    sess = _read(tmp_path / "qa" / SESSION_FILE)
    assert len(sess["tasks"]) == 3
    ids = [t["task_id"] for t in sess["tasks"]]
    # Newest first
    assert ids == ["m4", "m3", "m2"]


async def test_session_id_shared_across_roles_same_sink(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path, session_id="abc-session")
    await sink.start()
    await sink.handle(_claim("qa", msg_id="q1"))
    await sink.handle(_claim("pm", msg_id="p1"))

    qa_sess = _read(tmp_path / "qa" / SESSION_FILE)
    pm_sess = _read(tmp_path / "pm" / SESSION_FILE)
    assert qa_sess["session_id"] == pm_sess["session_id"] == "abc-session"


async def test_session_id_defaults_to_iso_compact_when_not_provided(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa"))
    sess = _read(tmp_path / "qa" / SESSION_FILE)
    # Format: YYYYMMDDTHHMMSSZ
    assert len(sess["session_id"]) == 16
    assert sess["session_id"].endswith("Z")
    assert "T" in sess["session_id"]


async def test_session_json_marks_abandoned_on_overlapping_claim(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa", msg_id="m1"))
    await sink.handle(_claim("qa", msg_id="m2"))

    sess = _read(tmp_path / "qa" / SESSION_FILE)
    by_id = {t["task_id"]: t for t in sess["tasks"]}
    assert by_id["m1"]["status"] == "abandoned"
    assert by_id["m1"]["ended_at"] is not None
    assert by_id["m2"]["status"] == "processing"
    assert sess["current_task_id"] == "m2"


async def test_pending_message_adds_session_entry_no_current_change(tmp_path: Path):
    """A MessageSent without a prior claim is queued — appears in
    session.json with status=pending, current_task_id stays null."""
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_sent("qa", msg_id="p1", content="user typed in UI"))

    sess = _read(tmp_path / "qa" / SESSION_FILE)
    assert sess["current_task_id"] is None
    assert len(sess["tasks"]) == 1
    t = sess["tasks"][0]
    assert t["task_id"] == "p1"
    assert t["status"] == "pending"
    assert t["ended_at"] is None
    assert "user typed in UI" in t["prompt_preview"]


async def test_pending_then_claim_then_done_transitions_in_session(tmp_path: Path):
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_sent("qa", msg_id="m1"))
    await sink.handle(_claim("qa", msg_id="m1"))
    await sink.handle(_done("qa", msg_id="m1"))

    sess = _read(tmp_path / "qa" / SESSION_FILE)
    statuses = [t["status"] for t in sess["tasks"]]
    assert statuses == ["done"]
    assert sess["current_task_id"] is None


async def test_pending_fires_task_pending_event(tmp_path: Path):
    events: list[dict] = []
    async def on_event(payload: dict) -> None:
        events.append(payload)

    sink = TranscriptSink(root=tmp_path, on_event=on_event)
    await sink.start()
    await sink.handle(_sent("qa", msg_id="p1"))
    assert len(events) == 1
    assert events[0]["type"] == "task_pending"
    assert events[0]["role"] == "qa"
    assert events[0]["task"]["task_id"] == "p1"
    assert events[0]["task"]["status"] == "pending"


async def test_duplicate_sent_event_is_no_op(tmp_path: Path):
    """Out-of-order delivery: a claim arrived before its sent. The later
    sent should not downgrade status back to pending."""
    sink = TranscriptSink(root=tmp_path)
    await sink.start()
    await sink.handle(_claim("qa", msg_id="m1"))
    await sink.handle(_sent("qa", msg_id="m1"))  # arrived late

    sess = _read(tmp_path / "qa" / SESSION_FILE)
    assert len(sess["tasks"]) == 1
    assert sess["tasks"][0]["status"] == "processing"  # not downgraded


async def test_session_json_not_pruned_by_archive_pruning(tmp_path: Path):
    """session.json + current.json shouldn't be deleted when archive prune fires."""
    sink = TranscriptSink(root=tmp_path, retention=1)
    await sink.start()
    await sink.handle(_claim("qa", msg_id="m1"))
    await sink.handle(_done("qa", msg_id="m1"))
    time.sleep(1.05)
    await sink.handle(_claim("qa", msg_id="m2"))
    await sink.handle(_done("qa", msg_id="m2"))
    # session.json survives even though only 1 archive should remain
    assert (tmp_path / "qa" / SESSION_FILE).exists()
    archives = _archives(tmp_path / "qa")
    assert len(archives) == 1
