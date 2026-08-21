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

const formStart = app.indexOf("async function handleMemoryCandidateExtract");
const decisionStart = app.indexOf("async function decideMemoryCandidate", formStart);
const deleteStart = app.indexOf("async function deleteMemory", decisionStart);
assert.ok(formStart >= 0 && decisionStart > formStart && deleteStart > decisionStart);
const formSource = app.slice(formStart, decisionStart);
const decisionSource = app.slice(decisionStart, deleteStart);
assert.match(formSource, /const sourceText = input\.value\.trim\(\)/);
assert.match(formSource, /api\("\/memory\/candidates\/extract"/);
assert.match(formSource, /memoryCandidateRuntime\.normalizeExtractionResponse/);
assert.ok(
  formSource.indexOf("normalizeExtractionResponse") < formSource.indexOf('input.value = ""'),
  "the input must clear only after a bound response"
);
assert.match(formSource, /user_id: userId/);
assert.match(formSource, /styling_session_id: state\.stylingSessionId \|\| null/);
assert.match(formSource, /namespace: byId\("memory-namespace"\)\.value/);
assert.match(formSource, /text: sourceText/);
assert.match(formSource, /request_id: requestId/);
assert.doesNotMatch(formSource, /state\.[A-Za-z0-9_]+\s*=\s*sourceText|localStorage|sessionStorage|fetchTrace\([^)]*sourceText/);

assert.match(app, /memoryCandidateRuntime\.candidateActions\(candidate\)/);
assert.match(app, /candidate\.confirmation_copy/);
assert.match(app, /candidate\.conflict_copy/);
assert.doesNotMatch(app, /candidate\.canonical_kind|candidate\.canonical_value|candidate\.provider/);
assert.match(decisionSource, /api\(`\/memory\/candidates\/\$\{encodeURIComponent\(candidate\.candidate_id\)\}\/decide`/);
assert.match(decisionSource, /memoryCandidateRuntime\.normalizeCandidateDecision/);
assert.match(decisionSource, /receipt\.decision !== action/);
assert.match(decisionSource, /byId\("memory-free-text"\)\.focus\(\)/);
assert.doesNotMatch(decisionSource, /canonical_kind|canonical_value|content|type|memory_class|supersedes/);

assert.match(app, /dialogueRuntime\.shouldSubmitComposerKey\(event\)/);
assert.match(app, /byId\("memory-propose-form"\)\.requestSubmit\(\)/);
assert.match(css, /\.memory-candidate-card/);
assert.match(css, /\.memory-candidate-actions/);
assert.doesNotMatch(app, /memoryTemplatesFor|renderMemoryTemplateOptions|isApprovedMemoryTemplate/);

console.log("web S16 free-text memory UI/static contract: PASS");
