# ProfAgent R1 Demo — 多 Agent 编排规则

> 本文件给 Codex 读。它定义"项目方向验收总管 + 子 agent"的协作方式、合同和验收门槛。
> 唯一需求权威：`docs/PRD.md`。本文件与 PRD 冲突时，以 PRD 为准。

## 角色与委托

| 角色 | agent 名 | 沙箱 | 职责 |
|---|---|---|---|
| 项目方向验收总管 | `supervisor` | workspace-write | 拆阶段、委托、验收、范围控制、BUILD_LOG |
| 后端 | `backend` | workspace-write | FastAPI、数据、检索、门控、Look 版本、评分、记忆、Trace、降级 |
| 前端 | `frontend` | workspace-write | Team Home、Stylist Studio、衣橱、评分卡、版本、定稿 |
| 审查 | `reviewer` | read-only | PRD 硬规则与安全合规审查，只报告不改代码 |
| 测试 | `tester` | workspace-write | 单测、固定评测、E2E/AC、版本化报告 |

委托规则：
- 主会话先把整体任务交给 `supervisor`；`supervisor` 再按阶段委托子 agent。
- 委托必须写明：目标、必须满足的 PRD AC 编号、不得破坏的硬规则、验收口径。
- `reviewer` 在每个可运行里程碑后跑一次；`tester` 在功能就绪后跑评测。
- 后端与前端可并行；写冲突高风险时，先约定接口合同（第 12 节字段）再并行。

## 不可破坏的硬规则（任何 agent 违反即 P0）

1. 衣物/商品 ID 必须来自工具白名单，幻觉为 0（AC-10）。
2. `now/today/unknown → high`；高急切度 `shopping_allowed=false`，Catalog 双层阻断（AC-01/AC-11）。
3. 禁忌色、非 available、季节/天气必需项在召回前过滤，排序不得恢复（AC-04/AC-05）。
4. 评分只评价"当前穿搭 × 当前目标"，不评价颜值/身材/年龄/性吸引力（AC-16/SAFE-08）。
5. 每轮 ≤2 项调整；用户拒绝的建议本会话不换说法重复（AC-14/ITER-06）。
6. Look 版本不可变、可比较、可回退（ITER-02/ITER-07）。
7. 敏感记忆默认不写；记忆走 propose→confirm→commit（MEM-01/SAFE-04）。
8. LLM/Dense/Catalog/Vision 失败可降级，核心规则推荐仍可用（OBS-05）。

## 范围红线（一律拒绝并记入 BUILD_LOG，标 R2+）

3D / 360° / 生成视频 / 实时视频展示（DEC-01/DEC-03/EVO-01）、真实支付、绕过风控的真实爬取、开放真人造型师市场、第二专业成员、社区内容流。

## 复用基线（不要重造）

- 数据：`data/fixtures/*`（fixtures_v1.0）、`data/eval/eval.jsonl`、`data/schemas/*`。
- 脚本：`scripts/generate.py`（seed=20260729）、`scripts/validate.py`。
- 测试：`tests/`。
- 环境：conda `torch128`。运行一律 `conda run -n torch128 ...`。

## 验收门槛（R1 DoD 23.1 的 Demo 子集，全绿才算完成）

- 一条命令启动 Demo；一条命令跑评测出报告。
- 评测数字达标：UrgencyAcc≥95%、高急切度 ShoppingGateAcc=100%、Item Hallucination=0、Hard Constraint Violation=0、Slot Completeness≥95%。
- AC-01/03/04/05/06/07/10/11/13/14/16/17 可演示或可测。
- README、PRD 0.3 现状说明、Demo 实现三者一致；后续能力明确标"未上线"。

## 多模型第二意见（可选）

`reviewer` 或 `supervisor` 对关键安全结论可用 `$call-grok` 做对抗复核。调用前必须先问用户用哪个模型名（不写死模型）。Grok 输出仅供参考，不替代验收。
