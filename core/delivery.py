from __future__ import annotations

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
