(function installRecommendationPreviewRuntime(root, factory) {
  "use strict";

  const imageRuntime = root?.PROFAGENT_IMAGE_ASSET_RUNTIME
    || (typeof require === "function" ? require("./image_asset_runtime.js") : null);
  const runtime = factory(imageRuntime);
  if (typeof module === "object" && module.exports) module.exports = runtime;
  if (root) root.PROFAGENT_RECOMMENDATION_PREVIEW_RUNTIME = runtime;
}(typeof window !== "undefined" ? window : globalThis, function createRecommendationPreviewRuntime(imageRuntime) {
  "use strict";

  if (!imageRuntime) throw new Error("Image asset runtime 未加载");
  const RAW_RECEIPT_FIELDS = Object.freeze(["cpa_trace_id", "x-cpa-trace-id", "cpa_receipt", "receipt"]);
  const AI_DISPLAY_LABEL = "AI生成的2D视觉参考";
  const AI_DISCLAIMER = "仅供风格与配色参考，不代表精确尺码、面料或垂坠";

  function strictIdList(value) {
    if (!Array.isArray(value) || value.some((id) => typeof id !== "string" || !id.trim())) return null;
    const unique = [...new Set(value)];
    return unique.length === value.length ? unique : null;
  }

  function sameIdSet(left, right) {
    const a = [...new Set(left)].sort();
    const b = [...new Set(right)].sort();
    return a.length === b.length && a.every((id, index) => id === b[index]);
  }

  function validateProvider(raw) {
    if (!raw || typeof raw !== "object" || !["ok", "fallback"].includes(raw.status)) throw new Error("图片 Provider 状态不完整");
    if (RAW_RECEIPT_FIELDS.some((field) => raw[field] !== undefined)) throw new Error("图片 Provider 泄露原始 CPA receipt");
    if (raw.requested_model !== imageRuntime.IMAGE_MODEL || raw.transport_model !== imageRuntime.IMAGE_MODEL) throw new Error("图片 Provider 模型合同不匹配");
    if (typeof raw.attempted !== "boolean") throw new Error("图片 Provider 尝试状态不完整");
    const successBasis = raw.status === "ok" ? imageRuntime.staticImageSuccessBasis(raw) : null;
    if (raw.status === "ok" && successBasis === null) throw new Error("图片 Provider 成功来源不可信");
    if (raw.status === "fallback" && (
      raw.degraded !== true
      || raw.model_verified !== false
      || raw.resolved_model !== null
      || raw.verification_basis !== null
      || typeof raw.request_model_pinned !== "boolean"
      || typeof raw.cpa_trace_verified !== "boolean"
      || typeof raw.model_reported !== "boolean"
      || typeof raw.reason_code !== "string"
      || !raw.reason_code
    )) throw new Error("图片 Provider 失败状态不完整");
    return Object.freeze({ ...raw, provenance_basis: successBasis });
  }

  function isExactImageUrl(value, expected) {
    if (typeof value !== "string" || !value.startsWith("/")) return false;
    let parsed;
    try {
      parsed = new URL(value, "https://profagent.invalid");
    } catch (_error) {
      return false;
    }
    const keys = [...parsed.searchParams.keys()].sort();
    return parsed.origin === "https://profagent.invalid"
      && parsed.pathname === `/recommend/previews/static-2d/${encodeURIComponent(expected.previewId)}/image`
      && keys.length === 2
      && keys[0] === "styling_session_id"
      && keys[1] === "user_id"
      && parsed.searchParams.get("user_id") === expected.userId
      && parsed.searchParams.get("styling_session_id") === expected.stylingSessionId;
  }

  function normalizeItem(raw, expected) {
    if (!raw || typeof raw !== "object" || raw.outfit_id !== expected.outfitId || !raw.preview_id) throw new Error("推荐 2D 回执 outfit 或 preview 标识不匹配");
    if (!["succeeded", "degraded", "failed"].includes(raw.status)) throw new Error("推荐 2D 回执状态不合法");
    const ids = strictIdList(raw.owned_garment_ids);
    if (ids === null || !sameIdSet(ids, expected.itemIds)) throw new Error("推荐 2D 回执衣物集合未通过 owner 白名单复核");
    const provider = validateProvider(raw.provider);
    if (raw.status === "succeeded" && provider.status !== "ok") throw new Error("推荐 2D 成功回执不得使用失败 Provider");
    if (raw.status !== "succeeded" && provider.status !== "fallback") throw new Error("推荐 2D 失败回执不得冒充 Provider 成功");
    if (!raw.fidelity || raw.fidelity.identity !== "not_assessed"
      || raw.fidelity.garment !== "style_color_reference_only"
      || raw.fidelity.fit !== "unknown"
      || raw.fidelity.material !== "unknown"
      || raw.fidelity.drape !== "unknown") throw new Error("推荐 2D 回执 fidelity 不可信");
    if (!raw.ai_label || raw.ai_label.generated !== true
      || raw.ai_label.display_label !== AI_DISPLAY_LABEL
      || raw.ai_label.disclaimer !== AI_DISCLAIMER
      || raw.ai_label.metadata_status !== "http_header_and_response") throw new Error("推荐 2D 回执 AI 标识不完整");
    if (!raw.trace_id) throw new Error("推荐 2D 回执缺少 Trace");
    if (raw.status === "succeeded") {
      if (!isExactImageUrl(raw.image_url, { ...expected, previewId: raw.preview_id })) throw new Error("推荐 2D 图片 URL 未通过 owner/session 路径复核");
    } else if (raw.image_url !== null || !raw.fallback) throw new Error("推荐 2D 失败回执不得携带图片或缺少 fallback");
    return Object.freeze({
      outfit_id: raw.outfit_id,
      preview_id: raw.preview_id,
      status: raw.status,
      styling_session_id: expected.stylingSessionId,
      request_id: expected.requestId,
      preview_request_id: expected.previewRequestId,
      owned_garment_ids: ids,
      provider,
      fidelity: Object.freeze({ ...raw.fidelity }),
      ai_label: Object.freeze({ ...raw.ai_label }),
      fallback: raw.fallback || null,
      trace_id: raw.trace_id
    });
  }

  function normalizeBatch(raw, expected) {
    if (!raw || typeof raw !== "object" || raw.api_version !== "r1_demo_v1") throw new Error("推荐 2D 批量端点未返回可信对象");
    if (raw.preview_request_id !== expected.previewRequestId
      || raw.request_id !== expected.requestId
      || raw.styling_session_id !== expected.stylingSessionId
      || !Array.isArray(raw.previews)
      || raw.previews.length !== expected.outfits.length) throw new Error("推荐 2D 批量回执 owner/session/request/数量不匹配");
    return expected.outfits.map((outfit, index) => {
      try {
        const preview = normalizeItem(raw.previews[index], {
          userId: expected.userId,
          outfitId: outfit.outfit_id,
          itemIds: outfit.items,
          stylingSessionId: expected.stylingSessionId,
          requestId: expected.requestId,
          previewRequestId: expected.previewRequestId
        });
        return Object.freeze({ status: preview.status, preview });
      } catch (error) {
        return Object.freeze({ status: "failed", preview: null, reason: error.message });
      }
    });
  }

  function imageUrl(preview, userId) {
    const query = new URLSearchParams({ user_id: userId, styling_session_id: preview.styling_session_id });
    return `/recommend/previews/static-2d/${encodeURIComponent(preview.preview_id)}/image?${query.toString()}`;
  }

  return Object.freeze({ AI_DISPLAY_LABEL, AI_DISCLAIMER, normalizeBatch, imageUrl, isExactImageUrl });
}));
