"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "styles.css"), "utf8");
const runtime = fs.readFileSync(path.join(__dirname, "memory_candidate_runtime.js"), "utf8");
const preferenceRuntime = fs.readFileSync(path.join(__dirname, "preference_clarification_runtime.js"), "utf8");

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

assert.match(app, /await import\("\.\/preference_clarification_runtime\.js"\)/);
assert.match(app, /preferenceClarificationRuntime\.normalizePreferenceClarification\(raw\.preference_clarification\)/);
assert.match(app, /preferenceClarificationRuntime\.normalizeDialogueMemoryCandidates/);
assert.match(app, /preferenceClarificationFlow\.acceptResponse\([\s\S]*?applyDialogueTurn\(response\)/);
assert.match(app, /dialogueRuntime\.shouldSubmitComposerKey\(event\)[\s\S]*?if \(!dialogueGuard\.inFlight\) byId\("scene-form"\)\.requestSubmit\(\)/);
assert.match(app, /preference_question_id = boundPreferenceTurn\.preference_question_id/);
assert.match(app, /preference_option_id = boundPreferenceTurn\.preference_option_id/);
assert.match(app, /function resetSessionScopedState\(stylingSessionId\) \{[\s\S]*?preferenceClarificationFlow\.reset\(\)/);
assert.match(app, /"dialogue-memory-candidate-title", "记忆确认（不影响本次推荐）"/);
assert.match(app, /state\.memoryCandidates = \[\.\.\.state\.memoryCandidates, \.\.\.response\.memory_candidates\]/);
assert.match(css, /\.preference-clarification-card/);
assert.match(css, /\.dialogue-memory-candidate-card/);

assert.match(preferenceRuntime, /\["question_id", "gap_code", "status", "options", "urgency_budget"\]/);
assert.match(preferenceRuntime, /\["option_id", "label"\]/);
assert.match(preferenceRuntime, /questionByGapCode/);
assert.match(preferenceRuntime, /gapByQuestionId/);
assert.match(preferenceRuntime, /responseContext\.urgency === "high" \? "last" : "normal"/);
assert.match(preferenceRuntime, /preference_question_id: normalizedQuestion\.question_id/);
assert.match(preferenceRuntime, /preference_option_id: authoritativeOption\.option_id/);
assert.doesNotMatch(preferenceRuntime, /localStorage|sessionStorage|server_ranking_score|shopping_suggestions|\/memory\//);
assert.doesNotMatch(preferenceRuntime, /canonical_kind|canonical_value|provider_reasoning|provider_body/);
assert.doesNotMatch(app, /preferenceClarification[^\n]*\.sort\(|preferenceClarification[^\n]*server_ranking_score/);

assert.match(html, /<h2 id="member-name">私人 Stylist<\/h2>/);
assert.match(html, /id="member-status"[^>]*>[^<]*<span aria-hidden="true">●<\/span> 正在确认<\/span>/);
assert.match(html, /aria-label="私人 Stylist 能力"/);
assert.match(html, /<strong>私人 Stylist<\/strong>/);
assert.match(html, /aria-label="私人 Stylist 对话"/);
assert.match(html, /<span class="message-author">私人 Stylist<\/span>/);
assert.match(html, /<label for="scene-input">和私人 Stylist 说点什么<\/label>/);
assert.match(html, /<span>发送给私人 Stylist<\/span>/);
assert.match(html, /<option value="stylist">仅私人 Stylist<\/option>/);
assert.match(app, /const PRIVATE_STYLIST_LABEL = "私人 Stylist"/);
assert.match(app, /createVisibleMemberIdentityBinding\(team\)/);
assert.match(app, /member\?\.member_id === "stylist" && member\?\.persona_id === "stylist"/);
assert.match(app, /const available = memberBinding\.trusted && member\.status === "available"/);
assert.doesNotMatch(app, /byId\("member-name"\)\.textContent = member\.name/);
assert.doesNotMatch(app, /appendMessage\("Stylist"|"正在和 Stylist 聊天"|"发送给 Stylist"|`Stylist 会话|"Stylist 理解为"|"仅 Stylist"/);
assert.doesNotMatch(html, /aria-label="Stylist (?:能力|对话)"|>Stylist<|和 Stylist 说点什么|发送给 Stylist|仅 Stylist/);

console.log("web S16 free-text memory UI/static contract: PASS");
