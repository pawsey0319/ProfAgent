# ProfAgent R1 mutual challenge packet

You are performing the second Grok pass. Challenge the Codex local-evidence mapping below. The first Grok pass returned `[P0] 0 / [P1] 0 / [P2] 0` but listed ten evidence-limited areas. For each area, decide whether the cited local evidence closes it for this explicitly limited R1 Demo subset. Do not turn a documented production limitation into a Demo defect unless PRD 23.1 requires it. Conversely, if the evidence does not actually test the claimed behavior, issue a concrete P0/P1/P2 finding with a reproducible failure condition.

## Gate state

- `compileall`: PASS.
- `pytest`: 32 passed.
- fixed evaluation: overall PASS.
- data/schema validation: PASS, `3/50/20/50/30`.
- JavaScript syntax: PASS.
- supervisor attack probe: PASS.
- real API/Web/Edge smoke: PASS.
- metrics: urgency 30/30; high gate 10/10; high Catalog 0/10; hallucination 0/515; observed hard violations 0/423; slots 74/74.

## Codex mapping to the first Grok pass

### 1. Time/intent, client relax, no-shopping gap, CTA, second Catalog layer

- `tests/test_r1_workflows.py` tampers a stored high-urgency Scene with `shopping_allowed=true`, CTA true, and relaxed urgency, then calls `/recommend`; the server resolves the authoritative request and returns high/no-shop with Trace Catalog zero.
- `tests/test_scene_horizon.py` covers query-only no deadline and structured planned plus more-urgent text/event-time monotonic merging. It spies the upper Catalog and directly calls the Catalog layer, which returns zero with an explicit block reason.
- `tests/test_scene_gap_catalog.py` covers planned and soon explicit slot gaps combined with “不要打开商品/目录”; both API/response CTAs are false, upper invocation and Trace are zero, and direct Catalog forced calls return zero.
- `profagent/eval.py` includes AC-01, AC-03, AC-11, a 21-case query-only parser assurance, a 3-case no-deadline audit, and a complete 25-case non-Call Catalog set. All participate in overall PASS.
- `tests/test_r1_workflows.py` statically checks the frontend all-path urgency AND guard; real API replay confirmed `today/high`, CTA false, Catalog attempted false/call zero.

### 2. Cross-owner/session/request and illegal IDs

- AC-10 injects an illegal `g999` and a cross-owner garment into final validation and requires rejection before public output.
- `tests/test_r1_workflows.py` covers cross-owner Look, asset, memory, and Trace reads/deletes with fail-closed 404 responses; it also checks request replay uses the server-owned Scene.
- Recommendation garment/product references are checked against the current owner and actual Catalog return; 515 public references produced zero hallucinations.
- The ordinary frontend validates owner/session/version and hides internal IDs; Debug alone exposes controlled version/Trace identifiers.

### 3. Hard-filter resurrection and safe empty inventory

- AC-04 verifies confirmed no-high-heel memory filters known high heels and heel-unknown shoes before Rule/BM25/Dense/RRF/output/alternatives; deletion restores eligibility.
- AC-05 verifies exact status filters e019/e020/e021 are absent from every ranker/output.
- AC-06 injects a sparse wardrobe where a taboo item would create another complete outfit if restored, then verifies only one legal direction and Catalog zero.
- SAFE-RAIN verifies every unverified outer is absent before all rankers, and Catalog fails closed for outer/top/bag/empty gaps.
- OBS-05 forces Dense/Catalog failures and observes a safe rule/wardrobe path.
- The independent Catalog oracle uses query + fixture Catalog + fixture user only, not the business service; it computes e009 `{c033}`, e010 `{c008,c029}`, e011/e012 empty, and e013 `{c002,c023,c044}`. Explicit gaps always cause a real call; empty eligible inventory must return no suggestion and Trace must cover every ineligible product reason.

### 4. Six dimensions and prohibited scoring language

- AC-16 exercises both qualitative and numeric cards and recursively scans every user-visible scorecard/adjustment field for appearance, body, weight, age, sexual appeal, health, and human-worth terms.
- Numeric cards require exactly the six closed dimensions and server-owned evidence. AC-17 creates an actual rule scorecard of exactly 78 rather than injecting a score.
- Vision free text, unknown issue codes, slot mismatch, extra fields, model mismatch, partial evidence, malformed payload, and HTTP failure all yield qualitative/no numeric output.

### 5. Adjustment limit and rejected-advice dedup

- AC-14 creates the canonical `trouser_cuff_single` proposal, verifies one or two priority adjustments only, rejects it without a fake Look version, verifies it is absent from later priority advice, rejects replay, and verifies a user-created next Look carries the decision history.
- The frontend decision ledger accepts only closed decision/canonical values bound to an owned source version; ordinary UI renders Chinese labels and no reason/internal decision IDs.

### 6. Look immutability, rollback, Final, ownership, races

- `test_look_asset_version_rollback_final_and_owner_contracts` creates multiple versions, verifies snapshots and parent/index chains, performs rollback as a new v4, compares versions, rejects cross-owner reads/deletes, finalizes with satisfaction 1, and verifies `/adjust`, `/scorecard`, and `/look` all return 409 after Final while history remains readable.
- AC-13 repeats the v1→accepted v2→rescore→Final path with Vision and verifies v1 immutability and parent comparison. AC-17 verifies satisfaction can finalize an exact score 78.
- `profagent/look_service.py` serializes version creation, adjustments, score commit, and Final with a lock, rechecks Final after Vision, and rechecks the active version inside the Final commit lock. This is a process-memory Demo, not a production distributed store; production durability/auth are explicitly excluded.

### 7. Memory lifecycle, bypass, TTL, privacy

- The workflow tests require propose with no record, reject a cross-owner confirm, then confirm to commit; direct record creation has no public endpoint.
- Unknown free text, explicit sensitive type, detected contact/medical/ethnicity content, and sensitive edits store no content, set commit blocked, and cannot produce a record.
- Confirmed feedback changes only a relevant later ranking, an unrelated scene is unaffected, deletion empties the record/proposal/index and restores exact ranking. TTL expiry removes both public state and behavioral effect.
- Trace scans prove sensitive strings, target item IDs, proposal contents, credentials, base64, and raw private values are absent.

### 8. Image/Vision attacks and exact model

- Asset tests construct and reject animated PNG and animated WebP, reject video MIME/angle, invalid bytes, oversized dimensions/pixels/files, and require a full Pillow decode for single-frame PNG/JPEG/WebP.
- The strict Vision matrix covers exact model success and mismatch `grok-4.5-build`, malformed JSON, free prose, partial visibility, slot mismatch, extra field, HTTP failure, and provider failure. Every unsafe/mismatched case yields qualitative/no dimensions/total/adjustments.
- Images require explicit consent and analysis purpose, live only in process memory, are absent from ordinary Trace, and have an owner-bound DELETE path.
- A real CPA request used exact transport `grok-4.5`, but the proxy returned `grok-4.5-build`; the project correctly recorded unverified/null resolved model and used the rule fallback rather than claiming Grok success.

### 9. UI/API scope, Debug leakage, commands

- `tests/test_r1_workflows.py` checks no video element and no id/href/src/action containing a usable 3d/360/video path. `/health` reports static_2d/video/three_d false.
- Real Edge rendering shows R2+ capabilities only as non-interactive “未上线” boundary labels. No static 2D generation endpoint exists.
- Ordinary timeline/compare renders names, counts, Chinese controlled decisions, and no asset/base64/reason/decision/adjustment/Trace IDs; Debug explicitly shows controlled IDs.
- One command starts FastAPI + SPA and one command atomically writes clean versioned JSON + Markdown reports. The report contains no raw queries, images/base64, or credentials.
- Browser smoke found and fixed one real UI honesty defect: disabled CPA was previously shown as normal. Normal now requires enabled, available, exact chat-model verification, and a resolved model; the verified screenshot shows the honest rule-core fallback.

### 10. Evaluation independence and no false-green metric

- An earlier false-green evaluator was discarded because it reused business HardFilter and trusted self-reported slot flags.
- Current main 30 rows transparently supply structured horizon/intent and call the metric “horizon-to-urgency mapping,” not query NLP accuracy. Query-only parsing and no-deadline cases are independent fail-closed assurances.
- Slot and hard-constraint checks are pure evaluation helpers. Response-reported validation is separately compared with the independent result, and tampered `true` validation has a unit test.
- Catalog call requirement and inventory eligibility are independent query/fixture helpers; actual invocation spies, actual tool-return IDs, Trace calls, independent product constraints, and full ineligible Trace coverage are all required.
- `overall` is an AND across threshold metrics and every assurance. Empty/failed assurance steps fail closed. JSON and Markdown contract tests verify the report shape and output directory contains only the two expected versioned reports.

## Documented limits that are not claimed as R1 proof

- LLM-enhanced live language quality is not claimed: exact CPA model verification failed and the rule fallback is the tested core.
- Distributed concurrency, persistent storage, production authentication, full DLP, real commerce, real user images, R2 static 2D generation, 3D/360/video, second member, expert market, and community are not implemented and are explicitly marked unlaunched.

## Required response

For each numbered area, state `closed for R1 Demo`, `still evidence-limited but not a defect`, or issue a concrete `[P0]/[P1]/[P2]`. Finish with a numeric finding count. Do not repeat generic production recommendations unless they reveal an actual mismatch with the stated R1 Demo subset.
