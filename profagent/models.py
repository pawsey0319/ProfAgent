from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


API_VERSION = "r1_demo_v1"
DATA_VERSION = "fixtures_v1.0"

Horizon = Literal["now", "today", "soon", "planned", "unknown"]
Urgency = Literal["high", "medium", "low"]
Intent = Literal["recommend", "buy", "fill_gap", "browse", "vent"]
ConversationMode = Literal[
    "stylist_chat",
    "styling_active",
    "support_pause",
    "safety_response",
    "task_closed",
]
DialogueAction = Literal[
    "chat", "support", "clarify", "recommend", "acknowledge"
]
PendingQuestionStatus = Literal[
    "none", "active", "suspended", "resolved", "cancelled"
]
Occasion = Literal[
    "daily",
    "commute",
    "interview",
    "meeting",
    "date",
    "party",
    "travel",
    "outdoor",
    "home",
    "sports",
]
Slot = Literal["outer", "top", "bottom", "dress", "shoes", "bag", "accessory"]
Color = Literal[
    "black",
    "white",
    "gray",
    "navy",
    "beige",
    "brown",
    "khaki",
    "red",
    "pink",
    "orange",
    "yellow",
    "green",
    "blue",
    "purple",
    "multi",
]
Season = Literal["spring", "summer", "autumn", "winter", "all"]
Style = Literal[
    "simple",
    "classic",
    "smart",
    "formal",
    "street",
    "sporty",
    "soft",
    "vintage",
    "business",
    "campus",
]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class User(ApiModel):
    schema_version: int
    data_version: str
    source_id: str
    synthetic: bool
    user_id: str
    name: str
    persona: str
    profile: str
    budget: str
    styles: list[str]
    favorite_colors: list[str]
    avoid_colors: list[str]
    occasions: list[str]
    goals: list[str]


class Garment(ApiModel):
    schema_version: int
    data_version: str
    source_id: str
    synthetic: bool
    garment_id: str
    user_id: str
    name: str
    slot: Slot
    color: Color
    seasons: list[Season]
    styles: list[Style]
    occasions: list[Occasion]
    status: Literal["available", "laundry", "reserved", "unavailable"]
    formal: int
    warmth: int
    material: Literal["cotton", "knit", "denim", "wool", "linen", "leather", "synthetic"]
    fit: Literal["slim", "regular", "loose", "straight"]
    search_text: str


class OutfitFixture(ApiModel):
    schema_version: int
    data_version: str
    source_id: str
    synthetic: bool
    outfit_id: str
    user_id: str
    name: str
    items: list[str]
    grade: str
    positive: bool
    season: str
    occasion: str
    goal: str
    note: str


class CatalogItem(ApiModel):
    model_config = ConfigDict(extra="allow")

    item_id: str
    title: str
    slot: str
    color: str
    seasons: list[str]
    occasions: list[str]
    styles: list[str]
    stock: int
    delivery_days: int
    synthetic: bool
    declaration: str
    search_text: str


class WardrobePatch(ApiModel):
    name: str | None = None
    slot: Slot | None = None
    color: Color | None = None
    seasons: list[Season] | None = None
    styles: list[Style] | None = None
    occasions: list[Occasion] | None = None
    status: Literal["available", "laundry", "reserved", "unavailable"] | None = None
    formal: int | None = Field(default=None, ge=0, le=4)
    warmth: int | None = Field(default=None, ge=1, le=5)
    material: Literal["cotton", "knit", "denim", "wool", "linen", "leather", "synthetic"] | None = None
    fit: Literal["slim", "regular", "loose", "straight"] | None = None
    search_text: str | None = None

    @field_validator("name", "search_text")
    @classmethod
    def non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value


class SceneConstraints(ApiModel):
    taboo_colors: list[str] = Field(default_factory=list)
    excluded_items: list[str] = Field(default_factory=list)
    comfort_notes: list[str] = Field(default_factory=list)
    required_slots: list[str] = Field(
        default_factory=lambda: ["top", "bottom", "shoes"]
    )
    gap_slots: list[Slot] = Field(default_factory=list)
    season: str | None = None
    weather_requirement: Literal["cold", "warm", "rain", "none"] = "none"


class UICapabilities(ApiModel):
    shopping_cta: bool


class SceneParseInput(ApiModel):
    user_id: str
    query_text: str = Field(min_length=1, max_length=1000)
    request_id: str | None = None
    styling_session_id: str | None = None
    intent: Intent | None = None
    occasion: Occasion | None = None
    event_time: datetime | None = None
    event_horizon: Horizon | None = None
    goals: list[str] | None = None
    constraints: SceneConstraints | None = None


class SceneRequest(ApiModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    request_id: str
    user_id: str
    team_id: Literal["personal_team"] = "personal_team"
    member_id: Literal["stylist"] = "stylist"
    persona_id: Literal["stylist"] = "stylist"
    styling_session_id: str
    query_text: str
    intent: Intent
    occasion: Occasion
    event_time: datetime | None = None
    event_horizon: Horizon
    urgency: Urgency
    shopping_allowed: bool
    goals: list[str]
    constraints: SceneConstraints
    backend: Literal["grok4.6", "rule_fallback"]
    ranking_profile: Literal["rule_bm25_rrf_v1"] = "rule_bm25_rrf_v1"
    input_mode: Literal["text_then_image"] = "text_then_image"
    clarification_required: bool = False
    clarification_question: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    ui_capabilities: UICapabilities
    trace_id: str


class RecommendationInput(ApiModel):
    request_id: str | None = None
    scene: SceneRequest | None = None


class OutfitValidation(ApiModel):
    all_ids_grounded: bool
    hard_constraints_passed: bool
    required_slots_complete: bool


class RecommendedOutfit(ApiModel):
    outfit_id: str
    strategy_label: str
    items: list[str]
    reasons: list[str]
    risks: list[str]
    alternatives: dict[str, list[str]]
    is_primary: bool
    trust_statement: str
    validation: OutfitValidation


class ShoppingSuggestion(ApiModel):
    item_id: str
    title: str
    reason: str
    optional: Literal[True] = True
    synthetic: Literal[True] = True
    declaration: str


class InitialRecommendation(ApiModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    request_id: str
    styling_session_id: str
    assistant_message: str
    outfits: list[RecommendedOutfit]
    shopping_suggestions: list[ShoppingSuggestion]
    requested_outfit_count: Literal[1, 2, 3] | None = None
    gap_explanation: str | None = None
    ui_capabilities: UICapabilities
    trace_id: str


class DialogueTurnInput(ApiModel):
    user_id: str
    message: str = Field(min_length=1, max_length=1000)
    styling_session_id: str | None = None
    request_id: str | None = None


class DialogueProviderStatus(ApiModel):
    status: Literal["ok", "fallback"]
    attempted: bool
    requested_model: Literal["grok4.6"] = "grok4.6"
    transport_model: Literal["grok-4.6-high"] = "grok-4.6-high"
    resolved_model: Literal["grok-4.6-high", "grok-4.6-build"] | None = None
    model_verified: bool
    degraded: bool
    generation_source: Literal["cpa", "local_fallback"]
    reason_code: str | None = None


class DialogueTurnResponse(ApiModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    turn_id: str
    turn_index: int = Field(ge=1)
    history_version: int = Field(ge=1)
    request_id: str
    styling_session_id: str
    trace_id: str
    conversation_mode: ConversationMode
    action: DialogueAction
    assistant_message: str
    suggested_replies: list[str] = Field(default_factory=list, max_length=3)
    recommendation_paused: bool
    pending_question_status: PendingQuestionStatus
    provider: DialogueProviderStatus
    scene: SceneRequest | None = None
    recommendation: InitialRecommendation | None = None


class TraceRecord(ApiModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    trace_id: str
    request_id: str
    styling_session_id: str
    created_at: datetime
    updated_at: datetime
    versions: dict[str, str]
    query: dict[str, Any]
    scene: dict[str, Any] = Field(default_factory=dict)
    provider: dict[str, Any] = Field(default_factory=dict)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    retrieval: dict[str, Any] = Field(default_factory=dict)
    validator: dict[str, Any] = Field(default_factory=dict)
    catalog: dict[str, Any] = Field(default_factory=dict)
    fallback_events: list[dict[str, Any]] = Field(default_factory=list)
    dialogue: dict[str, Any] = Field(default_factory=dict)


class TeamMember(ApiModel):
    member_id: str
    persona_id: str
    name: str
    status: str
    boundary: str
    capabilities: list[str]


class TeamHomeResponse(ApiModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    team: dict[str, str]
    members: list[TeamMember]
    active_sessions: list[dict[str, Any]]
    unavailable_capabilities: list[str]


class WardrobeResponse(ApiModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    user_id: str
    items: list[Garment]
    count: int
    available_filters: dict[str, list[str]]


class WardrobePatchResponse(ApiModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    item: Garment


class HealthResponse(ApiModel):
    api_version: Literal["r1_demo_v1"] = API_VERSION
    status: Literal["ok", "degraded"]
    ready: bool
    data: dict[str, Any]
    providers: dict[str, Any]
    capabilities: dict[str, bool]
