# Codex independent R1 review

Date: 2026-08-05
Reviewer: Codex supervisor
Requirement authority: `docs/PRD.md` v1.3
Scope: R1 Stylist MVP Demo subset in PRD 23.1

## Evidence reviewed

- Repository contracts: `AGENTS.md`, `docs/PRD.md`, `docs/API_CONTRACT.md`, `docs/AC_MATRIX.md`.
- Backend and frontend implementation under `profagent/` and `web/`.
- Fixed fixtures and evaluation input under `data/`.
- Tests under `tests/`, independent evaluation implementation in `profagent/eval.py`, and supervisor attack probe in `scripts/supervisor_s2_probe.py`.
- Generated reports `reports/eval/r1_demo_v1.json` and `reports/eval/r1_demo_v1.md`.
- Real startup, API calls, provider-health rendering, and an Edge headless render saved as `reports/demo/stylist-home-verified.png`.

## Reproducible gate evidence

All project commands ran serially in Conda environment `torch128`.

- `python -m compileall profagent tests scripts`: PASS.
- `python -m pytest -q`: `32 passed`.
- `python -m profagent.eval`: `overall: PASS`.
- `python scripts/validate.py`: PASS; users/garments/outfits/catalog/eval = `3/50/20/50/30`.
- `node --check web/app.js`: PASS.
- `python scripts/supervisor_s2_probe.py`: PASS; Vision closed-schema degradation, shopping gate, version/finalization, memory deletion, Trace redaction, and CPA privacy short-circuit all reproduced.
- Real Demo startup returned `/health.ready=true`, `capabilities.static_2d=false`, `video=false`, and `three_d=false`.
- Real high-urgency API replay returned `today/high`, `shopping_allowed=false`, UI CTA false, Catalog attempted false/call count 0, no products, and two grounded outfits.
- Real planned explicit top-gap replay returned `planned/fill_gap`, one Catalog call, and optional grounded Mock products `c002,c044,c023`.

Fixed evaluation metrics:

- Urgency mapping accuracy: `30/30 = 100%`.
- High-urgency shopping gate: `10/10 = 100%`.
- High-urgency Catalog invocations: `0/10`.
- Item hallucinations: `0/515` references.
- Observed hard-constraint violations: `0/423` checks.
- Slot completeness: `74/74 = 100%`.
- All listed assurances, including AC-01/03/04/05/06/07/10/11/13/14/16/17, SAFE-RAIN, SAFE-CPA-PRIVACY, OBS-05, validation alignment, query-only parsing, and Catalog oracle: PASS.

## Independent adversarial review and closures

The review did not accept earlier false-green reports. It rejected a first evaluator that reused the business `HardFilter`, trusted response self-reports, and incompletely mapped AC Given/Then clauses.

Issues found and closed before this review:

- No-deadline inventory/catalog language had been guessed as `planned/low`; it now fails closed to `unknown/high`, with actual and traced Catalog zero. Structured fixture horizons remain explicit inputs and cannot lower a more urgent text/time signal.
- Generic product/inventory words no longer create a shopping gap. Query-local slot-bound evidence is required. Explicit “do not open products/catalog” language overrides even a valid low/medium-urgency gap and is blocked at both orchestration and Catalog layers.
- Catalog evaluation now distinguishes “explicit gap must perform a real query” from “eligible inventory must be non-empty.” An independent fixture oracle finds e011/e012 safely empty because PRD-required occasion/ETA/budget constraints leave no legal products. It does not weaken SHOP-04 to manufacture a green result.
- Legacy `allowed_catalog` is correctly documented as a non-authoritative, non-exhaustive generator witness, not an exhaustive whitelist or a fully legal positive set.
- AC-07 feedback causality is tested on the same fused candidate universe: structured-similar shoes receive the fixed actual `0.01` penalty, safe shoes do not, the exact target is hard-excluded before recall, privacy IDs/signatures are not logged, and deletion restores behavior. Aggregate penalty uses the actual clamp-aware score delta.
- Look/Trace/Asset reads are owner-bound; Look versions are immutable and post-Final mutation is blocked. Reject decisions that create no new Look still remain in the validated decision ledger.
- Vision accepts only the exact model and closed evidence schema; mismatch, malformed fields, free text, partial visibility, or provider failure produces qualitative output with no fake numeric score.
- The UI originally displayed CPA as normal when text enhancement was disabled. The browser-discovered issue was fixed: normal status now requires availability, exact chat-model verification, and a resolved model; otherwise the page reports the relevant rule fallback without exposing endpoint, raw reason, or credentials.
- Two report-accuracy P2 findings were closed: the non-Call Catalog case list is the complete dynamic set of 25 cases, and memory penalty aggregation records actual clamp-aware deltas.

## Current finding count

- `[P0] 0`
- `[P1] 0`
- `[P2] 0`

This is the Codex-side independent conclusion before considering Grok output. Final release still requires the Grok independent pass, mutual challenge, local verification of every valid external finding, and a final gate after any resulting change.

## Honest limits

This is a local synthetic-data Demo, not a production service. Web uses synthetic user `u01`; state, memory, Look, assets, and Trace are process-memory only; there is no production authentication, persistent database, real commerce/payment, real scraping, second professional member, expert marketplace, community feed, static 2D generation, 3D/360, generated video, or real-time video. Image upload is single-frame outfit analysis, not image generation or virtual try-on.
