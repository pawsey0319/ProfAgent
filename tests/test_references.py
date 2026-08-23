from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Any

import httpx

from profagent.asset_service import AssetRecord
from profagent.vision import VisionAdapter, VisionInspection
from scripts import validate


def test_all_required_references_exist() -> None:
    validate.validate_references(validate.load_data())


def test_vision_legacy_and_catalog_operations_share_one_verified_request_path(
    offline_settings,
) -> None:
    assert hasattr(VisionAdapter, "_request_json"), (
        "Task 3 RED: the shared verified CPA response path is absent"
    )
    assert hasattr(VisionAdapter, "inspect_catalog_asset"), (
        "Task 3 RED: the catalog assessment operation is absent"
    )
    operations: list[str] = []
    transport_calls = 0

    async def forbidden_transport(_request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        raise AssertionError("shared-path spy must prevent every transport call")

    class SharedPathSpy(VisionAdapter):
        async def _request_json(self, **kwargs: Any):
            operation = kwargs["operation"]
            operations.append(operation)
            trace = {
                "component": "vision",
                "operation": operation,
                "status": "ok",
                "reason_code": None,
                "requested_model": "grok4.6",
                "transport_model": "grok-4.6-high",
                "resolved_model": "grok-4.6-high",
                "model_verified": True,
                "schema": kwargs.get("schema", "shared_test_schema"),
                "image_logged": False,
                "latency_ms": 0,
                "interaction_budget_seconds": 0.5,
                "assessment_count": 1,
                "quality_issue_count": 0,
            }
            if operation == "catalog_asset_assessment":
                return (
                    {
                        "slot": "top",
                        "audience": "womenswear",
                        "contains_identifiable_person": False,
                        "object_region": [0.1, 0.1, 0.9, 0.9],
                        "confidence_band": "high",
                        "quality_issues": [],
                    },
                    trace,
                )
            return (
                {
                    "regions": [
                        {
                            "slot": slot,
                            "visible": True,
                            "observation": f"{slot}_visible",
                            "confidence": 0.95,
                            "issue_codes": ["none"],
                        }
                        for slot in ("top", "bottom", "shoes", "overall")
                    ]
                },
                trace,
            )

    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_base_url="https://cpa.invalid/v1",
        cpa_timeout_seconds=1.0,
        vision_interaction_budget_seconds=0.5,
    )
    adapter = SharedPathSpy(
        settings,
        transport=httpx.MockTransport(forbidden_transport),
    )
    record = AssetRecord(
        asset_id="asset_shared_path",
        user_id="u01",
        styling_session_id="session_shared_path",
        angle="front",
        media_type="image/png",
        size_bytes=16,
        width=2,
        height=2,
        visual_quality="usable",
        created_at=datetime.now().astimezone(),
        trace_id="trace_shared_path",
    )

    async def exercise() -> tuple[Any, Any]:
        legacy = await adapter.inspect([(record, b"legacy-image")])
        catalog = await adapter.inspect_catalog_asset(
            image_bytes=b"catalog-image",
            mime_type="image/png",
            allowed_slot="top",
        )
        return legacy, catalog

    legacy, catalog = asyncio.run(exercise())
    assert isinstance(legacy, VisionInspection)
    assert legacy.assessment.complete_and_confident is True
    catalog_assessment, catalog_trace = catalog
    assert catalog_assessment.slot == "top"
    assert catalog_trace["operation"] == "catalog_asset_assessment"
    assert len(operations) == 2
    assert operations[0] != "catalog_asset_assessment"
    assert operations[1] == "catalog_asset_assessment"
    assert transport_calls == 0
