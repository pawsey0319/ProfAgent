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
CPA_IMAGE_TIMEOUT_MAX_SECONDS = 45.0
CPA_IMAGE_MODEL = "grok-imagine-image-quality"
LICENSED_ASSET_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
LICENSED_ASSET_MAX_PIXELS = 20_000_000
LICENSED_ASSET_MIN_DIMENSION = 1
LICENSED_ASSET_MAX_DIMENSION = 4096
LICENSED_ASSET_MAX_REDIRECTS = 3
LICENSED_ASSET_DNS_TIMEOUT_SECONDS = 3.0
LICENSED_ASSET_CONNECT_TIMEOUT_SECONDS = 5.0
LICENSED_ASSET_READ_TIMEOUT_SECONDS = 10.0
LICENSED_ASSET_WRITE_TIMEOUT_SECONDS = 5.0
LICENSED_ASSET_POOL_TIMEOUT_SECONDS = 2.0
LICENSED_ASSET_TOTAL_TIMEOUT_SECONDS = 20.0


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


def _bounded_int(
    value: str | int | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
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
    # value because this project freezes grok4.6 -> grok-4.6-high separately.
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
    grok_model: str = "grok4.6"
    cpa_text_enabled: bool = True
    cpa_image_enabled: bool = False
    cpa_image_model: str = CPA_IMAGE_MODEL
    cpa_timeout_seconds: float = 120.0
    cpa_image_timeout_seconds: float = CPA_IMAGE_TIMEOUT_MAX_SECONDS
    cpa_image_download_hosts: tuple[str, ...] = ()
    cpa_scene_budget_seconds: float = CPA_SCENE_BUDGET_MAX_SECONDS
    cpa_dialogue_budget_seconds: float = CPA_DIALOGUE_BUDGET_MAX_SECONDS
    cpa_health_budget_seconds: float = CPA_HEALTH_BUDGET_MAX_SECONDS
    vision_interaction_budget_seconds: float = VISION_INTERACTION_BUDGET_MAX_SECONDS
    dialogue_ttl_seconds: float = DIALOGUE_TTL_MAX_SECONDS
    preview_2d_budget_seconds: float = PREVIEW_2D_BUDGET_MAX_SECONDS
    licensed_asset_max_response_bytes: int = LICENSED_ASSET_MAX_RESPONSE_BYTES
    licensed_asset_max_pixels: int = LICENSED_ASSET_MAX_PIXELS
    licensed_asset_min_dimension: int = LICENSED_ASSET_MIN_DIMENSION
    licensed_asset_max_dimension: int = LICENSED_ASSET_MAX_DIMENSION
    licensed_asset_max_redirects: int = LICENSED_ASSET_MAX_REDIRECTS
    licensed_asset_dns_timeout_seconds: float = LICENSED_ASSET_DNS_TIMEOUT_SECONDS
    licensed_asset_connect_timeout_seconds: float = (
        LICENSED_ASSET_CONNECT_TIMEOUT_SECONDS
    )
    licensed_asset_read_timeout_seconds: float = LICENSED_ASSET_READ_TIMEOUT_SECONDS
    licensed_asset_write_timeout_seconds: float = LICENSED_ASSET_WRITE_TIMEOUT_SECONDS
    licensed_asset_pool_timeout_seconds: float = LICENSED_ASSET_POOL_TIMEOUT_SECONDS
    licensed_asset_total_timeout_seconds: float = LICENSED_ASSET_TOTAL_TIMEOUT_SECONDS
    database_url: str | None = field(default=None, repr=False)
    wardrobe_catalog_manifest: Path | None = None
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

    @property
    def effective_cpa_image_timeout_seconds(self) -> float:
        return _bounded_positive_float(
            self.cpa_image_timeout_seconds,
            default=CPA_IMAGE_TIMEOUT_MAX_SECONDS,
            maximum=CPA_IMAGE_TIMEOUT_MAX_SECONDS,
        )

    @property
    def effective_licensed_asset_max_response_bytes(self) -> int:
        return _bounded_int(
            self.licensed_asset_max_response_bytes,
            default=LICENSED_ASSET_MAX_RESPONSE_BYTES,
            minimum=1,
            maximum=25 * 1024 * 1024,
        )

    @property
    def effective_licensed_asset_max_pixels(self) -> int:
        return _bounded_int(
            self.licensed_asset_max_pixels,
            default=LICENSED_ASSET_MAX_PIXELS,
            minimum=1,
            maximum=40_000_000,
        )

    @property
    def effective_licensed_asset_min_dimension(self) -> int:
        minimum = _bounded_int(
            self.licensed_asset_min_dimension,
            default=LICENSED_ASSET_MIN_DIMENSION,
            minimum=1,
            maximum=8192,
        )
        return min(minimum, self.effective_licensed_asset_max_dimension)

    @property
    def effective_licensed_asset_max_dimension(self) -> int:
        return _bounded_int(
            self.licensed_asset_max_dimension,
            default=LICENSED_ASSET_MAX_DIMENSION,
            minimum=1,
            maximum=8192,
        )

    @property
    def effective_licensed_asset_max_redirects(self) -> int:
        return _bounded_int(
            self.licensed_asset_max_redirects,
            default=LICENSED_ASSET_MAX_REDIRECTS,
            minimum=0,
            maximum=5,
        )

    @property
    def effective_licensed_asset_dns_timeout_seconds(self) -> float:
        return _bounded_positive_float(
            self.licensed_asset_dns_timeout_seconds,
            default=LICENSED_ASSET_DNS_TIMEOUT_SECONDS,
            maximum=30.0,
        )

    @property
    def effective_licensed_asset_connect_timeout_seconds(self) -> float:
        return _bounded_positive_float(
            self.licensed_asset_connect_timeout_seconds,
            default=LICENSED_ASSET_CONNECT_TIMEOUT_SECONDS,
            maximum=30.0,
        )

    @property
    def effective_licensed_asset_read_timeout_seconds(self) -> float:
        return _bounded_positive_float(
            self.licensed_asset_read_timeout_seconds,
            default=LICENSED_ASSET_READ_TIMEOUT_SECONDS,
            maximum=30.0,
        )

    @property
    def effective_licensed_asset_write_timeout_seconds(self) -> float:
        return _bounded_positive_float(
            self.licensed_asset_write_timeout_seconds,
            default=LICENSED_ASSET_WRITE_TIMEOUT_SECONDS,
            maximum=30.0,
        )

    @property
    def effective_licensed_asset_pool_timeout_seconds(self) -> float:
        return _bounded_positive_float(
            self.licensed_asset_pool_timeout_seconds,
            default=LICENSED_ASSET_POOL_TIMEOUT_SECONDS,
            maximum=30.0,
        )

    @property
    def effective_licensed_asset_total_timeout_seconds(self) -> float:
        return _bounded_positive_float(
            self.licensed_asset_total_timeout_seconds,
            default=LICENSED_ASSET_TOTAL_TIMEOUT_SECONDS,
            maximum=30.0,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parents[1]
        local_cpa = _load_cpa_config()
        requested_model = os.getenv("PROFAGENT_GROK_MODEL", "grok4.6").strip()
        # The project contract freezes this logical model. Fail closed instead
        # of silently switching an operator's explicitly configured model.
        if requested_model != "grok4.6":
            raise ValueError("PROFAGENT_GROK_MODEL must be exactly grok4.6")
        requested_image_model = os.getenv(
            "PROFAGENT_CPA_IMAGE_MODEL", CPA_IMAGE_MODEL
        ).strip()
        if requested_image_model != CPA_IMAGE_MODEL:
            raise ValueError(
                "PROFAGENT_CPA_IMAGE_MODEL must match the frozen image model"
            )
        database_url = os.getenv("PROFAGENT_DATABASE_URL")
        if not database_url:
            database_url = f"sqlite:///{(root / '.profagent' / 'memory.sqlite3').as_posix()}"
        manifest_value = os.getenv("PROFAGENT_WARDROBE_CATALOG_MANIFEST")
        image_download_hosts = tuple(
            sorted(
                {
                    host.strip().rstrip(".").lower()
                    for host in os.getenv(
                        "PROFAGENT_CPA_IMAGE_DOWNLOAD_HOSTS", ""
                    ).split(",")
                    if host.strip()
                }
            )
        )
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
            cpa_image_enabled=_as_bool(
                os.getenv("PROFAGENT_CPA_IMAGE_ENABLED"), True
            ),
            cpa_image_model=requested_image_model,
            cpa_timeout_seconds=float(
                os.getenv("PROFAGENT_CPA_TIMEOUT_SECONDS", "120.0")
            ),
            cpa_image_timeout_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_CPA_IMAGE_TIMEOUT_SECONDS"),
                default=CPA_IMAGE_TIMEOUT_MAX_SECONDS,
                maximum=CPA_IMAGE_TIMEOUT_MAX_SECONDS,
            ),
            cpa_image_download_hosts=image_download_hosts,
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
            licensed_asset_max_response_bytes=_bounded_int(
                os.getenv("PROFAGENT_LICENSED_ASSET_MAX_RESPONSE_BYTES"),
                default=LICENSED_ASSET_MAX_RESPONSE_BYTES,
                minimum=1,
                maximum=25 * 1024 * 1024,
            ),
            licensed_asset_max_pixels=_bounded_int(
                os.getenv("PROFAGENT_LICENSED_ASSET_MAX_PIXELS"),
                default=LICENSED_ASSET_MAX_PIXELS,
                minimum=1,
                maximum=40_000_000,
            ),
            licensed_asset_min_dimension=_bounded_int(
                os.getenv("PROFAGENT_LICENSED_ASSET_MIN_DIMENSION"),
                default=LICENSED_ASSET_MIN_DIMENSION,
                minimum=1,
                maximum=8192,
            ),
            licensed_asset_max_dimension=_bounded_int(
                os.getenv("PROFAGENT_LICENSED_ASSET_MAX_DIMENSION"),
                default=LICENSED_ASSET_MAX_DIMENSION,
                minimum=1,
                maximum=8192,
            ),
            licensed_asset_max_redirects=_bounded_int(
                os.getenv("PROFAGENT_LICENSED_ASSET_MAX_REDIRECTS"),
                default=LICENSED_ASSET_MAX_REDIRECTS,
                minimum=0,
                maximum=5,
            ),
            licensed_asset_dns_timeout_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_LICENSED_ASSET_DNS_TIMEOUT_SECONDS"),
                default=LICENSED_ASSET_DNS_TIMEOUT_SECONDS,
                maximum=30.0,
            ),
            licensed_asset_connect_timeout_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_LICENSED_ASSET_CONNECT_TIMEOUT_SECONDS"),
                default=LICENSED_ASSET_CONNECT_TIMEOUT_SECONDS,
                maximum=30.0,
            ),
            licensed_asset_read_timeout_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_LICENSED_ASSET_READ_TIMEOUT_SECONDS"),
                default=LICENSED_ASSET_READ_TIMEOUT_SECONDS,
                maximum=30.0,
            ),
            licensed_asset_write_timeout_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_LICENSED_ASSET_WRITE_TIMEOUT_SECONDS"),
                default=LICENSED_ASSET_WRITE_TIMEOUT_SECONDS,
                maximum=30.0,
            ),
            licensed_asset_pool_timeout_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_LICENSED_ASSET_POOL_TIMEOUT_SECONDS"),
                default=LICENSED_ASSET_POOL_TIMEOUT_SECONDS,
                maximum=30.0,
            ),
            licensed_asset_total_timeout_seconds=_bounded_positive_float(
                os.getenv("PROFAGENT_LICENSED_ASSET_TOTAL_TIMEOUT_SECONDS"),
                default=LICENSED_ASSET_TOTAL_TIMEOUT_SECONDS,
                maximum=30.0,
            ),
            database_url=database_url.strip(),
            wardrobe_catalog_manifest=(
                Path(manifest_value).expanduser()
                if manifest_value
                else root / "data" / "manifests" / "wardrobe_generated_v1.json"
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
