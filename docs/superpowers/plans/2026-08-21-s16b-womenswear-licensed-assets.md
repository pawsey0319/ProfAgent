# S16B Womenswear Wardrobe and Licensed Assets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the reusable 50-garment fixture baseline to a 120-garment womenswear V1 wardrobe and display a verified, provenance-labeled image for every garment.

**Architecture:** Preserve `fixtures_v1.0` byte-for-byte and load a deterministic 70-row S16 extension. A `LicensedAssetIngestor` accepts only user-owned images, allowlisted Openverse API results, formal partner APIs, or clearly labeled existing AI references; it validates license, network safety, image bytes, audience, and quality before a same-origin asset service exposes the image.

**Tech Stack:** Python 3.12, Pydantic v2, httpx, Pillow, JSONL/JSON manifests, FastAPI, CPA Vision adapter, vanilla JavaScript/CommonJS, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-s16-personal-stylist-memory-assets-replacement-design.md`

## Global Constraints

- Prefix all commands with `conda run --no-capture-output -n torch128`.
- The total is exactly 120 garments: `u01=72`, `u02=24`, `u03=24`; existing `g001`–`g050` and all `fixtures_v1.0` files remain reusable and unchanged.
- Category totals are top 24, bottom 24, dress 16, outer 18, shoes 18, bag 10, accessory 10.
- Accepted audience values are exactly `womenswear` and `unisex_womenswear_compatible`; do not infer user gender.
- The visible identity remains “私人 Stylist”; do not rename it “私人女装 Stylist”.
- Do not build a generic scraper or bypass site controls. Only use user-owned assets, machine-readable licensed APIs, formal partner APIs, and explicitly labeled AI references.
- Initial real-image license allowlist is CC0, Public Domain Mark, and CC BY. Reject NC, unclear licenses, prohibited derivatives when processing is required, and unverifiable provenance.
- Every displayed image is served from a same-origin owner-bound endpoint after hash, MIME, decode, license, audience, and status checks; never point `<img>` directly at an arbitrary external URL.
- CPA Vision may crop/classify but may not assign garment IDs, owner, license, or authorization.
- Preserve all R1 shopping, ID, hard-filter, scorecard, Memory, and 2D-only gates.

---

## File Map

- Create `scripts/generate_s16_wardrobe.py`: deterministic 70-row extension, preserving base fixture hashes.
- Create `scripts/validate_s16_wardrobe.py`: exact overlay/owner/category/schema validator while leaving the base validator unchanged.
- Create `data/fixtures/garments_s16_womenswear.jsonl`, `data/schemas/garment_s16.schema.json`, and `data/manifests/fixtures_s16_womenswear_v1.json`.
- Modify `profagent/models.py` and `profagent/repository.py`: load the extension and expose controlled audience.
- Create `profagent/licensed_assets.py`: source/license models, safe fetcher, Vision classification, processing, quarantine, and manifest writer.
- Create `scripts/ingest_licensed_wardrobe_assets.py`: Openverse API/user-owned ingestion CLI; no generic crawl.
- Create `data/sources/wardrobe_s16_sources.jsonl`, `data/assets/wardrobe_licensed_v1/`, and `data/manifests/wardrobe_assets_v2.json`.
- Modify `profagent/vision.py` and `profagent/wardrobe_assets.py`: controlled catalog classification and versioned v2 serving.
- Create `web/wardrobe_asset_runtime.js`; modify existing wardrobe rendering and keep v1 exact validation compatible.
- Create `tests/test_s16_wardrobe_fixture.py`, `tests/test_s16_licensed_assets.py`, `web/wardrobe_asset_runtime_test.cjs`, `web/s16_wardrobe_browser_smoke.cjs`, and `scripts/tester_s16b_report.py`.

### Task 1: Generate a deterministic 70-garment extension

**Files:**
- Create: `scripts/generate_s16_wardrobe.py`
- Create: `scripts/validate_s16_wardrobe.py`
- Create: `data/fixtures/garments_s16_womenswear.jsonl`
- Create: `data/schemas/garment_s16.schema.json`
- Create: `data/manifests/fixtures_s16_womenswear_v1.json`
- Modify: `profagent/models.py`
- Modify: `profagent/repository.py`
- Test: `tests/test_s16_wardrobe_fixture.py`

**Interfaces:**
- Consumes: immutable `fixtures_v1.0` data and current `Garment` model.
- Produces: repository-visible 120-garment wardrobe with stable IDs and controlled audience.

- [ ] **Step 1: Write failing preservation, count, owner, and category tests**

Use exact assertions:

```python
EXPECTED_OWNER_COUNTS = {"u01": 72, "u02": 24, "u03": 24}
EXPECTED_SLOT_COUNTS = {
    "top": 24, "bottom": 24, "dress": 16, "outer": 18,
    "shoes": 18, "bag": 10, "accessory": 10,
}

def test_s16_extension_preserves_base_fixture_hashes(base_hashes):
    run_generator()
    assert current_base_hashes() == base_hashes

def test_repository_loads_exact_s16_distribution(repository):
    assert repository.counts()["garments"] == 120
    assert owner_counts(repository) == EXPECTED_OWNER_COUNTS
    assert slot_counts(repository) == EXPECTED_SLOT_COUNTS
```

- [ ] **Step 2: Run the focused test and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_wardrobe_fixture.py
```

Expected: FAIL because the extension and loader do not exist.

- [ ] **Step 3: Implement the additive fixture contract**

Add to `Garment`:

```python
Audience = Literal["womenswear", "unisex_womenswear_compatible"]
audience: Audience = "womenswear"
```

Keep the base schema unchanged. The new extension schema requires `data_version="fixtures_s16_womenswear_v1"`, `source_id="fixtures_s16"`, `synthetic=true`, `audience`, and IDs `g051`–`g120`.

- [ ] **Step 4: Implement exact additions and owner allocation**

The 70 additions must be:

```python
ADDED_SLOT_COUNTS = {
    "top": 14, "bottom": 15, "dress": 11, "outer": 11,
    "shoes": 10, "bag": 4, "accessory": 5,
}
ADDED_OWNER_COUNTS = {"u01": 44, "u02": 12, "u03": 14}
```

The final per-owner matrix is exact:

```python
EXPECTED_OWNER_SLOT_COUNTS = {
    "u01": {"top": 14, "bottom": 14, "dress": 10, "outer": 11, "shoes": 11, "bag": 6, "accessory": 6},
    "u02": {"top": 5, "bottom": 5, "dress": 3, "outer": 4, "shoes": 4, "bag": 2, "accessory": 1},
    "u03": {"top": 5, "bottom": 5, "dress": 3, "outer": 3, "shoes": 3, "bag": 2, "accessory": 3},
}
```

Use deterministic, human-readable women’s garment names and metadata covering commute, meeting, interview, date, daily, travel, athleisure, and party. Do not modify `scripts/generate.py` or the existing `scripts/validate.py` baseline contract.

- [ ] **Step 5: Load extension fail-closed**

`FixtureRepository` reads base first and the S16 extension second. Reject duplicate garment IDs, unknown owner IDs, invalid audience, or schema errors before exposing any partial extension.

- [ ] **Step 6: Run generator twice and verify determinism**

```powershell
conda run --no-capture-output -n torch128 python scripts/generate_s16_wardrobe.py
$first = (Get-FileHash data/fixtures/garments_s16_womenswear.jsonl -Algorithm SHA256).Hash
conda run --no-capture-output -n torch128 python scripts/generate_s16_wardrobe.py
$second = (Get-FileHash data/fixtures/garments_s16_womenswear.jsonl -Algorithm SHA256).Hash
if ($first -ne $second) { throw "S16 wardrobe generation is not deterministic" }
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_wardrobe_fixture.py
conda run --no-capture-output -n torch128 python scripts/validate.py
conda run --no-capture-output -n torch128 python scripts/validate_s16_wardrobe.py
```

Expected: identical hashes; extension tests pass; base validator still reports 50 base garments.

- [ ] **Step 7: Commit the fixture extension**

```powershell
git add scripts/generate_s16_wardrobe.py scripts/validate_s16_wardrobe.py data/fixtures/garments_s16_womenswear.jsonl data/schemas/garment_s16.schema.json data/manifests/fixtures_s16_womenswear_v1.json profagent/models.py profagent/repository.py tests/test_s16_wardrobe_fixture.py
git commit -m "feat: add deterministic womenswear fixture extension"
```

### Task 2: Implement license and remote-image safety contracts

**Files:**
- Create: `profagent/licensed_assets.py`
- Modify: `profagent/config.py`
- Test: `tests/test_s16_licensed_assets.py`

**Interfaces:**
- Consumes: source metadata from a licensed API or user-owned upload.
- Produces: `LicensedSourceReceipt`, `ProcessedWardrobeAsset`, and quarantine results.

- [ ] **Step 1: Write failing license, SSRF, MIME, size, decode, and attribution tests**

Test CC0/PDM/CC-BY acceptance and NC/unclear/ND-for-transformation rejection. Test loopback, RFC1918, link-local, file URLs, redirect-to-private, HTML-as-image, decompression bombs, missing author for CC BY, and hash mismatch.

- [ ] **Step 2: Run tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_licensed_assets.py -k "license or ssrf or mime or attribution"
```

Expected: FAIL because `licensed_assets.py` does not exist.

- [ ] **Step 3: Implement exact source and asset models**

```python
LicenseCode = Literal["CC0", "PDM", "CC-BY-2.0", "CC-BY-3.0", "CC-BY-4.0", "user-owned", "ai-generated"]
SourceKind = Literal["licensed_photo", "user_owned_photo", "ai_generated_reference"]
AssetStatus = Literal["ready", "missing", "failed", "quarantined", "takedown"]

class LicensedSourceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["openverse", "user_upload", "partner_api", "cpa_generated"]
    source_url: AnyHttpUrl | None
    creator: str | None
    license_code: LicenseCode
    license_url: AnyHttpUrl | None
    attribution: str
    imported_at: datetime

class PublicLicenseReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: LicenseCode
    name: str
    url: AnyHttpUrl | None
    author: str | None
    attribution: str

class ProcessedWardrobeAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    garment_id: str
    user_id: str
    source_kind: SourceKind
    status: AssetStatus
    original_sha256: str
    processed_sha256: str | None
    relative_path: str | None
    license: PublicLicenseReceipt

class SafeImageFetcher:
    async def fetch(self, url: str) -> tuple[bytes, str, str]:
        response = await self._client.get(url, follow_redirects=False)
        return self._validate_response_chain(url, response)
```

Require complete author/attribution for CC BY; source URLs are metadata only and never browser image URLs.

- [ ] **Step 4: Implement SafeImageFetcher**

Allow HTTPS only, resolve every redirect hop, reject private/reserved IPs, cap response bytes, require an approved image MIME, decode with Pillow, enforce dimensions/pixel count, normalize EXIF orientation, strip metadata, and compute original/processed SHA-256.

- [ ] **Step 5: Implement quarantine and takedown state**

Every failure returns a content-free controlled reason code. `takedown` immediately prevents serving bytes while retaining garment metadata and provenance audit fields.

- [ ] **Step 6: Run safety tests**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_licensed_assets.py -k "license or ssrf or mime or attribution or takedown"
```

Expected: PASS; no failing payload is written under the ready asset directory.

- [ ] **Step 7: Commit safety contracts**

```powershell
git add profagent/licensed_assets.py profagent/config.py tests/test_s16_licensed_assets.py
git commit -m "feat: validate licensed wardrobe assets"
```

### Task 3: Add CPA Vision catalog extraction without authority

**Files:**
- Modify: `profagent/vision.py`
- Modify: `profagent/licensed_assets.py`
- Test: `tests/test_s16_licensed_assets.py`
- Test: `tests/test_references.py`

**Interfaces:**
- Consumes: decoded licensed/user-owned image plus the target garment’s server-owned expected slot/audience.
- Produces: `CatalogAssetAssessment` used only for crop/classification/quarantine.

- [ ] **Step 1: Write failing Vision contract tests**

Test wrong model, additional keys, unknown audience, wrong slot, identifiable person, low confidence, malformed region, and timeout. Assert Vision cannot return or change `garment_id`, `user_id`, source URL, or license.

- [ ] **Step 2: Run Vision tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_licensed_assets.py -k vision
```

Expected: FAIL because the catalog assessment operation is absent.

- [ ] **Step 3: Add a dedicated strict assessment method**

```python
class CatalogAssetAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    slot: Slot
    audience: Audience
    contains_identifiable_person: bool
    object_region: tuple[float, float, float, float]
    confidence_band: Literal["high", "medium", "low"]
    quality_issues: tuple[str, ...] = ()

async def inspect_catalog_asset(
    self,
    *,
    image_bytes: bytes,
    mime_type: str,
    allowed_slot: Slot,
) -> tuple[CatalogAssetAssessment, dict[str, object]]:
    raw, trace = await self._request_json(
        operation="catalog_asset_assessment",
        image_bytes=image_bytes,
        mime_type=mime_type,
        allowed_values={"slot": (allowed_slot,), "audience": ("womenswear", "unisex_womenswear_compatible")},
    )
    return CatalogAssetAssessment.model_validate(raw), trace
```

Factor the existing verified model/timeout/duplicate-key request path into `_request_json()` and use it for both the old inspection operation and this new catalog operation.

Do not include garment IDs, owner IDs, or license claims in the provider prompt.

- [ ] **Step 4: Enforce server-side acceptance**

Accept only expected slot, allowed audience, no identifiable person, high/medium confidence, valid normalized crop, and no prohibited quality issue. Crop/process locally; Vision does not write the manifest.

- [ ] **Step 5: Run Vision and reference tests**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_licensed_assets.py tests/test_references.py
```

Expected: PASS; timeout quarantines the asset and leaves garment truth unchanged.

- [ ] **Step 6: Commit Vision extraction**

```powershell
git add profagent/vision.py profagent/licensed_assets.py tests/test_s16_licensed_assets.py tests/test_references.py
git commit -m "feat: extract licensed garment images safely"
```

### Task 4: Build the licensed API ingestion CLI and 120-item manifest

**Files:**
- Create: `scripts/ingest_licensed_wardrobe_assets.py`
- Create: `data/sources/wardrobe_s16_sources.jsonl`
- Create: `data/assets/wardrobe_licensed_v1/`
- Create: `data/manifests/wardrobe_assets_v2.json`
- Modify: `profagent/licensed_assets.py`
- Test: `tests/test_s16_licensed_assets.py`

**Interfaces:**
- Consumes: 70 extension garments, Openverse API or explicit user-owned paths, and CPA Vision assessment.
- Produces: 70 processed licensed assets plus one v2 manifest composing them with the existing 50 AI references.

- [ ] **Step 1: Write failing CLI dry-run, idempotency, and manifest tests**

Assert that mocked Openverse responses must contain machine-readable license, creator, license URL, source URL, and image URL; reruns reuse exact content hashes; source changes create a new receipt rather than overwriting history.

- [ ] **Step 2: Run CLI tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_licensed_assets.py -k "cli or manifest or idempotent"
```

Expected: FAIL because the CLI is absent.

- [ ] **Step 3: Implement explicit input modes**

```text
--provider openverse --license CC0,PDM,CC-BY --garment-file data/fixtures/garments_s16_womenswear.jsonl
--provider user-owned --source-map data/sources/user_owned_s16_sources.jsonl
--dry-run
--resume
```

The Openverse mode calls the official API only; it does not fetch or parse arbitrary web pages. It records the exact API response fields used for provenance.

- [ ] **Step 4: Implement selection and retry rules**

Search with the garment’s controlled English/Chinese description, accept only object/flat-lay images with no identifiable person, and quarantine mismatches. Continue to the next licensed candidate until the garment has one ready image or the bounded candidate budget is exhausted.

- [ ] **Step 5: Run the real licensed ingestion for `g051`–`g120`**

```powershell
conda run --no-capture-output -n torch128 python scripts/ingest_licensed_wardrobe_assets.py --provider openverse --license CC0,PDM,CC-BY --garment-file data/fixtures/garments_s16_womenswear.jsonl --resume
```

If CPA Vision or the API is unavailable, stop and report the exact remaining garment IDs; do not substitute unlicensed images or mark quarantined entries ready.

- [ ] **Step 6: Verify 120/120 ready and inspect contact sheets**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_licensed_assets.py -k "real_manifest or coverage"
conda run --no-capture-output -n torch128 python scripts/validate_s16_wardrobe.py
```

Expected: 50 AI-reference + 70 licensed/user-owned = 120 ready; owner/category/audience/license counts match; every image decodes and hashes match. A reviewer visually inspects generated contact sheets for wrong category, people, logos, text overlays, and male-only cuts.

- [ ] **Step 7: Commit assets and provenance**

```powershell
git add scripts/ingest_licensed_wardrobe_assets.py data/sources/wardrobe_s16_sources.jsonl data/assets/wardrobe_licensed_v1 data/manifests/wardrobe_assets_v2.json profagent/licensed_assets.py tests/test_s16_licensed_assets.py scripts/validate_s16_wardrobe.py
git commit -m "data: add licensed womenswear wardrobe assets"
```

### Task 5: Serve versioned v2 wardrobe assets

**Files:**
- Modify: `profagent/wardrobe_assets.py`
- Modify: `profagent/app.py`
- Modify: `profagent/config.py`
- Test: `tests/test_s16_licensed_assets.py`
- Test: `tests/test_s12_image_memory.py`

**Interfaces:**
- Consumes: v1 AI manifest and v2 composed manifest.
- Produces: owner-bound same-origin asset list/image responses with exact source labels.

- [ ] **Step 1: Write failing service/API tests**

Test source kinds, display labels, CC BY attribution, audience, owner isolation, v1 compatibility, hash binding, takedown/quarantine, unknown fields, wrong URLs, and no external redirect.

- [ ] **Step 2: Run service tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_licensed_assets.py -k "service or api" tests/test_s12_image_memory.py
```

Expected: FAIL because v2 is unsupported.

- [ ] **Step 3: Implement v2 models**

Each public asset exposes:

```python
source_kind: Literal["licensed_photo", "user_owned_photo", "ai_generated_reference"]
display_label: Literal["授权照片", "用户自有照片", "AI 生成参考"]
audience: Audience
license: PublicLicenseReceipt
image_url: str  # same-origin only
```

Keep original source URLs informational and HTTPS-validated; never use them as served image locations.

- [ ] **Step 4: Compose v1 and v2 without weakening v1 checks**

Retain exact S12R manifest validation for `g001`–`g050`; apply v2 license checks to `g051`–`g120`; reject the whole v2 manifest on duplicate IDs or owner mismatch.

- [ ] **Step 5: Run API and regression tests**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16_licensed_assets.py tests/test_s12_image_memory.py tests/test_references.py
```

Expected: PASS; cross-owner image reads return not found; quarantined/takedown bytes are never served.

- [ ] **Step 6: Commit v2 serving**

```powershell
git add profagent/wardrobe_assets.py profagent/app.py profagent/config.py tests/test_s16_licensed_assets.py tests/test_s12_image_memory.py
git commit -m "feat: serve licensed wardrobe asset manifest"
```

### Task 6: Upgrade the folded wardrobe UI

**Files:**
- Create: `web/wardrobe_asset_runtime.js`
- Create: `web/wardrobe_asset_runtime_test.cjs`
- Create: `web/s16_wardrobe_browser_smoke.cjs`
- Modify: `web/image_asset_runtime.js`
- Modify: `web/image_asset_runtime_test.cjs`
- Modify: `web/app.js:239-423`
- Modify: `web/index.html:347-382`
- Modify: `web/styles.css:1179-1405,2433-2670`
- Modify: `web/s14_frontend_static_test.cjs`
- Modify: `web/s16_frontend_static_test.cjs`

**Interfaces:**
- Consumes: owner-bound v2 asset list and same-origin image URLs.
- Produces: category-only folded state and source-labeled expanded cards.

- [ ] **Step 1: Write failing runtime/static/browser tests**

Assert zero garment cards before expansion, exact per-category count, lazy card creation, exact source labels, safe attribution link, same-origin image URL, no male-only audience, no direct external image URL, and metadata fallback for non-ready states.

- [ ] **Step 2: Run Node tests and verify RED**

```powershell
conda run --no-capture-output -n torch128 node web/wardrobe_asset_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/s16_frontend_static_test.cjs
```

Expected: FAIL because v2 normalization is absent.

- [ ] **Step 3: Implement strict v2 normalization**

```javascript
function normalizeWardrobeAssetV2(raw, garment, expectedVersion) {
  const allowedSources = new Set(["licensed_photo", "user_owned_photo", "ai_generated_reference"]);
  const allowedAudiences = new Set(["womenswear", "unisex_womenswear_compatible"]);
  if (raw.asset_version !== expectedVersion || raw.garment_id !== garment.garment_id) throw new Error("资产绑定不匹配");
  if (!allowedSources.has(raw.source_kind) || !allowedAudiences.has(raw.audience)) throw new Error("资产来源或受众无效");
  if (raw.status === "ready" && !String(raw.image_url || "").startsWith(`/wardrobe/${encodeURIComponent(garment.garment_id)}/`)) throw new Error("图片必须同源且绑定衣物");
  return Object.freeze({ ...raw, license: Object.freeze({ ...raw.license }) });
}
function isDisplayable(asset) { return asset.status === "ready" && asset.image_url.startsWith("/wardrobe/"); }
```

Keep the existing v1 validator exact; do not broaden its accepted provenance fields.

- [ ] **Step 4: Render folded categories and labels**

The closed `<details>` element contains only summary name/count. Create cards and `<img>` nodes only on the first `toggle` to open. Render the exact source label and server-generated attribution; use `rel="noopener noreferrer"` for approved informational links.

- [ ] **Step 5: Run Node and real-browser checks**

```powershell
conda run --no-capture-output -n torch128 node web/wardrobe_asset_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/image_asset_runtime_test.cjs
conda run --no-capture-output -n torch128 node web/s14_frontend_static_test.cjs
conda run --no-capture-output -n torch128 node web/s16_frontend_static_test.cjs
conda run --no-capture-output -n torch128 node web/s16_wardrobe_browser_smoke.cjs
```

Expected: PASS; browser smoke reports 120 server garments, owner-visible `u01=72`, zero cards before expansion, and all expanded images have nonzero natural dimensions.

- [ ] **Step 6: Commit wardrobe UI**

```powershell
git add web/wardrobe_asset_runtime.js web/wardrobe_asset_runtime_test.cjs web/s16_wardrobe_browser_smoke.cjs web/image_asset_runtime.js web/image_asset_runtime_test.cjs web/app.js web/index.html web/styles.css web/s14_frontend_static_test.cjs web/s16_frontend_static_test.cjs
git commit -m "feat: display licensed womenswear wardrobe assets"
```

### Task 7: Add the S16B atomic report and reviewer gate

**Files:**
- Modify: `scripts/validate_s16_wardrobe.py`
- Create: `scripts/tester_s16b_report.py`
- Create: `tests/test_s16b_report.py`
- Create: `reports/eval/s16b_womenswear_assets_v1.json`
- Create: `reports/eval/s16b_womenswear_assets_v1.md`
- Modify: `BUILD_LOG.md`

**Interfaces:**
- Consumes: extension fixtures, asset manifests, image files, API/UI tests, and base R1 validation.
- Produces: deterministic coverage/provenance report and a closed S16B milestone.

- [ ] **Step 1: Write the failing report contract**

Require exact total/owner/category/audience/source/license/status/hash/decode counts, base hash preservation, quarantine/takedown leakage `0`, cross-owner leakage `0`, browser lazy-load result, full pytest, and fixed R1 metrics.

- [ ] **Step 2: Run and verify RED**

```powershell
conda run --no-capture-output -n torch128 python -m pytest -q tests/test_s16b_report.py
```

Expected: FAIL because the report is absent.

- [ ] **Step 3: Implement validators and atomic report**

Write JSON and MD only after every subprocess and every 120-item invariant succeeds. Separate licensed, user-owned, and AI-reference counts; never describe AI images as photographs.

- [ ] **Step 4: Run reviewer read-only checks**

Reviewer checks licenses, attribution, no generic crawler, no identifiable people, women’s audience, owner/hash binding, same-origin serving, v1 provenance preservation, lazy loading, and all R1 P0 rules. Any finding returns to backend/frontend.

- [ ] **Step 5: Run the atomic report**

```powershell
conda run --no-capture-output -n torch128 python scripts/tester_s16b_report.py
```

Expected: exit `0`; 120/120 ready, license violations `0`, base fixture mutation `0`, reviewer findings `0/0/0`.

- [ ] **Step 6: Commit reports and close only S16B**

```powershell
git add scripts/validate_s16_wardrobe.py scripts/tester_s16b_report.py tests/test_s16b_report.py reports/eval/s16b_womenswear_assets_v1.json reports/eval/s16b_womenswear_assets_v1.md BUILD_LOG.md
git commit -m "test: close S16B womenswear asset milestone"
```
