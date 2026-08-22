"use strict";

const assert = require("node:assert/strict");
const { createMemoryCandidateFlowController } = require("./app.js");
const candidateRuntime = require("./memory_candidate_runtime.js");
const dialogueRuntime = require("./dialogue_runtime.js");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function candidate(id, allowedActions = ["remember", "session_only", "reject", "rephrase"]) {
  return Object.freeze({
    candidate_id: id,
    confirmation_copy: `要长期记住候选 ${id} 吗？`,
    confidence_band: "high",
    conflict_copy: null,
    allowed_actions: Object.freeze([...allowedActions])
  });
}

function extraction(requestId, status, candidates) {
  return { request_id: requestId, status, candidates, trace_id: `trace_${requestId}` };
}

function decision(card, action) {
  const status = action === "remember" ? "committed" : action === "session_only" ? "session_only" : "rejected";
  return {
    candidate_id: card.candidate_id,
    decision: action,
    status,
    record_id: action === "remember" ? `mem_${card.candidate_id}` : null,
    trace_id: `trace_${card.candidate_id}_${action}`
  };
}

function cloneView(view) {
  return {
    candidates: [...view.candidates],
    resultStatus: view.resultStatus,
    pendingIds: new Set(view.pendingIds),
    sessionOnlyCount: view.sessionOnlyCount,
    writeStatus: view.writeStatus
  };
}

function makeHarness(initialText = "我偏爱藏青色") {
  const input = {
    value: initialText,
    disabled: false,
    focusCount: 0,
    focus() { if (!this.disabled) this.focusCount += 1; }
  };
  const context = { userId: "u01", sessionId: "styling_01", namespace: "stylist" };
  let view = { candidates: [], resultStatus: "idle", pendingIds: new Set(), sessionOnlyCount: 0, writeStatus: "" };
  const requests = [];
  let failNextRender = false;
  let renderCount = 0;
  const controller = createMemoryCandidateFlowController({
    input,
    getContext: () => ({ ...context }),
    request: (path, options) => {
      const wait = deferred();
      requests.push({ path, options, wait });
      return wait.promise;
    },
    normalizeExtraction: candidateRuntime.normalizeExtractionResponse,
    normalizeDecision: candidateRuntime.normalizeCandidateDecision,
    candidateActions: candidateRuntime.candidateActions,
    shouldSubmitKey: dialogueRuntime.shouldSubmitComposerKey,
    createId: (() => { let index = 0; return (prefix) => `${prefix}_${++index}`; })(),
    snapshotView: () => cloneView(view),
    applyView: (next) => {
      view = cloneView(next);
      renderCount += 1;
      if (failNextRender) {
        failNextRender = false;
        throw new Error("render failed");
      }
    },
    restoreView: (previous) => { view = cloneView(previous); renderCount += 1; },
    clearPending: (candidateIds) => {
      const pendingIds = new Set(view.pendingIds);
      candidateIds.forEach((candidateId) => pendingIds.delete(candidateId));
      view = cloneView({ ...view, pendingIds });
      renderCount += 1;
    },
    setExtracting: (extracting) => { input.disabled = extracting; },
    onRemember: async () => {}
  });
  return {
    controller,
    input,
    context,
    requests,
    get view() { return view; },
    get renderCount() { return renderCount; },
    failRender() { failNextRender = true; },
    seedView(next) { view = cloneView({ ...view, ...next }); }
  };
}

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
}

(async () => {
  {
    const h = makeHarness();
    const submitted = h.controller.submit({ preventDefault() {} });
    assert.equal(h.input.value, "我偏爱藏青色", "input must remain while extraction is pending");
    const requestId = JSON.parse(h.requests[0].options.body).request_id;
    h.requests[0].wait.resolve(extraction(requestId, "ready", [candidate("mcand_ready")]));
    assert.equal(await submitted, true);
    assert.equal(h.input.value, "", "ready + non-empty + rendered cards clears the composer");
    assert.deepEqual(h.view.candidates.map((item) => item.candidate_id), ["mcand_ready"]);
  }

  for (const status of ["blocked", "needs_rephrase"]) {
    const h = makeHarness(`原文-${status}`);
    const submitted = h.controller.submit({ preventDefault() {} });
    const requestId = JSON.parse(h.requests[0].options.body).request_id;
    h.requests[0].wait.resolve(extraction(requestId, status, []));
    assert.equal(await submitted, true);
    assert.equal(h.input.value, `原文-${status}`, `${status} must preserve source text`);
    assert.equal(h.input.focusCount, 1, `${status} must restore focus`);
  }

  {
    const h = makeHarness("传输失败原文");
    const submitted = h.controller.submit({ preventDefault() {} });
    h.requests[0].wait.reject(new Error("network unavailable"));
    assert.equal(await submitted, false);
    assert.equal(h.input.value, "传输失败原文");
    assert.equal(h.input.focusCount, 1);
  }

  {
    const h = makeHarness("校验失败原文");
    const submitted = h.controller.submit({ preventDefault() {} });
    const requestId = JSON.parse(h.requests[0].options.body).request_id;
    h.requests[0].wait.resolve({ ...extraction(requestId, "ready", [candidate("mcand_bad")]), provider_body: "private" });
    assert.equal(await submitted, false);
    assert.equal(h.input.value, "校验失败原文");
    assert.equal(h.input.focusCount, 1);
  }

  {
    const h = makeHarness("渲染失败原文");
    const old = candidate("mcand_old", ["remember", "reject", "rephrase"]);
    h.seedView({ candidates: [old], resultStatus: "ready" });
    h.failRender();
    const submitted = h.controller.submit({ preventDefault() {} });
    const requestId = JSON.parse(h.requests[0].options.body).request_id;
    h.requests[0].wait.resolve(extraction(requestId, "ready", [candidate("mcand_new")]));
    assert.equal(await submitted, false);
    assert.equal(h.input.value, "渲染失败原文");
    assert.equal(h.input.focusCount, 1);
    assert.deepEqual(h.view.candidates.map((item) => item.candidate_id), ["mcand_old"], "render failure rolls view back");
  }

  {
    const h = makeHarness();
    let submits = 0;
    const form = { requestSubmit() { submits += 1; } };
    const event = (overrides = {}) => ({ key: "Enter", shiftKey: false, isComposing: false, keyCode: 13, prevented: false, preventDefault() { this.prevented = true; }, ...overrides });
    const enter = event();
    h.controller.handleComposerKey(enter, form);
    assert.equal(submits, 1);
    assert.equal(enter.prevented, true);
    h.controller.handleComposerKey(event({ shiftKey: true }), form);
    h.controller.handleComposerKey(event({ isComposing: true }), form);
    h.controller.handleComposerKey(event({ keyCode: 229 }), form);
    assert.equal(submits, 1, "Shift+Enter and IME must not submit");
  }

  {
    const h = makeHarness();
    const first = h.controller.submit({ preventDefault() {} });
    assert.equal(await h.controller.submit({ preventDefault() {} }), false);
    assert.equal(h.requests.length, 1, "extract is singleflight");
    const requestId = JSON.parse(h.requests[0].options.body).request_id;
    h.requests[0].wait.resolve(extraction(requestId, "ready", [candidate("mcand_single")]));
    await first;
  }

  {
    const h = makeHarness();
    const a = candidate("mcand_a");
    const b = candidate("mcand_b");
    h.seedView({ candidates: [a, b], resultStatus: "ready" });
    const decideA = h.controller.decide(a, "session_only");
    assert.equal(await h.controller.decide(a, "reject"), false, "one candidate has one pending decision");
    const decideB = h.controller.decide(b, "reject");
    assert.equal(h.requests.length, 2, "another candidate remains independently actionable");
    h.requests[1].wait.resolve(decision(b, "reject"));
    assert.equal(await decideB, true);
    assert.deepEqual(h.view.candidates.map((item) => item.candidate_id), ["mcand_a"]);
    h.requests[0].wait.resolve(decision(a, "session_only"));
    assert.equal(await decideA, true);
    assert.deepEqual(h.view.candidates, []);
    assert.equal(h.view.sessionOnlyCount, 1);
  }

  {
    const h = makeHarness();
    const retryCard = candidate("mcand_retry");
    h.seedView({ candidates: [retryCard], resultStatus: "ready" });
    const first = h.controller.decide(retryCard, "reject");
    const firstKey = JSON.parse(h.requests[0].options.body).idempotency_key;
    h.requests[0].wait.reject(new Error("response lost"));
    assert.equal(await first, false);
    const retry = h.controller.decide(retryCard, "reject");
    const retryKey = JSON.parse(h.requests[1].options.body).idempotency_key;
    assert.equal(retryKey, firstKey, "same candidate/action retry must reuse its idempotency key");
    h.requests[1].wait.resolve(decision(retryCard, "reject"));
    assert.equal(await retry, true);
  }

  {
    const h = makeHarness();
    const namespaceCard = candidate("mcand_namespace");
    h.seedView({ candidates: [namespaceCard], resultStatus: "ready", writeStatus: "等待决定" });
    const pending = h.controller.decide(namespaceCard, "reject");
    assert.deepEqual([...h.view.pendingIds], [namespaceCard.candidate_id]);
    assert.equal(Object.hasOwn(JSON.parse(h.requests[0].options.body), "namespace"), false, "decision API has no namespace authority field");
    h.context.namespace = "shared";
    h.requests[0].wait.resolve(decision(namespaceCard, "reject"));
    assert.equal(await pending, true, "namespace is extraction-only and must not stale a valid decision receipt");
    assert.deepEqual(h.view.candidates, []);
    assert.deepEqual([...h.view.pendingIds], []);
    assert.equal(h.view.writeStatus, "这条候选已拒绝，不会写入。");
  }

  for (const authorityChange of ["userId", "sessionId"]) {
    const h = makeHarness();
    const staleAuthority = candidate(`mcand_stale_${authorityChange}`);
    h.seedView({ candidates: [staleAuthority], resultStatus: "ready", writeStatus: "新任务原状态" });
    const pending = h.controller.decide(staleAuthority, "session_only");
    const beforeFocus = h.input.focusCount;
    h.context[authorityChange] = authorityChange === "userId" ? "u02" : "styling_02";
    h.requests[0].wait.resolve(decision(staleAuthority, "session_only"));
    assert.equal(await pending, false, `${authorityChange} change must stale the old receipt`);
    assert.deepEqual(h.view.candidates.map((item) => item.candidate_id), [staleAuthority.candidate_id]);
    assert.deepEqual([...h.view.pendingIds], [], `${authorityChange} stale return must clear pending`);
    assert.equal(h.view.sessionOnlyCount, 0);
    assert.equal(h.view.writeStatus, "新任务原状态");
    assert.equal(h.input.focusCount, beforeFocus);
  }

  {
    const h = makeHarness();
    const stale = candidate("mcand_stale");
    h.seedView({ candidates: [stale], resultStatus: "ready" });
    const pending = h.controller.decide(stale, "rephrase");
    const signal = h.requests[0].options.signal;
    const beforeResetFocus = h.input.focusCount;
    h.controller.reset();
    assert.deepEqual([...h.view.pendingIds], [], "reset must synchronously clear pending UI state");
    h.context.sessionId = "styling_02";
    const newTaskCard = candidate(stale.candidate_id, ["remember", "reject", "rephrase"]);
    h.seedView({
      candidates: [newTaskCard],
      resultStatus: "ready",
      pendingIds: new Set(),
      sessionOnlyCount: 2,
      writeStatus: "新任务状态"
    });
    const newTaskPending = h.controller.decide(newTaskCard, "reject");
    assert.equal(signal.aborted, true, "reset aborts the pending mutation");
    h.requests[0].wait.resolve(decision(stale, "rephrase"));
    assert.equal(await pending, false);
    assert.equal(h.input.focusCount, beforeResetFocus, "stale rephrase cannot focus the new session composer");
    assert.deepEqual(h.view.candidates.map((item) => item.candidate_id), [newTaskCard.candidate_id]);
    assert.deepEqual([...h.view.pendingIds], [newTaskCard.candidate_id], "old finally cannot clear a new generation pending card with the same ID");
    assert.equal(h.view.sessionOnlyCount, 2);
    assert.equal(h.view.writeStatus, "新任务状态");
    h.requests[1].wait.resolve(decision(newTaskCard, "reject"));
    assert.equal(await newTaskPending, true);
  }

  assert.deepEqual(
    candidateRuntime.candidateActions(candidate("mcand_no_session", ["remember", "reject", "rephrase"])),
    ["remember", "reject", "rephrase"],
    "the browser does not add session_only"
  );
  {
    const h = makeHarness();
    const noSession = candidate("mcand_no_session_flow", ["remember", "reject", "rephrase"]);
    h.seedView({ candidates: [noSession], resultStatus: "ready" });
    assert.equal(await h.controller.decide(noSession, "session_only"), false);
    assert.equal(h.requests.length, 0, "the actual flow cannot expand server actions");
  }

  await flush();
  console.log("web S16 async DOM candidate flow/race contract: PASS");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
