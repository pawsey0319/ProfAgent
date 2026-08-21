# S15 Memory 路线 A 独立验收

结论：PASS；tester P0/P1/P2 = 0/0/0。未调用 CPA，未触碰 8000，未修改 production/web、fixtures、eval 真值或阈值。

## 一条命令

`conda run --no-capture-output -n torch128 python scripts/tester_s15_report.py`

报告脚本启动时先原子写入 PENDING；仅全部门禁通过后原子替换为 PASS。任何异常都会原子替换为 FAIL 并返回非 0。

## 串行门禁

- S15 专项：19 passed；Memory 相关：44 passed；full：300 passed。
- Node：14 个 syntax；全部 7 个 runtime/static 合同通过。
- 固定 eval：Urgency 30/30；高急 Gate 10/10；Catalog 0/10；幻觉 0/515；硬约束 0/423；Slots 74/74。
- 随机非 8000 HTTP 完成 propose→confirm→list→delete、重启/双实例 SQLite、owner/namespace/ACL、future/expire/supersede/quarantine 和无正文删除回执。
- 真实 Chrome：web S15 Memory real-browser smoke: PASS valid-display=1 invalid-removal-only=1 private-body-rendered=0。

## 受控合成检索基准

该基准只验证确定性合同和受控诊断，不代表真实用户质量提升、生产语义 embedding 或 Cross-Encoder 效果。

| 阶段 | Recall@5 | Recall@10 | MRR |
|---|---:|---:|---:|
| BM25 | 0.833333 | 1.000000 | 1.000000 |
| Dense surrogate | 0.833333 | 1.000000 | 1.000000 |
| Weighted RRF | 0.500000 | 0.791667 | 0.687500 |
| RRF + structured_rerank_v1 | 0.708333 | 0.833333 | 1.000000 |

四路均冻结 Top 20；公式、权重、权威 Scene、重复确定性与 hard-memory exclusion 全绿；8 次本地调用 P95=4.89 ms。污染计数为 0。

LangMem、Mem0、Graphiti、Cross-Encoder 和真实用户语义提升均未上线、未在本报告中宣称。
