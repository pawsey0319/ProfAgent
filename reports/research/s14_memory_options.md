# S14 个性化 Agent Memory 方案调研

> 日期：2026-08-19
> 状态：只读决策材料；**未修改 Memory 生产实现、schema、RRF 权重或用户数据**。

## 结论

ProfAgent 当前最适合继续采用“事务真值 + 可重建派生索引”，而不是让知识图谱或第三方框架成为唯一记忆数据库：

- PostgreSQL（Demo 离线为 SQLite）负责确认状态、ACL、敏感度、TTL、删除、supersede、来源与事务一致性；
- hard memory 继续按 SQL 精确读取，不能进入任何相似度排序；
- soft memory 才进入 BM25 / Dense / Recency / Importance 与 Weighted RRF；
- LangMem、Mem0 或 Graphiti 若后续接入，只能消费已确认、非敏感的 outbox 事件，搜索结果必须回 PostgreSQL 复验；
- 移动端与进程重启恢复靠服务端持久数据库、稳定 user/session 身份和可重建索引解决，知识图谱本身不能解决进程丢失。

## 方案 A：SQL 事件真值 + 派生混合索引（推荐现在采用）

```text
对话/反馈
  → 敏感与来源检查
  → 模型只生成结构化候选
  → schema/ACL 校验
  → 用户确认
  → PostgreSQL 原子提交 + audit/outbox
  → BM25/Dense/Recency/Importance 派生索引
  → SQL 预过滤
  → Weighted RRF Top 5
```

建议把记忆分为：

1. `profile_current`：称呼、沟通风格、稳定审美等当前状态；
2. `hard_constraints`：禁忌、不穿项、授权与拒绝规则，只走 exact SQL；
3. `preference_events`：喜欢、拒绝、评分和穿搭反馈，保留事件历史；
4. `episodic_summaries`：经用户确认的代表性场景，不保存模型思维链；
5. `working_context`：当前会话 checkpoint + TTL，不冒充长期记忆。

下一步只需扩展结构化字段：`valid_from/valid_to/supersedes/source/provenance/consent_version`，并增加 outbox、索引重建与记忆管理页。

优点：迁移最小、延迟最低、重启可靠、完整保留现有 `propose→confirm→commit` 与 ACL。缺点是冲突合并、提取和评测需自行维护。

## 方案 B：方案 A + LangMem 后台候选提取

LangMem 区分 semantic、episodic 与 procedural memory，也区分单一 profile 与可搜索 collection；其文档说明 hot-path 写记忆会增加用户可感知延迟，因此本项目只应在回复完成后后台生成候选。[LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)

接法：LangMem 只生成 Profile/Preference/Episode 候选，仍经过本项目敏感过滤和用户确认，再由 PostgreSQL 提交；不启用 procedural prompt 自动修改，防止 Stylist 人格或安全规则被模型自改。

优点：较快获得自然语言候选抽取，迁移中低。缺点：增加 LangChain 依赖，候选仍可能过度记忆、误合并或受到提示注入。

## 方案 C：方案 A + Mem0 软检索侧车

Mem0 采用关系数据库、向量索引与实体索引组合；自动路径需要 LLM 提取，纠正/删除也需要显式操作。[How Mem0 works](https://docs.mem0.ai/core-concepts/how-it-works) [Entity-scoped memory](https://docs.mem0.ai/platform/features/entity-scoped-memory)

安全接法：PostgreSQL 先提交已确认真值，再通过 outbox 用 `infer=False` 镜像规范化、非敏感记录；Mem0 结果只能成为 soft-memory 的一个 RRF 通道，且须回表验证 user/team/member/namespace、有效期和删除状态。

优点：SDK 与托管平台接入快，语义/关键词/实体/时间检索开箱度较高。缺点：双写删除一致性、数据出域、供应商成本和 ACL 适配复杂；当前单用户/小衣橱规模收益可能不足。

## 方案 D：方案 A + Graphiti 时序知识图谱

Graphiti 适合双时间事实、事实失效、episode provenance，以及 semantic + BM25 + graph traversal；入图需要 LLM/embedding，自托管通常还需要图数据库。[Graphiti overview](https://help.getzep.com/graphiti/getting-started/overview) [Quick start](https://help.getzep.com/graphiti/getting-started/quick-start)

可表达：用户偏好风格、拒绝过的调整、场景采用过的 Look、Look 包含衣物，以及事实的有效时间。只把 PostgreSQL 已确认事件投影进图；图结果仍回表校验，图通道失败就从 RRF 中移除。

优点：最适合“偏好何时变化”“过去为何推荐”“场景—Look—衣物多跳关系”。缺点：入图成本、图数据库、ACL 投影、删除传播和运维明显更高；当前阶段属于过度设计。

## 暂不推荐：Letta/MemGPT 全栈迁移

Letta 的 memory blocks 与持久 agent runtime 适合从零构建能自维护状态的长期伴侣，但 agent 自编辑记忆、常驻上下文和 runtime 迁移与当前用户确认链、FastAPI 工具合同冲突。[Letta memory blocks](https://docs.letta.com/tutorials/attaching-detaching-blocks/) [MemGPT paper](https://arxiv.org/abs/2310.08560)

只有未来 ProfAgent 转成 always-on companion 时才值得单独立项，不应作为当前侧车。

## 对比

| 方案 | 迁移 | 热路径延迟 | 运维/成本 | 时序多跳 | 当前适配度 |
|---|---:|---:|---:|---:|---:|
| A SQL 真值演进 | 低 | 最低 | 低—中 | 中 | 最高 |
| B + LangMem | 中低 | 低（后台） | 中 | 中 | 高 |
| C + Mem0 | 中 | 中 | 中 | 中 | 中 |
| D + Graphiti | 高 | 检索低、入图高 | 高 | 最高 | 当前较低 |
| Letta 全迁移 | 很高 | 中—高 | 高 | 中 | 不推荐 |

## 推荐决策顺序

1. 现在选择方案 A；先补结构化 profile、事件历史、时效、outbox 与用户记忆管理页。
2. 候选抽取维护成本成为瓶颈时，增加方案 B。
3. 软召回离线评测不足时，用 shadow traffic 比较方案 C。
4. 只有时间多跳问题在真值集里稳定出现，才部署方案 D。

## 需要用户确认

1. 长期记忆是否坚持“每条都明确确认”，还是允许后台生成候选后集中确认？
2. 是否保存经确认的成功/失败穿搭经历，还是只保存结构化偏好和反馈？
3. 非敏感记忆能否发送第三方托管服务，还是全部要求自托管？
4. 当前是否真有“偏好如何随时间变化”的产品需求？若没有，只做 graph-ready schema。
5. 移动端是否需要查看、编辑、删除、导出每条记忆及来源？
6. Stylist 人格和安全规则是否永久禁止 procedural memory 自动修改？建议禁止。

## 最低验收线

- hard-memory exact 命中率 100%；
- 跨用户/team/member ACL 泄漏 0；
- 敏感持久化 0；
- 未确认、已拒绝、已删、过期、superseded 召回 0；
- 重启前后 active memory 与排序一致；
- 索引删除/重建后无残留；
- 外部 memory/graph 服务失败时，SQL hard rules 与现有 RRF 继续运行。

安全研究还表明普通查询即可污染长期记忆，支持继续保留确认写入防线：[MINJA](https://arxiv.org/abs/2503.03704)。长期记忆也存在隐私提取风险，支持最小化存储、ACL、敏感默认不写与可验证删除：[MEXTRA](https://arxiv.org/abs/2502.13172)。Mem0 的论文可参考 [arXiv:2504.19413](https://arxiv.org/abs/2504.19413)。

来源访问日期：2026-08-19。厂商 benchmark 数值未作为推荐依据。agent-reach `v1.5.0` 已为最新版本。
