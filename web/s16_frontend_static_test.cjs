"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "styles.css"), "utf8");
const runtime = fs.readFileSync(path.join(__dirname, "memory_candidate_runtime.js"), "utf8");

assert.match(html, /<textarea id="memory-free-text"[^>]*><\/textarea>/);
assert.doesNotMatch(html, /id="memory-type"|id="memory-content"|受控模板/);
assert.doesNotMatch(html, /id="memory-sensitive"/);
assert.match(html, /memory_candidate_runtime\.js\?v=([^"\s]+)/);
assert.ok(html.indexOf("memory_candidate_runtime.js") < html.indexOf("app.js"));

const buildMatch = app.match(/const UI_BUILD_VERSION = "([^"]+)";/);
assert.ok(buildMatch);
assert.match(html, new RegExp(`memory_candidate_runtime\\.js\\?v=${buildMatch[1]}`));

assert.match(runtime, /assertExactKeys\(payload, \["request_id", "status", "candidates", "trace_id"\]\)/);
assert.match(runtime, /assertExactKeys\(candidate, \["candidate_id", "confirmation_copy", "confidence_band", "conflict_copy", "allowed_actions"\]\)/);
assert.match(runtime, /assertExactKeys\(payload, \["candidate_id", "decision", "status", "record_id", "trace_id"\]\)/);
assert.match(runtime, /return Object\.freeze\(\[\.\.\.candidate\.allowed_actions\]\)/);
assert.doesNotMatch(runtime, /localStorage|sessionStorage|canonical_kind|canonical_value|provider_reasoning|provider_body/);

const flowStart = app.indexOf("function createMemoryCandidateFlowController");
const flowEnd = app.indexOf('if (typeof module === "object" && module.exports)', flowStart);
assert.ok(flowStart >= 0 && flowEnd > flowStart);
const flowSource = app.slice(flowStart, flowEnd);
const formStart = flowSource.indexOf("async function submit");
const decisionStart = flowSource.indexOf("async function decide", formStart);
const keyStart = flowSource.indexOf("function handleComposerKey", decisionStart);
assert.ok(formStart >= 0 && decisionStart > formStart && keyStart > decisionStart);
const formSource = flowSource.slice(formStart, decisionStart);
const decisionSource = flowSource.slice(decisionStart, keyStart);
assert.match(formSource, /const sourceText = dependencies\.input\.value\.trim\(\)/);
assert.match(formSource, /dependencies\.request\("\/memory\/candidates\/extract"/);
assert.match(formSource, /dependencies\.normalizeExtraction/);
assert.ok(
  formSource.indexOf("normalizeExtraction") < formSource.indexOf('dependencies.input.value = ""'),
  "the input must clear only after a bound response"
);
assert.match(formSource, /user_id: context\.userId/);
assert.match(formSource, /styling_session_id: context\.sessionId \|\| null/);
assert.match(formSource, /namespace: context\.namespace/);
assert.match(formSource, /text: sourceText/);
assert.match(formSource, /request_id: requestId/);
assert.doesNotMatch(formSource, /state\.[A-Za-z0-9_]+\s*=\s*sourceText|localStorage|sessionStorage|fetchTrace\([^)]*sourceText/);

assert.match(app, /memoryCandidateRuntime\.candidateActions\(candidate\)/);
assert.match(app, /candidate\.confirmation_copy/);
assert.match(app, /candidate\.conflict_copy/);
assert.doesNotMatch(app, /candidate\.canonical_kind|candidate\.canonical_value|candidate\.provider/);
assert.match(decisionSource, /dependencies\.request\(`\/memory\/candidates\/\$\{encodeURIComponent\(origin\.candidateId\)\}\/decide`/);
assert.match(decisionSource, /dependencies\.normalizeDecision/);
assert.match(decisionSource, /receipt\.decision !== action/);
assert.match(decisionSource, /dependencies\.input\.focus\(\)/);
assert.doesNotMatch(decisionSource, /canonical_kind|canonical_value|content|type|memory_class|supersedes/);

assert.match(flowSource, /dependencies\.shouldSubmitKey\(event\)/);
assert.match(flowSource, /form\.requestSubmit\(\)/);
assert.match(app, /normalizeExtraction: memoryCandidateRuntime\.normalizeExtractionResponse/);
assert.match(app, /normalizeDecision: memoryCandidateRuntime\.normalizeCandidateDecision/);
assert.match(app, /function resetSessionScopedState\(stylingSessionId\) \{[\s\S]*?memoryCandidateFlow\.reset\(\)/);
assert.match(app, /memoryCandidateFlow\.handleComposerKey\(event, byId\("memory-propose-form"\)\)/);
assert.match(css, /\.memory-candidate-card/);
assert.match(css, /\.memory-candidate-actions/);
assert.doesNotMatch(app, /memoryTemplatesFor|renderMemoryTemplateOptions|isApprovedMemoryTemplate/);

console.log("web S16 free-text memory UI/static contract: PASS");
