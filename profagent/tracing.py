from __future__ import annotations

import hashlib
import re
from datetime import datetime
from threading import RLock
from typing import Any

from .models import API_VERSION, TraceRecord


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "base64",
    "image",
    "raw_query",
    "query_text",
    "conversation",
    "messages",
}

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-[a-z0-9_-]{4,}\b"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"
    ),
)
_BASE64_BLOB = re.compile(r"(?i)(?:data:[^,]+;base64,|\b[a-z0-9+/]{128,}={0,2}\b)")


def utc_now() -> datetime:
    return datetime.now().astimezone()


def query_metadata(query: str) -> dict[str, Any]:
    return {
        "sha256_prefix": hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
        "character_count": len(query),
    }


def sanitize(value: Any, key: str = "") -> Any:
    normalized_key = key.lower().replace("-", "_")
    if normalized_key in _SENSITIVE_KEYS or any(
        marker in normalized_key for marker in ("secret", "token", "password")
    ):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item, key) for item in value]
    if isinstance(value, str):
        if len(value) > 512:
            return "[REDACTED_LONG_TEXT]"
        cleaned = value
        for pattern in _SECRET_PATTERNS:
            cleaned = pattern.sub("[REDACTED_SECRET]", cleaned)
        cleaned = _BASE64_BLOB.sub("[REDACTED_BLOB]", cleaned)
        return cleaned
    return value


class TraceStore:
    def __init__(self):
        self._records: dict[str, TraceRecord] = {}
        # Ownership is deliberately kept out of the public TraceRecord payload.
        # API authorization can therefore be enforced without exposing or
        # trusting a client-supplied owner field in trace JSON.
        self._owners: dict[str, str] = {}
        self._lock = RLock()

    def start(
        self,
        *,
        trace_id: str,
        request_id: str,
        styling_session_id: str,
        query_text: str,
        user_id: str | None = None,
    ) -> TraceRecord:
        now = utc_now()
        record = TraceRecord(
            api_version=API_VERSION,
            trace_id=trace_id,
            request_id=request_id,
            styling_session_id=styling_session_id,
            created_at=now,
            updated_at=now,
            versions={
                "data": "fixtures_v1.0",
                "eval": "eval_fixtures_v1.0_30",
                "prompt": "scene_parse_prompt_v1",
                "rules": "scene_rules_v1",
                "ranker": "rule_bm25_rrf_v1",
                "provider_contract": "cpa_grok4.5_v1",
                "model": "requested:grok4.5|transport:grok-4.5-high",
            },
            query=query_metadata(query_text),
            catalog={"attempted": False, "call_count": 0, "blocked_reason": None},
        )
        with self._lock:
            self._records[trace_id] = record
            if user_id is not None:
                self._owners[trace_id] = user_id
        return record.model_copy(deep=True)

    def update(self, trace_id: str, **sections: Any) -> TraceRecord:
        with self._lock:
            current = self._records[trace_id]
            payload = current.model_dump()
            for key, value in sections.items():
                payload[key] = sanitize(value, key)
            payload["updated_at"] = utc_now()
            updated = TraceRecord.model_validate(payload)
            self._records[trace_id] = updated
            return updated.model_copy(deep=True)

    def append_fallback(self, trace_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            current = self._records[trace_id]
            events = list(current.fallback_events)
            events.append(sanitize(event))
            payload = current.model_dump()
            payload["fallback_events"] = events
            payload["updated_at"] = utc_now()
            self._records[trace_id] = TraceRecord.model_validate(payload)

    def get(self, trace_id: str) -> TraceRecord | None:
        with self._lock:
            item = self._records.get(trace_id)
            return item.model_copy(deep=True) if item else None

    def discard(self, trace_id: str) -> None:
        """Remove an incomplete operation after its owning task is cancelled."""
        with self._lock:
            self._records.pop(trace_id, None)
            self._owners.pop(trace_id, None)

    def belongs_to(self, trace_id: str, user_id: str) -> bool:
        """Return False for both missing and cross-owner traces.

        Keeping this check in the store avoids accidental ID-existence leaks at
        HTTP boundaries, including memory traces that do not have a live
        styling session.
        """
        with self._lock:
            return trace_id in self._records and self._owners.get(trace_id) == user_id

    def list_for_session(self, styling_session_id: str) -> list[TraceRecord]:
        with self._lock:
            items = [
                record.model_copy(deep=True)
                for record in self._records.values()
                if record.styling_session_id == styling_session_id
            ]
        return sorted(items, key=lambda item: item.created_at)
