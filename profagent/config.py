from __future__ import annotations

import os
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CPA_SCENE_BUDGET_MAX_SECONDS = 8.0
CPA_DIALOGUE_BUDGET_MAX_SECONDS = 120.0
CPA_HEALTH_BUDGET_MAX_SECONDS = 1.5
VISION_INTERACTION_BUDGET_MAX_SECONDS = 10.0
DIALOGUE_TTL_MAX_SECONDS = 1800.0
PREVIEW_2D_BUDGET_MAX_SECONDS = 45.0


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _bounded_positive_float(
    value: str | float | None, *, default: float, maximum: float
) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed <= 0:
        return default
    return min(parsed, maximum)


def _load_cpa_config() -> dict[str, str]:
    configured_path = os.getenv("PROFAGENT_CPA_CONFIG")
    path = (
        Path(configured_path).expanduser()
        if configured_path
        else Path.home() / ".codex" / "skills" / "call-grok" / "config.local.json"
    )
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload: Any = json.load(stream)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    # Only import the two connection fields. In particular, ignore any model
    # value because this project freezes grok4.5 -> grok-4.5-high separately.
    return {
        key: value.strip()
        for key in ("base_url", "api_key")
        if isinstance((value := payload.get(key)), str) and value.strip()
    }


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    data_version: str = "fixtures_v1.0"
    api_version: str = "r1_demo_v1"
    host: str = "127.0.0.1"
    port: int = 8000
    cpa_base_url: str = "http://127.0.0.1:8317/v1"
    cpa_api_key: str | None = field(default=None, repr=False)
    grok_model: str = "grok4.5"
    cpa_text_enabled: bool = True
    cpa_timeout_seconds: float = 120.0
    cpa_scene_budget_seconds: float = CPA_SCENE_BUDGET_MAX_SECONDS
    cpa_dialogue_budget_seconds: float = CPA_DIALOGUE_BUDGET_MAX_SECONDS
    cpa_health_budget_seconds: float = CPA_HEALTH_BUDGET_MAX_SECONDS
    vision_interaction_budget_seconds: float = VISION_INTERACTION_BUDGET_MAX_SECONDS
    dialogue_ttl_seconds: float = DIALOGUE_TTL_MAX_SECONDS
    preview_2d_budget_seconds: float = PREVIEW_2D_BUDGET_MAX_SECONDS
    dense_enabled: bool = False
    dense_force_failure: bool = False
    catalog_enabled: bool = True
    catalog_force_failure: bool = False
    vision_force_failure: bool = False

    @property
    def effective_cpa_scene_budget_seconds(self) -> float:
        # Direct Settings construction is also bounded, not just from_env(), so
        # tests or embedding applications cannot accidentally outrun the UI.
        return _bounded_positive_float(
            self.cpa_scene_budget_seconds,
            default=CPA_SCENE_BUDGET_MAX_SECONDS,
            maximum=CPA_SCENE_BUDGET_MAX_SECONDS,
        )

    @property
    def effective_cpa_dialogue_budget_seconds(self) -> float:
        return _bounded_positive_float(
            self.cpa_dialogue_budget_seconds,
            default=CPA_DIALOGUE_BUDGET_MAX_SECONDS,
            maximum=CPA_DIALOGUE_BUDGET_MAX_SECONDS,
        )

    @property
    def effective_cpa_health_budget_seconds(self) -> float:
        return _bounded_positive_float(
            self.cpa_health_budget_seconds,
            default=CPA_HEALTH_BUDGET_MAX_SECONDS,
            maximum=CPA_HEALTH_BUDGET_MAX_SECONDS,
        )

    @property
    def effective_vision_interaction_budget_seconds(self) -> float:
        return _bounded_positive_float(
            self.vision_interaction_budget_seconds,
            default=VISION_INTERACTION_BUDGET_MAX_SECONDS,
            maximum=VISION_INTERACTION_BUDGET_MAX_SECONDS,
        )

    @property
    def effective_dialogue_ttl_seconds(self) -> float:
        return _bounded_positive_float(
            self.dialogue_ttl_seconds,
            default=DIALOGUE_TTL_MAX_SECONDS,
            maximum=DIALOGUE_TTL_MAX_SECONDS,
        )

    @property
    def effective_preview_2d_budget_seconds(self) -> float:
        return _bounded_positive_float(
            self.preview_2d_budget_seconds,
            default=PREVIEW_2D_BUDGET_MAX_SECONDS,
            maximum=PREVIEW_2D_BUDGET_MAX_SECONDS,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parents[1]
        local_cpa = _load_cpa_config()
        requested_model = os.getenv("PROFAGENT_GROK_MODEL", "grok4.5").strip()
        # The project contract freezes this logical model. Refuse accidental drift.
        if requested_model != "grok4.5":
            requested_model = "grok4.5"
        return cls(
            root_dir=root,
            host=os.getenv("PROFAGENT_HOST", "127.0.0.1"),
            port=int(os.getenv("PROFAGENT_PORT", "8000")),
            cpa_base_url=(
                _first_env(
                    "PROFAGENT_CPA_BASE_URL",
                    "GROK_BASE_URL",
                    "CPA_BASE_URL",
                    "OPENAI_BASE_URL",
                )
                or local_cpa.get("base_url")
                or "http://127.0.0.1:8317/v1"
            ).rstrip("/"),
            cpa_api_key=(
                _first_env(
                    "PROFAGENT_CPA_API_KEY",
                    "GROK_API_KEY",
                    "CPA_API_KEY",
                    "OPENAI_API_KEY",
                )
                or local_cpa.get("api_key")
                or None
            ),
            grok_model=requested_model,
            cpa_text_enabled=_as_bool(
                os.getenv("PROFAGENT_CPA_TEXT_ENABLED"), True
            ),
            cpa_timeout_seconds=float(
                os.getenv("PROFAGENT_CPA_TIMEOUT_SECONDS", "120.0")
            ),
            cpa_scene_budget_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_CPA_SCENE_BUDGET_SECONDS"),
                default=CPA_SCENE_BUDGET_MAX_SECONDS,
                maximum=CPA_SCENE_BUDGET_MAX_SECONDS,
            ),
            cpa_dialogue_budget_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_CPA_DIALOGUE_BUDGET_SECONDS"),
                default=CPA_DIALOGUE_BUDGET_MAX_SECONDS,
                maximum=CPA_DIALOGUE_BUDGET_MAX_SECONDS,
            ),
            cpa_health_budget_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_CPA_HEALTH_BUDGET_SECONDS"),
                default=CPA_HEALTH_BUDGET_MAX_SECONDS,
                maximum=CPA_HEALTH_BUDGET_MAX_SECONDS,
            ),
            vision_interaction_budget_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_VISION_INTERACTION_BUDGET_SECONDS"),
                default=VISION_INTERACTION_BUDGET_MAX_SECONDS,
                maximum=VISION_INTERACTION_BUDGET_MAX_SECONDS,
            ),
            dialogue_ttl_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_DIALOGUE_TTL_SECONDS"),
                default=DIALOGUE_TTL_MAX_SECONDS,
                maximum=DIALOGUE_TTL_MAX_SECONDS,
            ),
            preview_2d_budget_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_PREVIEW_2D_BUDGET_SECONDS"),
                default=PREVIEW_2D_BUDGET_MAX_SECONDS,
                maximum=PREVIEW_2D_BUDGET_MAX_SECONDS,
            ),
            dense_enabled=_as_bool(os.getenv("PROFAGENT_DENSE_ENABLED"), False),
            dense_force_failure=_as_bool(
                os.getenv("PROFAGENT_DENSE_FORCE_FAILURE"), False
            ),
            catalog_enabled=_as_bool(
                os.getenv("PROFAGENT_CATALOG_ENABLED"), True
            ),
            catalog_force_failure=_as_bool(
                os.getenv("PROFAGENT_CATALOG_FORCE_FAILURE"), False
            ),
            vision_force_failure=_as_bool(
                os.getenv("PROFAGENT_VISION_FORCE_FAILURE"), False
            ),
        )
