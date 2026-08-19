"use strict";

const assert = require("node:assert/strict");
const { IMAGE_MODEL, EXACT_REQUEST_WITH_CPA_TRACE } = require("./image_asset_runtime.js");
const { normalizeBatch, imageUrl } = require("./recommendation_preview_runtime.js");

const expected = {
  userId: "u01",
  previewRequestId: "preview-request-1",
  requestId: "dialogue-request-2",
  stylingSessionId: "session-1",
  outfits: [
    { outfit_id: "outfit-1", items: ["g001", "g002", "g003"] },
    { outfit_id: "outfit-2", items: ["g004", "g005", "g006"] }
  ]
};

const successProvider = () => ({
  status: "ok",
  attempted: true,
  requested_model: IMAGE_MODEL,
  transport_model: IMAGE_MODEL,
  resolved_model: null,
  model_reported: false,
  model_verified: false,
  request_model_pinned: true,
  cpa_trace_verified: true,
  verification_basis: EXACT_REQUEST_WITH_CPA_TRACE,
  degraded: false,
  reason_code: null
});

const fallbackProvider = () => ({
  status: "fallback",
  attempted: true,
  requested_model: IMAGE_MODEL,
  transport_model: IMAGE_MODEL,
  resolved_model: null,
  model_reported: false,
  model_verified: false,
  request_model_pinned: true,
  cpa_trace_verified: false,
  verification_basis: null,
  degraded: true,
  reason_code: "CPA_IMAGE_UNAVAILABLE"
});

const fidelity = () => ({
  identity: "not_assessed",
  garment: "style_color_reference_only",
  fit: "unknown",
  material: "unknown",
  drape: "unknown"
});

const aiLabel = () => ({
  generated: true,
  display_label: "AI生成的2D视觉参考",
  metadata_status: "http_header_and_response",
  disclaimer: "仅供风格与配色参考，不代表精确尺码、面料或垂坠"
});

function succeeded(outfit, index) {
  const previewId = `preview-${index}`;
  return {
    outfit_id: outfit.outfit_id,
    preview_id: previewId,
    status: "succeeded",
    image_url: `/recommend/previews/static-2d/${previewId}/image?user_id=u01&styling_session_id=session-1`,
    owned_garment_ids: [...outfit.items],
    provider: successProvider(),
    fidelity: fidelity(),
    ai_label: aiLabel(),
    fallback: null,
    trace_id: `trace-${index}`
  };
}

function degraded(outfit, index) {
  return {
    outfit_id: outfit.outfit_id,
    preview_id: `preview-${index}`,
    status: "degraded",
    image_url: null,
    owned_garment_ids: [...outfit.items],
    provider: fallbackProvider(),
    fidelity: fidelity(),
    ai_label: aiLabel(),
    fallback: { mode: "wardrobe_cards" },
    trace_id: `trace-${index}`
  };
}

function envelope(previews) {
  return {
    api_version: "r1_demo_v1",
    preview_request_id: expected.previewRequestId,
    request_id: expected.requestId,
    styling_session_id: expected.stylingSessionId,
    previews
  };
}

const ok = normalizeBatch(envelope(expected.outfits.map(succeeded)), expected);
assert.deepEqual(ok.map((entry) => entry.status), ["succeeded", "succeeded"]);
assert.equal(imageUrl(ok[0].preview, expected.userId), "/recommend/previews/static-2d/preview-0/image?user_id=u01&styling_session_id=session-1");

const oneFailure = normalizeBatch(envelope([
  succeeded(expected.outfits[0], 0),
  degraded(expected.outfits[1], 1)
]), expected);
assert.deepEqual(oneFailure.map((entry) => entry.status), ["succeeded", "degraded"], "one image failure must not remove the other successful image");

const wrongModel = succeeded(expected.outfits[1], 1);
wrongModel.provider.transport_model = "grok-4.6-high";
const wrongModelResult = normalizeBatch(envelope([succeeded(expected.outfits[0], 0), wrongModel]), expected);
assert.deepEqual(wrongModelResult.map((entry) => entry.status), ["succeeded", "failed"], "wrong model must fail only that preview");

const wrongOutfit = succeeded(expected.outfits[1], 1);
wrongOutfit.outfit_id = "outfit-from-another-request";
const wrongOutfitResult = normalizeBatch(envelope([succeeded(expected.outfits[0], 0), wrongOutfit]), expected);
assert.deepEqual(wrongOutfitResult.map((entry) => entry.status), ["succeeded", "failed"], "wrong outfit must not be accepted");

const wrongOwnerUrl = succeeded(expected.outfits[0], 0);
wrongOwnerUrl.image_url = "/recommend/previews/static-2d/preview-0/image?user_id=u02&styling_session_id=session-1";
const wrongOwnerResult = normalizeBatch(envelope([wrongOwnerUrl, succeeded(expected.outfits[1], 1)]), expected);
assert.deepEqual(wrongOwnerResult.map((entry) => entry.status), ["failed", "succeeded"], "cross-owner image URL must be rejected without deleting the other card");

assert.throws(
  () => normalizeBatch({ ...envelope(expected.outfits.map(succeeded)), styling_session_id: "session-other" }, expected),
  /owner\/session\/request\/数量不匹配/,
  "cross-session batch must fail closed"
);

console.log("web recommendation preview S14 success/degraded/cross-owner/wrong-model/wrong-outfit contract: PASS");
