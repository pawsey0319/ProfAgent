# ProfAgent R1 Demo BUILD LOG

> 唯一需求权威：`docs/PRD.md`（v1.17）。
> 当前状态：**R1 DoD Demo 子集及 S6–S15 实现与验收已关闭；S16A 自由文本记忆、偏好不确定性及 Task8 finalization 已通过 tester 全门禁与 fresh broad review，现已关闭。旧 JSON/MD projection 未经 canonical bundle 与双 hash 校验仍不构成 authoritative PASS。S16B、S16C、S16R 均未完成、未上线；LangMem、Mem0、Graphiti 与学习型 reranker 移至 S17+ 且未上线。**
> 目标：交付可运行的 R1 Stylist MVP Demo，并通过 PRD 23.1 的 Demo 子集验收。

## 0. 基线与执行约束

### 已核对基线（2026-08-04）

- `fixtures_v1.0`：3 用户、50 衣物、20 套搭、50 Mock 商品、30 条固定评测。
- 复用 `data/fixtures/*`、`data/schemas/*`、`data/eval/eval.jsonl`、`scripts/generate.py`（`seed=20260729`）、`scripts/validate.py` 和现有 `tests/`。
- `conda run -n torch128 python scripts/validate.py`：通过。
- `conda run -n torch128 python -m pytest -q`：`5 passed`。
- 开发、启动、测试、评测、Provider 辅助脚本和最终审查脚本统一在 Conda 环境 `torch128` 中运行；禁止改用系统 Python 或其他 Conda 环境。
- 本轮启动时仓库只有数据/规则基线，没有 FastAPI、完整会话状态机、视觉评分、Look 迭代或产品界面；这是接手前基线记录，不代表当前 R1 候选状态。
- Windows 上多个 `conda run` 并发会竞争 Conda 临时文件；项目命令与验收命令串行执行，agent 可并行编辑互不冲突的目录。
- 工作树在本轮开始前已有用户改动（`README.md`、`.codex/`、`AGENTS.md`、`docs/`）；所有 agent 必须保留并避开无关修改。

### 固定硬规则（违反即 P0）

1. 衣物/商品 ID 只能来自当前用户工具白名单；非法 ID 必须在服务端输出验证层拒绝，Item Hallucination=0（AC-10）。
2. `now/today/unknown -> high`；高急切度 `shopping_allowed=false`；Orchestrator 与 Catalog 双层阻断，Catalog 调用数必须为 0（AC-01/03/11）。
3. 禁忌色、非 `available`、季节与天气必需项必须在召回前过滤；排序、替换和降级路径均不得恢复（AC-04/05）。
4. Scorecard 仅评价“当前穿搭 × 当前目标”；禁止颜值、身材、年龄、性吸引力或人的价值维度（AC-16/SAFE-08）。
5. 每轮最多 2 项调整；用户拒绝的建议本会话内不得通过同义改写重复（AC-14/ITER-03/06）。
6. Look 版本不可变，v1→vN/Final 可追溯、比较、回退；用户满意可在任意分数立即定稿（AC-13/17）。
7. 记忆必须 `propose -> confirm -> commit`；敏感信息默认不写；支持查看与删除（MEM-01/SAFE-04）。
8. LLM、Dense、Catalog、Vision 失败时核心规则推荐仍可用；视觉证据不足时不得输出伪精确分数（OBS-05/AC-15）。

### 范围红线与 R2+ 记录

以下能力不进入本次 R1 DoD，不得在 UI/API/README/PRD 0.3 中暗示已上线：

- 3D、360°、生成视频、实时视频展示（DEC-01/03、EVO-01）；
- 真实支付、绕过风控的真实爬取；
- 开放真人造型师市场、第二专业成员、社区内容流；
- 真实商品交易、真人专家、语义模板市场。

### CPA 与 Grok 运行合同

- 用户已指定项目中的 Stylist Agent 通过 CPA 反代调用逻辑模型名 `grok4.6`。S13 固定 `requested_model=grok4.6 -> transport_model=grok-4.6-high`，并只接受精确回报 `{grok-4.6-high, grok-4.6-build}`。旧 4.5、前缀变体与其他模型均 fail-closed；禁止任意前缀/包含匹配或自动选模。自然语言回复使用 `GrokLLMProvider` 直接生成用户可见正文，本地保留权威状态机、硬门控、输出 Validator 与明确降级。
- CPA base URL、API key 与 `grok4.6` 模型选择通过运行配置注入，不提交密钥；请求和 Trace 只记录受控逻辑模型 ID、CPA 精确解析的模型 ID、provider/version、耗时与错误码。
- 应用健康检查必须验证 CPA 连通性和 `grok4.6` 的精确 transport 可路由性；不得在 CPA 不可用时静默换成另一个远程模型。失败时按 OBS-05 回退核心规则路径。
- 项目运行时文本使用 `grok4.6`；静态 2D 使用独立图片模型 `grok-imagine-image-quality`，二者配置、验证与 Health 完全隔离。独立 `$call-grok` 审查仍遵循仓库 AGENTS.md，输出仅作建议证据。

CPA Grok 生图限定为独立可选 `static_2d` Provider Adapter：

- 仅静态 2D，绝不接受 3D/360°/video 枚举；
- 默认不作为 R1 核心链路或完成门槛，失败回退到已有衣物卡片/平铺组合/文字解释；
- 本 Demo 已启用实验性 R2 Static2D 纵切，但完整 R2 购前试演仍未上线；响应记录 provider/model/version，并显示“不代表精确尺码、面料或垂坠”；
- 高急切度仍不得出现购物 CTA；
- 生图请求使用独立图片模型 `grok-imagine-image-quality` 和 CPA `/images/generations`；不得把文本 transport `grok-4.6-high` 发送到图片端点，也不得静默自动切换图片模型。图片响应有 `model` 字段时必须精确匹配；真实协议未回报时只接受 exact request + 受控 CPA trace 回执 + 全图 Validator，并诚实标记 actual model 未回报。MIME、静态单帧、大小与 owner/Look/garment 白名单合同仍须全部成立；失败诚实降级。

## 1. 阶段与委托计划

> 历史模型记录说明：以下 S1–S12/S12R 中出现的 Grok 4.5 名称仅用于还原当时的实现与验收，不是当前运行配置。自 S13 起，当前文本合同唯一为 `grok4.6 → grok-4.6-high`，完整调用只接受 `{grok-4.6-high, grok-4.6-build}`；历史 4.5、无后缀 4.6、前缀和其他模型均不得用于当前运行。

### S0 — 合同冻结与工作区划分（压缩 W1）

- **目标**：在并行开发前冻结 PRD 12 节的最小数据合同、API 方法/错误语义、文件所有权、启动/评测命令和 AC 测试矩阵。
- **负责人**：`supervisor`；确认后分别向 `backend`、`frontend` 下发同一合同，暂不并行修改共享合同文件。
- **覆盖**：TEAM-01/03/04、OBS-01/03；为 AC-01/03/04/05/06/07/10/11/13/14/16/17 建立双向追踪表；同时纳入 AC-09/12/15/18 以覆盖 PRD 23.1 的数据边界、角色连续性、视觉降级和团队边界。
- **合同最低项**：`SceneRequest`、`InitialRecommendation`、`LookVersion`、`Scorecard`、`Adjustment`、`MemoryProposal/Record`、`Trace`、错误/降级结构；端点覆盖 `/scene/parse`、`/recommend`、`/look`、`/scorecard`、`/adjust`、`/finalize`、`/memory`、`/trace`、`/health`、`/eval/run`。
- **默认架构提案**：FastAPI 单进程托管 API 与轻量静态 Web SPA，避免额外 Node 启动步骤；业务核心与 CPA `GrokLLMProvider`、`GrokImageProvider` 及本地降级 Provider 解耦。
- **文件边界提案**：`backend` 负责 Python 应用/领域/Provider；`frontend` 负责静态 Web；`tester` 负责新增测试与版本化报告；`supervisor` 负责合同、BUILD_LOG、README 与 PRD 0.3 收尾。
- **验收口径**：合同字段逐项映射 PRD 12 节；所有 endpoint 有请求/响应/错误示例；一条启动命令与一条评测命令被固定；无 R2+ 字段误宣称上线。
- **执行记录（2026-08-04）**：用户确认开始；冻结 `docs/API_CONTRACT.md` 与 `docs/AC_MATRIX.md`，标准启动命令为 `conda run -n torch128 python -m profagent`，标准评测命令为 `conda run -n torch128 python -m profagent.eval`。S0 验收通过，已按隔离文件边界并行委托 `backend` 与 `frontend` 执行 S1A/S1B。

### S1A — Grounded 推荐后端（压缩 W2–W3，可与 S1B 并行）

- **委托对象**：`backend`。
- **目标**：交付首个可运行 API 里程碑：Fixture Repository、CPA `grok4.5` Stylist Provider、SceneParser、紧迫度门控、HardFilter、Rule + BM25 + RRF、可失败 Dense Adapter、Outfit Assembler、ID/硬约束最终验证器、双层 Catalog Gate、Trace 与核心降级。
- **端点**：先完成 `/health`、`/scene/parse`、`/recommend`、`/trace`；为后续端点保留合同实现位置。
- **必须满足 AC**：AC-01、03、04、05、06、10、11；支持 AC-12 的多轮状态连续性。
- **不得破坏**：高急切度 Catalog 调用 0；过滤项永不复活；非法 ID 永不出服务边界；不足三方向时返回 1–2 个合法方向和缺口，不放宽硬规则；LLM/Dense/Catalog 故障走确定性规则路径。
- **验收口径**：API 可启动；既有校验/测试不回归；上述 AC 有单元/合同证据；Trace 能显示 `event_horizon`、`urgency`、`shopping_allowed`、过滤原因、各路召回、融合结果、最终白名单验证和 Catalog 未调用原因。
- **执行记录（2026-08-04）**：`backend` 已交付 FastAPI、CPA `grok4.5 -> grok-4.5` 固定映射、SceneParser、双层门控、HardFilter、Rule/BM25/RRF、可降级 Dense、Assembler、最终 Validator 与 Trace。supervisor 独立 smoke 首次发现 AC-01 混合情绪误判为纯 `vent` 的 P0；已回派并修复。精确 AC-01 现返回 3 套、Catalog 0；AC-08 纯倾诉仍不强推。supervisor 复验固定 30 例：Urgency 30/30、Shopping Gate 30/30、高急切度 Catalog 0/10、grounding/禁项/78 套槽位完整均无失败；`validate.py` 通过、基线 pytest 5/5。

### S1B — Team Home 与 Stylist Studio 壳层（压缩 W2，可与 S1A 并行）

- **委托对象**：`frontend`。
- **目标**：基于 S0 合同完成中文首发的 Team Home、Stylist Studio、衣橱浏览、场景摘要、初始方向卡和 Debug/评测入口；可先使用合同夹具，随后切真实 API。
- **必须满足 AC**：AC-01、03、06、11；展示团队/成员边界并支持 AC-12/18 的 R1 边界说明。
- **不得破坏**：R1 只显示 Stylist 可用；非穿搭任务不得伪装由未上线成员完成；高急切度 DOM、文案与响应数据中均无购物 CTA；普通模式隐藏内部 ID，Debug 可追溯；无 3D/360°/视频入口。
- **验收口径**：桌面 Web 完整，移动浏览器可完成核心路径；键盘可操作；重要状态有文字而非只靠颜色；最多三个方向且首选、理由、风险、替换入口齐全。
- **执行记录（2026-08-04）**：`frontend` 交付 `web/index.html`、`styles.css`、`fixtures.js`，但未在收敛时限内完成 `app.js`，因此不能宣称 agent 自验通过；supervisor 停止其继续扩写并接管最小交互。现已补齐同源 API/显式 fixture 降级、high 前端二次门控、最多三方向、单一 Active Look、普通/Debug ID 隔离、Trace 与衣橱筛选；`node --check`、DOM ID 合同和 FastAPI 静态资源 smoke 通过，待 reviewer 独立审查。

### M1 — 首个可运行纵切里程碑审查

- **委托对象**：`reviewer`（严格 read-only，只报告不修改）。
- **审查范围**：S1A/S1B 集成后的 API、UI、测试证据和 Trace。
- **输出格式**：按 `[P0]`/`[P1]`/`[P2]`，每项含 PRD 条款/AC、复现方式、影响和建议修复方向。
- **门槛**：发现退回对应 owner；不放宽规则。进入 S2 前必须关闭所有 P0/P1，P2 要么修复，要么证明确属已记录 R2+ 且不影响 R1。
- **可选对抗复核**：关键门控、身体尊重或记忆安全结论可用 `$call-grok`；调用前按 AGENTS.md 询问本次模型 ID。role 从本次安全审查任务派生，仅传必要片段/测试输出，结果只作建议证据。
- **审查执行记录（2026-08-04，未关闭）**：`reviewer` 的第一轮对抗复现发现三项 P0，里程碑保持红灯并已回派：① `/recommend` 可接受被客户端篡改的完整 Scene，从而把 `today/high` 改为 `planned/low` 并触发 Catalog；后端须按 `request_id` 绑定服务端权威 Scene，客户端只能收紧、不能放宽安全字段；② API 失联时前端 fixture 推荐未执行完整 HardFilter，可能把禁忌色衣物伪装成合法方案；前端离线模式改为只读浏览，不做不完整的安全推荐；③ 数据没有鞋跟结构化字段时，HardFilter 把未知鞋款当作符合“不穿高跟鞋”，后端须仅允许有明确非高跟证据的鞋并对其余候选 fail-closed。另有 AC-03 澄清会话、替换入口、递归 Trace 脱敏、Catalog 缺口槽位/场合过滤、整套场合复检、Trace 版本证据和 CPA 返回模型验证等 P1/P2，均已回派；修复与独立复测完成前不得进入 S2。
- **修复与 supervisor 独立复测（2026-08-04，待 reviewer 签收）**：暂停/恢复后 `backend` 与 `frontend` 已分别收敛上述发现。`/recommend` 绑定服务端 `request_id`；未知或身份不匹配 fail-closed；鞋跟未知证据不进召回、替换或最终；Trace 递归脱敏并补版本；Catalog 按明确鞋槽、正式场合、预算、季节和 ETA 过滤；关键场合逐槽复检；离线 UI 只读零方向；AC-03 复用 session 且只澄清一次；alternatives 可见；购物 UI 使用全链路 AND 门控。supervisor 串行复测：`validate.py` 通过、pytest `5 passed`、`node --check` 通过；攻击回放确认篡改后仍 `today/high/shopping=false` 且 Catalog `0`，`g021/g022` 被过滤，嵌套 secret 不可见，缺鞋补购仅返回合格 Mock 鞋，u03 无合法面试鞋时安全返回 gap；固定 30 例为 Urgency `30/30`、Shopping Gate `30/30`、高急切度 Catalog `0` 为 `10/10`、grounding/禁项无失败、Slot Completeness `74/74`。一次真实 CPA 调用精确发送 `grok-4.5`，但代理回显 `grok-4.5-build`，因此实现记录 `model_verified=false`、`resolved_model=null` 并规则降级，未伪装为已验证模型。当前由 `reviewer` 执行修复后只读复审，复审通过前仍不进入 S2。
- **M1 最终关闭记录（2026-08-04）**：修复后复审又对抗发现 parse-time 结构化字段可放宽 `today`、槽位词被误当购物缺口、否定同义表达及并列双缺共享谓词等问题；均按 owner 回派，不以特例放宽。最终实现采用单调安全合并、跨用户 session/request 冲突拒绝、硬约束并集持久、分句/槽位绑定的缺口正负语义和协调组共享谓词。supervisor 最终重放覆盖时间/意图冲突、17 个否定/已满足表达、9 个并列/独立表达、合法购物、跨用户隔离与 u03 正式鞋缺口；固定评测仍为 Urgency `30/30`、Gate `30/30`、高急 Catalog 0 `10/10`、Slot `74/74`、alternatives `269/269`。`reviewer` 明确确认所有 M1 P0/P1/P2 已关闭并签收，允许进入 S2。

### S2A — Look 共创、评分、调整、记忆后端（压缩 W4–W5，可与 S2B 并行）

- **委托对象**：`backend`。
- **目标**：完成不可变 Look 版本链、六维情境化 Scorecard、视觉证据/置信度与失败降级、调整接受/拒绝/部分接受、拒绝建议去重、满意定稿、反馈和记忆 `propose-confirm-commit/view/delete`。
- **端点**：完成 `/look`、`/scorecard`、`/adjust`、`/finalize`、`/memory`，并补齐各对象独立 Trace。
- **必须满足 AC**：AC-07、13、14、16、17；补充 AC-09、15；不得回归 AC-01/04/05/10/11。
- **不得破坏**：评分维度固定为场景适配、个人表达匹配、整体协调、轮廓与层次、舒适与实用、细节完成度；不评价身体；每轮最多 2 项；版本不可覆盖；满意后停止施压；敏感记忆默认拒写。
- **验收口径**：v1→vN→Final 可回放、比较与回退；所有调整含动作/理由/预期维度/成本；拒绝建议有规范化语义去重；Vision 失败只给定性/补拍指引；LLM/Dense/Catalog/Vision 故障注入均保留核心路径。

### S2B — 完整共创 UI 与 API 集成（压缩 W4–W5，可与 S2A 并行）

- **委托对象**：`frontend`。
- **目标**：完成当前 Look、静态图片上传、六维评分卡、证据与置信度、1–2 项调整、接受/拒绝/部分接受、版本轨迹/对比/回退、满意定稿、记忆管理、Trace/评测页。
- **必须满足 AC**：AC-07、13、14、16、17；补充 AC-09、15。
- **不得破坏**：用户满意优先于系统分数；评分文案始终指向穿搭与目标；视觉不足不显示伪精确总分；高急切度全界面无购物 CTA；仅静态 2D 资产，无 3D/视频入口。
- **验收口径**：两个固定自然语言故事与一个“图片评估→调整→复评→满意定稿”故事可从 UI 端到端完成；Debug 页可查版本与 Trace；错误/降级状态可理解、可继续操作。

### M2 — 完整 R1 闭环里程碑审查

- **委托对象**：`reviewer`（read-only）。
- **审查范围**：PRD 6、9、11、12、16、23.1；特别检查购物双门控、ID grounding、硬过滤、评分对象、调整上限/去重、版本不可变、记忆治理、故障降级及 R2+ 泄漏。
- **验收口径**：同 M1；所有发现回派对应 agent 修复并复审，不能通过改测试期望、降低阈值或隐藏 Trace 过关。
- **首轮预审记录（2026-08-04）**：supervisor 独立攻击先发现 CRC 合法但不可解码的伪 PNG 会被误标为视觉证据充分，以及图片上传未建立新 Look、`user_modified` 携带伪 adjustment ID 等链路问题，均已回派。随后 `reviewer` 只读预审确认三项 P0：AC-07 点踩/记忆未进入后续推荐，删除或 TTL 到期后 committed proposal 仍残留原文，Look/Trace 读取缺少 owner 绑定；并报告缺少 `/eval/run`、拒绝决策不可回放、Trace 缺对象 ID、`user_modified` 被误记为 partial 等 P1/P2。M2 因此保持红灯，没有用隐藏 Trace 或改低测试期望过关。
- **实现修复候选（2026-08-04，待最终签收）**：后端已补完整图片解码、单帧 PNG/JPEG/WebP 与大小/像素限制、显式同意/用途、进程内临时资产及删除；Vision 仅接受受控槽位可见性/问题码，模型不精确匹配、证据不全或自由文本一律降级为定性结果；图片上传、删除、回退均建立新 Look，旧版本不可覆盖。Look/Trace/Asset 已 owner-bound；反馈、受控记忆、TTL/删除与后续排序闭环；评分卡固定六维且只评价穿搭；调整每轮最多 2 项并阻断拒绝建议复述；`/eval/run`、对象 Trace、决策回放与安全 ValidationError 已补齐。前端同步完成上传/删除版本化、方向 like/dislike、白名单/owner/version 二次校验、评分安全文案、满意定稿和真实评测命令展示。
- **追加安全对抗修复（2026-08-04，待最终签收）**：rain 必需 outer 仅接受结构化 `waterproof=true`，fixture 无证据时在 Rule/BM25/Dense/RRF 前过滤；Mock Catalog 对 rain 的 top/bag/outer/empty 任意缺口均全局拒绝，禁止名称猜测。纯倾诉、医疗/身体越界、敏感文本在 CPA 前本地短路，Trace 只保留 `LOCAL_SAFETY_OR_PRIVACY_SHORT_CIRCUIT`。当前 reviewer 继续核查测试是否真正独立验证这些防线，复审通过前仍不关闭 M2。
- **M2 最终关闭（2026-08-06）**：后续对抗又关闭无期限 query 被猜作 planned、泛化商品词误建 gap、明确“不购物”未覆盖真实 gap、合法 Catalog 空库存语义、AC-07 排名因果/删除恢复、Final 并发锁、前端决策账本与 Provider 状态误报等问题。最终 reviewer 对服务端权威场景、双层购物门控、全召回层过滤、六维评分安全、调整去重、owner-bound 不可变 Look/Final、记忆生命周期、图片/Vision 降级、Trace 脱敏及 R2+ UI/路由逐项复审，结果 `[P0] 0 / [P1] 0 / [P2] 0`，M2 签收。

### S3 — 固定评测、E2E 与版本化报告（压缩 W6）

- **委托对象**：`tester`。
- **目标**：扩展现有 `tests/`，以 `data/eval/eval.jsonl` 为固定评测输入，新增 API/领域/UI 关键路径与故障注入测试，输出版本化 JSON + Markdown 报告；接通 `/eval/run`。
- **必须满足 AC**：AC-01/03/04/05/06/07/10/11/13/14/16/17；同时覆盖 AC-09/12/15/18 和 PRD 23.1 的其余可自动化条目。
- **不得破坏**：不修改既有评测真值来迁就实现；不把 Mock Catalog 当真实商品；报告必须记录 data/eval/rule/ranker/provider 版本和运行命令。
- **验收口径（全部为硬门槛）**：
  - UrgencyAcc ≥ 95%；
  - 高急切度 ShoppingGateAcc = 100%，Catalog 调用数 = 0；
  - Item Hallucination = 0；
  - Hard Constraint Violation = 0；
  - Slot Completeness ≥ 95%；
  - Look 版本链完整率 = 100%；
  - 评分对象误指向颜值/身体 = 0；
  - Sensitive Write False Positive = 0；
  - 每轮调整数 ≤ 2，拒绝建议在会话内不重复；
  - LLM/Dense/Catalog/Vision 故障注入下核心流程可用；
  - 一条命令生成 JSON + Markdown 报告。
- **评测完整性审查记录（2026-08-04，进行中）**：第一版候选曾得到 `pytest 15 passed` 与固定指标全绿，但 reviewer 发现报告复用了业务 `HardFilter` 作为“独立”真值，并信任响应自报的 `required_slots_complete`，同时 AC-01/03/04/05/06/10/11/16 的单项映射没有覆盖完整 Given/Then；该结果已明确作废，不作为发布证据。tester 正改为独立硬约束/槽位 oracle、扫描完整用户可见评分文本，并加入非法/跨 owner ID 注入、Catalog 上层 spy + 服务层 forced-call、两轮 unknown、鞋跟 fail-closed、状态过滤召回层、稀疏方向、rain 与 CPA 隐私短路等确定性 assurance。只有新报告与全量测试串行通过后才记录最终数字。
- **S3 首次正式门禁（2026-08-05，红灯）**：tester 串行执行得到 pytest `24 passed, 2 failed`，因此按规则立即停止，未用改低阈值或跳过用例继续发布。失败暴露 Catalog e011/e012 合法空库存 oracle、query intent、AC-07 同一候选域和 AC-05 归因等实现/评测问题；均回到对应 owner 修复，并补齐报告准确性 P2。
- **S3 最终门禁（2026-08-06，绿灯）**：tester 已扩展测试与独立评测；其会话凭据失效后，由 supervisor 使用同一冻结命令和工作树串行重放，不改变真值。`compileall` 通过，pytest `32 passed`；固定评测 `overall=PASS`：Urgency `30/30`、高急 Shopping Gate `10/10`、高急 Catalog `0/10`、ID 幻觉 `0/515`、硬约束违反 `0/423`、槽位完整 `74/74`。21 条 query-only、3 条 no-deadline、AC-01/03/04/05/06/07/10/11/13/14/16/17、SAFE-RAIN、SAFE-CPA-PRIVACY、OBS-05、Catalog oracle 和数据对齐 assurance 均通过。`scripts/validate.py` 复核 `3/50/20/50/30` 通过；JSON/Markdown 写入 `reports/eval/r1_demo_v1.*`。

### M3 — Grok + Codex 最终联合审查、相互验证与全绿复测

- **参与者**：`reviewer` 进行 Codex 侧只读审查；CPA `grok4.5` 进行独立对抗审查；`tester` 串行复测；`supervisor`（Codex）负责证据核验、缺陷回派、冲突裁决和最终收拢。
- **第一遍独立审查**：Codex reviewer 不依赖 Grok 结论，按 PRD/AC、代码、Trace、测试和候选发布文档输出 `[P0]/[P1]/[P2]` 报告。
- **第二遍独立审查**：使用 `conda run -n torch128 python ...call_grok.py --model "grok-4.5"`，以当前 R1 安全与验收任务派生 role，只发送必要的 PRD 摘要、diff、测试结果和 Codex 未披露结论的审查材料，得到独立 Grok 报告。
- **相互验证**：将 Codex 发现摘要交给 Grok 做反驳/漏项检查；Codex 再逐条用本地文件、运行结果、Trace 和 PRD 验证 Grok 发现。任何模型意见都不能直接替代可复现证据。
- **冲突规则**：PRD 是唯一需求权威；本地可复现代码/测试证据优先于模型断言。分歧由 supervisor（Codex）记录证据与裁决，不以多数意见放宽规则。
- **修复闭环**：任何有效失败返回 `backend`/`frontend`/`tester` 修复；修复后从相关最小测试开始，再跑全量校验、pytest、固定评测、Demo smoke test，并对受影响结论再次交叉复核。
- **外部失败处理**：若 CPA 或 `grok4.5` 最终审查调用失败，记录错误并继续本地修复准备，但不得宣称“Grok + Codex 联合审查完成”；恢复后补跑联合审查。
- **退出条件**：无未关闭的 in-scope P0/P1/P2；所有硬门槛全绿；Codex 与 Grok 的有效发现均有验证/关闭证据；不得以“Demo 特例”、跳过用例、放宽阈值或只改报告文案宣布完成。
- **M3 执行记录（2026-08-06）**：Codex 独立审查先得到 `[P0] 0 / [P1] 0 / [P2] 0`。CPA `grok-4.5` 在未获 Codex 结论的独立材料上同样报告零发现，并提出 10 个高风险证据缺口；Codex 逐项映射到本地代码、独立 oracle、测试、Trace、API/startup/browser smoke，再交 Grok 对抗复核。第二遍 Grok 将 10 项全部判为 `closed for R1 Demo`，仍为 `0/0/0`；Codex 最后逐项用本地证据复核并收拢。证据见 `reports/review/codex_independent_review.md`、`grok_independent_review.md`、`mutual_challenge_packet.md`、`grok_mutual_review.md`、`codex_mutual_verification.md`。浏览器 smoke 额外发现并关闭一项 CPA 关闭却显示“正常”的 P1；修复后只有 enabled、available、精确模型验证与 resolved model 同时成立才显示正常，截图为 `reports/demo/stylist-home-verified.png`。

### S4 — 文档、命令与发布一致性收尾

- **负责人**：`supervisor`（Codex，最终收口责任人）。
- **目标**：确保一条命令启动 Demo、一条命令出评测报告；同步 README、PRD 0.3、BUILD_LOG 和实际实现状态。
- **验收口径**：
  - README 提供可复制的 `conda run -n torch128 ...` 启动与评测命令；
  - PRD 0.3 只把经最终测试证实的能力移入“已完成”；
  - 2D 购前试演、真人专家、模板市场、真实购物、第二成员、3D/视频明确标“未上线”；
  - BUILD_LOG 记录每阶段委托、产物、命令、评测数字、reviewer 发现及关闭证据；
  - 现有数据生成/校验与 5 个基线测试继续通过；
  - Grok 提供独立审查证据但不替代本地验收；最终由 Codex supervisor 汇总实现、测试、两侧审查和修复证据，仅在全部门槛通过后宣布完成。
- **S4 执行记录（2026-08-06）**：README、PRD 0.3、API 合同、AC 矩阵与 BUILD_LOG 已同步一条启动命令、一条评测命令、最终固定数字、Provider 真实状态和产品边界。CPA 静态 2D 生图/购前试演、第二成员、真人专家、模板市场、真实购物/支付、3D/360°/视频及社区继续统一标记“未上线”；R1 仅包含用户授权静态穿搭图片分析，不把它描述为生图或虚拟试穿。
- **最终串行复测（2026-08-06）**：文档收口后再次执行 `compileall`、pytest、固定评测、`scripts/validate.py`、`scripts/supervisor_s2_probe.py` 与 `node --check`，结果分别为 PASS、`32 passed`、`overall=PASS`、`3/50/20/50/30` PASS、深度安全矩阵 PASS、PASS。新增 `scripts/supervisor_http_smoke.py` 在临时本地端口启动同一个 FastAPI/SPA 应用并干净停止，确认 `/health.ready=true`、数据计数正确、主页 HTTP 200，且 `static_2d/video/three_d=false`。宿主最初阻止隐藏后台进程方案，后续没有绕过策略，而是把等价的进程内真实 HTTP 启停固化为可复现脚本。
- **真实运行缺陷与关闭（2026-08-06）**：用户截图显示首个约会推荐出现 `signal is aborted without reason` 并误入离线只读。supervisor 对用户仍在运行的实例实测同句 `/scene/parse` 用时 `21,140ms` 后才返回 `rule_fallback`，而前端在 `12,000ms` 主动 abort，确认是 CPA 与浏览器预算竞态。backend 增加 Health/Scene/Vision `1.5s/8s/10s` 硬预算（环境变量只能收紧）、30 秒 Scene 熔断、受控 reason code，以及外部取消时删除未完成 Trace 并重新抛出；frontend 将 Scene/Recommendation 设为 `15s`，把 timeout/abort/network 映射为中文，单次失败保持 API 在线、强制 0 方向/0 商品/CTA false，并支持绑定 query+request+session 的安全重试。第一轮 reviewer 报告 P1=3（Scene 吞取消、Health/Vision 仍可先被前端中止），全部回派关闭；复审为 `[P0] 0 / [P1] 0 / [P2] 0`。tester 最终 8/8 串行门禁全绿：`39 passed`、固定评测原数字不变且 `overall=PASS`、校验 `3/50/20/50/30`、深度 probe、Node/static contract 与 HTTP smoke 均 PASS。
- **两轮澄清输入与意图边界缺陷关闭（2026-08-06）**：用户截图显示第二轮必须在残留首句后追加“明天”。frontend 修复提交后未清空 textarea 的根因，点击与 Ctrl/Cmd+Enter 共用同一逻辑；澄清轮仅发送新增 `query_text` 与原 `styling_session_id`，失败重试只使用绑定 state，不回填或重复显示旧句。backend 修复“具体约会 + 轻度紧张”被当作纯倾诉的问题，并以窄规则保留“穿什么都觉得不对”等整体无助表达的 `vent` 本地支持流；支持文案按实际期限显示“明天”，不误写“今天”。首轮 reviewer 因 e028 被过宽规则误判给出 P0=1，未放宽固定真值，回派后关闭；最终复审为 `[P0] 0 / [P1] 0 / [P2] 0`。tester 从头严格串行复测：澄清专项 `6 passed`、全量 pytest `45 passed`，固定评测 `overall=PASS` 且数字不变，validate、S2 probe、Node、前端静态契约与独立 HTTP smoke 全部 PASS。

### S5 — Stylist 会话优先重构（2026-08-06，已关闭）

用户实测确认当前界面仍表现为“字段收集工具”：首轮已说明“今天首次约会、紧张”，第二轮明确要求“先不推荐、先缓解情绪”，系统却重新询问截止时间。只读复现证明前端仅在 `pendingClarification=true` 时延续 session，普通后续轮会新建 session；即使手工复用 session，后端也没有暂停/恢复模式，仍会自动推荐。reviewer 初审为 `[P0] 2 / [P1] 2 / [P2] 1`，本阶段保持红灯直至全部关闭。

#### S5A — 对话合同与状态机（委托 `backend`）

- **目标**：新增会话优先的 `/dialogue/turn` 编排层；Stylist 是对话主体，SceneParser、衣橱检索、评分和 Catalog 是按回合计划调用的专业工具。
- **对应 AC/规则**：PRD 9.4、10.5、CNV-02/04/05/06/07、AC-03/08/11/12、SAFE-03/04、OBS-05。
- **状态合同**：`styling_active | support_pause | safety_response | task_closed`；回合动作 `support | clarify | recommend | acknowledge`；待澄清生命周期 `none | active | suspended | resolved | cancelled`。
- **硬优先级**：安全风险 → 当前轮明确暂停/纠正/终止 → 当前轮交流意图 → 已挂起澄清 → 推荐/工具；“最多澄清一次”是上限，不是必须执行脚本。
- **不得破坏**：支持/暂停/关闭轮必须 0 方向、0 商品、Catalog 实际调用 0；场景快照 owner-bound；情绪只留 session TTL，不自动写记忆；医疗/身体/安全越界仍本地处理；普通支持回复可使用经最小化的 CPA `grok4.5` 结构化上下文，失败或输出越界必须回退本地安全角色回复，不发送完整历史或身份信息。
- **验收口径**：截图三轮（首次约会紧张 → 明确暂停 → 明确恢复）保持同 session；暂停轮不追问、不推荐；恢复轮继承 date/today/goal/constraints；“不用推荐了”关闭；支持模式中的“明天”只更新上下文、不自动恢复；Trace 只记录受控 mode transition，不含情绪原文。

#### S5B — 连续对话 UI（委托 `frontend`，合同冻结后与 S5A 并行）

- **目标**：前端所有连续回合调用 `/dialogue/turn` 并默认携带当前 `styling_session_id`；只有用户明确开始新任务才重置。UI 根据服务端 `action` 决定是否展示澄清、支持回复或推荐，不再每轮自动进入 `/recommend`。
- **对应 AC/规则**：CNV-01/05/06/07、AC-03/11/12、中文首发与键盘可达。
- **不得破坏**：支持轮清空输入框但不丢 session；0 方向/0 购物 CTA；Ctrl/Cmd+Enter 与点击一致；失败重试绑定 turn/session，不重复用户消息；无 3D/视频入口。
- **验收口径**：截图路径可在 UI 完成；支持模式明确显示“已暂停穿搭建议”；建议回复可选择“继续聊聊/现在开始搭配/结束本次任务”，但不替用户自动恢复。

#### S5M — 角色、安全与回归验收（`reviewer` read-only → `tester`）

- **reviewer**：逐项复审初始 2 个 P0、2 个 P1、1 个 P2；检查角色是支持型专业伙伴而非治疗师或工具，暂停/恢复/终止的用户控制权、CPA 最小化、Trace/记忆隐私及全部 R1 硬门槛。
- **tester**：新增确定性多轮 E2E、前端静态合同、CPA 成功/失败/违规输出降级与旧 AC 回归；不修改冻结真值或阈值。
- **退出条件**：reviewer `[P0/P1/P2]=0/0/0`；全量 pytest、固定 eval、validate、S2 probe、Node/static contract、HTTP smoke 全绿；固定指标保持 UrgencyAcc≥95%、高急 Gate=100%、高急 Catalog=0、幻觉=0、硬约束违反=0、Slot≥95%。

#### S5 最终关闭记录

- `backend` 新增 owner-bound `/dialogue/turn`、`styling_active/support_pause/safety_response/task_closed` 状态机、澄清生命周期、暂停/恢复/关闭与受控纠正；普通支持使用最小枚举 CPA 合同及本地闭集回复，医疗/身体/整体无助保持本地。全局 async single-flight 保证同 request 不重复 Scene/CPA/recommend/Catalog；30 分钟可收紧滑动 TTL 惰性清理 Scene/receipt；`task_closed` 是不可逆 latch。
- `frontend` 所有连续回合默认复用 session，仅显式“开始新任务”重置；mode/action/pending/paused 组合、权威 Scene、额外工具字段和购物 UI 全部 fail-closed；只有 `action=recommend` 才渲染方向，支持轮可见更新后的 date/time 但保持 0 方向/0 CTA。
- `reviewer` 初审报告 `[P0] 2 / [P1] 2 / [P2] 1`，随后对抗发现并回派 session 丢失、模式控制、并发副作用、resume 时间误判、vent 恢复错配、terminal latch、同轮 safety+close、澄清暂停语义、时间/场合纠正、Trace 派生隐私和前端组合矩阵等问题；最终逐项重放后签收为 `[P0] 0 / [P1] 0 / [P2] 0`。
- `tester` 独立串行最终门禁全部 Exit 0：Dialogue 专项 `33 passed`、全量 pytest `78 passed`、固定评测 `overall=PASS`（Urgency `30/30`、高急 Gate `10/10`、高急 Catalog `0/10`、幻觉 `0/515`、硬约束 `0/423`、Slot `74/74`）、validate `3/50/20/50/30`、S2 probe、Node、静态合同与 HTTP smoke 全绿。独立多轮探针确认首句→暂停→冷场三轮实际 Recommend/Catalog=`0/0` 且回复不重复，支持中补充/纠正只更新 Scene，明确恢复后 Recommend=`1`、Catalog=`0`；关闭、安全、unknown 澄清与 CPA 故障降级均符合前后端矩阵。

### S6 — CPA 原生对话与实验性静态 2D（2026-08-06 至 2026-08-07）

用户实测指出 S5 虽修复会话状态，但回复仍是规则模板，不是“专业造型师人格 × 用户画像 × 多轮上下文”的自然角色对话。运行证据确认普通支持回合仅让 CPA 返回三个闭集策略码，用户可见正文仍由本地模板拼接；`“先聊聊天”` 还可能被旧页面链路送入 `/scene/parse`。同时 CPA 请求 `grok-4.5` 的成功响应回报构建标识 `grok-4.5-build`，旧实现因精确字符串比较误判为模型不匹配并显示 `rule_fallback`。

#### S6A — CPA-first 对话与 Provider 合同（委托 `backend`）

- **目标**：`/dialogue/turn` 的普通用户可见正文由 CPA 直接生成；服务端注入固定 Stylist 人格、当前相关的受控用户画像、权威 Scene 与有界多轮上下文。Scene/意图/动作可由模型建议，但本地状态机、安全策略、购物门控、ID 白名单、记忆写入和最终响应 Validator 始终拥有否决权。
- **模型合同**：逻辑模型固定 `grok4.5`，请求传输 ID 固定 `grok-4.5`；分别记录 `requested_model/transport_model/resolved_model`。只接受显式登记且来自当前 CPA 的同族构建回报（当前实测 `grok-4.5-build`），不得用任意前缀匹配或自动切换其他模型。
- **画像与上下文**：只发送当前任务必要、用户已授权且服务端白名单化的属性；情绪只留 session TTL，敏感记忆默认不发、不写；历史窗口有长度/字符上限、owner 隔离和 TTL。Prompt 中明确用户文本与历史都是不可信数据，不能覆盖系统人格、工具权限或硬规则。
- **动作合同**：没有活动穿搭任务时，`先聊聊天` 必须是 `stylist_chat + chat` 且 `scene=null`；已有任务时，`先不推荐/继续聊聊` 进入 `support_pause` 并保留 Scene。两类回合都尝试一次 CPA，Recommend/Catalog 为 0，不能追问穿搭截止时间；只有明确恢复才进入推荐。CPA 输出中的购物许可、Catalog 意图、衣物/商品 ID、长期记忆或 3D/视频指令一律不可信，由服务端删除或拒绝。
- **降级合同**：CPA 超时、断连、错误模型、坏 JSON 或安全校验失败时允许本地安全回复继续，但响应与 Trace 必须明确 `degraded=true` 和受控原因，不能伪装成模型个性化成功。
- **对应 AC/规则**：PRD 9.1/9.4、10.1–10.5、CNV-01/02/04/05/06/07、AC-03/08/10/11/12、SAFE-01/03/04/08、OBS-01/05。
- **验收口径**：传输 spy 证明每个普通回合 CPA=1；三轮以上同 session 的受控画像/历史连续；模型注入攻击不能改变权威门控；Trace 可证明 provider/model/status 且不含 API key、原始完整会话或敏感画像。

#### S6B — CPA 静态 2D 生成纵切（委托 `backend` + `frontend`）

- **目标**：启用实验性 `POST /preview/static-2d`；只接收 owner-bound 的不可变 `look_version_id` 与 `render_mode=static_2d`，衣物 ID 由服务端从 Look 派生，生成调用走同一 CPA 配置。前端提供明确的静态 2D 入口、进度、成功、可删除与降级状态。
- **范围**：这是用户明确要求的实验性 R2 纵切，不改变 R1 DoD；3D、360°、生成/实时视频请求必须在 Provider 调用前拒绝且调用数为 0。
- **可信度与隐私**：人像使用前单独同意；返回绑定 Look/scene/garment IDs、provider/model/version、AI 标识与“仅供风格/场景参考，不代表精确尺码、面料或垂坠”；日志/Trace 不保存 base64、原始提示词、API key 或人像内容。
- **失败降级**：CPA 不支持图像、超时、错误模型、坏 MIME/过大内容时降级为衣物卡片/平铺组合/文字解释，不阻塞 R1；不得偷偷切换其他模型。
- **对应 AC/规则**：PREV-04/05/06/08/10/12、SAFE-05/06/07、OBS-05、EVO-01。
- **验收口径**：成功路径实际 CPA 调用=1；未知/跨 owner/过期/篡改 Look 在调用前拒绝；3D/视频调用=0；生成资产可删除；前端与 `/health.capabilities.static_2d` 状态一致。

#### S6M — UI、对抗审查与全量回归（委托 `frontend` → `reviewer` read-only → `tester`）

- **前端目标**：以聊天为主界面，不再让 Scene 字段收集主导对话；连续回合始终携带 session；显示“CPA 已生成/安全降级”状态；通过静态资源版本号阻止旧 `app.js` 继续调用 `/scene/parse`；高急切度仍无购物 CTA。
- **reviewer**：按 `[P0]/[P1]/[P2]` 对抗检查 prompt injection、画像最小化、历史/owner/TTL 隔离、模型输出不可信、购物双门控、ID grounding、敏感记忆、降级诚实性及 2D-only 边界；只报告不改文件。
- **tester**：新增 CPA-first 自然对话、个性化历史、模型/用户/历史三层注入、Provider 失败、静态 2D 白名单/删除/红线/Trace 测试；不修改既有真值与阈值。
- **退出条件**：reviewer `[P0/P1/P2]=0/0/0`；专项、全量 pytest、固定 eval、validate、S2 probe、Node/static contract、HTTP smoke 全绿；固定 R1 指标不退化。完成后同步 README、PRD 0.3、API_CONTRACT、AC_MATRIX，并重启真实 8000 实例做 CPA 对话和静态 2D smoke。

#### S6 自动化与审查关闭记录（2026-08-07）

- `backend` 已实现每个新 `/dialogue/turn` 最多一次 CPA 调用、幂等重试零新增调用、固定 Stylist 人格、画像 allowlist、最多 6 条 TTL 历史、敏感/标识符最小化、30 秒独立 Dialogue 预算和 64 KiB/800 token/600 字符输出边界。本地状态机继续权威决定 mode/action/control、Scene、推荐、购物、ID、记忆与工具调用；模型正文/建议中的 ID、购物 CTA、截止时间诱导、3D/视频、人物外貌/身体/年龄/性吸引力评价、诊断和用药建议均整体 fail-closed。
- `backend` 与 `frontend` 已启用实验性 `/preview/static-2d`：只从 owner-bound 不可变 Look 派生衣物，生成中性无身份模特或平铺参考；前端固定不提交身份资产，服务端即使收到合法 `identity_asset_id` 也只做 owner/consent 校验且不发送给 CPA，身份一致性固定 `not_assessed`。响应使用 owner/session 图片 URL、AI 标识、局限说明及删除端点；3D/360°/视频在 Provider 前拒绝。
- `reviewer` 初轮报告 `[P0] 2 / [P1] 4 / [P2] 1`，修复旧模板架构、模型信任、隐私、来源标识和资源边界后，又通过 Mock 输出发现“3D试穿/视频/评价身材/否定诊断”可穿透的真实 P0；后端将 Validator 泛化覆盖整类风险。最终只读复审重放 84 项 Dialogue、13 项独立模型攻击矩阵、身份不发送、图片 owner/session/删除、Trace/Health 隔离及前端无旧链路/无 3D 视频入口，签收 `[P0] 0 / [P1] 0 / [P2] 0`。
- `tester` 发现并关闭一项 P1：Look 创建后衣物变为 unavailable 时，Static2D 虽能在 Provider 前拦截，却把领域异常泄漏成 500；现统一映射为安全 422 且 Provider 调用 0。初次 S6 门禁为 Static2D `11/11`、Dialogue `84/84`、全量 pytest `140 passed`；随后真实 CPA smoke 暴露并关闭 fenced JSON 兼容问题，tester 增补完整单围栏正例、围栏外正文/错语言/双围栏负例、围栏内六类安全攻击与熔断隔离矩阵。最终冻结门禁为 Dialogue + CPA 预算 `108 passed`、全量 pytest `157 passed`，`compileall`、Node 语法/静态合同、diff-check、S2 probe 与 HTTP smoke 全绿；`scripts/validate.py` 为 `3/50/20/50/30`；固定评测 `overall=PASS`，Urgency `30/30`、高急 Gate `10/10`、高急 Catalog `0/10`、幻觉 `0/515`、硬约束 `0/423`、Slot `74/74`。报告仍写入 `reports/eval/r1_demo_v1.json` 与 `.md`，未修改真值或阈值。
- **真实 8000 冒烟补充（2026-08-07）**：第一次业务验证发现 CPA 合法五字段 JSON 被包在完整 `json` fence 中，旧解析器整体拒绝后又把下一独立回合误熔断。backend 将兼容范围冻结为“整个 content 恰为一个 `json|JSON` 围栏、围栏外仅空白”，解包后仍执行原全量字段/action-control/购物/ID/人物医疗/3D视频 Validator；任意围栏外正文、错语言或多围栏继续拒绝。单次输出拒绝不再打开 transport circuit，网络、30 秒业务超时和错误模型仍可熔断；Trace 只记固定 diagnostic code。reviewer 增量签收 `[P0/P1/P2]=0/0/0`。重启后两轮真实 `/dialogue/turn` 均返回 `status=ok/generation_source=cpa/resolved_model=grok-4.5-build`，同一 session、`stylist_chat+chat`、`scene=null`、无 Recommendation。
- **真实 Static2D 能力证据（2026-08-07）**：完整 `Scene→Recommend→Look→/preview/static-2d` 发起一次 CPA 图像调用；当前 CPA 明确返回 HTTP 400，说明 `grok-4.5` 不支持 `/images/generations` 并建议其他图像模型。由于用户冻结了 `grok4.5→grok-4.5` 且禁止自动换模，实现正确返回 `status=degraded`、`identity=not_assessed` 与平铺/文字 fallback；没有图片 URL、没有发送身份图、没有切换模型。自动化 `11/11` 证明 Provider 成功时的 owner/session/AI header/删除生命周期，但不把 Mock 成功描述为当前远程模型已能出图。
- S6 的 Codex reviewer/tester 自动化门禁已关闭。独立 `$call-grok` 的 S6 外部复核尚未在本阶段调用；如执行，须按 AGENTS.md 先确认本次模型 ID，并由 Codex 以本地代码/测试证据逐条裁决。

### S7 — 情绪承接与穿搭任务并行（2026-08-07，自动化与 Codex 审查已关闭）

用户以“近期约会 + 轻度紧张 + 任务内量体信息与服装轮廓目标”复现时，真实 Trace 证明 CPA 已调用且未超时，但本地把轻度紧张误判为 `support_pause`，随后因可选 `scene_advisory` 不符合闭集而丢弃整段模型正文，UI 又把一般合同失败误标为“安全降级”。用户确认该设计逻辑不符合专业造型师角色，要求按以下边界修改代码与提示词。公开日志不保留量体原值或身体目标原句。

#### S7A — 混合意图、任务内测量信息与 Provider 解耦（委托 `backend`）

- **目标**：明确穿搭任务与轻度情绪可以同轮成立；先自然承接紧张，再继续澄清或推荐，除非用户明确说“先暂停/先聊聊”，不得仅因“紧张”进入 `support_pause`。
- **任务内测量信息**：用户主动提供的身高、体重及“想让服装轮廓更有量感/不那么单薄”等目标，只能作为当前 session 的版型、比例、层次和舒适度上下文；不得评价人的胖瘦好坏、推断健康、写长期记忆或进入跨 session 画像。Trace 不记录原值。
- **辅助字段隔离**：`scene_advisory` 仍不可信且永远不能放宽本地 Scene；若其结构或枚举非法，只丢弃 advisory、记录无正文诊断码，并继续对 `reply/suggested_replies/action/control` 执行购物、ID、人物/医疗、3D/视频等原安全 Validator。正文自身越界仍整体 fail-closed。
- **提示词**：要求 CPA 对“轻度情绪 + 明确穿搭任务”采用“先一句承接，再给专业建议”的自然造型师表达；测量信息只用于衣物轮廓，不作身体评价；不确定 advisory 时返回 `null`，不得创造新枚举。
- **对应 AC/规则**：PRD 9.1/9.4、10.1–10.5、AC-01/03/10/11/12/16、SAFE-04/08、OBS-05。
- **验收口径**：冻结原句与同义表达均不进入 `support_pause`；CPA 合法正文 + 非法 advisory 仍 `generation_source=cpa`；违法正文仍 fallback；today/high/shopping=false/Catalog=0；测量值不进长期记忆和 Trace；每个新回合 CPA≤1、receipt=0。

#### S7B — 诚实来源标签（委托 `frontend`）

- **目标**：消息来源只表达“CPA 生成”或“本地回复”；Provider/格式失败显示“本轮使用本地回复”，不能统称“安全降级”。真正的安全边界由回复正文解释，不用徽标暗示用户输入违规。
- **不得破坏**：高急切度购物 CTA=0；无 3D/360°/视频入口；不把本地回复伪装成 CPA；同 session、输入清空和 35 秒浏览器预算保持。
- **验收口径**：静态合同断言页面不再出现误导性的“已安全降级/智能对话暂时不可用”通用标签；CPA 成功徽标、一般本地回复和真实安全回复三类状态可理解。

#### S7M — 增量审查与全量门禁（`reviewer` read-only → `tester`）

- **reviewer**：检查混合意图不会因情绪抢占任务；测量信息严格 task-local；advisory 丢弃不影响权威 Scene且不能成为注入旁路；正文安全 Validator 未放宽；UI 标签诚实。
- **tester**：扩展原句/同义词、显式暂停反例、非法 advisory + 安全文正例、非法 advisory + 违法正文反例、测量隐私/记忆/Trace、购物/ID/人物/医疗/3D视频回归；保持固定真值与阈值。
- **退出条件**：reviewer `[P0/P1/P2]=0/0/0`；专项、全量 pytest、固定 eval、validate、Node/static contract、HTTP smoke 全绿；重启 8000 后真实原句返回 CPA 正文或仅因真实 Provider/正文安全失败而本地回复，绝不再因可选 advisory 单独降级。

#### S7 实施与自动化关闭记录（2026-08-07）

- `backend` 将明确场合、普通“穿什么”、穿搭目标和合法 fit goal 统一纳入当前回合任务证据；“紧张/不安/怕冷场 + 明确任务”先一句承接再继续澄清或推荐，无助表达仍保持支持流。新增 `explicit_pause_latched`：普通 `stylist_chat` 后的新任务可直接工作，只有用户明确“先不推荐/先聊聊”才持续暂停到显式 resume。
- 用户主动提供的身高/体重被窄范围抽取为 owner-bound 当前 session 的 `garment_fit_only` 上下文，原句不进入会话历史或 Trace，Memory 与跨 session 画像不写；孤立测量不创建任务。CPA 提示词限制其仅用于衣物版型、比例、层次和舒适度；服务端输出 Validator 定向拒绝原值复述以及“标准身材/黄金比例”等人物评价。
- 非法 `scene_advisory` 只被置为 `null` 并记录无正文诊断，权威 Scene 不变；随后仍执行顶层字段、action/control、购物、ID、人物/医疗、3D/视频及量体原值的全量校验。危险正文仍整轮 fail-closed；一般 Provider/格式失败显示“本地回复”，真正 `safety_response` 优先显示“安全回应”。
- `reviewer` 初审报告 4 项后端 P1（弱任务证据、轻情绪覆盖、chat→task 状态优先、量体原值输出缺少强制边界）与 1 项前端 P1（安全响应来源标签优先级）；全部回派 owner 修复并逐项静态关闭。最终只读签收 `[P0/P1/P2]=0/0/0`。
- `tester` 对 refrozen 最终树独立重跑：S7 新增专项 `26 passed`、全量 pytest `180 passed`；`scripts/validate.py` 为 `3/50/20/50/30`；固定评测 `overall=PASS`，Urgency `30/30`、高急 Gate `10/10`、高急 Catalog `0/10`、幻觉 `0/515`、硬约束 `0/423`、Slot `74/74`；Node、前端静态合同与独立临时端口 HTTP smoke 全绿，`static_2d=true`、`video=false`、`three_d=false`。未修改 fixture、评测真值或阈值。
- **真实 8000 原句复现**：supervisor 仅停止经 PID/命令校验的旧 `torch128 python -m profagent` 并重启新版本。首轮“你好”真实返回 `status=ok/generation_source=cpa/resolved_model=grok-4.5-build`；随后截图原句在同 session 与独立新 session 各调用一次 CPA，两次均因超过 30 秒硬预算返回 `CPA_INTERACTION_BUDGET_EXCEEDED + local_fallback`。两次都不再进入 `support_pause`，而是 `styling_active + recommend`；本地组装并验证 3 套方案，`today/high/shopping=false/CTA=false/Catalog calls=0`，Trace 只有查询哈希前缀与长度，不含量体原值或身体目标原句。因此 S7 的错误安全/advisory 降级已关闭；当前复杂请求的远端 CPA 延迟作为真实运行限制保留，不提高预算或伪称模型成功。

### S8 — 5 秒内可见回复与近时表达修复（2026-08-07，已关闭）

用户在新版页面复现“等会儿要约会了，怎么办，好紧张”时，Trace `trace_c276f711f186` 证明 CPA 已调用但超过 30 秒预算，且“等会儿”被错误解析为 `unknown`，造成半分钟等待后仍用本地回复并追问截止时间。用户明确要求继续修复，并将可见回复延迟优先控制在 5 秒内。

#### S8A — 服务端延迟上限与近时语义（委托 `backend`）

- **目标**：同步 `/dialogue/turn` 在 CPA 卡住时仍于 5 秒内返回；每个新、非幂等回合仍最多调用 CPA 一次，幂等 receipt 调用 0。服务端 CPA 等待预算冻结为不高于 3.5 秒，超时立即使用本地 Stylist 回复；Dialogue 业务超时不得再开启 30 秒 circuit 而让后续回合跳过 CPA。配置、网络不可达或错误模型仍按既有 fail-closed 规则处理。
- **时间语义**：`等会儿/待会儿/一会儿/过会儿/马上/一会就` 等明确近时表达映射到当天高急切度，不再触发 unknown 截止时间澄清；`shopping_allowed=false`、CTA=false、Catalog=0 保持。
- **本地回复**：混合轻情绪只承接一次，不重复“紧张”；若权威 Scene 已足够则直接给现有衣橱方向，不再追问已经由近时表达回答的时间。
- **对应 AC**：AC-01/03/10/11/12/16、OBS-05、CNV-02/04/05/06/07。
- **验收**：慢 Provider 故障注入下 HTTP 回合墙钟 <5 秒；真实短预算 Trace 原因受控；后续独立新回合仍再次尝试 CPA；近时同义词均 today/high/禁购/不澄清/Catalog0。

#### S8B — 浏览器预算与即时反馈（委托 `frontend`）

- **目标**：浏览器 Dialogue 超时高于服务端但低于 5 秒目标边界，冻结为 4.5 秒；发送后立即进入明确等待态并清空输入，正常本地 fallback 继续显示“本地回复”，不得错误切离线或沿用上一轮来源。
- **不得破坏**：同 session、幂等 request、购物双门控、safety_response 标签优先及 2D-only 红线。
- **验收**：静态合同锁定预算顺序 `server < browser < 5s`、等待/失败状态、来源标签和高急 CTA0。

#### S8M — 独立审查与门禁（`reviewer` read-only → `tester`）

- **reviewer**：审查 5 秒口径不是伪成功，CPA 调用预算、幂等与安全 Validator 未绕过；近时表达不放宽购物；本地文案不重复或评价人。
- **tester**：新增真实墙钟慢 Provider、连续回合调用、近时同义词和 UI 预算回归；重跑全量 pytest、固定 eval、validate、Node/static 与临时端口 HTTP smoke。
- **退出条件**：最终 `[P0/P1/P2]=0/0/0`；慢 Provider HTTP <5 秒；固定门槛不退化；重启 8000 后截图句不再追问时间，实际墙钟 <5 秒。

#### S8 实施与验收记录（2026-08-07）

- `backend` 将 `CPA_DIALOGUE_BUDGET_MAX_SECONDS` 冻结为 `3.5`，外层 `wait_for` 超时只结束当前回合，不打开或延长 transport circuit；连续两个新超时回合各自实际调用 CPA 一次，幂等 receipt 新增调用 0。网络不可达、错误模型和输出安全仍按原合同 fail-closed。
- SceneParser 将“等会儿/待会儿/一会儿/过会儿/一会就”映射 `today/high`，“马上”保持既有 `now/high`；均不澄清、购物关闭、CTA=false、Catalog=0。否定、结束、“骑在马上”“等待会计”等反例未误命中。截图句本地回复只承接一次“紧张”，并直接提供 3 个已验证衣橱方向。
- `frontend` 将 Dialogue 预算冻结为 `4500ms`，发送后立即清空并显示当前轮等待态；正常 fallback、真正安全回应和失败状态互不混淆，超时/abort 不切离线或沿用上一轮来源；cache build 为 `s8b-dialogue-4500ms-20260807`。
- `tester` 对最终树独立重跑：S8 专项 `16 passed`，安全/ID/fit/advisory/3D-video 回归 `32 passed`，全量 pytest `196 passed`；慢 CPA 真实墙钟 `3.55s`；固定评测 `overall=PASS`（Urgency `30/30`、高急 Gate `10/10`、Catalog `0/10`、幻觉 `0/515`、硬约束 `0/423`、Slot `74/74`）；validate `3/50/20/50/30`、Node、前端静态合同与临时端口 HTTP smoke 全绿，未修改 fixture、真值或阈值。
- supervisor 重启真实 8000 后回放截图句：`3.57s` 返回 `styling_active+recommend`、`today/high`、pending question none、3 套方向、Catalog0、“紧张”一次；同 session 下一新回合 `3.56s`，再次 `attempted=true` 且非 `CPA_CIRCUIT_OPEN`。远端 Grok 在 3.5 秒内仍未返回完整合约正文，因此两轮均诚实标记“本地回复”，没有伪装 CPA 成功。
- `reviewer` 最终只读复核预算、幂等、熔断隔离、时间反例、购物门控、前端等待/失败/来源、真实墙钟测试和文档一致性，关闭初审文档 P1，最终签收 `[P0/P1/P2]=0/0/0`。

### S9 — 等待 CPA 完整生成并测量真实耗时（2026-08-07，已关闭）

用户进一步澄清：不要 provisional、本地先答或后台替换；要让当前 HTTP 回合等待 CPA 完整生成并通过服务端校验后一次性返回，再观察真实耗时。该最新要求覆盖 S8 的“5 秒内可见回复”优先级。上一版后台轮询提案在实现前撤销，不新增 pending 状态或轮询端点。

#### S9A — 同步 CPA 完整回复（委托 `backend`）

- **目标**：每个新、非幂等 `/dialogue/turn` 同步等待唯一一次 CPA 调用完成；服务端 Dialogue 等待上限恢复为 120 秒，与现有 CPA transport timeout 对齐。只有 CPA 正文通过完整 Schema/action/control/advisory/购物/ID/人物医疗/fit/3D-video Validator 后才返回 `ok/cpa`；真实网络、超时、错模型或输出拒绝才返回本地 fallback。
- **测量**：Trace 记录受控 `latency_ms`，supervisor 以客户端墙钟独立计时，不用 Provider 自报替代。幂等 receipt 仍为 0 次调用；业务超时不跨回合熔断。
- **不得破坏**：权威 Scene、推荐、购物门控、ID、Memory 与安全规则均由本地控制；“等会儿”等 S8 时间修复保留。

#### S9B — 浏览器长等待态（委托 `frontend`）

- **目标**：浏览器 Dialogue 预算设为 125 秒，高于服务端 120 秒；等待期间显示“CPA 正在生成”及经过秒数，不先插入本地 assistant 回复，不追加重复消息。成功后一次性显示“CPA 生成”；真实最终失败才显示“本地回复”。
- **不得破坏**：输入立即清空、同 session/request 幂等、失败不切离线、safety_response 优先、购物双门控和 2D-only。

#### S9M — 独立审查与门禁（`reviewer` read-only → `tester`）

- **reviewer/tester**：核对没有 pending/轮询死代码；同步预算顺序为 server 120s < browser 125s；真实 CPA 成功才标 CPA；固定安全门槛、全量评测与 HTTP smoke 不退化。
- **退出条件**：重启 8000 后“你好”和截图约会句均等待 CPA 完整结果；记录真实墙钟、Provider latency/model/status。若 120 秒内仍失败，必须如实报告远端失败，不以本地回复冒充模型成功。

#### S9 实施记录

- 服务端同步预算恢复为 120 秒，浏览器预算为 125 秒；唯一 `/dialogue/turn` 等待完整 CPA 输出通过全量 Validator 后才返回 `ok/cpa`，无 provisional、pending、后台替换或轮询端点。
- 前端等待期间显示经过秒数并执行全入口防重入；reset/new task 取消旧请求并以 generation token 阻断旧响应。严格 CPA 正文不再被前端二次改写；正式回复到达立即结束等待，Trace 独立 best-effort 加载。
- 初审的重复提交/旧响应竞态、CPA 正文来源错标与 Trace 延长等待共 2 项 P1、1 项 P2 均回派关闭；S10 最终树保留全部 S9 回归。

### S10 — CPA 当前别名迁移与真实计时（2026-08-10，本地验收已关闭）

用户已修改本机 CPA 参数，并授权项目按当前配置调整。supervisor 只读探测确认：CPA `/models` 当前只暴露客户端 ID `grok-4.5-high`；`oauth-model-alias.xai` 将上游 `grok-4.5` 映射为该别名；最小真实调用返回 HTTP 200、`model=grok-4.5-build`。项目继续使用逻辑产品名 `grok4.5`，但 transport 参数必须迁移为 `grok-4.5-high`，精确回报 allowlist 冻结为 `{grok-4.5-high, grok-4.5-build}`，不得接受任意前缀或自动切换到其他模型。

#### S10A — 运行参数与类型合同迁移（委托 `backend`）

- **目标**：统一 Provider、API 类型、Trace/version、Vision/Static2D 与评测版本中的 transport ID；更新所有受影响测试，确保 Health 能识别当前 CPA 广告，真实回复仍须以 `grok-4.5-build` 精确验证后才标 `ok/cpa`。
- **不得破坏**：120/125 秒同步等待、全量输出 Validator、购物双门控、ID/硬约束、Memory、2D-only 与所有 S7/S8/S9 回归；逻辑模型仍为 `grok4.5`。
- **对应 AC**：AC-01/04/05/10/11/16/17，OBS-03/05、SAFE-08。

#### S10M — 独立审查、回归与真实 CPA 计时（`reviewer` read-only → `tester` → `supervisor`）

- **reviewer**：核对不存在旧 transport 漏网、前缀 allowlist 或 `grok-4.5-high` 之外的自动选模；安全边界不因参数迁移放宽。
- **tester**：全量 pytest、固定 eval/validate、Node/runtime/static、临时端口 HTTP；不修改 fixture、真值或阈值。
- **supervisor**：安全重启 8000，依次回放“你好”和截图约会句；记录墙钟、Trace latency、requested/transport/resolved/status，并在真实证据后同步 README、PRD 0.3、API Contract、AC Matrix 与本日志。

#### S10 实施与验收记录（2026-08-10）

- `backend` 将 Provider、Pydantic 类型、Health、Dialogue、Vision、Static2D、Look、Trace 与 eval metadata 统一迁移到 logical `grok4.5`、transport `grok-4.5-high`、reported allowlist `{grok-4.5-high,grok-4.5-build}`；目录 Health 只认精确 high，build 只作为真实调用回报。旧 transport 和前缀只保留为明确拒绝负例。
- `frontend` 同步迁移 Dialogue/Health/Static2D exact-model 判定；初审发现并关闭一个旧 transport 导致所有 high 回复被拒绝的 P0。历史 supervisor probe 的旧 transport P1 也已迁移，成功证据使用 high/build，旧值只作 mismatch 负例。
- `tester` 最终独立重跑：全量 pytest `203 passed`，S10 CPA/Dialogue/Preview/Vision 专项 `51 passed`；`validate.py` 为 `3/50/20/50/30`；固定评测 `overall=PASS`，Urgency `30/30`、高急 Gate `10/10`、Catalog `0/10`、幻觉 `0/515`、硬约束 `0/423`、Slot `74/74`；Node/runtime/static、更新后的 S2 probe、临时端口 HTTP 与 diff-check 全绿。`reviewer` 最终功能签收 `[P0/P1/P2]=0/0/0`。
- supervisor 仅停止经 PID、解释器路径和命令行校验的旧 8000 进程并启动 S10。真实“你好”返回 `status=ok/source=cpa`，墙钟 `15.970s`、Provider `15,912.02ms`；真实“等会儿要约会了，怎么办，好紧张”返回 `status=ok/source=cpa`，墙钟 `38.057s`、Provider `38,014.74ms`。两次均为 `requested=grok4.5/transport=grok-4.5-high/resolved=grok-4.5-build/model_verified=true`。
- 约会句权威 Scene 为 `event_horizon=today/urgency=high/shopping=false/CTA=false`，3 套方向全部 grounded 且硬约束通过，Catalog `attempted=false/call_count=0`，“紧张”仅承接一次。完成真实对话后 Health 为 `status=ok/available=true/chat_model_verified=true`。
- 完整 `Scene→Recommend→Look→Static2D` 真实调用使用 high transport，墙钟 `0.229s` 返回诚实 `degraded`、无 image URL、identity=`not_assessed`；直接 CPA 能力响应为 HTTP 400，明确该模型不支持 `/images/generations`。实现没有自动切换到代理建议的其他图像模型。
- 证据写入 `reports/demo/s10_real_cpa_latency.json` 与 `.md`。本地实现、测试和 Codex reviewer 已关闭；用户要求的最终 Grok + Codex 增量相互验证尚需按 `AGENTS.md` 先确认本次外部审查模型 ID，不得提前宣称联合审查完成。

### S11 — 上线架构决策：云端自托管模型、专用 Grok 2D 与持久化记忆（2026-08-10，已更新决策、未实施）

本阶段仅记录用户确认的后续上线方向和只读技术判断，不改变已关闭的 R1 DoD，不代表这些能力已经上线。任何实现均须另立里程碑、合同、迁移方案和独立评测；3D/360°/视频、真实支付及其他既有 R2+ 红线保持不变。

#### S11A — 云端自托管开放权重模型路线（后续委托 `backend`，`tester` 建立对照评测）

- **目标**：全部推理部署在云端，避免占用开发电脑 GPU/内存，同时降低当前真实 CPA 对话约 `15.970s–38.057s` 的延迟和稳定调用成本；保留 CPA 在复杂、多义、长对话和低置信度场景中的质量兜底，不以模型替代确定性购物门控、ID 白名单、HardFilter、Memory commit 或输出 Validator。
- **部署边界**：移动端与网页端只调用 ProfAgent 服务端；开放权重模型运行在独立 Linux GPU 推理服务，通过私网 OpenAI-compatible API 接入。开发电脑不承载生产推理，不将权重打包到移动端。首版使用常驻实例避免冷启动，模型服务与 API、PostgreSQL/Redis/对象存储分离，并设置健康检查、最小/最大副本、请求队列和成本告警。
- **候选路线（2026-08-12 复核）**：第一轮主测 2026 年 7 月发布的原生多模态 `google/gemma-4-26B-A4B-it`（25.2B 总参数、约 3.8B 激活）作为速度/质量主候选；以 `google/gemma-4-31B-it` 作为同系列质量上限，以 `Qwen/Qwen3.6-27B-FP8` 作为中文 Stylist 对话与视觉理解控制组，并可用 `google/gemma-4-12B-it` 测最低可接受成本。`Qwen3.6-35B-A3B-FP8` 降为历史参考，不再作为默认首测；`Mistral-Small-4-119B-2603-NVFP4` 虽为原生多模态且支持 JSON/工具调用，但官方部署体量和双卡要求显著更高，仅在前三者未达标时进入第二轮；`MiniCPM-V-4.6` 虽更新且极低成本，但其约 1B 规模不足以直接承担本项目的长期中文人格对话，只可作为视觉侧实验下界。所有生产候选必须保留原生视觉能力，未取得本项目真实延迟、质量和安全证据前不得冻结唯一生产模型。
- **服务方式**：生产优先使用 vLLM 暴露 OpenAI-compatible 多模态 API，明确禁止 `--language-model-only`；纯文本请求只是不附带图片，服务本身始终保留视觉编码器。交互对话优先采用非思考模式、8K–16K 受控上下文、受控图片像素/数量、受控最大输出和 JSON Schema；云端硬件从单张 80GB GPU 的安全基线起测，再按实际显存和 P95 结果评估 48GB GPU 降本，不用官方 262K 上下文示例反推本项目必须多卡。
- **分层职责**：规则层继续权威处理安全和业务门控；云端多模态模型承担 Scene/意图/轻情绪承接/槽位补全/结构化 JSON/普通 Stylist 对话/Memory proposal，并理解衣橱服装图、用户授权的人物/穿搭图、Look Sheet 和生成结果，用于属性抽取、视觉问答与生成后核验。图片中的物品身份仍由服务端资产 ID 与白名单绑定，视觉推断不得创造或替换 ID；模型只负责理解，不负责最终生图。输出低置信度、超时、合同失败或涉及复杂情绪和高歧义时才路由 CPA。
- **不得破坏**：云端开放权重模型与 CPA 均须通过同一服务端全量 Validator；不得因追求低延迟放宽购物门控、人物评价、ID、硬约束、敏感记忆或 3D/video 边界；模型失败必须走确定性安全路径或明确 CPA 兜底，不得伪称任何 Provider 成功。
- **对应 AC**：AC-01/03/04/05/10/11/12/16/17，OBS-03/05，SAFE-04/08。
- **进入实现前验收口径**：在固定 30 条评测外新增至少 100 条真实中文对话、结构化合同、长短会话和情绪样例，并新增衣物单图、多件 Look Sheet、授权人物图和生成结果核验的多模态真值集；同一云区、同一输入/输出/图片上限对全部候选报告首 token、文本与视觉请求各自的 P50/P95、吞吐、单次与月度成本、JSON 合同通过率、人格/情绪承接盲评和安全指标。目标纯文本对话端到端 P95 `<=5s`；多模态请求单独报告并在实测后冻结门槛，不以更慢视觉请求稀释文本指标；JSON 合同通过率 `>=99%`，并保持高急 Shopping Gate `100%`、Catalog `0`、衣物 ID 幻觉 `0`、硬约束违反 `0`。只有对照评测证明质量与安全达到门槛后，才允许提高云端开放权重模型路由占比；微调/LoRA 排在基线评测之后。

#### S11B — 专用 Grok 2D 双资产链路（后续委托 `backend` + `frontend`，`reviewer` 审查身份与资产边界）

- **用户确认**：用户已有 Grok 订阅并希望直接使用其生图能力。产品接入时仍须确认 CPA 或 xAI API 实际开放的图片 endpoint、图片模型 ID、授权与计费；消费者订阅额度不得未经验证就当作可自动化调用的 API 权限。图片 Provider 必须独立配置和精确验证，不再把文本 transport `grok-4.5-high` 发送到 `/images/generations`。
- **资产初始化目标**：衣橱导入时将原始服装图去背景、裁切并标准化为透明服装资产，必要时使用 Grok 图片编辑统一角度、光照和背景，但不得凭文本重造并改变颜色、印花、材质或结构；用户授权后生成并由用户确认稳定的虚拟人物形象。数据库只保存 `garment_asset_id/avatar_version_id`、来源、版本、授权、状态和对象存储 URL，图片二进制进入对象存储而非关系库大字段。
- **按 Look 生成两种输出**：① 使用透明白名单服装资产进行确定性排版，生成可核对 ID、数量和颜色的统一套装图/Look Sheet；② 将已确认 Avatar 与 Look Sheet 作为参考图交给专用 Grok 图片编辑 Provider，生成上身效果图。多件衣物先合成为一张 Look Sheet，再与 Avatar 一起送入图片编辑，避免参考图数量限制和逐件编辑漂移。
- **产品边界**：确定性套装图是商品/衣橱事实呈现；上身效果图只能标记为 `visualization_only`，不得宣称精确尺码、面料垂坠、身体变化或真实合身度。用户照片和 Avatar 必须有显式授权、版本、TTL/删除与访问控制；评分继续只评价穿搭，不评价人。
- **运行方式**：图片生成使用独立异步任务、幂等 job、对象存储和完成通知，不阻塞 `/dialogue/turn`；失败回退到确定性套装图/衣物卡，不静默切换未经授权的图片模型。高急切度仍不得因此出现购物 CTA，2D 生图不得扩展成 3D/360°/视频。
- **对应 AC/安全边界**：AC-10/11/13/15/16/17，SAFE-01/03/04/08，OBS-03/05；属于实验性 R2 2D 纵切，不计入当前 R1 完成门槛。
- **进入实现前验收口径**：每张套装图全部 item ID 来自当前 Look；生成图逐件核对颜色/类别/关键结构且无额外衣物；Avatar 未被替换、明显瘦身/增高或身份漂移；生成失败可回退；资产删除可传播；Trace 只记录受控 ID/版本/耗时/错误码，不记录原始人像或图片正文。

#### S11C — PostgreSQL 真值 + RRF 软记忆召回（后续委托 `backend`，`tester` 建立 Memory eval）

- **保持不变的持久化架构**：PostgreSQL 继续作为用户、会话、Look 版本、Memory `propose→confirm→commit`、撤回/删除、授权和审计的唯一真值；对象存储保存图片；Redis 可用于任务、缓存和锁；知识图谱仅作为已确认关系的可重建投影，不能解决进程重启，也不能成为唯一记忆库。
- **只读参考结论**：邻接项目 `personalized-shopping-copilot` 已实现 `BGE-M3 Dense + BM25 + Rule → weighted RRF(k=60) → Cross-Encoder/MMR`，RRF 分数为 `sum(weight_i / (k + rank_i))` 并保留各分支排名。其既有 20 条对照显示 Dense 整体优于 Rule、但 Rule 在部分查询获胜，证明多路互补；32 条 formal RRF 消融的部分最佳值为 `Precision@3=0.4896`、`MRR=0.7349`、`Recall@20=0.6927`。由于数据集和标注不同，这些数字只支持架构候选，不得直接宣称 ProfAgent Memory 已提升。
- **硬记忆不进 RRF**：已确认禁忌、敏感授权、拒绝且不得重复的建议、删除/过期/superseded 状态及其他安全关键事实必须先通过 PostgreSQL 精确过滤和直接加载，召回排序不得漏掉、恢复或覆盖它们。
- **软记忆使用 RRF**：仅对已按 `user_id + confirmed + 未删除 + 未过期 + sensitivity ACL` 过滤后的情景/偏好/反馈摘要并行执行 Dense 语义召回、BM25 精确词召回、Recency 与 Importance 排名，再以 weighted RRF 融合；融合候选可用轻量 Cross-Encoder 重排后选 Top 3–5 注入 Stylist。初始 `k=60` 与权重只作可复现实验基线，必须用 ProfAgent 自有评测调优。
- **为什么选择 RRF**：它按名次融合异构检索器，不要求直接比较 BM25、向量相似度、时间和重要性分数的数值尺度；但 RRF 不是向量数据库、不是持久化层，也不负责事务、授权、删除或硬约束。
- **对应 AC/安全边界**：AC-12/13/14/17，MEM-01/02/03，SAFE-04/08，OBS-01/03/05。
- **进入实现前验收口径**：建立 ProfAgent Memory 专用真值集并比较 `Dense-only`、`BM25-only`、`Dense+BM25 RRF`、`RRF+rerank`；至少报告 Recall@5/10、MRR、过期/撤回污染率、敏感未确认泄漏率、硬记忆漏召回、拒绝建议重复率和 P95 延迟。硬记忆漏召回、未授权/已删除记忆返回和拒绝建议重复均必须为 `0`，否则不得上线。

#### S11 退出状态

- 当前仅完成架构判断与 BUILD_LOG 更新，**没有**新增云端模型服务、Grok 图片 Provider、异步图片任务、PostgreSQL/Redis/对象存储、向量索引、RRF Memory 或知识图谱生产实现。
- 后续开始编码前，supervisor 必须先更新 PRD/API Contract/数据迁移与威胁模型，并按 `backend`/`frontend` → `reviewer` → `tester` 顺序执行；不能使用本节计划反向宣称 GitHub 当前 R1 Demo 已具备移动端持久化或真实上身试穿。

### S12 — 专用 CPA Grok Image、衣橱 2D 资产与持久记忆检索（2026-08-18，本地验收通过）

本阶段是用户明确授权的实验性 R2 2D 与移动端前置基础设施纵切，不改变 R1 已关闭的 DoD，也不引入 3D/360°/视频。未上线个人开发阶段继续使用 CPA；云端自托管模型仅在真实客户量、稳定 SLA 与成本数据证明必要时另立项目。

#### S12-0 — 合同冻结与基线审计（`supervisor`）

- **目标**：复核 PRD 6/9/11/12/16/23、现有 Static2D/Memory/资产生命周期、CPA 图片模型能力和 50 件 fixture 衣橱；冻结后端/前端边界、图片清单、RRF 权重与验收命令。
- **委托对象**：`supervisor`；合同冻结后才允许 `backend` 与 `frontend` 并行，`reviewer` 全程只读。
- **对应 AC**：AC-07/09/10/11/12/13/14/15/16/17/19；MEM-01/02/03、SAFE-01/03/04/08、OBS-03/05。
- **验收口径**：不复用文本 transport 生图；不改 fixture ID/真值/阈值；硬记忆与软记忆路径明确分离；所有图片标记 AI 生成且只承诺风格参考。

#### S12A — 专用 CPA 图片 Provider 与衣橱资产 API（委托 `backend`）

- **目标**：将图片链路迁移到 CPA `/images/generations` 的独立 `grok-imagine-image-quality`；支持 URL/base64 两种受控响应、有回报时精确模型验证、无回报时 request-bound CPA trace 验证、静态 PNG/JPEG/WebP 验证、幂等请求、超时/熔断/降级、Trace 无 prompt/图片正文/API key/原始 CPA trace；为 fixture garment 增加可追踪 AI 目录图清单和 owner-bound 读取。
- **硬规则**：图片模型与文本 `grok4.5 → grok-4.5-high` 完全解耦；不得把人物照片发入本轮衣橱目录图生成；不得生成或引用白名单外 garment ID；高急购物门控、2D-only、身份/身体尊重边界不变。
- **对应 AC**：AC-10/11/13/15/16/17/19，SAFE-01/03/04/08，OBS-03/05。
- **验收口径**：成功必须真实返回且通过完整图片 Validator；错误模型/前缀、动画、超限、坏 MIME、网络/超时均诚实降级；衣橱清单每个资产绑定既有 `garment_id`、固定请求模型、actual-model 回报状态、prompt-hash 留存状态、内容 hash、状态与 AI label，不得补造未保存的原始 prompt hash。

#### S12B — 衣橱 2D 展示与生成状态（委托 `frontend`，与 S12A 并行）

- **目标**：衣橱卡片优先显示服务端已验证的对应目录图，明确“AI 生成目录参考”；缺图/生成失败继续显示现有元数据卡；Static2D 状态与图片 Provider 来源诚实展示。
- **硬规则**：只消费服务端 owner-bound URL 和现有 garment ID；DOM 不出现 3D/360°/视频；高急切度没有任何购物 CTA；不得把目录图宣称为用户真实服装照片或真实上身效果。
- **对应 AC**：AC-10/11/15/16/19，WRD-01/02/04/07/08，SAFE-03/08。
- **验收口径**：50 件 fixture 有图时稳定映射，无图时零破坏降级；跨用户/未知 ID 不展示；缓存版本更新，Node/runtime/static 合同全绿。

#### S12C — PostgreSQL-ready 硬记忆 + Weighted RRF 软记忆（委托 `backend`）

- **目标**：将 Memory 存储抽象为持久仓储，生产配置使用 PostgreSQL，测试/离线可用等价事务仓储；`propose→confirm→commit`、删除/过期/superseded/授权状态跨服务实例可恢复。硬记忆按 `user_id + confirmed + 未删除 + 未过期 + sensitivity ACL + namespace/member/team 可见性` 精确读取，绝不进入 RRF；软记忆在同一预过滤后并行 BM25、Dense、Recency、Importance，以 weighted RRF 融合并经确定性轻量 reranker 后注入 Top 5。Demo 的 Dense 分支是可复现的 deterministic hashed surrogate，只验证融合和降级合同；真实语义 embedding/Cross-Encoder 仍未上线，后续可按邻接项目的 BGE 适配器替换。
- **初始实验参数**：`rrf_k=60`、候选池 `20`、`bm25=1.0`、`dense=1.0`、`recency=0.75`、`importance=1.25`、最终 `top_k=5`；这些是可复现基线而非已优化结论，后续只能通过 ProfAgent Memory 真值集调整。
- **硬规则**：禁忌、敏感授权、不穿某衣服、拒绝且不得重复、删除/过期/superseded 等安全关键事实必须直接从真值仓储读取；RRF 不得漏掉、恢复或覆盖；敏感默认不写，未确认不召回，Trace 不含正文/向量/原始敏感值。
- **对应 AC**：AC-07/09/12/13/14/17，MEM-01/02/03，SAFE-04/08，OBS-01/03/05。
- **验收口径**：跨实例重启后 confirmed 记录仍存在且未确认/已删/过期/越权记录为 0；硬记忆漏召回=0、拒绝建议重复=0、敏感泄漏=0；报告 BM25-only、Dense-only、RRF、RRF+rerank 的 Recall@5/10、MRR 与 P95，不宣称未测提升。

#### S12D — 50 件 fixture 衣橱目录图生成（`supervisor` 使用 `$grok-image`，S12A 合同冻结后执行）

- **目标**：调用 CPA 中的 `grok-imagine-image-quality`，按 `garment_id` 与结构化 fixture 元数据为 50 件现有衣物生成统一 1:1 中性背景目录图；写入版本化资产目录与 manifest，供后续 Look Sheet/上身效果图使用。
- **硬规则**：不覆盖 fixture，不伪造新的 garment ID，不含真人/Logo/文字/水印/额外服装，不以图像推断改写颜色、材质、版型或状态；失败可重试且可断点续跑。
- **对应 AC**：AC-10/15/16/19，WRD-01/05/07，SAFE-03/08。
- **验收口径**：manifest 50/50 可追溯；逐文件可解码、静态、尺寸/大小合法、SHA-256 唯一记录；50 件全量视觉复核 fixture name/color/slot、单件与中性背景，类别或颜色明显错配必须重生或标记失败。任何失败项保留失败状态，不以占位图冒充成功。

#### S12M — 只读审查、独立测试与文档收口（`reviewer` → `tester` → `supervisor`）

- **reviewer**：逐里程碑检查图片模型解耦、ID/owner/隐私、硬记忆不经 RRF、软记忆 ACL/删除/过期、3D/video 红线，输出 `[P0]/[P1]/[P2]`；发现回派 owner，绝不放宽规则。
- **tester**：在功能冻结后串行使用 `conda run -n torch128 ...` 跑新增图片/Memory 专项、全量 pytest、validate、固定 eval、Node/runtime/static、临时端口 HTTP；输出 JSON+MD 报告，不改真值和阈值。
- **supervisor**：核验衣橱 manifest 与真实 CPA 图片证据，同步 README、PRD 0.3、API Contract、AC Matrix 与 BUILD_LOG；只有全绿才关闭 S12。外部 Grok 对抗复核如需 `$call-grok`，仍按 AGENTS.md 先向用户确认本次审查模型 ID。

#### S12R — 真实 CPA 协议回报修正（`backend` + `frontend` → `reviewer` → `tester`）

- **触发证据**：50 件批处理和 mock 合同通过后，supervisor 直接用项目 `GrokImageProvider` 发起真实请求。CPA 返回 HTTP 200、`b64_json`与 `x-cpa-trace-id`，但顶层只有 `created/data/usage`，无 `model`；旧实现因此误降级为 `CPA_IMAGE_MODEL_VERIFICATION_FAILED`。
- **修正目标**：不伪造 actual/resolved model；有模型回报时继续 exact fail-closed，无回报时仅在 exact request + CPA trace + 全图 Validator 成立时接受，并在 API/UI/文件元数据诚实标记“请求模型已固定，实际模型未回报”。
- **不得破坏**：错模型/前缀仍拒绝；无 trace 仍拒绝；SSRF/DoS、owner/ID、PNG provenance、高急购物、2D-only 和 Memory 全部门禁不放宽。
- **验收口径**：真实项目 Provider 一次返回可解码图片；API 不声称 `model_verified=true`；UI 显示请求固定/未回报局限；50 件目录图 manifest/内嵌元数据同步诚实迁移；专项、full、Node、HTTP、真实 smoke 全绿。

#### S12/S12R 执行与验收记录（2026-08-18）

- **S12A / S12R backend**：图片与文本 Provider 完全隔离；运行时图片成功分为 `reported_model_exact` 与 `exact_request_with_cpa_trace`，两类都要求 exact request、受控单值 receipt 和全图 Validator。缺失模型的真实路径保持 `model_reported/model_verified=false`、`resolved_model=null`；错模/前缀、缺或非法 receipt、重复 JSON key、多图片体、超限/动画/坏 MIME 全 fail-closed。CPA envelope 8 MiB 与 allowlist URL 5 MiB 均流式硬中止；raw receipt、prompt、正文、base64、API key 不进入 API/Health/Trace。Backend 最终专项 `51 passed`、全量候选 `242 passed`，reviewer 功能签收 `0/0/0`。
- **S12B frontend**：Static2D 精确区分 reported/unreported 两类成功；Catalog 使用独立 `batch_exact_request_contract`，不伪造逐图 receipt 或 actual model。目录 URL 强制 `user_id + asset_version=wardrobe_generated_v1_s12r2 + content_sha256`，旧版本、旧二参数、错 hash、跨 owner 或来源字段混搭均不展示；未回报时显示“请求模型已固定，CPA 未回报实际模型”。Node syntax/runtime/static 与购物双门控、single dialogue POST、2D-only 全绿。
- **S12C Memory**：默认 SQLite 文件仓储可跨进程/实例恢复，生产可选 PostgreSQL；proposal/record 持久化 team/member/visibility ACL，SQLite `BEGIN IMMEDIATE` 与 PostgreSQL `FOR UPDATE` 保证 confirm CAS。硬记忆按 SQL/ACL 精确读取且 `hard_memory_in_rrf=false`；软记忆在全体预过滤后，各取 BM25、`deterministic_hashed_surrogate_v1`、Recency、Importance Top 20 并集，以 `k=60`、权重 `1/1/.75/1.25` 融合并轻量重排 Top 5。受控 synthetic 消融只证明管线可复现，不宣称真实语义提升。
- **S12D 资产**：50 件 fixture 对应 50 张 1024×1024 单帧 PNG；ID/hash 各 50 唯一，owner/fixture/bytes/dimensions/hash 全一致。reviewer 逐件按 name/color/slot 复核并关闭首轮类别错配。S12R 无损迁移为 schema 2 六个内嵌来源键；当前 50 条均诚实记录 `prompt_sha256=null/prompt_hash_status=not_preserved`，不使用当前模板冒充原生成 prompt。
- **真实 CPA Image 证据**：项目 `GrokImageProvider` 在 `torch128` 中一次真实调用成功，`5,288.5ms` 返回 `image/jpeg`、228,841 bytes、`b64_json`；结果为 `request_model_pinned=true`、`cpa_trace_verified=true`、`model_reported/model_verified=false`、`resolved_model=null`、`verification_basis=exact_request_with_cpa_trace`。报告见 `reports/demo/s12r_real_image_provider.{json,md}`，不包含 raw receipt、图片正文、prompt 或凭据。
- **最终独立门禁**：`conda run --no-capture-output -n torch128 python scripts/tester_s12_report.py` exit 0；该命令真实串行执行并原子生成 schema 2 报告，pending/失败必为非零。最终 pytest `246 passed`、S12 image/memory `21 passed`、Preview `34 passed`；Urgency `30/30`、高急 Gate `10/10`、Catalog `0/10`、幻觉 `0/515`、硬约束 `0/423`、Slots `74/74`；validate `3/50/20/50/30`、Node 6 syntax + 3 runtime、随机非 8000 HTTP、50 资产、固定数据 diff、secret/path/privacy 与 `.profagent` hygiene 全绿。报告见 `reports/eval/s12_memory_image_v1.{json,md}`。
- **对抗审查关闭矩阵**：URL DNS/缓冲 DoS、Memory ACL/CAS、PNG 内嵌 provenance、视觉错绑、真实协议无 `model`、receipt 约束、误导性 `X-AI-Model`、复用 prompt hash 失真、旧缓存 URL、重复 JSON key、旧 tester 合同及 pending 也写 PASS 等发现均回派并关闭；最终本地 `[P0/P1/P2]=0/0/0`。3D/360°/视频、真实支付/爬取、开放真人市场、第二成员与社区流仍未上线。

### S13 — 衣橱图片呈现与 CPA Text 4.6 迁移（2026-08-18 至 2026-08-19，已关闭）

- **触发证据**：真实 8000 衣橱页拿到 `u01` 的 28 条 `ready` 资产且图片接口 HTTP 200，但前端同时设置 `loading=lazy` 与 `hidden=true`，浏览器不启动隐藏图片的懒加载，卡片永久停在“目录图正在读取”。同一运行实例的文本 Health 显示 `catalog_model_advertised=false`：项目仍请求已从 CPA 实时目录消失的 `grok-4.5-high`，因此“你好”诚实回退为本地回复。
- **S13A backend（委托 `backend`）**：根据实时目录与 supervisor 无正文真实探测，迁移文本合同为 logical `grok4.6` → transport `grok-4.6-high`，完整调用只接受精确回报 `{grok-4.6-high, grok-4.6-build}`；旧 4.5、前缀和其他模型全部 fail-closed。同步 Dialogue/Health/Vision/Look/Preview/Trace/eval 与 Pydantic，但图片 `grok-imagine-image-quality` 合同不变。
- **S13B frontend（委托 `frontend`）**：消除 hidden+lazy 加载死锁，确保通过 S12R owner/version/content-hash/provenance 验证的图片能加载并显示；缓存完成竞态和 error 仍须安全回元数据卡。同步文本 exact-model 来源合同为 4.6，旧 4.5 不得显示 CPA 正常。
- **对应 AC / 硬规则**：AC-10/11/13/15/16/17/19，OBS-03/05，SAFE-01/03/08；每个新 Dialogue 仍最多一次 CPA，120/125s、购物双门控、ID 白名单、2D-only、Memory ACL 与来源诚实性不得放宽。
- **验收口径**：真实 8000 至少一张目录 PNG 实际渲染；真实“你好”返回严格 CPA 4.6 来源或诚实失败，不得再因旧 transport 固定降级；Node/runtime/static、模型拒绝矩阵、targeted/full、固定 eval、临时 HTTP 和 reviewer 全绿。最后使用用户指定 `grok4.6` 做外部 Grok 对抗终审，Codex 对其发现逐项本地验证后收拢。
- **执行与本地验收（2026-08-19）**：backend 将生产逻辑模型、transport 与回报 allowlist 精确迁移为 `grok4.6 → grok-4.6-high → {grok-4.6-high,grok-4.6-build}`，旧 4.5 仅保留在拒绝负例；frontend 改为 eager、可布局的 pending 图片节点，并以 `naturalWidth>0`、owner/version/content-hash/provenance 共同决定成功显示，cached/error/cancel/stale 均安全回元数据卡。真实 8000 “你好”约 `3.456s` 返回 CPA `ok`、resolved `grok-4.6-build`；真实 Chrome 目录图 `naturalWidth=1024`。独立 tester 为 S13 定向 `214 passed`、全量 `252 passed`，固定 eval、validate、Node/runtime/static、临时 HTTP 和浏览器 DOM 全绿，报告见 `reports/eval/s13_text46_catalog_display_v1.{json,md}`。
- **门禁稳定性**：一次 Windows 满载全量运行曾在 0.5 秒测试注入预算下取消 80ms mock；生产 120 秒预算与独立 50ms 超时取消测试均无回归。测试成功路径预算改为 2 秒后连续 5 次定向通过，连续两轮 full 均为 `252 passed`，因此关闭为测试调度抖动而非产品缺陷。
- **Grok 4.6 + Codex 相互验证（2026-08-19）**：用户指定 Grok 4.6，CPA 实时目录解析到精确 `grok-4.6-high`。外部 Grok 建议核实图片/文本模型隔离、无后缀错模、single-flight、图片旧卡竞态、评分/调整/Look/Memory 与历史 4.5 误读。Codex 逐项映射本地代码与测试，并补充无后缀 `grok-4.6` 拒绝、图片请求体不含文本模型 ID 反断言和历史提示；定向 2 项、三套 Node 合同及最终 full `252 passed in 38.44s`。所有建议发现关闭，联合终审 `[P0/P1/P2]=0/0/0`。证据见 `reports/review/s13_grok46_external_review.md` 与 `reports/review/s13_codex_mutual_verification.md`。

### S14 — 连续衣橱搭配、推荐级 2D 与衣橱折叠（2026-08-19，已关闭）

本阶段修复真实交互中“第二轮重复首轮、已拥有衣橱却反问用户列衣服、明确要两套却不落推荐卡、输入必须点按钮”的断链。Memory 仅做方案调研与决策材料，未经用户确认不修改现有持久化、ACL、RRF 权重或写入规则。

#### S14-0 — 阶段与接口冻结（`supervisor`）

- **目标**：冻结 Enter/Shift+Enter、服务端套数识别、推荐先于对话生成、推荐级静态 2D、owner-bound 图片读取和衣橱分类折叠合同；复用现有 50/50 目录图，不重生 fixture 资产。
- **委托对象**：`supervisor`；合同冻结后 `backend` 与 `frontend` 可并行，`reviewer` 只读。
- **对应 AC**：AC-01/03/04/05/06/10/11/13/15/17/19；PREV-04/05/06/08/10/12/13/14/15；WRD-01/02/07/08。
- **硬规则**：明确“一/两/二/三套”只由服务端从当前回合解析为 1–3；不得由客户端或 CPA 放大。推荐仍只来自 owner 白名单与 HardFilter 后的衣橱；高急切度 Catalog=0、购物 CTA=0；2D 只作无身份风格/配色参考，不声称真实试穿、精确尺码、面料或垂坠。
- **验收口径**：同一 session 的“今晚辩论赛”→“帮我搭配两套”无需复述场景，返回恰好两套合法衣橱方向（若合法候选不足则诚实少于两套并给 gap），CPA 回复不得再询问用户手头有什么；每个方向可独立生成一张 owner-bound 静态 2D，失败仅降级该图。

#### S14A — 连续对话与衣橱优先推荐（委托 `backend`）

- **目标**：在 Dialogue 状态中继承已确认 Scene；识别当前轮明确套数；`action=recommend` 时先执行现有 HardFilter→Rule/BM25/Dense→RRF→Assembler→Validator，得到权威衣橱方向后，再向 CPA 发送脱敏、有界、无内部 ID 的推荐摘要与最近历史，要求回答当前增量而非复述上一轮。
- **硬规则**：每个新回合最多一次文本 CPA；CPA 不决定或改写 garment ID、套数、购物门控和推荐正文结构；Provider 输出若询问用户重新列衣橱、与权威套数冲突或高度复读上一轮，则 fail-closed 使用已验证的本地衣橱摘要。任何 ID 幻觉/硬约束违反为 0。
- **对应 AC**：AC-01/03/04/05/06/10/11/13/14/17，SAFE-08，OBS-03/05。
- **验收口径**：新增截图原句与同义词回归；第二轮 `turn_index/history_version` 连续；两套 item IDs 全部属于当前 owner 且 available、禁色/季节/天气复检通过；今晚/unknown 仍 shopping=false、Catalog0；文本 CPA 失败时仍返回同两套卡片。

#### S14B — 推荐级静态 2D 组合图（委托 `backend` + `frontend`）

- **目标**：新增 owner/session/request/outfit 绑定的批量推荐预览 API（单批最多 3，当前 UI 只请求权威推荐里的方向），复用独立 `grok-imagine-image-quality` Provider，为每个已验证 outfit 生成一张中性、无身份模特或平铺的静态 2D 组合参考；前端先显示文字/卡片，再并行显示每张图的生成、成功或诚实降级状态。
- **硬规则**：客户端不提交 garment/product ID；服务端从已保存 Recommendation 派生并重验 owner、available、Scene 硬约束和 outfit ID。禁止人物身份复刻、用户照片发送、3D/360°/视频；图片 Provider 与文本 4.6 完全隔离；响应不含 base64、prompt 或 raw CPA receipt。
- **对应 AC**：AC-10/11/13/15/16/17/19，PREV-04/05/06/08/10/12/13/14/15，SAFE-01/03/08。
- **验收口径**：两套推荐触发不超过两次图片 Provider 调用且各自有幂等 request ID；成功图 owner-bound 可读、错误/前缀模型和跨 owner 均拒绝；图片失败不删除文字推荐、不启用购物、不阻塞下一轮对话。UI 固定显示“AI 生成的 2D 视觉参考；不代表真实试穿或精确版型”。

#### S14C — 输入与衣橱折叠呈现（委托 `frontend`）

- **目标**：普通 Enter 发送，Shift+Enter 换行；中文输入法组合期间 Enter 不提交；沿用 single-flight，等待中键盘、按钮、快捷回复均不能产生第二个 POST。衣橱按服务端 slot 的受控中文类别分组，以可访问的折叠区呈现，初始只显示类别名和数量，展开后才渲染该类 owner 衣物及目录图。
- **硬规则**：不允许前端自行拼接历史、推断套数或信任未验证图片 URL；目录图仍须通过 S12R owner/version/content-hash/provenance 合同；50 件 fixture 均已有生成资产，但单个用户只展示其 owner-bound 子集。无 3D/视频入口，高急购物 CTA 双层阻断不变。
- **对应 AC**：AC-10/11/15/17/19，WRD-01/02/04/07/08，SAFE-03/08。
- **验收口径**：Enter/Shift+Enter/IME/等待期重入有可运行 Node 测试；浏览器验证折叠初始无卡片、展开后图片 `naturalWidth>0`、收起后类别仍可见；筛选后分类和数量正确，图片失败回元数据卡。

#### S14M — 个性化 Agent Memory 方案调研（`memory_research`，只读）

- **目标**：用官方文档、论文和项目仓库比较事件/画像分层记忆、向量+BM25/RRF、时序知识图谱、Mem0、Zep/Graphiti、Letta/MemGPT、LangGraph/LangMem 等路线，并对照当前 PostgreSQL-ready ACL、硬记忆 SQL 直读与软记忆 weighted RRF。
- **不得执行**：本阶段不改 Memory production、schema、向量模型、知识图谱、权重、API 或用户数据；不把外部框架宣传材料当作已验证效果。
- **对应 AC/安全边界**：AC-07/09/12/13/14/17，MEM-01/02/03，SAFE-04/08，OBS-01/03/05。
- **交付与决策门**：提交 3–4 套含架构、成本、隐私/删除、一致性、移动端重启恢复、迁移量和评测方法的方案；明确推荐起步方案。只有用户确认某方案后，才另立实现阶段。

#### S14R — 审查、独立测试与收口（`reviewer` → `tester` → `supervisor`）

- **reviewer**：逐项审查连续状态、复读/套数控制、owner/ID、图片身份与来源、Catalog 双层门控、single-flight、2D-only，输出 `[P0]/[P1]/[P2]`，只报告不改代码。
- **tester**：串行使用 `conda run -n torch128 ...` 跑新专项、全量 pytest、固定 eval、Node/runtime/static、随机非 8000 HTTP 与真实浏览器 DOM；不得改 fixture/eval 真值或阈值。
- **全绿门槛**：截图链路无需用户列衣橱且两套可演示；图片失败降级、跨 owner/错模/错 ID/3D-video 负例全绿；Urgency/Gate/Catalog0/幻觉0/硬约束0/Slots 指标不退化。README、PRD 0.3、API Contract、AC Matrix 与 BUILD_LOG 最终同步后才关闭 S14。

#### S14 执行与验收记录（2026-08-19）

- **连续推荐**：服务端从当前回合解析明确 1–3 套，并支持否定、纠正与歧义不猜；推荐先完成 owner 衣橱 HardFilter/检索/RRF/Assembler/Validator，再把无内部 ID 的权威摘要交给 CPA。截图链“今晚辩论赛”→“帮我搭两套”继承 `meeting/today`，返回恰好两套合法方向、`shopping=false`、Catalog 0；模型反问衣橱、套数冲突或高复读均 fail-closed 到同一权威本地摘要。
- **推荐级 2D**：新增 owner/session/request/outfit 绑定的 batch POST 及图片读取/删除；单批 1–3、客户端不提交 garment/product ID、每项图片调用和来源证据独立，错误模型/跨 owner/未知 outfit/3D-video 在 Provider 前或边界拒绝。文字与方向卡先显示，每图独立生成/降级。
- **输入与衣橱**：Enter 发送、Shift+Enter 换行、IME 组合态不发送，所有入口沿用 single-flight；衣橱使用可访问的类别折叠，初始卡片 0，展开 owner 图片 `naturalWidth=1024`。50/50 fixture 目录资产保持不变，`u01/u02/u03` 仅分别可见 28/12/10。
- **真实 CPA 证据**：进程内 TestClient、不监听且不触碰 8000，文本 CPA 调用 0，独立图片 CPA 两次并行调用均成功；尝试约 `7,059.1ms/6,845.4ms`，批量总墙钟约 `7,126.3ms`，JPEG 248,145/142,301 bytes、不同 SHA-256。CPA 未回报 actual model，故保持 `model_verified=false/resolved_model=null/verification_basis=exact_request_with_cpa_trace`；报告不含 prompt、raw receipt、base64、key 或图片正文。
- **reviewer 关闭项**：初审发现的“首命中吞掉套数纠正”“衣橱反问语义可绕过”“并发失败项读取兄弟共享 Provider health”，以及复审发现的“改成/最后要/那就等无否定词显式纠正未生效”四项 P1，均已回派并以否定/纠正/歧义、请求/陈述边界和并发交错逐项证据关闭；最终只读终审为 `[P0/P1/P2]=0/0/0`。
- **tester 最终门禁**：S14 专项 `26 passed`、Dialogue/Preview/购物/Memory 相关 `241 passed`、full `278 passed`；validate `3/50/20/50/30`，fixed eval Urgency `30/30`、高急 Gate `10/10`、Catalog `0/10`、幻觉 `0/515`、硬约束 `0/423`、Slots `74/74`；Node 11 syntax + 5 runtime/static、随机非 8000 mock HTTP、真实 Chrome、privacy/secret/diff 全绿。证据见 `reports/eval/s14_continuity_preview_v1.{json,md}` 与 `reports/demo/s14_real_recommendation_previews.{json,md}`。
- **Memory 决策门**：S14M 只完成调研，未修改 production/schema/RRF 权重或用户数据。推荐先采用“PostgreSQL/SQLite 事务真值 + hard SQL + soft RRF 派生索引”，LangMem、Mem0、Graphiti 仅作为后续可重建侧车；待用户确认方案后另立实现阶段。详见 `reports/research/s14_memory_options.md`。

### S15 — Memory 路线 A：事务真值、可重建索引与确定性重排（2026-08-19，已关闭）

用户已确认先采用方案 A，并确认第一版使用 **Weighted RRF + 确定性结构化 rerank，不调用额外模型**。本阶段复用 S12 已有 PostgreSQL/SQLite 仓储、ACL、事务 CAS、`propose→confirm→commit`、硬记忆 SQL 直读与记忆管理页，不重造另一套 Memory，也不接入 LangMem、Mem0、Graphiti 或 Cross-Encoder。

#### S15-0 — 合同与迁移冻结（`supervisor`）

- **目标**：冻结五层记忆语义、生命周期/来源字段、事务 outbox、派生索引重建、`structured_rerank_v1` 公式、记忆管理呈现与降级边界；先迁移旧 SQLite/PostgreSQL schema，再启用新读写路径。
- **记忆分层**：`profile_current`、`hard_constraint`、`preference_event`、`episodic_summary`、`working_context`。`working_context` 仍为 session+TTL，不冒充长期记忆；`episodic_summary` 只允许受控摘要并经用户确认，不保存原始对话或模型思维链。
- **对应 AC / 硬规则**：AC-07/09/10/13/14/17，MEM-01–12，SAFE-04/08，OBS-01/03/05；敏感默认不写，未确认/删除/过期/superseded/跨 user/team/member/namespace 为 0，硬记忆永远不进 RRF，Trace 不含正文、向量、原始来源文本或敏感值。
- **验收口径**：旧数据库幂等迁移且记录不丢；字段缺失、非法枚举、客户端伪造来源/确认次数/排序特征均 fail-closed；README/PRD/API/AC/BUILD 只在最终门禁后宣称 S15 完成。

#### S15A — 事务真值、生命周期与 outbox（委托 `backend`）

- **目标**：在现有 `memory_proposals/memory_records/memory_audit` 上增量加入 `memory_class/valid_from/valid_to/supersedes_memory_id/source_kind/provenance_version/consent_version/confirmation_count`；commit、supersede、delete、expire 与内容最小化 outbox 事件在同一事务提交。派生索引只消费已确认、非敏感、ACL 合法的记录，按 memory ID 回真值表复验。
- **硬规则**：outbox 不保存记忆正文、向量、用户原话或私有衣物 ID；失败不得留下“真值已改、事件未写”的半事务。删除/过期/superseded 立即从真值资格集合消失，即使派生索引暂时失败也不能被召回。PostgreSQL 为生产真值，SQLite 通过同一合同用于离线和测试。
- **对应 AC**：AC-07/09/13/17，MEM-01/02/03/04/05/06/07/08/09/10/11，SAFE-04，OBS-01/03/05。
- **验收口径**：故障注入证明事务原子性；跨实例重复 confirm 仍只产生一个权威 record/outbox 版本；重启后记录、授权、TTL、superseded、审计和索引版本一致；可从 SQL 真值全量重建派生索引且结果确定。

#### S15B — Soft Memory Weighted RRF + `structured_rerank_v1`（委托 `backend`）

- **召回不变**：SQL 对 user/confirmed/non-sensitive/active/ACL 做全量预过滤；BM25、`deterministic_hashed_surrogate_v1` Dense、Recency、Importance 各取 Top 20，并按 `k=60`、权重 `1.0/1.0/0.75/1.25` 做 RRF 并集。硬记忆只走 `active_signals()`。
- **重排公式**：`final = 0.60*rrf_norm + 0.15*context_match + 0.10*specificity + 0.10*confirmation_strength + 0.05*lexical_norm`。所有特征由服务端 Scene、受控记忆类别/适用标签、确认历史和 BM25 计算，范围 `[0,1]`；客户端和 CPA 均不得提交或覆盖。并列依次按 BM25、Importance、稳定 memory ID，最终最多 Top 5。
- **硬规则**：重排只改变已通过 SQL 预过滤的软记忆顺序，不能恢复硬过滤项；不调用 CPA、Cross-Encoder 或第三方 Memory；无适用证据时不得凭模型猜测 context/specificity。Trace 只记录算法版本、受控分值分桶/ID、名次、权重、候选数和耗时，不记录正文或向量。
- **对应 AC**：AC-03/04/05/07/10/13/17，MEM-04/06/11/12，OBS-01/03/05。
- **验收口径**：同 query/数据/版本结果确定；场景精确偏好优先于仅近期但无关记忆；全局偏好仍可回退；被拒绝/删除/过期/跨 ACL 污染率 0；受控消融报告包含 BM25、Dense、RRF、RRF+rerank 的 Recall@5/10、MRR、P95 和污染率，并明确合成边界。

#### S15C — 记忆管理可见性（委托 `frontend`）

- **目标**：复用现有“待确认/已提交/删除”页面，增加受控的记忆分层、命名空间、适用期、来源类型、确认强度和 superseded/expired 状态说明；working context 明确标注为“仅本次会话”，不显示为长期记忆。
- **硬规则**：前端不计算 RRF/rerank、不信任客户端缓存决定有效性、不展示 outbox 内部状态、正文以外的私有来源或 Trace；敏感/blocked 仍只显示脱敏状态。删除后立即移出可用列表，API 失败不得假装成功。
- **对应 AC**：AC-07/09/13/17，MEM-01/03/05/07/08/09，SAFE-04/08。
- **验收口径**：Node/runtime/static 与真实浏览器验证提议、确认、查看、删除、namespace、过期/替代状态；离线 fixture 明确只读；无第二成员、社区或自动共享入口。

#### S15R — 里程碑审查、独立测试与收口（`reviewer` → `tester` → `supervisor`）

- **reviewer**：每个 backend/frontend 冻结里程碑后只读审查事务原子性、迁移、ACL、敏感/删除传播、hard/soft 分流、特征可伪造性、Trace 隐私与排序污染，输出 `[P0]/[P1]/[P2]`，发现回派 owner。
- **tester**：严格串行使用 `conda run -n torch128 ...`；新增 schema/migration/outbox/rebuild/restart/cross-instance/rerank/污染/管理页测试，跑 full、固定 eval、validate、Node、随机非 8000 HTTP 与浏览器；不得改 fixture/eval 真值或放宽阈值。
- **关闭门槛**：Memory 新专项、全量与固定指标全绿；硬记忆漏召回/误恢复 0，软记忆污染 0，重启/删除/TTL/supersede/ACL 一致，RRF+rerank 的受控报告可复现；最终文档和 Demo 一致后才将 S15 标为已关闭。

#### S15 执行与验收记录（2026-08-19）

- **后端/前端实现**：在既有仓储增量加入五类语义、来源/同意/有效期/确认次数、粘性 quarantine、事务 outbox/幂等 consumer、可重建 ID-only soft projection 与 PostgreSQL/SQLite 并发头；硬记忆继续 SQL 直读。前端只接受 `committed+active` 长期记录，完整绑定 propose/confirm/edit/reject/delete 回执，未知元数据仅保留 owner-bound 脱敏删除入口。
- **重排冻结**：四路 Top 20、`k=60` 与 `1/1/.75/1.25` RRF 不变；`structured_rerank_v1` 使用 `0.60/0.15/0.10/0.10/0.05` 的 RRF/context/specificity/confirmation/lexical 权重。所有特征来自权威 Scene 与服务端受控标签；不调用 CPA、Cross-Encoder 或第三方 Memory。
- **reviewer 对抗闭环**：初审发现 mutation 回执欠绑定、未来有效期误召回、来源组合、PostgreSQL CAS/gap race、无 consumer、跨 owner payload 泄漏、quarantine 洗白和无事件陈旧投影等问题；均回派 owner 修复并逐项重放。最终 backend/frontend 只读审查 `[P0/P1/P2]=0/0/0`。
- **tester 最终门禁**：`conda run --no-capture-output -n torch128 python scripts/tester_s15_report.py` exit 0 并原子生成 `reports/eval/s15_memory_route_a_v1.{json,md}`；S15 `19 passed`、Memory 相关 `44 passed`、full `300 passed`。固定 eval Urgency `30/30`、高急 Gate `10/10`、Catalog `0/10`、幻觉 `0/515`、硬约束 `0/423`、Slots `74/74`；Node 14 syntax + 7 runtime/static、随机非 8000 HTTP、双实例/重启 SQLite、ACL/生命周期/删除回执与真实 Chrome 全绿，CPA 调用 0。
- **合成检索边界**：受控 benchmark 中 RRF+rerank 的 Recall@5/Recall@10/MRR 为 `.708333/.833333/1`；该数字仅验证确定性合同与排序方向，不代表真实用户质量或生产语义提升。

### S16 — 私人 Stylist：自由文本记忆、女装资产与图上换装（2026-08-21，已批准、实施中；S16A 已完成，S16B/S16C/S16R 未完成）

S16 复用 S15 Memory 路线 A、R1 owner/ID/HardFilter/购物门控、不可变 Look 和独立 CPA Text/Image/Vision Provider。界面身份始终为“私人 Stylist”；女装仅表示 V1 资产池范围，不推断用户性别。本阶段不接入 LangMem、Mem0、Graphiti、Cross-Encoder 或学习型 reranker，也不实现通用网页爬虫、3D、360°或视频。

#### S16-0 — 合同与迁移冻结（`supervisor`，未完成）

- **目标**：冻结 Memory 候选端点、Dialogue 可选字段、owner/session/幂等边界和 S16A/B/C/R 的依赖顺序；只做兼容性文档与迁移设计，不修改生产实现。
- **输入/输出**：消费已批准 S16 设计和 S15 API；产出后续 backend/frontend/tester 共用的唯一字段名与状态语义。
- **硬规则**：任何合同都不得把浏览器或 CPA 变成排名、追问预算、衣物 ID、购物、记忆写入或 Look truth 的权威；S16 当前不得宣称已上线。
- **验收口径**：合同文档互相一致，旧 S15/R1 门槛原文保留，文档检查与 `git diff --check` 为绿。

#### S16A — 自由文本记忆与偏好不确定性（`backend` + `frontend` → `reviewer` → `tester`，已完成并关闭）

- **目标**：实现自由文本拆分候选、逐条 `remember|session_only|reject|rephrase`、冲突确认和 session-bound working context；多套合法候选接近且缺少关键偏好时最多提出一个非阻断问题。
- **对应 AC / 硬规则**：AC-01/03/04/05/06/07/10/11/14/16，MEM-01–12，SAFE-04/08，OBS-05；敏感默认不写，长期记忆仍走 `propose→confirm→commit`，硬记忆永远不进 RRF，高急切度 `shopping_allowed=false` 且 Catalog 调用为 0，ID 幻觉/硬约束违反/人物评分均为 0。
- **输入/输出依赖**：消费 S16-0 冻结合同及 S15 SQL/outbox/RRF；产出候选确认卡、working context 和由服务端拥有的排序/问题预算证据。
- **验收口径**：原始自由文本、原始对话、模型推理/正文、敏感值和直接标识符不得进入长期 Memory、RRF、outbox、Trace、Debug DOM 或 browser storage；候选卡仅含服务端生成的脱敏闭集字段。高急最多问一次且不阻塞合法推荐；拒绝后不换说法重复。
- **Task8 权威性修复边界（2026-08-22）**：canonical bundle 是唯一权威，固定 JSON/MD 仅为 projection；只有 bundle hash、双 projection hash 和 source/tree/command-plan provenance 全部匹配才可读取 authoritative PASS。修复代码 clean 提交后才能运行全套 runner 并另行提交 artifacts；后续只读 review 前 finding 保持 pending/unknown。
- **Task8 finalization 关闭证据（2026-08-23）**：clean source `72903b5f953dc4aabd825b27f9ea31a282708ad5`、artifact-only reviewed head `6d9ae2f8f5b5837951e1daebb98d19eeccd37677`；fixed package SHA-256 `67e16e2fc23f4ddf173a682fdb075a2b3198df1e39cc3bc45b9597f958c3c606`，fresh broad review SHA-256 `b5b3cc8bca38e11e572cdcd500b805ca9f3cc22456966cd55c4dc144b0592825`，reviewer `PASS` 且 `P0/P1/P2=0/0/0`。真实 atomic runner：focused `5`、Memory `51`、Dialogue `128`、CPA closed-envelope `43`、full pytest `450`、Node runtime/static `12` 与 syntax `21` 全绿；固定 R1 为 Urgency `30/30`、高急 Gate `10/10`、Catalog `0/10`、幻觉 `0/515`、硬约束 `0/423`、Slots `74/74`，external provider calls `0`。S16A/Task8 到此关闭；S16B、S16C、S16R 仍未完成、未上线，S17+ 能力仍未上线。

#### S16B — 女装 V1 资产与授权来源（`backend` + `frontend` → `reviewer` → `tester`，未完成）

- **目标**：在保留 `fixtures_v1.0` 50 件稳定 ID 的前提下扩充至总计 120 件（`u01=72/u02=24/u03=24`），提供 owner-bound、来源明确的 ready 2D 资产和按类别折叠展示。
- **范围边界**：只允许用户自有/明确授权、机器可读许可 API、正式合作 API 或明确标记的 AI 生成参考；不开放通用爬虫，不用许可不明图片凑数。
- **验收口径**：120/120 资产可展示、来源和受众闭集可审计，旧 ID 不重编号，Overlay 启用后 R1 固定评测不退化。
- **Task 4 / Ruling I 关闭证据（2026-08-24）**：CPA 女装目录图的失败诊断与私有隔离合同已完成 mock-only TDD 和 fresh reviewer 收口；最终实现 `4468cb7`，licensed-assets `383 passed`、full `869 passed, 1 skipped`、两项 validator 通过，fresh reviewer `P0/P1/P2=0/0/0`。隔离图不进入公开 manifest/source/静态服务，Provider/local 失败不伪造资产；所有 existing diagnostic（含 dry-run）在 Provider 前重验 canonical garment/user/prompt/product/private hash 与服务端模型策略。首轮真实 g051 失败证据继续保持 untracked 且哈希冻结；S16B 仍未完成。
- **Ruling J 第二次真实 g051 canary（2026-08-24，已执行并安全停止）**：冻结命令仅执行一次，实际发生一次 CPA Image 与一次 CPA Vision 调用，随后立即停止；没有 retry、resume、g052–g120、Openverse、批量或 manual promote。g051 继续为公开 `quarantined`，`license/relative_path/accepted hashes/receipt_history` 均为空，source ledger 仍无 g051。私有隔离 PNG 与诊断分别冻结为 SHA-256 `46c5d710493b59daa823f72d9c68ab18d059e313ead69531c4cb70c2058a4e98`、`12f59ef81ff67befe4a3614c1da0d090bc25ba7566c5ea7a6c1c44be88d00d00`；公开 manifest/source 哈希为 `6e71d9ca9cd3e5e9c7a037c56498303ece03021179505dfd21ed2a5c218f4682`、`eca7fd576e10853388cb2de624698b06bb8a49bc8cb443e11e63118599ef19e2`。人工只确认图片外观符合单件女装目录图要求，不能覆盖 Vision/schema 失败或手动晋升为 ready。
- **Ruling K 第二次 canary 的 Vision schema 修复（2026-08-24，mock-only 已关闭）**：实际 canary reviewer 先报 `P0/P1/P2=0/1/0`，指出 CPA 标准 envelope/content/payload 无受控分层；测试 `c845b19` 与实现 `aab57a3` 增加 `envelope|content|payload` 闭集、标准小型 envelope 白名单及单一完整 JSON/fence 解析。首轮 fresh review 又发现 allowed metadata 只控键名、不控语义，仍为 `0/1/0`；测试 `da9b7a8` 与实现 `9476cca` 进一步要求 `index=0`、`finish_reason=stop`、`role=assistant`、`object=chat.completion`，拒绝 `usage` 并限制其余元数据类型/长度。replacement reviewer 最终 `PASS`，`P0/P1/P2=0/0/0`，focused `13 passed`、licensed-assets `393 passed`；post-review full `879 passed, 1 skipped`，两项 validator 通过。全程没有新的网络/CPA/Image/Vision/ingestion 调用，四份真实证据哈希精确不变。Ruling K 只关闭 mock schema 边界，不授权第三次 canary、批量生成、manual promote、Task 5/UI 或 S16B 完成。
- **Ruling L 第三次真实 g051 canary（2026-08-24，已执行并安全停止）**：冻结命令只执行一次，exit `1`、wall `14.24s`，恰好一次 Image candidate 与一次 Vision；无 retry、额外 flags、g052–g120、Openverse、bulk 或 manual promote。g051 继续公开 `quarantined`，`failure_stage=vision`、`reason_code=response_schema_invalid`，license/path/accepted hashes/receipt 均为空，source g051 为 0，Image/Vision provenance 均诚实为 resolved `null`、verified `false`。manifest/source 哈希仍为 `6e71d9ca9cd3e5e9c7a037c56498303ece03021179505dfd21ed2a5c218f4682`、`eca7fd576e10853388cb2de624698b06bb8a49bc8cb443e11e63118599ef19e2`；private diagnostic 增为两行、SHA-256 `869172061aa8b8504813673daaea2df56c63dd18787538db6161501b43fe41de`，新增非服务 PNG 为 678,636 bytes、SHA-256 `afc00f9705193d7d0ea2072989b07bdb6b74ceae757b1f20ba5f8575db662abf`，旧 PNG `46c5...4e98` 保留且 marker 不存在。人工只确认新图为无人物/logo/文字的海军蓝系带女衬衫，不能覆盖 Vision 失败或手工晋升 ready。
- **Ruling M 第三次 canary 的 ingestion stage 持久化修复（2026-08-24，mock-only 已关闭）**：actual reviewer 先报 `P0/P1/P2=0/1/0`，确认 adapter 已有 stage 但 ingestion/CLI/private diagnostic 丢失；测试 `caab958`、fixture correction `ca2ae4f` 与实现 `eba1e0c` 建立 v2 stage 传递。fresh review 又报 `0/2/0`：任意双删 version/stage 可伪装 legacy，且非 schema Vision 拒绝的合法 stage 被错误中断；测试 `0d3db27` 与实现 `2e26a9b` 将 legacy v1 限定为冻结两行 raw-line SHA allowlist，并允许所有 Vision 拒绝携带 closed stage、unknown/prose fail closed。replacement reviewer 最终 `PASS`，`P0/P1/P2=0/0/0`；licensed-assets `400 passed`，post-review full `886 passed, 1 skipped`，两项 validator 通过。五份真实证据哈希精确不变、marker 不存在，全修复阶段无网络/CPA/Image/Vision/real ingestion 调用。Ruling M 不授权第四次 canary、batch、manual promote、Task 5/UI 或 S16B 完成。
- **Ruling N 第四次真实 g051 canary（2026-08-24，已执行并安全停止）**：基于 `c9752f6` 与 Ruling M `PASS 0/0/0` 的冻结 preflight，唯一授权命令只执行一次，exit `1`、wall `15.91s`，恰好一次 Image candidate 与一次 Vision；无额外参数、retry、g052–g120、Openverse、bulk、manual promote、Task 5/UI。g051 继续公开 `quarantined`，`failure_stage=vision`、`reason_code=response_schema_invalid`、`schema_stage=envelope`，license/path/accepted hashes/receipt 均为空，source g051 为 0；Image/Vision provenance 均诚实保留 requested model、resolved `null`、verified `false`。manifest/source 哈希仍为 `6e71d9ca9cd3e5e9c7a037c56498303ece03021179505dfd21ed2a5c218f4682`、`eca7fd576e10853388cb2de624698b06bb8a49bc8cb443e11e63118599ef19e2`；private diagnostic 增为三行、2,740 bytes、SHA-256 `0f981797ee2dc37d41a0563e1d5d34b8d31802ac5589beae4aa223309843dd28`，前两条 raw-line SHA 仍精确为 `8dfb54...bf5f`、`c2cbcf...9bfc`，第三条是严格 v2 `envelope` 记录。新增非服务 PNG 为 784,326 bytes、SHA-256 `33c9439e4516e571925689f0ef501a9771f3d25888ff24ecbe044c6b16d19749`，旧 PNG `46c5d7...4e98`、`afc00f...2abf` 保持精确且 marker 不存在。人工只确认新图为无人物/logo/文字的海军蓝女式上衣，同时领口超长交叉带结构异常；它必须继续隔离，不能覆盖 Vision 失败或手工晋升 ready。
- **Ruling N actual-canary review（2026-08-24）**：fresh read-only reviewer 返回 `PASS`，`P0/P1/P2=0/0/0`。六份证据、三条 transaction/私图绑定、legacy byte prefix、v2 闭集 stage、公开/来源隔离、provenance、receipt 与 marker 均通过。`schema_stage=envelope` 已把失败限定到 JSON/completion envelope/profile 层并排除 content/payload，但不能断言具体 metadata 字段。下一步只规划 mock-only TDD：增加服务端生成的 `envelope_failure_code` 闭集，不记录响应原文、字段值、未知键名或 Provider prose；已知标准 metadata profile 必须版本化、严格验证后丢弃，未知字段继续 fail closed。该本地修复经 fresh review 通过后，才可另立新 ruling，最多授权对现有私图 `33c943...9749` 做一次 Vision-only 诊断；不得再生图、不得修改 manifest/source/private evidence、不得自动或手工晋升。Ruling N 不授权第五次 canary、任何外调、batch、Task 5/UI 或 S16B 完成。
- **Ruling O envelope 细分诊断（2026-08-24，mock-only 已关闭）**：tester `661f115` 以 7 个测试函数/22 cases 冻结七值 `envelope_failure_code`、版本化 `cpa_chat_completion_metadata_v1` profile、无 raw body/content/值/未知键名/prose 泄漏以及 Vision→attempt→CLI/outcome→private v3 传递；旧 fixture 同步 `019aec4` 明确新写一律 v3、真实 v1/v1/v2 仅只读兼容。backend `069459d` 只改 Vision 与 ingestion，focused `22 passed`、旧 K/M `13 passed`、licensed-assets `422 passed`。fresh review 报唯一 P1 `0/1/0`：任意新 v2 仍可借历史读取降级；tester `3e459c6` 以 4 函数/5 cases 固化唯一 frozen v2 raw-line SHA `51bbd3...397e`、伪造 pre-provider/零写入拒绝与真实 byte-exact 兼容，backend `d536cd8` 在 model validation 前绑定该唯一 SHA。replacement reviewer 最终 `PASS`，`P0/P1/P2=0/0/0`；focused `28 passed`、licensed-assets `427 passed`。post-review full 为 `913 passed, 1 skipped in 376.96s`；base validator 为 `3 users / 50 garments / 20 outfits / 50 catalog / 30 eval`，S16 validator 为 120 件、owners `72/24/24` 且类别 `24/24/16/18/18/10/10`。报告副作用已恢复，tracked/cached clean，六份真实证据哈希精确不变、marker 不存在，全阶段无网络/CPA/Image/Vision/real ingestion。Ruling O 只关闭本地诊断合同；当前不授权任何 Vision-only 调用、生图、第五次 canary、bulk/manual promote、Task 5/UI 或 S16B 完成。若要对现有私图做一次 Vision-only 诊断，必须另立 Ruling P 独立 preflight。
- **Ruling P 单图 Vision envelope 诊断工具（2026-08-25，mock-only 已关闭）**：tester `6a93dca`/`23f4272` 先冻结只读私图 `33c943...9749`、`top/tie-neck blouse`、唯一 `--expected-head` 参数、Provider 前 HEAD/hash/decode/config/model 校验、0 Image/最多 1 Vision/0 evidence 写入，以及 stdout 六键 + provenance 三键的最小闭集；backend `59636e1` 只新增诊断脚本。fresh review 连续发现真实 success profile、额外 trace 键、生产可达状态矩阵和 `quality_issue_count` 不变量四类 P1；均按 tester RED `30e4943`、`a9dd55d`、`a2216d1` 与 backend 单脚本修复 `60c1306`、`5336e4d`、`b67adab` 关闭。最终脚本只接受真实 17-key catalog trace、精确成功/前置失败/model mismatch/envelope-content-payload/semantic 状态组合和 5 项唯一质量码的可达计数；未知、缺失、poison、raw/provider body、非法组合统一最小化为 `provider_unavailable`，不输出响应正文、字段值、路径、ID、token 或 credential。final fresh reviewer 在核对 `vision.py` 的唯一质量码 validator 后更正误读并签收 `PASS`，`P0/P1/P2=0/0/0`。最终独立 gate：diagnostic `58 passed`、Ruling O `28 passed, 399 deselected`、licensed-assets `427 passed`、full `971 passed, 1 skipped in 329.68s`；base validator `3/50/20/50/30`，S16 validator `120`、owners `72/24/24`、类别 `24/24/16/18/18/10/10`，`py_compile` 与双 diff check 全绿，报告恢复为测试前精确字节。manifest/source/diagnostic/三张私图六哈希保持 `6e71...4682`、`eca7...f19e2`、`0f98...dd28`、`46c5...4e98`、`afc0...2abf`、`33c9...9749`，marker 不存在；整个 Ruling P 无网络/CPA/Image/Vision/diagnostic CLI/ingestion 调用。该 ruling 只关闭本地 mock 诊断工具，最多允许下一步另立 **read-only preflight**；仍不授权真实 Vision-only 调用、生图、第五次 canary、retry、promotion、batch、Task 5/UI 或 S16B 完成。
- **Ruling Q 单次 Vision-only 只读预检（2026-08-25，第一轮 PASS、待文档 HEAD 重绑定）**：tester 对候选 HEAD `6b5b2330ecf80b13234a6710ba2c9a76dbb38c68` 做纯只读快照，确认固定私图 `33c943...9749` 为 784,326-byte、1024×1024 PNG，语义固定为 `top/tie-neck blouse`；六证据 hash/size exact、marker absent、tracked/cached clean、仅预期 manifest/sources 未跟踪、无残留进程，且配置只暴露 CPA base 与 key-presence 布尔，逻辑模型 `grok4.6`、transport `grok-4.6-high`、reported allowlist exact。静态预算为 Image `0`、Vision `≤1`、retry/write/ingestion/promotion/batch `0`，stdout 仅六键且 provenance 仅三键；mock `58 passed`、两组 validators 通过。tester 与 fresh reviewer 均签收 `PASS`、`P0/P1/P2=0/0/0`，但 reviewer 正确指出本文档提交会改变 HEAD，因此第一轮逐字命令授权随提交失效。提交后必须用新 HEAD 重新做 tester + fresh reviewer 只读绑定；重绑定 PASS 前严禁执行诊断。最终只允许 `conda run --no-capture-output -n torch128 python scripts/diagnose_cpa_vision_envelope.py --expected-head <POST-DOC-FULL-HEAD>` 单次运行；任何非零退出、异常、非法 6+3 键输出、调用数超限、证据/diff/marker/status 变化均立即停止且不得重试。Ruling Q 不授权 Image、生图、ingestion、promotion、batch 或其他真实调用。

#### S16C — 图上长按替换、Look vN 与异步图片（`backend` + `frontend` → `reviewer` → `tester`，未完成）

- **目标**：用户选定 Active Look 后，通过约 450ms 长按、鼠标或键盘 Enter 选择槽位；服务端返回 owner-bound 合法候选，单槽 CAS 创建不可变 Look vN，再异步生成版本绑定的 Static2D。
- **硬规则**：客户端不能提交整套 Look truth；替换必须重验 owner/ID/receipt/状态/季节/禁忌/完整性/购物门控；图片与槽位定位失败不得回滚或伪造 Look。
- **验收口径**：低置信定位回退槽位选择器；高急换装仍 Catalog 0；2D 只标记为视觉参考，无 3D/360°/视频入口。

#### S16R — 文档、评测与联合终审（`reviewer` → `tester` → `supervisor`，未完成）

- **目标**：运行 S16 专项、S15/R1 全量回归、固定 eval、真实浏览器和单命令 JSON+MD 报告；关闭全部 `[P0]/[P1]/[P2]` 后再同步 README、PRD 0.3、API、AC 与 BUILD_LOG 的完成状态。
- **终审边界**：先由 Codex 核查本地事实和门禁；实际调用 Grok 前必须让用户确认当次模型 ID，Grok 只提供咨询证据，最终由 Codex 收拢。
- **退出条件**：UrgencyAcc≥95%、高急 ShoppingGateAcc=100%、Catalog 0、Item Hallucination=0、Hard Constraint Violation=0、Slot Completeness≥95%，其余 R1 P0 门槛不退化；在此之前 S16 不得宣布完成。

#### S17+ — 已同步的未来规划（S16 不实施）

1. **A + LangMem 后台提取（路线 B）**：仅在积累真实对话与误提取标注后启用；回复完成后异步产生结构化候选，仍经本项目敏感过滤、用户确认和 SQL 提交，不允许自动改写人格/安全 prompt。
2. **替代评估：A + Mem0 侧车（路线 C）**：仅当“自研检索维护成本”超过接入与双写治理成本时立项；Mem0 只能作为 soft RRF 的可重建通道，结果必须回 SQL 校验，和 B 不同时起步。
3. **远期：A + Graphiti 时序图（路线 D）**：只有出现跨场景偏好演化、多实体关系、双时间追溯与图解释的真实需求后再建；图仍是 SQL outbox 派生投影，不成为授权、删除或硬约束真值。
4. **学习型 reranker**：只有受控 deterministic V1 在真实反馈集上出现明确质量瓶颈，且隐私/延迟预算通过后，才评估小型本地 Cross-Encoder；其输出仍不能越过 SQL/HardFilter。

## 2. 并行与冲突控制

1. S0 先冻结合同，之后 S1A/S1B、S2A/S2B 才并行。
2. `backend` 与 `frontend` 不同时改共享合同；需要变更时由 supervisor 先更新合同并通知双方。
3. `reviewer` 始终只读；不以直接改代码掩盖审查发现。
4. `tester` 在功能就绪后接手测试/报告，不重写业务实现；测试发现回派 owner。
5. 所有项目、CPA Provider 辅助和最终审查命令使用 `conda run -n torch128 ...` 且串行执行。

## 3. 已确认执行项

1. 用户已确认按以上阶段、委托、AC 映射和硬门槛开始执行。
2. 环境统一为 `torch128`；Stylist 对话通过 CPA 使用逻辑模型 `grok4.6`，静态 2D 生图通过 CPA 使用独立图片模型 `grok-imagine-image-quality`，两者配置、验证与健康状态完全解耦。
3. 用户在 2026-08-06 明确要求本 Demo 的对话与静态 2D 生图都调用 CPA；S6 因此启用一条实验性 R2 `static_2d` 纵切，但不把它混入或放宽 R1 DoD。3D/360°/视频仍是硬红线。
4. 最终由 Grok + Codex 独立审查并相互验证，Codex supervisor 依据 PRD 与本地可复现证据收拢结束。
5. 用户曾在 2026-08-10 要求未来模型不在开发电脑或移动端运行生产推理；随后进一步确认，未上线的个人开发阶段继续 CPA-first，不为了预测成本提前自托管小模型。只有真实客户量、SLA 和成本数据证明有必要时，才另立云端自托管迁移项目；候选模型必须先通过 ProfAgent 固定安全与对话集，未达门槛不得替换 CPA 链路。
6. 用户确认 2D 需要同时覆盖衣橱虚拟服装/虚拟人物资产初始化、统一套装图和上身效果图；实现改用独立配置并验证的 Grok 图片 Provider，文本模型不再承担生图。该能力仍为实验性 R2 2D，不扩展至 3D/视频。
7. 用户确认移动端持久化主体保持 PostgreSQL/对象存储/可选 Redis/知识图谱投影不变，并参考 `personalized-shopping-copilot` 将 weighted RRF 用于软记忆多路融合；硬记忆继续走精确数据库路径，不受 RRF 排名支配。
