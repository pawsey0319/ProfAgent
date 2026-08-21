(function installMemoryRuntime(root, factory) {
  "use strict";

  const runtime = factory();
  if (typeof module === "object" && module.exports) module.exports = runtime;
  if (root) root.PROFAGENT_MEMORY_RUNTIME = runtime;
}(typeof window !== "undefined" ? window : globalThis, function createMemoryRuntime() {
  "use strict";

  const MEMORY_CLASSES = new Set([
    "profile_current",
    "hard_constraint",
    "preference_event",
    "episodic_summary"
  ]);
  const NAMESPACES = new Set(["shared", "stylist"]);
  const SENSITIVITIES = new Set(["non_sensitive", "sensitive"]);
  const PROPOSAL_STATUSES = new Set(["proposed", "committed", "rejected"]);
  const RECORD_STATUSES = new Set(["committed"]);
  const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
  const ISO_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

  function validId(value) {
    return typeof value === "string" && ID_PATTERN.test(value);
  }

  function validIso(value) {
    return typeof value === "string" && ISO_PATTERN.test(value) && Number.isFinite(Date.parse(value));
  }

  function validVersion(value, allowed) {
    return typeof value === "string" && allowed.has(value);
  }

  function safeMetadata(kind, raw, id) {
    return Object.freeze({
      kind,
      proposal_id: kind === "proposal" ? id : null,
      memory_id: kind === "record" ? id : null,
      record_id: null,
      source_proposal_id: null,
      user_id: raw.user_id,
      namespace: NAMESPACES.has(raw.namespace) ? raw.namespace : null,
      type: null,
      content: null,
      status: "blocked",
      sensitivity: "sensitive",
      commit_blocked: true,
      memory_class: null,
      valid_from: null,
      valid_to: null,
      supersedes_memory_id: null,
      source_kind: null,
      provenance_version: null,
      consent_version: null,
      confirmation_count: null,
      ttl_days: null,
      created_at: null,
      updated_at: null,
      styling_session_id: null,
      lifecycle_status: null,
      metadata_valid: false,
      actionable: false,
      deletable: true
    });
  }

  function validNullableId(value) {
    return value === null || validId(value);
  }

  function validTtl(value) {
    return value === null || (Number.isInteger(value) && value >= 1 && value <= 365);
  }

  function normalizeLifecycle(raw, config, kind) {
    const id = kind === "proposal" ? raw?.proposal_id : raw?.memory_id;
    if (!validId(id) || raw?.user_id !== config.userId) return null;
    const statusAllowed = kind === "proposal" ? PROPOSAL_STATUSES : RECORD_STATUSES;
    const supersedesValid = raw.supersedes_memory_id === null || validId(raw.supersedes_memory_id);
    const datesValid = validIso(raw.valid_from)
      && (raw.valid_to === null || validIso(raw.valid_to))
      && (raw.valid_to === null || Date.parse(raw.valid_to) >= Date.parse(raw.valid_from));
    const countValid = Number.isInteger(raw.confirmation_count)
      && raw.confirmation_count >= 0
      && raw.confirmation_count <= 1_000_000
      && (kind !== "record" || raw.confirmation_count >= 1);
    const effectiveNow = Number.isFinite(config.nowMs) ? config.nowMs : Date.now();
    const activeWindow = Date.parse(raw.valid_from) <= effectiveNow
      && (raw.valid_to === null || Date.parse(raw.valid_to) > effectiveNow);
    const legacyTuple = raw.source_kind === "legacy_migrated"
      && raw.provenance_version === "memory_legacy_v0"
      && raw.consent_version === "legacy_confirm_v0";
    const currentTuple = raw.source_kind !== "legacy_migrated"
      && raw.provenance_version === "memory_provenance_v1"
      && raw.consent_version === "explicit_confirm_v1";
    const sourceTypeValid = (raw.source_kind !== "feedback_confirmed" || raw.type === "feedback")
      && (raw.type !== "feedback" || new Set(["feedback_confirmed", "user_edited_confirmed", "legacy_migrated"]).has(raw.source_kind));
    const confirmationChainValid = legacyTuple
      || (raw.supersedes_memory_id === null ? raw.confirmation_count === 1 : raw.confirmation_count >= 2);
    const ttlWindowValid = validTtl(raw.ttl_days)
      && ((raw.ttl_days === null && raw.valid_to === null) || (raw.ttl_days !== null && raw.valid_to !== null));
    const metadataValid = NAMESPACES.has(raw.namespace)
      && MEMORY_CLASSES.has(raw.memory_class)
      && config.allowedTypes.has(raw.type)
      && typeof raw.content === "string"
      && raw.content.length > 0
      && raw.content.length <= 1_000
      && config.isApprovedContent(raw.type, raw.content)
      && config.expectedMemoryClass(raw.type, raw.content) === raw.memory_class
      && statusAllowed.has(raw.status)
      && raw.lifecycle_status === "active"
      && raw.sensitivity === "non_sensitive"
      && raw.source === "user_confirmed"
      && validId(raw.source_proposal_id)
      && validIso(raw.created_at)
      && Date.parse(raw.created_at) <= Date.parse(raw.valid_from)
      && config.allowedSourceKinds.has(raw.source_kind)
      && validVersion(raw.provenance_version, config.allowedProvenanceVersions)
      && validVersion(raw.consent_version, config.allowedConsentVersions)
      && (legacyTuple || currentTuple)
      && sourceTypeValid
      && supersedesValid
      && raw.supersedes_memory_id !== raw.memory_id
      && datesValid
      && raw.expires_at === raw.valid_to
      && ttlWindowValid
      && activeWindow
      && countValid
      && confirmationChainValid
      && raw.styling_session_id == null;
    if (!metadataValid) return safeMetadata(kind, raw, id);
    return { id, status: raw.status };
  }

  function normalizeProposal(raw, config) {
    const id = raw?.proposal_id;
    if (!validId(id) || raw?.user_id !== config.userId) return null;
    const sessionValid = validNullableId(raw.styling_session_id);
    const timeValid = validIso(raw.created_at)
      && validIso(raw.updated_at)
      && Date.parse(raw.updated_at) >= Date.parse(raw.created_at);
    const contentShapeValid = raw.content === null
      || (typeof raw.content === "string" && raw.content.length > 0 && raw.content.length <= 1_000);
    const proposedStateValid = raw.status !== "proposed" || (
      raw.record_id === null
      && (raw.commit_blocked === true
        ? raw.content === null
        : raw.sensitivity === "non_sensitive"
          && typeof raw.content === "string"
          && config.isApprovedContent(raw.type, raw.content))
    );
    const committedStateValid = raw.status !== "committed" || (
      validId(raw.record_id)
      && raw.content === null
      && raw.commit_blocked === false
      && raw.sensitivity === "non_sensitive"
    );
    const rejectedStateValid = raw.status !== "rejected" || (
      raw.record_id === null
      && raw.content === null
      && (raw.commit_blocked === true || raw.sensitivity === "non_sensitive")
    );
    const proposalMetadataValid = NAMESPACES.has(raw.namespace)
      && config.allowedTypes.has(raw.type)
      && PROPOSAL_STATUSES.has(raw.status)
      && SENSITIVITIES.has(raw.sensitivity)
      && typeof raw.commit_blocked === "boolean"
      && validTtl(raw.ttl_days)
      && sessionValid
      && validId(raw.trace_id)
      && contentShapeValid
      && timeValid
      && proposedStateValid
      && committedStateValid
      && rejectedStateValid;
    if (!proposalMetadataValid) return safeMetadata("proposal", raw, id);
    const visibleContent = raw.status === "proposed"
      && raw.sensitivity === "non_sensitive"
      && raw.commit_blocked === false;
    return Object.freeze({
      kind: "proposal",
      proposal_id: raw.proposal_id,
      memory_id: null,
      record_id: raw.record_id,
      source_proposal_id: null,
      user_id: raw.user_id,
      namespace: raw.namespace,
      type: raw.type,
      content: visibleContent ? raw.content : null,
      status: raw.status,
      sensitivity: raw.sensitivity,
      commit_blocked: raw.commit_blocked,
      ttl_days: raw.ttl_days,
      created_at: raw.created_at,
      updated_at: raw.updated_at,
      memory_class: null,
      valid_from: null,
      valid_to: null,
      supersedes_memory_id: null,
      source_kind: null,
      provenance_version: null,
      consent_version: null,
      confirmation_count: null,
      styling_session_id: validId(raw.styling_session_id) ? raw.styling_session_id : null,
      lifecycle_status: null,
      metadata_valid: true,
      actionable: raw.status === "proposed",
      deletable: true
    });
  }

  function normalizeRecord(raw, config) {
    const lifecycle = normalizeLifecycle(raw, config, "record");
    if (!lifecycle || lifecycle.metadata_valid === false) return lifecycle;
    return Object.freeze({
      kind: "record",
      proposal_id: null,
      memory_id: raw.memory_id,
      record_id: null,
      source_proposal_id: raw.source_proposal_id,
      user_id: raw.user_id,
      namespace: raw.namespace,
      type: raw.type,
      content: raw.content,
      status: raw.status,
      sensitivity: "non_sensitive",
      commit_blocked: false,
      ttl_days: raw.ttl_days,
      created_at: raw.created_at,
      updated_at: null,
      memory_class: raw.memory_class,
      valid_from: raw.valid_from,
      valid_to: raw.valid_to,
      supersedes_memory_id: raw.supersedes_memory_id,
      source_kind: raw.source_kind,
      provenance_version: raw.provenance_version,
      consent_version: raw.consent_version,
      confirmation_count: raw.confirmation_count,
      styling_session_id: null,
      lifecycle_status: raw.lifecycle_status,
      metadata_valid: true,
      actionable: raw.status === "committed",
      deletable: true
    });
  }

  function normalizeMutationReceipt(raw, expected, config) {
    if (!raw || typeof raw !== "object" || raw.api_version !== "r1_demo_v1" || !validId(raw.trace_id)) {
      throw new Error("Memory mutation 回执版本或 Trace 不可信");
    }
    if (expected.operation === "delete") {
      const item = expected.item;
      if (!item || item.user_id !== config.userId || !new Set(["proposal", "record"]).has(item.kind)) {
        throw new Error("Memory 删除对象未通过 owner 与类型复核");
      }
      const expectedId = item.kind === "proposal" ? item.proposal_id : item.memory_id;
      const namespaceTrusted = item.namespace === null
        ? NAMESPACES.has(raw.namespace)
        : raw.namespace === item.namespace;
      const versionTrusted = item.kind === "proposal"
        ? raw.truth_version === 0
        : Number.isInteger(raw.truth_version) && raw.truth_version >= 2;
      if (raw.deleted_id !== expectedId
        || raw.deleted_kind !== item.kind
        || raw.user_id !== config.userId
        || !namespaceTrusted
        || !versionTrusted) {
        throw new Error("Memory 删除回执未绑定 owner、namespace 与真值版本");
      }
      return Object.freeze({
        operation: "delete",
        deleted_id: raw.deleted_id,
        deleted_kind: raw.deleted_kind,
        user_id: raw.user_id,
        namespace: raw.namespace,
        truth_version: raw.truth_version,
        trace_id: raw.trace_id
      });
    }

    const normalizedProposal = normalizeProposal(raw.proposal, config);
    if (!normalizedProposal || normalizedProposal.metadata_valid !== true || raw.proposal.trace_id !== raw.trace_id) {
      throw new Error("Memory mutation proposal 回执不可信");
    }
    if (expected.proposalId && normalizedProposal.proposal_id !== expected.proposalId) {
      throw new Error("Memory mutation proposal_id 与操作对象不匹配");
    }
    if (expected.proposal && (
      normalizedProposal.user_id !== expected.proposal.user_id
      || normalizedProposal.namespace !== expected.proposal.namespace
      || normalizedProposal.type !== expected.proposal.type
      || normalizedProposal.sensitivity !== expected.proposal.sensitivity
      || normalizedProposal.ttl_days !== expected.proposal.ttl_days
      || normalizedProposal.styling_session_id !== expected.proposal.styling_session_id
      || normalizedProposal.created_at !== expected.proposal.created_at
    )) {
      throw new Error("Memory mutation proposal 元数据与操作对象不匹配");
    }
    if (expected.operation === "propose") {
      const request = expected.request;
      const requestTupleMatches = request
        && raw.proposal.user_id === request.user_id
        && raw.proposal.namespace === request.namespace
        && raw.proposal.type === request.type
        && raw.proposal.content === request.content
        && raw.proposal.sensitivity === request.sensitivity
        && raw.proposal.ttl_days === request.ttl_days
        && raw.proposal.styling_session_id === request.styling_session_id;
      if (!requestTupleMatches || normalizedProposal.status !== "proposed" || raw.record !== null) {
        throw new Error("propose 不得直接提交长期记录");
      }
      return Object.freeze({ operation: "propose", proposal: normalizedProposal, record: null, trace_id: raw.trace_id });
    }
    if (expected.operation === "reject") {
      if (normalizedProposal.status !== "rejected" || raw.record !== null || normalizedProposal.record_id !== null) {
        throw new Error("reject 回执必须是无 record 的 rejected proposal");
      }
      return Object.freeze({ operation: "reject", proposal: normalizedProposal, record: null, trace_id: raw.trace_id });
    }
    if (!new Set(["confirm", "edit"]).has(expected.operation)) throw new Error("未知 Memory mutation 操作");
    const normalizedRecord = normalizeRecord(raw.record, config);
    const expectedSource = expected.operation === "edit"
      ? "user_edited_confirmed"
      : normalizedProposal.type === "feedback" ? "feedback_confirmed" : "user_confirmed";
    if (normalizedProposal.status !== "committed"
      || !normalizedRecord
      || normalizedRecord.metadata_valid !== true
      || normalizedProposal.record_id !== normalizedRecord.memory_id
      || normalizedRecord.source_proposal_id !== normalizedProposal.proposal_id
      || normalizedRecord.user_id !== normalizedProposal.user_id
      || normalizedRecord.namespace !== normalizedProposal.namespace
      || normalizedRecord.type !== normalizedProposal.type
      || normalizedRecord.ttl_days !== expected.proposal.ttl_days
      || normalizedRecord.source_kind !== expectedSource
      || (typeof expected.content === "string" && normalizedRecord.content !== expected.content)) {
      throw new Error("confirm/edit 回执的 proposal-record 链不完整");
    }
    return Object.freeze({
      operation: expected.operation,
      proposal: normalizedProposal,
      record: normalizedRecord,
      trace_id: raw.trace_id
    });
  }

  function normalizeList(raw, config) {
    if (!raw || typeof raw !== "object" || raw.api_version !== "r1_demo_v1" || raw.user_id !== config.userId) {
      throw new Error("Memory 列表 owner 或版本不匹配");
    }
    if (raw.namespace !== null && !NAMESPACES.has(raw.namespace)) throw new Error("Memory 列表 namespace 不可信");
    if (!Array.isArray(raw.proposals) || !Array.isArray(raw.records)) throw new Error("Memory 列表结构不完整");
    return Object.freeze({
      proposals: raw.proposals
        .map((item) => new Set(["committed", "rejected"]).has(item?.status) ? null : normalizeProposal(item, config))
        .filter((item) => item && (item.status === "proposed" || item.metadata_valid === false)),
      records: raw.records.map((item) => normalizeRecord(item, config)).filter(Boolean)
    });
  }

  return Object.freeze({
    MEMORY_CLASSES,
    NAMESPACES,
    normalizeProposal,
    normalizeRecord,
    normalizeList,
    normalizeMutationReceipt,
    validId,
    validIso
  });
}));
