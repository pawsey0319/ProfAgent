# S16A 自由文本记忆与偏好不确定性验收

结论：PASS（tester 技术门禁）；hash-bound reviewer 状态：PENDING。PENDING 时 P0/P1/P2 保持 unknown。

## 一条命令

`conda run --no-capture-output -n torch128 python scripts/tester_s16a_report.py`

唯一权威是 canonical bundle；JSON/MD 只是 projection。runner 先写两份 projection，最后以一次 atomic os.replace 提交 bundle。

直接读取未经 bundle hash 与双 projection hash 校验的 JSON/MD 不受支持；mixed pair、projection 失败或进程中断均不能成为 authoritative PASS，后续运行可 repair convergence。

## S16A 合同

- 候选提取：PASS；敏感写入 0。
- 原始文本泄漏：0；supersede 原子性：PASS。
- 单轮追问上限：1；高急不回答时推荐仍继续。
- 高急：shopping_allowed=false，Catalog actual calls=0。
- 确定性 provider 测试外部调用：0。

## 串行门禁

- focused pytest：5 passed；Memory：51 passed；Dialogue：128 passed；full：391 passed。
- Node：21 个 syntax，12 个 runtime/static 合同通过。
- fixture：validation passed: users=3 garments=50 outfits=20 catalog=50 eval=30。
- 固定 R1：Urgency=100.00%；ShoppingGate=100.00%；Catalog=0；幻觉=0；硬约束=0；Slots=100.00%。

## 审查边界

本报告只证明 tester 技术门禁。当前 reviewer=PENDING；接受 reviewer evidence 时，source_revision 是代码/测试/BUILD_LOG 结构的 clean commit，reviewed_head 必须是当前 HEAD 且为其非空线性后代，区间每个提交只能改三份固定 Task8 report artifacts。调用方只提供仓库允许根内的 fixed package/review output 绝对路径与预期 source/head，runner 从安全读取的同一份 bytes 自行解析并计算 SHA-256；只有 ancestry、逐提交路径、内容、路径与当前 HEAD 持续匹配时才保留 reviewer evidence。S16B、S16C、S16R 均未关闭。

报告不包含原始对话、敏感值、直接标识符、模型推理或外部 Provider 正文。
