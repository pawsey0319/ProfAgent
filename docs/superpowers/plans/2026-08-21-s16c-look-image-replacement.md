# S16C Active-Look Image Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user select one recommended direction as Look v1, long-press a garment region, choose a server-validated replacement, receive immutable Look vN immediately, and then receive a separately generated 2D reference image.

**Architecture:** Replacement is an owner-bound single-slot server transaction over an active immutable Look, not a client array edit. An image-job service wraps the existing CPA Image path asynchronously, while a dedicated slot-localization adapter converts CPA Vision output into validated normalized regions and falls back to an accessible slot chooser whenever localization is unreliable.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, asyncio, existing Look/Preview/CPA Image/Vision services, vanilla JavaScript Pointer Events/CommonJS tests, Playwright/Chrome smoke tests, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-s16-personal-stylist-memory-assets-replacement-design.md`

## Global Constraints

- Prefix every command with `conda run --no-capture-output -n torch128`.
- A recommendation direction is not a Look. Long-press replacement becomes available only after the user selects one direction and the server creates active Look v1.
- The client may send only owner/session/base Look version/base version index/slot/replacement garment/receipt/idempotency. It may not submit complete item IDs, prompts, models, scores, or hard-constraint results.
- Every replacement must recheck owner whitelist, receipt, available status, season/weather, taboo/excluded items, required slots, occasion/formality, shopping gate, and active-version CAS.
- High urgency always keeps `shopping_allowed=false`, Catalog call count `0`, and no shopping CTA.
- Every Look version is immutable, comparable, and rollback-capable. Image output is never Look truth.
- CPA Image uses the independently configured image operation; slot localization uses a separate CPA Vision schema. Text, Image, and Vision provenance may not be conflated.
- Static2D is a “搭配视觉参考”; never claim real try-on, identity reproduction, exact fit, size, fabric, texture, or drape.
- Long press is `450ms`; pointer movement over `12 CSS px`, scrolling, cancellation, or lost capture cancels it. Keyboard Enter must reach the same slot chooser.
- Invalid, overlapping, out-of-bounds, or confidence `<0.75` regions trigger the slot chooser; never guess.
- S16 Demo keeps Look/Image Job in the current single-process lifecycle and documents that restart boundary. Repository interfaces must permit later persistence, but cross-restart Look/Image Job persistence is not claimed in S16.

---

## File Map

- Create `profagent/look_replacement.py`: replacement options receipts and single-slot transaction orchestration.
- Create `profagent/look_slot_localizer.py`: slot-map models and server validation.
- Create `profagent/image_jobs.py`: asynchronous Look-bound Static2D jobs, retry, idempotency, and status.
- Modify `profagent/look_models.py`, `profagent/look_service.py`, `profagent/preview.py`, `profagent/vision.py`, `profagent/app.py`, and `profagent/config.py`.
- Create `web/look_replacement_runtime.js` and focused runtime/browser tests.
- Modify `web/app.js`, `web/recommendation_preview_runtime.js`, `web/index.html`, and `web/styles.css`.
- Create `tests/test_s16_replacement.py`, `tests/test_s16_image_jobs.py`, `tests/test_s16_slot_localization.py`, `web/look_replacement_runtime_test.cjs`, `web/s16_replacement_browser_smoke.cjs`, and `scripts/tester_s16_report.py`.

### Task 1: Freeze Active Look, replacement, image-job, and restart contracts

**Files:**
- Modify: `BUILD_LOG.md`
- Modify: `docs/API_CONTRACT.md`
- Modify: `docs/AC_MATRIX.md`
- Modify: `docs/PRD.md`
- Modify: `docs/superpowers/specs/2026-08-21-s16-personal-stylist-memory-assets-replacement-design.md`

**Interfaces:**
- Consumes: S16-0 contracts and current Look/Preview APIs.
- Produces: exact endpoint names and single-process lifecycle statement used by backend and frontend.

- [ ] **Step 1: Freeze replacement endpoints**

Document these exact routes:

```text
POST /look/replacements/options
POST /look/replacements/apply
```

Options requires `user_id`, `styling_session_id`, `base_look_version_id`, `base_version_index`, and `slot`. Apply adds `replacement_garment_id`, `options_receipt`, and `idempotency_key`.

- [ ] **Step 2: Freeze image-job and slot-map endpoints**

```text
POST /look/{look_version_id}/images/static-2d
GET  /look/image-jobs/{job_id}
POST /look/image-jobs/{job_id}/retry
GET  /look/image-jobs/{job_id}/image
GET  /look/{look_version_id}/slot-map
```

The Look transaction returns before image completion. Image/job/slot responses bind `user_id + styling_session_id + look_version_id + job_id + content hash`.

- [ ] **Step 3: Resolve the old “替换入口” wording**

Change PRD/AC/API wording from a permanent button to “可发现且键盘可访问的单件替换能力：图片长按或槽位选择器”. Do not remove the replacement AC itself.

- [ ] **Step 4: State the restart boundary**

Document: Memory remains restart/cross-instance durable; Look, Dialogue, Preview, and Image Job remain owner-bound single-process Demo state in S16. A future persistence migration is not implied to be complete.

- [ ] **Step 5: Verify contract consistency**

```powershell
conda run --no-capture-output -n torch128 python -c "from pathlib import Path; t='\n'.join(Path(p).read_text(encoding='utf-8') for p in ['BUILD_LOG.md','docs/API_CONTRACT.md','docs/AC_MATRIX.md','docs/PRD.md']); assert '/look/replacements/apply' in t; assert '/look/image-jobs/' in t; assert '450' in t; assert '单进程' in t"
git diff --check
```

Expected: exit `0`.

- [ ] **Step 6: Commit the frozen replacement contract**

```powershell
git add BUILD_LOG.md docs/API_CONTRACT.md docs/AC_MATRIX.md docs/PRD.md docs/superpowers/specs/2026-08-21-s16-personal-stylist-memory-assets-replacement-design.md
git commit -m "docs: freeze S16 active look replacement contracts"
```

### Task 2: Implement server-side replacement options receipts

**Files:**
- Create: `profagent/look_replacement.py`
- Modify: `profagent/look_models.py`
- Modify: `profagent/look_service.py`
- Modify: `profagent/app.py`
- Test: `tests/test_s16_replacement.py`
- Test: `tests/test_r1_workflows.py`

**Interfaces:**
- Consumes: active Look snapshot, authoritative Scene, owner wardrobe, `HardFilter`, and whole-Look validation.
- Produces: signed/opaque expiring `ReplacementOptionsReceipt` and grounded replacement cards.

- [ ] **Step 1: Write failing options tests**

Cover active v1 requirement, wrong owner, wrong session, superseded/final Look, invalid slot, current garment exclusion, laundry/reserved/unavailable, taboo color, season/weather, incomplete result, and high-urgency Catalog zero.

- [ ] **Step 2: Run options tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_replacement.py -k options
```

Expected: FAIL because the service and route do not exist.

- [ ] **Step 3: Implement exact models**

```python
class ReplacementOptionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    styling_session_id: str
    base_look_version_id: str
    base_version_index: int = Field(ge=1)
    slot: Slot

class ReplacementOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    garment_id: str
    name: str
    slot: Slot
    color: Color
    reason: str

class ReplacementOptionsResponse(BaseModel):
    options_receipt: str
    expires_at: datetime
    base_look_version_id: str
    slot: Slot
    options: tuple[ReplacementOption, ...]
    shopping_allowed: Literal[False] = False
```

- [ ] **Step 4: Generate options through full server validation**

Load only the active base version. For each same-slot owner garment, replace exactly one item in a temporary tuple, then call the existing whole-Look `_validate_items()` path. Return at most eight stable-ranked options. Never call Catalog.

- [ ] **Step 5: Bind the opaque receipt**

Store a server-side receipt record containing owner, session, active base ID/index, slot, exact option IDs, Scene request ID, created/expiry times, and a digest. Do not expose the internal candidate set or secret material through Trace.

- [ ] **Step 6: Run options and R1 regression tests**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_replacement.py -k options tests/test_r1_workflows.py tests/test_scene_gap_catalog.py
```

Expected: PASS; every option is owner-grounded, hard-valid, and shopping false.

- [ ] **Step 7: Commit options receipts**

```powershell
git add profagent/look_replacement.py profagent/look_models.py profagent/look_service.py profagent/app.py tests/test_s16_replacement.py tests/test_r1_workflows.py
git commit -m "feat: add grounded look replacement options"
```

### Task 3: Apply one replacement as immutable Look vN

**Files:**
- Modify: `profagent/look_replacement.py`
- Modify: `profagent/look_models.py`
- Modify: `profagent/look_service.py`
- Create: `profagent/image_jobs.py`
- Modify: `profagent/app.py`
- Test: `tests/test_s16_replacement.py`
- Test: `tests/test_s16_image_jobs.py`
- Test: `tests/test_r1_workflows.py`

**Interfaces:**
- Consumes: valid options receipt and active base Look.
- Produces: `SlotReplacementResponse` containing new Look, parent, comparison, and queued image-job reference.

- [ ] **Step 1: Write failing apply/CAS/idempotency tests**

Test invalid/cross-owner ID, ID not in receipt, expired receipt, wrong slot, stale base index, concurrent apply, repeated idempotency key, receipt replay with a different payload, finalized session, and old Look immutability.

- [ ] **Step 2: Run apply tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_replacement.py -k "apply or cas or idempotent"
```

Expected: FAIL because apply is absent.

- [ ] **Step 3: Add the exact request model**

```python
class SlotReplacementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    styling_session_id: str
    base_look_version_id: str
    base_version_index: int = Field(ge=1)
    slot: Slot
    replacement_garment_id: str
    options_receipt: str
    idempotency_key: str
```

Add `created_from="slot_replacement"` to the closed Look version source enum; do not accept a client-supplied item list.

Return this exact bound model:

```python
class ImageJobStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    job_id: str
    user_id: str
    styling_session_id: str
    look_version_id: str
    status: Literal["queued", "generating", "succeeded", "degraded", "failed"]
    image_url: str | None = None
    preview_id: str | None = None
    reason_code: str | None = None

class SlotReplacementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    look: LookVersion
    parent: LookVersion
    comparison: LookComparison
    image_job: ImageJobStatus
    trace_id: str
```

- [ ] **Step 4: Implement the CAS transaction**

Under the Look service lock: re-read the active base, check version index and receipt, replace one slot, rerun whole-Look validation, create Look vN, and save idempotency receipt. Other item IDs and all prior versions stay unchanged.

After commit, create one owner/session/Look-bound `queued` job record through the minimal in-process `ImageJobService`; actual provider execution is added in the next task.

- [ ] **Step 5: Produce a bound comparison**

Return `LookComparison` with exactly one removed and one added garment. A duplicate idempotency request returns the same Look version and does not enqueue a second image job.

- [ ] **Step 6: Verify the immutable chain**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_replacement.py tests/test_r1_workflows.py tests/test_preview_static_2d.py
```

Expected: PASS; v1 remains byte-equivalent, v2 is active and comparable, rollback still works.

- [ ] **Step 7: Commit single-slot replacement**

```powershell
git add profagent/look_replacement.py profagent/look_models.py profagent/look_service.py profagent/image_jobs.py profagent/app.py tests/test_s16_replacement.py tests/test_s16_image_jobs.py tests/test_r1_workflows.py
git commit -m "feat: create immutable looks from slot replacement"
```

### Task 4: Add asynchronous Look-bound image jobs

**Files:**
- Modify: `profagent/image_jobs.py`
- Modify: `profagent/preview.py`
- Modify: `profagent/app.py`
- Modify: `profagent/config.py`
- Modify: `profagent/look_replacement.py`
- Test: `tests/test_s16_image_jobs.py`
- Test: `tests/test_preview_static_2d.py`

**Interfaces:**
- Consumes: immutable Look version and existing `Preview2DService.generate()`.
- Produces: `ImageJobStatus`, retry, and owner-bound image responses.

- [ ] **Step 1: Write failing queue/status/retry/race tests**

Cover queued→generating→succeeded, provider failure, timeout, wrong model, cancellation, owner mismatch, stale late completion, retry idempotency, duplicate enqueue, and application shutdown cancellation.

- [ ] **Step 2: Run image-job tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_image_jobs.py
```

Expected: FAIL because asynchronous provider execution, retry, and terminal-state transitions are absent.

- [ ] **Step 3: Implement the repository-shaped in-process service**

```python
class ImageJobStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    job_id: str
    user_id: str
    styling_session_id: str
    look_version_id: str
    status: Literal["queued", "generating", "succeeded", "degraded", "failed"]
    image_url: str | None
    preview_id: str | None
    reason_code: str | None
    provider: PreviewProviderStatus | None

class ImageJobService:
    def enqueue(self, payload: Preview2DInput, *, idempotency_key: str) -> ImageJobStatus:
        return self._store.enqueue(payload, idempotency_key, self._run_job)

    def get_owned(self, job_id: str, user_id: str, session_id: str) -> ImageJobStatus:
        return self._store.get_owned(job_id, user_id, session_id)

    def retry(self, job_id: str, user_id: str, session_id: str) -> ImageJobStatus:
        payload, retry_key = self._store.retry_payload(job_id, user_id, session_id)
        return self.enqueue(payload, idempotency_key=retry_key)
```

Implement `_store` as the S16 in-process adapter with a lock, exact payload fingerprints, and immutable response copies; keep this boundary replaceable by a later persistent adapter.

Hide asyncio task objects behind the service; keep the public interface suitable for a later persistent adapter.

- [ ] **Step 4: Enqueue only after Look commit**

`/look/replacements/apply` commits Look vN first, then enqueues one job. If enqueue itself fails, return the valid Look with a failed/degraded job status; never roll back the Look.

- [ ] **Step 5: Prevent image lies**

Bind job output to exact owner/session/Look/preview/provider receipt and content hash. Failed jobs have no image URL. A previous version image or late older job may never overwrite the current version display.

- [ ] **Step 6: Run jobs and Preview regressions**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_image_jobs.py tests/test_preview_static_2d.py tests/test_s14_backend.py
```

Expected: PASS; image failure preserves Look and reports the real provider state.

- [ ] **Step 7: Commit image jobs**

```powershell
git add profagent/image_jobs.py profagent/preview.py profagent/app.py profagent/config.py profagent/look_replacement.py tests/test_s16_image_jobs.py tests/test_preview_static_2d.py
git commit -m "feat: generate replacement look images asynchronously"
```

### Task 5: Add dedicated slot localization and fallback

**Files:**
- Create: `profagent/look_slot_localizer.py`
- Modify: `profagent/vision.py`
- Modify: `profagent/image_jobs.py`
- Modify: `profagent/app.py`
- Test: `tests/test_s16_slot_localization.py`

**Interfaces:**
- Consumes: succeeded image bytes and server-owned closed slots from the bound Look.
- Produces: verified `[0,1]` slot regions or a content-free fallback status.

- [ ] **Step 1: Write failing localization tests**

Test exact regions, unknown slot, ID leakage, negative/out-of-bounds coordinates, zero area, overlap, duplicate slot, missing required slot, confidence `0.749`, wrong model, malformed JSON, timeout, and image hash mismatch.

- [ ] **Step 2: Run localization tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_slot_localization.py
```

Expected: FAIL because the localizer is absent.

- [ ] **Step 3: Implement exact region models**

```python
class LookSlotRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    region_id: str
    slot: Slot
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    confidence: float = Field(ge=0, le=1)

class LookSlotMap(BaseModel):
    status: Literal["verified", "fallback"]
    regions: tuple[LookSlotRegion, ...]
    fallback_slots: tuple[Slot, ...]
    reason_code: str | None
```

- [ ] **Step 4: Add a separate Vision operation**

Send image plus human-readable closed slots only. Do not send garment IDs, owner, Memory, scores, or raw provider receipts. Validate exact schema, image hash, region geometry, overlap policy, and confidence `>=0.75` server-side.

- [ ] **Step 5: Use fallback rather than partial guessing**

Any invalid or low-confidence result returns `status="fallback"`, empty regions, and server-owned `fallback_slots` derived from the Look. Trace records only controlled reason code and counts.

- [ ] **Step 6: Run localization and Vision regressions**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_slot_localization.py tests/test_references.py tests/test_r1_workflows.py
```

Expected: PASS; Vision failure never blocks Look or image availability.

- [ ] **Step 7: Commit slot localization**

```powershell
git add profagent/look_slot_localizer.py profagent/vision.py profagent/image_jobs.py profagent/app.py tests/test_s16_slot_localization.py
git commit -m "feat: localize replaceable look slots safely"
```

### Task 6: Build the long-press and keyboard runtime

**Files:**
- Create: `web/look_replacement_runtime.js`
- Create: `web/look_replacement_runtime_test.cjs`
- Create: `web/s16_replacement_browser_smoke.cjs`
- Modify: `web/recommendation_preview_runtime.js`
- Modify: `web/app.js:662-717,874-1022`
- Modify: `web/styles.css:1012-1050,1683-1760`
- Modify: `web/s16_frontend_static_test.cjs`

**Interfaces:**
- Consumes: active Look image-job status, slot map, options receipt, and replacement response.
- Produces: accessible pointer/keyboard slot selection without client-side Look mutation.

- [ ] **Step 1: Write failing pointer-state tests**

Use fake timers to test `449ms` no trigger, `450ms` trigger, `12px` allowed, `>12px` cancel, scroll cancel, pointer cancel, lost capture, mouse, touch, context-menu suppression only during armed state, keyboard Enter, and stale Look response rejection.

- [ ] **Step 2: Run runtime tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 node web/look_replacement_runtime_test.cjs
```

Expected: FAIL because the runtime is absent.

- [ ] **Step 3: Implement a DOM-independent state machine**

```javascript
function createLongPressController({ thresholdMs = 450, cancelDistancePx = 12, onTrigger }) {
  let armed = null;
  const cancel = () => { if (armed) clearTimeout(armed.timer); armed = null; };
  const start = ({ pointerId, x, y }) => {
    cancel();
    const timer = setTimeout(() => { const current = armed; armed = null; if (current) onTrigger(current); }, thresholdMs);
    armed = { pointerId, x, y, timer };
  };
  const move = ({ pointerId, x, y }) => {
    if (armed?.pointerId === pointerId && Math.hypot(x - armed.x, y - armed.y) > cancelDistancePx) cancel();
  };
  return Object.freeze({ start, move, cancel });
}
function hitTestSlot(regions, normalizedX, normalizedY) {
  return regions.find((r) => normalizedX >= r.x && normalizedX <= r.x + r.width && normalizedY >= r.y && normalizedY <= r.y + r.height)?.slot || null;
}
function normalizeReplacementResponse(payload, expected) {
  if (payload.look.user_id !== expected.userId || payload.look.styling_session_id !== expected.sessionId) throw new Error("替换 owner/session 不匹配");
  if (payload.parent.look_version_id !== expected.baseLookVersionId || payload.look.parent_version_id !== expected.baseLookVersionId) throw new Error("替换版本链不匹配");
  if (payload.image_job.look_version_id !== payload.look.look_version_id) throw new Error("图片任务未绑定新 Look");
  return Object.freeze(payload);
}
```

Do not change garment arrays, ranking, constraints, or image status in this runtime.

- [ ] **Step 4: Require Active Look before enabling interaction**

Recommendation images remain read-only. After the user selects a direction and Look v1 exists, render a focusable image wrapper. Verified regions may become focusable hotspots; fallback renders an explicit slot chooser opened by long press or Enter.

- [ ] **Step 5: Remove the client-only replacement path**

Delete the current `applyAlternative()`/permanent “替换入口” behavior only after backend options/apply, keyboard fallback, and browser smoke are green. The UI submits only the exact server contract.

- [ ] **Step 6: Run Node and browser tests**

```powershell
conda run --no-capture-output -n torch128 node web/look_replacement_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/recommendation_preview_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/s16_frontend_static_test.cjs
conda run --no-capture-output -n torch128 node web/s16_replacement_browser_smoke.cjs
```

Expected: PASS; no permanent replacement button; keyboard and fallback paths work.

- [ ] **Step 7: Commit the interaction runtime**

```powershell
git add web/look_replacement_runtime.js web/look_replacement_runtime_test.cjs web/s16_replacement_browser_smoke.cjs web/recommendation_preview_runtime.js web/app.js web/styles.css web/s16_frontend_static_test.cjs
git commit -m "feat: replace active look garments by long press"
```

### Task 7: Render Look vN and honest image-job states

**Files:**
- Modify: `web/app.js`
- Modify: `web/index.html`
- Modify: `web/look_replacement_runtime.js`
- Modify: `web/styles.css`
- Modify: `web/s16_replacement_browser_smoke.cjs`
- Test: `web/look_replacement_runtime_test.cjs`

**Interfaces:**
- Consumes: replacement response and polled job/slot-map endpoints.
- Produces: immediate version card, comparison, queued/generating/succeeded/failed states, and retry.

- [ ] **Step 1: Write failing race and display tests**

Cover new Look before image, old job late arrival, job owner/session/version mismatch, failed image with valid comparison, retry, rollback, and finalized session.

- [ ] **Step 2: Run tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 node web/look_replacement_runtime_test.cjs
```

Expected: FAIL for unimplemented display transitions.

- [ ] **Step 3: Render the business result first**

On apply success, render Look vN, the one-item comparison, version trail, and rollback target before polling the image job. Bind every poll to `job_id + look_version_id + user_id + styling_session_id`.

- [ ] **Step 4: Render honest image states**

Use “图片排队中 / 正在生成 / 搭配视觉参考已生成 / 图片生成失败，穿搭版本仍有效”. Never leave an old image under a new version or mark a failed job successful.

- [ ] **Step 5: Verify browser E2E**

```powershell
conda run --no-capture-output -n torch128 node web/look_replacement_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/s16_replacement_browser_smoke.cjs
```

Expected: PASS for success, localization fallback, provider failure, retry, stale response, and keyboard scenarios.

- [ ] **Step 6: Commit version/image rendering**

```powershell
git add web/app.js web/index.html web/look_replacement_runtime.js web/look_replacement_runtime_test.cjs web/styles.css web/s16_replacement_browser_smoke.cjs
git commit -m "feat: show replacement look versions and image jobs"
```

### Task 8: Run deterministic and real CPA evidence

**Files:**
- Create: `scripts/supervisor_s16_real_replacement_smoke.py`
- Create: `reports/demo/s16_real_replacement_image.json`
- Create: `reports/demo/s16_real_replacement_image.md`
- Test: `tests/test_s16_image_jobs.py`

**Interfaces:**
- Consumes: running S16 Demo, configured CPA Image/Vision operations, and one deterministic owner Look.
- Produces: real wall-clock/provenance evidence without changing fixed eval truth.

- [ ] **Step 1: Implement a non-destructive smoke script**

The script creates a temporary styling session, recommendation, selected Look v1, initial image, options receipt, one slot replacement, Look v2, and second image. Record only controlled IDs, hashes, model-request evidence, status, and latency; do not log prompts or private text.

- [ ] **Step 2: Run deterministic provider-mock tests first**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_replacement.py tests/test_s16_image_jobs.py tests/test_s16_slot_localization.py
```

Expected: PASS with external provider calls `0`.

- [ ] **Step 3: Start the feature Demo and run the real smoke**

```powershell
conda run --no-capture-output -n torch128 python -m profagent
conda run --no-capture-output -n torch128 python scripts/supervisor_s16_real_replacement_smoke.py --base-url http://127.0.0.1:8000
```

Expected: Look v1 and v2 are grounded and comparable; real image responses are either verified-success evidence or honest provider failure. Do not convert a real failure into a pass.

- [ ] **Step 4: Commit real evidence only if provenance is valid**

```powershell
git add scripts/supervisor_s16_real_replacement_smoke.py reports/demo/s16_real_replacement_image.json reports/demo/s16_real_replacement_image.md
git commit -m "test: record S16 real replacement image evidence"
```

### Task 9: Add final S16 atomic report, documentation, and joint review

**Files:**
- Create: `scripts/tester_s16_report.py`
- Create: `tests/test_s16_report.py`
- Create: `reports/eval/s16_personal_stylist_v1.json`
- Create: `reports/eval/s16_personal_stylist_v1.md`
- Modify: `README.md`
- Modify: `BUILD_LOG.md`
- Modify: `docs/PRD.md`
- Modify: `docs/API_CONTRACT.md`
- Modify: `docs/AC_MATRIX.md`

**Interfaces:**
- Consumes: S16A, S16B, S16C focused reports plus all R1/S15 gates.
- Produces: one-command final JSON+MD report and documentation synchronized to the Demo.

- [ ] **Step 1: Write the failing final report contract**

Require: full pytest; base and S16 validators; fixed R1 eval; S15 regression; Memory raw leakage `0`; 120/120 asset coverage; license violations `0`; owner/ID/hard violations `0`; high urgency Catalog `0`; pointer/keyboard/browser smoke; Look v1→vN; image/localization failure paths; text/image latency reported separately; findings P0/P1/P2.

- [ ] **Step 2: Run the report contract and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_report.py
```

Expected: FAIL because the final runner is absent.

- [ ] **Step 3: Implement an atomic one-command report**

Run every prerequisite with `subprocess.run(command, cwd=ROOT, check=True, text=True, capture_output=True)` and write final JSON+MD only after all gates pass. Include R1 thresholds without changing denominators or limits to hide failures.

- [ ] **Step 4: Run reviewer and tester gates**

Reviewer performs read-only PRD/safety review after each milestone and final adversarial pass. Tester runs all commands serially in `torch128`. Findings return to backend/frontend until `[P0]/[P1]/[P2]=0/0/0`.

- [ ] **Step 5: Run the final report**

```powershell
conda run --no-capture-output -n torch128 python scripts/tester_s16_report.py
```

Expected: exit `0`; JSON and MD say `PASS`; R1, S15, S16A/B/C and browser gates are green.

- [ ] **Step 6: Synchronize documentation after evidence**

Update README and PRD 0.3 to state what is actually online. Mark LangMem/Mem0/Graphiti, learning reranker, Look/Image Job restart persistence, real try-on, 3D/video, payment, generic crawling, and production authentication as not online.

- [ ] **Step 7: Run external Grok and Codex mutual review**

Before invoking `$call-grok`, ask the user for the model ID for that call as required by AGENTS.md. Treat Grok output as advisory; Codex verifies every claim against local files and reproducible tests and performs the final consolidation.

- [ ] **Step 8: Commit final S16 closure**

```powershell
git add scripts/tester_s16_report.py tests/test_s16_report.py reports/eval/s16_personal_stylist_v1.json reports/eval/s16_personal_stylist_v1.md README.md BUILD_LOG.md docs/PRD.md docs/API_CONTRACT.md docs/AC_MATRIX.md reports/review
git commit -m "feat: complete S16 personal stylist workflow"
```
