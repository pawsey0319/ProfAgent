# S14 连续搭配 + 推荐 2D + 衣橱折叠独立验收

结论：PASS；tester P0/P1/P2 = 0/0/0。未触碰现有 8000，未修改 production、fixtures、eval 真值或阈值。

## 自动门禁

- S14 backend：26 passed；Dialogue/Preview/购物/Memory 相关：241 passed；full：278 passed。
- Node：11 个 syntax、5 套 runtime/static 合同通过。
- 固定 eval：Urgency 30/30；高急 Gate 10/10；Catalog 0/10；幻觉 0/515；硬约束 0/423；Slots 74/74。
- Enter/Shift+Enter/IME/single-flight、两轮 Scene 继承、requested=2/实际恰好 2、owner/硬约束、CPA 反问/套数冲突/高复读拒绝、Catalog0、预览幂等和单项失败隔离全部通过。

## Mock HTTP 与真实浏览器（不是 CPA 证据）

随机非 8000 HTTP 明确使用 `mock_image_provider_no_external_calls`；mock 两图=['succeeded', 'succeeded']，mock 单项失败=['degraded', 'succeeded']。Chrome：web wardrobe accordion real-browser smoke: PASS collapsedCards=0 naturalWidth=1024。这部分不得解释为真实 CPA 成功。

## 独立真实 CPA 推荐两图

真实 TestClient 隔离调用状态：PASS；image attempts=2；text CPA calls=0；未监听端口且 `port_8000_touched=false`。
- 图 1：succeeded，7059.1 ms，image/jpeg，248145 bytes，SHA-256 `3c329d7a86c2a53c4475044a5491418f7c6a4399a43b65ea9f961449293482a4`
- 图 2：succeeded，6845.4 ms，image/jpeg，142301 bytes，SHA-256 `d5575c5755d3b0b6fe7868fa23c0cfef028e2b7ea6ce6224b48271422747f518`

真实 CPA 与 mock 证据在 JSON 中使用不同 `evidence_kind`，报告不保存 prompt、raw receipt、base64、API key 或图片正文。
