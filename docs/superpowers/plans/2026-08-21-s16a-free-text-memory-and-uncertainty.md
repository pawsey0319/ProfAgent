# S16A Free-Text Memory and Preference Uncertainty Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the template-only Memory UI with CPA-assisted, server-controlled candidate extraction and add one-question preference clarification that can influence the current recommendation without bypassing S15 consent.

**Architecture:** A new `MemoryCandidateService` performs local sensitive filtering, calls CPA only for structured extraction, maps results to a closed canonical vocabulary, and creates ordinary S15 proposals. A shared session-only preference store and `PreferenceUncertaintyPolicy` connect Memory decisions to Dialogue while PostgreSQL/SQLite remains the only long-term truth.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite/PostgreSQL repository adapters, httpx CPA transport, vanilla JavaScript/CommonJS runtime tests, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-s16-personal-stylist-memory-assets-replacement-design.md`

## Global Constraints

- Prefix every Python, pytest, Node, validation, report, and server command with `conda run --no-capture-output -n torch128`.
- Normal Dialogue and Memory extraction use CPA logical model `grok4.6`; never accept an unverified transport model or label a local fallback as CPA.
- Raw free text, raw dialogue, model reasoning, sensitive values, and direct identifiers must not enter long-term Memory, RRF, outbox, Trace, Debug DOM, or browser storage.
- Long-term writes remain `propose→confirm→commit`; sensitive memory defaults to no-write; hard memory never enters RRF.
- Keep S15 parameters exactly: `rrf_k=60`, branch weights `1.0/1.0/0.75/1.25`, Top 20 per branch, Top 5 result, and `structured_rerank_v1` weights `0.60/0.15/0.10/0.10/0.05`.
- One short preference question per turn; high urgency permits at most one such question and must continue conservatively if unanswered.
- Preference clarification is non-blocking: the same response remains `action="recommend"`, includes the legal recommendation, and keeps `recommendation_paused=false`.
- A Dialogue turn still makes at most one text CPA call. A preference answer maps through the server closure and must not trigger a second Memory-extraction CPA call in that turn.
- Do not infer or rate gender, age, body, health, or attractiveness. The visible role name remains “私人 Stylist”.
- Keep high-urgency `shopping_allowed=false`, Catalog calls `0`, grounded garment IDs, hard filters, ≤2 adjustments, immutable Look versions, and 2D-only boundaries unchanged.

---

## File Map

- Create `profagent/memory_candidates.py`: canonical candidate models, local prefilter, candidate orchestration, decision handling, and session-only preference store.
- Create `profagent/preference_uncertainty.py`: deterministic server policy for deciding and resolving preference questions.
- Modify `profagent/providers.py`: add one strict CPA structured-extraction operation using the existing verified text transport.
- Modify `profagent/memory_service.py`: expand the closed semantic vocabulary and server-authored supersede metadata without changing RRF parameters.
- Modify `profagent/models.py`: add optional preference clarification and memory-card response contracts.
- Modify `profagent/dialogue.py`, `profagent/service.py`, `profagent/scene.py`, `profagent/retrieval.py`: apply session/confirmed color and style signals and integrate the one-question policy.
- Modify `profagent/app.py`: wire shared services and add candidate extraction/decision endpoints.
- Create `web/memory_candidate_runtime.js` and `web/preference_clarification_runtime.js`: small fail-closed browser adapters.
- Modify `web/index.html`, `web/app.js`, `web/memory_runtime.js`, `web/dialogue_runtime.js`, `web/styles.css`: free-text UI and non-blocking confirmation cards.
- Create `tests/test_s16_memory_candidates.py`, `tests/test_s16_preference_uncertainty.py`, `web/memory_candidate_runtime_test.cjs`, `web/preference_clarification_runtime_test.cjs`, and `scripts/tester_s16a_report.py`.

### Task 1: Freeze S16 contracts and BUILD_LOG stages

**Files:**
- Modify: `BUILD_LOG.md`
- Modify: `docs/API_CONTRACT.md`
- Modify: `docs/AC_MATRIX.md`
- Modify: `docs/PRD.md`
- Modify: `docs/superpowers/specs/2026-08-21-s16-personal-stylist-memory-assets-replacement-design.md`

**Interfaces:**
- Consumes: approved S16 design and S15 API contracts.
- Produces: authoritative request/response names used by every later S16 task.

- [ ] **Step 1: Mark the written design approved and append ordered S16 stages**

Use `S16-0`, `S16A`, `S16B`, `S16C`, and `S16R`; move the old “S16+ LangMem” future label to `S17+`. Do not mark any S16 stage complete.

- [ ] **Step 2: Freeze the candidate endpoints**

Add these exact contracts to `docs/API_CONTRACT.md`:

```text
POST /memory/candidates/extract
POST /memory/candidates/{candidate_id}/decide
```

The extract request contains only `user_id`, optional `styling_session_id`, `namespace`, `text`, and `request_id`. The decision request contains only `user_id`, optional `styling_session_id`, `decision=remember|session_only|reject|rephrase`, and `idempotency_key`.

- [ ] **Step 3: Freeze the Dialogue additions**

Document optional response fields `preference_clarification` and `memory_candidates`; document optional input fields `preference_question_id` and `preference_option_id`. State that the server, not the browser or CPA, owns ranking and question budgets.

- [ ] **Step 4: Synchronize AC wording**

Map S16A to AC-01/03/04/05/06/07/10/11/14/16, MEM-01–12, SAFE-04/08, and OBS-05. Keep the existing R1 thresholds verbatim.

- [ ] **Step 5: Verify documentation consistency**

Run:

```powershell
conda run --no-capture-output -n torch128 python -c "from pathlib import Path; files=[Path('BUILD_LOG.md'),Path('docs/API_CONTRACT.md'),Path('docs/AC_MATRIX.md'),Path('docs/PRD.md')]; text='\n'.join(p.read_text(encoding='utf-8') for p in files); assert '/memory/candidates/extract' in text; assert 'S17+' in text; assert 'rrf_k=60' in text or 'k=60' in text"
git diff --check
```

Expected: both commands exit `0`.

- [ ] **Step 6: Commit the frozen contract**

```powershell
git add BUILD_LOG.md docs/API_CONTRACT.md docs/AC_MATRIX.md docs/PRD.md docs/superpowers/specs/2026-08-21-s16-personal-stylist-memory-assets-replacement-design.md
git commit -m "docs: freeze S16 personal stylist contracts"
```

### Task 2: Add the closed canonical memory vocabulary

**Files:**
- Create: `profagent/memory_candidates.py`
- Modify: `profagent/memory_service.py`
- Modify: `profagent/scene.py`
- Modify: `profagent/retrieval.py`
- Test: `tests/test_s16_memory_candidates.py`
- Test: `tests/test_memory_rerank_policy.py`

**Interfaces:**
- Consumes: existing `MemoryService.propose()`, `confirm()`, `active_signals()`, and `retrieve_soft()`.
- Produces: `CanonicalMemoryCandidate`, `SessionPreferenceStore`, and controlled `prefer_color:*`/`avoid_color:*` signals.

- [ ] **Step 1: Write failing canonicalization and no-write tests**

Add tests equivalent to:

```python
def test_color_candidates_map_to_closed_content_without_raw_text():
    mapped = canonicalize_candidate("color_preference", "navy")
    assert mapped.memory_type == "preference"
    assert mapped.content == "偏爱藏青色"
    assert mapped.soft_term == "color:navy"

def test_sensitive_text_is_blocked_before_provider_or_repository(app_services):
    result = app_services.memory_candidates.prefilter("我体重80kg，帮我长期记住")
    assert result.code == "SENSITIVE_MEMORY_DEFAULT_NO_WRITE"
    assert app_services.llm.interaction_count == 0
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_memory_candidates.py -k "color_candidates or sensitive_text"
```

Expected: FAIL because `memory_candidates.py` and canonical functions do not exist.

- [ ] **Step 3: Implement the closed models and mapper**

Create these public types and signatures:

```python
CanonicalMemoryKind = Literal[
    "color_preference", "color_avoidance", "style_preference",
    "garment_preference", "garment_avoidance", "comfort_preference",
    "occasion_preference",
]

class CanonicalMemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    canonical_kind: CanonicalMemoryKind
    canonical_value: str
    memory_type: MemoryType
    memory_class: MemoryClass
    content: str
    applicability_tags: tuple[str, ...] = ()
    confirmation_copy: str

@dataclass(frozen=True)
class SessionPreference:
    user_id: str
    styling_session_id: str
    canonical_kind: CanonicalMemoryKind
    canonical_value: str
    expires_at: float

class SessionPreferenceStore:
    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._items: dict[tuple[str, str, str], SessionPreference] = {}
        self._lock = RLock()

    def put(self, preference: SessionPreference) -> SessionPreference:
        key = (preference.user_id, preference.styling_session_id, preference.canonical_kind)
        with self._lock:
            self._items[key] = preference
        return preference

    def active(self, user_id: str, styling_session_id: str, now: float) -> tuple[SessionPreference, ...]:
        with self._lock:
            return tuple(item for item in self._items.values() if item.user_id == user_id and item.styling_session_id == styling_session_id and item.expires_at > now)

def canonicalize_candidate(kind: str, value: str) -> CanonicalMemoryCandidate:
    key = (kind.strip(), value.strip().lower())
    try:
        return CANONICAL_MEMORY_MAP[key].model_copy(deep=True)
    except KeyError as exc:
        raise MemoryCandidateError("candidate is outside the controlled closure") from exc
```

Use explicit dictionaries for the 15 existing color enums and current style/garment/comfort closure. Unknown values raise `MemoryCandidateError`; do not store arbitrary CPA wording.

- [ ] **Step 4: Extend MemoryService without changing RRF constants**

Add generated closed strings such as `偏爱藏青色` and `不穿紫色`, derive `prefer_color:navy` and `avoid_color:purple`, add avoid colors to authoritative Scene constraints, and allow controlled color terms in retrieval:

```python
if signal.applied_signal.startswith("avoid_color:"):
    taboo.add(signal.applied_signal.removeprefix("avoid_color:"))

controlled_soft_terms = tuple(
    term for term in soft_memory_terms
    if re.fullmatch(r"(?:fit|style|comfort|color):[a-z_]+", term)
)
```

Keep `RRF_K`, `RRF_WEIGHTS`, `RRF_CANDIDATE_LIMIT`, `RRF_TOP_K`, and the rerank formula byte-for-byte unchanged.

- [ ] **Step 5: Run focused and S15 regression tests**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_memory_candidates.py tests/test_memory_rerank_policy.py tests/test_s15_memory.py
```

Expected: PASS; hard-memory-in-RRF remains `false` and S15 exact weights remain unchanged.

- [ ] **Step 6: Commit canonical memory support**

```powershell
git add profagent/memory_candidates.py profagent/memory_service.py profagent/scene.py profagent/retrieval.py tests/test_s16_memory_candidates.py tests/test_memory_rerank_policy.py
git commit -m "feat: add controlled S16 memory candidates"
```

### Task 3: Add strict CPA candidate extraction

**Files:**
- Modify: `profagent/providers.py`
- Modify: `profagent/memory_candidates.py`
- Modify: `profagent/config.py`
- Test: `tests/test_s16_memory_candidates.py`
- Test: `tests/test_cpa_interaction_budget.py`

**Interfaces:**
- Consumes: verified `GrokLLMProvider` text transport and `canonicalize_candidate()`.
- Produces: `GrokLLMProvider.extract_memory_candidates(text, allowed_schema)` returning validated candidate dictionaries plus provider evidence.

- [ ] **Step 1: Write provider success, wrong-model, malformed-schema, and timeout tests**

The success fixture must return only:

```json
{"candidates":[{"canonical_kind":"color_preference","canonical_value":"navy","applicability_tags":[],"confidence_band":"high"}]}
```

Assert duplicate keys, additional fields, unknown enums, wrong transport models, identifiers, and more than five candidates are rejected before proposals are created.

- [ ] **Step 2: Run tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_memory_candidates.py -k provider
```

Expected: FAIL because the provider operation is absent.

- [ ] **Step 3: Implement one bounded provider operation**

Add this method to `GrokLLMProvider`:

```python
async def extract_memory_candidates(
    self,
    text: str,
    *,
    allowed_kinds: tuple[str, ...],
    allowed_values: dict[str, tuple[str, ...]],
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    """Return schema-only candidates; never return or persist reasoning."""
```

Use the existing exact model verification, CPA headers, cancellation, duplicate-key rejection, JSON parsing, and output size limits. Give the model no user IDs, garment IDs, Memory rows, raw Trace, or hidden profile. Cap output at five candidates.

- [ ] **Step 4: Orchestrate prefilter → CPA → canonical mapper**

In `MemoryCandidateService.extract()`, call `MemoryService.is_sensitive(text)` and direct-identifier detection first. Only safe text reaches CPA. Map every provider candidate through `canonicalize_candidate()`; if any candidate is invalid, reject the whole provider payload rather than partially trusting it.

- [ ] **Step 5: Verify provider and cancellation behavior**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_memory_candidates.py tests/test_cpa_interaction_budget.py
```

Expected: PASS; timeout creates zero proposals and provider success retains verified `grok4.6` evidence.

- [ ] **Step 6: Commit extraction support**

```powershell
git add profagent/providers.py profagent/memory_candidates.py profagent/config.py tests/test_s16_memory_candidates.py tests/test_cpa_interaction_budget.py
git commit -m "feat: extract memory candidates through CPA"
```

### Task 4: Add candidate extraction and decision APIs

**Files:**
- Modify: `profagent/memory_candidates.py`
- Modify: `profagent/memory_service.py`
- Modify: `profagent/memory_repository.py`
- Modify: `profagent/app.py`
- Test: `tests/test_s16_memory_candidates.py`

**Interfaces:**
- Consumes: canonical mapper, CPA extractor, S15 proposal/confirm repository.
- Produces: `POST /memory/candidates/extract` and `POST /memory/candidates/{candidate_id}/decide`.

- [ ] **Step 1: Write HTTP tests for multi-card, idempotency, supersede, session-only, reject, and rephrase**

Assert that the browser cannot submit `canonical_kind`, `canonical_value`, `memory_class`, `supersedes_memory_id`, or confirmation counts to the decision endpoint.

- [ ] **Step 2: Run HTTP tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_memory_candidates.py -k "http or decision or supersede"
```

Expected: FAIL with route-not-found or missing service methods.

- [ ] **Step 3: Implement exact request and response models**

```python
class MemoryCandidateExtractInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    styling_session_id: str | None = None
    namespace: Literal["shared", "stylist"]
    text: str = Field(min_length=1, max_length=1000)
    request_id: str

class MemoryCandidateDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    styling_session_id: str | None = None
    decision: Literal["remember", "session_only", "reject", "rephrase"]
    idempotency_key: str

class MemoryCandidateCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: str
    confirmation_copy: str
    confidence_band: Literal["high", "medium", "low"]
    conflict_copy: str | None = None
    allowed_actions: tuple[Literal["remember", "session_only", "reject", "rephrase"], ...]

class MemoryCandidateExtractResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    status: Literal["ready", "needs_rephrase", "blocked"]
    candidates: tuple[MemoryCandidateCard, ...]
    trace_id: str

class MemoryCandidateDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: str
    decision: Literal["remember", "session_only", "reject", "rephrase"]
    status: Literal["committed", "session_only", "rejected"]
    record_id: str | None
    trace_id: str

class MemoryCandidateService:
    async def extract(self, payload: MemoryCandidateExtractInput) -> MemoryCandidateExtractResponse:
        return await self._extract_prefiltered_and_create_proposals(payload)

    def decide(self, candidate_id: str, payload: MemoryCandidateDecisionInput) -> MemoryCandidateDecisionResponse:
        return self._decide_from_server_candidate(candidate_id, payload)
```

`session_only` requires an active owner-bound `styling_session_id`; without one the server rejects that decision and the UI disables it with an explanation.

The extract response exposes natural confirmation copy, conflict copy, confidence band, allowed actions, and an opaque candidate ID. It must not expose raw text or provider reasoning.

- [ ] **Step 4: Reuse S15 proposal truth and implement supersede**

For each canonical candidate, create an ordinary proposal. Candidate IDs resolve server-side to proposals. `remember` performs confirm/commit; `session_only` rejects the proposal and adds a TTL-bound entry to `SessionPreferenceStore`; `reject` and `rephrase` reject it. Server-authored semantic conflicts carry `supersedes_memory_id`, and commit atomically marks the previous record superseded.

- [ ] **Step 5: Verify persistence, ACL, and absence of raw text**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_memory_candidates.py tests/test_s15_memory.py
```

Expected: PASS; after restart only confirmed canonical content remains; raw input is absent from SQLite rows, outbox payloads, Trace JSON, and list responses.

- [ ] **Step 6: Commit candidate APIs**

```powershell
git add profagent/memory_candidates.py profagent/memory_service.py profagent/memory_repository.py profagent/app.py tests/test_s16_memory_candidates.py
git commit -m "feat: add free-text memory candidate APIs"
```

### Task 5: Add deterministic preference uncertainty policy

**Files:**
- Create: `profagent/preference_uncertainty.py`
- Modify: `profagent/models.py`
- Modify: `profagent/dialogue.py`
- Modify: `profagent/service.py`
- Modify: `profagent/retrieval.py`
- Modify: `profagent/app.py`
- Test: `tests/test_s16_preference_uncertainty.py`
- Test: `tests/test_dialogue_turn.py`
- Test: `tests/test_s14_backend.py`

**Interfaces:**
- Consumes: validated `InitialRecommendation`, repository garment metadata, confirmed Memory signals, and `SessionPreferenceStore`.
- Produces: optional `PreferenceClarification` and stable reordering of the same grounded outfits.

- [ ] **Step 1: Write policy tests for material difference and budgets**

Expose a pure `PreferenceUncertaintyEvidence` boundary and include these cases:

```python
def test_asks_once_when_two_legal_outfits_differ_on_unremembered_color():
    evidence = PreferenceUncertaintyEvidence(
        gap_code="color:navy_vs_beige", neutral_margin=0.03,
        counterfactual_winners=("outfit_navy", "outfit_beige"),
        urgency="low", confirmed=False, already_asked=False,
    )
    assert PreferenceUncertaintyPolicy().should_ask(evidence) is True

def test_does_not_ask_when_confirmed_color_preference_exists():
    evidence = PreferenceUncertaintyEvidence(
        gap_code="color:navy_vs_beige", neutral_margin=0.03,
        counterfactual_winners=("outfit_navy", "outfit_beige"),
        urgency="low", confirmed=True, already_asked=False,
    )
    assert PreferenceUncertaintyPolicy().should_ask(evidence) is False

def test_high_urgency_second_question_is_blocked():
    evidence = PreferenceUncertaintyEvidence(
        gap_code="color:navy_vs_beige", neutral_margin=0.03,
        counterfactual_winners=("outfit_navy", "outfit_beige"),
        urgency="high", confirmed=False, already_asked=True,
    )
    assert PreferenceUncertaintyPolicy().should_ask(evidence) is False
```

- [ ] **Step 2: Run policy tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_preference_uncertainty.py
```

Expected: FAIL because the policy and models do not exist.

- [ ] **Step 3: Implement the policy boundary**

```python
@dataclass(frozen=True)
class PreferenceDecision:
    clarification: PreferenceClarification | None
    reordered_outfit_ids: tuple[str, ...]
    reason_code: str

class PreferenceOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    option_id: str
    label: str

class PreferenceClarification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    question_id: str
    gap_code: str
    status: Literal["open"] = "open"
    options: tuple[PreferenceOption, ...]
    urgency_budget: Literal["normal", "last"]

@dataclass(frozen=True)
class PreferenceUncertaintyEvidence:
    gap_code: str
    neutral_margin: float
    counterfactual_winners: tuple[str, str]
    urgency: Urgency
    confirmed: bool
    already_asked: bool

class PreferenceUncertaintyPolicy:
    @staticmethod
    def should_ask(evidence: PreferenceUncertaintyEvidence) -> bool:
        return (
            not evidence.confirmed
            and not evidence.already_asked
            and evidence.neutral_margin <= 0.05
            and len(set(evidence.counterfactual_winners)) == 2
        )

    def evaluate(
        self,
        *,
        scene: SceneRequest,
        recommendation: InitialRecommendation,
        confirmed_signals: tuple[MemorySignal, ...],
        session_preferences: tuple[SessionPreference, ...],
        asked_gap_codes: frozenset[str],
    ) -> PreferenceDecision:
        evidence = self.build_evidence(
            scene=scene,
            recommendation=recommendation,
            confirmed_signals=confirmed_signals,
            session_preferences=session_preferences,
            asked_gap_codes=asked_gap_codes,
        )
        if evidence is None or not self.should_ask(evidence):
            return PreferenceDecision(None, tuple(o.outfit_id for o in recommendation.outfits), "NO_MATERIAL_GAP")
        return PreferenceDecision(self.build_clarification(evidence), tuple(o.outfit_id for o in recommendation.outfits), "MATERIAL_PREFERENCE_GAP")
```

Only compare hard-valid outfits. Add a server-only normalized outfit-ranking diagnostic: ask only when the neutral top-two score margin is `<=0.05` **and** applying either controlled preference in a counterfactual rerank changes the winner. Use stable retrieval/assembler evidence and garment metadata, never CPA or client scores.

- [ ] **Step 4: Extend Dialogue state and contracts**

Add `asked_preference_gap_codes`, `pending_preference_question_id`, and `pending_preference_gap_code` to `DialogueEnvelope`. Add optional `preference_question_id`/`preference_option_id` to input and optional `preference_clarification`/`memory_candidates` to response. Preserve retry fingerprints and copy logic.

The response remains `action="recommend"`, carries the already validated recommendation, and sets `recommendation_paused=false`; the question is advisory and cannot hide or delay high-urgency results.

- [ ] **Step 5: Integrate current-turn reorder and optional memory card**

On an explicit answer, reorder the existing grounded outfits without changing item sets, update `is_primary`, and emit at most one canonical memory candidate card. Map the explicit option through the server closure, so this turn still uses at most the one normal Dialogue CPA call. “随便/你决定” records only a neutral session decision and emits no long-term candidate.

- [ ] **Step 6: Run Dialogue and R1 safety regressions**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_preference_uncertainty.py tests/test_dialogue_turn.py tests/test_s14_backend.py tests/test_scene_clarification_context.py tests/test_scene_horizon.py
```

Expected: PASS; high urgency retains shopping false and Catalog zero; each response contains at most one question.

- [ ] **Step 7: Commit uncertainty policy**

```powershell
git add profagent/preference_uncertainty.py profagent/models.py profagent/dialogue.py profagent/service.py profagent/retrieval.py profagent/app.py tests/test_s16_preference_uncertainty.py tests/test_dialogue_turn.py tests/test_s14_backend.py
git commit -m "feat: clarify material styling preferences"
```

### Task 6: Replace the Memory template UI with free text

**Files:**
- Create: `web/memory_candidate_runtime.js`
- Create: `web/memory_candidate_runtime_test.cjs`
- Modify: `web/index.html:385-420`
- Modify: `web/app.js:2638-2917`
- Modify: `web/memory_runtime.js`
- Modify: `web/memory_runtime_test.cjs`
- Modify: `web/s15_frontend_static_test.cjs`
- Modify: `web/styles.css:2134-2235`
- Create: `web/s16_frontend_static_test.cjs`

**Interfaces:**
- Consumes: candidate extraction/decision API.
- Produces: one free-text composer and independently actionable candidate cards.

- [ ] **Step 1: Write failing runtime/static tests**

Assert: no `memory-type` or template selector; no raw text in runtime state after response; additional fields fail closed; every candidate card exposes exactly four actions; conflict copy is visible; rephrase focuses the text box without exposing canonical fields.

- [ ] **Step 2: Run Node tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 node web/memory_candidate_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/s16_frontend_static_test.cjs
```

Expected: FAIL because the runtime and new markup do not exist.

- [ ] **Step 3: Implement the fail-closed runtime**

Export only:

```javascript
function assertExactKeys(value, keys) {
  const actual = Object.keys(value || {}).sort();
  const expected = [...keys].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error("响应字段不匹配");
}
function normalizeExtractionResponse(payload, expectedRequestId) {
  assertExactKeys(payload, ["request_id", "status", "candidates", "trace_id"]);
  if (payload.request_id !== expectedRequestId || !Array.isArray(payload.candidates)) throw new Error("提取回执不匹配");
  return Object.freeze({ ...payload, candidates: Object.freeze(payload.candidates.map(Object.freeze)) });
}
function normalizeCandidateDecision(payload, expectedCandidateId) {
  assertExactKeys(payload, ["candidate_id", "decision", "status", "record_id", "trace_id"]);
  if (payload.candidate_id !== expectedCandidateId) throw new Error("候选回执不匹配");
  return Object.freeze({ ...payload });
}
function candidateActions(candidate) { return ["remember", "session_only", "reject", "rephrase"]; }
```

Do not write source text or candidate values to `localStorage`, `sessionStorage`, Debug, or Trace views.

- [ ] **Step 4: Replace the form and render cards**

Use a single `<textarea id="memory-free-text">`; submit to the extract endpoint; clear the textarea after a bound response; render each candidate separately. Keep S15 committed-record validation and deletion UI unchanged.

- [ ] **Step 5: Run Memory UI and S15 regressions**

```powershell
conda run --no-capture-output -n torch128 node web/memory_candidate_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/memory_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/s15_frontend_static_test.cjs
conda run --no-capture-output -n torch128 node web/s16_frontend_static_test.cjs
```

Expected: PASS.

- [ ] **Step 6: Commit free-text Memory UI**

```powershell
git add web/memory_candidate_runtime.js web/memory_candidate_runtime_test.cjs web/index.html web/app.js web/memory_runtime.js web/memory_runtime_test.cjs web/s15_frontend_static_test.cjs web/s16_frontend_static_test.cjs web/styles.css
git commit -m "feat: add free-text memory confirmation cards"
```

### Task 7: Render preference clarification in Dialogue

**Files:**
- Create: `web/preference_clarification_runtime.js`
- Create: `web/preference_clarification_runtime_test.cjs`
- Modify: `web/dialogue_runtime.js`
- Modify: `web/dialogue_runtime_test.cjs`
- Modify: `web/app.js:519-602,3143-3321`
- Modify: `web/styles.css:647-770`
- Modify: `web/s16_frontend_static_test.cjs`

**Interfaces:**
- Consumes: optional `preference_clarification` and `memory_candidates` from `/dialogue/turn`.
- Produces: one server-authored question, option replies through the same Dialogue endpoint, and a separate non-blocking Memory confirmation card.

- [ ] **Step 1: Write failing Dialogue runtime tests**

Cover question ID binding, duplicate gap suppression, neutral choice, high-urgency last-question state, stale response rejection, and the absence of browser-side ranking.

- [ ] **Step 2: Run Node tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 node web/preference_clarification_runtime_test.cjs
```

Expected: FAIL because the runtime is absent.

- [ ] **Step 3: Implement exact response normalization**

```javascript
function normalizePreferenceClarification(value) {
  if (!value) return null;
  assertExactKeys(value, ["question_id", "gap_code", "status", "options", "urgency_budget"]);
  if (value.status !== "open" || !Array.isArray(value.options) || value.options.length < 2 || value.options.length > 3) throw new Error("偏好追问合同无效");
  return Object.freeze({ ...value, options: Object.freeze(value.options.map(Object.freeze)) });
}
function buildPreferenceTurn(question, option, state) {
  return { message: option.label, preference_question_id: question.question_id, preference_option_id: option.option_id };
}
```

The runtime must not create question text, change outfits, or write memory.

- [ ] **Step 4: Integrate with existing Enter/Shift/IME and single-flight logic**

Typed answers continue through the normal composer. Option clicks submit the same `/dialogue/turn` once. Render `memory_candidates` below the assistant response without asking a second chat question.

- [ ] **Step 5: Run browser-facing regressions**

```powershell
conda run --no-capture-output -n torch128 node web/dialogue_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/preference_clarification_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/s16_frontend_static_test.cjs
```

Expected: PASS.

- [ ] **Step 6: Commit Dialogue clarification UI**

```powershell
git add web/preference_clarification_runtime.js web/preference_clarification_runtime_test.cjs web/dialogue_runtime.js web/dialogue_runtime_test.cjs web/app.js web/styles.css web/s16_frontend_static_test.cjs
git commit -m "feat: render one-question styling clarification"
```

### Task 8: Add the S16A atomic report and reviewer gate

**Files:**
- Create: `scripts/tester_s16a_report.py`
- Create: `tests/test_s16a_report.py`
- Create: `reports/eval/s16a_memory_uncertainty_v1.json`
- Create: `reports/eval/s16a_memory_uncertainty_v1.md`
- Modify: `BUILD_LOG.md`

**Interfaces:**
- Consumes: all S16A tests and existing S15/R1 reports.
- Produces: one deterministic JSON+MD report used by reviewer and supervisor.

- [ ] **Step 1: Write the failing report contract test**

Require fields for candidate extraction, sensitive no-write, raw-text leakage count, supersede, one-question budget, high-urgency continuation, provider calls, full pytest, Node tests, and fixed R1 metrics.

- [ ] **Step 2: Run the report test and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16a_report.py
```

Expected: FAIL because the report script is absent.

- [ ] **Step 3: Implement the atomic runner**

Use `subprocess.run(command, cwd=ROOT, check=True, text=True, capture_output=True)` for focused pytest, Memory tests, Dialogue tests, Node runtime/static tests, full pytest, `scripts/validate.py`, and fixed eval. Write reports only after every command succeeds.

- [ ] **Step 4: Run the reviewer read-only gate**

Reviewer checks MEM-01–12, SAFE-04/08, AC-01/03/04/07/10/11/14/16, raw-text leakage, model provenance, question repetition, and unchanged S15 formulas. Any `[P0]/[P1]/[P2]` returns to the owning agent.

- [ ] **Step 5: Run the atomic report**

```powershell
conda run --no-capture-output -n torch128 python scripts/tester_s16a_report.py
```

Expected: exit `0`, reports say `PASS`, external CPA calls are `0` in deterministic tests, and reviewer findings are all zero.

- [ ] **Step 6: Commit S16A reports and close only S16A**

```powershell
git add scripts/tester_s16a_report.py tests/test_s16a_report.py reports/eval/s16a_memory_uncertainty_v1.json reports/eval/s16a_memory_uncertainty_v1.md BUILD_LOG.md
git commit -m "test: close S16A memory uncertainty milestone"
```
