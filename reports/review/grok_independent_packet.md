# ProfAgent R1 independent adversarial review packet

You are the external independent safety and release reviewer. Do not assume the local reviewer is correct. Report concrete findings as `[P0]`, `[P1]`, or `[P2]`. For every finding, identify the violated requirement, a reproducible attack or evidence gap, impact, and the minimum safe fix. If evidence is insufficient, say exactly what must be checked instead of inventing a failure.

## Authority and hard requirements

The sole requirement authority is `docs/PRD.md` v1.3. The R1 Demo must satisfy this subset:

1. Garment and product IDs must be grounded in the current owner/tool return; hallucinations and cross-owner IDs are zero.
2. `now/today/unknown -> high`; high urgency has `shopping_allowed=false`, no shopping CTA, orchestration Catalog invocation zero, and a second Catalog-layer refusal.
3. Taboo colors, non-available status, season/weather mandatory items, inventory, budget, occasion, and ETA/safety-buffer constraints are applied before output; ranking/fallback cannot resurrect filtered IDs.
4. Scorecards contain exactly six outfit-by-goal dimensions and never score appearance, body, age, sexual appeal, health, or human worth.
5. Each adjustment round contains at most two suggestions; rejected canonical advice is not paraphrased and repeated within the session.
6. Look versions are immutable, owner-bound, comparable, rollback creates a new version, and Final blocks later mutation while historical reads remain available.
7. Memory is controlled-template `propose -> confirm -> commit`; sensitive content is no-write by default; edit/delete/TTL revalidate and remove behavioral effect.
8. LLM, Dense, Catalog, and Vision failures retain a safe core flow. Vision mismatch/malformed/partial evidence cannot produce fake numeric precision.
9. One command starts API+Web and one command produces versioned JSON+Markdown evaluation reports.
10. R1 has no usable 3D/360/video, payment, real scraping, open stylist market, second member, community feed, or static 2D image-generation route. Those capabilities must be honestly marked unlaunched.

## Implementation surface

- FastAPI and static Chinese SPA: `profagent/`, `web/`.
- Endpoints include `/health`, `/scene/parse`, `/recommend`, `/recommend/feedback`, `/team/home`, `/wardrobe`, `/assets`, `/look`, `/scorecard`, `/adjust`, `/finalize`, `/memory`, `/trace`, `/eval/run`.
- Retrieval: server-authoritative SceneParser, pre-recall hard filter, Rule + BM25 + optional Dense + RRF, assembler, final ID/constraint validator.
- Optional text/Vision provider uses logical `grok4.5`, exact CPA transport request `grok-4.5`, and fails closed if the chat response does not exactly report `grok-4.5`.
- `/health` in offline smoke reported rule core ready, LLM disabled, `static_2d=false`, `video=false`, and `three_d=false`.

## Independent execution evidence

All commands used Conda `torch128` serially:

- compileall: PASS.
- pytest: `32 passed`.
- fixed evaluation: PASS.
- data/schema validation: PASS, fixture counts `3/50/20/50/30`.
- JavaScript syntax check: PASS.
- supervisor attack probe: PASS.
- real API/Web startup and Edge render: PASS.

Evaluation metrics:

- Urgency mapping `30/30`.
- High-urgency shopping gate `10/10`, high Catalog invocation `0/10`.
- Item hallucination `0/515`.
- Observed hard-constraint violations `0/423`.
- Slot completeness `74/74`.
- Every recorded assurance is PASS: AC-01/03/04/05/06/07/10/11/13/14/16/17, query-only parsing, no-deadline audit, rain fail-closed, CPA privacy short-circuit, four-leg degradation, response validation, and Catalog oracle.

The evaluation declares its 30-row main urgency number as structured horizon-to-urgency mapping, not query-only NLP accuracy. A separate 21-case query-only parser assurance and a 3-case no-deadline audit participate in overall PASS. Explicit slot gaps require a real Catalog call. A separate fixture-only oracle computes legal inventory; e011/e012 correctly allow an empty result because no product satisfies all current PRD constraints. The legacy `allowed_catalog` array is only a non-authoritative/non-exhaustive generator witness.

## Privacy and scope facts

- Generated report declares no raw queries, images/base64, or credentials.
- Sensitive/medical/body/pure-vent requests are locally short-circuited before CPA; Trace uses controlled codes.
- User images require consent and purpose, accept only fully decoded single-frame PNG/JPEG/WebP within size/pixel limits, live only in process memory, and can be deleted.
- Current limitations: synthetic fixed Web user, no production auth/database/persistence, process-memory Trace/Look/memory/assets, no real commerce, no static 2D generation.

## Required attack focus

Try to falsify the release with emphasis on:

- time/intent conflicts, no-deadline texts, generic Catalog words, explicit “do not shop” combined with a real slot gap, and client attempts to relax server safety;
- Catalog call-vs-presence semantics, empty legal inventory, inventory filtering Trace, actual tool-return grounding, and high-urgency forced calls;
- cross-owner/session/request access, illegal IDs, fallback resurrection, Look mutation races, and post-Final operations;
- visual evidence schema smuggling, animated/polyglot images, model-ID mismatch, scorecard body/appearance language, and adjustment deduplication;
- memory proposal/edit/delete/TTL, feedback relevance and causal ranking effects, sensitive content retention, and Trace leakage;
- frontend all-path CTA gating, provider-status honesty, decision-ledger validation, ordinary-vs-Debug ID leakage, and accidental R2+ entry points;
- whether any report metric or AC assurance can pass without independently observing the claimed behavior.

Return an independent report only. Do not praise the implementation. If you find no issue, explicitly state `[P0] 0 / [P1] 0 / [P2] 0` and identify the highest-risk areas that were still evidence-limited.
