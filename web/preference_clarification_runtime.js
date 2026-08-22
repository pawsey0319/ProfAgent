(function installPreferenceClarificationRuntime(root, factory) {
  "use strict";

  const runtime = factory();
  if (typeof module === "object" && module.exports) module.exports = runtime;
  if (root) root.PROFAGENT_PREFERENCE_CLARIFICATION_RUNTIME = runtime;
}(typeof window !== "undefined" ? window : globalThis, function createPreferenceClarificationRuntime() {
  "use strict";

  const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
  const GAP_PATTERN = /^[a-z][a-z0-9_-]{0,39}:[a-z0-9_-]{1,119}$/;
  const URGENCY_BUDGETS = new Set(["normal", "last"]);
  const CONFIDENCE_BANDS = new Set(["high", "medium", "low"]);
  const ACTION_ORDER = Object.freeze(["remember", "session_only", "reject", "rephrase"]);
  const ALLOWED_ACTIONS = new Set(ACTION_ORDER);

  function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function assertExactKeys(value, expectedKeys, label) {
    if (!isPlainObject(value)) throw new Error(`${label}必须是对象`);
    const actual = Object.keys(value).sort();
    const expected = [...expectedKeys].sort();
    if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
      throw new Error(`${label}不符合闭集合同`);
    }
  }

  function validId(value) {
    return typeof value === "string" && ID_PATTERN.test(value);
  }

  function normalizePreferenceOption(value) {
    assertExactKeys(value, ["option_id", "label"], "偏好追问选项");
    if (!validId(value.option_id)) throw new Error("偏好追问选项标识无效");
    if (typeof value.label !== "string" || value.label.trim().length === 0 || value.label.length > 80) {
      throw new Error("偏好追问选项文案无效");
    }
    return Object.freeze({ option_id: value.option_id, label: value.label });
  }

  function normalizePreferenceClarification(value) {
    if (value === null) return null;
    assertExactKeys(
      value,
      ["question_id", "gap_code", "status", "options", "urgency_budget"],
      "偏好追问"
    );
    if (!validId(value.question_id)) throw new Error("偏好追问问题标识无效");
    if (typeof value.gap_code !== "string" || !GAP_PATTERN.test(value.gap_code)) {
      throw new Error("偏好追问问题缺口无效");
    }
    if (value.status !== "open") throw new Error("偏好追问状态无效");
    if (!Array.isArray(value.options) || value.options.length < 2 || value.options.length > 3) {
      throw new Error("偏好追问选项不闭合");
    }
    const options = value.options.map(normalizePreferenceOption);
    if (new Set(options.map((option) => option.option_id)).size !== options.length) {
      throw new Error("偏好追问选项标识重复");
    }
    if (new Set(options.map((option) => option.label)).size !== options.length) {
      throw new Error("偏好追问选项文案重复");
    }
    if (!URGENCY_BUDGETS.has(value.urgency_budget)) throw new Error("偏好追问紧迫度预算无效");
    return Object.freeze({
      question_id: value.question_id,
      gap_code: value.gap_code,
      status: value.status,
      options: Object.freeze(options),
      urgency_budget: value.urgency_budget
    });
  }

  function normalizeMemoryCandidate(value) {
    assertExactKeys(
      value,
      ["candidate_id", "confirmation_copy", "confidence_band", "conflict_copy", "allowed_actions"],
      "对话记忆候选"
    );
    if (!validId(value.candidate_id)) throw new Error("对话记忆候选标识无效");
    if (typeof value.confirmation_copy !== "string"
      || value.confirmation_copy.trim().length === 0
      || value.confirmation_copy.length > 500) throw new Error("对话记忆候选文案无效");
    if (!CONFIDENCE_BANDS.has(value.confidence_band)) throw new Error("对话记忆候选置信状态无效");
    if (value.conflict_copy !== null
      && (typeof value.conflict_copy !== "string"
        || value.conflict_copy.trim().length === 0
        || value.conflict_copy.length > 500)) throw new Error("对话记忆候选冲突文案无效");
    if (!Array.isArray(value.allowed_actions)
      || value.allowed_actions.length === 0
      || value.allowed_actions.length > ACTION_ORDER.length
      || value.allowed_actions.some((action) => !ALLOWED_ACTIONS.has(action))
      || new Set(value.allowed_actions).size !== value.allowed_actions.length
      || value.allowed_actions.some((action, index, actions) => index > 0
        && ACTION_ORDER.indexOf(action) <= ACTION_ORDER.indexOf(actions[index - 1]))) {
      throw new Error("对话记忆候选动作不符合闭集合同");
    }
    return Object.freeze({
      candidate_id: value.candidate_id,
      confirmation_copy: value.confirmation_copy,
      confidence_band: value.confidence_band,
      conflict_copy: value.conflict_copy,
      allowed_actions: Object.freeze([...value.allowed_actions])
    });
  }

  function normalizeDialogueMemoryCandidates(value) {
    if (!Array.isArray(value) || value.length > 1) throw new Error("对话记忆候选列表无效");
    const candidates = value.map(normalizeMemoryCandidate);
    if (new Set(candidates.map((candidate) => candidate.candidate_id)).size !== candidates.length) {
      throw new Error("对话记忆候选标识重复");
    }
    return Object.freeze(candidates);
  }

  function buildPreferenceTurn(question, option, state) {
    const normalizedQuestion = normalizePreferenceClarification(question);
    if (!normalizedQuestion) throw new Error("偏好追问绑定缺失");
    const normalizedOption = normalizePreferenceOption(option);
    if (state?.question_id !== normalizedQuestion.question_id || state?.gap_code !== normalizedQuestion.gap_code) {
      throw new Error("偏好追问与当前问题绑定不匹配");
    }
    const authoritativeOption = normalizedQuestion.options.find((entry) => entry.option_id === normalizedOption.option_id);
    if (!authoritativeOption || authoritativeOption.label !== normalizedOption.label) {
      throw new Error("偏好追问选项不在当前问题闭集内");
    }
    return Object.freeze({
      message: authoritativeOption.label,
      preference_question_id: normalizedQuestion.question_id,
      preference_option_id: authoritativeOption.option_id
    });
  }

  function createPreferenceClarificationController(dependencies) {
    if (typeof dependencies?.getContext !== "function"
      || typeof dependencies?.submitTurn !== "function"
      || typeof dependencies?.shouldSubmitKey !== "function") {
      throw new Error("偏好追问控制器依赖不完整");
    }
    let generation = 0;
    let active = null;
    let pending = null;
    const gapByQuestionId = new Map();
    const questionByGapCode = new Map();

    function contextSnapshot() {
      const value = dependencies.getContext();
      if (!isPlainObject(value) || typeof value.userId !== "string") throw new Error("偏好追问会话上下文无效");
      if (value.sessionId !== null && typeof value.sessionId !== "string") throw new Error("偏好追问会话上下文无效");
      return Object.freeze({ userId: value.userId, sessionId: value.sessionId });
    }

    function captureTurn() {
      return Object.freeze({ generation, context: contextSnapshot() });
    }

    function assertCurrent(origin, responseContext) {
      if (!origin || origin.generation !== generation) throw new Error("偏好追问响应已过期");
      const current = contextSnapshot();
      if (current.userId !== origin.context.userId || current.sessionId !== origin.context.sessionId) {
        throw new Error("偏好追问会话已变化，响应已过期");
      }
      if (!isPlainObject(responseContext)
        || responseContext.userId !== origin.context.userId
        || typeof responseContext.sessionId !== "string"
        || (origin.context.sessionId !== null && responseContext.sessionId !== origin.context.sessionId)) {
        throw new Error("偏好追问响应会话不匹配");
      }
      if (!new Set(["high", "medium", "low"]).has(responseContext.urgency)) {
        throw new Error("偏好追问响应紧迫度无效");
      }
    }

    function acceptResponse(origin, value, responseContext) {
      assertCurrent(origin, responseContext);
      const next = normalizePreferenceClarification(value);
      if (next === null) {
        active = null;
        return null;
      }
      const requiredBudget = responseContext.urgency === "high" ? "last" : "normal";
      if (next.urgency_budget !== requiredBudget) throw new Error("偏好追问紧迫度预算不匹配");
      const knownGap = gapByQuestionId.get(next.question_id);
      const knownQuestion = questionByGapCode.get(next.gap_code);
      if ((knownGap && knownGap !== next.gap_code) || (knownQuestion && knownQuestion !== next.question_id)) {
        throw new Error("偏好追问 question_id 与 preference_gap 绑定冲突");
      }
      if (knownGap || knownQuestion) throw new Error("偏好追问重复问题已抑制");
      gapByQuestionId.set(next.question_id, next.gap_code);
      questionByGapCode.set(next.gap_code, next.question_id);
      active = Object.freeze({
        question: next,
        question_id: next.question_id,
        gap_code: next.gap_code,
        userId: responseContext.userId,
        sessionId: responseContext.sessionId,
        generation
      });
      return next;
    }

    function activeQuestion() {
      return active?.generation === generation ? active.question : null;
    }

    function findTypedOption(message) {
      const current = activeQuestion();
      if (!current || typeof message !== "string") return null;
      const label = message.trim();
      return current.options.find((option) => option.label === label) || null;
    }

    async function submitOption(optionId) {
      const current = activeQuestion();
      if (!current || pending !== null) return false;
      const option = current.options.find((entry) => entry.option_id === optionId);
      if (!option) return false;
      const context = contextSnapshot();
      if (active.userId !== context.userId || active.sessionId !== context.sessionId || active.generation !== generation) {
        return false;
      }
      const origin = Object.freeze({ generation, context, questionId: current.question_id, optionId });
      pending = origin;
      try {
        const result = await dependencies.submitTurn(buildPreferenceTurn(current, option, active));
        return pending === origin
          && origin.generation === generation
          && contextSnapshot().userId === origin.context.userId
          && contextSnapshot().sessionId === origin.context.sessionId
          && result !== false;
      } finally {
        if (pending === origin) pending = null;
      }
    }

    function submitTyped(message) {
      const option = findTypedOption(message);
      return option ? submitOption(option.option_id) : Promise.resolve(false);
    }

    function handleComposerKey(event, form) {
      if (!dependencies.shouldSubmitKey(event)) return false;
      event.preventDefault();
      if (pending === null) form.requestSubmit();
      return true;
    }

    function reset() {
      generation += 1;
      active = null;
      pending = null;
      gapByQuestionId.clear();
      questionByGapCode.clear();
    }

    return Object.freeze({
      acceptResponse,
      activeQuestion,
      captureTurn,
      findTypedOption,
      handleComposerKey,
      reset,
      submitOption,
      submitTyped
    });
  }

  return Object.freeze({
    buildPreferenceTurn,
    createPreferenceClarificationController,
    normalizeDialogueMemoryCandidates,
    normalizePreferenceClarification
  });
}));
