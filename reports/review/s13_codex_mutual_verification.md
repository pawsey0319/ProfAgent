# S13 Grok 4.6 × Codex 相互验证

- 日期：2026-08-19
- 外部建议报告：`reports/review/s13_grok46_external_review.md`
- Codex 裁决依据：本地生产代码、可运行测试、真实 8000 CPA 冒烟、真实浏览器目录图加载和同步后的 v1.14 合同
- 最终结论：`[P0/P1/P2] = 0/0/0`

## 1. 图片 Provider 与文本模型隔离 — Grok [P1]，已关闭

Grok 要求证明 Static2D 的生成请求不会复用 S13 文本模型。Codex 复核：

- `profagent/image_provider.py` 使用独立固定图片模型 `grok-imagine-image-quality`。
- `tests/test_preview_static_2d.py::test_cpa_static_2d_adapter_requests_frozen_model_and_accepts_exact_image_model` 精确检查 `/images/generations` 请求体仅含图片模型、prompt、`n=1`、`1024x1024` 和 `b64_json`；本轮增加反断言，序列化请求体不得包含 `grok4.6` 或 `grok-4.6-high`。
- 同文件的 `test_cpa_static_2d_rejects_present_but_nonexact_model`、receipt 缺失/重复、重复 JSON key、错模型/前缀/MIME/大小测试继续 fail-closed；`test_static_2d_success_does_not_authenticate_text_health` 证明图片成功不会认证文本 Health。

裁决：文本/图片配置、请求、回报验证和 Health 均独立，P1 关闭。

## 2. 文本错模型拒绝与单回合唯一调用 — Grok [P1]，已关闭

Codex 复核并点名以下证据：

- `test_text_model_configuration_drift_fails_closed_without_auto_switch`：旧逻辑 `grok4.5` 配置和 Provider 构造直接拒绝；本轮补充无后缀回报 `grok-4.6` 不在 allowlist 的明确拒绝。
- `test_support_cpa_failure_or_violation_falls_back_locally`：旧 alias/transport/build、4.6 前缀和其他私有模型元数据均返回受控 fallback，且不启动专业工具。
- `test_health_requires_exact_high_transport_advertisement`：Health 目录只接受精确 `grok-4.6-high` 广告；旧4.5、build广告和前缀不能冒充可路由 transport。
- `web/dialogue_runtime_test.cjs`：前端拒绝旧4.5、无后缀4.6、transport/resolved前缀、其他模型、未验证和 degraded 来源，不把它们标成 CPA 回复。
- `test_single_flight_same_request_calls_support_cpa_once`、`test_single_flight_same_request_does_not_repeat_recommend_or_catalog`、`test_each_new_turn_calls_cpa_once_receipt_retry_and_context_are_bounded`：并发同 request、幂等回执和连续新回合均执行 single-flight/每新回合最多一次。
- `test_injected_short_dialogue_budget_times_out_once_and_receipt_calls_zero`：50ms 超时会取消子任务，同 request 重试复用回执，总 CPA 调用仍为 1。
- `test_high_urgency_provider_shopping_prose_is_rejected_and_catalog_stays_zero`、`test_dialogue_rejects_all_internal_garment_and_catalog_ids`、`test_provider_prompt_injection_output_fails_closed_without_side_effects`：迟到或违规模型输出不能覆盖购物、ID、Memory/工具权威。

裁决：P1 关闭；不存在自动换模或同回合第二次 CPA。

## 3. 目录图片旧元素/缓存竞态 — Grok [P2]，已关闭

Grok 提醒“若同一 `HTMLImageElement` 被跨 owner 复用”可能显示旧像素。Codex 检查实际渲染路径：

- `web/app.js::renderWardrobe` 每次筛选都在 `items.map` 中新建 `article`、`img`、badge 和 swatch，最后以 `grid.replaceChildren(...)` 替换整组节点；不存在将 owner A 的同一 `img` 重新绑定给 owner B 的生产路径。
- `bindCatalogImageLifecycle` 在赋 `src` 前安装 load/error，成功必须 `naturalWidth>0`；同 card 的新 lifecycle token 会使旧 load/error 成为 stale no-op，cancel 也移除监听器。
- `web/image_asset_runtime_test.cjs` 覆盖 pending、正常 load、error、cached-complete、cached-broken、cancel 后迟到事件，以及共享 card 上旧 lifecycle 被新 token 取代；真实浏览器还确认 owner-bound PNG `naturalWidth=1024`、badge 显示、swatch 隐藏。

裁决：Grok 描述的“同一 img 跨 owner 复用”前提在生产代码中不成立，且相邻 stale/cached 风险已有运行时与浏览器证据；P2 关闭。

## 4. 评分、调整、Look、Memory 回归 — Grok [P2]，已关闭

252 项全量中的具体权威用例仍存在并通过：

- `test_look_asset_version_rollback_final_and_owner_contracts`：六维评分卡禁止 `appearance/body/age/sexual_attractiveness`，Look v1→vN、回退新建版本、owner 访问与历史不可变。
- `test_strict_vision_matrix_reject_replay_and_decision_trace`：六维闭集、每轮 `1..2` 个 adjustment、验证视觉证据、拒绝 canonical key 后不重复、重复 decision 拒绝和不可变版本链。
- `test_feedback_memory_causal_chain_and_memory_safety`：propose 后无 record、跨 owner confirm 404、confirm 后才 commit、敏感/自由文本 content-free 且不可提交、删除后索引与行为恢复。
- `test_memory_cross_instance_confirm_is_transactionally_idempotent`、`test_memory_sql_acl_metadata_is_authoritative_not_caller_namespace`、`test_memory_hard_signals_bypass_rrf_and_soft_prefilter_is_acl_safe`：跨实例 CAS、ACL 与硬/软记忆路径仍闭合。
- `test_cpa_person_proportion_or_standard_body_evaluation_fails_closed` 与对话危险正文矩阵：人物身体/年龄/医疗/3D/video 输出继续拒绝。

裁决：P2 关闭。

## 5. 历史 4.5 操作员误读 — Grok [P2]，已关闭

`BUILD_LOG.md` 已在阶段记录前加入统一提示：S1–S12/S12R 的 4.5 名称只用于历史追溯；S13 当前唯一文本合同为 `grok4.6 → grok-4.6-high → {grok-4.6-high,grok-4.6-build}`。README、PRD v1.14、API Contract 和 AC Matrix 的当前状态段均已同步，API 中残留的 4.5 只在明确标注的 2026-08-10 历史证据中出现。

裁决：P2 关闭。

## 6. 测试抖动复核

一次 Windows 满载 full 在 0.5 秒成功测试预算下取消了 80ms mock；生产 120 秒没有变化，50ms 超时/取消测试继续保留。成功测试预算改为 2 秒后，定向连续 5 次通过、连续两轮 full 均 `252 passed`；本轮补强后再次执行 full 为 `252 passed in 38.44s`。

裁决：这是测试调度抖动，不是生产回归；P0/P1/P2 均不保留。

## 联合终审结论

Grok 的建议发现全部经过 Codex 本地反证或补强并关闭。S13 可以作为 **可运行的 MVP Demo 增量**关闭：文本 CPA 4.6、目录图片真实呈现、购物/ID/硬约束/评分/调整/Look/Memory/2D-only 门禁均有可复现证据。

本结论不代表生产就绪。当前仍使用合成用户、fixture 衣橱和 Mock Catalog；身份认证、Scene/Look/Trace 主数据库、对象存储、生产并发/可用性、完整衣橱编辑、真实交易、真人试穿、3D/360°/视频均未上线。
