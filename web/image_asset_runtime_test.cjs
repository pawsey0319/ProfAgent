"use strict";

const assert = require("node:assert/strict");
const {
  IMAGE_MODEL,
  CATALOG_ASSET_VERSION,
  REPORTED_MODEL_EXACT,
  EXACT_REQUEST_WITH_CPA_TRACE,
  BATCH_EXACT_REQUEST_CONTRACT,
  staticImageSuccessBasis,
  catalogImageSuccessBasis,
  imageProvenanceLabel,
  normalizeCatalogManifest,
  bindCatalogImageLifecycle
} = require("./image_asset_runtime.js");

const hash = "a".repeat(64);
const contentHash = "b".repeat(64);
const allowedIds = ["g001", "g002"];
const commonSuccess = {
  status: "ok",
  attempted: true,
  requested_model: IMAGE_MODEL,
  transport_model: IMAGE_MODEL,
  request_model_pinned: true,
  degraded: false,
  reason_code: null
};
const reportedStaticProvider = (overrides = {}) => ({
  ...commonSuccess,
  resolved_model: IMAGE_MODEL,
  model_reported: true,
  model_verified: true,
  cpa_trace_verified: true,
  verification_basis: REPORTED_MODEL_EXACT,
  ...overrides
});
const unreportedStaticProvider = (overrides = {}) => ({
  ...commonSuccess,
  resolved_model: null,
  model_reported: false,
  model_verified: false,
  cpa_trace_verified: true,
  verification_basis: EXACT_REQUEST_WITH_CPA_TRACE,
  ...overrides
});
const batchCatalogProvider = (overrides = {}) => ({
  ...commonSuccess,
  resolved_model: null,
  model_reported: false,
  model_verified: false,
  provenance_verified: true,
  verification_basis: BATCH_EXACT_REQUEST_CONTRACT,
  ...overrides
});

assert.equal(staticImageSuccessBasis(reportedStaticProvider()), REPORTED_MODEL_EXACT);
assert.equal(staticImageSuccessBasis(unreportedStaticProvider()), EXACT_REQUEST_WITH_CPA_TRACE);
assert.equal(imageProvenanceLabel(EXACT_REQUEST_WITH_CPA_TRACE), "请求模型已固定，CPA 未回报实际模型");
assert.equal(catalogImageSuccessBasis(batchCatalogProvider()), BATCH_EXACT_REQUEST_CONTRACT);
assert.equal(imageProvenanceLabel(BATCH_EXACT_REQUEST_CONTRACT), "请求模型已固定，CPA 未回报实际模型");

const staticRejectedMatrix = [
  ["reported resolved prefix", reportedStaticProvider({ resolved_model: "grok-imagine-image-quality-preview" })],
  ["reported wrong basis", reportedStaticProvider({ verification_basis: EXACT_REQUEST_WITH_CPA_TRACE })],
  ["reported flag false", reportedStaticProvider({ model_reported: false })],
  ["reported unverified", reportedStaticProvider({ model_verified: false })],
  ["reported trace field missing", reportedStaticProvider({ cpa_trace_verified: undefined })],
  ["reported trace false", reportedStaticProvider({ cpa_trace_verified: false })],
  ["reported basis missing", reportedStaticProvider({ verification_basis: undefined })],
  ["unreported trace false", unreportedStaticProvider({ cpa_trace_verified: false })],
  ["unreported trace missing", unreportedStaticProvider({ cpa_trace_verified: undefined })],
  ["unreported resolved model", unreportedStaticProvider({ resolved_model: IMAGE_MODEL })],
  ["unreported claims reported", unreportedStaticProvider({ model_reported: true })],
  ["unreported claims verified", unreportedStaticProvider({ model_verified: true })],
  ["unreported report flag missing", unreportedStaticProvider({ model_reported: undefined })],
  ["unreported wrong basis", unreportedStaticProvider({ verification_basis: REPORTED_MODEL_EXACT })],
  ["request prefix", unreportedStaticProvider({ requested_model: "grok-imagine-image-quality-preview" })],
  ["transport prefix", unreportedStaticProvider({ transport_model: "grok-imagine-image-quality-preview" })],
  ["text model", unreportedStaticProvider({ requested_model: "grok4.5", transport_model: "grok-4.5-high" })],
  ["missing request pin", unreportedStaticProvider({ request_model_pinned: false })],
  ["request pin field missing", unreportedStaticProvider({ request_model_pinned: undefined })],
  ["fallback status", unreportedStaticProvider({ status: "fallback", degraded: true })],
  ["unattempted", unreportedStaticProvider({ attempted: false })],
  ["degraded success", unreportedStaticProvider({ degraded: true })],
  ["success reason", unreportedStaticProvider({ reason_code: "SHOULD_BE_NULL" })],
  ["static cross-contract provenance", unreportedStaticProvider({ provenance_verified: true })],
  ["raw CPA receipt", unreportedStaticProvider({ cpa_trace_id: "cpa-secret-receipt" })]
];
for (const [name, provider] of staticRejectedMatrix) {
  assert.equal(staticImageSuccessBasis(provider), null, `${name} must fail closed`);
}

const readyAsset = (overrides = {}) => ({
  garment_id: "g001",
  status: "ready",
  asset_version: CATALOG_ASSET_VERSION,
  image_url: `/wardrobe/g001/catalog-image?user_id=u01&asset_version=${CATALOG_ASSET_VERSION}&content_sha256=${contentHash}`,
  media_type: "image/png",
  provider: batchCatalogProvider(),
  prompt_hash_status: "not_preserved",
  prompt_sha256: null,
  content_sha256: contentHash,
  ai_label: {
    generated: true,
    display_label: "AI生成目录参考",
    disclaimer: "仅供品类、风格与配色参考，不代表真实尺码、面料或垂坠"
  },
  ...overrides
});

const valid = normalizeCatalogManifest({
  api_version: "r1_demo_v1",
  user_id: "u01",
  manifest_version: CATALOG_ASSET_VERSION,
  assets: [readyAsset(), { garment_id: "g002", status: "missing", image_url: null }]
}, "u01", allowedIds);
assert.equal(valid.assets.get("g001").displayable, true);
assert.equal(valid.assets.get("g001").status, "ready");
assert.equal(valid.assets.get("g001").provider.resolved_model, null);
assert.equal(valid.assets.get("g001").provider.model_verified, false);
assert.equal(valid.assets.get("g001").provenance_label, "请求模型已固定，CPA 未回报实际模型");
assert.equal(valid.assets.get("g001").prompt_hash_status, "not_preserved");
assert.equal(valid.assets.get("g001").prompt_sha256, null);
assert.equal(valid.assets.get("g002").displayable, false);
assert.equal(valid.assets.get("g002").status, "missing");

assert.throws(() => normalizeCatalogManifest({
  api_version: "r1_demo_v1",
  user_id: "u02",
  manifest_version: CATALOG_ASSET_VERSION,
  assets: [readyAsset()]
}, "u01", allowedIds));
assert.throws(() => normalizeCatalogManifest({
  api_version: "r1_demo_v1",
  user_id: "u01",
  manifest_version: "wardrobe_generated_v1",
  assets: [readyAsset()]
}, "u01", allowedIds));

const unknown = normalizeCatalogManifest({
  api_version: "r1_demo_v1",
  user_id: "u01",
  manifest_version: CATALOG_ASSET_VERSION,
  assets: [readyAsset({
    garment_id: "g999",
    image_url: `/wardrobe/g999/catalog-image?user_id=u01&asset_version=${CATALOG_ASSET_VERSION}&content_sha256=${contentHash}`
  })]
}, "u01", allowedIds);
assert.equal(unknown.assets.has("g999"), false);
assert.equal(unknown.assets.get("g001").displayable, false);

const recordedPrompt = normalizeCatalogManifest({
  api_version: "r1_demo_v1",
  user_id: "u01",
  manifest_version: CATALOG_ASSET_VERSION,
  assets: [readyAsset({ prompt_hash_status: "recorded_at_generation", prompt_sha256: hash })]
}, "u01", allowedIds);
assert.equal(recordedPrompt.assets.get("g001").displayable, true);
assert.equal(recordedPrompt.assets.get("g001").prompt_sha256, hash);

const catalogRejectedMatrix = [
  ["catalog reported exact", { provider: reportedStaticProvider() }],
  ["catalog trace receipt", { provider: unreportedStaticProvider() }],
  ["catalog resolved claim", { provider: batchCatalogProvider({ resolved_model: IMAGE_MODEL }) }],
  ["catalog model reported", { provider: batchCatalogProvider({ model_reported: true }) }],
  ["catalog model verified", { provider: batchCatalogProvider({ model_verified: true }) }],
  ["catalog provenance false", { provider: batchCatalogProvider({ provenance_verified: false }) }],
  ["catalog provenance missing", { provider: batchCatalogProvider({ provenance_verified: undefined }) }],
  ["catalog basis missing", { provider: batchCatalogProvider({ verification_basis: undefined }) }],
  ["catalog attempted missing", { provider: batchCatalogProvider({ attempted: undefined }) }],
  ["catalog cross-contract trace", { provider: batchCatalogProvider({ cpa_trace_verified: true }) }],
  ["catalog raw receipt", { provider: batchCatalogProvider({ "x-cpa-trace-id": "cpa-secret-receipt" }) }],
  ["catalog request prefix", { provider: batchCatalogProvider({ requested_model: "grok-imagine-image-quality-preview" }) }],
  ["catalog transport prefix", { provider: batchCatalogProvider({ transport_model: "grok-imagine-image-quality-preview" }) }],
  ["catalog fallback", { provider: batchCatalogProvider({ status: "fallback", degraded: true }) }],
  ["catalog non-PNG", { media_type: "image/jpeg" }],
  ["not-preserved with prompt hash", { prompt_hash_status: "not_preserved", prompt_sha256: hash }],
  ["recorded generation without hash", { prompt_hash_status: "recorded_at_generation", prompt_sha256: null }],
  ["recorded attempt is not ready", { prompt_hash_status: "recorded_at_attempt", prompt_sha256: hash }],
  ["unknown prompt provenance", { prompt_hash_status: "preserved", prompt_sha256: hash }],
  ["cross-user URL", { image_url: `/wardrobe/g001/catalog-image?user_id=u02&asset_version=${CATALOG_ASSET_VERSION}&content_sha256=${contentHash}` }],
  ["unknown path", { image_url: `/wardrobe/g999/catalog-image?user_id=u01&asset_version=${CATALOG_ASSET_VERSION}&content_sha256=${contentHash}` }],
  ["legacy two-query URL", { image_url: `/wardrobe/g001/catalog-image?user_id=u01&asset_version=${CATALOG_ASSET_VERSION}` }],
  ["wrong content hash URL", { image_url: `/wardrobe/g001/catalog-image?user_id=u01&asset_version=${CATALOG_ASSET_VERSION}&content_sha256=${"c".repeat(64)}` }],
  ["legacy asset version", {
    asset_version: "wardrobe_generated_v1",
    image_url: `/wardrobe/g001/catalog-image?user_id=u01&asset_version=wardrobe_generated_v1&content_sha256=${contentHash}`
  }],
  ["missing image", { status: "missing", image_url: null }],
  ["failed image", { status: "failed", image_url: null }]
];
for (const [name, override] of catalogRejectedMatrix) {
  const manifest = normalizeCatalogManifest({
    api_version: "r1_demo_v1",
    user_id: "u01",
    manifest_version: CATALOG_ASSET_VERSION,
    assets: [readyAsset(override)]
  }, "u01", allowedIds);
  assert.equal(manifest.assets.get("g001").displayable, false, `${name} must fall back to metadata`);
}

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class FakeNode {
  constructor() {
    this.hidden = false;
    this.complete = false;
    this.naturalWidth = 0;
    this.removed = false;
    this.textContent = "";
    this.dataset = {};
    this.classList = new FakeClassList();
    this.listeners = new Map();
  }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  removeEventListener(type, listener) {
    if (this.listeners.get(type) === listener) this.listeners.delete(type);
  }
  dispatch(type) { this.listeners.get(type)?.(); }
  remove() { this.removed = true; }
}

function bindFakeCatalogImage({ card = {}, complete = false, naturalWidth = 0 } = {}) {
  const scheduled = [];
  const image = new FakeNode();
  const badge = new FakeNode();
  const swatch = new FakeNode();
  const statusNode = new FakeNode();
  image.complete = complete;
  image.naturalWidth = naturalWidth;
  statusNode.textContent = "目录图正在读取 · 元数据可用";
  const controller = bindCatalogImageLifecycle({
    card,
    image,
    badge,
    swatch,
    statusNode,
    imageUrl: readyAsset().image_url,
    successText: "AI 生成目录参考 · 请求模型已固定，CPA 未回报实际模型",
    failureText: "目录图读取失败 · 显示元数据",
    schedule: (callback) => scheduled.push(callback)
  });
  return { card, image, badge, swatch, statusNode, scheduled, controller };
}

const pendingLifecycle = bindFakeCatalogImage();
assert.equal(pendingLifecycle.image.src, readyAsset().image_url, "validated src is assigned exactly once");
assert.equal(pendingLifecycle.image.hidden, false, "pending image must remain layout-eligible");
assert.equal(pendingLifecycle.image.loading, "eager", "owner-bound catalog images must not wait behind a hidden-page lazy-load cycle");
assert.equal(pendingLifecycle.image.classList.contains("is-pending"), true);
assert.equal(pendingLifecycle.badge.hidden, true);
assert.equal(pendingLifecycle.swatch.hidden, false);
pendingLifecycle.image.naturalWidth = 512;
pendingLifecycle.image.dispatch("load");
assert.equal(pendingLifecycle.image.classList.contains("is-pending"), false);
assert.equal(pendingLifecycle.image.classList.contains("is-loaded"), true);
assert.equal(pendingLifecycle.image.hidden, false);
assert.equal(pendingLifecycle.badge.hidden, false);
assert.equal(pendingLifecycle.swatch.hidden, true);
assert.equal(pendingLifecycle.image.dataset.catalogNaturalWidth, "512");
assert.equal(pendingLifecycle.statusNode.textContent, "AI 生成目录参考 · 请求模型已固定，CPA 未回报实际模型");

const failedLifecycle = bindFakeCatalogImage();
failedLifecycle.image.dispatch("error");
assert.equal(failedLifecycle.image.removed, true);
assert.equal(failedLifecycle.badge.removed, true);
assert.equal(failedLifecycle.swatch.hidden, false);
assert.equal(failedLifecycle.statusNode.textContent, "目录图读取失败 · 显示元数据");

const cachedLifecycle = bindFakeCatalogImage({ complete: true, naturalWidth: 512 });
assert.equal(cachedLifecycle.scheduled.length, 1);
cachedLifecycle.scheduled[0]();
assert.equal(cachedLifecycle.badge.hidden, false, "cached complete image must settle to loaded");
assert.equal(cachedLifecycle.swatch.hidden, true);

const cachedFailureLifecycle = bindFakeCatalogImage({ complete: true, naturalWidth: 0 });
cachedFailureLifecycle.scheduled[0]();
assert.equal(cachedFailureLifecycle.image.removed, true, "cached broken image must be removed");
assert.equal(cachedFailureLifecycle.badge.removed, true);
assert.equal(cachedFailureLifecycle.swatch.hidden, false);
assert.equal(cachedFailureLifecycle.statusNode.textContent, "目录图读取失败 · 显示元数据");

const cancelledLifecycle = bindFakeCatalogImage({ complete: true, naturalWidth: 512 });
assert.equal(cancelledLifecycle.controller.cancel(), true);
cancelledLifecycle.scheduled[0]();
cancelledLifecycle.image.dispatch("load");
cancelledLifecycle.image.dispatch("error");
assert.equal(cancelledLifecycle.badge.hidden, true, "cancelled lifecycle must ignore queued and late completion");
assert.equal(cancelledLifecycle.swatch.hidden, false);
assert.equal(cancelledLifecycle.statusNode.textContent, "目录图正在读取 · 元数据可用");

const sharedCard = {};
const staleLifecycle = bindFakeCatalogImage({ card: sharedCard });
const currentLifecycle = bindFakeCatalogImage({ card: sharedCard });
staleLifecycle.image.dispatch("load");
assert.equal(staleLifecycle.badge.hidden, true, "stale lifecycle must not mutate an obsolete card binding");
assert.equal(staleLifecycle.swatch.hidden, false);
currentLifecycle.image.naturalWidth = 512;
currentLifecycle.image.dispatch("load");
assert.equal(currentLifecycle.badge.hidden, false);
assert.equal(currentLifecycle.swatch.hidden, true);

console.log("web S12R catalog provenance + image display lifecycle runtime contract: PASS");
