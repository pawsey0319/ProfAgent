"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const dialogueRuntimeSource = fs.readFileSync(path.join(__dirname, "dialogue_runtime.js"), "utf8");
const dialogueRuntimeTestSource = fs.readFileSync(path.join(__dirname, "dialogue_runtime_test.cjs"), "utf8");
const imageAssetRuntimeSource = fs.readFileSync(path.join(__dirname, "image_asset_runtime.js"), "utf8");
const imageAssetRuntimeTestSource = fs.readFileSync(path.join(__dirname, "image_asset_runtime_test.cjs"), "utf8");
const stylesSource = fs.readFileSync(path.join(__dirname, "styles.css"), "utf8");
const catalogBrowserSmokeSource = fs.readFileSync(path.join(__dirname, "catalog_image_browser_smoke.cjs"), "utf8");

function functionSource(name, nextName) {
  const start = source.indexOf(`function ${name}`);
  const end = source.indexOf(`function ${nextName}`, start + 1);
  assert.notEqual(start, -1, `${name} must exist`);
  assert.notEqual(end, -1, `${nextName} must follow ${name}`);
  return source.slice(start, end);
}

// Cache versioning: all entry assets must share the code-declared build ID.
const buildMatch = source.match(/const UI_BUILD_VERSION = "([^"]+)";/);
assert.ok(buildMatch, "UI build version must exist");
const buildVersion = buildMatch[1];
["styles.css", "dialogue_runtime.js", "image_asset_runtime.js", "fixtures.js", "app.js"].forEach((asset) => {
  assert.match(html, new RegExp(`${asset.replace(".", "\\.")}\\?v=${buildVersion}`));
});
assert.ok(html.indexOf("dialogue_runtime.js") < html.indexOf("app.js"), "dialogue runtime must load before app.js");
assert.ok(html.indexOf("image_asset_runtime.js") < html.indexOf("app.js"), "image asset runtime must load before app.js");

// Chat-first UX: no prefilled Scene form and no higher-dimensional entry.
assert.match(html, /id="conversation-origin"/);
assert.match(html, /data-example="先聊聊天"/);
assert.match(html, /<textarea id="scene-input"[^>]*placeholder="[^"]+"[^>]*><\/textarea>/);
assert.match(html, /id="scene-panel"[^>]*hidden/);
assert.doesNotMatch(html, />我今天下午面试，有点紧张，想显得可靠但别太老气。<\/textarea>/);
assert.doesNotMatch(html, /3D|360°|生成视频|实时视频|视频入口/);

const apiSource = functionSource("api", "setSource");
assert.match(apiSource, /REQUEST_TIMEOUT/);
assert.match(apiSource, /REQUEST_ABORTED/);
assert.match(apiSource, /NETWORK_UNAVAILABLE/);
assert.match(apiSource, /signal: externalSignal/);
assert.match(apiSource, /externalSignal\?\.addEventListener\("abort", abortFromExternalSignal/);
assert.match(apiSource, /externalSignal\?\.removeEventListener\("abort", abortFromExternalSignal\)/);
assert.doesNotMatch(apiSource, /安全降级|用户输入|违规|不合规/);

// S9: one synchronous HTTP turn waits longer than the server's 120s CPA budget.
const dialogueTimeoutMatch = source.match(/const DIALOGUE_TURN_TIMEOUT_MS = (\d+);/);
assert.ok(dialogueTimeoutMatch, "dialogue timeout must exist");
assert.equal(Number(dialogueTimeoutMatch[1]), 125000, "S9 browser dialogue timeout must be frozen at 125000ms");
assert.ok(Number(dialogueTimeoutMatch[1]) > 120000, "browser dialogue timeout must exceed backend 120s budget");
assert.doesNotMatch(source, /DIALOGUE_TURN_TIMEOUT_MS = (?:4500|35000)|dialogue-(?:4500ms|35s)/);
assert.doesNotMatch(html, /dialogue-(?:4500ms|35s)|35 秒|35s/i);

// Dialogue origin is determined from the frozen generation_source contract, never status alone.
const originSource = functionSource("isCpaDialogueReply", "appendMessage");
assert.match(originSource, /dialogueRuntime\.isStrictCpaReply\(provider\)/);
assert.match(originSource, /conversationMode === "safety_response"\) return "安全回应"/);
assert.match(originSource, /"CPA 生成" : "本地回复"/);
assert.ok(
  originSource.indexOf('conversationMode === "safety_response"') < originSource.indexOf('isCpaDialogueReply(provider) ? "CPA 生成"'),
  "safety_response must take precedence over CPA/local provider origin"
);
assert.match(dialogueRuntimeSource, /provider\.generation_source === "cpa"/);
assert.match(dialogueRuntimeSource, /provider\.status === "ok"/);
assert.match(dialogueRuntimeSource, /provider\.model_verified === true/);
assert.match(dialogueRuntimeSource, /provider\.degraded === false/);
assert.match(dialogueRuntimeSource, /const CPA_REQUESTED_MODEL = "grok4\.6"/);
assert.match(dialogueRuntimeSource, /const CPA_TRANSPORT_MODEL = "grok-4\.6-high"/);
assert.match(dialogueRuntimeSource, /new Set\(\["grok-4\.6-high", "grok-4\.6-build"\]\)/);
assert.match(dialogueRuntimeSource, /provider\?\.requested_model === CPA_REQUESTED_MODEL/);
assert.match(dialogueRuntimeSource, /provider\.transport_model === CPA_TRANSPORT_MODEL/);
assert.match(dialogueRuntimeSource, /CPA_RESOLVED_MODELS\.has\(provider\.resolved_model\)/);
assert.doesNotMatch(dialogueRuntimeSource, /(?:startsWith|includes)\([^\n]*grok-4\.6/);
assert.match(dialogueRuntimeSource, /if \(isStrictCpaReply\(provider\)\) return message/);
assert.match(source, /raw\.requested_model !== "grok4\.6" \|\| raw\.transport_model !== "grok-4\.6-high"/);

const appendSource = functionSource("appendMessage", "renderSuggestedReplies");
assert.match(appendSource, /make\("p", "", text\)/);
assert.doesNotMatch(appendSource, /innerHTML|insertAdjacentHTML/);
assert.match(appendSource, /meta\.conversationMode === "safety_response"/);
assert.match(appendSource, /!safetyResponse && isCpaDialogueReply\(meta\.provider\)/);
assert.match(appendSource, /dialogueReplyLabel\(meta\.provider, meta\.conversationMode\)/);
assert.match(appendSource, /origin-safety/);
assert.match(appendSource, /origin-cpa/);
assert.match(appendSource, /origin-local/);
assert.doesNotMatch(appendSource, /resolved_model|reason_code|transport_model/);

const connectSource = functionSource("connect", "renderTeam");
assert.match(connectSource, /CPA 对话已就绪/);
assert.match(connectSource, /本地回复已就绪/);
assert.match(connectSource, /对话仍可使用本地回复/);
assert.match(connectSource, /dialogueRuntime\.hasExactCpaModel\(llm\)/);
assert.match(connectSource, /const cpaModelReady =/);
assert.match(connectSource, /api\(`\/wardrobe\/catalog-assets\?user_id=\$\{encodeURIComponent\(userId\)\}`\)\.catch\(\(\) => null\)/);
assert.match(connectSource, /imageAssetRuntime\.normalizeCatalogManifest/);
assert.match(connectSource, /\(wardrobe\.items \|\| \[\]\)\.map\(\(item\) => item\.garment_id\)/);
assert.match(connectSource, /state\.wardrobeCatalogManifestState = "unavailable"/);
assert.doesNotMatch(connectSource, /已安全降级|安全降级|智能对话暂时不可用|用户输入|违规|不合规/);

// S12 wardrobe catalog images are owner/garment bound and fail closed to metadata cards.
assert.match(html, /id="wardrobe-catalog-status"/);
assert.match(html, /AI 生成目录参考仅供品类、风格与配色识别/);
assert.match(html, /不是衣物实拍，也不是上身效果/);
assert.match(html, /不代表真实尺码、面料或垂坠/);
const wardrobeSource = functionSource("renderWardrobe", "isCompleteIds");
assert.match(wardrobeSource, /state\.wardrobeCatalogAssets\.get\(item\.garment_id\)/);
assert.match(wardrobeSource, /catalogAsset\?\.displayable === true/);
assert.match(wardrobeSource, /imageAssetRuntime\.bindCatalogImageLifecycle/);
assert.match(wardrobeSource, /imageUrl: catalogAsset\.image_url/);
assert.doesNotMatch(wardrobeSource, /catalogImage\.hidden\s*=\s*true/);
assert.match(wardrobeSource, /目录图正在读取 · 元数据可用/);
assert.match(wardrobeSource, /目录图读取失败 · 显示元数据/);
assert.match(wardrobeSource, /目录图未完成 · 显示元数据/);
assert.match(wardrobeSource, /AI 生成目录参考/);
assert.match(wardrobeSource, /catalogAsset\.provenance_label/);
assert.match(wardrobeSource, /可显示的 AI 目录参考/);
assert.doesNotMatch(wardrobeSource, /实际模型已验证|resolved_model/);
assert.doesNotMatch(wardrobeSource, /item\.(?:image|image_url|asset_url)/);

assert.match(imageAssetRuntimeSource, /const IMAGE_MODEL = "grok-imagine-image-quality"/);
assert.match(imageAssetRuntimeSource, /const CATALOG_ASSET_VERSION = "wardrobe_generated_v1_s12r2"/);
assert.match(imageAssetRuntimeSource, /const READY_MEDIA_TYPES = new Set\(\["image\/png"\]\)/);
assert.match(imageAssetRuntimeSource, /provider\?\.requested_model === IMAGE_MODEL/);
assert.match(imageAssetRuntimeSource, /provider\.transport_model === IMAGE_MODEL/);
assert.match(imageAssetRuntimeSource, /provider\.status === "ok"/);
assert.match(imageAssetRuntimeSource, /provider\.attempted === true/);
assert.match(imageAssetRuntimeSource, /provider\.degraded === false/);
assert.match(imageAssetRuntimeSource, /provider\.request_model_pinned === true/);
assert.match(imageAssetRuntimeSource, /const REPORTED_MODEL_EXACT = "reported_model_exact"/);
assert.match(imageAssetRuntimeSource, /const EXACT_REQUEST_WITH_CPA_TRACE = "exact_request_with_cpa_trace"/);
assert.match(imageAssetRuntimeSource, /const BATCH_EXACT_REQUEST_CONTRACT = "batch_exact_request_contract"/);
assert.match(imageAssetRuntimeSource, /const RAW_RECEIPT_FIELDS = Object\.freeze/);
assert.match(imageAssetRuntimeSource, /RAW_RECEIPT_FIELDS\.every\(\(field\) => provider\[field\] === undefined\)/);
assert.match(imageAssetRuntimeSource, /typeof provider\.cpa_trace_verified !== "boolean"/);
assert.match(imageAssetRuntimeSource, /provider\.provenance_verified !== undefined/);
assert.match(imageAssetRuntimeSource, /provider\.resolved_model === IMAGE_MODEL/);
assert.match(imageAssetRuntimeSource, /provider\.model_reported === true/);
assert.match(imageAssetRuntimeSource, /provider\.model_verified === true/);
assert.match(imageAssetRuntimeSource, /provider\.cpa_trace_verified === true/);
assert.match(imageAssetRuntimeSource, /provider\.model_reported === false/);
assert.match(imageAssetRuntimeSource, /provider\.model_verified === false/);
assert.match(imageAssetRuntimeSource, /provider\.resolved_model === null/);
assert.match(imageAssetRuntimeSource, /provider\.provenance_verified === true/);
assert.match(imageAssetRuntimeSource, /provider\.cpa_trace_verified !== undefined/);
assert.match(imageAssetRuntimeSource, /catalogImageSuccessBasis\(raw\.provider\) === BATCH_EXACT_REQUEST_CONTRACT/);
assert.match(imageAssetRuntimeSource, /raw\.prompt_hash_status === "recorded_at_generation"/);
assert.match(imageAssetRuntimeSource, /raw\.prompt_hash_status === "not_preserved"/);
assert.match(imageAssetRuntimeSource, /raw\.prompt_sha256 === null/);
assert.match(imageAssetRuntimeSource, /prompt_hash_status: raw\.prompt_hash_status/);
assert.match(imageAssetRuntimeSource, /请求模型已固定，CPA 未回报实际模型/);
assert.match(imageAssetRuntimeSource, /raw\.user_id !== userId/);
assert.match(imageAssetRuntimeSource, /raw\.api_version !== "r1_demo_v1"/);
assert.match(imageAssetRuntimeSource, /raw\.manifest_version !== CATALOG_ASSET_VERSION/);
assert.match(imageAssetRuntimeSource, /if \(!allowed\.has\(garmentId\)\) continue/);
assert.match(imageAssetRuntimeSource, /parsed\.searchParams\.get\("user_id"\) === userId/);
assert.match(imageAssetRuntimeSource, /keys\.length === 3/);
assert.match(imageAssetRuntimeSource, /keys\[1\] === "content_sha256"/);
assert.match(imageAssetRuntimeSource, /parsed\.searchParams\.get\("asset_version"\) === CATALOG_ASSET_VERSION/);
assert.match(imageAssetRuntimeSource, /parsed\.searchParams\.get\("content_sha256"\) === contentSha256/);
assert.match(imageAssetRuntimeSource, /parsed\.pathname === expectedPath/);
assert.match(imageAssetRuntimeSource, /raw\.status === "missing"/);
assert.match(imageAssetRuntimeSource, /raw\.status === "failed"/);
assert.match(imageAssetRuntimeSource, /function bindCatalogImageLifecycle/);
assert.match(imageAssetRuntimeSource, /image\.hidden = false/);
assert.match(imageAssetRuntimeSource, /image\.loading = "eager"/);
assert.match(imageAssetRuntimeSource, /image\.classList\.add\("is-pending"\)/);
assert.match(imageAssetRuntimeSource, /image\.src = imageUrl/);
assert.match(imageAssetRuntimeSource, /image\.complete === true/);
assert.match(imageAssetRuntimeSource, /Number\(image\.naturalWidth\) > 0/);
assert.match(imageAssetRuntimeSource, /image\.dataset\.catalogNaturalWidth = String\(image\.naturalWidth\)/);
assert.match(imageAssetRuntimeSource, /catalogImageLifecycleTokens\.get\(card\) === token/);
assert.match(imageAssetRuntimeSource, /image\.remove\(\)/);
assert.match(imageAssetRuntimeSource, /badge\.remove\(\)/);
assert.match(stylesSource, /\.garment-catalog-image\.is-pending\s*\{[^}]*opacity:\s*0/s);
assert.match(stylesSource, /\.garment-catalog-image\.is-loaded\s*\{[^}]*opacity:\s*1/s);
assert.match(imageAssetRuntimeTestSource, /pending image must remain layout-eligible/);
assert.match(imageAssetRuntimeTestSource, /hidden-page lazy-load cycle/);
assert.match(imageAssetRuntimeTestSource, /cached complete image must settle to loaded/);
assert.match(imageAssetRuntimeTestSource, /cached broken image must be removed/);
assert.match(imageAssetRuntimeTestSource, /cancelled lifecycle must ignore queued and late completion/);
assert.match(imageAssetRuntimeTestSource, /stale lifecycle must not mutate an obsolete card binding/);
assert.match(catalogBrowserSmokeSource, /real browser must load at least one validated owner-bound catalog image/);
assert.match(catalogBrowserSmokeSource, /Number\(widthMatch\[1\]\) > 0/);
assert.match(catalogBrowserSmokeSource, /isOwnerBoundCatalogImageUrl/);
assert.doesNotMatch(imageAssetRuntimeSource, /startsWith\([^\n]*IMAGE_MODEL|includes\([^\n]*IMAGE_MODEL/);
assert.match(imageAssetRuntimeTestSource, /cross-user URL/);
assert.match(imageAssetRuntimeTestSource, /unknown path/);
assert.match(imageAssetRuntimeTestSource, /reported resolved prefix/);
assert.match(imageAssetRuntimeTestSource, /request prefix/);
assert.match(imageAssetRuntimeTestSource, /transport prefix/);
assert.match(imageAssetRuntimeTestSource, /text model/);
assert.match(imageAssetRuntimeTestSource, /missing image/);
assert.match(imageAssetRuntimeTestSource, /failed image/);
assert.match(imageAssetRuntimeTestSource, /unreported trace false/);
assert.match(imageAssetRuntimeTestSource, /unreported resolved model/);
assert.match(imageAssetRuntimeTestSource, /reported trace field missing/);
assert.match(imageAssetRuntimeTestSource, /reported trace false/);
assert.match(imageAssetRuntimeTestSource, /reported basis missing/);
assert.match(imageAssetRuntimeTestSource, /unreported trace missing/);
assert.match(imageAssetRuntimeTestSource, /unreported report flag missing/);
assert.match(imageAssetRuntimeTestSource, /request pin field missing/);
assert.match(imageAssetRuntimeTestSource, /static cross-contract provenance/);
assert.match(imageAssetRuntimeTestSource, /raw CPA receipt/);
assert.match(imageAssetRuntimeTestSource, /catalog cross-contract trace/);
assert.match(imageAssetRuntimeTestSource, /catalog raw receipt/);
assert.match(imageAssetRuntimeTestSource, /catalog reported exact/);
assert.match(imageAssetRuntimeTestSource, /catalog trace receipt/);
assert.match(imageAssetRuntimeTestSource, /catalog provenance missing/);
assert.match(imageAssetRuntimeTestSource, /catalog basis missing/);
assert.match(imageAssetRuntimeTestSource, /catalog attempted missing/);
assert.match(imageAssetRuntimeTestSource, /catalog non-PNG/);
assert.match(imageAssetRuntimeTestSource, /not-preserved with prompt hash/);
assert.match(imageAssetRuntimeTestSource, /recorded generation without hash/);
assert.match(imageAssetRuntimeTestSource, /recorded attempt is not ready/);
assert.match(imageAssetRuntimeTestSource, /unknown prompt provenance/);
assert.match(imageAssetRuntimeTestSource, /legacy two-query URL/);
assert.match(imageAssetRuntimeTestSource, /wrong content hash URL/);
assert.match(imageAssetRuntimeTestSource, /legacy asset version/);
assert.match(imageAssetRuntimeTestSource, /请求模型已固定，CPA 未回报实际模型/);

const providerSource = functionSource("validateCpaProvider", "hasEnabledToolPayload");
assert.match(providerSource, /raw\.requested_model !== "grok4\.6"/);
assert.match(providerSource, /raw\.transport_model !== "grok-4\.6-high"/);
assert.match(providerSource, /!dialogueRuntime\.hasExactCpaModel\(raw\)/);
assert.match(providerSource, /typeof raw\.attempted !== "boolean"/);
assert.match(providerSource, /raw\.model_verified !== true/);
assert.match(providerSource, /raw\.status === "fallback" && raw\.degraded !== true/);
assert.match(providerSource, /raw\.generation_source !== "cpa" && raw\.generation_source !== "local_fallback"/);
assert.doesNotMatch(providerSource, /startsWith|includes/);

const suggestionSource = functionSource("renderSuggestedReplies", "setDialogueMode");
assert.match(suggestionSource, /controlledSuggestedReplies\.has\(reply\)/);
assert.match(suggestionSource, /button\.disabled = dialogueGuard\.inFlight/);
assert.match(suggestionSource, /if \(dialogueGuard\.inFlight\) return/);
assert.match(suggestionSource, /input\.value = suggestedReplyMessages\[reply\];/);
assert.doesNotMatch(suggestionSource, /requestSubmit|handleSceneSubmit|conversationMode\s*=(?!=)/);

const sceneValidationSource = functionSource("validateDialogueScene", "validateDialogueTurn");
assert.match(sceneValidationSource, /raw\.user_id !== userId/);
assert.match(sceneValidationSource, /raw\.styling_session_id !== stylingSessionId/);
assert.match(sceneValidationSource, /!sceneHorizons\.has\(raw\.event_horizon\)/);
assert.match(sceneValidationSource, /hardenScene/);

const validationSource = functionSource("validateDialogueTurn", "applyDialogueTurn");
assert.match(source, /stylist_chat: Object\.freeze\(\{\s*chat: Object\.freeze\(\{ paused: true, pending: Object\.freeze\(\["none"\]\)/);
assert.match(validationSource, /dialogueCombinationRules\[raw\.conversation_mode\]\?\.\[raw\.action\]/);
assert.match(validationSource, /validateCpaProvider\(raw\.provider, true\)/);
assert.match(validationSource, /raw\.scene === null \? null : validateDialogueScene/);
assert.match(validationSource, /raw\.conversation_mode === "stylist_chat" && scene !== null/);
assert.match(validationSource, /raw\.conversation_mode === "styling_active" && scene === null/);
assert.match(validationSource, /raw\.action !== "recommend" && raw\.recommendation !== null/);
assert.match(validationSource, /scene\.shopping_allowed !== false \|\| scene\.ui_capabilities\.shopping_cta !== false/);
assert.match(validationSource, /raw\.turn_index !== expectedTurnIndex/);
assert.match(validationSource, /raw\.history_version < raw\.turn_index/);
assert.match(validationSource, /forbiddenTopLevelToolFields\.some/);
assert.match(validationSource, /dialogueRuntime\.selectAssistantText\([\s\S]*raw\.assistant_message,[\s\S]*provider,[\s\S]*safeStylingText\(message/);
assert.doesNotMatch(validationSource, /assistant_message: safeStylingText\(raw\.assistant_message/);
assert.match(dialogueRuntimeTestSource, /我不会评价你的身材，只讨论衣服版型/);
assert.match(dialogueRuntimeTestSource, /selectAssistantText\(legalMessage, strictProvider/);

const applySource = functionSource("applyDialogueTurn", "showDialogueFailure");
assert.match(applySource, /^function applyDialogueTurn\(response\) \{\s*clearDialogueWaitTimer\(\);/);
assert.match(applySource, /setDialogueMode\(response\.conversation_mode\);[\s\S]*appendMessage\("Stylist", response\.assistant_message, false, \{/);
assert.equal((applySource.match(/appendMessage\("Stylist"/g) || []).length, 1, "a completed turn must insert one formal Stylist reply");
assert.match(applySource, /provider: response\.provider,[\s\S]*conversationMode: response\.conversation_mode,[\s\S]*turnIndex: response\.turn_index/);
assert.match(applySource, /if \(response\.action === "recommend"\)/);
assert.match(applySource, /sanitizeRecommendation\(state\.scene, response\.recommendation\)/);
assert.match(applySource, /if \(response\.scene\) renderScene\(\);[\s\S]*clearRecommendationPresentation\(\);/);
assert.match(applySource, /response\.conversation_mode === "stylist_chat"/);
assert.match(applySource, /byId\("scene-panel"\)\.hidden = true/);
assert.match(applySource, /`本轮：\$\{dialogueReplyLabel\(response\.provider, response\.conversation_mode\)\}`/);
assert.doesNotMatch(applySource, /response\.provider\.status === "ok"/);
assert.doesNotMatch(applySource, /api\("\/recommend"|fixtures\.recommendation/);

const failureSource = functionSource("showDialogueFailure", "handleSceneSubmit");
assert.match(failureSource, /^function showDialogueFailure\(error, retryBinding\) \{\s*clearDialogueWaitTimer\(\);/);
assert.match(failureSource, /pendingDialogueRetry = \{ \.\.\.retryBinding \}/);
assert.match(failureSource, /clearRecommendationPresentation\(\)/);
assert.match(failureSource, /call_count: 0/);
assert.match(failureSource, /byId\("conversation-origin"\)\.textContent = "本轮回应未完成"/);
assert.doesNotMatch(failureSource, /resetSessionScopedState|fixtures\.|error\.message/);
assert.doesNotMatch(failureSource, /setSource\(|showOfflineBrowseOnly\(|state\.source\s*=/);
assert.doesNotMatch(failureSource, /已安全降级|安全降级|用户输入|违规|不合规/);
assert.match(source, /Stylist 这次回应时间较长；会话仍保留，可以安全重试。/);

const debugSource = functionSource("renderDebug", "showOfflineBrowseOnly");
assert.match(debugSource, /\["回复来源", replySource\]/);
assert.match(debugSource, /state\.dialogueProvider \|\| trace\.provider/);
assert.match(debugSource, /dialogueReplyLabel\(provider, state\.conversationMode\)/);
assert.match(debugSource, /trace\.mode === "dialogue_retry_required"/);
assert.match(debugSource, /\? "本轮回应未完成"/);
assert.match(debugSource, /\["请求模型", provider\.requested_model/);
assert.match(debugSource, /\["实际模型", provider\.resolved_model/);

const traceSource = functionSource("fetchTrace", "renderDebug");
assert.match(traceSource, /isStillCurrent = \(\) => true, signal = null/);
assert.ok((traceSource.match(/if \(!isStillCurrent\(\)\) return;/g) || []).length >= 4, "stale trace responses must not mutate a reset UI generation");
assert.match(traceSource, /api\(`\/trace\/[\s\S]*\{ signal \}\)/);

const offlineSource = functionSource("showOfflineBrowseOnly", "recommendationFailureCopy");
assert.match(offlineSource, /byId\("conversation-origin"\)\.textContent = "对话服务未连接"/);

const waitTimerSource = functionSource("clearDialogueWaitTimer", "releaseDialogueRequestUi");
assert.match(waitTimerSource, /window\.clearInterval\(dialogueWaitTimer\)/);
assert.match(waitTimerSource, /dialogueWaitTimer = null/);
assert.match(waitTimerSource, /dialogueWaitStartedAt = 0/);
assert.match(waitTimerSource, /Date\.now\(\) - dialogueWaitStartedAt/);
assert.match(waitTimerSource, /`CPA 正在生成 · 已等待 \$\{elapsedSeconds\}s`/);
assert.match(waitTimerSource, /window\.setInterval\(renderDialogueWaitElapsed, 1000\)/);
assert.doesNotMatch(waitTimerSource, /fetch\(|api\(|\/dialogue\//);

const releaseSource = functionSource("releaseDialogueRequestUi", "loadDialogueTraceBestEffort");
assert.match(releaseSource, /dialogueGuard\.finish\(requestToken\)/);
assert.match(releaseSource, /clearDialogueWaitTimer\(\)/);
assert.match(releaseSource, /setDialogueInFlightControls\(false\)/);

const traceLoaderSource = functionSource("loadDialogueTraceBestEffort", "createDialogueRequestId");
assert.match(traceLoaderSource, /dialogueTraceGuard\.cancel\(\)/);
assert.match(traceLoaderSource, /dialogueTraceGuard\.begin\(response\.trace_id, traceController\)/);
assert.match(traceLoaderSource, /dialogueGuard\.isGenerationCurrent\(requestToken\)/);
assert.match(traceLoaderSource, /state\.stylingSessionId === response\.styling_session_id/);
assert.match(traceLoaderSource, /state\.turnIndex === response\.turn_index/);
assert.match(traceLoaderSource, /fetchTrace\(response\.trace_id, isStillCurrent, traceController\.signal\)/);
assert.doesNotMatch(traceLoaderSource, /appendMessage|conversation-origin|startDialogueWaitTimer|setDialogueInFlightControls\(true\)/);

const submitSource = functionSource("handleSceneSubmit", "startNewTask");
assert.match(submitSource, /api\("\/dialogue\/turn"/);
assert.doesNotMatch(submitSource, /api\("\/scene\/parse"|api\("\/recommend"/);
assert.match(submitSource, /const payload = \{ user_id: userId, message, request_id: requestId \};/);
assert.match(submitSource, /if \(stylingSessionId\) payload\.styling_session_id = stylingSessionId;/);
assert.match(submitSource, /retry\.message === message/);
assert.match(submitSource, /retry\.stylingSessionId === state\.stylingSessionId/);
assert.match(submitSource, /requestId = isSafeRetry \? retry\.requestId/);
assert.match(submitSource, /if \(dialogueGuard\.inFlight\) return/);
assert.match(submitSource, /dialogueGuard\.begin\(requestId, requestController\)/);
assert.match(submitSource, /setDialogueInFlightControls\(true\)/);
assert.match(submitSource, /signal: requestController\.signal/);
assert.ok((submitSource.match(/if \(!dialogueGuard\.isCurrent\(requestToken\)\) return;/g) || []).length >= 3, "success, validation and failure paths must reject stale generations");
assert.match(submitSource, /dialogueTraceGuard\.cancel\(\)/);
assert.match(submitSource, /applyDialogueTurn\(response\);\s*requestReleased = releaseDialogueRequestUi\(requestToken, sceneInput, submitLabel\);\s*if \(!requestReleased\) return;\s*loadDialogueTraceBestEffort\(response, requestToken\);/);
assert.match(submitSource, /startDialogueWaitTimer\(\)/);
assert.match(submitSource, /finally \{\s*if \(!requestReleased && dialogueGuard\.isCurrent\(requestToken\)\) \{\s*requestReleased = releaseDialogueRequestUi/);
assert.doesNotMatch(submitSource, /finally \{[\s\S]*dialogueGuard\.finish\(requestToken\)/);
assert.doesNotMatch(submitSource, /appendMessage\("Stylist"|appendMessage\("系统"/);
const appendUserIndex = submitSource.indexOf('appendMessage("你", message, true);');
const clearInputIndex = submitSource.indexOf('sceneInput.value = "";', appendUserIndex);
const waitingTimerIndex = submitSource.indexOf('startDialogueWaitTimer();', clearInputIndex);
const dialogueCallIndex = submitSource.indexOf('api("/dialogue/turn"', clearInputIndex);
assert.notEqual(appendUserIndex, -1);
assert.ok(clearInputIndex > appendUserIndex, "input must clear immediately");
assert.ok(waitingTimerIndex > clearInputIndex, "elapsed waiting state must follow immediate input clear");
assert.ok(dialogueCallIndex > waitingTimerIndex, "waiting UI must render before the only dialogue request");
assert.equal((submitSource.match(/api\("\/dialogue\/turn"/g) || []).length, 1, "one HTTP turn must use one dialogue endpoint call");

const sanitizerSource = functionSource("sanitizeRecommendation", "appendMessage");
assert.match(sanitizerSource, /shoppingCtaAllowed/);
assert.match(sanitizerSource, /!highGuard/);
assert.match(sanitizerSource, /shopping_suggestions: shoppingCtaAllowed \? rawSuggestions : \[\]/);

// Static 2D is a separate CPA job bound to the exact immutable Look.
assert.match(html, /id="preview2d-generate"/);
assert.match(html, /id="preview2d-delete"/);
assert.match(html, /AI 生成 · 视觉参考/);
assert.match(html, /不代表精确尺码、面料触感或真实垂坠/);
assert.doesNotMatch(html, /本人试穿|真人试穿|虚拟试穿/);
assert.match(html, /仅生成中性模特\/平铺效果/);
assert.match(html, /不会发送身份图像/);
assert.doesNotMatch(html, /已选择的静态人像/);
const previewNormalizeSource = functionSource("normalizeStatic2dPreview", "preview2dImageUrl");
assert.match(previewNormalizeSource, /raw\.render_mode !== "static_2d"/);
assert.match(previewNormalizeSource, /raw\.user_id !== userId/);
assert.match(previewNormalizeSource, /raw\.styling_session_id !== expected\.stylingSessionId/);
assert.match(previewNormalizeSource, /raw\.look_version_id !== expected\.lookVersionId/);
assert.match(previewNormalizeSource, /externalItemIds\.length !== 0/);
assert.match(previewNormalizeSource, /sameIdSet\(ownedGarmentIds, expected\.itemIds\)/);
assert.match(previewNormalizeSource, /raw\.identity_asset_id !== null/);
assert.match(previewNormalizeSource, /raw\.fidelity\.identity !== "not_assessed"/);
assert.match(previewNormalizeSource, /validateImageProvider\(raw\.provider\)/);
assert.match(previewNormalizeSource, /raw\.status === "succeeded" && provider\.status !== "ok"/);
assert.match(previewNormalizeSource, /raw\.status !== "succeeded" && provider\.status !== "fallback"/);
assert.match(previewNormalizeSource, /raw\.ai_label/);
assert.match(previewNormalizeSource, /raw\.status === "succeeded" && !raw\.asset_id/);
assert.doesNotMatch(previewNormalizeSource, /raw\.image_url/);

const previewUrlSource = functionSource("preview2dImageUrl", "renderPreview2d");
assert.match(previewUrlSource, /`\/preview\/static-2d\/\$\{encodeURIComponent\(preview\.preview_id\)\}\/image\?/);
assert.match(previewUrlSource, /user_id: userId/);
assert.match(previewUrlSource, /styling_session_id: preview\.styling_session_id/);

const previewRenderSource = functionSource("renderPreview2d", "generateStatic2dPreview");
assert.match(previewRenderSource, /中性模特\/平铺，未评估身份一致性/);
assert.match(previewRenderSource, /不会使用身份图像/);
assert.doesNotMatch(previewRenderSource, /身份还原|本人试穿/);
assert.match(previewRenderSource, /imageAssetRuntime\.staticImageSuccessBasis\(preview\.provider\)/);
assert.match(previewRenderSource, /imageAssetRuntime\.imageProvenanceLabel\(provenanceBasis\)/);
assert.match(previewRenderSource, /generated \? `CPA 生成 · \$\{provenanceLabel\}` : "本地参考"/);
assert.match(previewRenderSource, /请求模型已固定，CPA 未回报实际模型|provenanceLabel/);
assert.match(previewRenderSource, /preview\.status === "succeeded" && generated/);
assert.doesNotMatch(previewRenderSource, /安全降级/);

const imageProviderSource = functionSource("validateImageProvider", "normalizeStatic2dPreview");
assert.match(imageProviderSource, /raw\.requested_model !== imageAssetRuntime\.IMAGE_MODEL/);
assert.match(imageProviderSource, /raw\.transport_model !== imageAssetRuntime\.IMAGE_MODEL/);
assert.match(imageProviderSource, /imageAssetRuntime\.staticImageSuccessBasis\(raw\)/);
assert.match(imageProviderSource, /successBasis === null/);
assert.match(imageProviderSource, /typeof raw\.request_model_pinned !== "boolean"/);
assert.match(imageProviderSource, /typeof raw\.cpa_trace_verified !== "boolean"/);
assert.match(imageProviderSource, /typeof raw\.model_reported !== "boolean"/);
assert.match(imageProviderSource, /raw\.verification_basis !== null/);
assert.match(imageProviderSource, /raw\.status === "fallback"/);
assert.doesNotMatch(imageProviderSource, /grok-4\.5-high|grok4\.5|startsWith|includes/);

const previewGenerateSource = functionSource("generateStatic2dPreview", "deleteStatic2dPreview");
assert.match(previewGenerateSource, /api\("\/preview\/static-2d"/);
assert.match(previewGenerateSource, /render_mode: "static_2d"/);
assert.match(previewGenerateSource, /consent: true/);
assert.match(previewGenerateSource, /look_version_id: lookVersion/);
assert.match(previewGenerateSource, /identity_asset_id: null/);
assert.doesNotMatch(previewGenerateSource, /availableLookAssetIds|identityAssetId/);
assert.match(previewGenerateSource, /timeoutMs: PREVIEW_2D_TIMEOUT_MS/);
assert.doesNotMatch(previewGenerateSource, /external_item_ids|3d|video/i);

const previewDeleteSource = functionSource("deleteStatic2dPreview", "previewAssetFile");
assert.match(previewDeleteSource, /method: "DELETE"/);
assert.match(previewDeleteSource, /user_id: userId, styling_session_id: state\.stylingSessionId/);
assert.match(previewDeleteSource, /status: "deleted"/);

const newTaskSource = functionSource("startNewTask", "bindEvents");
assert.match(newTaskSource, /window\.confirm/);
assert.match(newTaskSource, /resetSessionScopedState\(null\)/);
assert.ok(newTaskSource.indexOf("resetSessionScopedState(null)") < newTaskSource.indexOf('byId("conversation").replaceChildren()'), "reset must cancel and invalidate the old request before rebuilding UI");
const resetCalls = source.match(/resetSessionScopedState\(/g) || [];
assert.equal(resetCalls.length, 2, "only explicit new task may reset session");

assert.doesNotMatch(source, /已安全降级|智能对话暂时不可用|origin-degraded/);
assert.doesNotMatch(html, /已安全降级|智能对话暂时不可用/);
assert.match(source, /safety_response: "安全回应 · 已暂停穿搭建议"/);
assert.doesNotMatch(source, /local_provisional|["'`]\/dialogue\/(?:pending|poll|status)/i);
assert.match(source, /window\.addEventListener\("beforeunload", \(\) => \{\s*dialogueTraceGuard\.cancel\(\);\s*dialogueGuard\.cancel\(\);\s*clearDialogueWaitTimer\(\);/);
assert.match(source, /function resetSessionScopedState\(stylingSessionId\) \{\s*dialogueTraceGuard\.cancel\(\);\s*dialogueGuard\.cancel\(\);\s*clearDialogueWaitTimer\(\);/);
assert.match(dialogueRuntimeSource, /isGenerationCurrent\(token\)/);
assert.match(dialogueRuntimeTestSource, /formal response must unlock controls before deferred trace settles/);
assert.match(dialogueRuntimeTestSource, /trace failure must not change the formal reply source/);
assert.match(dialogueRuntimeTestSource, /reset must prevent an old trace from writing into the new UI/);
assert.match(dialogueRuntimeTestSource, /transport_model: "grok-4\.6-high"/);
assert.match(dialogueRuntimeTestSource, /resolved_model: "grok-4\.6-build"/);
assert.match(dialogueRuntimeTestSource, /resolved_model: "grok-4\.6-high-preview"/);
assert.match(dialogueRuntimeTestSource, /legacy logical 4\.5/);
assert.match(dialogueRuntimeTestSource, /legacy transport 4\.5/);
assert.match(dialogueRuntimeTestSource, /legacy resolved 4\.5/);
assert.match(dialogueRuntimeTestSource, /must not be labelled CPA/);

const controlsSource = functionSource("setDialogueInFlightControls", "renderSuggestedReplies");
assert.match(controlsSource, /byId\("scene-input"\)\.disabled = locked \|\| closed/);
assert.match(controlsSource, /byId\("scene-submit"\)\.disabled = locked \|\| closed/);
assert.match(controlsSource, /\[data-example\], #suggested-replies button/);
assert.match(source, /if \(!dialogueGuard\.inFlight\) byId\("scene-form"\)\.requestSubmit\(\)/);
assert.match(source, /document\.querySelectorAll\("\[data-example\]"\)[\s\S]*if \(dialogueGuard\.inFlight\) return/);

console.log("web static S13 text-model/catalog-image-display/provenance/trace/gates contract: PASS");
