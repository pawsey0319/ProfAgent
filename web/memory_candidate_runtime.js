(function installMemoryCandidateRuntime(root, factory) {
  "use strict";

  const runtime = factory();
  if (typeof module === "object" && module.exports) module.exports = runtime;
  if (root) root.PROFAGENT_MEMORY_CANDIDATE_RUNTIME = runtime;
}(typeof window !== "undefined" ? window : globalThis, function createMemoryCandidateRuntime() {
  "use strict";

  const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
  const EXTRACTION_STATUSES = new Set(["ready", "needs_rephrase", "blocked"]);
  const CONFIDENCE_BANDS = new Set(["high", "medium", "low"]);
  const ACTION_ORDER = Object.freeze(["remember", "session_only", "reject", "rephrase"]);
  const ALLOWED_ACTIONS = new Set(ACTION_ORDER);
  const DECISION_STATUSES = Object.freeze({
    remember: "committed",
    session_only: "session_only",
    reject: "rejected",
    rephrase: "rejected"
  });

  function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function validId(value) {
    return typeof value === "string" && ID_PATTERN.test(value);
  }

  function assertExactKeys(value, expectedKeys, label = "响应字段") {
    if (!isPlainObject(value)) throw new Error(`${label}必须是对象`);
    const actual = Object.keys(value).sort();
    const expected = [...expectedKeys].sort();
    if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
      throw new Error(`${label}不符合闭集合同`);
    }
  }

  function normalizeCandidate(candidate) {
    assertExactKeys(candidate, ["candidate_id", "confirmation_copy", "confidence_band", "conflict_copy", "allowed_actions"]);
    if (!validId(candidate.candidate_id)) throw new Error("候选 ID 不可信");
    if (typeof candidate.confirmation_copy !== "string"
      || candidate.confirmation_copy.trim().length === 0
      || candidate.confirmation_copy.length > 500) throw new Error("候选确认文案不可信");
    if (!CONFIDENCE_BANDS.has(candidate.confidence_band)) throw new Error("候选置信状态不可信");
    if (candidate.conflict_copy !== null
      && (typeof candidate.conflict_copy !== "string"
        || candidate.conflict_copy.trim().length === 0
        || candidate.conflict_copy.length > 500)) throw new Error("候选冲突文案不可信");
    if (!Array.isArray(candidate.allowed_actions)
      || candidate.allowed_actions.length === 0
      || candidate.allowed_actions.length > ALLOWED_ACTIONS.size
      || candidate.allowed_actions.some((action) => !ALLOWED_ACTIONS.has(action))
      || candidate.allowed_actions.some((action, index, actions) => index > 0
        && ACTION_ORDER.indexOf(action) <= ACTION_ORDER.indexOf(actions[index - 1]))
      || new Set(candidate.allowed_actions).size !== candidate.allowed_actions.length) {
      throw new Error("候选动作不符合闭集合同");
    }
    return Object.freeze({
      candidate_id: candidate.candidate_id,
      confirmation_copy: candidate.confirmation_copy,
      confidence_band: candidate.confidence_band,
      conflict_copy: candidate.conflict_copy,
      allowed_actions: Object.freeze([...candidate.allowed_actions])
    });
  }

  function normalizeExtractionResponse(payload, expectedRequestId) {
    assertExactKeys(payload, ["request_id", "status", "candidates", "trace_id"]);
    if (!validId(expectedRequestId) || payload.request_id !== expectedRequestId) throw new Error("提取回执未绑定当前请求");
    if (!EXTRACTION_STATUSES.has(payload.status)) throw new Error("提取回执状态不可信");
    if (!validId(payload.trace_id)) throw new Error("提取回执 Trace 不可信");
    if (!Array.isArray(payload.candidates) || payload.candidates.length > 5) throw new Error("候选列表不可信");
    const candidates = payload.candidates.map(normalizeCandidate);
    if (new Set(candidates.map((item) => item.candidate_id)).size !== candidates.length) throw new Error("候选 ID 重复");
    if ((payload.status === "ready") !== (candidates.length > 0)) throw new Error("提取状态与候选数量不一致");
    return Object.freeze({
      request_id: payload.request_id,
      status: payload.status,
      candidates: Object.freeze(candidates),
      trace_id: payload.trace_id
    });
  }

  function candidateActions(candidate) {
    normalizeCandidate(candidate);
    return Object.freeze([...candidate.allowed_actions]);
  }

  function normalizeCandidateDecision(payload, expectedCandidateId) {
    assertExactKeys(payload, ["candidate_id", "decision", "status", "record_id", "trace_id"]);
    if (!validId(expectedCandidateId) || payload.candidate_id !== expectedCandidateId) throw new Error("候选回执未绑定当前对象");
    if (!Object.hasOwn(DECISION_STATUSES, payload.decision)) throw new Error("候选决定不可信");
    if (payload.status !== DECISION_STATUSES[payload.decision]) throw new Error("候选决定状态不匹配");
    if (!validId(payload.trace_id)) throw new Error("候选回执 Trace 不可信");
    if (payload.decision === "remember") {
      if (!validId(payload.record_id)) throw new Error("record 回执不可信");
    } else if (payload.record_id !== null) {
      throw new Error("record 回执不符合决定状态");
    }
    return Object.freeze({
      candidate_id: payload.candidate_id,
      decision: payload.decision,
      status: payload.status,
      record_id: payload.record_id,
      trace_id: payload.trace_id
    });
  }

  return Object.freeze({
    assertExactKeys,
    normalizeExtractionResponse,
    normalizeCandidateDecision,
    candidateActions
  });
}));
