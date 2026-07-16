# NovelAgent 修订版升级与 Git 发布计划

## 一、复审结论

原计划方向正确，但存在必须修正的结构问题，不能原样实施：

- 现有 `strict` 代表“Markdown 语义解析权威”，不能直接改成“事件日志权威”。
- 活动小说迁移不能早于 Persistence v2，否则会出现身份已切换、基线状态未原子发布。
- 自主生成不能只逐章临时规划，必须先生成跨章 `RunArcPlan`。
- Provider 响应返回但尚未落盘时无法证明调用结果，不能承诺“绝不重复”，必须进入 `uncertain` 状态。
- 细纲不应提前成为独立 canonical 提交；应先作为会话检查点，正文通过后与章节一起发布。
- 质量阈值不能靠 LLM 自报 confidence 直接硬编码，必须使用独立校准集和保留测试集。
- Persistence v2 还需补齐路径安全、prepare 崩溃窗口、项目移动和完整 read-set 校验。

以下计划是完整替换版。

## 2026-07-16 执行范围修订

本修订优先于下文原有的真实 E2E 安排：

- 当前目标不再包含由 Codex 执行任何计费的真实 Provider 自治测试，具体包括真实单章、4 章、10 章和至少 20 章测试。
- 这些测试没有取消；它们改为当前目标完成后由操作者手动执行。Codex 在当前目标内只保留测试工具和操作说明，不获取凭据、不发起调用。
- 操作者之后交回脱敏报告时，Codex 只分析报告完整性、连续性、Intent/Receipt、Delivery、失败分类和 SLO，不代替操作者重跑。
- 单元测试、v1 冒烟和 50 章确定性离线模拟仍属于当前工程验收范围。
- 缺少真实报告继续意味着“真实验证：否”和“默认启用：否”，但不再阻塞当前目标本身的完成。
- 旧书事件权威迁移功能暂时搁置并移出当前目标、当前执行待办和完成条件，改记为延期事项。已有代码、Schema、测试、文档、只读预览及历史记录全部保留；当前目标不继续补功能、不要求六项人工裁决、不生成 `MigrationApproval`、不执行迁移或激活。以后只有用户明确恢复后才重新开始，并须按当时文件生成新的只读预览，不能直接依赖可能已过期的旧预览。

## 二、先整理并上传当前 Git

当前事实：

- 分支为 `feature`，与 `origin/feature` 完全同步。
- 有 15 个已跟踪文件改动，共约 `+327/-16`。
- 完整 900 项单元测试通过，2 项跳过。
- CI 同款 `smoke_v1.py` 通过。
- `git diff --check` 通过，未检出 API Key、Token 或私钥。
- 当前补丁包含已知临时方案，因此只作为“可靠性改造前 checkpoint”，不宣称根因已解决。

### 1. `feature` 分支提交

先拉取远端引用；若 `origin/feature` 已前进则停止，不做 force push。

增加忽略规则：

```gitignore
/.active-book
/books/
/pytest-cache-files-*/
```

小说分支中的规范内容使用显式 force-add；代码分支默认不暴露任何小说或运行产物。

当前字符截断、bigram 阈值和字节 Token 估算均为待替换方案，不视为最终可靠性机制。

分三次提交：

1. `checkpoint: bound long-form generation contexts`
   - 当前 context budget、prompt、chapter pipeline、repair payload 及对应测试。
2. `checkpoint: harden Chinese validation paths`
   - 当前 coverage、validator、LLM JSON 恢复、Schema 及对应测试。
3. `docs: record reliability upgrade and ignore local artifacts`
   - 修订计划与忽略规则。

提交前重新执行：

```powershell
python -B -m unittest discover -s tests
python -B scripts/smoke_v1.py
git diff --cached --check
```

检查暂存区无凭据、绝对用户路径、runtime 备份或模型运行产物后，非强制推送到 `origin/feature`。

### 2. 单独建立小说分支

从更新后的 `feature` 创建：

```text
codex/novel-shichao-baseline
```

该分支只添加：

- 总纲与第 1—10 章细纲。
- 第 1—10 章正文。
- 作品、能力、地点和角色设定。
- 四份追踪文件。
- 清理后的 `.novelagent/project.json`，将绝对 `root_hint` 改为 `"."`。
- 一份 baseline 说明，注明这是测试生成结果，仍有男声/女声矛盾和状态漂移，未通过最终迁移审计。

明确排除：

- `.active-book`。
- `.novelagent/runtime/` 下 594 个运行、事务、备份和输入产物。
- 所有 API 配置、本机绝对路径和 Provider 原始响应。

提交为：

```text
content: archive shichao generation baseline
```

推送到 `origin/codex/novel-shichao-baseline`。随后切回 `feature`，恢复本地被忽略的小说工作目录并核对正文、追踪及 runtime 未丢失，最终保证 `feature` 工作区干净。

## 三、升级实施顺序

### 阶段 1：运行证据、模型调用与质量基础

- 新增 `ExecutionProvenance`，记录运行时代码 bundle 哈希、Prompt/Schema 哈希、Python 与依赖版本、非敏感配置、provider/model 和功能开关。
- 禁止记录密钥、完整环境变量、Authorization、原始 dirty diff 和本机绝对路径。
- Provider 客户端改为返回结构化 `ModelResponse`：
  - text、usage、finish reason、request id、实际 model 和 endpoint 类型。
- 每次物理网络尝试前写入 `ModelCallIntent` 并预留预算；响应后立即写 `ModelCallReceipt`。
- Token 计数模式区分：
  - `provider_exact`
  - `model_tokenizer`
  - `calibrated_estimate`
- OpenAI 兼容端点和未知模型不得把 `tiktoken` 结果标为 exact。
- 预览阶段必须检查 3000—4500 中文字与模型输出 Token 上限是否相容。
- 接通全任务 `RunBudgetTracker`，覆盖细纲、正文、润色、校验和修复的所有物理调用。
- 删除 JSON 和上下文的字符头尾截断，改为完整结构条目选择与相关性检索。
- 统一 Review、Gate、Repair 和 Quality Decision：
  - warning-only 为 advisory。
  - Gate 不得被 Quality Decision 覆盖。
  - 两个质量门都通过才允许提交。
- 第一章豁免依据是“没有已提交上一章”，不是单纯判断章号。
- LLM 发现必须绑定可验证正文证据和事实 ID；校准完成前，medium 默认只作 advisory。

### 阶段 2：建立独立事件权威

不创建平行事件系统，直接升级现有链路：

```text
MemoryPatch
→ MemoryEvent
→ MemoryEventBatch
→ CanonicalMemory
```

新增 `ProjectIdentity 2.0` 权威配置：

```text
authority.mode:
  legacy_markdown_v1
  event_v1

authority_epoch
head_event_hash
activation_receipt
minimum_writer_contract
```

规则：

- `story_state_mode` 只保留旧书兼容，不再承担事件权威含义。
- `event_v1` 启用后，Markdown Parser 只能审计，不能覆盖 CanonicalMemory。
- CanonicalMemory 升级为类型化 Schema，覆盖人物、位置、关系、伤势、库存、资源、词条、侵蚀、时间和伏笔生命周期。
- Event 必须带 before/after、precondition、正文 hash、证据区间、Schema 版本、reducer 版本和 authority epoch。
- Snapshot 和追踪 Markdown 只是可重建投影。
- 新书支持空状态 Genesis，不要求先生成 10 章 shadow。
- 旧书先运行 shadow reducer 生成候选 Projection，但此阶段不激活活动小说。
- 已发布正文默认不可原地改；修改通过 `amend/import/retcon` 创建审计事件。修改历史章节会让后续事件和细纲失效，并生成影响报告。

### 阶段 3：Persistence v2.1、恢复与 File Delivery

- 建立 v1/v2 Backend 兼容层；v2 失败不得静默回退 v1。
- v2 补齐完整 StoryProject read-set 在 prepare、pre-apply 和 pre-marker 阶段的重复验证。
- Persistence prepare 调整为：
  1. 临时目录完整 stage、校验和 fsync。
  2. 原子发布 journal。
  3. 最后注册 pending entry。
- marker 前失败允许 CAS 回滚；marker 后只允许前滚恢复。
- 所有路径使用受控 `PathRef` 和逻辑 root UUID。
- Windows 下拒绝 symlink、junction、reparse point、TOCTOU 路径替换和允许根目录外写入。
- 项目移动必须使用显式 `remap-roots`；存在 pending 事务或活动 session 时禁止移动。
- 非 canonical 阶段检查点和 canonical 章节事务在本阶段统一实现，避免两套恢复系统。
- Final RunResult 在 Receipt 后保持不可变；Delivery 状态只能写独立 Job 和 Attempt Receipt。
- 事务内发布完整无凭据 `DeliveryIntent`，Receipt 后幂等物化 DeliveryJob。
- File Delivery 载荷固定为本章 Canonical Event Batch 及正文 hash，目标只能来自预配置 export profile，且路径必须包含 run 或 chapter 唯一标识。
- Readiness 拆成：
  - `OutlineReadiness`：允许当前没有细纲。
  - `DraftReadiness`：要求有效细纲 Stage Receipt、authority head 不变。
  - 后续每个模型阶段均验证自己的 Stage Authorization 和输入 digest。
- required Delivery 失败时章节为 `local_committed_delivery_blocked`，计入本地提交数，但禁止开始下一章。

Provider 不确定窗口固定处理：

- 已有 ModelCallReceipt 时绝不重发。
- 只有 Intent、没有 Receipt 时标记 `provider_call_uncertain`。
- Provider 无幂等查询能力时默认暂停，不能自动无限重试。
- 超时调用按预留上限计入预算。

### 阶段 4：跨章规划、自动细纲与耐久会话

自然语言采用预览后执行：

```powershell
python main.py --story-project auto --instruction "<文字指令>"
python main.py --story-project auto --execute-plan <plan.json>
```

同时提供：

```text
--session-status
--resume-session
--cancel-session
--abandon-session
```

指令解析器只能从可信配置中选择：

- StoryProject。
- provider/model profile。
- File Delivery profile。
- 预算上限和质量策略。

自然语言或小说正文不得授权任意系统路径、Notion 写入、环境变量、凭据或提高预算。

执行计划首先生成 `RunArcPlan`：

- 为目标章节区间分配主线节点、关系变化、升级节奏、资源代价和伏笔播种/回收。
- 每章细纲从 ArcPlan 领取目标。
- 已提交目标不可重写，只允许调整未提交章节。
- 每章记录“计划目标—正文兑现—后续调整”差异。

细纲生命周期固定为：

- 自动生成或采用唯一既有细纲。
- 以非 canonical、hash 绑定的 Stage Receipt 保存。
- 失败重试复用相同 outline hash。
- 正文质量和状态校验通过后，细纲与正文在同一个 v2 章节事务中发布到 canonical 目录。
- authority head 或输入发生变化时，旧细纲失效并重新规划。
- 现有 `plan_chapter()` 保留为弃用兼容别名，新名称为 `plan_scenes()`。

自治会话规则：

- 每本书同一时间只能有一个写会话。
- 在第一次细纲 Provider 调用前取得可续租的 book lease。
- `session_id`、`plan_id` 和每章 Receipt 形成链。
- 重复执行相同 plan 时恢复已有会话，不重复创建。
- 完成章数从 Receipt 链重建，不信任可变 Session 缓存。
- 拒稿和 Provider 失败不增加目标章数，不得跳章。
- requested chapter 只能从 canonical next chapter 追加；覆盖旧正文必须走独立 retcon 流程。
- `--steps` 保留为低层兼容参数，不再表示“提交 N 章”。

### 目标外（暂时搁置）：阶段 5 活动小说迁移与激活

> 2026-07-16 用户范围决定：以下内容作为已经投入的设计与实现完整保留，但不再属于当前目标。当前活动小说保持未迁移、未批准、未激活状态；Codex 不继续收集裁决、不创建 `MigrationApproval`、不执行迁移。恢复该阶段需要用户之后明确提出，并从新的只读预览重新开始。

迁移采用“预览—确认—CAS 执行”：

1. 冻结正文、总纲、设定、细纲、追踪、ProjectIdentity 和相关历史产物 hash。
2. 生成 `MigrationPlan` 和冲突报告。
3. 已发布正文证明已发生事件；明确人工设定证明静态约束；无证据或冲突状态保持 unknown。
4. 用户裁决约 155 分钟、人物第 10 章状态、开放伏笔、库存、词条和侵蚀值。
5. 确认结果形成不可变 `MigrationApproval`。
6. 执行时任一输入 hash 变化，MigrationPlan 立即过期。
7. 使用合法的首个 `source_sync` 批次表示审计基线，不伪造第 1—10 章逐章事件。
8. 在一次 v2 bootstrap 事务中原子发布：
   - Baseline Event Batch
   - CanonicalMemory
   - Snapshot
   - 追踪投影
   - ProjectIdentity authority 切换
   - Bootstrap Receipt
9. 历史 v1 journal 和 RunRecord 保持原字节，仅由外部 wrapper manifest 归属，不补造 Receipt。

第一份 event-authority Receipt 落盘前允许关闭功能回到 v1；落盘后禁止重新启用 v1 writer。之后的状态错误必须通过补偿事件修复，灾难恢复也必须恢复到完整 Receipt 边界。

## 四、接口与兼容契约

新增或升级的核心契约：

- `ExecutionProvenance`
- `ModelResponse`
- `ModelCallIntent/ModelCallReceipt`
- `ProjectIdentity.authority`
- 类型化 `CanonicalMemory`
- 版本化 `MemoryEvent/MemoryEventBatch`
- `BookRunPlan`
- `RunArcPlan`
- `ChapterOutline`
- `BookRunSession`
- `StageReceipt`
- `PersistenceManifest/PublicationReceipt`
- `DeliveryIntent/DeliveryJob/DeliveryAttemptReceipt`
- `OutlineReadiness/DraftReadiness`

兼容原则：

- 旧 v1 journal、旧 Receipt 和旧 DeliveryJob 保持原字节验证。
- 历史 Event 只通过确定性 upcaster 读取，不原地改写。
- 未知未来 Schema 或低于 `minimum_writer_contract` 的程序一律停止写入。
- Snapshot reducer 版本不匹配时删除并从事件链重建。
- v2 正式写入后，可停用自治和模型调用，但不能降级成本地 v1 写入。

## 五、测试、验收与发布门槛

### 自动化

- 保持现有 900 项测试和 v1 smoke 全部通过。
- Token 校准覆盖官方模型、兼容端点、中英混合和未知模型；估算误差只在保留测试集上评价。
- 1000 章合成历史下 Prompt 保持有界，所有保留 JSON 完整可解析。
- 质量集至少 60 例，分为校准集和不可用于调参的保留测试集：
  - blocking precision ≥85%。
  - critical/high 召回 ≥90%。
  - 干净样本误阻塞 ≤10%。
  - 男声/女声矛盾必须检出。
- 删除 Snapshot、追踪投影和 Session 缓存后，可由 Receipt/Event 链确定性重建。
- 在 Provider 响应落盘前、Persistence 每个 fsync/rename、marker 前后、Receipt 后、Delivery enqueue 前注入故障。
- 两个进程同时执行同一本书时，只有一个能在细纲调用前取得资格。
- stale plan、锁过期接管、手工源文件漂移、历史正文修改、Windows 中文路径、junction 和目标越界全部有回归测试。
- 首个 v2 Receipt 后尝试 v1 写入必须 fail closed。
- 旧 v1 数据升级前后字节和验证结果不变。

### 目标完成后的操作者手动真实 E2E

以下验证由操作者在当前目标完成后手动执行，不是当前 Codex 目标的待办或完成条件：

1. 真实单章闭环测试：临时小说生成 1 章，验证 v2、Event、Receipt 和 required File Delivery 真实闭环。
2. 真实 4 章连续性测试：
   - ContextBudgetError 和内部 ValueError 为 0。
   - 无人工补细纲、追踪或状态。
3. 真实 10 章无人值守测试：
   - 10 份细纲、10 章正文、连续 Event 链、10 个 Receipt 和 10 个 File Delivery 成功。
   - 无跳章、无状态漂移、无手工文件修改。
4. 真实至少 20 章耐久测试：验证长时间运行的连续性、恢复和交付。

50 章确定性模拟仍是当前目标内的离线工程证据。默认开放自治仍需同时具备该离线证据和操作者之后提供的至少 20 章真实耐久报告；在此之前保持 opt-in。默认开放自治不属于当前目标的交付承诺。

逻辑尝试次数作为效率 SLO 而非降低质量门的硬目标：

- 目标中位数不超过 2 次/章。
- Provider 传输重试、质量修复和系统失败分别统计。
- 不允许通过关闭 Validator 或放宽 Gate 达成低尝试数。

本轮继续禁止真实 Notion 写入。文档只能按“代码存在、接入主链、默认启用、真实验证”四级声明能力。
