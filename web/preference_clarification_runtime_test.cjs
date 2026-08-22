"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  buildPreferenceTurn,
  createPreferenceClarificationController,
  normalizeDialogueMemoryCandidates,
  normalizePreferenceClarification
} = require("./preference_clarification_runtime.js");

const question = (overrides = {}) => ({
  question_id: "prefq_color_navy_beige_01",
  gap_code: "color:navy_vs_beige",
  status: "open",
  options: [
    { option_id: "prefopt_navy_01", label: "藏青色" },
    { option_id: "prefopt_beige_01", label: "米色" },
    { option_id: "prefopt_neutral_01", label: "你决定" }
  ],
  urgency_budget: "normal",
  ...overrides
});

const memoryCandidate = (overrides = {}) => ({
  candidate_id: "mcand_preference_01",
  confirmation_copy: "要长期记住这次颜色偏好吗？",
  confidence_band: "high",
  conflict_copy: null,
  allowed_actions: ["remember", "session_only", "reject", "rephrase"],
  ...overrides
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function responseContext(sessionId = "session_01", urgency = "low") {
  return { userId: "u01", sessionId, urgency };
}

async function run() {
  const normalized = normalizePreferenceClarification(question());
  assert.equal(Object.isFrozen(normalized), true);
  assert.equal(Object.isFrozen(normalized.options), true);
  assert.equal(Object.isFrozen(normalized.options[0]), true);
  assert.deepEqual(Object.keys(normalized).sort(), [
    "gap_code", "options", "question_id", "status", "urgency_budget"
  ]);
  assert.equal(normalizePreferenceClarification(null), null);

  for (const [name, value] of [
    ["top-level extra", question({ ranking_score: 0.99 })],
    ["missing status", (() => { const value = question(); delete value.status; return value; })()],
    ["invalid status", question({ status: "answered" })],
    ["too few options", question({ options: [question().options[0]] })],
    ["too many options", question({ options: [...question().options, { option_id: "prefopt_other", label: "其他" }] })],
    ["option extra", question({ options: [{ ...question().options[0], canonical_value: "navy" }, ...question().options.slice(1)] })],
    ["duplicate option id", question({ options: [question().options[0], { ...question().options[1], option_id: question().options[0].option_id }] })],
    ["duplicate option label", question({ options: [question().options[0], { ...question().options[1], label: question().options[0].label }] })],
    ["invalid question id", question({ question_id: "bad/id" })],
    ["invalid gap", question({ gap_code: "not a controlled gap" })],
    ["invalid urgency budget", question({ urgency_budget: "unlimited" })]
  ]) {
    assert.throws(() => normalizePreferenceClarification(value), /偏好追问|闭集|选项|标识|问题/, name);
  }

  const cards = normalizeDialogueMemoryCandidates([memoryCandidate()]);
  assert.equal(Object.isFrozen(cards), true);
  assert.equal(Object.isFrozen(cards[0]), true);
  assert.deepEqual(Object.keys(cards[0]).sort(), [
    "allowed_actions", "candidate_id", "confidence_band", "confirmation_copy", "conflict_copy"
  ]);
  assert.deepEqual(normalizeDialogueMemoryCandidates([]), []);
  for (const [name, value] of [
    ["not an array", {}],
    ["more than one card", [memoryCandidate(), memoryCandidate({ candidate_id: "mcand_preference_02" })]],
    ["card extra", [memoryCandidate({ canonical_value: "navy" })]],
    ["duplicate action", [memoryCandidate({ allowed_actions: ["remember", "remember"] })]],
    ["invalid action", [memoryCandidate({ allowed_actions: ["remember", "commit"] })]],
    ["invalid action order", [memoryCandidate({ allowed_actions: ["reject", "remember"] })]]
  ]) {
    assert.throws(() => normalizeDialogueMemoryCandidates(value), /记忆候选|闭集|动作/, name);
  }
  assert.doesNotMatch(JSON.stringify(cards), /canonical|provider|reasoning|body|user_id|styling_session_id/);

  const built = buildPreferenceTurn(normalized, normalized.options[0], {
    question_id: normalized.question_id,
    gap_code: normalized.gap_code
  });
  assert.deepEqual(built, {
    message: "藏青色",
    preference_question_id: normalized.question_id,
    preference_option_id: normalized.options[0].option_id
  });
  assert.deepEqual(Object.keys(built), ["message", "preference_question_id", "preference_option_id"]);
  assert.throws(
    () => buildPreferenceTurn(normalized, normalized.options[0], { question_id: "prefq_other", gap_code: normalized.gap_code }),
    /绑定/
  );

  let context = { userId: "u01", sessionId: null };
  let postCount = 0;
  let latestPayload = null;
  let pendingPost = deferred();
  const controller = createPreferenceClarificationController({
    getContext: () => ({ ...context }),
    shouldSubmitKey: (event) => event.key === "Enter"
      && event.shiftKey !== true
      && event.isComposing !== true
      && event.keyCode !== 229,
    submitTurn: async (payload) => {
      postCount += 1;
      latestPayload = payload;
      return pendingPost.promise;
    }
  });

  const firstOrigin = controller.captureTurn();
  const firstQuestion = controller.acceptResponse(firstOrigin, question(), responseContext());
  context = { userId: "u01", sessionId: "session_01" };
  assert.equal(firstQuestion.question_id, question().question_id);
  assert.equal(controller.activeQuestion().gap_code, question().gap_code);
  assert.equal(controller.findTypedOption("  藏青色  ").option_id, question().options[0].option_id);
  assert.equal(controller.findTypedOption("想要藏青色"), null, "free prose must remain a normal dialogue turn");

  let submitCalls = 0;
  const fakeForm = { requestSubmit() { submitCalls += 1; } };
  let prevented = 0;
  assert.equal(controller.handleComposerKey({ key: "Enter", shiftKey: false, isComposing: false, preventDefault() { prevented += 1; } }, fakeForm), true);
  assert.equal(controller.handleComposerKey({ key: "Enter", shiftKey: true, isComposing: false, preventDefault() { prevented += 1; } }, fakeForm), false);
  assert.equal(controller.handleComposerKey({ key: "Enter", shiftKey: false, isComposing: true, preventDefault() { prevented += 1; } }, fakeForm), false);
  assert.equal(controller.handleComposerKey({ key: "Enter", shiftKey: false, isComposing: false, keyCode: 229, preventDefault() { prevented += 1; } }, fakeForm), false);
  assert.equal(submitCalls, 1);
  assert.equal(prevented, 1);

  const optionSubmit = controller.submitOption(question().options[0].option_id);
  const duplicateClick = await controller.submitOption(question().options[0].option_id);
  const duplicateTyped = await controller.submitTyped("藏青色");
  assert.equal(duplicateClick, false);
  assert.equal(duplicateTyped, false);
  assert.equal(postCount, 1, "option, typed answer and double click share one single-flight");
  assert.deepEqual(latestPayload, built);
  assert.doesNotMatch(JSON.stringify(latestPayload), /memory|rank|score|outfit/);
  pendingPost.resolve(true);
  assert.equal(await optionSubmit, true);

  pendingPost = deferred();
  const neutralSubmit = controller.submitTyped("你决定");
  assert.equal(postCount, 2);
  assert.deepEqual(latestPayload, {
    message: "你决定",
    preference_question_id: question().question_id,
    preference_option_id: question().options[2].option_id
  });
  pendingPost.resolve(true);
  assert.equal(await neutralSubmit, true);

  const duplicateOrigin = controller.captureTurn();
  assert.throws(
    () => controller.acceptResponse(duplicateOrigin, question({ question_id: "prefq_color_navy_beige_02" }), responseContext()),
    /重复|绑定/,
    "the same preference gap must not be asked twice"
  );

  controller.reset();
  const highOrigin = controller.captureTurn();
  assert.throws(
    () => controller.acceptResponse(highOrigin, question(), responseContext("session_01", "high")),
    /紧迫度/,
    "high urgency only accepts the server's last-question budget"
  );
  const highQuestion = question({ urgency_budget: "last" });
  assert.equal(
    controller.acceptResponse(controller.captureTurn(), highQuestion, responseContext("session_01", "high")).urgency_budget,
    "last"
  );

  const staleOrigin = controller.captureTurn();
  controller.reset();
  assert.throws(
    () => controller.acceptResponse(staleOrigin, null, responseContext("session_01")),
    /过期/
  );

  const sessionOrigin = controller.captureTurn();
  context = { userId: "u01", sessionId: "session_02" };
  assert.throws(
    () => controller.acceptResponse(sessionOrigin, null, responseContext("session_01")),
    /会话|过期/
  );
  context = { userId: "u01", sessionId: "session_01" };

  controller.reset();
  const bindingOrigin = controller.captureTurn();
  controller.acceptResponse(bindingOrigin, question(), responseContext());
  const mismatchOrigin = controller.captureTurn();
  assert.throws(
    () => controller.acceptResponse(mismatchOrigin, question({ gap_code: "color:black_vs_white" }), responseContext()),
    /绑定/
  );

  pendingPost = deferred();
  const staleSubmit = controller.submitOption(question().options[0].option_id);
  controller.reset();
  pendingPost.resolve(true);
  assert.equal(await staleSubmit, false, "a submit receipt after reset must not become current");

  const source = fs.readFileSync(path.join(__dirname, "preference_clarification_runtime.js"), "utf8");
  assert.doesNotMatch(source, /localStorage|sessionStorage|server_ranking_score|shopping_suggestions|\.sort\(.*outfit|\/memory\//);
  console.log("web S16 preference clarification exact-key/binding/async/singleflight/stale/neutral/high-urgency contract: PASS");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
