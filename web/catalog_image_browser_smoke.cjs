"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { CATALOG_ASSET_VERSION, isOwnerBoundCatalogImageUrl } = require("./image_asset_runtime.js");

const chromeCandidates = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
].filter(Boolean);
const chromePath = chromeCandidates.find((candidate) => fs.existsSync(candidate));
assert.ok(chromePath, "Chrome or Edge is required for the catalog image browser smoke");

const demoUrl = process.env.PROFAGENT_SMOKE_URL || "http://127.0.0.1:8000";
const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "profagent-catalog-smoke-"));
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
    demoUrl
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
    const evaluate = async (expression) => {
      const result = await cdp.call("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "browser evaluation failed");
      return result.result.value;
    };

    const collapsed = await waitFor(async () => {
      const value = await evaluate(`(() => ({ categories: document.querySelectorAll("details.wardrobe-category").length, cards: document.querySelectorAll(".garment-card").length }))()`);
      return value.categories > 0 ? value : null;
    }, 15000, "wardrobe categories did not render");
    assert.ok(collapsed.categories > 0, "wardrobe must render category summaries");
    assert.equal(collapsed.cards, 0, "collapsed wardrobe must initially render zero garment cards");

    const opened = await evaluate(`(() => {
      const details = document.querySelector("details.wardrobe-category");
      if (!details) return false;
      details.open = true;
      details.dispatchEvent(new Event("toggle"));
      return true;
    })()`);
    assert.equal(opened, true);
    const cardHtml = await waitFor(
      () => evaluate(`document.querySelector(".garment-card:has(.garment-catalog-image.is-loaded)")?.outerHTML || ""`),
      15000,
      "real browser must load at least one validated owner-bound catalog image after expanding a category"
    );
    const widthMatch = cardHtml.match(/data-catalog-natural-width="(\d+)"/);
    assert.ok(widthMatch && Number(widthMatch[1]) > 0, "real catalog image naturalWidth must be greater than zero");
    assert.match(cardHtml, /class="[^"]*garment-catalog-badge[^"]*"(?![^>]*\shidden(?:=|\s|>))[^>]*>/);
    assert.match(cardHtml, /class="[^"]*garment-swatch[^"]*"[^>]*\shidden(?:=""|="hidden")/);
    assert.match(cardHtml, /AI 生成目录参考 · 请求模型已固定，CPA 未回报实际模型/);
    const sourceMatch = cardHtml.match(/src="([^"]*\/wardrobe\/(g\d+)\/catalog-image\?[^"]+)"/);
    assert.ok(sourceMatch, "loaded image must retain the backend-provided owner-bound URL");
    const imageUrl = sourceMatch[1].replaceAll("&amp;", "&");
    const contentSha256 = new URL(imageUrl, "https://profagent.invalid").searchParams.get("content_sha256");
    assert.equal(isOwnerBoundCatalogImageUrl(imageUrl, "u01", sourceMatch[2], CATALOG_ASSET_VERSION, contentSha256), true);
    console.log(`web wardrobe accordion real-browser smoke: PASS collapsedCards=0 naturalWidth=${widthMatch[1]}`);
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
      // A Chromium child can briefly retain Windows profile locks after the page closes.
    }
  }
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
