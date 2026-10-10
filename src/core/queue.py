"""Dashboard queue ids, snapshots, and drops."""

from __future__ import annotations

import logging

from src.core.event_flow import EVENT_PRIORITY
from src.twitch.chat import ChatLine
from src.twitch.eventsub import StreamEvent

log = logging.getLogger("cohost.orch")


def _next_qid(orch) -> str:
    orch._queue_seq += 1
    return f"q{orch._queue_seq}"


def _assign_queue_ids(orch) -> None:
    for line in orch._chat_pending:
        if not line.id:
            line.id = orch._next_qid()
    for line in getattr(orch, "_questions", ()):
        if not line.id:
            line.id = orch._next_qid()
    for event in orch._event_pending:
        if not event.id:
            event.id = orch._next_qid()


def _retain_questions(orch, kept: list[ChatLine]) -> None:
    orch._questions.clear()
    orch._questions.extend(kept)


def _retain_chat(orch, kept: list[ChatLine]) -> None:
    orch._chat_pending.clear()
    orch._chat_pending.extend(kept)


def _retain_events(orch, kept: list[StreamEvent]) -> None:
    orch._event_pending.clear()
    orch._event_pending.extend(kept)


def queue_snapshot(orch) -> dict[str, list]:
    orch._assign_queue_ids()
    alerts = sorted(orch._event_pending, key=lambda e: EVENT_PRIORITY.get(e.kind, 9))
    chat = [
        {"id": line.id, "user": line.user, "text": line.text[:120]} for line in list(orch._chat_pending)[:12]
    ]
    questions = [
        {"id": line.id, "user": line.user, "text": line.text[:120]}
        for line in list(getattr(orch, "_questions", ()))[:12]
    ]
    return {
        "chat": chat,
        "alerts": [{"id": event.id, "kind": event.kind, "text": event.scene()} for event in alerts],
        "questions": questions,
    }


async def drop_queued(orch, item_id: str) -> bool:
    orch._assign_queue_ids()
    item_id = (item_id or "").strip()
    if not item_id:
        return False
    for line in list(orch._chat_pending):
        if line.id == item_id:
            orch._retain_chat([item for item in orch._chat_pending if item is not line])
            await orch._publish_queue()
            return True
    for event in list(orch._event_pending):
        if event.id == item_id:
            orch._retain_events([item for item in orch._event_pending if item is not event])
            await orch._publish_queue()
            return True
    for line in list(getattr(orch, "_questions", ())):
        if line.id == item_id:
            orch._retain_questions([item for item in orch._questions if item is not line])
            await orch._publish_queue()
            return True
    return False


async def answer_queued(orch, item_id: str) -> bool:
    orch._assign_queue_ids()
    item_id = (item_id or "").strip()
    if not item_id:
        return False
    for line in list(orch._chat_pending):
        if line.id == item_id:
            orch._retain_chat([item for item in orch._chat_pending if item is not line])
            await orch._publish_queue()
            await orch._perform_chat(line, ignore_rate=True)
            return True
    for event in list(orch._event_pending):
        if event.id == item_id:
            orch._retain_events([item for item in orch._event_pending if item is not event])
            await orch._publish_queue()
            await orch._react_event(event)
            return True
    for line in list(getattr(orch, "_questions", ())):
        if line.id == item_id:
            orch._retain_questions([item for item in orch._questions if item is not line])
            await orch._publish_queue()
            await orch._perform_chat(line, ignore_rate=True)
            return True
    return False


async def _publish_queue(orch) -> None:
    snap = orch.queue_snapshot()
    await orch.hub.patch_status(queue=snap)
    current = (snap.get("questions") or [{}])[0] if snap.get("questions") else {}
    await orch.hub.broadcast(
        {"type": "question", "data": {"user": current.get("user") or "", "text": current.get("text") or ""}}
    )
