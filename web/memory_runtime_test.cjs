"use strict";

const assert = require("node:assert/strict");
const runtime = require("./memory_runtime.js");

const NOW = Date.parse("2026-08-19T08:00:00Z");
const approved = Object.freeze({
  preference: new Set(["偏爱直筒裤", "偏爱低调配色"]),
  constraint: new Set(["不穿高跟鞋"]),
  feedback: new Set(["偏好久走与长时间站立时选择舒适鞋履"])
});
const config = Object.freeze({
  userId: "u01",
  allowedTypes: new Set(Object.keys(approved)),
  allowedSourceKinds: new Set([
    "user_confirmed",
    "user_edited_confirmed",
    "feedback_confirmed",
    "legacy_migrated"
  ]),
  allowedProvenanceVersions: new Set(["memory_provenance_v1", "memory_legacy_v0"]),
  allowedConsentVersions: new Set(["explicit_confirm_v1", "legacy_confirm_v0"]),
  nowMs: NOW,
  isApprovedContent: (type, content) => approved[type]?.has(content) === true,
  expectedMemoryClass: (type) => type === "constraint" ? "hard_constraint" : "preference_event"
});

function proposal(overrides = {}) {
  return {
    proposal_id: "mp_01",
    user_id: "u01",
    styling_session_id: "styling_01",
    namespace: "stylist",
    type: "preference",
    content: "偏爱直筒裤",
    status: "proposed",
    sensitivity: "non_sensitive",
    commit_blocked: false,
    ttl_days: null,
    record_id: null,
    created_at: "2026-08-19T07:00:00Z",
    updated_at: "2026-08-19T07:00:00Z",
    trace_id: "private_trace",
    source: { private: "must_not_escape" },
    ...overrides
  };
}

function record(overrides = {}) {
  return {
    memory_id: "mem_01",
    user_id: "u01",
    namespace: "stylist",
    type: "preference",
    content: "偏爱直筒裤",
    status: "committed",
    sensitivity: "non_sensitive",
    source: "user_confirmed",
    source_proposal_id: "mp_01",
    created_at: "2026-08-19T07:00:00Z",
    ttl_days: 1,
    expires_at: "2026-08-20T08:00:00Z",
    memory_class: "preference_event",
    valid_from: "2026-08-19T07:00:00Z",
    valid_to: "2026-08-20T08:00:00Z",
    supersedes_memory_id: null,
    source_kind: "user_confirmed",
    provenance_version: "memory_provenance_v1",
    consent_version: "explicit_confirm_v1",
    confirmation_count: 1,
    lifecycle_status: "active",
    styling_session_id: null,
    private_source: { private: "must_not_escape" },
    trace_id: "private_trace",
    outbox: { content: "must_not_escape" },
    rrf_score: 999,
    ...overrides
  };
}

const normalizedProposal = runtime.normalizeProposal(proposal(), config);
assert.equal(runtime.validId("trace_123"), true);
assert.equal(runtime.validId("bad/id"), false);
assert.equal(normalizedProposal.metadata_valid, true);
assert.equal(normalizedProposal.actionable, true);
assert.equal(normalizedProposal.content, "偏爱直筒裤");
assert.equal(Object.hasOwn(normalizedProposal, "trace_id"), false);
assert.equal(Object.hasOwn(normalizedProposal, "source"), false);

const sensitiveProposal = runtime.normalizeProposal(proposal({ content: null, sensitivity: "sensitive", commit_blocked: true }), config);
assert.equal(sensitiveProposal.metadata_valid, true);
assert.equal(sensitiveProposal.content, null);
assert.equal(sensitiveProposal.commit_blocked, true);

for (const [name, invalid] of [
  ["non-boolean commit_blocked", { commit_blocked: "false" }],
  ["invalid session", { styling_session_id: "bad/session" }],
  ["missing nullable session", { styling_session_id: undefined }],
  ["reversed timestamps", { created_at: "2026-08-19T07:01:00Z", updated_at: "2026-08-19T07:00:00Z" }],
  ["proposed record link", { record_id: "mem_01" }],
  ["proposed missing content", { content: null }],
  ["invalid trace", { trace_id: "trace/bad" }]
]) {
  const result = runtime.normalizeProposal(proposal({ proposal_id: `mp_${name.replaceAll(" ", "_")}`, ...invalid }), config);
  assert.equal(result.metadata_valid, false, name);
  assert.equal(result.content, null, `${name} must redact content`);
  assert.equal(result.actionable, false, `${name} must not be confirmable`);
}

const normalizedRecord = runtime.normalizeRecord(record(), config);
assert.equal(normalizedRecord.metadata_valid, true);
assert.equal(normalizedRecord.lifecycle_status, "active");
assert.equal(normalizedRecord.source_kind, "user_confirmed");
assert.equal(normalizedRecord.confirmation_count, 1);
for (const privateKey of ["private_source", "trace_id", "outbox", "rrf_score"]) {
  assert.equal(Object.hasOwn(normalizedRecord, privateKey), false, `${privateKey} must stay private`);
}

const legacyRecord = runtime.normalizeRecord(record({
  memory_id: "mem_legacy",
  source_kind: "legacy_migrated",
  provenance_version: "memory_legacy_v0",
  consent_version: "legacy_confirm_v0"
}), config);
assert.equal(legacyRecord.metadata_valid, true);

const supersedingRecord = runtime.normalizeRecord(record({
  memory_id: "mem_02",
  source_proposal_id: "mp_02",
  supersedes_memory_id: "mem_01",
  confirmation_count: 2
}), config);
assert.equal(supersedingRecord.metadata_valid, true);

for (const [name, invalid] of [
  ["unknown class", { memory_class: "working_context", styling_session_id: "styling_01" }],
  ["unknown source", { source_kind: "user_explicit" }],
  ["bad provenance", { provenance_version: "memory_provenance_v2" }],
  ["bad consent", { consent_version: "implicit_v1" }],
  ["mixed legacy provenance", { source_kind: "legacy_migrated", provenance_version: "memory_provenance_v1", consent_version: "explicit_confirm_v1" }],
  ["mixed current consent", { source_kind: "user_confirmed", provenance_version: "memory_provenance_v1", consent_version: "legacy_confirm_v0" }],
  ["inactive lifecycle", { lifecycle_status: "expired" }],
  ["expired date", { valid_to: "2026-08-19T07:59:59Z", expires_at: "2026-08-19T07:59:59Z" }],
  ["future start", { valid_from: "2026-08-19T09:00:00Z", valid_to: null }],
  ["reversed dates", { valid_from: "2026-08-20T08:00:00Z", valid_to: "2026-08-19T08:00:00Z" }],
  ["invalid count", { confirmation_count: 0 }],
  ["unexpected session", { styling_session_id: "styling_01" }],
  ["expires mismatch", { expires_at: "2026-08-21T08:00:00Z" }],
  ["wrong class", { memory_class: "hard_constraint" }],
  ["wrong source", { source: "model_inferred" }],
  ["bad proposal link", { source_proposal_id: "bad/link" }],
  ["self supersedes", { supersedes_memory_id: "mem_self_supersedes", confirmation_count: 2 }],
  ["count without supersedes", { confirmation_count: 2 }],
  ["supersedes without increment", { supersedes_memory_id: "mem_old", confirmation_count: 1 }],
  ["feedback-only source on preference", { source_kind: "feedback_confirmed" }]
]) {
  const result = runtime.normalizeRecord(record({ memory_id: `mem_${name.replaceAll(" ", "_")}`, ...invalid }), config);
  assert.equal(result.metadata_valid, false, name);
  assert.equal(result.content, null, `${name} content must be redacted`);
  assert.equal(result.actionable, false, `${name} must not be actionable`);
  assert.equal(result.deletable, true, `${name} must retain owner-bound deletion governance`);
}

assert.equal(runtime.normalizeRecord(record({ user_id: "u02" }), config), null, "cross-owner record must be dropped");
assert.equal(runtime.normalizeProposal(proposal({ user_id: "u02" }), config), null, "cross-owner proposal must be dropped");

const list = runtime.normalizeList({
  api_version: "r1_demo_v1",
  user_id: "u01",
  namespace: null,
  proposals: [proposal(), proposal({ proposal_id: "mp_terminal", status: "committed", content: null })],
  records: [record(), record({ memory_id: "mem_cross", user_id: "u02" })],
  retrieval_index_count: 1,
  outbox: [{ content: "private" }]
}, config);
assert.deepEqual(list.proposals.map((item) => item.proposal_id), ["mp_01"]);
assert.deepEqual(list.records.map((item) => item.memory_id), ["mem_01"]);

const proposeReceipt = runtime.normalizeMutationReceipt({
  api_version: "r1_demo_v1",
  proposal: proposal(),
  record: null,
  trace_id: "private_trace"
}, {
  operation: "propose",
  request: {
    user_id: "u01",
    styling_session_id: "styling_01",
    namespace: "stylist",
    type: "preference",
    content: "偏爱直筒裤",
    sensitivity: "non_sensitive",
    ttl_days: null
  }
}, config);
assert.equal(proposeReceipt.proposal.proposal_id, "mp_01");
assert.equal(proposeReceipt.proposal.ttl_days, null);
assert.equal(proposeReceipt.proposal.created_at, "2026-08-19T07:00:00Z");

const immutableRequest = {
  user_id: "u01",
  styling_session_id: "styling_01",
  namespace: "stylist",
  type: "preference",
  content: "偏爱直筒裤",
  sensitivity: "non_sensitive",
  ttl_days: null
};
for (const [name, mutation] of [
  ["wrong namespace", { namespace: "shared" }],
  ["wrong type", { type: "constraint", content: "不穿高跟鞋" }],
  ["wrong content", { content: "偏爱低调配色" }],
  ["wrong ttl", { ttl_days: 30 }],
  ["wrong session", { styling_session_id: "styling_other" }],
  ["wrong sensitivity", { sensitivity: "sensitive", commit_blocked: true, content: null }]
]) {
  assert.throws(() => runtime.normalizeMutationReceipt({
    api_version: "r1_demo_v1",
    proposal: proposal({ proposal_id: `mp_propose_${name.replaceAll(" ", "_")}`, ...mutation }),
    record: null,
    trace_id: "private_trace"
  }, { operation: "propose", request: immutableRequest }, config), /propose|回执/, `propose ${name}`);
}

const committedProposal = proposal({
  content: null,
  status: "committed",
  record_id: "mem_01",
  updated_at: "2026-08-19T07:05:00Z"
});
const committedRecord = record({ ttl_days: null, valid_to: null, expires_at: null });
const confirmReceipt = runtime.normalizeMutationReceipt({
  api_version: "r1_demo_v1",
  proposal: committedProposal,
  record: committedRecord,
  trace_id: "private_trace"
}, {
  operation: "confirm",
  proposalId: "mp_01",
  proposal: normalizedProposal,
  content: "偏爱直筒裤"
}, config);
assert.equal(confirmReceipt.record.memory_id, "mem_01");

const editedReceipt = runtime.normalizeMutationReceipt({
  api_version: "r1_demo_v1",
  proposal: committedProposal,
  record: { ...committedRecord, source_kind: "user_edited_confirmed" },
  trace_id: "private_trace"
}, {
  operation: "edit",
  proposalId: "mp_01",
  proposal: normalizedProposal,
  content: "偏爱直筒裤"
}, config);
assert.equal(editedReceipt.record.source_kind, "user_edited_confirmed");

const rejectedProposal = proposal({
  content: null,
  status: "rejected",
  record_id: null,
  updated_at: "2026-08-19T07:05:00Z"
});
const rejectReceipt = runtime.normalizeMutationReceipt({
  api_version: "r1_demo_v1",
  proposal: rejectedProposal,
  record: null,
  trace_id: "private_trace"
}, { operation: "reject", proposalId: "mp_01", proposal: normalizedProposal }, config);
assert.equal(rejectReceipt.record, null);

const deleteReceipt = runtime.normalizeMutationReceipt({
  api_version: "r1_demo_v1",
  deleted_id: "mem_01",
  deleted_kind: "record",
  user_id: "u01",
  namespace: "stylist",
  truth_version: 2,
  trace_id: "trace_delete_01"
}, { operation: "delete", item: normalizedRecord }, config);
assert.equal(deleteReceipt.truth_version, 2);

for (const [name, mutate] of [
  ["wrong proposal id", (raw) => { raw.proposal.proposal_id = "mp_other"; }],
  ["wrong record link", (raw) => { raw.proposal.record_id = "mem_other"; }],
  ["wrong source proposal", (raw) => { raw.record.source_proposal_id = "mp_other"; }],
  ["wrong source kind", (raw) => { raw.record.source_kind = "user_edited_confirmed"; }],
  ["unexpected reject record", (raw) => { raw.proposal = rejectedProposal; raw.record = record(); }]
]) {
  const raw = structuredClone({ api_version: "r1_demo_v1", proposal: committedProposal, record: committedRecord, trace_id: "private_trace" });
  mutate(raw);
  const expected = name === "unexpected reject record"
    ? { operation: "reject", proposalId: "mp_01", proposal: normalizedProposal }
    : { operation: "confirm", proposalId: "mp_01", proposal: normalizedProposal, content: "偏爱直筒裤" };
  assert.throws(() => runtime.normalizeMutationReceipt(raw, expected, config), /Memory|回执|proposal-record|reject/, name);
}

for (const [name, mutate] of [
  ["wrong namespace", (raw) => { raw.proposal.namespace = "shared"; raw.record.namespace = "shared"; }],
  ["wrong type", (raw) => { raw.proposal.type = "constraint"; raw.record.type = "constraint"; raw.record.content = "不穿高跟鞋"; raw.record.memory_class = "hard_constraint"; }],
  ["wrong content", (raw) => { raw.record.content = "偏爱低调配色"; }],
  ["wrong ttl", (raw) => {
    raw.proposal.ttl_days = 30;
    raw.record.ttl_days = 30;
    raw.record.valid_to = "2026-09-18T07:00:00Z";
    raw.record.expires_at = "2026-09-18T07:00:00Z";
  }],
  ["wrong session", (raw) => { raw.proposal.styling_session_id = "styling_other"; }]
]) {
  const raw = structuredClone({ api_version: "r1_demo_v1", proposal: committedProposal, record: committedRecord, trace_id: "private_trace" });
  mutate(raw);
  assert.throws(() => runtime.normalizeMutationReceipt(raw, {
    operation: "confirm",
    proposalId: "mp_01",
    proposal: normalizedProposal,
    content: "偏爱直筒裤"
  }, config), /Memory|回执|proposal|链/, `confirm ${name}`);
}

for (const invalidDelete of [
  { user_id: "u02" },
  { namespace: "shared" },
  { truth_version: 1 },
  { truth_version: "2" },
  { deleted_id: "mem_other" }
]) {
  assert.throws(() => runtime.normalizeMutationReceipt({
    api_version: "r1_demo_v1",
    deleted_id: "mem_01",
    deleted_kind: "record",
    user_id: "u01",
    namespace: "stylist",
    truth_version: 2,
    trace_id: "trace_delete_01",
    ...invalidDelete
  }, { operation: "delete", item: normalizedRecord }, config), /删除回执/);
}

const invalidOwnedProposal = runtime.normalizeProposal(proposal({ commit_blocked: "false" }), config);
const deleteProposalReceipt = runtime.normalizeMutationReceipt({
  api_version: "r1_demo_v1",
  deleted_id: "mp_01",
  deleted_kind: "proposal",
  user_id: "u01",
  namespace: "stylist",
  truth_version: 0,
  trace_id: "trace_delete_proposal"
}, { operation: "delete", item: invalidOwnedProposal }, config);
assert.equal(deleteProposalReceipt.truth_version, 0);

assert.throws(() => runtime.normalizeList({
  api_version: "r1_demo_v1",
  user_id: "u02",
  namespace: null,
  proposals: [],
  records: []
}, config), /owner/);

console.log("web S15 memory runtime contract: PASS");
