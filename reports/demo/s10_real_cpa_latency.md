# S10 真实 CPA 延迟与能力证据

日期：2026-08-10
环境：`conda torch128`
服务：`http://127.0.0.1:8000`

## 模型合同

- 逻辑模型：`grok4.5`
- CPA transport：`grok-4.5-high`
- 精确回报 allowlist：`grok-4.5-high`、`grok-4.5-build`
- 两条真实 Dialogue 均回报 `grok-4.5-build` 且 `model_verified=true`

## Dialogue 墙钟

| 用例 | 客户端墙钟 | Provider latency | 结果 |
|---|---:|---:|---|
| 简单问候 | 15.970s | 15,912.02ms | `ok / cpa / stylist_chat / chat` |
| 近时约会 + 轻度紧张 | 38.057s | 38,014.74ms | `ok / cpa / styling_active / recommend` |

近时约会用例的权威服务端结果：

- `event_horizon=today`
- `urgency=high`
- `shopping_allowed=false`
- `shopping_cta=false`
- 3 套已验证衣橱方向
- `all_ids_grounded=true`
- `hard_constraints_passed=true`
- Catalog `attempted=false / call_count=0`
- “紧张”只承接一次

## Static2D 当前能力

完整 `Scene → Recommend → Look → /preview/static-2d` 使用相同 high transport。调用墙钟为 0.229s，应用返回 `degraded`、无 `image_url`、`identity=not_assessed`，保留 5 件 owner-bound 衣物且无外部商品。直接 CPA 能力响应为 HTTP 400，明确 `grok-4.5-high` 不支持 `/v1/images/generations` 或 `/v1/images/edits`。

应用没有自动切换到其他图像模型；Static2D 继续使用平铺/文字降级，不伪装生图成功。
