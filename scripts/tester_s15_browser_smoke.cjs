"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const chromeCandidates = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
].filter(Boolean);
const chromePath = chromeCandidates.find((candidate) => fs.existsSync(candidate));
assert.ok(chromePath, "Chrome or Edge is required for the S15 memory browser smoke");

const demoUrl = process.env.PROFAGENT_SMOKE_URL;
assert.ok(demoUrl && /^http:\/\/127\.0\.0\.1:\d+$/.test(demoUrl), "a loopback random-port smoke URL is required");
assert.notEqual(new URL(demoUrl).port, "8000", "S15 browser smoke must not touch port 8000");
const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "profagent-s15-memory-smoke-"));
const devtoolsPortFile = path.join(userDataDir, "DevToolsActivePort");
const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function waitFor(predicate, timeoutMs, message) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) return value;
    await delay(100);
  }
  throw new Error(message);
}

async function connectCdp(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  let nextId = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(String(event.data));
    const receipt = pending.get(message.id);
    if (!receipt) return;
    pending.delete(message.id);
    if (message.error) receipt.reject(new Error(message.error.message));
    else receipt.resolve(message.result);
  });
  return {
    call(method, params = {}) {
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    close() { socket.close(); }
  };
}

async function run() {
  const chrome = spawn(chromePath, [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--remote-debugging-port=0",
    `--user-data-dir=${userDataDir}`,
    `${demoUrl}/#memory`
  ], { stdio: "ignore", windowsHide: true });
  let cdp;
  try {
    await waitFor(() => fs.existsSync(devtoolsPortFile), 10000, "Chrome DevTools port was not created");
    const [port] = fs.readFileSync(devtoolsPortFile, "utf8").trim().split(/\r?\n/);
    const targets = await waitFor(async () => {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`).catch(() => null);
      return response?.ok ? response.json() : null;
    }, 10000, "Chrome page target was not available");
    const target = targets.find((entry) => entry.type === "page");
    assert.ok(target?.webSocketDebuggerUrl, "Chrome page target missing websocket URL");
    cdp = await connectCdp(target.webSocketDebuggerUrl);
    await cdp.call("Page.enable");
    const evaluate = async (expression) => {
      const result = await cdp.call("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "browser evaluation failed");
      return result.result.value;
    };

    const normal = await waitFor(async () => {
      const value = await evaluate(`(() => ({
        api: document.querySelector("#memory-api-state")?.textContent || "",
        committed: document.querySelector("#memory-committed-list")?.innerText || "",
        working: document.querySelector("#memory-working-list")?.innerText || ""
      }))()`);
      return value.api === "API 可用" && value.committed.includes("已提交长期记忆") ? value : null;
    }, 15000, "real Memory page did not render the persisted committed record");
    assert.match(normal.committed, /偏爱直筒裤/);
    assert.match(normal.committed, /偏好事件/);
    assert.match(normal.committed, /仅 Stylist/);
    assert.match(normal.committed, /用户确认/);
    assert.match(normal.committed, /显式确认 v1/);
    assert.match(normal.working, /仅由当前 Session \+ TTL/);

    const privateSentinel = "PRIVATE_INVALID_MEMORY_BODY_MUST_NOT_RENDER";
    const fakeList = {
      api_version: "r1_demo_v1",
      user_id: "u01",
      namespace: null,
      proposals: [{
        proposal_id: "mprop_invalid_browser_contract",
        user_id: "u01",
        styling_session_id: null,
        namespace: "stylist",
        type: "preference",
        content: privateSentinel,
        ttl_days: null,
        status: "proposed",
        sensitivity: "non_sensitive",
        commit_blocked: false,
        created_at: "2026-08-19T10:00:00+08:00",
        updated_at: "2026-08-19T10:00:00+08:00",
        trace_id: "trace_invalid_browser_contract",
        record_id: null
      }],
      records: [],
      retrieval_index_count: 0
    };
    const injected = `(() => {
      const originalFetch = window.fetch.bind(window);
      const fake = ${JSON.stringify(fakeList)};
      window.__PROFAGENT_S15_FAKE_MEMORY_LIST__ = true;
      window.fetch = (input, init) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/memory?user_id=")) {
          return Promise.resolve(new Response(JSON.stringify(fake), {
            status: 200,
            headers: { "Content-Type": "application/json" }
          }));
        }
        return originalFetch(input, init);
      };
    })();`;
    await cdp.call("Page.addScriptToEvaluateOnNewDocument", { source: injected });
    await cdp.call("Page.reload", { ignoreCache: true });
    await waitFor(
      () => evaluate(`window.__PROFAGENT_S15_FAKE_MEMORY_LIST__ === true`),
      10000,
      "invalid Memory browser fixture was not installed before app startup"
    );

    const invalid = await waitFor(async () => {
      const value = await evaluate(`(() => {
        const card = document.querySelector("#memory-pending-list .memory-item");
        return card ? {
          page: document.body.innerText,
          text: card.innerText,
          buttons: [...card.querySelectorAll("button")].map((item) => item.textContent.trim()),
          state: card.dataset.state || ""
        } : null;
      })()`);
      return value?.state === "blocked" ? value : null;
    }, 15000, "invalid Memory response did not fail closed in the browser");
    assert.doesNotMatch(invalid.page, new RegExp(privateSentinel));
    assert.doesNotMatch(invalid.text, /确认提交|保存模板并确认|拒绝/);
    assert.deepEqual(invalid.buttons, ["从服务端删除此提议"]);
    assert.match(invalid.text, /内容与来源均已脱敏/);
    assert.match(invalid.text, /不可确认/);

    console.log("web S15 Memory real-browser smoke: PASS valid-display=1 invalid-removal-only=1 private-body-rendered=0");
  } finally {
    cdp?.close();
    chrome.kill();
    await Promise.race([
      new Promise((resolve) => chrome.once("exit", resolve)),
      delay(1500)
    ]);
    await delay(250);
    try {
      fs.rmSync(userDataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    } catch (_error) {
      // Chromium children can briefly retain Windows profile locks.
    }
  }
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
