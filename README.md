# ProfAgent R1 Stylist Demo

ProfAgent 的首位专业成员是私人穿搭师（Stylist）。本仓库现提供一个本地可运行的 R1 Demo：它使用合成衣橱与 Mock 商品完成中文场景解析、衣橱内推荐、穿搭共创、六维评分、调整、Look 版本、定稿、受控记忆和可追溯评测。

这是产品 Demo，不是生产服务。业务数据仍以仓库合成 fixture 为基线；S12 已将受控 Memory 改为可跨进程恢复的 SQL 仓储（本地默认 SQLite，生产配置可用 PostgreSQL），并为 50 件合成衣物加入可追溯 AI 目录图。S14 增加了连续场景内的明确套数推荐、推荐级两张静态 2D 参考和按类别折叠的衣橱浏览；S15 路线 A 增加事务 outbox、可重建软索引和确定性结构化重排。当前 Web 仍固定演示合成用户 `u01`，没有生产级身份认证、持久 Trace/Scene/Look 主库、完整衣橱编辑工作台、真实商品、穿后结果回流、支付或真人服务。

## 快速启动

所有命令统一使用 Conda 环境 `torch128`。首次运行如需安装项目依赖：

```powershell
conda run -n torch128 python -m pip install -e ".[dev]"
```

需要将 Memory 连接到 PostgreSQL 时再安装可选驱动；本地 Demo 不需要：

```powershell
conda run -n torch128 python -m pip install -e ".[dev,postgres]"
$env:PROFAGENT_DATABASE_URL = "postgresql://<user>:<password>@<host>:5432/<database>"
```

一条命令启动 API 与 Web Demo：

```powershell
conda run -n torch128 python -m profagent
```

打开 `http://127.0.0.1:8000`。FastAPI 与中文 Web 界面由同一进程提供，按 `Ctrl+C` 停止。

一条命令运行固定评测并生成 JSON 与 Markdown 报告：

```powershell
conda run -n torch128 python -m profagent.eval
```

报告写入：

- `reports/eval/r1_demo_v1.json`
- `reports/eval/r1_demo_v1.md`

数据校验与全量测试：

```powershell
conda run -n torch128 python scripts/validate.py
conda run -n torch128 python -m pytest -q
```

## 本地验收结果（2026-08-19）

R1 DoD Demo 子集及 S6–S15 增量已在 `torch128` 串行验收通过：pytest `300 passed`，S15 专项 `19 passed`、Memory 相关 `44 passed`；Urgency `30/30`、高急切度 Shopping Gate `10/10`、高急切度 Catalog 调用 `0/10`、衣物/商品 ID 幻觉 `0/515`、硬约束违反 `0/423`、槽位完整 `74/74`，固定评测 `overall=PASS`。S15 的随机非 8000 HTTP、双实例/重启 SQLite、ACL/生命周期/删除回执、真实 Chrome 记忆页与 14 项 Node 语法检查 + 7 套 runtime/static 均通过。S14 截图链仍能在同一 session 返回两套 owner 衣橱组合且 Catalog 为 0；真实 CPA 推荐两图并行总墙钟约 `7,126.3ms`，两次尝试约 `7,059.1ms/6,845.4ms`，均返回可解码 JPEG。CPA 未回报实际图片模型，因此仍保持 `model_verified=false/resolved_model=null`。文本合同仍为 `grok4.6 → grok-4.6-high → {grok-4.6-high,grok-4.6-build}`，服务端/浏览器等待上限仍为 120/125 秒。

可复查 [固定评测报告](reports/eval/r1_demo_v1.md)、[S15 Memory 路线 A 验收](reports/eval/s15_memory_route_a_v1.md)、[S14 连续推荐与 2D 验收](reports/eval/s14_continuity_preview_v1.md)、[S14 真实 CPA 推荐两图](reports/demo/s14_real_recommendation_previews.md)、[S14 Memory 方案调研](reports/research/s14_memory_options.md)、[S13 文本 4.6 与目录图验收](reports/eval/s13_text46_catalog_display_v1.md)、[Grok 4.6 外部审查](reports/review/s13_grok46_external_review.md)与 [Codex 相互验证](reports/review/s13_codex_mutual_verification.md)。S15 reviewer 对跨 owner payload、粘性隔离、PostgreSQL 并发、outbox consumer 和 mutation 回执的发现均已修复，最终 reviewer/tester 为 `[P0/P1/P2]=0/0/0`。上述结论只适用于合成数据与 Demo 边界，不代表生产就绪。

## Demo 能力

- Team Home：只展示已上线的 Stylist，并说明成员能力边界。
- Stylist Studio：Enter 发送、Shift+Enter 换行，中文输入法组合态不会误提交；每个未命中幂等回执的新回合最多尝试一次 CPA 对话调用。推荐回合先由服务端从当前 owner 衣橱完成过滤、检索、拼套和验证，再把不含内部 ID 的权威方向摘要交给模型组织当前增量回复；本地编排层仍决定动作、套数、工具权限、购物门控、ID、记忆和安全边界。
- 聊天与情绪承接：没有穿搭任务时，普通聊天进入 `stylist_chat + chat`，不伪造场景、不追问截止时间；“紧张、不安、怕冷场”等轻度情绪与明确场合、穿衣问题或造型目标同时出现时，Stylist 会先用一句话自然承接，再继续澄清或推荐。普通闲聊后的新穿搭任务可直接开始；只有用户明确说“先不推荐/先聊聊”才锁存暂停，并在明确说“现在开始搭配”后恢复。系统可提供非医疗的情绪支持，但不诊断、不治疗，也不评价用户本人。
- 最少澄清：时间未知时最多追问一次；输入发送后会立即清空。下一轮只需回答新增信息或纠正（例如“明天”“不是今天，是明天”），场合、目标与约束继续继承，不需要重复第一句话。
- Grounded 推荐：召回前硬过滤，Rule + BM25 + RRF 排序，可选 Dense 降级；最多三个合法方向，或说明安全缺口。当前回合明确要求一至三套时由服务端解析，合法候选足够则返回恰好该数量，否定/纠正可覆盖早先数字，歧义时不猜也不复制凑数。
- 衣橱与 Mock Catalog：衣物 ID 来自当前 owner 衣橱白名单，商品 ID 来自本次 Catalog 返回白名单；Catalog 只在非高急切度且同时满足服务端时间、明确购物意图和合法缺口门控时作为可选建议。
- 共创闭环：上传并分析一张静态当前穿搭图、六维情境化评分、每轮最多两项调整、接受/拒绝/部分接受、复评与满意定稿。
- 衣橱工作台与实验性 Static2D：当前 owner 衣物按类别折叠，初始只显示类别和数量，展开后才加载目录卡与图片；50 件 fixture 均已有生成资产，但三位合成用户仍严格 owner 隔离。图片链路与文本完全分离，只通过 CPA `/images/generations` 固定请求 `grok-imagine-image-quality`。除不可变 Look 预览外，S14 还会在文字推荐卡出现后，为每个已验证方向异步生成“中性无身份模特或平铺”的静态 2D 组合参考；单图失败不删除文字推荐。图片必须通过请求/回执来源、owner/ID、静态解码、哈希与来源校验才展示。实际模型未回报时诚实保持未验证；任何身份参考图都不发给生图 Provider，身份一致性固定为 `not_assessed`，不宣称真人试穿或精确尺码、面料、垂坠。
- Look 轨迹：v1→vN 不可变，可比较；回退通过创建新版本完成，不覆盖历史。
- 记忆治理：仅支持受控类型和模板，必须 `propose → confirm → commit`；敏感内容默认不写，用户可查看和删除。SQL 真值与内容最小化 outbox 同事务更新，软投影可从真值重建且每次召回仍复验 owner/ACL/生命周期；已确认的硬记忆按 SQL 精确读取，绝不进 RRF。软偏好在同一预过滤后按 BM25、deterministic hashed Dense surrogate、Recency、Importance 四路 Top 20 以 weighted RRF 融合（`k=60`，权重 `1.0/1.0/0.75/1.25`），再按 `structured_rerank_v1` 的 `0.60 RRF + 0.15 context + 0.10 specificity + 0.10 confirmation + 0.05 lexical` 确定性重排，最多注入 5 条受控信号。本地 SQLite 会保留记忆跨进程重启，生产可选 PostgreSQL；LangMem、Mem0、Graphiti、真实语义 embedding 与 Cross-Encoder 均未上线。
- Debug / 评测：查看当前进程的 Trace、版本关联和 Provider 降级状态；评测区展示固定 CLI 命令与报告位置，不在网页内直接启动评测。

评分只评价“当前穿搭 × 当前目标”，不会评价颜值、身材、体重、年龄、性吸引力或人的价值。图片证据不足、Vision 失败或返回非受控证据时只给定性反馈，不显示伪精确总分。

## CPA Grok 4.6

Stylist 的自然语言 Provider 通过 OpenAI-compatible CPA 反代调用用户固定的逻辑模型 `grok4.6`。当前应用固定 transport ID `grok-4.6-high`，完整调用只接受显式精确 allowlist `{grok-4.6-high, grok-4.6-build}`；旧 4.5、前缀变体和其他模型均 fail-closed，不会模糊匹配或自动选模。经用户明确同意上传的当前穿搭图片若启用 Vision，也只接受受控证据。

Static2D 不复用上述文本 transport；它有独立配置、健康状态与输出 Validator，请求模型固定为 `grok-imagine-image-quality`。若 CPA 回报 `model`，必须精确匹配；若不回报，只在 exact request、受控 `x-cpa-trace-id` 与完整图片 Validator 同时成立时接受，并保持 `resolved_model=null/model_verified=false`。CPA JSON envelope 以 8 MiB 流式上限读取；若返回图片 URL，则还需显式 host allowlist、公网 DNS、HTTPS/443、无重定向，并以 5 MiB 流式上限校验 Content-Type/magic/完整解码。任一验证失败都只回退已验证的衣物卡/文字，不会切换模型或伪称成功。

运行配置可由以下环境变量提供：

```text
PROFAGENT_CPA_BASE_URL=<OpenAI-compatible /v1 endpoint>
PROFAGENT_CPA_API_KEY=<runtime secret>
PROFAGENT_GROK_MODEL=grok4.6
PROFAGENT_CPA_TEXT_ENABLED=true
PROFAGENT_CPA_DIALOGUE_BUDGET_SECONDS=120
PROFAGENT_DIALOGUE_TTL_SECONDS=1800
PROFAGENT_CPA_IMAGE_ENABLED=true
PROFAGENT_CPA_IMAGE_MODEL=grok-imagine-image-quality
PROFAGENT_CPA_IMAGE_TIMEOUT_SECONDS=45
# 仅在 CPA 返回远程 URL 而非 b64_json 时配置受信 CDN host，逗号分隔
PROFAGENT_CPA_IMAGE_DOWNLOAD_HOSTS=<trusted-cdn-hosts>
# 未设置时默认使用 .profagent/memory.sqlite3
PROFAGENT_DATABASE_URL=postgresql://<user>:<password>@<host>:5432/<database>
```

也可通过 `PROFAGENT_CPA_CONFIG` 指向本地 JSON；未指定时会尝试读取 `~/.codex/skills/call-grok/config.local.json` 的 `base_url` 与 `api_key`。密钥不会写入仓库、API 响应或 Trace。每个新回合最多尝试一次 CPA，幂等重试不会重复调用；模型直接返回用户可见正文和受控建议。本地只发送固定 Stylist 人格、当前必要且白名单化的画像、最多 6 条逐条截断的会话历史、权威 Scene/动作以及脱敏后的当前输入。用户主动提供的身高、体重可在明确穿搭任务中转换为当前 session 的 `fit_context`，只用于服装版型、比例、层次与舒适度；原句不进入历史或 Trace，数据不进入长期记忆或跨 session 画像，模型也不得复述原值或据此评价人。姓名、用户 ID、画像自由文本、原始记忆、未确认记忆和其他敏感医疗/身体内容不发送；敏感或直接标识符输入改为最小分类摘要。该门控不是完整 DLP。

对话 session 默认采用 30 分钟滑动 TTL，`PROFAGENT_DIALOGUE_TTL_SECONDS` 只能收紧；过期后 Scene 快照与回合 receipt 会惰性清理。Dialogue/Scene/Look/Trace 状态仍只存在当前进程，重启即清空；只有已确认的受控 Memory 通过 SQL 仓储跨进程保留。R1 Demo 为保证并发同 request 不重复调用 CPA 或专业工具，保守地串行执行所有 Dialogue 回合；这不是生产吞吐架构。

CPA 不可用、120 秒内未完成、模型回显不在精确 allowlist 或输出越界时，界面如实标记“本地回复”，核心规则推荐仍可运行；只有真正的 `safety_response` 标记为“安全回应”，不会把普通 Provider/格式故障暗示成用户违规。Health/Scene/Dialogue/Vision 的服务端硬预算分别为 `1.5s / 8s / 120s / 10s` 且只能通过环境变量收紧，浏览器 Dialogue 预算为 125 秒。等待期间不轮询、不插入临时 assistant 气泡；正式回复到达即结束计时，Trace 独立加载。Dialogue 业务预算超时只影响当前回合，不开启跨回合熔断；场景增强失败回退规则解析，Vision 降为定性评分，Catalog 故障时不返回购物建议，Static2D 失败时保留衣物卡片/平铺组合/文字解释。要进行完全离线的确定性演示，可在当前 PowerShell 会话先执行：

```powershell
$env:PROFAGENT_CPA_TEXT_ENABLED = "false"
conda run -n torch128 python -m profagent
```

## 关键安全规则

- `now/today/unknown → urgency=high → shopping_allowed=false`。
- 高急切度由 Orchestrator 与 Catalog 服务双层阻断，Catalog 实际调用数为 0，界面不显示购物 CTA。
- 禁忌色、非 `available`、季节/天气必需项在召回前过滤，后续排序、替换和降级不得恢复。
- 衣物和商品 ID 必须属于当前用户/本次工具白名单；最终 Validator 再次拒绝非法或跨用户 ID。
- 明确拒绝的调整在同一会话中不会换说法重复；每轮最多两项调整。
- 图片只接受显式同意与用途说明的单帧 PNG/JPEG/WebP；限制 5 MB、8192 像素边长和 25 MP，完整解码后才进入进程内临时存储，并支持删除。
- LLM 或 Dense 失败时规则推荐继续可用；Catalog 失败时不返回商品；Vision 失败时仅给定性穿搭反馈。

## 主要端点

`/health`、`/dialogue/turn`、`/scene/parse`、`/recommend`、`/recommend/feedback`、`/recommend/previews/static-2d`、`/team/home`、`/wardrobe`、`/wardrobe/catalog-assets`、`/wardrobe/{garment_id}/catalog-image`、`/assets`、`/look`、`/scorecard`、`/adjust`、`/finalize`、`/memory`、`/trace`、`/eval/run`、`/preview/static-2d` 及对应图片读取/删除端点。

字段、错误语义和安全合同见 [API_CONTRACT.md](docs/API_CONTRACT.md)，验收映射见 [AC_MATRIX.md](docs/AC_MATRIX.md)，完整需求见 [PRD.md](docs/PRD.md)。

## 数据基线

- 3 个虚拟用户
- 50 件合成衣物
- 20 套人工/合成穿搭（含硬约束负例）
- 50 条中文电商风格 Mock 商品
- 30 条固定评测样本
- 50 张以固定请求模型 `grok-imagine-image-quality` 生成、绑定 fixture ID 且带内嵌 AI 来源元数据的 1:1 PNG 目录图；批次来源明确记录 actual model 未由 CPA 回报
- 固定生成种子 `20260729`

数据位于 `data/fixtures/`、`data/schemas/`、`data/eval/eval.jsonl`、`data/assets/wardrobe_generated_v1/` 与 `data/manifests/wardrobe_generated_v1.json`。它们全部为合成或人工设计数据，不含真实用户信息，也不抓取或复制真实购物平台的商品、品牌、商家、图片、评论或页面内容。

## 明确未上线

以下属于 R2+ 路线或范围红线，当前 API 与 UI 均不提供可用入口：

- 完整 R2 购前决策预览（外部候选商品导入、真人身份保持试穿、多场景购前对比与到货反馈）；
- 第二专业成员和实际团队转交；
- 真人造型师、语义模板市场、社区内容流；
- 真实商品抓取、真实购物、支付与开放真人市场；
- 3D、360°、生成视频与实时视频展示。

当前已实现的只是实验性 Static2D 纵切和合成衣橱目录图：对既有 Look 只生成中性无身份模特或平铺参考，不发送身份图片、不评估身份一致性，不是真人上身效果保证，也不属于 R1 DoD。虚拟人物资产初始化、稳定身份保持上身图、多场景试演和到货闭环仍未上线；3D/360°/视频继续是硬红线。
