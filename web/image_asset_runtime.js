(function installImageAssetRuntime(root, factory) {
  "use strict";

  const runtime = factory();
  if (typeof module === "object" && module.exports) module.exports = runtime;
  if (root) root.PROFAGENT_IMAGE_ASSET_RUNTIME = runtime;
}(typeof window !== "undefined" ? window : globalThis, function createImageAssetRuntime() {
  "use strict";

  const IMAGE_MODEL = "grok-imagine-image-quality";
  const CATALOG_ASSET_VERSION = "wardrobe_generated_v1_s12r2";
  const READY_MEDIA_TYPES = new Set(["image/png"]);
  const CATALOG_AI_LABEL = "AI生成目录参考";
  const CATALOG_DISCLAIMER = "仅供品类、风格与配色参考，不代表真实尺码、面料或垂坠";
  const SHA256_PATTERN = /^[a-f0-9]{64}$/;
  const REPORTED_MODEL_EXACT = "reported_model_exact";
  const EXACT_REQUEST_WITH_CPA_TRACE = "exact_request_with_cpa_trace";
  const BATCH_EXACT_REQUEST_CONTRACT = "batch_exact_request_contract";
  const RAW_RECEIPT_FIELDS = Object.freeze(["cpa_trace_id", "x-cpa-trace-id", "cpa_receipt", "receipt"]);
  const catalogImageLifecycleTokens = new WeakMap();

  function hasPinnedSuccessfulRequest(provider) {
    return provider?.requested_model === IMAGE_MODEL
      && provider.transport_model === IMAGE_MODEL
      && provider.status === "ok"
      && provider.attempted === true
      && provider.degraded === false
      && provider.request_model_pinned === true
      && provider.reason_code === null
      && RAW_RECEIPT_FIELDS.every((field) => provider[field] === undefined);
  }

  function staticImageSuccessBasis(provider) {
    if (
      !hasPinnedSuccessfulRequest(provider)
      || typeof provider.cpa_trace_verified !== "boolean"
      || provider.provenance_verified !== undefined
    ) return null;
    if (
      provider.verification_basis === REPORTED_MODEL_EXACT
      && provider.cpa_trace_verified === true
      && provider.model_reported === true
      && provider.model_verified === true
      && provider.resolved_model === IMAGE_MODEL
    ) return REPORTED_MODEL_EXACT;
    if (
      provider.verification_basis === EXACT_REQUEST_WITH_CPA_TRACE
      && provider.cpa_trace_verified === true
      && provider.model_reported === false
      && provider.model_verified === false
      && provider.resolved_model === null
    ) return EXACT_REQUEST_WITH_CPA_TRACE;
    return null;
  }

  function catalogImageSuccessBasis(provider) {
    if (!hasPinnedSuccessfulRequest(provider) || provider.cpa_trace_verified !== undefined) return null;
    return provider.verification_basis === BATCH_EXACT_REQUEST_CONTRACT
      && provider.provenance_verified === true
      && provider.model_reported === false
      && provider.model_verified === false
      && provider.resolved_model === null
      ? BATCH_EXACT_REQUEST_CONTRACT
      : null;
  }

  function imageProvenanceLabel(basis) {
    if (basis === REPORTED_MODEL_EXACT) return "实际模型已精确回报";
    if (basis === EXACT_REQUEST_WITH_CPA_TRACE || basis === BATCH_EXACT_REQUEST_CONTRACT) {
      return "请求模型已固定，CPA 未回报实际模型";
    }
    return "图片来源未验证";
  }

  function isOwnerBoundCatalogImageUrl(value, userId, garmentId, assetVersion, contentSha256) {
    if (typeof value !== "string" || !value.startsWith("/")) return false;
    let parsed;
    try {
      parsed = new URL(value, "https://profagent.invalid");
    } catch (_error) {
      return false;
    }
    const expectedPath = `/wardrobe/${encodeURIComponent(garmentId)}/catalog-image`;
    const keys = [...parsed.searchParams.keys()].sort();
    return parsed.origin === "https://profagent.invalid"
      && parsed.pathname === expectedPath
      && keys.length === 3
      && keys[0] === "asset_version"
      && keys[1] === "content_sha256"
      && keys[2] === "user_id"
      && parsed.searchParams.get("user_id") === userId
      && assetVersion === CATALOG_ASSET_VERSION
      && parsed.searchParams.get("asset_version") === CATALOG_ASSET_VERSION
      && SHA256_PATTERN.test(contentSha256)
      && parsed.searchParams.get("content_sha256") === contentSha256;
  }

  function fallbackAsset(garmentId, status = "missing") {
    return Object.freeze({ garment_id: garmentId, status, displayable: false, image_url: null });
  }

  function normalizeCatalogAsset(raw, userId, garmentId) {
    if (!raw || typeof raw !== "object" || raw.garment_id !== garmentId) return fallbackAsset(garmentId, "failed");
    if (raw.status === "missing") {
      return raw.image_url === null ? fallbackAsset(garmentId) : fallbackAsset(garmentId, "failed");
    }
    if (raw.status === "failed") {
      return raw.image_url === null ? fallbackAsset(garmentId, "failed") : fallbackAsset(garmentId, "failed");
    }
    const aiLabelValid = raw.ai_label?.generated === true
      && raw.ai_label.display_label === CATALOG_AI_LABEL
      && raw.ai_label.disclaimer === CATALOG_DISCLAIMER;
    const promptEvidenceValid = (
      raw.prompt_hash_status === "recorded_at_generation"
      && SHA256_PATTERN.test(raw.prompt_sha256)
    ) || (
      raw.prompt_hash_status === "not_preserved"
      && raw.prompt_sha256 === null
    );
    const valid = raw.status === "ready"
      && raw.asset_version === CATALOG_ASSET_VERSION
      && READY_MEDIA_TYPES.has(raw.media_type)
      && catalogImageSuccessBasis(raw.provider) === BATCH_EXACT_REQUEST_CONTRACT
      && promptEvidenceValid
      && SHA256_PATTERN.test(raw.content_sha256)
      && aiLabelValid
      && isOwnerBoundCatalogImageUrl(raw.image_url, userId, garmentId, raw.asset_version, raw.content_sha256);
    if (!valid) return fallbackAsset(garmentId, "failed");
    return Object.freeze({
      garment_id: garmentId,
      status: "ready",
      displayable: true,
      asset_version: raw.asset_version,
      image_url: raw.image_url,
      media_type: raw.media_type,
      provider: Object.freeze({
        status: "ok",
        attempted: true,
        requested_model: raw.provider.requested_model,
        transport_model: raw.provider.transport_model,
        resolved_model: null,
        model_reported: false,
        model_verified: false,
        request_model_pinned: true,
        provenance_verified: true,
        verification_basis: BATCH_EXACT_REQUEST_CONTRACT,
        degraded: false,
        reason_code: null
      }),
      provenance_label: imageProvenanceLabel(BATCH_EXACT_REQUEST_CONTRACT),
      prompt_hash_status: raw.prompt_hash_status,
      prompt_sha256: raw.prompt_sha256,
      content_sha256: raw.content_sha256,
      ai_label: Object.freeze({
        generated: true,
        display_label: CATALOG_AI_LABEL,
        disclaimer: CATALOG_DISCLAIMER
      })
    });
  }

  function normalizeCatalogManifest(raw, userId, garmentIds) {
    if (
      !raw
      || typeof raw !== "object"
      || raw.api_version !== "r1_demo_v1"
      || raw.user_id !== userId
      || raw.manifest_version !== CATALOG_ASSET_VERSION
      || !Array.isArray(raw.assets)
    ) {
      throw new Error("catalog manifest owner or shape mismatch");
    }
    const allowed = new Set(garmentIds);
    const normalized = new Map([...allowed].map((garmentId) => [garmentId, fallbackAsset(garmentId)]));
    const seen = new Set();
    for (const asset of raw.assets) {
      const garmentId = asset?.garment_id;
      if (!allowed.has(garmentId)) continue;
      if (seen.has(garmentId)) {
        normalized.set(garmentId, fallbackAsset(garmentId, "failed"));
        continue;
      }
      seen.add(garmentId);
      normalized.set(garmentId, normalizeCatalogAsset(asset, userId, garmentId));
    }
    return Object.freeze({
      manifest_version: raw.manifest_version,
      assets: normalized
    });
  }

  function bindCatalogImageLifecycle({
    card,
    image,
    badge,
    swatch,
    statusNode,
    imageUrl,
    successText,
    failureText,
    schedule = (callback) => queueMicrotask(callback)
  }) {
    if (!card || !image || !badge || !swatch || !statusNode || typeof imageUrl !== "string") {
      throw new TypeError("catalog image lifecycle binding is incomplete");
    }
    const token = Object.freeze({});
    catalogImageLifecycleTokens.set(card, token);
    let settled = false;
    const isCurrent = () => catalogImageLifecycleTokens.get(card) === token;
    const cleanup = () => {
      image.removeEventListener("load", onLoad);
      image.removeEventListener("error", onError);
    };
    const onLoad = () => {
      if (settled || !isCurrent()) return;
      if (!(Number(image.naturalWidth) > 0)) {
        onError();
        return;
      }
      settled = true;
      cleanup();
      image.classList.remove("is-pending");
      image.classList.add("is-loaded");
      image.dataset.catalogNaturalWidth = String(image.naturalWidth);
      image.hidden = false;
      badge.hidden = false;
      swatch.hidden = true;
      statusNode.textContent = successText;
    };
    const onError = () => {
      if (settled || !isCurrent()) return;
      settled = true;
      cleanup();
      image.remove();
      badge.remove();
      swatch.hidden = false;
      statusNode.textContent = failureText;
    };

    image.hidden = false;
    image.loading = "eager";
    image.classList.add("is-pending");
    badge.hidden = true;
    image.addEventListener("load", onLoad);
    image.addEventListener("error", onError);
    image.src = imageUrl;
    schedule(() => {
      if (!settled && isCurrent() && image.complete === true) {
        if (Number(image.naturalWidth) > 0) onLoad();
        else onError();
      }
    });
    return Object.freeze({
      cancel() {
        if (!isCurrent()) return false;
        catalogImageLifecycleTokens.delete(card);
        cleanup();
        return true;
      }
    });
  }

  return Object.freeze({
    IMAGE_MODEL,
    CATALOG_ASSET_VERSION,
    REPORTED_MODEL_EXACT,
    EXACT_REQUEST_WITH_CPA_TRACE,
    BATCH_EXACT_REQUEST_CONTRACT,
    staticImageSuccessBasis,
    catalogImageSuccessBasis,
    imageProvenanceLabel,
    isOwnerBoundCatalogImageUrl,
    normalizeCatalogAsset,
    normalizeCatalogManifest,
    bindCatalogImageLifecycle
  });
}));
