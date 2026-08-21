"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const runtime = fs.readFileSync(path.join(__dirname, "memory_runtime.js"), "utf8");

assert.match(html, /memory_runtime\.js\?v=s15-memory-visibility-20260819/);
assert.match(html, /Working context 使用 Session \+ TTL，只服务当前任务，不是长期记忆/);
assert.match(html, /不会作为长期记录从此列表返回/);

assert.match(app, /user_confirmed: "用户确认"/);
assert.match(app, /user_edited_confirmed: "用户编辑后确认"/);
assert.match(app, /feedback_confirmed: "反馈经用户确认"/);
assert.match(app, /legacy_migrated: "旧版记录迁移"/);
assert.match(app, /explicit_confirm_v1: "显式确认 v1"/);
assert.match(app, /legacy_confirm_v0: "旧版确认 v0"/);
assert.match(app, /确认协议：\$\{consent\}/);
assert.match(app, /本条已替代一条较早记录/);
assert.match(app, /过期、被替代或删除的记录由服务端移出有效集合，浏览器不会按缓存恢复/);

assert.doesNotMatch(runtime, /"working_context"/);
assert.match(runtime, /raw\.lifecycle_status === "active"/);
assert.match(runtime, /raw\.styling_session_id == null/);
assert.match(runtime, /Date\.parse\(raw\.valid_to\) > effectiveNow/);
assert.match(runtime, /raw\.expires_at === raw\.valid_to/);
assert.match(runtime, /config\.expectedMemoryClass\(raw\.type, raw\.content\) === raw\.memory_class/);
assert.match(runtime, /raw\.supersedes_memory_id !== raw\.memory_id/);
assert.match(runtime, /raw\.source === "user_confirmed"/);
assert.match(runtime, /legacyTuple \|\| currentTuple/);
assert.match(runtime, /typeof raw\.commit_blocked === "boolean"/);
assert.match(runtime, /Date\.parse\(raw\.updated_at\) >= Date\.parse\(raw\.created_at\)/);
assert.match(runtime, /raw\.proposal\.content === request\.content/);
assert.match(runtime, /raw\.proposal\.ttl_days === request\.ttl_days/);
assert.match(runtime, /raw\.proposal\.styling_session_id === request\.styling_session_id/);
assert.match(runtime, /normalizedProposal\.ttl_days !== expected\.proposal\.ttl_days/);
assert.match(runtime, /normalizedProposal\.created_at !== expected\.proposal\.created_at/);
assert.match(runtime, /normalizedRecord\.ttl_days !== expected\.proposal\.ttl_days/);
assert.match(runtime, /raw\?\.user_id !== config\.userId/);
assert.match(runtime, /metadata_valid: false/);
assert.match(runtime, /actionable: false/);

const memoryStart = app.indexOf("function memoryTemplatesFor");
const memoryEnd = app.indexOf("function sanitizeDebugValue", memoryStart);
const memoryUi = app.slice(memoryStart, memoryEnd);
assert.doesNotMatch(memoryUi, /\brrf\b|rerank|outbox|final_score|context_match|confirmation_strength/i);
assert.doesNotMatch(memoryUi, /item\.source\b|record\.source\b|proposal\.source\b/);
assert.match(app, /\(rrf\|rerank\|outbox\)\/i\.test\(key\)/);
assert.match(app, /query_text\|conversation\|transcript\|message\|reply\|prompt\|content/);

const deleteStart = app.indexOf("async function deleteMemory");
const deleteEnd = app.indexOf("function sanitizeDebugValue", deleteStart);
const deleteBlock = app.slice(deleteStart, deleteEnd);
const receiptCheck = deleteBlock.indexOf('memoryRuntime.normalizeMutationReceipt(response, { operation: "delete", item }');
const localRemoval = deleteBlock.indexOf("state.memoryRecords = state.memoryRecords.filter");
assert.ok(receiptCheck >= 0 && localRemoval > receiptCheck, "local record must remain until the owner-bound delete receipt is verified");
assert.match(deleteBlock, /catch \(error\)[\s\S]*button\.disabled = false/);

assert.match(app, /memoryRuntime\.normalizeList\(response, memoryRuntimeConfig\(\)\)/);
assert.match(app, /normalizeMutationReceipt\(response, \{[\s\S]*operation: "propose",[\s\S]*request: \{ \.\.\.proposePayload, sensitivity: "non_sensitive" \}/);
assert.match(app, /styling_session_id: state\.stylingSessionId \|\| null,[\s\S]*ttl_days: null/);
assert.match(app, /proposalId: proposal\.proposal_id/);
assert.match(runtime, /normalizedProposal\.record_id !== normalizedRecord\.memory_id/);
assert.match(runtime, /normalizedRecord\.source_proposal_id !== normalizedProposal\.proposal_id/);
assert.match(runtime, /raw\.user_id !== config\.userId/);
assert.match(runtime, /raw\.truth_version >= 2/);
assert.match(app, /proposal\.metadata_valid !== true[\s\S]*该项不可确认，也不会进入长期记忆/);
assert.match(app, /record\.deletable === true[\s\S]*deleteMemory/);
assert.match(app, /长时间站立时优先选择适合久走的鞋。/);

console.log("web S15 memory UI/static visibility contract: PASS");
