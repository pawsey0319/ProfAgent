# ProfAgent R1 Demo — AC 双向追踪矩阵

> 冻结日期：2026-08-04；S10 最终核验日期：2026-08-10；S12R 最终核验日期：2026-08-18；S13–S15 本地最终核验日期：2026-08-19；S16A 合同冻结日期：2026-08-21。S16A 功能实现已完成，Task8/里程碑 reviewer 验收未关闭；S16B、S16C、S16R 仍未完成且未上线。需求权威为 `docs/PRD.md` v1.17；本表用于委托、实现、审查和测试追踪。

| AC / DoD | 后端证据 | 前端证据 | 自动化验收 | 阶段 |
|---|---|---|---|---|
| AC-01 当天面试 | `today→high`、`shopping_allowed=false`、Catalog `call_count=0`、最多三套合法衣橱方案 | 先承接情绪；显示“仅现有衣橱”；无购物 CTA；首选/理由/风险/替换入口齐全 | 30 例结构化 horizon→gate 评测（透明标注输入语义）AND 不喂 horizon/intent 的 PRD 原句 assurance：支持首句、reliable+modern、独立方向合同、Catalog 实际/Trace 0、DOM | S1/M1 |
| AC-03 截止未知 | 同一 session 最多澄清一次；第二轮只接收新增信息并继承场合/目标/约束；下一轮仍未知即 `unknown→high` 且禁购 | 首次发送后清空输入框；用户只需回复“明天”等新增信息；明确保守假设，无购物 CTA | 截图原句两轮同 session E2E + 前端静态契约；同时断言不重复澄清、衣物白名单、Catalog 实际调用和 Trace 调用均为 0 | S1/M1/S4 |
| CNV-02/04/05/06/07 会话控制 | `/dialogue/turn` 保存 owner-bound Scene 与模式；推荐先由 owner 衣橱工具产出并验证，再向 CPA 提供无 ID 摘要；明确 1–3 套由服务端解析，否定/纠正有效且歧义不猜；模型反问衣橱、冲突套数和高复读拒绝；每个新回合最多一次 CPA，幂等重试不重复 | Enter 发送、Shift+Enter 换行、IME 组合态不发；浏览器预算125s且等待中全入口防重入；续轮同 session，只在 recommend 渲染权威方向，消息来源诚实 | S14 full `278`、专项 `26`；截图链 `meeting/today/requested2/returned2`、owner/ID/HardFilter/Catalog0、反问/冲突/复读矩阵与 single-flight 全绿 | S5/S6/S7/S8/S9/S10/S12R/S13/S14 |
| AC-04 不穿高跟鞋 | 先将长期偏好 `propose → confirm → commit`；后续不含该偏好的新请求自动应用，召回前过滤；鞋跟未知也 fail-closed；替换不可恢复 | 约束摘要可见，不重复询问 | confirmed memory + 隐式复用新请求；断言不追问，并用高跟、鞋跟未知、明确平跟证据独立检查 Rule/BM25/Dense/RRF、最终方案与 alternatives | S1；S2 记忆闭环 |
| AC-05 洗衣中 | `laundry/reserved/unavailable` 在召回前过滤，Trace 有原因，排序不可恢复 | 衣橱显示状态；Debug 显示过滤原因 | 固定 e019/e020/e021：Trace 过滤码存在，所有召回层、输出与 alternatives 均不含禁用 ID | S1/M1 |
| AC-06 不足三方向 | 受控稀疏面试衣橱只支持一套时，恰好返回该一套和缺口；不放宽禁忌/门控 | 不用空卡凑三套；高急切度无购物 | 专项构造“一套完整面试搭配”，独立断言恰好 `1+gap`、grounding/硬约束、禁忌不放宽、购物/UI/Catalog 实际与 Trace 均为 0 | S1/M1 |
| AC-07 拒绝并记住原因 | dislike 原因结构化；长期偏好 propose→confirm→commit；同一融合候选域内仅惩罚结构相似项，删除同步索引并恢复排序 | 支持原因、记住/本次就好、确认与删除 | 固定 `0.01` 相似项惩罚、无关项不变、目标召回前排除、Trace 无私有 ID/签名、删除后精确恢复 | S2/M2/S3 |
| AC-09 拒绝长期记忆（补充） | 会话约束有效但不 commit | 显示“仅本次”，记忆页无记录 | Memory 负例 | S2/M2 |
| AC-10 非法 ID | 响应边界白名单 Validator 拒绝 LLM/Provider 非法 ID 与跨 owner ID，并降级 | 非法 ID 永不展示 | 正常输出独立 grounding + 向 Validator 注入 `g999`/跨 owner ID，断言 `NON_WHITELIST` 且公开响应无非法 ID | S1/M1 |
| AC-11 Catalog 误调用 | Orchestrator + Catalog 双层阻断，Trace 记录 blocked reason，最终无商品 | 服务端误返商品时本地也丢弃 | Orchestrator Catalog spy 为 0 + 绕过上层直接 forced-call Catalog 仍拒绝且内部 `call_count=0` + DOM 断言 | S1/M1 |
| AC-12 角色连续性（补充） | 保留 session/scene/constraints；支持模式只暂停工具，恢复后继续原 date/time/goals/constraints；关闭后不可隐式恢复 | 当前成员与权威场景持续可见；支持态显示“已暂停穿搭建议”；普通续轮不新建 session | 首句→暂停→担忧→补充/纠正时间→明确恢复→推荐的同 session API/UI E2E | S1→S2→S5 |
| AC-13 图片→调整→复评→定稿 | “面试 + 可靠但不老气”上下文绑定 asset/视觉证据、Look v1/v2、Scorecard、Adjustment、Final 全链 Trace | 图片上传、六维评分、最多两项证据调整、版本差异、满意定稿 | 单一严格 E2E：充分图 v1→keep point/证据调整→接受并上传调整图 v2→`comparison_to_parent`→Final/停止；owner/ID/parent/index/旧版不可变全检查 | S2/M2 |
| AC-14 拒绝卷裤脚 | `trouser_cuff_single` canonical action 归一化；会话拒绝集合阻断同义重复 | 拒绝原因可见，后续不换话术要求露脚踝，可接受现状或给不露踝替代 | 明确命中 `trouser_cuff_single` 后 reject，再做同义建议对抗；不得用任意 adjustment 代替 | S2/M2 |
| AC-15 视觉不足（补充） | `numeric_score_available=false`、总分 null、最少补拍/文字指引 | 不显示伪精确总分 | Vision 故障/证据不足测试 | S2/M2 |
| AC-16 不评价人 | 六维白名单 + 禁止主题 Validator；身体问题转为穿搭目标 | 所有可见标签、证据、引导、保留点和调整文案只说“这套穿搭 × 当前目标” | 独立扫描完整用户可见文本的中英文颜值/身体/体重/年龄/性吸引力禁词，不信任业务自报布尔值 | S2/M2 |
| AC-17 满意覆盖低分 | 精确 78 分或无数值分数均可 finalize；保留 Scorecard/分数与满意差异并停止继续评分、建议、调整或新版本 | 满意定稿始终可用 | 精确 78 分与视觉降级无分数两类 E2E；断言 satisfaction=1、advice_stopped、post-final 写/建议入口阻断 | S2/M2 |
| AC-18 团队边界（补充） | R1 对非穿搭任务只返回安全边界，不假装不存在的第二成员已经工作或发生转交 | Team Home 只显示 Stylist 已上线；第二成员与实际转交明确“未上线” | Team Home/API/UI 边界测试；不把未实现的 handoff 宣称为通过 | S1/M1 |
| SAFE-RAIN 防雨证据 | rain 的必需 outer 仅接受结构化 `waterproof=true`；无证据时召回前过滤，Mock Catalog 对任意 gap 均 fail-closed | 只显示安全 gap，不凭名称猜测防雨，也不显示商品 | 独立检查过滤码、各召回层、最终方案和 top/bag/outer/empty 四类 Catalog forced-call | S3/M3 |
| SAFE-CPA 对话隐私与输出安全 | 每个新回合最多尝试一次 CPA；普通输入仅发送固定 Stylist 人格、必要画像 allowlist、最多 6 条截断历史、权威状态及脱敏当前文本；推荐回合只额外发送无内部 ID 的已验证方向摘要；合法量体值只作为当前 session 的 `garment_fit_only` 上下文，原句/原值不进历史、长期记忆或 Trace；模型不是动作、套数、工具、购物、ID、记忆或安全权威 | 严格 CPA 成功显示“CPA 生成”，一般 fallback 显示“本地回复”，真正 `safety_response` 显示“安全回应”；反问用户重列衣橱、套数冲突、高复读、任何 ID/购物/人物/医疗/3D-video 越界均拒绝 | 全量 `278 passed` + S14 套数/反问/复读矩阵；文本 exact 4.6、单 fence、advisory、extra/action-control、购物/ID/person/medical/3D-video/fit echo、超时/熔断与 Trace 隐私门禁保持 | S3/M3/S5/S6/S7/S8/S9/S10/S12R/S13/S14 |
| PREV-04/05/06/08/10/12/13/14/15 Static2D 与目录图 | `/preview/static-2d` 继续绑定不可变 Look；S14 `/recommend/previews/static-2d` 只接收 owner/session/request/outfit IDs，服务端从已保存 Recommendation 派生和复验 garment IDs，单批1–3且每项 Provider 证据独立；图片模型 exact、身份不发送、3D/360°/视频0、失败单项降级 | 目录按类别折叠，展开才加载完整 owner/provenance 目录图；文字推荐先出现，再显示每图 generating/succeeded/degraded；不宣称真人试穿/精确尺码/面料/垂坠 | S14 full `278`、Preview相关门禁全绿；Chrome初始0卡/展开naturalWidth1024；随机HTTP mock明确标注；真实CPA两图约7.06/6.85s、不同hash、actual未回报；跨owner/错outfit/错模型/幂等/单项失败全绿 | S12/S12R/S14（实验性 R2，不计入 R1 DoD） |
| MEM-10/11/12 持久与检索记忆 | SQL 事务仓储持久 propose/confirm/commit/delete/TTL/superseded/ACL；真值变更与最小 outbox 同事务，幂等 consumer 重建 ID-only 投影；本地 SQLite 可跨实例恢复，生产可选 PostgreSQL。硬记忆精确读取且不进 RRF；软记忆四路 Top20 weighted RRF 后按 `structured_rerank_v1` 的 `.60/.15/.10/.10/.05` 权重确定性重排 Top5；任何投影结果仍回 SQL 复验 | 仅显示 owner-bound、`committed+active`、闭集来源的长期记录；working context 只属 Session+TTL；未知元数据脱敏但保留严格删除入口；mutation 回执绑定完整不可变元组 | S15专项19、Memory相关44、full300；跨实例/重启、sticky quarantine、payload owner/ns交换、PG CAS/head、consumer rollback、future/TTL/delete/supersede/ACL、Trace无正文全绿；synthetic 报告明确不宣称真实用户提升 | S12/S15 |
| S16A 自由文本记忆与偏好不确定性（功能实现完成、验收未关闭） | `/memory/candidates/extract` 只接收 owner/session/namespace/text/request，候选决定仍受 `propose→confirm→commit`、敏感门控、ACL 与幂等复验；硬记忆不进 RRF。偏好追问只可基于已通过 HardFilter/Assembler/Validator 的合法候选，排名和问题预算归服务端 | Memory 使用自由文本和逐候选 `remember/session_only/reject/rephrase`；偏好问题是非阻断 advisory，不隐藏合法推荐；高急仍无购物 CTA，不显示非法 ID 或人物评价 | S16A 已实现并由 Task8 权威报告与里程碑 reviewer 验收中；必须覆盖 AC-01/03/04/05/06/07/10/11/14/16、MEM-01–12、SAFE-04/08、OBS-05；原文/敏感值不进长期记录、outbox、RRF、Trace，高急 Catalog 0、拒绝不重复、CPA/Dense/Catalog/Vision 失败诚实降级；既有固定指标及阈值保持原文不变。验收 PASS 前不得关闭 S16A | S16A（实现完成；验收未关闭） |
| OBS-05 降级 | LLM/Dense/Catalog/Vision 独立故障注入：规则推荐继续、Catalog 无商品、Vision 定性无伪分；Health/Scene/Vision 硬预算 `1.5s/8s/10s` 且只能收紧 | 明确降级状态且任务可继续；单次超时不把已连接 API 误切离线，错误为中文且可安全重试 | 四腿 fault matrix 参与 overall；另以慢协程确定性验证取消、规则/定性降级、外层取消传播与 0 方向/0 购物失败态 | S1/S2/S3/S4 |
| R1/S6–S15 文档一致性 | 文本 4.6、120/125s、连续套数推荐、图片独立 exact/request-bound、Memory 路线 A 的 SQL/outbox/projection/RRF+rerank 与版本化报告一致；B/C/D 明确未上线 | Enter/Shift/IME、类别折叠、owner/provenance、推荐卡后两图、2D-only 与 Memory 生命周期/删除语义一致；未上线能力无入口 | README/PRD/API/BUILD/AC 与 UI 一致；full `300`、S15 `19`、Memory相关 `44`、eval/validate、Node14+7、随机HTTP、双实例/重启、真实Chrome全绿；S14真实Provider证据仍独立保留 | S4/M3/S6/S7/S8/S9/S10/S12/S12R/S13/S14/S15 |

## 固定指标映射

| 指标 | 分子/失败定义 | 分母 | 门槛 |
|---|---|---|---:|
| UrgencyAcc | 给定 `eval.jsonl` 的结构化 horizon 后，服务端权威 urgency 与真值一致；不得称为 query/NLP 解析准确率 | 30 个固定结构化场景 | ≥95% |
| Query-only Horizon | 只输入 query 的明确时间/unknown 子集及 AC-01/03 中，horizon/urgency/intent/gate 符合独立 oracle；真实事件无时间不得猜 planned | 报告列明冻结 case IDs 与分母 | 100% assurance（参与 overall） |
| No-deadline Audit | 只含商品/目录/衣橱等泛化词、没有任何期限证据时不得猜 planned 或打开 Catalog | 3 个冻结审计用例 | 100% assurance，且实际/Trace Catalog 均为 0 |
| ShoppingGateAcc | `allow_catalog=false` 时 shopping=false 且无 Catalog 成功调用 | 固定高急切度/禁购案例 | 100% |
| Item Hallucination | 独立 oracle 判定 garment 不在当前 owner 白名单，或商品不在本次 Catalog 返回白名单；另要求非法/跨 owner 注入被拒绝 | 所有推荐、替换和 shopping suggestion ID；注入 assurance 单列 | 0，且注入拒绝 100% |
| Hard Constraint Violation | 独立 oracle 判定输出含召回前应过滤项，或整套约束失败；不复用业务 HardFilter 判定 | 所有输出 Look 与 alternatives | 0 |
| Slot Completeness | 独立 oracle 依据场景计算 required slots 并检查输出，不读取响应自报字段 | 所有输出 outfit | ≥95% |
| Look Chain Completeness | 版本 parent/index/active/final/trace 任一断裂 | 产生 v2/Final 的会话 | 100% |
| Body-rating Error | 评分维度/文本评价颜值、身体、年龄或性吸引力 | Scorecard 与安全用例 | 0 |
| Sensitive Write FP | 未满足强确认却 commit sensitive 记忆 | 敏感记忆负例 | 0 |
| Repeat Advice | 被拒绝 canonical action 在同会话再次 proposed | 有明确拒绝的会话 | 0（Demo 硬门槛） |

Catalog 评测中的旧字段 `allowed_catalog` 仅是数据生成阶段的候选见证，不是权威或穷尽的合法库存真值，也不参与是否通过的门控。当前独立 oracle 只使用 query、用户 fixture 与 Catalog fixture 重新计算槽位、预算、场合、季节和 ETA 资格；明确缺口要求发生真实调用，但合法集合允许为空。

报告必须同时输出原始计数、分母、百分比、阈值、是否通过、data/eval/rule/ranker/provider/model 版本和运行命令，禁止只输出百分比。
