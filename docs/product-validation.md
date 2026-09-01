# NovelAgent 产品验证与自用上线标准

本文是面向作者的实际操作手册。目标不是证明“代码能运行”，而是证明 NovelAgent 的日常生产可以在不手工搬运草稿、不直接修改正式文件、不依赖 CLI 恢复命令的前提下稳定完成。当前第 23 章既有失败状态的强制重置是正式 Cohort 开始前的一次性受控清理例外；对应 UI 入口补齐前，必须使用本文给出的无模型命令并核验追加式 checkpoint。

## 0. 当前基线与准入顺序（2026-08-28）

本轮开发的关键实现基线包括：`d41e34a` 收口本地创作工作台与恢复加固，`7c7da1c` 增加只观察、不接管执行的 Shadow Runtime 边界，`1baa11b` 补齐 Runtime schema 预检、跨字段不变量与基于真实项目状态的验收手册；最终收口还补上了可重复的第 20 章隔离恢复夹具。针对性回归、schema/run record/provenance 测试和本地 smoke 已通过；完整测试、真实 Provider smoke、异常恢复纵切和两个十章 cohort 仍是作者手工验收门，不能用本地小测试代替。

目标书 `_重写V2` 当前已有 22 个正式正文文件。第 23 章存在一条 `RepairNotConvergingError` 失败运行，`accepted=false`、`committed=false`，并保留了失败正文候选、chapter-pipeline 与 RunRecord 证据；工作台旧任务停在 `awaiting_input`，但没有第 23 章可编辑草稿。因此它还不是正式正文，也不能直接作为 Cohort 1 的第一次有效尝试。仓库 `.active-book` 当前仍指向旧书，而不是 `_重写V2`。

基于这些事实，本轮准入顺序固定为：确认目标书与指针 → 无模型分类并关闭第 23 章旧的未提交状态 → 在隔离项目完成第 20 章恢复纵切 → 完整自动测试与真实 Provider smoke → 目标书校准并激活 Strict → Cohort 1（23–32）→ Cohort 2（33–42）。不要跳步，也不要把第 21、22 章的既有 Shadow 生成历史计入正式 Cohort。

## 1. 两道门分别证明什么

### 自动测试门

在任何付费真实生成之前依次运行：

```powershell
python -B -m pytest -q
python -B scripts/smoke_v1.py --skip-tests
python -B scripts/provider_smoke.py --providers openai claude
```

完整 `pytest` 与本地 smoke 保护代码契约、事务提交、恢复路径、Receipt/readback 和已有项目数据不被破坏。Provider smoke 用有限预算确认凭据、端点和真实模型调用链可用。任一项失败时先修复，不开始长篇生成，也不消耗十章验证预算。

### 真实模型与人工阅读门

真实连续生成验证模型在实际上下文中的连续性、文学质量、编辑成本和长程衰减。人工评分决定内容是否真的可用。自动测试不能代替阅读，偶尔成功的一次真实调用也不能代替回归测试。

## 2. 启动与安全边界

1. 安装 Python 3.12 或更新版本，并执行一次 `pip install -r requirements.txt`。
2. 双击仓库根目录的 `启动创作工作台.bat`。
3. 等待浏览器自动打开，然后从书架明确选择 `_重写V2`，不要依赖当前 `.active-book` 的旧书指针。
4. 如需运行任何 `--story-project auto` CLI，在执行前把 `.active-book` 第一行改成 `books/尸潮之下_我能提取进化词条_重写V2`，再用 `Get-Content .active-book` 核对；本步骤由作者在正式验收开始时手工完成。
5. 日常操作只使用“概览、章节、生产、待处理、历史”五个主区域；项目健康和章纲入口用于生产前准备。
6. 使用结束后关闭启动窗口，工作台随即停止。

服务只监听本机 `127.0.0.1`，并校验 Host、Origin 和仅本次启动有效的令牌。工作台的 SQLite 位于 `%LOCALAPPDATA%\NovelAgent\workbench.db`，只保存项目索引、任务、事件游标、草稿修订和产品验收记录；StoryProject、RunRecord、正文与 Receipt 仍是领域事实来源。

API Key 只能放在本机环境配置中。不要把 Key 填入浏览器表单；工作台不会在网页响应或 SQLite 中保存完整 Key，只显示脱敏的“已就绪/缺失”状态。

## 3. 第 20 章：异常恢复垂直切片

验收目标是证明场景 1、2 已完成、场景 3 部分响应的中断，可以由作者在网页中安全恢复，不重复调用不确定的 Provider，也不重复提交正文。

当前 `_重写V2` 已正式推进到第 22 章，所以本纵切必须在专用恢复测试项目中进行。不要删除、回退、复制或伪造目标书的第 20 章正式文件来制造故障，也不要在生产书上临时注入。仓库提供了一个只创建新目录、不调用模型、不更新工作台数据库且拒绝覆盖既有路径的合成夹具生成器：

```powershell
$fixture = python -B scripts/prepare_chapter20_recovery_fixture.py | ConvertFrom-Json
$fixture | Select-Object status, synthetic, target, chapter_index, expected_reusable_scene_count, expected_next_scene_index, unresolved_attempt_count, provider_calls
```

输出必须为 `status=ready`、`synthetic=true`、`chapter_index=20`、`expected_reusable_scene_count=2`、`expected_next_scene_index=3`、`unresolved_attempt_count=1`、`provider_calls=0`。脚本会先在临时副本中实际执行一次无模型恢复分类，只有确认“复用场景 1、2，从场景 3 继续”后才发布 `$fixture.target`；发布后的项目仍保持未分类，供网页验收。默认目标是 `books/_validation_chapter20_recovery`，如果它已经存在，脚本会安全失败；先核对并导入既有夹具，或使用 `--target books/<新的隔离目录名>` 创建另一个新目录，绝不能用目标书路径。

这个夹具的两个已结算场景和场景 3 部分响应都是明确标记的合成证据，只证明恢复 UI、Receipt/哈希边界、断点复用和状态持久化；它不计入真实 Provider smoke、文学质量或正式 Cohort 证据。一次性准备命令发生在网页纵切之前，不是恢复命令，也不会生成恢复 checkpoint。

1. 启动工作台，从书架导入 `$fixture.target`，名称标记为“第20章恢复验收（合成）”；确认下一章为 20，并再次确认项目路径不是正式 `_重写V2`。
2. 在“待处理”选择该项目的第 20 章恢复项，核对页面显示本章可恢复、正式正文不存在并等待无模型分类；分类前不要把任何场景当成可信内容。
3. 点击安全恢复。该步骤只读取现有锁、调用记录和草稿证据进行分类，不调用模型；结果必须显示复用 2 个场景、下一场景为 3，且新增 Provider 调用为 0。
4. 明确确认从场景 3 继续。只有这一步之后才允许为剩余场景和后续审核调用真实模型；场景 1、2 不得重复生成。
5. 核对页面显示的已完成场景、下一场景和保留草稿。若草稿需要调整，在网页编辑后保存；不要直接编辑正式正文文件。
6. 点击验证。若结果仍为 `needs_revision`、`blocked` 或 `error`，任务必须继续暂停，修订后再次验证。
7. 验证通过后检查草稿正文，再点击“确认提交并继续”。进入人工暂停分支后，即使修复已通过，也必须保留这一步作者确认，不得静默提交。
8. 在“历史”确认事务提交成功，并能看到 Receipt 与正文 readback；刷新浏览器或重启工作台，不应重复调用模型或重复提交。

如果紧急停止发生在 Provider 状态不确定时，不要点击自动重试。先执行安全恢复分类，再由作者决定保留响应、修订还是重启本章。

第 20 章通过条件：导入夹具后，作者全程不查看运行 JSON、不手工搬草稿、不输入恢复命令；无模型分类严格为 `provider_calls=0`，场景 1、2 不重复生成；正式正文只由既有事务提交链写入；刷新和服务重启后暂停状态、草稿与事件均保留。

## 4. 生产前准备

1. 确认工作台选中 `_重写V2`，需要 CLI 时确认 `.active-book` 也指向该书。
2. 在“待处理”对现有第 23 章失败运行先做无模型安全分类，确认所有 Provider 调用均已结算。正式 Cohort 需要新的干净尝试；当前工作台 UI 尚未暴露强制重置开关，因此关闭工作台后，在仓库根目录执行下方受支持的无模型命令。它只追加 `action=reset` 的恢复标记，不删除失败 RunRecord、草稿版本或事件历史，也不修改正式正文与快照。命令成功后重新打开工作台，在“生产”停止旧的已暂停任务；暂停态停止会直接进入 `cancelled`，不得产生新 Provider 调用。最后确认：正式正文仍止于第 22 章、下一章为 23、没有不确定 Provider 调用、没有未结束生成任务或未解释的锁。

   ```powershell
   $reset = python -B main.py --story-project "books/尸潮之下_我能提取进化词条_重写V2" --recover-locked-chapter --locked-chapter-reset --output-json | ConvertFrom-Json
   $reset | Select-Object ok, action, provider_calls, checkpoint_path
   $marker = Get-Content -Raw -LiteralPath $reset.checkpoint_path | ConvertFrom-Json
   $marker | Select-Object action, source_run_id, discarded_run_ids, resolved_execution_ids
   ```

   第一段输出必须包含 `ok=true`、`action=reset`、`provider_calls=0`；恢复标记中的 `discarded_run_ids` 必须包含旧的第 23 章未提交运行。任一条件不满足都应停止验收，不要手工删除运行文件或工作台数据库记录。
3. 在“项目健康”完成目标书校准，并显式激活 Strict。未激活时只能做实验运行；StoryProject 语义 Shadow 结果不能计入生产 cohort。
4. 在章纲页提出第 23–32 章的十章提案。逐章检查标题、核心事件、必写节拍和结尾压力，必要时编辑。
5. 批量确认十章章纲。确认会冻结连续章号、字段、项目状态和文件 SHA；之后外部修改章纲会暂停队列并使本轮基线失效。
6. 事先冻结本轮模型、Provider、生成配置、代码提交和周预算。开始后不得中途替换。建议在本机 `.env` 设置 `NOVELAGENT_RUNTIME_MODE=shadow` 采集节点证据；它只观察现有执行链，不等同于 StoryProject 语义 Shadow，也不会让未激活 Strict 的运行变成生产证据。

校准证据必须来自真实的目标书测量，工作台不会伪造或自动填满门槛。取得现有校准流程产出的合格 JSON 后，可在“项目健康”直接选择文件并显式激活，无需输入绝对路径或执行激活 CLI。工作台会再次验证报告哈希、目标书身份、全部证据阈值以及解析器/语义版本；任一项失败时继续保持实验模式。

连续生产的预期行为：`pass` 或 `warning` 会事务提交、验证 Receipt/readback 后自动进入下一章，且 warning 必须进入历史和批次总结；`needs_revision`、`blocked` 或 `error` 会立即暂停，等待编辑、恢复、重验证和作者确认。

## 5. 两个固定十章 cohort

### Cohort 1：第 23–32 章

1. 在“生产”创建范围严格为第 23–32 章的 cohort，并填入事先冻结的周预算。
2. 启动与 cohort 相同范围、相同模型和相同配置的连续生成。
3. 每章完成后记录九项 1–5 分评分、实际编辑分钟数、是否需要人工流程介入、自动修复次数、实际费用和严重缺陷。第 23、26、32 章对应本轮第 1、4、10 章，必须重点复核。
4. 十章全部完成后结束并验收本轮。不得在未满足条件时挑选成功章节宣称通过。

### Cohort 2：第 33–42 章

只有 Cohort 1 完整通过后，才按同一流程提出并冻结第 33–42 章章纲，创建一个全新的 cohort。模型、配置和预算可以在两轮之间重新决定，但 Cohort 2 开始后再次冻结。第 33、36、42 章必须重点复核。

两轮必须各自包含十个连续章节；不能把跨轮的成功章节拼成一个通过结果。

## 6. 每章人工记录

九项评分都使用整数 1–5 分：

1. 情节推进（`plot_progression`）
2. 前后连续性（`continuity`）
3. 人物一致性（`character_consistency`）
4. 世界设定一致性（`world_consistency`）
5. 文笔质量（`prose_quality`）
6. 节奏（`pacing`）
7. 对话（`dialogue`）
8. 章末钩子（`ending_hook`）
9. 总体可用性（`overall_usability`）

同时记录：

- 实际编辑分钟数：从开始人工修改到内容达到可用状态的净时间。
- 严重缺陷：人物身份/关系、数量、地点、时间线、资源或关键情节发生不可接受的错误，或出现严重连续性断裂。
- 是否人工流程介入、自动修复次数、实际费用和简短备注。

已提交章节如需安全润色，应从“章节”创建 amendment。系统保存不可变版本，并在确定性事实检查和语义等价检查通过后才把它作为当前阅读、导出和后续上下文版本；不要直接覆盖历史正文。

## 7. 单轮通过阈值

以下条件必须同时满足：

- 10/10 章均完成事务提交，并通过 Receipt 和正式正文 readback。
- 没有不确定调用、内部错误、状态链断裂或严重连续性缺陷。
- 首次通过率不低于 70%。
- 每章自动修复不超过一次。
- 九项人工评分的总均值不低于 3.8。
- 至少 8/10 章的“总体可用性”不低于 4 分。
- 编辑时间中位数不超过 20 分钟，P90 不超过 30 分钟。
- 最多两章需要人工流程介入。
- 本轮第 8–10 章的九项平均分，不得比第 1–3 章的九项平均分下降超过 0.3 分。
- 实际费用不超过开始前冻结的周预算。

任一条件失败，本轮状态为未通过；修复产品后必须创建新 cohort 重新验证。

## 8. 轮次失效条件

出现以下任一情况，本轮直接失效，不能只重跑失败章节或删除不理想结果：

- 任一严重缺陷。
- 作者或工具绕过工作台事务链，直接修改正式正文文件。
- cohort 进行中更换代码、模型、Provider、生成配置或已冻结章纲。
- 项目状态或章纲 SHA 漂移、Strict 资格失效，或以 StoryProject 语义 Shadow/实验运行冒充生产证据。Execution Runtime Shadow 只是旁路证据，不改变本条判定。
- 不确定 Provider 调用被自动重试，导致无法证明是否重复调用或重复提交。

修复后，从新的十章连续范围建立新 cohort；所有失败和作废历史都应保留。

## 9. 版本定义

### 自用 Beta

只有在工作台的建书/导入、十章规划与批准、连续生成、暂停/停止、异常恢复、编辑确认、历史追溯、健康检查和安全润色均可从网页完成，第 20 章隔离异常恢复垂直切片通过，且完整自动测试、smoke 和 Provider smoke 通过后，才达到“自用 Beta”。此阶段允许作者监督使用，但不宣称已证明二十章连续生产能力。

### 自用生产版

在代码和配置冻结的前提下，第 23–32 章与第 33–42 章两个连续十章 cohort 均完整通过上述阈值，且没有被失效条件污染，才可标记“自用生产版”。这一定义只覆盖 Windows 本地单用户生产；公网访问、多人协作、云同步、完整剧情 retcon、公开安装包、移动端和模板市场仍不在首版范围内。

## 10. 本轮剩余手工验收清单

- [ ] 工作台明确选中 `_重写V2`；开始 CLI 验收前手工更新并核对 `.active-book`。
- [ ] 对第 23 章旧失败运行先在工作台执行无模型分类，再使用本节给出的 `--locked-chapter-reset` 命令追加安全重置标记并停止旧暂停任务；确认正式正文止于第 22 章、下一章为 23，且失败证据仍可追溯。
- [ ] 运行 `python -B scripts/prepare_chapter20_recovery_fixture.py` 创建并导入新的合成隔离项目；在网页完成第 20 章异常恢复纵切，不修改目标书正式历史，也不把合成场景当作真实 Provider 或质量证据。
- [ ] 在最终冻结提交上运行完整 `python -B -m pytest -q`。
- [ ] 完整测试通过后运行 `python -B scripts/smoke_v1.py --skip-tests`，避免重复执行测试套件。
- [ ] 使用真实凭据运行 `python -B scripts/provider_smoke.py --providers openai claude`；仅当正式配置依赖 Notion 时，再把 `notion` 加入 providers 并检查 read/write/readback。
- [ ] 用下面的 PowerShell 检查 Execution Shadow。该命令每次建立独立的临时运行目录，避免本机既有 `.tmp/runtime` 的章节历史影响验收；`--output-run-json` 的输出根对象就是 RunRecord，因此字段路径是 `$run.runtime`，artifact ref 相对于 `$shadowWork/runs`：

  ```powershell
  $shadowWork = Join-Path ".tmp/manual_acceptance" ("execution_shadow_" + (Get-Date -Format "yyyyMMddTHHmmss"))
  New-Item -ItemType Directory -Path $shadowWork -Force | Out-Null
  Copy-Item -LiteralPath data/snapshot.example.json -Destination (Join-Path $shadowWork "snapshot.json")
  Copy-Item -LiteralPath data/notion_memory.example.json -Destination (Join-Path $shadowWork "memory.json")
  $run = python -B main.py --dry-run --persist-dry-run --runtime-mode shadow --snapshot (Join-Path $shadowWork "snapshot.json") --memory (Join-Path $shadowWork "memory.json") --run-dir (Join-Path $shadowWork "runs") --chapter-dir (Join-Path $shadowWork "chapters") --output-run-json | ConvertFrom-Json
  $run.runtime | Select-Object mode, authority, execution_cutover, artifact_ref, evidence_hash
  $runtimeArtifact = Join-Path (Join-Path $shadowWork "runs") $run.runtime.artifact_ref
  $artifactRuntime = Get-Content -Raw -LiteralPath $runtimeArtifact | ConvertFrom-Json
  $artifactRuntime | Select-Object mode, authority, execution_cutover, evidence_hash
  ```

  两处都必须为 `mode=shadow`、`authority=legacy_executor`、`execution_cutover=false` 且 `evidence_hash` 相同；`Test-Path -LiteralPath $runtimeArtifact` 必须为 `True`。
- [ ] 完成目标书真实校准并显式激活 Strict；冻结代码、模型、Provider、配置、章纲和预算。
- [ ] 依次运行并人工评分 Cohort 1（23–32）和 Cohort 2（33–42）；任一轮失效都保留历史并从新的连续十章范围重建，不拼接成功章节。
