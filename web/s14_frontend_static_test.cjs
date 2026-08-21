"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "styles.css"), "utf8");

assert.match(html, /Enter 发送 · Shift \+ Enter 换行/);
assert.match(app, /dialogueRuntime\.shouldSubmitComposerKey\(event\)/);
assert.match(app, /if \(!dialogueGuard\.inFlight\) byId\("scene-form"\)\.requestSubmit\(\)/);
assert.doesNotMatch(app, /\(event\.ctrlKey \|\| event\.metaKey\) && event\.key === "Enter"/);

const wardrobeStart = app.indexOf("function renderWardrobe()");
const wardrobeEnd = app.indexOf("function isCompleteIds", wardrobeStart);
const wardrobe = app.slice(wardrobeStart, wardrobeEnd);
assert.match(wardrobe, /make\("details", "wardrobe-category"\)/);
assert.match(wardrobe, /make\("summary", "wardrobe-category-summary"\)/);
assert.match(wardrobe, /categoryGrid\.dataset\.rendered = "false"/);
assert.match(wardrobe, /if \(!details\.open \|\| categoryGrid\.dataset\.rendered === "true"\) return/);
assert.match(wardrobe, /categoryGrid\.replaceChildren\(\.\.\.categoryItems\.map\(createGarmentCard\)\)/);
assert.equal((wardrobe.match(/categoryGrid\.replaceChildren/g) || []).length, 1, "initial collapsed render must create zero garment cards");
assert.match(css, /\.wardrobe-category-summary:focus-visible/);

const applyStart = app.indexOf("function applyDialogueTurn");
const applyEnd = app.indexOf("function showDialogueFailure", applyStart);
const apply = app.slice(applyStart, applyEnd);
assert.ok(apply.indexOf("renderRecommendations();") < apply.indexOf("generateRecommendationPreviews(response)"), "text/direction cards must render before automatic image generation starts");
assert.match(app, /api\("\/recommend\/previews\/static-2d"/);
assert.match(app, /outfit_ids: expected\.outfits\.map/);
assert.doesNotMatch(app.slice(app.indexOf("body: JSON.stringify({", app.indexOf("function generateRecommendationPreviews")), app.indexOf("timeoutMs: RECOMMENDATION_PREVIEW_TIMEOUT_MS")), /garment|product|prompt|model|render_mode|identity/i);
assert.match(app, /AI 生成的 2D 视觉参考；不代表真实试穿、精确尺码、面料或垂坠。/);
assert.match(app, /recommendationPreviewRuntime\.normalizeBatch/);
assert.match(html, /recommendation_preview_runtime\.js\?v=s15-memory-visibility-20260819/);

console.log("web S14 frontend Enter/accordion/async-preview static contract: PASS");
