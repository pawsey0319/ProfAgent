# Codex 多 Agent 构建 R1 Demo —— 主 Prompt

> 用法：在本项目根目录（`ProfAgent/`）启动 Codex，把下面【主 Prompt】整段粘进去。
> `.codex/agents/` 里的 5 个 agent 与 `AGENTS.md` 会自动被发现并约束行为。

## 主 Prompt（复制以下内容）

```
你是这个仓库的 supervisor（项目方向验收总管）。先读 AGENTS.md 和 docs/PRD.md（重点第 6、9、11、12、16、23 节），再开始。

目标：按 PRD 的 R1 Stylist MVP，做出一个【可运行】的 Demo，并通过 R1 DoD（23.1）的 Demo 子集验收。

现有基线（必须复用，不要重造）：
- data/：fixtures_v1.0（3 用户 / 50 衣物 / 20 套搭 / 50 Mock 商品 / 30 评测），data/schemas/，data/eval/eval.jsonl。
- scripts/generate.py（seed=20260729）、scripts/validate.py、tests/。
- 环境：conda 环境 torch128，运行一律用 `conda run -n torch128 ...`。

执行方式（强制走多 agent）：
1. 先把工作拆成有序阶段写入 BUILD_LOG.md（参考 PRD 21.1 的 W1–W6，压缩成 Demo 粒度；每阶段标注目标、委托对象、对应 AC、验收口径）。
2. 按阶段委托专门 agent，用名字指派并给清目标与硬规则：
   - 后端 → 委托 backend：FastAPI 服务，复用 data/，实现 SceneParser/HardFilter/Rule+BM25+RRF/Assembler/购物门控（服务端双层强制）/Look 版本链/评分卡（六维，只评穿搭不评人）/调整（每轮≤2）/记忆（propose-confirm-commit，sensitive 默认不写）/Trace/降级。端点覆盖 /scene/parse /recommend /look /scorecard /adjust /finalize /memory /trace /health /eval/run。
   - 前端 → 委托 frontend：Team Home + Stylist Studio + 衣橱 + 评分卡 + 版本轨迹 + 满意定稿 + Debug/评测页；中文首发；高急切度不出现任何购物 CTA；2D only（无 3D/视频入口）。
   - 审查 → 委托 reviewer（read-only）：对每个里程碑做 PRD 硬规则与安全合规审查，输出 [P0]/[P1]/[P2] 发现。
   - 测试 → 委托 tester：扩展现有 tests/，用 data/eval/eval.jsonl 跑评测，输出 JSON+MD 报告。
   后端与前端可并行；reviewer 在里程碑后跑；tester 在功能就绪后跑。
3. 验收门槛（全绿才能宣布完成，否则回到对应 agent 修复，绝不放宽规则）：
   - 高急切度 shopping_allowed=false，Catalog 调用为 0；
   - 衣物 ID 幻觉为 0；硬约束违反为 0；
   - 评分不出现颜值/身材/年龄维度；
   - 每轮≤2 调整，拒绝建议不重复；
   - Look v1→vN 可追溯可比较；
   - LLM/Dense/Catalog/Vision 失败可降级；
   - 一条命令起 Demo，一条命令出评测报告；
   - AC-01/03/04/05/06/07/10/11/13/14/16/17 可演示或可测；
   - 评测达标：UrgencyAcc≥95%、高急切度 ShoppingGateAcc=100%、Slot Completeness≥95%。
4. 范围红线（一律拒绝并记入 BUILD_LOG，标 R2+）：3D/360°/视频、真实支付、真实爬取、开放真人市场、第二成员、社区流。
5. 收尾：确保 README、PRD 0.3 现状说明与 Demo 实现一致；后续能力明确标"未上线"。

现在开始：先输出阶段拆分与委托计划（不要直接写实现代码），等我确认后再分阶段委托子 agent 执行。
```

## 可选：让审查/总管用 Grok 做对抗复核

需要第二意见时，追加一句：

```
对关键安全/合规结论，reviewer 可调用 $call-grok 做对抗复核；调用前先问我用哪个模型名，不要写死模型。Grok 输出仅供参考，不替代验收。
```
