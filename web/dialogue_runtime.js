(function installDialogueRuntime(root, factory) {
  "use strict";

  const runtime = factory();
  if (typeof module === "object" && module.exports) module.exports = runtime;
  if (root) root.PROFAGENT_DIALOGUE_RUNTIME = runtime;
}(typeof window !== "undefined" ? window : globalThis, function createDialogueRuntime() {
  "use strict";

  const CPA_REQUESTED_MODEL = "grok4.6";
  const CPA_TRANSPORT_MODEL = "grok-4.6-high";
  const CPA_RESOLVED_MODELS = new Set(["grok-4.6-high", "grok-4.6-build"]);

  function hasExactCpaModel(provider) {
    return provider?.requested_model === CPA_REQUESTED_MODEL
      && provider.transport_model === CPA_TRANSPORT_MODEL
      && CPA_RESOLVED_MODELS.has(provider.resolved_model);
  }

  function isStrictCpaReply(provider) {
    return hasExactCpaModel(provider)
      && provider.generation_source === "cpa"
      && provider.status === "ok"
      && provider.model_verified === true
      && provider.degraded === false;
  }

  function createInFlightGuard() {
    let generation = 0;
    let active = null;

    return Object.freeze({
      begin(requestId, controller) {
        if (active !== null) return null;
        active = Object.freeze({ requestId, controller, generation });
        return active;
      },
      isCurrent(token) {
        return active === token
          && token?.generation === generation
          && token.controller?.signal?.aborted !== true;
      },
      isGenerationCurrent(token) {
        return token?.generation === generation;
      },
      finish(token) {
        if (active !== token) return false;
        active = null;
        return true;
      },
      cancel() {
        generation += 1;
        const cancelled = active;
        active = null;
        if (cancelled && cancelled.controller?.signal?.aborted !== true) cancelled.controller.abort();
        return cancelled !== null;
      },
      get inFlight() {
        return active !== null;
      }
    });
  }

  function selectAssistantText(message, provider, fallbackTransform) {
    if (isStrictCpaReply(provider)) return message;
    return fallbackTransform(message);
  }

  function shouldSubmitComposerKey(event) {
    return event?.key === "Enter"
      && event.shiftKey !== true
      && event.isComposing !== true
      && event.keyCode !== 229;
  }

  function normalizeComposerMessage(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  return Object.freeze({
    createInFlightGuard,
    hasExactCpaModel,
    isStrictCpaReply,
    normalizeComposerMessage,
    selectAssistantText,
    shouldSubmitComposerKey
  });
}));
