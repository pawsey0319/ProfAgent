"use strict";

const assert = require("node:assert/strict");
const {
  createInFlightGuard,
  hasExactCpaModel,
  isStrictCpaReply,
  selectAssistantText,
  shouldSubmitComposerKey
} = require("./dialogue_runtime.js");

async function run() {
  assert.equal(shouldSubmitComposerKey({ key: "Enter", shiftKey: false, isComposing: false }), true);
  assert.equal(shouldSubmitComposerKey({ key: "Enter", shiftKey: true, isComposing: false }), false, "Shift+Enter must remain a newline");
  assert.equal(shouldSubmitComposerKey({ key: "Enter", shiftKey: false, isComposing: true }), false, "IME composition Enter must not submit");
  assert.equal(shouldSubmitComposerKey({ key: "Enter", shiftKey: false, isComposing: false, keyCode: 229 }), false, "IME keyCode 229 must not submit");
  assert.equal(shouldSubmitComposerKey({ key: "a", shiftKey: false, isComposing: false }), false);

  const guard = createInFlightGuard();
  let posts = 0;
  let inserted = 0;
  let resolveFirst;

  const start = (entry, requestId = "request-1") => {
    if (guard.inFlight) return null;
    const controller = new AbortController();
    const token = guard.begin(requestId, controller);
    if (!token) return null;
    posts += 1;
    return { entry, token, controller };
  };

  const first = start("submit");
  assert.ok(first);
  assert.equal(start("double-click"), null);
  assert.equal(start("ctrl-enter"), null);
  assert.equal(start("quick-action"), null);
  assert.equal(posts, 1, "all re-entry paths must produce zero second POSTs");

  const lateResponse = new Promise((resolve) => { resolveFirst = resolve; });
  const applyIfCurrent = lateResponse.then((message) => {
    if (guard.isCurrent(first.token)) inserted += 1;
    return message;
  });
  assert.equal(guard.cancel(), true);
  assert.equal(first.controller.signal.aborted, true);
  resolveFirst("old response");
  await applyIfCurrent;
  assert.equal(inserted, 0, "a response from before reset must not enter the new UI generation");

  const legalMessage = "我不会评价你的身材，只讨论衣服版型";
  const strictProvider = {
    status: "ok",
    generation_source: "cpa",
    requested_model: "grok4.6",
    transport_model: "grok-4.6-high",
    model_verified: true,
    degraded: false,
    resolved_model: "grok-4.6-build"
  };
  assert.equal(hasExactCpaModel(strictProvider), true);
  assert.equal(isStrictCpaReply(strictProvider), true);
  assert.equal(isStrictCpaReply({ ...strictProvider, resolved_model: "grok-4.6-high" }), true);
  const rejectedModelMatrix = [
    { name: "legacy logical 4.5", provider: { ...strictProvider, requested_model: "grok4.5" } },
    { name: "legacy transport 4.5", provider: { ...strictProvider, transport_model: "grok-4.5-high" } },
    { name: "legacy resolved 4.5", provider: { ...strictProvider, resolved_model: "grok-4.5-build" } },
    { name: "unsuffixed transport 4.6", provider: { ...strictProvider, transport_model: "grok-4.6" } },
    { name: "unsuffixed resolved 4.6", provider: { ...strictProvider, resolved_model: "grok-4.6" } },
    { name: "transport prefix", provider: { ...strictProvider, transport_model: "grok-4.6-high-preview" } },
    { name: "other transport", provider: { ...strictProvider, transport_model: "grok-4.6-fast" } },
    { name: "resolved prefix", provider: { ...strictProvider, resolved_model: "grok-4.6-high-preview" } },
    { name: "other resolved", provider: { ...strictProvider, resolved_model: "grok-4.6-fast" } },
    { name: "wrong logical request", provider: { ...strictProvider, requested_model: "grok-4.6-high" } },
    { name: "unverified", provider: { ...strictProvider, model_verified: false } },
    { name: "degraded", provider: { ...strictProvider, degraded: true } }
  ];
  for (const entry of rejectedModelMatrix) {
    assert.equal(isStrictCpaReply(entry.provider), false, `${entry.name} must not be labelled CPA`);
    assert.equal(
      selectAssistantText(legalMessage, entry.provider, () => "local fallback"),
      "local fallback",
      `${entry.name} must not bypass the fallback text path`
    );
  }
  const fallbackProvider = {
    ...strictProvider,
    status: "fallback",
    generation_source: "local_fallback",
    attempted: true,
    model_verified: false,
    degraded: true,
    resolved_model: null
  };
  assert.equal(isStrictCpaReply(fallbackProvider), false);
  assert.equal(selectAssistantText(legalMessage, fallbackProvider, () => "local fallback"), "local fallback");
  assert.equal(isStrictCpaReply(fallbackProvider) ? "CPA 生成" : "本地回复", "本地回复");
  const displayed = selectAssistantText(legalMessage, strictProvider, () => "被错误替换");
  const label = isStrictCpaReply(strictProvider) ? "CPA 生成" : "本地回复";
  assert.equal(displayed, legalMessage, "verified CPA prose must remain byte-for-byte unchanged");
  assert.equal(label, "CPA 生成");

  const formal = start("formal-response", "request-2");
  let controlsLocked = true;
  let reply = null;
  let source = "CPA 正在生成";
  reply = legalMessage;
  source = "CPA 生成";
  assert.equal(guard.finish(formal.token), true);
  controlsLocked = false;

  const traceGuard = createInFlightGuard();
  const traceController = new AbortController();
  const traceToken = traceGuard.begin("trace-2", traceController);
  let rejectTrace;
  const traceFailure = new Promise((resolve, reject) => { rejectTrace = reject; })
    .catch(() => null);
  assert.equal(controlsLocked, false, "formal response must unlock controls before deferred trace settles");
  rejectTrace(new Error("trace unavailable"));
  await traceFailure;
  assert.equal(reply, legalMessage, "trace failure must not replace the formal reply");
  assert.equal(source, "CPA 生成", "trace failure must not change the formal reply source");
  traceGuard.finish(traceToken);

  let resolveOldTrace;
  let traceWrites = 0;
  const oldTraceController = new AbortController();
  const oldTraceToken = traceGuard.begin("trace-old", oldTraceController);
  const oldTrace = new Promise((resolve) => { resolveOldTrace = resolve; }).then(() => {
    if (traceGuard.isCurrent(oldTraceToken) && guard.isGenerationCurrent(formal.token)) traceWrites += 1;
  });
  traceGuard.cancel();
  guard.cancel();
  resolveOldTrace();
  await oldTrace;
  assert.equal(oldTraceController.signal.aborted, true);
  assert.equal(traceWrites, 0, "reset must prevent an old trace from writing into the new UI");

  console.log("web dialogue runtime S14 Enter/Shift/IME + exact-4.6-model/guard/reset/CPA-text/trace contract: PASS");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
