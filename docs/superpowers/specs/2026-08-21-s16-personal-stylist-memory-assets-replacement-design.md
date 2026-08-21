# S16 私人 Stylist：自由文本记忆、女装资产与图上换装设计

**日期**：2026-08-21

**状态**：用户已批准，进入分阶段实施规划

**基线**：S15 Memory 路线 A；R1 Stylist Demo；CPA-first 文本与独立 CPA Image/Vision Provider

## 1. 背景与目标

当前 Demo 已具备 owner 衣橱推荐、不可变 Look 版本、Static2D、CPA 对话、`propose→confirm→commit` 记忆、SQL 硬记忆和 Weighted RRF 软记忆。S16 不重造这些基础能力，而是在其上解决四个连续体验问题：

1. Memory 管理页仍要求选择模板，不能自然地表达多条偏好。
2. 当多套合法穿搭接近且缺少关键偏好时，Stylist 不会恰当地追问并沉淀答案。
3. 推荐结果的固定“替换入口”脱离图片语境；用户希望直接长按图中服饰进行替换并得到新 Look 图片。
4. 现有 50 件 fixture 衣橱的品类和风格覆盖有限；第一版产品应聚焦女装，但界面身份仍是“私人 Stylist”。

S16 的目标是交付一个可演示、可测且不放宽 R1 安全门槛的完整闭环：自然表达偏好 → 显式确认 → 检索记忆 → owner 衣橱推荐 → 2D Look → 长按服饰替换 → Look vN 与新图。

## 2. 范围与非目标

### 2.1 本期范围

- Memory 页自由文本输入与候选记忆确认卡。
- 对话中的偏好不确定性识别、单问题澄清及可选长期记忆确认。
- 女装 V1 衣橱扩充至 120 件，并为每件衣物提供可展示图片和来源状态。
- 结果图上的长按选槽、合法替换候选、服务端换装和新 Static2D 图片。
- 现有 S15 事务真值、ACL、生命周期、outbox、硬记忆直读、软记忆 RRF 与结构化 rerank 的复用。
- CPA 文本、Image 与 Vision 职责分离及失败降级。

### 2.2 明确非目标

- 不做 3D、360°、视频、实时视频或生成视频。
- 不宣称虚拟试穿、真实合身度、真实尺码、面料垂坠或人物身份还原。
- 不做真实支付、真实 Catalog、开放真人市场、第二专业成员或社区流。
- 不做绕过网站风控、许可或服务条款的真实网页爬取。
- 不接入 LangMem、Mem0、Graphiti、学习型 reranker 或 Cross-Encoder；它们保持未上线。
- 不因女装 V1 推断或评价用户性别、年龄、身材、健康或吸引力。

## 3. 已选架构：受控混合路线 A

所有自然语言能力由 CPA 帮助理解或表达，但所有身份、授权、硬约束、衣物集合、Look 版本和记忆写入权仍在 ProfAgent 服务端。

```text
Memory 页或 Dialogue 用户文本
        ↓
本地敏感/标识符预过滤
        ↓
CPA 仅提取结构化候选
        ↓
服务端闭集映射、Schema 与 ACL 校验
        ↓
逐条确认卡：长期记住 / 仅本次 / 否认 / 重述
        ↓
confirmed 候选进入 S15 propose→confirm→commit
        ↓
硬记忆 SQL 直读；软记忆 Weighted RRF + structured_rerank_v1
        ↓
Stylist 推荐与不确定性决策
```

CPA 没有数据库写权限，不能决定 `user_id`、team/member/namespace、可见性、敏感授权、确认次数、适用期、supersedes 关系或排序特征。原始自由文本、原始对话、模型推理过程和敏感值不进入长期记忆、软投影、RRF、Trace 或 outbox。

## 4. 自由文本记忆

### 4.1 用户体验

Memory 页移除“类型 + 模板”的主输入方式，替换为一个自由文本框。用户可以一次表达一条或多条事实，例如“我平时喜欢藏青和米白，正式场合不穿紫色”。系统按候选拆成独立确认卡，而不是将整段文本作为一条记忆保存。

每张确认卡使用自然中文，并提供四种动作：

- **以后记住**：进入现有 `propose→confirm→commit`。
- **仅本次使用**：写入 session-bound `working_context`，随 TTL 失效，不进长期记录或 RRF。
- **不是这个意思**：拒绝候选，不换说法重复提出。
- **重新说一下**：回到自由文本重述并重新提取；不允许直接编辑数据库字段。

发生冲突时必须明确呈现“将用新偏好替代旧偏好”，用户确认后用 `supersedes_memory_id` 保留版本链；不得静默覆盖。

### 4.2 受控候选合同

CPA 只能输出服务端允许的候选结构，例如：

- `canonical_kind`：颜色偏好、颜色禁忌、款式偏好、款式禁忌、场景偏好、舒适度偏好、材质偏好/回避等闭集。
- `canonical_value`：经服务端词典映射后的受控值。
- `memory_class`：仅允许映射到 S15 既有类；安全关键禁忌进入 `hard_constraint`，普通偏好进入 `profile_current` 或受控 `preference_event`。
- `applicability_tags`：场景、目标、舒适度等闭集标签。
- `confidence`：只影响是否要求重述，不产生写权限。
- `evidence_span`：只在当前请求内用于确认卡，不持久化。

任何未知字段、非法枚举、用户/作用域伪造、敏感候选或多个互相矛盾候选均 fail-closed。身高、体重、年龄、医疗信息、生物识别信息、联系方式和身份标识符默认不得成为长期候选。

### 4.3 与 S15 检索的关系

S16 不改变已确认的初始检索参数：

- SQL 先过滤 `user_id + confirmed + active + 未删除 + 未过期 + sensitivity ACL + namespace/member/team`。
- 硬记忆直接加载，永远不进入 RRF。
- 软记忆使用 `rrf_k=60`、每路候选 20、BM25 `1.0`、Dense `1.0`、Recency `0.75`、Importance `1.25`，融合后最多 Top 5。
- 继续使用 `structured_rerank_v1`：`0.60*rrf_norm + 0.15*context_match + 0.10*specificity + 0.10*confirmation_strength + 0.05*lexical_norm`。

本期只扩展受控记忆类型和适用标签，不修改权重，也不宣称 deterministic hashed Dense 等同生产语义 embedding。

## 5. 对话中的偏好不确定性

### 5.1 何时追问

服务端先完成权威 Scene、HardFilter、owner 衣橱检索、Assembler 和最终验证。只有同时满足以下条件才追问：

1. 存在至少两套合法且排序接近的候选；
2. 缺失的已确认偏好会实质改变候选排序；
3. 本会话尚未拒绝或回答同一偏好问题；
4. 当前没有更高优先级的安全或场景缺口。

典型问题是“这两套都合适，你今晚更想要稳重一点，还是更柔和一点？”或“你更偏向藏青还是米白？”问题必须基于真实候选差异，不能为了收集画像而泛问。

### 5.2 追问预算与回答处理

- 每回合最多一个简短问题。
- 高急切度最多追问一次；用户不答时采用保守默认并继续推荐，不阻塞任务。
- 追问是非阻断 advisory：同一响应仍返回 `action=recommend`、合法推荐和 `recommendation_paused=false`，不能为了收集偏好隐藏或延迟推荐。
- 若只有一个清晰最优方向，或已存在适用的 confirmed memory，则不追问。
- “随便”“你决定”只作为本次 session 的中性选择，不进入长期记忆。
- 用户答案立即影响当前推荐排序，但长期保存必须另显示“以后也记住这个偏好吗？”确认卡；该卡不是新的聊天问题。
- 用户拒绝的问题或建议在本会话不得换说法重复。

Trace 只记录受控的 `preference_gap_code`、是否追问、是否跳过及原因，不记录问题原文、答案原文或画像值。

## 6. 女装 V1 衣橱与授权资产

### 6.1 产品表达

界面和角色名称始终为“私人 Stylist”。女装是当前衣橱商品池和造型知识范围，不是对用户性别的判断。任何用户只要选择女装范围都可以使用。

2D 图片使用无身份女性模特或女装平铺；不得上传或复刻真实用户身份，不得评价人的身材。生成图必须标记为“搭配视觉参考”，衣物卡和服务端 receipt 才是具体 garment truth。

### 6.2 120 件覆盖矩阵

目标是总计 120 件，而不是在现有 50 件之外再加 120 件。owner 分配为：

- `u01`：72 件
- `u02`：24 件
- `u03`：24 件

品类配额：

| 品类 | 数量 | 覆盖重点 |
|---|---:|---|
| 上装 | 24 | 衬衫、针织、T 恤、轻正式与层搭 |
| 下装 | 24 | 西裤、直筒/阔腿裤、牛仔、半裙 |
| 连衣裙 | 16 | 通勤、约会、日常与场合裙装 |
| 外套 | 18 | 女士西装、风衣、大衣、夹克、开衫 |
| 鞋 | 18 | 乐福鞋、低跟鞋、平底鞋、运动鞋、短靴 |
| 包 | 10 | 通勤、日常、约会与轻旅行 |
| 配饰 | 10 | 腰带、围巾及小型配饰 |

必须覆盖通勤、会议、面试、约会、日常、旅行、运动休闲和场合活动；覆盖简约、商务、经典、柔和、休闲、街头、运动和场合风格；兼顾四季/跨季与中性色、常用重点色。每个核心场景至少应产生三个有实质差异的合法方向。

资产受众闭集只接受 `womenswear` 和 `unisex_womenswear_compatible`。明显男装版型不得进入 V1 推荐池；不能可靠分类的图片进入隔离区。

### 6.3 来源与许可

不实现通用网页爬虫。`LicensedAssetIngestor` 只允许：

1. 用户拥有或明确授权的图片；
2. Openverse 等提供机器可读许可与来源的 API，首期许可 allowlist 为 CC0、Public Domain 和 CC BY；
3. 后续品牌或合作方的正式 API；
4. 现有 CPA 生成目录图，且必须明确标记为 AI 生成参考。

每张资产保存来源 URL、作者、许可名称与 URL、导入时间、原始/处理后内容哈希、署名文本、处理方式和 takedown 状态。NC、许可不明、需要变换但禁止演绎、来源无法验证或疑似绕过风控的资产一律拒绝。

CPA Vision 只能做裁切、背景处理、受控品类/颜色/风格识别和质量评分，不能决定衣物 ID、owner、许可、授权或可见性。低置信度和审核失败资产隔离且不得被推荐。

### 6.4 展示

衣橱按服饰大类折叠。折叠态只显示类别名称和数量；展开后才加载该 owner 的全部卡片与图片。每张卡明确显示“授权照片”“用户自有照片”或“AI 生成参考”。120 件均须有可展示的 `ready` 资产；元数据卡不能伪装成照片。

## 7. 图上长按换装

### 7.1 交互

移除推荐卡上常驻的“替换入口”。当 Look 的 2D 图片生成后：

- 移动端在衣物区域长按约 450ms；桌面端支持鼠标长按；移动超过阈值或滚动时取消。
- 键盘用户可聚焦图片槽位并按 Enter 打开同一替换面板。
- 若槽位定位已验证，长按直接选中上衣、下装、外套、鞋等槽位。
- 若定位置信度不足，不猜区域，改为弹出槽位选择器。

### 7.2 槽位定位

Static2D 完成后，CPA Vision 接收生成图和服务端闭集槽位列表，返回归一化区域、槽位和置信度。服务端验证区域边界、重叠、槽位闭集和与当前 Look 的一致性。Vision 输出仅用于交互热区，不能改变 garment IDs 或 Look 内容。

### 7.3 替换事务

用户选中槽位后，服务端只返回该 owner 衣橱中经过以下检查的候选：

- garment ID 白名单、owner 绑定和 receipt 绑定；
- `available`、季节/天气、禁忌、拒绝项和场景硬约束；
- 槽位兼容、整套完整性和购物门控；
- 高急切度继续 `shopping_allowed=false`、Catalog 调用为 0。

客户端不能提交整套 Look 或任意 garment 集合，只能提交 `base_look_id + base_version + slot + replacement_garment_id + idempotency_key`。服务端重新加载不可变的 base Look，替换一个槽位，复检整套并创建新的不可变 Look vN；其他槽位保持不变。并发基于 base version 做 CAS，冲突时要求刷新。

### 7.4 新图片

合法 Look vN 先作为业务真值返回，随后异步请求 CPA Image 生成新图。图片 job 与 Look version、garment receipt、provider receipt 和 prompt hash 绑定。图片失败时：

- Look vN、衣物卡、理由和版本比较仍可用；
- 显示明确的生成失败/可重试状态；
- 不回滚合法 Look，不复用旧图冒充新图，不伪造 Provider 成功。

图片是风格与组合参考，不得暗示对真实衣服纹理、尺码或上身效果的精确复刻。

## 8. 服务边界与建议合同

实现计划应沿用现有 FastAPI 服务和事务模型，新增或扩展以下清晰边界：

- `MemoryCandidateExtractor`：本地预过滤、CPA 结构化提取和闭集校验；供 Memory 页与 Dialogue 共用。
- `PreferenceUncertaintyPolicy`：根据权威候选差异和 confirmed memory 决定是否追问。
- `LicensedAssetIngestor`：许可验证、下载安全、内容寻址、Vision 提取、隔离与 takedown。
- `LookSlotLocalizer`：维护图片槽位区域及置信度，不拥有 Look truth。
- `LookReplacementService`：候选生成、HardFilter、CAS、不可变 Look vN 和图片 job。
- `ImageJobService`：CPA Image 调用、幂等、状态、receipt 和诚实失败。

建议 API 形态由实施计划对照现有合同细化，但必须遵守：自由文本不能直接 commit；替换请求不能提交整套客户端 truth；图片 job 不阻塞 Look 创建；所有写操作都要 owner-bound、幂等且可审计。

## 9. 错误处理与降级

- **CPA 记忆提取失败/超时/非法输出**：不创建 proposal，不写数据库；当前对话可继续，Memory 页提示用户重述。
- **CPA 对话失败**：正常路径必须展示 CPA 结果；只有真实网络、超时、模型不匹配或输出 Validator 拒绝时才返回明确标识的本地降级，不得把本地文案标成 CPA。
- **偏好问题无人回答**：高急切度采用保守默认继续；其他场景不重复催问。
- **资产下载、解码、许可或 Vision 失败**：隔离资产；不进入推荐。许可撤销或 takedown 后图片立即下线，garment truth 保留并显示资产不可用。
- **槽位定位失败**：使用槽位选择器；不得猜。
- **替换校验失败**：不创建新 Look；返回受控原因，不显示未验证图片。
- **图片生成失败**：保留有效 Look vN 和可比较版本链，允许幂等重试。
- **Dense、Catalog、Vision 或 Image 不可用**：沿用 R1 降级规则；任何降级不得恢复被 HardFilter 删除的候选。

Trace 仅记录受控错误码、provider/model allowlist 结果、耗时、receipt/version 和降级路径，不保存自由文本、图片内容、画像值、敏感值或模型正文。

## 10. 迁移与发布

1. 先以幂等迁移增加候选提取和资产来源所需字段/表；不删除 S15 字段或旧记录。Look、Dialogue、Preview 与图片 job 在 S16 Demo 中仍是单进程生命周期，先通过 repository 形状预留持久化边界，但不宣称跨重启恢复。
2. 保留现有 50 个 garment IDs、owner 关系、Look 引用和已生成图片声明；扩充到 120 件时新增稳定 ID，不重编号。
3. Memory 自由文本提取上线前，旧模板记录保持可读；新 UI 不再要求用户理解内部类型。
4. 长按换装服务端合同和槽位回退就绪后，才移除前端常驻“替换入口”。
5. 图片、Memory 和 Dialogue 各自使用独立 Provider 操作与预算；图片异步任务不能延长文本对话返回时间。
6. BUILD_LOG 新增 S16 分阶段执行记录；README、PRD 0.3、API_CONTRACT 和 AC_MATRIX 只在门禁通过后同步宣称完成。

## 11. 测试与验收

### 11.1 单元与合同测试

- 自由文本拆分多候选、闭集映射、冲突/supersedes、逐条确认和 session-only。
- 敏感/身份信息默认不写；原始文本不进数据库、outbox、RRF 或 Trace。
- 硬记忆不进 RRF；S15 权重和 `structured_rerank_v1` 不变。
- 不确定性策略只在排序实质受影响时问；每轮最多一问；高急最多一次；拒绝不重复。
- 授权 allowlist、署名、哈希、SSRF/MIME/大小/解码、防 takedown 失效。
- 槽位区域校验、低置信回退、owner/receipt/ID/CAS、单槽替换和 Look 不可变。
- Image job 幂等、版本绑定和失败诚实性。

### 11.2 集成与 E2E

- Memory 页自由输入 → 多卡确认 → 重启/跨实例后可检索。
- Dialogue 缺少颜色偏好 → 一次追问 → 当前排序改变 → 可选长期确认。
- “今晚/现在”高急场景即使追问或换装仍 Catalog 0、shopping false。
- 推荐两套 owner 衣橱 Look → CPA Image → 长按上衣 → 合法候选 → Look v2 → 新图或诚实失败。
- 移动触控、鼠标长按、滚动取消、键盘 Enter 和折叠衣橱。
- 120/120 衣物均有可显示 ready 资产，owner 分布和品类配额正确，V1 不出现男装专属资产。
- CPA Text/Image/Vision 任一失败时，来源标签、版本链和降级状态真实。

### 11.3 不得退化的 R1 门槛

- UrgencyAcc ≥95%。
- 高急切度 ShoppingGateAcc =100%，Catalog 调用为 0。
- Item Hallucination =0，Hard Constraint Violation =0。
- Slot Completeness ≥95%。
- 评分卡不出现颜值、身材、年龄或性吸引力维度。
- 每轮调整 ≤2；拒绝建议不重复。
- Look v1→vN 可追溯、可比较、可回退。
- LLM/Dense/Catalog/Vision/Image 失败均有诚实降级。
- 一条命令启动 Demo，一条命令生成 JSON+MD 评测报告。

文本 CPA 继续单独记录首响应与完整响应墙钟；目标仍是 P95 `≤5s`，但外部 Provider 超时必须如实报告，不能通过提前插入本地回复伪造达标。图片延迟单独报告，不计入文本对话指标。

## 12. 多 Agent 执行与终审

实施计划应按依赖拆为 S16-0 合同/迁移、S16A 自由文本记忆与不确定性、S16B 授权女装资产、S16C 图上长按换装与图片 job、S16R 文档/评测/终审。主会话按仓库规则先委托 `supervisor`，再由其明确委托 `backend`、`frontend`、`reviewer` 和 `tester`；每个里程碑后 reviewer 只读审查，功能就绪后 tester 生成版本化 JSON+MD 报告。

最终由 Codex 做本地事实核查和门禁收拢，再调用 Grok 做独立对抗复核并相互验证。根据仓库规则，每次实际调用 Grok 前仍须让用户确认当次模型 ID；Grok 结论是咨询证据，不能替代本地测试和 PRD 验收。

## 13. 完成定义

S16 只有在以下条件全部满足时才可宣布完成：

1. 自由文本记忆、对话追问、女装 120 件资产、长按换装和新图闭环均可在网页与移动浏览器演示。
2. 原始自由文本和敏感数据没有进入长期记忆、检索索引、Trace 或 outbox。
3. 所有推荐与替换 garment IDs 都来自当前 owner 白名单，并通过整套硬约束复检。
4. 每个 Look version 是不可变业务真值，图片只是版本绑定的可降级视觉参考。
5. 所有资产有明确来源类型；未经许可或审核失败的真实图片为 0。
6. 既有 S15 与 R1 固定评测全绿，S16 专项报告全绿，reviewer 无 P0/P1/P2 未关闭项。
7. README、PRD、API 合同、AC 矩阵、BUILD_LOG 和实际 Demo 一致；未上线能力明确标注。
