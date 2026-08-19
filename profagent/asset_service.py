from __future__ import annotations

import re
import uuid
import warnings
import zlib
from datetime import datetime
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict
from PIL import Image, ImageStat, UnidentifiedImageError

from .models import API_VERSION
from .tracing import TraceStore, utc_now


MAX_ASSET_BYTES = 5 * 1024 * 1024
MIN_USABLE_EDGE = 256
MAX_IMAGE_EDGE = 8192
MAX_IMAGE_PIXELS = 25_000_000


class AssetError(ValueError):
    pass


class AssetNotFound(KeyError):
    pass


class AssetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: str = API_VERSION
    asset_id: str
    user_id: str
    styling_session_id: str
    angle: str
    media_type: str
    size_bytes: int
    width: int | None
    height: int | None
    visual_quality: Literal["usable", "limited"]
    storage: Literal["memory_ephemeral"] = "memory_ephemeral"
    consent_obtained: Literal[True] = True
    purpose: Literal["styling_assessment", "preview_2d"] = "styling_assessment"
    retention: Literal["process_lifetime"] = "process_lifetime"
    created_at: datetime
    status: str = "available"
    trace_id: str


class AssetUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: str = API_VERSION
    asset_id: str
    user_id: str
    styling_session_id: str
    angle: str
    media_type: str
    size_bytes: int
    width: int | None
    height: int | None
    visual_quality: Literal["usable", "limited"]
    storage: Literal["memory_ephemeral"] = "memory_ephemeral"
    consent_obtained: Literal[True] = True
    purpose: Literal["styling_assessment", "preview_2d"] = "styling_assessment"
    retention: Literal["process_lifetime"] = "process_lifetime"
    created_at: datetime
    status: str
    trace_id: str


class AssetDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: str = API_VERSION
    deleted_id: str
    storage_deleted: Literal[True] = True
    trace_id: str


class AssetService:
    _MEDIA = {
        "image/png": (".png", lambda body: body.startswith(b"\x89PNG\r\n\x1a\n")),
        "image/jpeg": (".jpg", lambda body: body.startswith(b"\xff\xd8\xff")),
        "image/webp": (
            ".webp",
            lambda body: len(body) >= 12
            and body.startswith(b"RIFF")
            and body[8:12] == b"WEBP",
        ),
    }
    _ANGLE = re.compile(r"^[a-zA-Z0-9_\-\u4e00-\u9fff]{1,40}$")

    @classmethod
    def validate_static_image(
        cls, content: bytes, media_type: str | None = None
    ) -> tuple[str, int, int]:
        """Validate untrusted image bytes without storing them.

        This is the single static-image boundary shared by uploads, generated
        previews and versioned wardrobe catalog assets.
        """
        if not content:
            raise AssetError("empty asset is not accepted")
        if len(content) > MAX_ASSET_BYTES:
            raise AssetError("asset exceeds the 5MB limit")
        normalized_type = (media_type or "").lower().split(";", 1)[0].strip()
        if not normalized_type:
            normalized_type = next(
                (
                    candidate
                    for candidate, (_extension, signature) in cls._MEDIA.items()
                    if signature(content)
                ),
                "",
            )
        spec = cls._MEDIA.get(normalized_type)
        if spec is None:
            raise AssetError(
                "only static png/jpeg/webp images are accepted; video/3D is rejected"
            )
        _extension, signature_check = spec
        if not signature_check(content):
            raise AssetError(
                "file signature does not match the declared static image type"
            )
        technical_dimensions = cls._technical_dimensions(normalized_type, content)
        width, height, _informative = cls._safe_decode(
            normalized_type, content, technical_dimensions
        )
        return normalized_type, width, height

    def __init__(self, root_dir: Path, traces: TraceStore):
        self.root_dir = root_dir.resolve()
        self.traces = traces
        self._records: dict[str, AssetRecord] = {}
        self._content: dict[str, bytes] = {}
        self._lock = RLock()

    @staticmethod
    def _png_dimensions(body: bytes) -> tuple[int, int] | None:
        if len(body) < 24:
            return None
        position = 8
        dimensions: tuple[int, int] | None = None
        has_image_data = False
        has_end = False
        while position + 8 <= len(body):
            length = int.from_bytes(body[position : position + 4], "big")
            chunk_type = body[position + 4 : position + 8]
            chunk_end = position + 12 + length
            if length > MAX_ASSET_BYTES or chunk_end > len(body):
                return None
            data = body[position + 8 : position + 8 + length]
            expected_crc = int.from_bytes(
                body[position + 8 + length : chunk_end], "big"
            )
            actual_crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
            if expected_crc != actual_crc:
                raise AssetError("malformed PNG chunk checksum")
            if chunk_type == b"IHDR":
                if length != 13 or dimensions is not None:
                    raise AssetError("malformed PNG IHDR")
                dimensions = (
                    int.from_bytes(data[0:4], "big"),
                    int.from_bytes(data[4:8], "big"),
                )
            elif chunk_type == b"IDAT":
                has_image_data = True
            elif chunk_type == b"IEND":
                if length != 0:
                    raise AssetError("malformed PNG IEND")
                has_end = True
                break
            position = chunk_end
        return dimensions if dimensions and has_image_data and has_end else None

    @staticmethod
    def _jpeg_dimensions(body: bytes) -> tuple[int, int] | None:
        if len(body) < 4 or not body.endswith(b"\xff\xd9"):
            return None
        position = 2
        sof_markers = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while position + 1 < len(body):
            if body[position] != 0xFF:
                position += 1
                continue
            while position < len(body) and body[position] == 0xFF:
                position += 1
            if position >= len(body):
                return None
            marker = body[position]
            position += 1
            if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(body):
                return None
            segment_length = int.from_bytes(body[position : position + 2], "big")
            if segment_length < 2 or position + segment_length > len(body):
                return None
            if marker in sof_markers:
                if segment_length < 7:
                    raise AssetError("malformed JPEG size segment")
                height = int.from_bytes(body[position + 3 : position + 5], "big")
                width = int.from_bytes(body[position + 5 : position + 7], "big")
                return width, height
            if marker == 0xDA:
                return None
            position += segment_length
        return None

    @staticmethod
    def _webp_dimensions(body: bytes) -> tuple[int, int] | None:
        if len(body) < 20:
            return None
        declared_size = int.from_bytes(body[4:8], "little") + 8
        if declared_size > len(body):
            return None
        position = 12
        while position + 8 <= len(body):
            chunk_type = body[position : position + 4]
            chunk_size = int.from_bytes(body[position + 4 : position + 8], "little")
            data_start = position + 8
            data_end = data_start + chunk_size
            if data_end > len(body):
                return None
            data = body[data_start:data_end]
            if chunk_type == b"VP8X" and len(data) >= 10:
                return (
                    1 + int.from_bytes(data[4:7], "little"),
                    1 + int.from_bytes(data[7:10], "little"),
                )
            if chunk_type == b"VP8 " and len(data) >= 10 and data[3:6] == b"\x9d\x01\x2a":
                return (
                    int.from_bytes(data[6:8], "little") & 0x3FFF,
                    int.from_bytes(data[8:10], "little") & 0x3FFF,
                )
            if chunk_type == b"VP8L" and len(data) >= 5 and data[0] == 0x2F:
                bits = int.from_bytes(data[1:5], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            position = data_end + (chunk_size % 2)
        return None

    @classmethod
    def _technical_dimensions(
        cls, media_type: str, body: bytes
    ) -> tuple[int, int] | None:
        parser = {
            "image/png": cls._png_dimensions,
            "image/jpeg": cls._jpeg_dimensions,
            "image/webp": cls._webp_dimensions,
        }[media_type]
        dimensions = parser(body)
        if dimensions is None:
            return None
        width, height = dimensions
        if (
            width <= 0
            or height <= 0
            or width > MAX_IMAGE_EDGE
            or height > MAX_IMAGE_EDGE
            or width * height > MAX_IMAGE_PIXELS
        ):
            raise AssetError("image dimensions are invalid or exceed safety limits")
        return width, height

    @staticmethod
    def _validate_dimensions(width: int, height: int) -> None:
        if (
            width <= 0
            or height <= 0
            or width > MAX_IMAGE_EDGE
            or height > MAX_IMAGE_EDGE
            or width * height > MAX_IMAGE_PIXELS
        ):
            raise AssetError("image dimensions are invalid or exceed safety limits")

    @classmethod
    def _safe_decode(
        cls,
        media_type: str,
        body: bytes,
        technical_dimensions: tuple[int, int] | None,
    ) -> tuple[int, int, bool]:
        expected_format = {
            "image/png": "PNG",
            "image/jpeg": "JPEG",
            "image/webp": "WEBP",
        }[media_type]
        bomb_warning = Image.DecompressionBombWarning
        bomb_error = Image.DecompressionBombError
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", bomb_warning)
                with Image.open(BytesIO(body)) as image:
                    if image.format != expected_format:
                        raise AssetError("decoded image format does not match media type")
                    # The endpoint is intentionally static-only. Pillow exposes
                    # animation metadata for APNG and animated WebP; accepting
                    # frame zero would silently turn a prohibited moving asset
                    # into an apparently static upload.
                    if bool(getattr(image, "is_animated", False)) or int(
                        getattr(image, "n_frames", 1)
                    ) != 1:
                        raise AssetError("animated images are not supported")
                    width, height = image.size
                    cls._validate_dimensions(width, height)
                    if technical_dimensions and technical_dimensions != (width, height):
                        raise AssetError("decoded dimensions do not match the image header")
                    # verify() validates container integrity without trusting a
                    # forged IHDR/SOF/VP8 dimension field.
                    image.verify()

                # Re-open and fully decode pixels. verify() alone does not catch
                # every truncated or invalid compressed payload.
                with Image.open(BytesIO(body)) as image:
                    if image.format != expected_format or image.size != (width, height):
                        raise AssetError("image decode is inconsistent")
                    if bool(getattr(image, "is_animated", False)) or int(
                        getattr(image, "n_frames", 1)
                    ) != 1:
                        raise AssetError("animated images are not supported")
                    cls._validate_dimensions(*image.size)
                    image.load()
                    sample = image.convert("RGB")
                    sample.thumbnail((256, 256))
                    statistics = ImageStat.Stat(sample)
                    dynamic_range = max(
                        upper - lower for lower, upper in statistics.extrema
                    )
                    entropy = sample.entropy()
                    informative = dynamic_range >= 16 and entropy >= 1.0
        except AssetError:
            raise
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, bomb_error, bomb_warning) as exc:
            raise AssetError("static image could not be safely decoded") from exc
        return width, height, informative

    def _trace(
        self,
        *,
        operation: str,
        user_id: str,
        session_id: str,
        asset_id: str,
        details: dict[str, object],
    ) -> str:
        trace_id = f"trace_{uuid.uuid4().hex[:12]}"
        self.traces.start(
            trace_id=trace_id,
            request_id=f"asset_{operation}_{asset_id}",
            styling_session_id=session_id,
            query_text=f"asset {operation}",
            user_id=user_id,
        )
        self.traces.update(
            trace_id,
            provider={
                "component": "static_asset_store",
                "operation": operation,
                "rules": (
                    "static_image_only|single_frame_only|max_5mb|owner_bound_v1|"
                    "explicit_consent|purpose_bound|process_lifetime_retention"
                ),
                "user_id": user_id,
                "asset_id": asset_id,
                **details,
            },
        )
        return trace_id

    def upload(
        self,
        *,
        user_id: str,
        styling_session_id: str,
        angle: str,
        media_type: str | None,
        content: bytes,
        consent_obtained: bool,
        purpose: str,
    ) -> AssetUploadResponse:
        if not user_id or not styling_session_id:
            raise AssetError("user_id and styling_session_id are required")
        if not self._ANGLE.fullmatch(angle.strip()):
            raise AssetError("angle must be a short static-view label")
        if consent_obtained is not True:
            raise AssetError("explicit consent=true is required for static image upload")
        if purpose not in {"styling_assessment", "preview_2d"}:
            raise AssetError("asset purpose must be styling_assessment or preview_2d")
        lowered_angle = angle.strip().lower()
        if any(
            marker in lowered_angle
            for marker in ("3d", "360", "video", "视频", "三维", "动态")
        ):
            raise AssetError("video, 3D and 360-degree asset modes are not supported")
        normalized_type = (media_type or "").lower().split(";", 1)[0].strip()
        normalized_type, width, height = self.validate_static_image(
            content, normalized_type
        )
        technical_dimensions = (width, height)
        width, height, informative = self._safe_decode(
            normalized_type, content, technical_dimensions
        )
        visual_quality: Literal["usable", "limited"] = (
            "usable"
            if min(width, height) >= MIN_USABLE_EDGE and informative
            else "limited"
        )

        asset_id = f"asset_{uuid.uuid4().hex}"
        trace_id = self._trace(
            operation="upload",
            user_id=user_id,
            session_id=styling_session_id,
            asset_id=asset_id,
            details={
                "media_type": normalized_type,
                "size_bytes": len(content),
                "width": width,
                "height": height,
                "visual_quality": visual_quality,
                "minimum_content_signal": informative,
                "consent_obtained": True,
                "purpose": purpose,
                "retention": "process_lifetime",
                "content_logged": False,
            },
        )
        record = AssetRecord(
            asset_id=asset_id,
            user_id=user_id,
            styling_session_id=styling_session_id,
            angle=angle.strip(),
            media_type=normalized_type,
            size_bytes=len(content),
            width=width,
            height=height,
            visual_quality=visual_quality,
            purpose=purpose,
            created_at=utc_now(),
            trace_id=trace_id,
        )
        with self._lock:
            self._records[asset_id] = record
            self._content[asset_id] = bytes(content)
        return AssetUploadResponse.model_validate(record.model_dump())

    def get_owned(
        self, asset_id: str, user_id: str, styling_session_id: str
    ) -> AssetRecord:
        with self._lock:
            record = self._records.get(asset_id)
        if record is None:
            raise AssetNotFound(asset_id)
        if record.user_id != user_id or record.styling_session_id != styling_session_id:
            # Do not reveal whether a cross-owner ID exists.
            raise AssetNotFound(asset_id)
        with self._lock:
            content_exists = asset_id in self._content
        if record.status != "available" or not content_exists:
            raise AssetNotFound(asset_id)
        return record.model_copy(deep=True)

    def read_owned(
        self, asset_id: str, user_id: str, styling_session_id: str
    ) -> tuple[AssetRecord, bytes]:
        record = self.get_owned(asset_id, user_id, styling_session_id)
        with self._lock:
            content = self._content.get(asset_id)
        if content is None:
            raise AssetNotFound(asset_id)
        return record, bytes(content)

    def resolve_many(
        self, asset_ids: list[str], user_id: str, styling_session_id: str
    ) -> list[AssetRecord]:
        if len(asset_ids) != len(set(asset_ids)):
            raise AssetError("duplicate asset_ids are not allowed")
        return [
            self.get_owned(asset_id, user_id, styling_session_id)
            for asset_id in asset_ids
        ]

    def delete(
        self, asset_id: str, user_id: str, styling_session_id: str
    ) -> AssetDeleteResponse:
        # Resolve ownership before mutation and use the same not-found response
        # for missing and cross-owner IDs.
        record = self.get_owned(asset_id, user_id, styling_session_id)
        with self._lock:
            current = self._records.get(asset_id)
            if (
                current is None
                or current.user_id != user_id
                or current.styling_session_id != styling_session_id
                or asset_id not in self._content
            ):
                raise AssetNotFound(asset_id)
            self._content.pop(asset_id, None)
            self._records.pop(asset_id, None)
        trace_id = self._trace(
            operation="delete",
            user_id=user_id,
            session_id=styling_session_id,
            asset_id=asset_id,
            details={
                "media_type": record.media_type,
                "size_bytes": record.size_bytes,
                "storage": "memory_ephemeral",
                "storage_deleted": True,
                "consent_obtained": True,
                "purpose": record.purpose,
                "retention": record.retention,
                "content_logged": False,
            },
        )
        return AssetDeleteResponse(deleted_id=asset_id, trace_id=trace_id)

    def close(self) -> None:
        with self._lock:
            self._content.clear()
            self._records.clear()
