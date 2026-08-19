# S13 Grok external review packet

Review target: ProfAgent R1 Stylist Demo, S13 text-model migration and wardrobe catalog-image display fix.

Please return a concise adversarial report with only actionable findings, classified `[P0]`, `[P1]`, or `[P2]`. For every finding, state the violated invariant and the exact evidence needed to confirm or close it. If evidence below closes a risk, say so rather than inventing a defect. Maximum 900 words.

## Frozen safety/product invariants

1. Garment/product IDs must come from the current owner/tool whitelist; hallucination target is zero.
2. `now/today/unknown -> high`; high urgency means `shopping_allowed=false`, no shopping CTA, Catalog call count zero.
3. Hard constraints filter before ranking and cannot be restored by fallback or alternatives.
4. Scorecards assess outfit × goal only; never appearance, body, age, sexual attractiveness, diagnosis, or medication.
5. Each adjustment round has at most two actions; rejected advice is not repeated through paraphrase.
6. Look versions are immutable and comparable; Memory uses propose-confirm-commit, sensitive data defaults to no-write.
7. Text/Image/Dense/Catalog/Vision failures degrade honestly; 3D/360/video/real payment are out of scope.

## S13 implementation contract

- Text logical model: exact `grok4.6`.
- CPA transport: exact `grok-4.6-high`.
- Successful reported model allowlist: exact `{grok-4.6-high, grok-4.6-build}`.
- Old 4.5, prefixes, other models, unsafe/invalid output: fail closed; no fuzzy match or automatic model switch.
- Each new non-idempotent dialogue turn attempts CPA at most once. Server waits at most 120 seconds; browser at most 125 seconds. The local state machine, validators, tool permissions, shopping gate, IDs, Memory, Look, and safety remain authoritative.
- Image provider remains separate and exact `grok-imagine-image-quality`; text transport never enters image requests.
- Catalog assets require owner, asset version, content SHA-256, PNG decode and embedded provenance. Failure shows metadata/swatch rather than claiming an image.
- S13 wardrobe fix: image is eager and layout-eligible while pending; card/status nodes are attached before `src`; load success requires `naturalWidth>0`; cached-complete, cached-broken, error, cancel and stale-card events are covered. Source URL still comes only from normalized owner-bound metadata.

## Independent evidence

- Full pytest: `252 passed`; S13 targeted: `214 passed`.
- A previous Windows full run had one cancellation in a success test using a 0.5-second injected budget with an 80ms mock. Production remained 120 seconds and an independent 50ms timeout/cancel test stayed intact. The success-test budget was raised to 2 seconds; it passed five targeted repeats and two consecutive full suites (`252 passed` each).
- Fixed eval: urgency 30/30, high gate 10/10, high Catalog 0/10, hallucination 0/515, hard violations 0/423, slots 74/74.
- Node: 7 syntax checks and 3 runtime/static suites pass.
- Isolated HTTP/browser smoke: owner-bound catalog PNG actually renders with `naturalWidth=1024`; wrong owner/version/hash/provenance fail closed; 2D-only and high-urgency Catalog zero remain.
- Real current port 8000 text smoke: “你好” completed in about 3.456 seconds with `status=ok`, `generation_source=cpa`, requested `grok4.6`, transport `grok-4.6-high`, resolved `grok-4.6-build`, verified true, degraded false.
- Current docs were synchronized to PRD v1.14 / S13 / 252+214 evidence. Historical S6/S10 records still name 4.5 explicitly as history.

## Review questions

1. Does S13 introduce a safety, source-truthfulness, owner-isolation, cache/race, or model-confusion regression?
2. Is the image lifecycle fix sufficient without bypassing provenance checks?
3. Does the test-flake hardening preserve meaningful timeout/cancellation coverage?
4. Do the stated results justify closing S13 as an MVP Demo increment, while still not claiming production readiness?
