from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Any


PUBLISH_NOT_PUBLISHED = "not_published"
PUBLISH_QUEUED = "queued"
PUBLISH_PUBLISHED = "published"
PUBLISH_FAILED_RETRYABLE = "failed_retryable"
PUBLISH_UNKNOWN_MANUAL_RECOVERY = "unknown_manual_recovery"

PUBLISH_STATUSES = frozenset(
    {
        PUBLISH_NOT_PUBLISHED,
        PUBLISH_QUEUED,
        PUBLISH_PUBLISHED,
        PUBLISH_FAILED_RETRYABLE,
        PUBLISH_UNKNOWN_MANUAL_RECOVERY,
    }
)

RUN_PUBLISHED = "published"
RUN_FAILED = "failed"
RUN_SKIPPED = "skipped"
RUN_UNKNOWN = "unknown"
RUN_STATUSES = frozenset({RUN_PUBLISHED, RUN_FAILED, RUN_SKIPPED, RUN_UNKNOWN})

OUTCOME_PUBLISHED = "published"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"


def _nested_success(result: dict[str, Any]) -> bool | None:
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("success"), bool):
        return bool(structured["success"])

    if isinstance(result.get("success"), bool):
        return bool(result["success"])

    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            payload = json.loads(item["text"])
        except ValueError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("success"), bool):
            return bool(payload["success"])
    return None


def classify_publish_outcome(result: dict[str, Any]) -> str:
    """Classify a publish response without treating an indeterminate result as failure."""

    outcome = str(result.get("outcome") or "").strip().lower()
    if outcome == OUTCOME_PUBLISHED:
        return PUBLISH_PUBLISHED
    if outcome == OUTCOME_FAILED:
        return PUBLISH_FAILED_RETRYABLE
    if outcome == OUTCOME_UNKNOWN:
        return PUBLISH_UNKNOWN_MANUAL_RECOVERY

    if result.get("isError") is True:
        return PUBLISH_FAILED_RETRYABLE
    nested_success = _nested_success(result)
    if nested_success is True:
        return PUBLISH_PUBLISHED
    if nested_success is False:
        return PUBLISH_FAILED_RETRYABLE

    # Fail closed: an unrecognized response must not be retried automatically.
    return PUBLISH_UNKNOWN_MANUAL_RECOVERY


def content_fingerprint(account_key: str, content: str) -> str:
    normalized_content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    payload = f"{account_key.strip()}\0{normalized_content}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def next_scheduled_time(
    *,
    latest_at: str | None,
    min_interval_minutes: int,
    jitter_minutes: int,
    now: datetime | None = None,
) -> str:
    local_now = now or datetime.now().astimezone()
    if local_now.tzinfo is None:
        local_now = local_now.astimezone()
    latest = parse_iso_datetime(latest_at)
    if latest is not None:
        latest = latest.astimezone(local_now.tzinfo)
        base = max(local_now, latest + timedelta(minutes=max(0, min_interval_minutes)))
    else:
        base = local_now
    return (base + timedelta(minutes=max(0, jitter_minutes))).isoformat()
