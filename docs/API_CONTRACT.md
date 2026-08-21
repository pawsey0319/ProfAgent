# ProfAgent Demo — API 与协作合同

> 状态：S6–S15 已实现并通过本地验收；S15 Memory 路线 A 已关闭（2026-08-19）
> 需求权威：`docs/PRD.md` v1.16，尤其第 6、9、10、11、12、15、16、23 节；S12 static 2D 是用户明确追加的实验性 R2 纵切，不改变 R1 DoD。
> 若本合同与 PRD 冲突，以 PRD 为准，并由 supervisor 统一更新合同后通知前后端。

## 1. 运行与目录合同

所有 Python、Demo、测试、评测及 Provider 辅助命令必须使用 Conda 环境 `torch128`。

```powershell
# 一条命令启动 API 与 Web Demo
conda run -n torch128 python -m profagent

# 一条命令运行固定评测并输出 JSON + Markdown
conda run -n torch128 python -m profagent.eval
```

默认地址：`http://127.0.0.1:8000`。FastAPI 同进程托管 API 与 `web/` 静态界面。

目录所有权：

| 目录/文件 | owner | 约束 |
|---|---|---|
| `profagent/`、`pyproject.toml` | backend | 应用、领域服务、Repository、Provider、API |
| `web/` | frontend | HTML/CSS/JS 与静态资源；只依赖本合同 |
| `tests/`、`reports/eval/` | tester | 新增测试、固定评测和版本化报告 |
| `docs/API_CONTRACT.md`、`BUILD_LOG.md`、`README.md`、`docs/PRD.md` 0.3 | supervisor | 合同、状态与最终验收 |

共享合同变更必须先交给 supervisor；backend/frontend 不直接并发修改本文件。

## 2. CPA Grok Provider 合同

用户已经冻结逻辑模型名为 `grok4.6`，无需再次询问。应用执行固定映射 `grok4.6 -> transport_model=grok-4.6-high`，不得自行选择其他模型。目录广告不等于实际调用已验证；Health 只把精确广告的 transport `grok-4.6-high` 视为可路由，完整调用后只接受显式精确回报 allowlist `{grok-4.6-high, grok-4.6-build}`。禁止任意前缀匹配、模糊包含或自动选模；旧 4.5 transport、前缀变体和其他模型必须 fail-closed。配置优先级为：`PROFAGENT_*` 环境变量，其次 `GROK_*` / `CPA_*` / `OPENAI_*` 兼容环境变量，最后是 `PROFAGENT_CPA_CONFIG` 指向的本地 JSON 或 `~/.codex/skills/call-grok/config.local.json`。本地 JSON 只读取 `base_url` 与 `api_key`，忽略其中的模型值；密钥不得写入仓库、响应、日志或 Trace：

```text
PROFAGENT_CPA_BASE_URL=http://127.0.0.1:8317/v1
PROFAGENT_CPA_API_KEY=<runtime secret; never commit>
PROFAGENT_GROK_MODEL=grok4.6
PROFAGENT_CPA_TEXT_ENABLED=true
```

- `GrokLLMProvider` 通过 CPA 的 OpenAI-compatible接口生成每个 Dialogue 回合的自然语言正文与结构化建议；不再以闭集策略码拼接本地模板冒充个性化对话。
- 每个未命中幂等 receipt 的 `/dialogue/turn` 必须且最多 attempt 一次 CPA。普通回合发送固定 Stylist persona、脱敏后的当前必要文本、最多 6 条且逐条截断的 session 历史、权威 Scene 摘要、当前 mode/action，以及画像 allowlist：`styles/favorite_colors/avoid_colors/goals/occasions`；`budget` 仅在合法购物场景发送。禁止发送 `name/user_id/profile` 自由文本、联系方式、原始记忆、未确认记忆、敏感医疗/身体内容或图片字节。敏感/安全/终止回合只发送本地分类后的最小摘要，仍可请求自然措辞，但本地安全回复可覆盖任何违规输出。
- Dialogue CPA 返回 JSON，只允许 `reply`、`action`、`control`、`scene_advisory`、`suggested_replies`；兼容的唯一包装是“整个 `content` 恰为一个 `json` 或 `JSON` Markdown 围栏且围栏外只有空白”，解包后仍执行完整合同。围栏外正文、其他语言、多围栏或从自由文本中抽取 JSON 一律拒绝。`action/control` 必须与本地预先给定的必需值完全一致，只是响应闭合校验，不是模型决策权。`scene_advisory` 是可选、不可信提示：结构或枚举非法时服务端只将其置为 `null` 并记录受控诊断码，不得丢弃本来安全的正文，也不得据此改变权威 Scene；顶层额外字段、action/control 冲突或正文/建议越界仍整体拒绝。`reply` 最大 600 个 Unicode 字符，HTTP 响应体最大 64 KiB，`max_tokens` 固定为 800 上限。模型不得直接调用工具、决定 mode、打开购物、产生最终衣物/商品 ID、提交记忆或创建 Look。用户文本、历史、画像、衣物名与模型输出全部是不可信数据。
- 本地编排层先判定不可放宽的安全边界和权威状态，CPA 生成后再执行 Schema、长度、角色/安全语言、购物 CTA、身体/医疗、3D/video、ID 与工具字段 Validator；任何越界整体 fail-closed，使用明确标记的本地降级回复。
- 服务端交互硬预算为 Health `1.5s`、Scene advisory `8s`、Dialogue `120s`、Vision `10s`；环境变量只能收紧。前端 Dialogue 超时固定 `125s`，必须大于服务端预算；等待期间只显示经过秒数，不插入 provisional 或本地 assistant 正文。服务端超时取消 Provider 子任务并返回诚实的 `fallback/local_fallback`，不得回显底层 AbortSignal 文案。
- S12 将实验性 `GrokImageProvider` 与 `/preview/static-2d` 迁移为独立图片合同：固定 `image_model=grok-imagine-image-quality`，通过 CPA `/images/generations` 调用；文本 transport `grok-4.6-high` 不得进入图片请求。不接受 3D/360°/video，也不得因能力不匹配偷偷切换模型。2026-08-18 真实 CPA 协议探测确认该端点成功响应为 `created/data/usage + x-cpa-trace-id`，不回报顶层 `model`。实现不得伪造 `resolved_model`，也不得把“未回报”误说成“已验证”。
- 两类图片成功都必须同时具有服务端发出的 exact requested model、受控格式的单值 `x-cpa-trace-id`、唯一图片体与完整静态图 Validator。响应若存在 `model` 字段，必须是精确等于 `grok-imagine-image-quality` 的字符串，错值、`null`、空串、非字符串或前缀一律拒绝；精确回报时返回 `request_model_pinned/cpa_trace_verified/model_reported/model_verified=true`、精确 `resolved_model`、`verification_basis=reported_model_exact`。若协议完全没有 `model` 字段，才返回 `request_model_pinned/cpa_trace_verified=true`、`model_reported/model_verified=false`、`resolved_model=null`、`verification_basis=exact_request_with_cpa_trace`。UI 必须区分这两类来源；未回报时明示“请求模型已固定，CPA 未回报实际模型”。两类图片成功都不得认证文本 Health，反之亦然；原始 CPA trace 不得进入 API、Trace 或日志。
- Grok 输出永远不是 ID、硬约束、购物门控、评分安全或记忆写入的最终权威。服务端必须重新计算门控并执行白名单/Schema/安全 Validator。
- LLM 失败：规则解析 + 明确标记的本地回复；单次模型输出格式/安全拒绝或 Dialogue 业务预算超时只影响当前回合，不开启 transport 熔断，下一独立回合仍尝试 CPA；网络不可达或错误模型仍可短熔断。静态 2D 失败：衣物卡片/平铺组合/文字解释。两种降级都不得伪装 CPA 个性化或生图成功。
- Trace/日志不得记录 API key、完整原始对话、base64 原图或其他秘密。

## 3. 通用约定

- Content-Type：JSON API 使用 `application/json`；图片上传使用 `multipart/form-data`。
- 时间：ISO 8601，含时区；Demo 默认 `Asia/Shanghai`。
- ID：服务端生成，不接受客户端伪造最终 ID。
- 版本：每个响应包含 `api_version="r1_demo_v1"`；数据版本为 `fixtures_v1.0`。
- `team_id="personal_team"`、`member_id="stylist"`、`persona_id="stylist"` 固定存在。
- `now/today/unknown -> urgency=high -> shopping_allowed=false`。
- 所有用户可见衣物/商品 ID 必须来自本次工具白名单；非法 ID 在响应边界前拒绝。
- 高急切度响应固定含 `ui_capabilities.shopping_cta=false`；前端还需执行本地防御，不渲染任何购物 CTA。

标准错误：

```json
{
  "api_version": "r1_demo_v1",
  "error": {
    "code": "CATALOG_BLOCKED_HIGH_URGENCY",
    "message": "当前场景只使用现有衣橱。",
    "details": {},
    "trace_id": "trace_001",
    "recoverable": true,
    "fallback": "wardrobe_only"
  }
}
```

## 4. 核心数据合同

### 4.1 SceneRequest

PRD 12.1 字段全部保留；允许新增以下字段：

```json
{
  "request_id": "req_001",
  "user_id": "u01",
  "team_id": "personal_team",
  "member_id": "stylist",
  "persona_id": "stylist",
  "styling_session_id": "session_001",
  "query_text": "我今天下午面试，有点紧张，想可靠但别太老气",
  "intent": "recommend",
  "occasion": "interview",
  "event_time": "2026-08-04T15:00:00+08:00",
  "event_horizon": "today",
  "urgency": "high",
  "shopping_allowed": false,
  "goals": ["reliable", "modern"],
  "constraints": {
    "taboo_colors": [],
    "excluded_items": [],
    "comfort_notes": [],
    "required_slots": ["top", "bottom", "shoes"]
  },
  "backend": "grok4.6|rule_fallback",
  "ranking_profile": "rule_bm25_rrf_v1",
  "input_mode": "text_then_image",
  "clarification_required": false,
  "missing_fields": [],
  "assumptions": [],
  "ui_capabilities": {"shopping_cta": false},
  "trace_id": "trace_001"
}
```

服务端必须从 `event_horizon` 重算 `urgency` 与 `shopping_allowed`，不得信任客户端或 LLM 给出的值。自然语言没有时间证据时必须保持 `unknown/high`；仅有“商品/目录/衣橱”等泛化词不得猜测为 `planned`，也不得凭空创建购物缺口。显式结构化 horizon 仅可作为透明输入，且不能把文本或事件时间已经判定出的更高急切度降级。

购物门控还要求 query-local 的明确购买意图与槽位缺口。显式“不买/不要打开商品或目录”优先于低/中急切度和真实缺口，并同时关闭响应 CTA、上层 Catalog 调用与 Catalog 服务层调用。允许购物且存在明确缺口时必须真实查询 Mock Catalog；若独立的预算、场合、季节或 ETA 约束使合法库存为空，应安全返回零商品，不得放宽约束制造结果。

### 4.2 InitialRecommendation

遵循 PRD 12.2；`outfits` 最多三个，不足时合法返回 1–2 个并填写 `gap_explanation`。S14 起，服务端可从当前自然语言回合中的明确“一/两/二/三套（1/2/3 套）”解析 `requested_outfit_count=1|2|3`；客户端与 CPA 均无权提交、放大或改写该值。合法候选足够时响应必须恰好返回该数量；不足时不得复制方向凑数，须返回实际合法数量并解释缺口。没有明确套数时保持原最多三个合同。

每个 outfit 必须含：`outfit_id`、`strategy_label`、`items`、`reasons`、`risks`、`alternatives`、`is_primary`、`validation.all_ids_grounded`、`validation.hard_constraints_passed`。响应另含：

```json
{
  "shopping_suggestions": [],
  "requested_outfit_count": 2,
  "gap_explanation": null,
  "ui_capabilities": {"shopping_cta": false},
  "trace_id": "trace_001"
}
```

### 4.3 LookVersion

遵循 PRD 12.3。版本不可变；更新永远创建新记录，不覆盖旧版本。至少含：

- `styling_session_id`、`look_version_id`、`parent_version_id`、`version_index`；
- `status=active|final|superseded`；
- `item_ids`、`asset_ids`；
- `accepted_adjustments`、`rejected_adjustments`；
- `created_from`、`created_at`、`trace_id`。

回退也创建一个指向目标历史版本的新 active 版本，保持完整审计链。

### 4.4 Scorecard

评分对象固定为“当前 Look × 当前场景/目标”。六维枚举固定：

1. `occasion_fit`（场景适配）；
2. `expression_match`（个人表达匹配）；
3. `overall_harmony`（整体协调）；
4. `silhouette_layering`（衣物轮廓与层次，不评价身体）；
5. `comfort_practicality`（舒适与实用）；
6. `detail_finish`（细节完成度）。

至少含 PRD 12.4 字段，并增加：

- `numeric_score_available: bool`；
- `missing_evidence: string[]`；
- `visual_evidence: {region, observation, confidence}[]`；
- `prohibited_subject_checks: {appearance, body, age, sexual_attractiveness}`，全部为 `false`。

证据不足时：`numeric_score_available=false`、`total_score=null`，只返回可见部分定性建议及最少补拍/文字指引。

### 4.5 Adjustment

每轮 `priority_adjustments` 长度必须为 0–2。每项至少含：

- `adjustment_id`、`canonical_action`、`action`、`reason`；
- `expected_dimensions`、`cost_level`；
- `status=proposed|accepted|rejected|partial`。

`canonical_action` 用于会话内拒绝去重；例如“卷裤脚”“露脚踝式卷边”必须归入同一 canonical action。

### 4.6 Memory

- `MemoryProposal.status=proposed`；确认后才创建 `MemoryRecord.status=committed`。
- 必含 `memory_id/proposal_id`、`user_id`、`namespace=shared|stylist`、`type`、`content`、`source`、`sensitivity`、`ttl/expires_at`、状态与时间。
- `type` 是服务端闭集；`sensitive` 与 `session_emotion` 无条件不持久化。可提交的长期内容必须命中服务端受控安全模板；任意未知自由文本只留下 `content=null`、`commit_blocked=true` 的脱敏提议状态。
- `confirm` 与 `edit` 必须重新执行同一受控语义与敏感门控；不得把安全提议编辑成自由文本或敏感信息后提交。
- AC-07 的反馈目标由后端从已保存 outfit 推导，API 不接受客户端裸报衣物 ID；私有 target 不进入 Memory API 或 Trace。仅在相关“久走/久站”新场景应用，删除/TTL 后立即失效。
- S12 持久化仓储按 `user_id + confirmed + 未删除 + 未过期 + sensitivity ACL + namespace/member/team 可见性` 先做事务级预过滤；`shared` 只对已授权团队范围可见，`stylist` 只对 `personal_team:stylist` 可见。禁忌、敏感授权、不穿项、拒绝且不得重复、删除/过期/superseded 等硬记忆直接读取，禁止经 RRF 排名。
- 软记忆才进入 weighted RRF：`rrf_k=60`、每路候选 `20`、BM25 `1.0`、Dense `1.0`、Recency `0.75`、Importance `1.25`，先对 SQL 预过滤后的全体记忆分别取每路 Top 20，再对各路并集融合，确定性轻量重排后最多返回 Top 5。Demo 的 Dense 分支明确标记为 `deterministic_hashed_surrogate_v1`，不宣称等同生产语义 embedding 或 Cross-Encoder。响应/Trace只暴露受控 memory ID、分支名次、权重、版本与耗时，不输出正文、向量或原始敏感值。
- 删除必须同步主存储与检索索引，并保留不含内容的审计事件。
- S15 路线 A 增量字段为：`memory_class=profile_current|hard_constraint|preference_event|episodic_summary|working_context`、`valid_from/valid_to`、`supersedes_memory_id`、`source_kind`、`provenance_version`、`consent_version`、`confirmation_count`。长期记录的 `source_kind=user_confirmed|user_edited_confirmed|feedback_confirmed|legacy_migrated`，`provenance_version=memory_provenance_v1|memory_legacy_v0`，`consent_version=explicit_confirm_v1|legacy_confirm_v0`；全部由服务端闭集产生，客户端不得伪造来源、确认次数、supersedes、适用期或排序特征。`working_context` 仅属于 session+TTL，不得 commit、进入长期 `GET /memory` 或 RRF；`episodic_summary` 只允许受控、已确认摘要，不保存原始对话或模型思维链。旧记录幂等迁移为 `legacy_migrated/memory_legacy_v0/legacy_confirm_v0`，不冒充新协议证据。
- commit/supersede/delete/expire 与内容最小化 `memory_outbox` 事件必须同事务提交；outbox 只含事件 ID、user/namespace/memory ID、操作、真值版本、状态与时间，不含正文、向量、用户原话、私有衣物 ID或敏感值。派生索引可从 SQL 真值确定性重建；任何索引延迟/失败都不得恢复 SQL 已失效记录。
- S15 `structured_rerank_v1` 固定为：`final=0.60*rrf_norm+0.15*context_match+0.10*specificity+0.10*confirmation_strength+0.05*lexical_norm`。`rrf_norm=rrf/max_rrf`；`lexical_norm=max(BM25,0)/max_bm25`；`context_match∈{0,1}`，仅表示服务端受控 applicability tag 与权威 Scene tag 是否相交（推荐路径必须传 Scene；兼容层无 Scene 时只允许受控 exact lexical compat）；`specificity` 按 `comfort=1.00`、含 `occasion=0.75`、受控 `goal=0.50`、无 tag 的 `profile_current=0.25`、其余 `0`；`confirmation_strength=min(1,0.70+0.10*(confirmation_count-1))`。受控 tag 闭集为 `comfort:long_walk`、`goal:comfortable|low_key|reliable`、`occasion:interview|meeting`。排序并列依次使用 BM25、Importance、稳定 memory ID。该阶段不得调用 CPA、Cross-Encoder、LangMem、Mem0 或 Graphiti；硬记忆不进入此公式。

### 4.7 Trace

至少包含：

- request/session/look/scorecard/adjustment 关联 ID；
- data/eval/prompt/rule/ranker/provider/model 版本；
- `event_horizon`、服务端重算 `urgency`、`shopping_allowed`；
- 过滤项及原因、Rule/BM25/Dense/RRF 候选摘要；
- Catalog `attempted/call_count/blocked_reason`；
- 白名单与硬约束最终验证；
- Provider 调用、耗时、错误与 fallback；
- 不含秘密、base64 原图和完整原始对话。

## 5. API 路由合同

### POST `/dialogue/turn`

Stylist Studio 的统一对话入口。`/scene/parse`、`/recommend` 及 Look/评分端点继续保留为内部专业工具和可测合同，但前端不得把每条消息自动串成“解析后立即推荐”。

请求字段：

- `user_id`：当前 owner；
- `message`：仅当前轮新输入，不拼接历史原文；
- `styling_session_id`：首轮可空，后续连续回合必须携带；只有用户明确新建任务才省略；
- 可选 `request_id`：仅用于同一回合幂等重试，不得跨消息复用；
- 可选 `preference_question_id`：仅回答服务端当前会话已签发且仍有效的偏好问题；
- 可选 `preference_option_id`：仅引用上述问题的服务端闭集选项，浏览器不得提交画像值或排名权重。

响应最低字段：

- `turn_id`、`request_id`、`styling_session_id`、`trace_id`；
- `conversation_mode`：`stylist_chat | styling_active | support_pause | safety_response | task_closed`；
- `action`：`chat | support | clarify | recommend | acknowledge`；
- `assistant_message` 与受控 `suggested_replies`；
- `recommendation_paused`；
- `pending_question_status`：`none | active | suspended | resolved | cancelled`；
- `turn_index/history_version`，用于服务端会话连续性证明；浏览器不自行拼接完整历史；
- `provider`：`status=ok|fallback`、`generation_source=cpa|local_fallback`、`attempted`、`requested_model`、`transport_model`、`resolved_model`、`model_verified`、`degraded` 和可选受控 `reason_code`；
- 当前权威 `scene` 可空：没有穿搭任务的纯聊天不得伪造 `daily/unknown/high` Scene；仅当 `action=recommend` 时允许出现非空 `recommendation`；
- 可选 `preference_clarification`：仅由服务端在权威候选完成 HardFilter、Assembler 与 Validator 后签发；它是非阻断 advisory，同一响应仍保持 `action=recommend`、合法 `recommendation` 与 `recommendation_paused=false`；
- 可选 `memory_candidates`：服务端签发的候选记忆确认卡集合；浏览器或 CPA 都不能直接提交长期记忆。

排名、候选差异、是否追问、问题预算、问题/选项 ID 的签发与有效性均由服务端拥有。浏览器只回传服务端签发的可选 ID；CPA 只能提供不可信语言 advisory，不能决定排序、问题数量、购物门控或记忆写入。

服务端回合优先级固定为：安全风险 → 当前轮明确暂停/纠正/终止 → 明确穿搭任务与任务内情绪承接 → 纯交流意图 → 已挂起澄清 → 推荐和工具调用。轻度“紧张/怕冷场”与明确场合、穿衣问题、造型目标或任务内测量信息同轮出现时，不得仅因情绪进入 `support_pause`；Stylist 先简短承接，再继续澄清或推荐。只有用户明确要求“先暂停/先不推荐/先聊聊”才暂停。`stylist_chat`、`support_pause`、`safety_response` 与 `task_closed` 必须跳过 HardFilter 之后的推荐链路与 Catalog，返回 0 方向、0 商品、`shopping_cta=false`；已有 Scene 快照继续 owner-bound 保存，恢复时不要求用户复述。

合法状态组合固定如下，服务端与前端均须拒绝其他组合：

- `styling_active + clarify`：`pending_question_status=active`、`recommendation_paused=true`、`recommendation=null`；
- `styling_active + recommend`：`recommendation_paused=false`，且必须携带权威 Recommendation；
- `stylist_chat + chat|support`：可有 `scene=null`，`recommendation_paused=true`、0 方向/商品；
- `support_pause + chat|support|acknowledge`：`recommendation_paused=true`、0 方向/商品；
- `safety_response + support`：`recommendation_paused=true`、0 方向/商品；
- `task_closed + acknowledge`：终止 latch 不可逆；即使后续出现 safety 或 resume 指令也保持关闭。若 safety 与 close 同轮出现，回复满足安全边界，同时状态直接进入 `task_closed`。

同一 `request_id + user_id + message + styling_session_id` 的顺序或并发重试必须返回同一 receipt；不同 fingerprint/owner 冲突拒绝。R1 Demo 使用全局 async single-flight 保证 Scene、CPA、Recommendation 和 Catalog 均不重复执行；代价是不同 Dialogue 回合也暂时串行。

没有活动穿搭任务时，“先聊聊天/我们先聊天吧/可以先聊天吗/先陪我说会儿话/聊会儿天”进入 `stylist_chat + chat`，不得臆测用户紧张，不创建伪 Scene，不追问截止时间。已有穿搭任务时，“先不推荐/先别给方案/先陪我聊聊/先帮我缓解情绪”进入 `support_pause` 并保留权威 Scene；恢复只接受“继续推荐/现在开始搭/可以看穿搭”等明确指令；“不用推荐了/结束本次任务”进入关闭。支持模式中的普通场景补充（如“明天”）可以更新权威 Scene，但在没有明确恢复前不得调用推荐工具。

每个新回合按第 2 节 CPA-first 合同同步等待至多一次 CPA 调用完成。普通回合只发送脱敏、有界的当前必要文本、最多 6 条截断历史、权威 Scene 和画像 allowlist；用户主动提供的身高/体重仅可转换成当前 styling session 的 purpose-bound `fit_context`，供版型、比例、层次和舒适度建议使用，不进入长期记忆、跨 session 画像或 Trace 原值，也不得用于评价人的胖瘦/好坏或推断健康。其他敏感/安全/终止回合只发送最小分类摘要。Trace 只记录 provider/version/状态与 `previous_mode/current_mode/transition_reason_code/pending_question_status/recommendation_paused`，不得记录原始消息、测量原值、画像内容、历史内容、情绪/担忧文本或模型正文。S9 服务端 Dialogue 等待上限为 120 秒，浏览器为 125 秒；等待期间不先返回 provisional 或本地 assistant 正文。只有 CPA 正文通过完整输出 Validator 后才返回 `ok/cpa`；真实网络、120 秒超时、错误模型或输出拒绝才返回 `fallback/local_fallback`。幂等 receipt 仍不新增 CPA 调用；业务超时不打开跨回合 circuit。会话历史采用最长 30 分钟滑动 TTL，可由 `PROFAGENT_DIALOGUE_TTL_SECONDS` 收紧，不自动进入长期记忆。

S14 对 `action=recommend` 增加顺序约束：服务端必须先用权威 Scene 和当前 owner 衣橱完成 HardFilter→召回→RRF→Assembler→Validator，之后才把无内部 ID、最多三个方向的受控摘要交给文本 CPA 组织当前回合回复。摘要必须包含权威方向数量、类别/颜色/理由和“已读取当前衣橱”的事实；不得包含 garment/product ID。CPA 不能改变套数、衣物集合、购物门控或 Recommendation 结构，也不得要求用户重新列出手头衣服。与上一 assistant 回合高度复读、再次询问衣橱、或与权威方向数量冲突的输出必须被拒绝并使用同一权威 Recommendation 的本地摘要。每个新回合仍最多一次文本 CPA，推荐链和 CPA 均受同一幂等 receipt 保护。

### GET `/health`

返回服务、数据和 Provider 状态。CPA 或增强 Provider 失败时可返回 `status="degraded"`，但规则核心必须 `ready=true`。

前端仅在 Provider `enabled=true`、`available=true`、`chat_model_verified=true` 且 `resolved_model` 非空时显示 CPA 正常；关闭、不可用或模型未验证均须诚实显示相应规则降级，不得把目录广告或配置存在误报为已验证调用。

### POST `/scene/parse`

请求：`user_id`、`query_text`，可选 `request_id`、`styling_session_id`、显式场景字段。
响应：`SceneRequest`。高急切度最多一个澄清问题；仍未知时 fail-closed。澄清轮只提交本轮新增的 `query_text`（例如“明天”）与原 `styling_session_id`，服务端继承已确认的场合、目标和约束；客户端不得拼接或要求用户重复首轮原文。显式安全字段只可收紧文本解析结果，不能把 `now/today/unknown` 放宽为 planned、清除既有硬约束或凭客户端字段打开购物；跨用户 session、重复 request ID 冲突必须拒绝。

### POST `/recommend`

请求：完整 `SceneRequest` 或 `request_id`。
响应：`InitialRecommendation`。服务端始终按 `request_id` 加载已保存的权威 Scene；完整 `SceneRequest` 仅为兼容输入，安全字段不得覆盖权威状态。执行 HardFilter → Rule/BM25/(Dense) → RRF → Assembler → 整套复检 → 白名单输出验证。

`weather_requirement=rain` 时，`outer` 是必需槽位。`fixtures_v1.0` 没有结构化 `waterproof=true` 证据，因此所有普通 outer 必须在 Rule/BM25/Dense/RRF 前以 `RAIN_PROOF_EVIDENCE_MISSING` 过滤，最终返回安全 gap；不得凭“风衣”等名称猜测防雨。任意 rain 场景的 Mock Catalog 同样返回 0 商品并记录该 blocked reason，即使用户声明的可选缺口是 top/bag 等其他槽位。

### POST `/recommend/feedback`

对服务端已保存的 `request_id + outfit_id` 提交 like/dislike 与受控 `reason_code`。后端按 owner/session/request 重新读取原始 outfit，并自行推导可记忆的 grounded 目标；不接受客户端衣物 ID。若用户在方向卡只做了本地替换，前端必须先禁用该卡反馈，避免把反馈错误绑定到原始 outfit。

### POST `/recommend/previews/static-2d`（S14 实验性 R2 纵切）

请求只接受 `user_id`、`styling_session_id`、已保存 Recommendation 的 `request_id`、该 Recommendation 内的 `outfit_ids`（1–3 个）和幂等 `preview_request_id`。客户端不得提交 garment/product ID、人物照片、prompt、模型名或 render mode；服务端按 owner/session/request 重读权威 Recommendation，从 outfit 派生衣物并重新验证 owner、available、白名单、Scene 硬约束和完整槽位。未知/重复/跨 owner outfit ID 在图片 Provider 前拒绝，调用数为 0。

服务端对每个合法 outfit 最多调用一次独立 `grok-imagine-image-quality`，可并行但整批最多三次；图片提示只含受控衣物类别、颜色、材质/版型标签与 Scene 目标，不含内部 ID、用户原话、长期记忆正文或身份资产。输出固定为中性无身份模特或干净平铺的单帧静态 2D 组合参考；禁止身份复刻、真实上身/精确合身承诺、3D/360°/动画/视频。图片 Provider 必须复用 `/preview/static-2d` 的 exact request、CPA trace、重复键、大小、MIME、解码、SSRF 与来源验证合同。

响应按输入的权威 outfit 顺序返回 `previews`，每项至少含：`outfit_id`、`preview_id`、`status=succeeded|degraded|failed`、`image_url|null`、服务端派生的 `owned_garment_ids`、`provider`、`fidelity`、`ai_label`、`fallback` 与无正文 `trace_id`。`fidelity.identity=not_assessed`、`garment=style_color_reference_only`、`fit/material/drape=unknown`；AI 标签固定说明“AI 生成的 2D 视觉参考；不代表真实试穿、精确尺码、面料或垂坠”。单项失败不得取消其他项，也不得删除或替换文本 Recommendation。

`GET /recommend/previews/static-2d/{preview_id}/image?user_id=...&styling_session_id=...` 与对应 DELETE 均按 owner/session 返回或删除临时图片。跨 owner/session、未知/已删 ID 拒绝。前端必须先渲染文字与方向卡，再异步请求图片；等待或失败不得阻塞 Dialogue、启用购物 CTA 或显示生图成功来源。

### GET `/team/home?user_id=...`

返回 `personal_team`、唯一已上线成员 `stylist`、成员边界、进行中的 Styling Session 和未上线能力说明。

### GET `/wardrobe?user_id=...`

返回当前用户衣物；支持 `slot/status/season/color/occasion` 查询过滤。普通 UI 不展示内部 ID，Debug 可展示。

### PATCH `/wardrobe/{garment_id}`

仅允许编辑 PRD R1 最低属性；校验衣物属于当前用户。删除不是本次 Demo 必需，但若实现必须同步索引。

### GET `/wardrobe/catalog-assets?user_id=...`

返回当前 owner 衣橱白名单的版本化 2D 目录图状态；每项只能为 `ready/missing/failed`。`ready` 必须同时满足：源 fixture `garment_id/user_id/data_version`精确匹配，manifest 的 `asset_version=wardrobe_generated_v1_s12r2`、`requested_model=grok-imagine-image-quality`、`request_model_pinned=true`、`model_reported/model_verified=false`、`resolved_model=null` 与 `verification_basis=batch_exact_request_contract`精确成立，文件为单帧完整可解码 PNG，bytes/dimensions/SHA-256 与 manifest 一致，且 PNG 内嵌 `AI-Generated=true`、`Requested-Model=grok-imagine-image-quality`、`Model-Reported=false`、`Verification-Basis=batch_exact_request_contract`、当前 `Garment-ID` 与 `Generation-Contract=wardrobe_catalog_single_garment_v1`六项精确成立。复用资产若原生成 prompt hash 无可信留存，必须是 `prompt_sha256=null/prompt_hash_status=not_preserved`，不得用当前模板重算后冒充原值。该批次只证明生成脚本固定请求模型与文件来源链，不声称 CPA 曾逐张回报实际模型或回执。目录资产在未冻结 JPEG/WebP 内嵌来源方案前只接受 PNG；任一字段缺失、篡改、重复 ID 或文件失败均不返回 URL，不阻断原衣物元数据卡。

### GET `/wardrobe/{garment_id}/catalog-image`

必须携带 `user_id`、精确 `asset_version=wardrobe_generated_v1_s12r2` 与当前 item 的 lowercase 64-hex `content_sha256`；服务端重做 owner/fixture/manifest/文件全部校验，并要求 query hash 与重验后的 manifest/文件 hash 精确相同，才返回 `image/png`。旧版本、旧二参数 URL、错 hash、未知或跨 owner、篡改或缺图统一拒绝；成功 URL 可按内容寻址缓存。响应附加 `X-AI-Generated: true`、`X-AI-Requested-Model: grok-imagine-image-quality`、`X-AI-Model-Reported: false` 与 `nosniff`，不得附加或暗示未经 CPA 回报的 actual/resolved model；HTTP header/sidecar 不取代文件内嵌元数据。

### POST `/assets`

以 multipart 上传 `user_id`、`styling_session_id`、`angle`、`consent=true`、`purpose=styling_assessment` 与 `file`。只接受不超过 5 MB、单帧、完整可解码、最大边不超过 8192 px 且总像素不超过 25 MP 的 PNG/JPEG/WebP 静态当前穿搭图片；APNG、动画 WebP、视频与 3D 资产必须拒绝。返回 `asset_id`、类型、角度、`storage=memory_ephemeral`、保留策略和视觉质量状态。原始字节只保存在当前进程内存，日志/Trace 不得包含原图或 base64。

### DELETE `/assets/{asset_id}?user_id=...&styling_session_id=...`

按 owner 与会话双重绑定删除进程内图片；跨用户、跨会话和已删除 ID 统一不可读取。服务退出时清空尚存资产。

### POST `/look`

从 `selected_outfit`、`user_revision` 或 `rollback` 创建不可变 LookVersion。图片上传本身不直接创建 Look；前端须先上传 owner-bound Asset，再以 `user_revision` 把完整 `asset_ids` 集合绑定到新版本。所有 `item_ids` 必须属于当前用户白名单；无法对应的视觉单品不得虚构 ID。

- `selected_outfit`：传 `user_id/styling_session_id/request_id/outfit_id/asset_ids`，服务端从已保存推荐中读取单品，客户端不得自报 outfit 内容；
- `user_revision`：传 `parent_version_id/item_ids/asset_ids`，重新执行 owner、available、硬/半硬与槽位复检；
- `rollback`：传当前 `parent_version_id` 与 `rollback_target_version_id`，复制目标快照并创建新版本，绝不覆盖旧版本。

### GET `/look?user_id=...&styling_session_id=...`

返回版本链、active/final 版本和父子关系；可用 `look_version_id` 精确读取。

### POST `/scorecard`

请求字段为 `user_id/styling_session_id/look_version_id/asset_ids?`；场景从服务端 session 读取，不能由客户端放宽。`asset_ids` 若传入，必须与不可变 LookVersion 绑定的资产集合完全一致。Vision Provider 只可返回 `top|bottom|shoes|overall` 的 `*_visible|*_not_visible` 受控 observation code、可见性、置信度和闭集 issue code；slot/visible 不一致、自由文本、额外字段、模型回显不精确、区域不全、低置信或超过 10 秒服务端预算均定性降级。用户可见中文证据只由服务端固定映射生成。响应为六维 `Scorecard`；硬约束失败优先；证据不足时不输出精确总分或调整。

### POST `/adjust`

请求包含 `look_version_id`、`decision=accept|reject|partial|user_modified`、调整 ID、原因及可选新 item/asset。
响应包含决策记录、可选新 LookVersion、与父版本比较和下一轮最多两项建议。拒绝但无有效修改时不得伪造新版本。单次冻结字段为 `user_id/styling_session_id/look_version_id/adjustment_id?/decision/reason?/item_ids?/asset_ids?`；任何响应的 `priority_adjustments` 长度不得超过 2。

所有 accept/reject/partial/user_modified 决策均写入独立、owner-bound 的 `AdjustmentDecisionRecord` 并在 Look chain 中可回放；拒绝的 canonical action 在会话内不得换说法重提。

### POST `/finalize`

请求：`styling_session_id`、`look_version_id`、明确满意意图、1–5 满意度和可选原因。
响应：不可变 Final Look/FinalizationRecord。冻结字段为 `user_id/styling_session_id/look_version_id/satisfied=true/satisfaction/reason?`。任何系统分数下都允许定稿；完成后不得继续施压。

### POST `/memory/propose`

创建提议，不可立即用于长期个性化。公共类型闭集为 `constraint|comfort_constraint|preference|profile_stable|feedback|session_emotion|sensitive`；UI 只发送服务端公布的安全模板。

### POST `/memory/{proposal_id}/confirm`

确认/编辑/拒绝；只有确认且通过敏感度门控才 commit。

### POST `/memory/candidates/extract`

S16A 自由文本候选提取入口；当前合同已冻结但尚未实现。请求只能包含：

- `user_id`；
- 可选 `styling_session_id`；
- `namespace`；
- `text`；
- `request_id`。

不接受客户端提交 canonical 类型、值、适用标签、敏感度、来源、确认次数、排序特征或作用域。响应使用 `memory_candidates` 返回服务端签发的候选卡；候选卡仅含服务端生成的脱敏闭集字段，不得回显原始自由文本、原始对话、模型推理/正文、敏感值或直接标识符，也不得将这些内容写入长期 Memory、RRF、outbox、Trace、Debug DOM 或 browser storage。每项 `candidate_id` 只能用于下面的决定端点。自由文本不能直接 commit，长期保存仍须执行受控 `propose→confirm→commit`。

### POST `/memory/candidates/{candidate_id}/decide`

S16A 单候选决定入口；当前合同已冻结但尚未实现。请求只能包含：

- `user_id`；
- 可选 `styling_session_id`；
- `decision=remember|session_only|reject|rephrase`；
- `idempotency_key`。

`remember` 仍受敏感门控并进入 S15 `propose→confirm→commit`；`session_only` 只写入 session-bound TTL working context，不进入长期 `GET /memory`、outbox 或 RRF；`reject` 在本会话不得换说法重复；`rephrase` 只回到重新提取流程，不允许直接编辑数据库字段。candidate、owner、namespace、session 和幂等 receipt 的绑定全部由服务端复验。

### GET `/memory?user_id=...&namespace=...`

返回待确认提议和当前有效记录，清楚区分 shared 与 stylist namespace。`records` 仅允许 `status=committed` 且 `lifecycle_status=active`；expired、superseded、deleted 不返回。`working_context` 不属于长期记录，也不得经该端点返回。

### DELETE `/memory/{memory_id}`

删除已提交记录并同步索引；响应不回显敏感内容。权威回执固定为 `api_version + deleted_id + deleted_kind + user_id + namespace + truth_version + trace_id`；`truth_version` 是删除后的真值版本，proposal-only 删除固定为 `0`。已 committed 的 proposal ID 不得伪装成 proposal-only 删除并级联删除 record；必须按 record 删除语义返回其权威版本。

### GET `/trace/{trace_id}`

必须带 `user_id`，仅 owner 可读取安全清洗后的 Trace；支持 Debug 页。

### GET `/trace?user_id=...&styling_session_id=...`

返回当前会话关联 Trace 列表。

### POST `/eval/run`

串行运行 `data/eval/eval.jsonl` 固定评测，返回指标、版本和 JSON/Markdown 报告路径。不得修改真值或降低阈值。

### POST `/preview/static-2d`（S6 实验性 R2 纵切）

请求只接受：`user_id`、`styling_session_id`、owner-bound 不可变 `look_version_id`、固定 `render_mode="static_2d"`、可选且仅用于 owner/consent 校验的 `identity_asset_id`、`consent` 与幂等 `request_id`。客户端不提交 garment/product ID；服务端从 LookVersion 派生并重新验证 owner、available 与白名单。当前前端固定提交 `identity_asset_id=null`；即使 API 调用方提供了合法资产，原图/字节/URL/视觉特征也绝不发送给生图 Provider，身份一致性始终为 `not_assessed`。`3d/360/video` 或未知 render mode 在 Provider 调用前返回 422，调用数必须为 0。

Provider 通过 CPA `/images/generations` 固定请求独立图片模型 `grok-imagine-image-quality`，并支持受控 URL 或 base64 Provider envelope。所有成功都必须验证受控单值 `x-cpa-trace-id` 回执；响应若包含 `model` 字段（包括 `null`、空串或非字符串），只有字符串精确等于请求模型才可继续；字段完全缺失时仍把 actual/resolved model 记为未回报。JSON envelope 使用递归 duplicate-key 拒绝解析，任何层级重复 `model/data/b64_json/url` 或其他键均整体 `CPA_IMAGE_OUTPUT_REJECTED`，不得依赖 last-wins。两条路径都只接受唯一单帧 PNG/JPEG/WebP。CPA envelope 本身也必须流式读取并在 8 MiB 立即中止。远程 URL 必须为 HTTPS、无 userinfo，且 host 必须存在于运维显式 allowlist；禁止 loopback、private、link-local、multicast、unspecified、reserved IPv4/IPv6 及解析到这些地址的主机；默认禁止 redirect；URL 图片响应采用流式读取并在 5 MiB 立即中止，同时精确校验 Content-Type、文件 magic 与完整 decode。解码后继续执行 8192 px 最大边、25 MP 与动画拒绝。应用响应不得内嵌 base64 或原始 CPA trace，只返回 owner-bound 的临时 `preview_id/image_url`：

```json
{
  "api_version": "r1_demo_v1",
  "preview_id": "preview_001",
  "request_id": "previewreq_001",
  "user_id": "u01",
  "styling_session_id": "session_001",
  "look_version_id": "look_v2",
  "render_mode": "static_2d",
  "owned_garment_ids": ["g001", "g004", "g009"],
  "external_item_ids": [],
  "scene_id": "req_001",
  "identity_asset_id": null,
  "asset_id": "asset_001",
  "status": "succeeded|degraded|failed",
  "image_url": "/preview/static-2d/preview_001/image?user_id=u01&styling_session_id=session_001",
  "provider": {
    "status": "ok",
    "requested_model": "grok-imagine-image-quality",
    "transport_model": "grok-imagine-image-quality",
    "resolved_model": null,
    "request_model_pinned": true,
    "cpa_trace_verified": true,
    "model_reported": false,
    "model_verified": false,
    "verification_basis": "exact_request_with_cpa_trace",
    "degraded": false
  },
  "fidelity": {
    "identity": "not_assessed",
    "garment": "style_color_reference_only",
    "fit": "unknown",
    "material": "unknown",
    "drape": "unknown"
  },
  "ai_label": {
    "generated": true,
    "display_label": "AI生成的2D视觉参考",
    "metadata_status": "http_header_and_response",
    "disclaimer": "仅供风格与配色参考，不代表精确尺码、面料或垂坠"
  },
  "fallback": null,
  "trace_id": "trace_001"
}
```

`GET /preview/static-2d/{preview_id}/image?user_id=...&styling_session_id=...` 按 owner/session 返回正确 MIME 的临时图片；`DELETE /preview/static-2d/{preview_id}?user_id=...&styling_session_id=...` 删除生成资产。跨 owner/session、未知或已删除 ID 统一不可读取。Trace 仅记录 Look/garment IDs、provider/model/status、耗时、错误码与资产元数据，绝不记录提示词、API key、base64 或原始人像。

CPA 图片端点不可用、超时、错误模型、坏响应、下载或解码失败时返回 `status=degraded|failed` 和衣物卡片/平铺组合/文字解释 fallback，不阻塞 R1 Styling Session，不伪装生图成功，不切换其他图片模型。2026-08-10 使用文本 transport `grok-4.5-high` 的失败只保留为历史证据；S12 起不得再用该请求证明图片 Provider 状态，必须以独立图片模型真实调用为准。

## 6. 前端防御合同

- UI 既读取服务端 `shopping_allowed/ui_capabilities`，也以 `urgency=high` 做本地二次防御；高急切度 DOM 中不得存在隐藏购物按钮或链接。
- 用户选定方向后只聚焦一个 Active Look；不得每轮重新发散三套。
- 每个方向展示首选、理由、风险、替换入口和“全部来自现有衣橱/含可选补购”。
- Scorecard 文案必须使用“这套穿搭在当前目标下”，不得把分数指向人。
- 每轮调整最多两项；满意定稿始终可用，不受总分限制。
- 版本轨迹展示 v1→vN→Final、父版本、变化与回退。
- S6 只增加有明确 AI 标识的实验性 static_2d 入口；全站无 3D/360°/视频入口。
- `index.html` 必须对当前 UI build 禁止缓存或引用带 build version 的 `styles.css/fixtures.js/app.js`；`/health` 与 Debug 显示同一 build version，避免旧 bundle 继续调用 `/scene/parse`。

## 7. 固定验收门槛

- UrgencyAcc ≥ 95%；高急切度 ShoppingGateAcc=100%，Catalog call count=0。
- Item Hallucination=0；Hard Constraint Violation=0；Slot Completeness≥95%。
- Look 版本链完整率=100%；评分对象误指向颜值/身体=0；Sensitive Write False Positive=0。
- 每轮调整数≤2；拒绝建议本会话不重复；LLM/Dense/Catalog/Vision 故障时核心流程可用。
- AC-01/03/04/05/06/07/10/11/13/14/16/17 可演示或可测；补充 AC-09/12/15/18 覆盖 PRD 23.1。
