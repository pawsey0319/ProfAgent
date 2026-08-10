from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import API_VERSION


DimensionName = Literal[
    "occasion_fit",
    "expression_match",
    "overall_harmony",
    "silhouette_layering",
    "comfort_practicality",
    "detail_finish",
]
LookStatus = Literal["active", "final", "superseded"]


class LookInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LookOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _LookCreateCommon(LookInputModel):
    user_id: str = Field(min_length=1, max_length=128)
    styling_session_id: str = Field(min_length=1, max_length=160)

    @field_validator("user_id", "styling_session_id")
    @classmethod
    def strip_identity(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("identity fields must not be blank")
        return stripped


class SelectedOutfitLookInput(_LookCreateCommon):
    created_from: Literal["selected_outfit"]
    request_id: str = Field(min_length=1, max_length=160)
    outfit_id: str = Field(min_length=1, max_length=160)
    asset_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=8)

    @field_validator("asset_ids")
    @classmethod
    def unique_assets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("asset_ids must be unique")
        return value


class UserRevisionLookInput(_LookCreateCommon):
    created_from: Literal["user_revision"]
    parent_version_id: str = Field(min_length=1, max_length=160)
    item_ids: tuple[str, ...] = Field(min_length=1, max_length=12)
    asset_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=8)

    @model_validator(mode="after")
    def unique_ids(self) -> "UserRevisionLookInput":
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("item_ids must be unique")
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("asset_ids must be unique")
        return self


class RollbackLookInput(_LookCreateCommon):
    created_from: Literal["rollback"]
    parent_version_id: str = Field(min_length=1, max_length=160)
    rollback_target_version_id: str = Field(min_length=1, max_length=160)


LookCreateInput = Annotated[
    Union[SelectedOutfitLookInput, UserRevisionLookInput, RollbackLookInput],
    Field(discriminator="created_from"),
]
# Compatibility aliases for endpoint wiring.
LookInput = LookCreateInput
LookMutationInput = LookCreateInput


class LookVersion(LookOutputModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    user_id: str
    styling_session_id: str
    request_id: str
    look_version_id: str
    parent_version_id: str | None
    version_index: int = Field(ge=1)
    status: LookStatus
    item_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    accepted_adjustments: tuple[str, ...] = Field(default_factory=tuple)
    rejected_adjustments: tuple[str, ...] = Field(default_factory=tuple)
    partially_accepted_adjustments: tuple[str, ...] = Field(default_factory=tuple)
    created_from: Literal["selected_outfit", "user_revision", "adjustment", "rollback"]
    source_outfit_id: str | None = None
    rollback_target_version_id: str | None = None
    trace_id: str
    created_at: datetime


class AdjustmentDecisionRecord(LookOutputModel):
    decision_record_id: str
    styling_session_id: str
    look_version_id: str
    adjustment_id: str | None
    canonical_action: str
    canonical_key: str
    decision: Literal["accept", "reject", "partial", "user_modified"]
    reason: str | None
    trace_id: str
    created_at: datetime

    @model_validator(mode="after")
    def canonical_fields_match(self) -> "AdjustmentDecisionRecord":
        if self.canonical_action != self.canonical_key:
            raise ValueError("canonical_action and canonical_key must match")
        return self


class LookChainResponse(LookOutputModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    user_id: str
    styling_session_id: str
    versions: tuple[LookVersion, ...]
    active_version_id: str | None
    final_version_id: str | None
    status: Literal["empty", "active", "final"]
    look_version: LookVersion | None = None
    parent: LookVersion | None = None
    comparison: "LookComparison | None" = None
    adjustment_decisions: tuple[AdjustmentDecisionRecord, ...] = Field(
        default_factory=tuple
    )


LookChain = LookChainResponse


class IdChanges(LookOutputModel):
    added: tuple[str, ...]
    removed: tuple[str, ...]
    retained: tuple[str, ...]


class LookComparison(LookOutputModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    styling_session_id: str
    from_version_id: str
    to_version_id: str
    item_changes: IdChanges
    asset_changes: IdChanges
    accepted_adjustments: tuple[str, ...]
    rejected_adjustments: tuple[str, ...]
    partially_accepted_adjustments: tuple[str, ...]
    total_score_delta: float | None = None
    improved_dimensions: tuple[DimensionName, ...] = Field(default_factory=tuple)
    declined_dimensions: tuple[DimensionName, ...] = Field(default_factory=tuple)
    unchanged_dimensions: tuple[DimensionName, ...] = Field(default_factory=tuple)
    uncertain_dimensions: tuple[DimensionName, ...] = Field(default_factory=tuple)


class ScorecardInput(LookInputModel):
    user_id: str = Field(min_length=1, max_length=128)
    styling_session_id: str = Field(min_length=1, max_length=160)
    look_version_id: str = Field(min_length=1, max_length=160)
    asset_ids: tuple[str, ...] | None = Field(default=None, max_length=8)

    @field_validator("asset_ids")
    @classmethod
    def unique_assets(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("asset_ids must be unique")
        return value


ScoreInput = ScorecardInput


class AdjustmentProposal(LookOutputModel):
    adjustment_id: str
    canonical_action: str
    canonical_key: str
    action: str
    reason: str
    expected_dimensions: tuple[DimensionName, ...] = Field(min_length=1, max_length=2)
    cost_level: Literal["zero_cost", "wardrobe_swap", "optional_purchase"]
    evidence_source: Literal["verified_visual"]
    evidence_regions: tuple[str, ...] = Field(min_length=1, max_length=3)
    scorecard_id: str
    status: Literal["proposed"] = "proposed"
    trace_id: str

    @model_validator(mode="after")
    def canonical_fields_match(self) -> "AdjustmentProposal":
        if self.canonical_action != self.canonical_key:
            raise ValueError("canonical_action and canonical_key must match")
        return self


class ScoreDimension(LookOutputModel):
    label: str
    weight: float = Field(gt=0, le=1)
    score: int | None = Field(default=None, ge=0, le=100)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=3)
    confidence: float = Field(ge=0, le=1)


class ScoreDimensions(LookOutputModel):
    occasion_fit: ScoreDimension
    expression_match: ScoreDimension
    overall_harmony: ScoreDimension
    silhouette_layering: ScoreDimension
    comfort_practicality: ScoreDimension
    detail_finish: ScoreDimension


class ScoreComparison(LookOutputModel):
    parent_version_id: str | None
    total_delta: float | None
    improved: tuple[DimensionName, ...] = Field(default_factory=tuple)
    declined: tuple[DimensionName, ...] = Field(default_factory=tuple)
    unchanged: tuple[DimensionName, ...] = Field(default_factory=tuple)
    uncertain: tuple[DimensionName, ...] = Field(default_factory=tuple)


class ScoreContext(LookOutputModel):
    occasion: str
    goals: tuple[str, ...]
    styling_session_id: str


class ProhibitedSubjectChecks(LookOutputModel):
    appearance: Literal[False] = False
    body: Literal[False] = False
    age: Literal[False] = False
    sexual_attractiveness: Literal[False] = False


class VisualEvidence(LookOutputModel):
    region: str
    observation: str
    confidence: float = Field(ge=0, le=1)


class ScoreKeepPoint(LookOutputModel):
    label: Literal["值得保留"] = "值得保留"
    statement: str
    evidence_basis: Literal["grounded_metadata", "verified_visual"]


class Scorecard(LookOutputModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    scorecard_id: str
    look_version_id: str
    target: Literal["当前穿搭×当前目标"] = "当前穿搭×当前目标"
    context: ScoreContext
    hard_constraint_status: Literal["pass", "fail"]
    dimensions: ScoreDimensions
    numeric_score_available: bool
    total_score: float | None = Field(default=None, ge=0, le=100)
    missing_evidence: tuple[str, ...] = Field(default_factory=tuple)
    visual_evidence: tuple[VisualEvidence, ...] = Field(default_factory=tuple)
    visual_confidence: float = Field(ge=0, le=1)
    evidence_status: Literal[
        "sufficient",
        "no_asset",
        "limited_asset",
        "vision_degraded",
        "hard_constraint_failed",
    ]
    guidance: str
    keep_point: ScoreKeepPoint
    prohibited_subject_checks: ProhibitedSubjectChecks = Field(
        default_factory=ProhibitedSubjectChecks
    )
    rule_version: Literal["stylist_score_v1"] = "stylist_score_v1"
    priority_adjustments: tuple[AdjustmentProposal, ...] = Field(
        default_factory=tuple, max_length=2
    )
    comparison_to_parent: ScoreComparison
    trace_id: str
    created_at: datetime


class AdjustLookInput(LookInputModel):
    user_id: str = Field(min_length=1, max_length=128)
    styling_session_id: str = Field(min_length=1, max_length=160)
    look_version_id: str = Field(min_length=1, max_length=160)
    adjustment_id: str | None = Field(default=None, min_length=1, max_length=160)
    decision: Literal["accept", "reject", "partial", "user_modified"]
    reason: str | None = Field(default=None, max_length=200)
    item_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=12)
    asset_ids: tuple[str, ...] | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def decision_contract(self) -> "AdjustLookInput":
        if self.decision != "user_modified" and self.adjustment_id is None:
            raise ValueError("adjustment_id is required for a proposed adjustment")
        if self.decision == "user_modified" and self.item_ids is None and self.asset_ids is None:
            raise ValueError("user_modified requires item_ids or asset_ids")
        if self.decision == "reject" and (self.item_ids is not None or self.asset_ids is not None):
            raise ValueError("reject cannot create a revised item or asset set")
        for field_name in ("item_ids", "asset_ids"):
            values = getattr(self, field_name)
            if values is not None and len(set(values)) != len(values):
                raise ValueError(f"{field_name} must be unique")
        return self


AdjustInput = AdjustLookInput


class AdjustLookResponse(LookOutputModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    decision_record: AdjustmentDecisionRecord
    look_version: LookVersion | None
    comparison: LookComparison
    priority_adjustments: tuple[AdjustmentProposal, ...] = Field(
        default_factory=tuple, max_length=2
    )


AdjustmentResult = AdjustLookResponse


class FinalizeLookInput(LookInputModel):
    user_id: str = Field(min_length=1, max_length=128)
    styling_session_id: str = Field(min_length=1, max_length=160)
    look_version_id: str = Field(min_length=1, max_length=160)
    satisfied: Literal[True]
    satisfaction: int = Field(ge=1, le=5)
    reason: str | None = Field(default=None, max_length=200)


FinalizeInput = FinalizeLookInput


class FinalLook(LookOutputModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    final_look_id: str
    user_id: str
    styling_session_id: str
    look_version_id: str
    item_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    scorecard_id: str | None
    total_score: float | None
    satisfied: Literal[True] = True
    satisfaction: int = Field(ge=1, le=5)
    reason: str | None
    actual_wear_status: Literal["pending"] = "pending"
    advice_stopped: Literal[True] = True
    trace_id: str
    finalized_at: datetime
