# ProfAgent 跨设备开发交接

> 本文面向接手开发的 Codex/开发者。需求权威仍是 [PRD](PRD.md)，协作与安全规则以仓库根目录的 [AGENTS.md](../AGENTS.md) 为准；本文只记录如何恢复当前开发现场，不能覆盖二者。

## 1. 权威代码线与当前边界

- 权威远端：`https://github.com/pawsey0319/ProfAgent.git`
- 继续开发的远端分支：`origin/codex/s16-personal-stylist`
- 不要从 `main` 续做 S16，也不要把尚未完成的 S16 合并到 `main`。
- 本文编写前，远端分支与本地分支均指向 `149d92e418a4de684a0f0bd935a776e7b37e2334`。这是**交接文档编写前的基线**，不是永久 HEAD；提交本文本身就会让 HEAD 前进。
- 接手时以 `git fetch` 后的 `origin/codex/s16-personal-stylist` 为唯一代码基准，并要求本地 `HEAD` 与远端引用相等。最终交接提交 SHA 由 supervisor 在推送后另行报告，不在文档内建立会被自身提交破坏的自指固定值。

### 本轮 Git 上传审计摘要

fresh `git fetch --prune origin` 后，交接文档修改前的审计事实为：

- 本地 `codex/s16-personal-stylist` 与 `origin/codex/s16-personal-stylist` 均为 `149d92e418a4de684a0f0bd935a776e7b37e2334`，ahead/behind 为 `0/0`，tracked diff 与 staged diff 均为 0。
- 本地 `main` 为 `83f19a2664ec3913ad38b497f31e2898cd07188d`，相对 `origin/main` 的 `7e802bedeb234206576d680d3f6419f442cc68df` 领先 4 个提交。`main` 是权威 S16 分支的祖先，所以这 4 个提交的内容已经存在于远端 S16 分支；它们只是尚未单独推到或合并进远端 `main`。
- S16 分支相对本地 `main` 领先 123 个提交、落后 0 个，并包含本地 `main` 的全部内容。继续开发应检出 S16 分支，不能从远端 `main` 推断当前功能状态。
- 仓库没有 submodule，也没有 Git LFS 跟踪对象；普通 `git clone` 加 `git switch` 可以恢复全部 **tracked** 文件，不需要额外拉取 submodule/LFS。
- 普通 clone 不会恢复两项未跟踪开发证据 `data/manifests/wardrobe_assets_v2.json`、`data/sources/wardrobe_s16_sources.jsonl`，也不会恢复 `.gitignore` 排除的私有 quarantine、诊断证据和本地 SQLite。它们不是“已上传内容”，迁移规则见第 8 节。

在新设备的 PowerShell 中：

```powershell
git clone https://github.com/pawsey0319/ProfAgent.git
Set-Location .\ProfAgent
git fetch --prune origin
git switch --track -c codex/s16-personal-stylist origin/codex/s16-personal-stylist

$localHead = git rev-parse HEAD
$remoteHead = git rev-parse origin/codex/s16-personal-stylist
if ($localHead -ne $remoteHead) { throw "本地 HEAD 与权威远端分支不一致" }
git status --short --branch
```

预期是本地与远端 SHA 相同且 tracked worktree clean。不要把旧设备的整个 `.git`、`.worktrees` 或另一个 worktree 目录直接复制到新设备。

## 2. Windows、Conda 与依赖

仓库要求所有 Python 项目命令通过 Conda 环境 `torch128` 运行。旧设备已审计的该环境是 **Python 3.10.19**，所以新设备默认用 Python 3.10 建环境：

```powershell
conda create -n torch128 python=3.10 -y
conda run -n torch128 python -m pip install --upgrade pip
conda run -n torch128 python -m pip install -e ".[dev]"
```

`pyproject.toml` 只声明 Python `>=3.10` 和依赖版本区间；仓库没有 dependency lock、`environment.yml` 或可精确重建旧机全部包版本的清单。因此上述命令只能恢复受支持环境，不能声称位级复现旧设备。安装后必须重新运行 validator、专项/全量测试和当前阶段门禁，不能继承旧设备的 PASS。

只有需要连接 PostgreSQL 时才安装可选驱动：

```powershell
conda run -n torch128 python -m pip install -e ".[dev,postgres]"
```

基础恢复检查：

```powershell
conda run --no-capture-output -n torch128 python --version
conda run --no-capture-output -n torch128 python scripts/validate.py
conda run --no-capture-output -n torch128 python -m pytest -q
```

不要直接使用激活后的裸 `python` 来替代这些命令；CI/验收记录统一以 `conda run -n torch128 ...` 为准。

## 3. 启动、浏览和固定评测

一条命令启动 FastAPI 与中文 Web Demo：

```powershell
conda run --no-capture-output -n torch128 python -m profagent
```

浏览器打开 `http://127.0.0.1:8000`，按 `Ctrl+C` 停止。需要完全离线验证界面时，可在同一个 PowerShell 会话禁用 CPA 文本：

```powershell
$env:PROFAGENT_CPA_TEXT_ENABLED = "false"
conda run --no-capture-output -n torch128 python -m profagent
```

一条命令运行 R1 固定评测并输出 JSON 和 Markdown：

```powershell
conda run --no-capture-output -n torch128 python -m profagent.eval
```

输出位置：

- `reports/eval/r1_demo_v1.json`
- `reports/eval/r1_demo_v1.md`

评测会更新报告文件。比较或提交前要检查 `git diff`，不要误把一次本地运行造成的非预期报告漂移当作功能修改。

## 4. CPA 文本、Vision 与图片配置

任何密钥、个人网关配置和 `.env` 都不得提交。推荐在新设备的当前 PowerShell 会话设置环境变量，或在仓库外维护本地 JSON。配置优先级是 `PROFAGENT_*` 环境变量，其次兼容的 `GROK_*` / `CPA_*` / `OPENAI_*`，最后才是 `PROFAGENT_CPA_CONFIG` 或 `~/.codex/skills/call-grok/config.local.json` 中的 `base_url`、`api_key`。

| 变量 | 来源/用途 | 是否必需 |
|---|---|---|
| `PROFAGENT_CPA_BASE_URL` | OpenAI-compatible CPA `/v1` endpoint；未设置时默认本机 `http://127.0.0.1:8317/v1` | 真实 CPA 调用必需；离线 Demo 否 |
| `PROFAGENT_CPA_API_KEY` | CPA runtime secret | 网关要求鉴权时必需；绝不入库 |
| `PROFAGENT_CPA_CONFIG` | 指向仓库外本地 JSON，仅读取 `base_url` 与 `api_key` | 可选替代来源 |
| `PROFAGENT_GROK_MODEL` | 文本和 Vision 的逻辑模型；代码只接受精确 `grok4.6` | 可省略并使用固定默认；不得改成其他值 |
| `PROFAGENT_CPA_TEXT_ENABLED` | 是否允许普通对话走 CPA | 可选，默认 `true`；离线验证设 `false` |
| `PROFAGENT_CPA_IMAGE_ENABLED` | 是否允许 Static2D 图片 transport | 可选，默认 `true`；**这不构成真实调用授权** |
| `PROFAGENT_CPA_IMAGE_MODEL` | 独立图片模型；只接受固定 `grok-imagine-image-quality` | 可省略并使用固定默认；不得模糊选模 |
| `PROFAGENT_CPA_IMAGE_DOWNLOAD_HOSTS` | CPA 返回远程 URL 时允许下载的受信 CDN host，逗号分隔 | 仅 URL 响应需要；`b64_json` 不需要 |
| `PROFAGENT_CPA_DIALOGUE_BUDGET_SECONDS` | 对话预算，只能收紧代码上限 | 可选 |
| `PROFAGENT_CPA_IMAGE_TIMEOUT_SECONDS` | 图片请求预算，只能收紧代码上限 | 可选 |
| `PROFAGENT_VISION_INTERACTION_BUDGET_SECONDS` | Vision 交互预算，只能收紧代码上限 | 可选 |
| `PROFAGENT_DIALOGUE_TTL_SECONDS` | 进程内对话 session 滑动 TTL，只能收紧 | 可选 |
| `PROFAGENT_DATABASE_URL` | Memory SQL 真值；未设置时使用 `.profagent/memory.sqlite3`，生产可指向 PostgreSQL | 本地 Demo 可选；跨主机服务应显式配置 |
| `PROFAGENT_WARDROBE_CATALOG_MANIFEST` | 显式选择衣橱目录 manifest | 可选；默认使用已跟踪的 v1 manifest |
| `PROFAGENT_HOST` / `PROFAGENT_PORT` | HTTP 监听地址/端口 | 可选，默认 `127.0.0.1:8000` |

Vision 没有一个可以绕过流程的独立“开启即调用”开关；它复用受控 CPA base/key、`grok4.6` 和显式用户同意，仍受 adapter、模型 allowlist、输入验证及当次授权约束。不要把图片模型用于文本，也不要把文本 transport 当作生图模型。

## 5. 已上传且可从仓库恢复的内容

以下内容在 `codex/s16-personal-stylist` 中可直接恢复：

- R1 Stylist Demo 主流程和 R1 DoD Demo 子集实现；
- S12 SQL Memory：本地默认 SQLite、生产可选 PostgreSQL，受控 `propose → confirm → commit`；
- S13 CPA 文本 4.6 合同、衣橱目录图展示和相关安全边界；
- S14 连续多轮推荐、明确 1–3 套、推荐级静态 2D 参考、按类别折叠的衣橱；
- S15 Memory 路线 A：SQL 真值、事务 outbox、可重建软索引、weighted RRF 与确定性 `structured_rerank_v1`；
- S16A 自由文本记忆候选、偏好不确定性追问及 Task8 finalization；
- 3 用户 / 50 衣物 / 20 套装 / 50 Mock 商品 / 30 评测样本的 fixture 基线、schemas、tests 和固定 seed `20260729`；
- 50 张已跟踪的合成衣橱 v1 目录图和对应 manifest；
- S16 120 件女装候选数据的生成器、validator、测试和安全合同代码；这些不等于候选图片资产已经完成或公开。

准确状态以 [BUILD_LOG](../BUILD_LOG.md) 为准。历史验收数字是对应历史 HEAD 的证据，新设备或新 HEAD 必须重新运行适当门禁，不能直接继承“已通过”结论。

## 6. S16 Ruling U/V 的准确含义

### Ruling U：已关闭的兼容层工作

Ruling U 根据绑定到 CLIProxyAPI `7.2.97` / commit `42f36b94e0805a9897c3aa3be46a2b124be0057e` 的公开源码，确认 CPA Chat Completions serializer 与项目旧 profile 存在版本化 schema drift。随后完成 mock-only TDD：已知 metadata 严格验证后丢弃，未知字段、错误类型或非法状态继续 fail-closed；Vision、ingestion 和诊断路径都传播闭合的 v1/v2 profile。最终历史门禁为 `1041 passed, 1 skipped`，reviewer `P0/P1/P2=0/0/0`。

这只说明兼容层在绑定版本和 mock 合同下关闭，不能证明图片已生成、资产已摄取或 S16B 已完成。

### Ruling V：只证明一次 Vision 兼容验证成功

在历史 HEAD `cf3016a24a0114e41b0ed7f3bdfff5495c28ef2c` 上，唯一授权的 Vision-only 诊断执行一次：exit `0`、约 `6.28s`、`vision_call_count=1`，归一化 provenance 为 requested `grok4.6`、resolved `grok-4.6-build`、`model_verified=true`。事后审查确认冻结证据未变化、无 Image 调用、无写入、无摄取、无晋升、无重试。

因此 Ruling V **仅证明 Vision 响应兼容和受控模型验证链路在那次运行成功**。它不证明：

- Grok Image 生图成功或图片质量合格；
- 任何 S16 女装候选已经进入公开 manifest；
- g051 或其他隔离资产为 `ready`；
- S16B、S16C 或整个 S16 完成；
- 历史 HEAD 绑定的真实调用授权可以在新 HEAD/新设备复用。

## 7. 未完成且不得描述为已上线

- **S16-0**：完整迁移冻结仍未正式关闭。
- **S16B**：女装 V1 授权来源、图片资产、摄取/晋升和 UI 覆盖未完成。已有生成器、validator、mock 诊断和单次 Vision 兼容证据，但不等于 Image 或批量资产链路完成。
- **S16C**：套装图上约 450ms 长按/键盘选择槽位、owner-bound 候选、单槽 CAS 创建不可变 Look vN、再异步生成版本绑定 Static2D，均未完成。
- **S16R**：S16 专项、完整回归、真实浏览器、JSON+MD 报告以及 Codex/Grok 联合终审未关闭。
- LangMem、Mem0、Graphiti、真实语义 embedding/Cross-Encoder 是 S17+ 评估项，未上线。
- 完整 R2 真人身份保持试穿、真实商品导入/购物/支付、第二成员、真人专家、社区、3D、360°和视频均未上线。

## 8. Git 不包含的本地状态

Git 不负责跨设备恢复运行时状态。本文编写时，旧设备还有以下未上传内容：

| 路径 | 状态 | 用途 |
|---|---|---|
| `.profagent/memory.sqlite3` | `.gitignore` 排除 | 本地 Demo 已确认 Memory；不迁移也可启动，但本地记忆会从空库开始 |
| `data/assets/private_quarantine/` | `.gitignore` 排除 | 私有/隔离诊断图片；不得公开提交或当作已授权资产 |
| `data/assets/cpa_generated_quarantine_diagnostics.jsonl` | `.gitignore` 排除 | 冻结诊断证据；不得当公开来源数据 |
| `data/manifests/wardrobe_assets_v2.json` | 未跟踪 | S16 候选 manifest，仍非权威公开资产 |
| `data/sources/wardrobe_s16_sources.jsonl` | 未跟踪 | S16 候选来源记录，仍待 S16B 收口 |
| CPA 本地 JSON、`.env`、API key | 必须留在 Git 外 | 只可在新设备重新安全配置，不能随项目归档 |

这解释了为什么只 clone 仓库不能恢复旧设备的 Memory 和 S16 私有诊断现场；它不会影响从已跟踪 fixture 启动基本 Demo。

### 可选的安全人工迁移

只有确实要继续 S16B 取证或保留本地 Demo 记忆时才迁移上述文件：

1. 先停止 ProfAgent、测试、CPA 诊断和所有使用 SQLite 的进程。
2. 绝不打包 `.env`、`config.local.json`、`*.key`、`*.pem`、shell history 或 credential。
3. 用受密码保护的加密介质/端到端加密通道传输；不要临时提交 Git，也不要上传公共网盘。
4. 保持相对路径。SQLite 只能在无写入进程时复制；若改用 PostgreSQL，使用数据库官方 dump/restore，不复制服务器数据目录。
5. 传输前后分别运行 `Get-FileHash -Algorithm SHA256`；不一致就停止，不要导入、晋升或继续真实调用。
6. 新设备上限制文件访问权限；检查内容后再让应用读取。缓存、`.worktrees`、`__pycache__`、`.pytest_cache`、`reports/runtime/` 不迁移。

Ruling V 事后冻结的六项开发证据 SHA-256 为：

```text
data/manifests/wardrobe_assets_v2.json
  6e71d9ca9cd3e5e9c7a037c56498303ece03021179505dfd21ed2a5c218f4682
data/sources/wardrobe_s16_sources.jsonl
  eca7fd576e10853388cb2de624698b06bb8a49bc8cb443e11e63118599ef19e2
data/assets/cpa_generated_quarantine_diagnostics.jsonl
  0f981797ee2dc37d41a0563e1d5d34b8d31802ac5589beae4aa223309843dd28
private quarantine PNGs
  46c5d710493b59daa823f72d9c68ab18d059e313ead69531c4cb70c2058a4e98
  afc00f9705193d7d0ea2072989b07bdb6b74ceae757b1f20ba5f8575db662abf
  33c9439e4516e571925689f0ef501a9771f3d25888ff24ecbe044c6b16d19749
```

这些 hash 是防止误用/漂移的证据，不是 license、摄取或公开授权。`.profagent/memory.sqlite3` 是可变化的运行时数据库，不应写死为长期固定 hash。

## 9. 恢复前后检查单

### 只使用 Git 的新环境

```powershell
git fetch --prune origin
git rev-parse HEAD
git rev-parse origin/codex/s16-personal-stylist
git status --short --branch
conda run --no-capture-output -n torch128 python scripts/validate.py
conda run --no-capture-output -n torch128 python scripts/validate_s16_wardrobe.py
conda run --no-capture-output -n torch128 python -m pytest -q
```

### 迁移本地文件后

```powershell
Get-FileHash -Algorithm SHA256 .\data\manifests\wardrobe_assets_v2.json
Get-FileHash -Algorithm SHA256 .\data\sources\wardrobe_s16_sources.jsonl
Get-FileHash -Algorithm SHA256 .\data\assets\cpa_generated_quarantine_diagnostics.jsonl
Get-ChildItem .\data\assets\private_quarantine -Recurse -File |
  Get-FileHash -Algorithm SHA256

git status --short --branch
```

预期：只有明确人工迁移的 manifest/source 显示为未跟踪；私有 quarantine、diagnostic 和 `.profagent` 应继续被 `.gitignore` 排除。任何 tracked diff、未知未跟踪文件、hash 漂移或残留服务进程都应先调查，不要直接继续 provider、摄取或晋升。

## 10. 后续执行顺序

继续开发必须遵守 `AGENTS.md` 的多 agent 编排：

1. 主会话先委托 `supervisor`，由其在 `BUILD_LOG.md` 冻结当前小阶段、HEAD、目标、AC、禁止事项和验收口径。
2. `supervisor` 再按需要委托 `backend` / `frontend`；共享合同先冻结再并行，避免同时修改。
3. 可运行里程碑后由只读 `reviewer` 报 `[P0]/[P1]/[P2]`；发现回派原 owner，不能由 reviewer 直接修。
4. 功能就绪后由 `tester` 扩展测试并运行专项、全量、固定 eval 和浏览器验收。
5. 只有门禁全绿、文档与实现一致，才由 supervisor 收口并决定是否推送；S16 完成前不合并 `main`。

建议下一步仍按 `S16B → S16C → S16R`：先完成合法女装来源/资产的生产摄取闭环，再做图上长按替换与 Look vN，最后统一回归和终审。不要因为已有单次 Vision PASS 就跳到批量生图或 UI 宣称完成。

在新设备开启新的模型会话时，可用下面这段最小交接提示；它只是导航，模型仍必须亲自核验仓库事实：

```text
你是 ProfAgent 的 supervisor。当前权威开发线是远端分支
codex/s16-personal-stylist，不是 main。先完整阅读 AGENTS.md、
docs/PRD.md（尤其 0.3、6、9、11、12、16、23.1）、BUILD_LOG.md、
README.md 和 docs/DEVICE_HANDOFF.md；再核对本地 HEAD 等于远端分支且
tracked worktree clean。先报告已完成/未完成和本地缺失资产，不要凭历史
验收数字宣布当前 HEAD 通过。继续按 supervisor → backend/frontend →
reviewer → tester 编排。未经本轮用户明确授权和 fresh HEAD-bound preflight，
不得执行真实 CPA、Vision、Image、摄取、晋升、批量生成或重试。
```

## 11. 不可放宽的验收和安全门槛

必须维持可演示或可测试的 `AC-01/03/04/05/06/07/10/11/13/14/16/17`，并至少满足：

- `now/today/unknown → high`；高急 `shopping_allowed=false`，Catalog 服务端双层阻断且实际调用为 0；
- 衣物/商品 ID 必须来自当前 owner/本次工具白名单，幻觉为 0；
- 禁忌色、非 available、季节/天气必需项在召回前过滤且不得被排序恢复；
- Scorecard 只评价“当前穿搭 × 当前目标”，不得评价颜值、身材、体重、年龄、性吸引力或人的价值；
- 每轮调整不超过 2 项，拒绝建议不换说法重复；Look v1→vN 不可变、可比较、可回退；
- 记忆坚持 `propose → confirm → commit`，敏感内容默认不写；硬记忆 SQL 精确读取，不能交给 RRF 决定；
- LLM/Dense/Catalog/Vision/Image 失败必须诚实降级；文字推荐不因图片失败消失；
- R1 指标保持：UrgencyAcc≥95%、高急 ShoppingGateAcc=100%、Item Hallucination=0、Hard Constraint Violation=0、Slot Completeness≥95%；
- 只做 2D 静态参考；禁止 3D、360°、生成/实时视频、真实支付、绕过风控的真实爬取、开放真人市场、第二成员和社区流。

## 12. Provider 操作禁令

在新设备、新 HEAD 或新阶段，**不得直接执行真实 CPA、Vision 或 Image 调用**。历史 Ruling V 的一次性授权已消耗且只绑定当时 HEAD，不能继承。

每次真实调用前都必须：

1. 获得用户对当次具体调用和预算的明确授权；
2. 由 tester 做 fresh、只读、绑定当前完整 HEAD 的 preflight；
3. 由 fresh read-only reviewer 核对命令、模型、调用次数、输入 hash、证据 hash、输出闭集、0 retry/0 write/0 ingestion/0 promotion 和停止条件；
4. 只执行被逐字批准的命令和次数；失败立即停止，禁止自动重试或顺手扩大到 Image/batch；
5. 运行后重新核验 Git diff、证据 hash、marker、进程和调用计数，再写 BUILD_LOG。

同理，不得把未跟踪 manifest/source 或 private quarantine 手工改成 `ready`，不得绕过 ingestion validator，不得把来源不明的网络图片放入公开静态服务。Grok 外部审查如需调用 `$call-grok`，必须先让用户确认当次模型 ID；Grok 输出只是咨询证据，最终仍由 Codex supervisor 依据本地可复现证据收口。
