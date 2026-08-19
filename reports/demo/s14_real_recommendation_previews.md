# S14 真实 CPA 推荐两图

结论：PASS。这是项目真实 `grok-imagine-image-quality` Provider 证据，不是 mock；使用进程内 TestClient，不监听或触碰 8000。

- 图片 CPA 尝试：2；响应状态：['succeeded', 'succeeded']。
- 批量墙钟（含本地 Scene/Recommendation 设置）：7126.3 ms。
- 尝试 1：succeeded，7059.1 ms，image/jpeg，248145 bytes，SHA-256 `3c329d7a86c2a53c4475044a5491418f7c6a4399a43b65ea9f961449293482a4`
- 尝试 2：succeeded，6845.4 ms，image/jpeg，142301 bytes，SHA-256 `d5575c5755d3b0b6fe7868fa23c0cfef028e2b7ea6ce6224b48271422747f518`

报告不保存 prompt、raw CPA receipt、base64、API key 或图片正文。
