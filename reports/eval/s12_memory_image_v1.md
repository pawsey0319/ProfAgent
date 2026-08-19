# S12R Memory + Image 独立验收报告 v1（schema 2）

结论：S12R tester 最终门禁 PASS；P0/P1/P2 = 0/0/0。未调用真实 CPA，未重新生图，未修改固定 fixtures/eval 真值或阈值。

## 固定门禁

- Full pytest：246 passed。S12 image/memory：21 passed。Preview Static2D：34 passed。
- 固定 eval：Urgency=30/30，高急 ShoppingGate=10/10，Catalog calls=0，衣物幻觉=0/515，硬约束违反=0/423，Slot=74/74。
- validate：exit 0，stdout SHA-256=64c0b718eb76544a2b97805fadd958337504c46075575a43660a57da99e487ce。
- Node：6 个语法检查与 3 套 runtime/static contract，PASS。
- 临时随机非 8000 端口 HTTP：owner={'u01': 28, 'u02': 12, 'u03': 10}，三参数内容寻址 URL、旧版本/错 hash 拒绝、高急 Catalog0、2D-only，PASS。

## 50 张目录 PNG 技术核验

Manifest schema 2：asset_version=wardrobe_generated_v1_s12r2，requested model 固定；model_reported/model_verified=false、resolved=null、basis=batch_exact_request_contract。Manifest=50、PNG=50、fixtures=50，ID/owner 一一对应，bytes/SHA-256/1024×1024/单帧匹配，六个内嵌来源键逐值精确。当前 50 项 prompt_sha256=null、prompt_hash_status=not_preserved；批次不声称逐文件模型回报或 CPA 回执。

## Memory 与图片 Provider 对抗

图片 Provider 分为 A（回报精确模型）与 B（模型未回报但 exact request + CPA receipt）；两类安全 Trace 均不得泄露原始 receipt。字段存在但为 null/空/非字符串/错模/前缀、缺失或非法 receipt、重复 model/data/b64_json/url key 均须拒绝。SSRF/DoS 与全图 Validator 不放宽。

## Supervisor 真实 Provider 证据（外部引用）

Supervisor 报告：exit 0，5.2885s，image/jpeg 228841 bytes，response_source=b64_json；request pinned=true、receipt verified=true、model_reported/model_verified=false、resolved=null、basis=exact_request_with_cpa_trace。本 tester 未重复真实调用。

## Controlled synthetic 消融

数据边界：10 条受控合成 query、24 条受控合成记录；Dense 是 `deterministic_hashed_surrogate_v1`。以下结果不代表生产语义质量或提升。

| 方法 | Recall@5 | Recall@10 | MRR | P95 (ms) |
|---|---:|---:|---:|---:|
| BM25-only | 1.0000 | 1.0000 | 1.0000 | 2.5744 |
| hashed-dense-only | 1.0000 | 1.0000 | 1.0000 | 0.6316 |
| weighted-RRF | 1.0000 | 1.0000 | 0.8833 | 3.2010 |
| RRF + deterministic rerank | 1.0000 | 1.0000 | 1.0000 | 3.1490 |

## 隐私与仓库卫生

Manifest 仅相对路径；报告不含 prompt 正文、API key、base64 payload 或用户绝对路径；`.profagent/memory.sqlite3` 保持 git ignored 且不进入候选。
