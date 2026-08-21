"use strict";

const assert = require("node:assert/strict");
const runtime = require("./memory_candidate_runtime.js");

const candidate = (overrides = {}) => ({
  candidate_id: "mcand_01",
  confirmation_copy: "要长期记住“偏爱藏青色”吗？",
  confidence_band: "high",
  conflict_copy: "已有同类已确认记忆；确认后将由服务端建立新版本。",
  allowed_actions: ["remember", "session_only", "reject", "rephrase"],
  ...overrides
});

const extraction = (overrides = {}) => ({
  request_id: "memory_extract_01",
  status: "ready",
  candidates: [candidate()],
  trace_id: "trace_memory_candidate_01",
  ...overrides
});

const normalized = runtime.normalizeExtractionResponse(extraction(), "memory_extract_01");
assert.equal(Object.isFrozen(normalized), true);
assert.equal(Object.isFrozen(normalized.candidates), true);
assert.equal(Object.isFrozen(normalized.candidates[0]), true);
assert.equal(Object.isFrozen(normalized.candidates[0].allowed_actions), true);
assert.deepEqual(runtime.candidateActions(normalized.candidates[0]), [
  "remember", "session_only", "reject", "rephrase"
]);
assert.deepEqual(Object.keys(normalized.candidates[0]).sort(), [
  "allowed_actions", "candidate_id", "confidence_band", "confirmation_copy", "conflict_copy"
]);
assert.doesNotMatch(JSON.stringify(normalized), /canonical_kind|canonical_value|provider|reasoning|body|user_id|styling_session_id/);

const noSession = runtime.normalizeExtractionResponse(extraction({
  candidates: [candidate({ allowed_actions: ["remember", "reject", "rephrase"] })]
}), "memory_extract_01");
assert.deepEqual(
  runtime.candidateActions(noSession.candidates[0]),
  ["remember", "reject", "rephrase"],
  "the browser must not add session_only when the server omits it"
);

for (const [name, payload] of [
  ["top-level extra", extraction({ provider_body: "private" })],
  ["candidate extra", extraction({ candidates: [candidate({ canonical_value: "navy" })] })],
  ["candidate missing", extraction({ candidates: [{ ...candidate(), conflict_copy: undefined }] })],
  ["invalid status", extraction({ status: "ok" })],
  ["non-array candidates", extraction({ candidates: {} })],
  ["duplicate candidate", extraction({ candidates: [candidate(), candidate()] })],
  ["invalid candidate id", extraction({ candidates: [candidate({ candidate_id: "bad/id" })] })],
  ["invalid confidence", extraction({ candidates: [candidate({ confidence_band: "certain" })] })],
  ["invalid copy", extraction({ candidates: [candidate({ confirmation_copy: "" })] })],
  ["invalid conflict", extraction({ candidates: [candidate({ conflict_copy: { text: "conflict" } })] })],
  ["invalid action", extraction({ candidates: [candidate({ allowed_actions: ["remember", "commit"] })] })],
  ["invalid action order", extraction({ candidates: [candidate({ allowed_actions: ["rephrase", "remember"] })] })],
  ["duplicate action", extraction({ candidates: [candidate({ allowed_actions: ["remember", "remember"] })] })],
  ["empty actions", extraction({ candidates: [candidate({ allowed_actions: [] })] })],
  ["ready without cards", extraction({ candidates: [] })],
  ["blocked with cards", extraction({ status: "blocked" })],
  ["needs-rephrase with cards", extraction({ status: "needs_rephrase" })],
  ["wrong request", extraction({ request_id: "memory_extract_other" })],
  ["invalid trace", extraction({ trace_id: "trace/bad" })]
]) {
  assert.throws(
    () => runtime.normalizeExtractionResponse(payload, "memory_extract_01"),
    /响应字段|提取回执|候选|状态|动作|Trace/,
    name
  );
}

for (const status of ["blocked", "needs_rephrase"]) {
  const empty = runtime.normalizeExtractionResponse(extraction({ status, candidates: [] }), "memory_extract_01");
  assert.equal(empty.status, status);
  assert.deepEqual(empty.candidates, []);
}

const decisions = [
  ["remember", "committed", "mem_01"],
  ["session_only", "session_only", null],
  ["reject", "rejected", null],
  ["rephrase", "rejected", null]
];
for (const [decision, status, recordId] of decisions) {
  const receipt = runtime.normalizeCandidateDecision({
    candidate_id: "mcand_01",
    decision,
    status,
    record_id: recordId,
    trace_id: `trace_${decision}`
  }, "mcand_01");
  assert.equal(Object.isFrozen(receipt), true);
  assert.equal(receipt.decision, decision);
}

for (const [name, payload] of [
  ["decision extra", { candidate_id: "mcand_01", decision: "reject", status: "rejected", record_id: null, trace_id: "trace_reject", canonical_kind: "color_preference" }],
  ["decision mismatch", { candidate_id: "mcand_other", decision: "reject", status: "rejected", record_id: null, trace_id: "trace_reject" }],
  ["decision invalid", { candidate_id: "mcand_01", decision: "confirm", status: "committed", record_id: "mem_01", trace_id: "trace_confirm" }],
  ["status mismatch", { candidate_id: "mcand_01", decision: "remember", status: "rejected", record_id: null, trace_id: "trace_remember" }],
  ["record mismatch", { candidate_id: "mcand_01", decision: "reject", status: "rejected", record_id: "mem_01", trace_id: "trace_reject" }],
  ["decision trace", { candidate_id: "mcand_01", decision: "reject", status: "rejected", record_id: null, trace_id: "trace/bad" }]
]) {
  assert.throws(
    () => runtime.normalizeCandidateDecision(payload, "mcand_01"),
    /响应字段|候选回执|决定|状态|record|Trace/,
    name
  );
}

console.log("web S16 memory candidate runtime exact-key/action contract: PASS");
