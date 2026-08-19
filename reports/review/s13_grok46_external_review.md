# S13 Grok 4.6 外部对抗审查

- 调用日期：2026-08-19
- 用户指定模型：Grok 4.6
- CPA 实时精确模型 ID：`grok-4.6-high`
- 角色：ProfAgent S13 safety / contract truthfulness / MVP acceptance reviewer
- 输入：`reports/review/s13_joint_review_packet.md`（不含密钥、原始对话、用户画像或图片正文）
- 调用结果：成功

## 外部结论

Grok 认为 S13 可以作为 **MVP Demo 增量**关闭，但不能宣称生产就绪；证据未显示 P0 产品缺陷。它将下列事项作为需由 Codex 回到本地代码与测试核验的建议发现：

### [P1] 图片 Provider 隔离证据需具体点名

需证明图片生成请求只使用 `grok-imagine-image-quality`，不会把文本 `grok4.6/grok-4.6-high` 或文本 allowlist 泄漏到图片请求；目录图显示冒烟本身不能替代图片生成请求合同测试。

### [P1] 错模型 fail-closed 与单回合唯一 CPA 调用证据需具体点名

需展示旧 4.5、无后缀 4.6、前缀变体及其他模型的拒绝测试，并证明超时、重试与幂等回执不会在同一回合产生第二次 CPA 调用；模型输出不能覆盖本地购物、ID 或 Memory 权威。

### [P2] 同一图片元素复用时的旧图竞态

需证明已显示 owner A 图片的卡片若重新绑定到 pending/失败的 owner B，不会继续显示 A 的像素或旧 `src`；成功仍须由 `complete && naturalWidth>0`/load 合同决定，取消和 stale load 不得写回。

### [P2] 评分、调整、Look 与 Memory 门禁需具体点名

需指出 252 项测试中仍覆盖：不评价人、每轮最多两项调整且拒绝不重复、Look 不可变、Memory propose-confirm-commit 与敏感默认不写的测试。

### [P2] 历史 4.5 记录需防止操作员误读

建议在历史记录附近明确标注：历史 4.5 仅供追溯，当前运行 allowlist 是 `{grok-4.6-high, grok-4.6-build}`。

## Grok 对 S13 的总体判断

- 高急购物门控、Catalog 0、ID/硬约束固定评测、真实 CPA 4.6 成功、目录图 owner/provenance fail-closed 和测试抖动修复方向均可接受。
- 图片生命周期修复对 MVP 条件性充分，但应以元素复用/缓存竞态测试完成证据闭环。
- 测试抖动修复可以接受，前提是独立 50ms 超时/取消测试继续保留，生产 120/125 秒不变。
- 不得对外宣称生产就绪、风险被“消除”，或把通过数量等同独立审计。

> 本报告是外部模型建议证据，不替代 Codex 对本地代码、测试、合同与运行结果的逐项复核。
