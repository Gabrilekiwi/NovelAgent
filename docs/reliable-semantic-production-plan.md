# NovelAgent 下一阶段开发计划：从可靠写入升级为可靠语义生产

> 实施状态（2026-07-13）：Commit 1–14 已按本文边界完成。默认仍为 shadow；仓库合成 fixtures 不代表任一真实目标书已取得 strict 资格。真实书发布仍须提供该书的脱敏校准证据，并运行显式 opt-in 的两章真实 Provider E2E。

## 文档状态

- 状态：已按 14 个提交边界实施；真实目标书的 strict 发布资格仍需逐书验收。
- 基线提交：`7bbc740`。
- 基线验证：713 tests、79 subtests 全部通过。
- 本文取代原“8 个提交”的草案，作为下一阶段的架构契约、实施顺序和验收依据。
- 本文只规划本机、本地文件系统上的可靠语义生产；不承诺分布式事务或 Notion exactly-once。

## 1. 目标与范围

当前系统已经具备可恢复的本地写入闭环，但 StoryProject 仍未成为可无损读取、可往返、可审计的语义主状态。下一阶段要建立：

```text
读取 → 解析 → 语义合并 → Prompt 编译 → 生成 → 统一质量裁决
     → 本地提交 → 发布收据 → 持久投递 → 下一章准入
```

本阶段的目标是：

1. StoryProject 人工 Markdown 始终是最高权威事实源。
2. NovelAgent 对自己写入的结构化状态可以无损往返。
3. 生成所读取的完整 read-set 与提交事务绑定。
4. Prompt 大小、整次运行成本和重试次数都有确定上限。
5. `accepted`、`committed` 和 `ready_for_next_step` 只有一套定义。
6. 本地主状态提交与外部投递解耦，但 required delivery 会阻断下一章。
7. Memory V2 成为可回放事件投影，Snapshot 仅为可重建缓存。

明确不在本阶段实现：

- embedding、向量数据库或语义检索服务。
- UNC、网络文件系统或跨机器分布式锁。
- 执行 oh-story hooks、Node、npm、Agent 或任意项目脚本。
- 自动解决无法确定的剧情冲突。
- 对 Notion 宣称分布式 exactly-once。

## 2. 已确认的当前问题

以下问题已经由当前代码和测试核验：

- StoryProject Mapper 默认只保留每个上下文文件开头 20,000 字符；Loader 本身会完整读取文件，因此修复点在 Context 映射和 Prompt 编译边界，而不是底层文本读取函数。
- Writer 对列表值只输出前 5 项，属于有损投影。
- 上一章正文当前会被转换为 timeline memory item，混淆 Previous Chapter 与长期事实。
- StoryProject tracking Writer 采用逐次追加 block 的方式，长期运行会膨胀。
- 当前 read-set 没有完整绑定 outline/prose/tracking/settings 的内容和目录 membership。
- Memory V2 2.0 仍以可变 JSONL 追加为主，不是带 hash chain 的不可变事件批次。
- 当前 OpenAI 客户端可配置 SDK retry；Claude 客户端尚未显式把 SDK retry 固定为 0。

## 3. 不可妥协的语义不变量

### 3.1 三个顶层结果

`accepted`：

- 唯一来源是最终 `QualityDecision`。
- 表示最终正文通过当前 `QualityPolicy`。
- 不表示任何本地文件已经提交。

`committed`：

- 本地主状态、Memory event batch、Snapshot、最终 artifacts 和不可变 Final RunRecord 已由有效 commit marker 绑定。
- `publication_receipt.json` 已原子发布并通过哈希校验。
- 消费者不得仅凭孤立的 Final RunRecord 或 commit marker 判断 `committed=true`；必须验证 Publication Receipt。

`ready_for_next_step`：

- 是查询时派生值，不写死在不可变 Final RunRecord 中。
- 计算公式固定为：

```text
accepted
and committed
and project_identity_matches
and every required DeliveryJob is succeeded
and next_step_context_preflight is valid
and current read-set still equals next_step_context_digest
```

- CLI、Loop、Report 和 Recovery 必须通过同一个 ReadinessService 收集当前证据，再调用同一个纯决策函数计算；不得各自维护状态集合。
- `next_step_context_preflight` 必须验证下一章唯一细纲、当前 StoryProject sources、identity、parser qualification 和 blocking conflicts。
- 用户在两章之间修改人工 Markdown 是合法行为；它会使旧 readiness decision 失效，但新的无 Provider preflight 可以基于新 read-set 产生新的 readiness decision。
- readiness decision 携带 `next_step_context_digest`，Provider 必须消费同一 read-set；预检后发生漂移时停止，不自动重调 Provider。
- 只有 `ready_for_next_step=true` 才能调用 Provider 生成下一章。

### 3.2 本地提交状态机

本地事务状态固定为：

```text
new
  → prepared
  → applying
  → commit_marked
  → publishing
  → completed

prepared/applying ──失败──→ rolling_back → rolled_back
                                  └──────→ recovery_required
commit_marked/publishing ──启动恢复──→ publishing → completed
```

规则：

- commit marker 之前失败，优先执行带 CAS 的回滚。
- commit marker 之后不得回滚已经提交的主状态，只允许完成幂等 publication。
- `completed` 必须意味着 Publication Receipt 已存在且有效。
- `recovery_required` 永远禁止下一章生成，直到显式 reconcile 成功。

### 3.3 哈希依赖必须无环

固定依赖顺序：

```text
read-set ──→ context_digest ──→ candidate_digest
                                  │
staged targets ──→ artifact_bundle_digest
                                  │
manifest_digest ──────────────────┼──→ commit_marker_hash
                                  │
Final RunRecord bytes ──→ final_run_hash
                                  │
commit_marker_hash + final_run_hash
                    ──→ publication_receipt ──→ receipt_hash
                                                        │
Delivery attempts ──────────────────────────────────────┘
```

为避免循环依赖：

- Final RunRecord 只能保存预定 Receipt PathRef/ID，不能保存 receipt hash 或完整 receipt。
- Commit marker 可以绑定 Final RunRecord hash，但不能绑定 receipt hash。
- Publication Receipt 保存 marker hash、Final RunRecord hash和 artifact hashes。
- DeliveryJob 在本地事务中创建，保存 job id、payload hash 和 policy；DeliveryAttemptReceipt 在 Publication Receipt 之后引用 receipt hash。
- artifact bundle 不包含 Publication Receipt 自身。
- Final RunRecord 中历史兼容的 `committed/status` 只是待 Receipt 验证的提交声明；读取新记录时必须由 Receipt 校验结果派生对外 committed 状态，孤立记录中的布尔值不是证明。
- `manifest_digest` 只覆盖 manifest 的 immutable section，排除 digest 字段本身以及 state、errors、updated_at 等恢复期可变字段。
- 任一对象保存自身 hash 时，hash 输入必须排除该 hash 字段，并写明 canonicalization version。

## 4. StoryProject 身份与运行空间

首次真实持久化时，原子创建：

```text
<StoryProject>/.novelagent/project.json
```

至少包含：

- 稳定 UUID `book_id`。
- project schema version。
- 创建时间。
- 非权威 root hint。
- 当前 story-state mode。
- strict 激活时绑定的 parser/schema/layout profile 版本。

StoryProject 默认运行空间：

```text
.novelagent/runtime/snapshot.json
.novelagent/runtime/runs/
.novelagent/runtime/chapters/
.novelagent/runtime/reviews/
.novelagent/runtime/persistence/
.novelagent/runtime/deliveries/
.novelagent/runtime/memory/
```

规则：

- Snapshot、Run、Loop、Journal、Receipt、Memory 和 DeliveryJob 必须携带相同 `book_id`。
- 任一不匹配以 `story_project_state_identity_mismatch` fail closed。
- Preview 不创建 `project.json`；使用标记为 ephemeral 的临时 identity，且不得被后续提交或迁移接受。
- 非 StoryProject 默认路径继续使用 `.tmp/runtime`。
- 旧全局 runtime 只支持只读诊断和显式迁移；迁移不删除旧数据，并生成 migration manifest。
- 旧记录无法证明 StoryProject root/book 归属时不得自动恢复或迁移。

## 5. StoryProjectSemanticState 与来源权威

### 5.1 Schema

新增 schema-checked `StoryProjectSemanticState`：

- `schema_version`、`book_id`、`chapter_index`。
- `story_state`、`world_state`、`spatial_state`。
- `characters`、`timeline`、`constraints`。
- `foreshadowing[]`：稳定 id、内容、状态、introduced/target/resolved chapter。
- `provenance[]`、`conflicts[]`、`parse_warnings[]`。
- `parser_version`、`layout_profile_version`、`source_digest`。
- `unsupported_excerpts[]`：只保存来源、范围、hash 和受限 excerpt，不升级为事实。

所有会被 Director、Validator、Repair 或 Prompt 编译器消费的事实必须有 provenance。provenance 至少包含：

- PathRef 和字节/字符范围。
- source kind。
- parser version。
- observed revision/hash。
- authority class。
- 是否为人工字段、managed projection、正文证据或模型推断。

### 5.2 字段级权威矩阵

不使用一条覆盖所有字段的全局优先级。至少按以下语义分类：

| 字段类别 | 首选权威 | 次级证据 | 禁止行为 |
|---|---|---|---|
| 本章目标、required beats | 当前章细纲、用户显式输入 | 无 | 不得由 Snapshot/Memory 覆盖 |
| 当前人物/地点状态 | tracking 人工字段 | managed block、唯一 N-1 正文证据 | 不得由未来细纲覆盖当前事实 |
| 长期世界设定 | `设定/` 明确结构化事实 | tracking 人工字段 | 不从未知自由文本推断权威事实 |
| 伏笔状态 | tracking 人工字段及 tombstone | managed block、已提交章节分析 | 不以文件顺序解决同级冲突 |
| 最近时间线 | tracking 人工事件、唯一 N-1 正文 | committed Memory projection | rejected/preview 内容不得进入 |
| 运行缓存 | StoryProject + Memory projection 重建 | 无 | Snapshot 不得反向成为权威 |

同权威级冲突必须产生稳定 conflict id。strict 模式阻断；shadow 模式只报告。

## 6. Parser、Managed Block 与三方合并

### 6.1 解析范围

- `上下文.md`：解析当前章节、当前位置、最近决定、待处理线索、上一章结尾、opening bridge。
- `角色状态.md`：按角色 section 解析身份、状态、位置、能力、关系、最近变更。
- `伏笔.md`：解析受支持的 Markdown table/list 字段。
- `时间线.md`：解析受支持的 table/list 和 NovelAgent managed events。
- `设定/`：只抽取有明确标题、标签、键值或表格结构的事实。
- 未支持自由文本只进入 `unsupported_excerpts`，必须标注 non-authoritative。

### 6.2 Managed Block 协议

每个 tracking 文件最多有一个 versioned managed block。每个 block 只保存该文件负责的投影，不在每个文件重复嵌入完整合并状态。

Block 至少包含：

- 人类可读 Markdown projection。
- schema-checked、文件作用域内的 JSON projection。
- `book_id`、run id、chapter、schema/parser version。
- `base_revision`、`base_source_digest`。
- `owned_fields[]`。
- payload SHA-256。
- 可选 tombstones。

其中：

- `base_source_digest` 只覆盖 managed block 外的人工字节和已声明外部来源，不能包含当前 managed block 自身。
- payload SHA-256 对 canonical JSON projection 计算，排除 `payload_sha256` 字段本身。
- block 起止 marker 必须是独占行；重复、嵌套或畸形 marker 一律阻断写入。

Writer 规则：

- 只替换 managed block 的精确字节范围。
- managed block 外的 BOM、换行风格和人工字节保持不变。
- 首次写入只新增一个 block，不批量重写历史 append blocks。
- 旧 append blocks 保留为 legacy evidence，best-effort 解析且永不自动删除。
- 同一投影重复写入字节幂等，文件不增长。

### 6.3 三方合并与删除语义

每次写入前执行三方合并：

```text
base managed projection
        + current manual text
        + proposed new projection
        → merged projection / conflict
```

规则：

- 人工明确值覆盖 managed 值。
- 人工文本中存在显式删除/tombstone 时，旧 managed 值不得复活。
- 单纯“字段缺失”不能自动解释为删除。
- NovelAgent 只能更新 `owned_fields`；未知字段逐字节保留。
- 实体改名、内容改写和章节迁移必须保留稳定 ID 或产生显式 supersedes 关系。
- 无法确定是删除、改名还是解析失败时产生 blocking conflict，不静默选择。

必须满足：

- `parse_managed(write_managed(projection)) == projection`。
- 相同输入二次写入字节幂等。
- managed block 外字节不变。
- 人工修改后能执行确定性的三方合并。
- 人工 tombstone 不会被 Memory、Snapshot 或旧 managed state 复活。

## 7. Shadow 校准与 Strict 激活

模式固定为：

- `compatible`：保留历史行为，不启用语义投影。
- `shadow`：完整解析、diff、冲突和预算报告，但不得影响 Director/Validator、Snapshot、Memory 权威或真实 tracking 写入。
- `strict`：经过校准的 semantic state 成为生成和校验输入。

Strict 激活门槛：

1. 至少一个目标书脱敏真实样本。
2. 至少两个其他模板或历史格式变体。
3. Managed block round-trip 覆盖率 100%。
4. 必需字段 exact-match 100%。
5. 受支持人工事实 precision 100%，不得出现错误权威化。
6. 受支持非必需字段 recall 不低于 95%。
7. 未支持结构全部进入 warning/excerpt，不得被静默丢弃或推断。
8. 目标书至少连续 10 章 shadow 零 blocking conflict；样本不足 10 章时不得仅凭 3 章自动激活。
9. 所有 Director/Validator 字段都有 provenance。

激活必须显式执行 `--activate-story-state`，并把 parser/schema/layout profile 版本写入 `project.json`。

Parser、schema 或 layout profile 升级后：

- strict 默认 fail closed 并要求重新校准。
- 不自动静默降为 shadow 后继续生产。
- 只有显式 `--allow-story-state-shadow-downgrade` 才允许降级；降级后 `ready_for_next_step=false`，直到用户确认新的生产策略。

## 8. Previous Chapter、Attempt 与 Recovery Context

新增 `PreviousChapterContext`：

- `chapter_index`、PathRef、全文 SHA-256、原始字符数。
- `generation_excerpt`、`review_tail`。
- excerpt 策略、字符范围、token 数/估算和截断状态。

固定规则：

- 第 N 章只允许唯一正文 N-1；第 1 章允许为空。
- N>1 缺失上一章是 high-risk；standard/strict 均 fail closed。
- 多个正文候选始终 blocking。
- 上一章正文不再转换为长期 timeline event。
- Review 使用本次 read transaction 中的 N-1 `review_tail`。
- 同章 rejection retry 时，N-1 仍是 Previous Chapter；rejected draft 仅进入 Attempt/Recovery Context。
- rejected、preview 或 hash 未验证的 artifact 永远不能被当作上一章。
- 非 StoryProject 回退只读取 `committed=true`、chapter=N-1 且 artifact hash 有效的历史正文。

## 9. Context Read Transaction 与 Prompt 预算

### 9.1 Read-set

`StoryProjectReadSet` 记录：

- outline、previous prose、tracking、settings 的 role、PathRef、exists、size、SHA-256。
- parser version、parse status。
- outline/prose 候选集合 fingerprint。
- tracking/settings Markdown membership fingerprint。
- project identity revision。
- 最终 `context_digest`。

事务还要计算 expected post-membership：目录在 apply 后应等于 pre-membership 加上声明创建、删除或替换的目标。marker 前校验不得把本事务自己的合法写入误报为外部 source drift；任何未声明的新增、删除或候选冲突仍然阻断。

文件完整读取用于解析和哈希；Prompt 只接收 canonical semantic state 和被选择的 excerpts。

### 9.2 提交前并发检查

事务必须：

1. 生成时冻结 read-set 和 directory membership。
2. prepare 前重新验证一次。
3. 每个写目标替换前执行 expected-before CAS。
4. marker 前再次验证所有只读来源、membership 和 after-hash。
5. 发现 source drift 后停止当前循环，不自动重新调用 Provider。
6. 回滚时只有目标仍等于本事务 `after_hash` 才能恢复 backup。
7. 若用户在写入后又修改目标，进入 `recovery_required`，绝不覆盖用户修改。

本计划承诺确定性恢复，不宣称多文件系统调用具有不可见的严格原子性。

### 9.3 PathRef 安全

`PathRef={root_id, relative_path, original_path_hint?}` 必须：

- 拒绝绝对 `relative_path`、`..` 越界和空路径歧义。
- resolve 后仍位于声明 root 内。
- 检查 Windows 大小写归一化、符号链接和 junction 越界。
- 对每个事务持久化解析后的 root identity，而不是在恢复时依赖当前 CLI 猜测。
- 默认拒绝 UNC 和网络文件系统真实写回。

### 9.4 预算定义

配置按 provider/model 显式声明 `model_context_window`。每次调用的输入上限为：

```text
usable_input_tokens =
    model_context_window
    - output_reserve_tokens
    - protocol_overhead_tokens
    - safety_margin_tokens
```

`max_input_tokens` 表示最终可发送输入的硬上限，不再与 output reserve 重复计算。

默认建议值：

- `max_input_tokens=32000`，但不得超过上述模型公式。
- `story_project_tokens=16000`。
- `previous_chapter_tokens=6000`。
- `output_reserve_tokens=8000`。
- `safety_ratio=0.15` 用于把估算转换为更保守的 hard limit。

选择顺序：

1. Chapter Blueprint 和强制 semantic facts，不可静默裁剪。
2. N-1 正文最多 6000 tokens，10% paragraph-aligned head + 90% tail。
3. 未解决伏笔、当前角色/位置、最近时间线。
4. 与本章实体直接相关的设定 excerpts。
5. 其他 heading summaries。

Mandatory facts 超预算时，在 Provider 前以 `story_project_context_budget_exceeded` 失败。

计数规则：

- 优先使用 provider/model 对应 tokenizer 的确定性计数。
- 无可用 tokenizer 时使用版本化、可测试的保守 upper-bound estimator。
- RunRecord 标记 `exact` 或 `estimate`，记录 estimator/tokenizer version。
- system prompt、tool/schema、消息包装和 repair instructions 都计入预算。

整次运行还必须支持：

- `max_provider_calls`。
- `max_total_input_tokens`。
- `max_total_output_tokens`。
- `max_elapsed_seconds`。
- 可选 `max_estimated_cost`。

Plan 编译一次 chapter context；Scene 使用 shared compact context；Repair 只复用 context digest 和必要 excerpts，不重复发送完整 tracking/settings。

## 10. 唯一 QualityDecision

`QualityDecision` 是 `accepted` 的唯一来源，统一：

- 基础 Validation。
- Blueprint Coverage。
- Deterministic Review。
- Narrative Rules。
- 可选 LLM Validator。
- 每轮 Repair 后的完整复验。

`QualityFinding` 至少包含：

- 稳定 finding id。
- producer、code、category、severity、blocking。
- canonical subject/predicate/time range。
- evidence 和 source artifact。
- repair action/parameters。
- validation coverage。
- 全部 producer evidence。

Finding id 由版本化 canonical identity 生成，不能直接依赖自然语言 message。相同事实的合并键至少由 policy version、subject、predicate、time range 和 code family 构成。

Severity 顺序固定为：

```text
info < warning < needs_revision < blocking
```

策略：

| Policy | 内容 | Repair | LLM Validator |
|---|---|---:|---|
| minimal | 基础 Validation | 历史兼容 | 不要求 |
| standard | 全量基础 Validation + deterministic Review，门槛 `needs_revision` | 最多 2 次 | 默认关闭 |
| strict | standard + 门槛 `warning` | 最多 3 次 | 必须配置 |

规则：

- StoryProject 真实写回默认 standard；非 StoryProject 保持历史 minimal。
- Gate 和 Repair Loop 只消费 QualityDecision。
- 旧 validation/review/review_gate 只作为证据和兼容投影。
- 每次修复后重新运行完整 QualityDecision 和语言契约。
- 无法安全确定性修复的剧情问题转为 `manual_review`，禁止插入泛化剧情句。
- LLM Validator 必须记录 provider、model、prompt hash、policy version、attempt history；不可用时 strict fail closed。
- QualityDecision 对已保存 findings 和 policy 的归并必须可确定重放，不要求重新调用 LLM 才能审计旧决定。

## 11. 中文本地修复与 Provider Retry

### 11.1 中文修复

`repair_scene()` 和 `apply_repair_plan()` 接收显式 language/RepairContext。建立 `zh-CN` 与 `en` strategy registry。

中文确定性策略仅覆盖：

- 章节号修正。
- 已知 opening bridge 插入/替换。
- 已知地点与人物位置锚定。
- 已知冲突提示。
- inactive character action 删除或受限改写。
- 中文句界、标点和动作词的结构性修复。

删除 serum 等测试域硬编码。任何需要发明新剧情事实的修复转为 manual review。

### 11.2 RetryPolicy

统一错误分类，但按 operation profile 配置策略：

- model read/generation。
- Claude polish。
- Notion read/query。
- Notion create，禁止进入通用自动重试。

默认 model/read 策略：

- `max_attempts=3`。
- base delay 1 秒、max delay 8 秒、jitter 20%。
- 同时受总 deadline 和运行级预算限制。
- 仅 connection、timeout、429、明确可重试的 5xx 重试。
- 优先尊重 `Retry-After`，但不得超过剩余 deadline。
- 配置、认证、schema、output contract 错误不重试。
- OpenAI 和 Claude SDK 内部 retry 显式固定为 0。
- 流式响应只有在尚未收到任何内容时允许重试；部分内容不得拼接或自动重放。

为保证测试确定性，RetryPolicy 注入 clock、sleep 和 random source。attempt history 记录分类、elapsed、delay、是否收到部分内容，并做凭据和正文脱敏。

旧 `OPENAI_MAX_RETRIES` 保留一个兼容版本，映射为 `max_attempts=max_retries+1`，输出 deprecation warning。

## 12. Memory V2.1 事件源

Memory 2.0 只读兼容；新写格式为 2.1。

每个事务生成一个不可变 event batch：

- batch id、schema version、book id。
- first/last revision。
- previous batch hash、batch hash。
- patch id、patch content hash、expected revision。
- source project/context digest。
- canonical JSON algorithm version。

规则：

- 同 patch id + 同 content hash：no-op。
- 同 patch id + 不同 content：conflict。
- revision 不连续或 hash chain 断裂：fail closed。
- hash 使用明确的 canonical JSON 规范，排除存储路径、mtime 等环境字段。
- batch hash 和 event hash 的输入排除各自 hash 字段本身；previous hash 仍参与计算。
- 每 20 个 committed chapters 创建 immutable checkpoint；source-sync patch 不计入 chapter 间隔，但仍推进 revision。
- `canonical_memory.json` 是可删除、可重建 cache。
- 启动从最新有效 checkpoint + 后续 batches 验证，不重复读取全部历史。

提供：

- `replay_memory_events()`。
- `verify_memory_projection()`。
- `rebuild_canonical_memory()`。

边界：

- Parser 产生 source-sync patch。
- committed chapter analysis 产生 chapter patch。
- rejected/failed/preview 内容不得进入世界状态 Memory。
- QualityDecision 摘要进入独立 `quality_state`，不污染世界事实。
- StoryProject 人工事实不可被 Memory 反向覆盖。
- Snapshot 每步由 StoryProjectSemanticState + Memory projection 重建。

## 13. Persistence v2 与 Publication Receipt

### 13.1 事务顺序

1. 从 pending registry 发现未完成事务。
2. 从历史 manifest 推导 book/run/state locks。
3. 按稳定顺序获取 locks。
4. 锁内重新发现并验证 read-set 和 directory membership。
5. 渲染全部 targets、Memory batch、Final RunRecord、artifacts 和 DeliveryJobs。
6. Stage bytes，计算 target hashes、candidate digest、artifact bundle digest 和 Final RunRecord hash。
7. 对所有目标执行 expected-before CAS。
8. Apply StoryProject、Memory V2、Snapshot 和本地 DeliveryJobs。
9. 验证 after-hash 与只读 read-set。
10. 创建绑定 manifest/candidate/artifact bundle/Final RunRecord hash 的 commit marker。
11. 幂等发布 chapter/review/input artifacts 和 Final RunRecord。
12. 原子创建 Publication Receipt。
13. 验证 Receipt 后把 manifest 标记 completed。
14. 再尝试 required external delivery。

### 13.2 Publication Receipt

至少包含：

- book/run/schema version。
- context digest。
- generation 使用的 input context digest，以及 apply 后的 StoryProject source revision。
- marker PathRef/hash。
- manifest/candidate/artifact bundle hashes。
- Final RunRecord PathRef/hash/size。
- chapter/review/input artifacts PathRef/hash/size。
- StoryProject/Memory/Snapshot target hash 摘要。
- DeliveryJob ids 和 policies。

Final RunRecord 的 `publication_receipt` 字段只能是预定 PathRef/ID。报告层验证实际 Receipt 后再派生 `committed=true`。

### 13.3 恢复

- marker 前：CAS rollback；任一目标发生外部漂移则 `recovery_required`。
- marker 后：不回滚主状态；补发 artifacts、Final RunRecord 和 Receipt。
- orphan Final RunRecord 没有有效 Receipt 时不得作为 committed history 消费。
- completed 事务启动不依赖 candidate；candidate 损坏只 warning。
- pending candidate 损坏 fail closed。
- startup 只扫描 pending registry，不扫描全部 completed journal。
- reconcile 锁和 root mapping 只能来自历史 manifest。

### 13.4 Retention/GC

- 最近 10 个 completed journals 保留 staged、backup 和 candidate。
- 更早 completed journals 仅保留 manifest、marker、Receipt 和最小审计索引。
- `recovery_required` 永久保留全部恢复材料，不计入 completed 的 10 个上限。
- rolled_back 最近 10 个保留完整 journal，更早只保留失败 receipt。
- GC 只能在 reconcile 成功且不存在 recovery_required 冲突时运行。
- GC 失败只产生 warning，不改变提交状态。
- dry-run 输出 reclaimed bytes、拟删除列表和跳过原因。

验收中的“完整 journal 不超过 10”必须分别按 completed 和 rolled_back 状态统计，不能把永久保留的 recovery_required 错算为违规。

## 14. Durable Delivery Queue

本地主状态文件不是 Delivery。只有事务提交之后需要发布到独立目标的 file export、Notion 等才进入 Delivery Queue。

状态固定为：

- `not_required`。
- `pending`。
- `delivering`。
- `succeeded`。
- `retryable_failed`。
- `permanent_failed`。
- `uncertain`。
- `conflict`。
- `cancelled`。

规则：

- `delivering` 必须带 worker id、lease expiry 和 attempt id；过期 lease 可被 reconcile 接管。
- 单个 job 同时最多有一个有效 lease。
- required job 只有 `succeeded` 才满足下一章准入。
- `not_required` 只用于该 delivery policy 明确不要求投递的情况。
- required delivery 未成功时，本地提交不回滚，CLI/Loop 返回 1 并停止。
- 下一次持久生成在 Provider 前优先 reconcile deliveries。
- `best-effort` 只能显式选择。
- 配置 writer 时默认 policy=`required`。

Notion 算法：

1. 为 job 生成稳定 operation id、Memory ID 和 canonical payload hash。
2. 查询必须处理分页，并按 operation id/Memory ID 查找。
3. 唯一页面且完整 payload hash 相同：succeeded。
4. 同 id 内容不同或多页面：conflict。
5. 无页面时才 POST。
6. POST timeout、缺 page id 或 readback failure：uncertain。
7. uncertain 只做查询确认，不自动 POST。
8. `--confirmed-absent` 之前必须经过配置的 quarantine/read-after 窗口并再次分页查询。
9. 即使人工确认也不宣称完全排除远端延迟；后续发现重复页时转 conflict，禁止自动删除。

Notion required delivery 的 Preflight 必须先验证数据库具备可查询的 operation id/Memory ID、payload hash 和必要映射字段，并验证字段类型及长度限制。无法满足远端 schema 时，required policy fail closed；不得退化成仅按标题或模糊内容去重。

命令：

- `--reconcile-deliveries [--run-id ID]`。
- `--inspect-delivery JOB_ID`。
- `--resolve-delivery JOB_ID --confirmed-absent`。
- `--delivery-policy required|best-effort`。

每次 attempt 保存独立 receipt，不保存凭据和完整敏感正文。

## 15. oh-story 与真实 Provider 验收

Workspace 解析顺序：

1. 显式 `--workspace-root`。
2. 从 StoryProject 向上寻找最近的 `.story-deployed` 或已知有效 setup assets。
3. StoryProject 位于 CWD 内时使用 CWD。
4. 否则使用 StoryProject root。

报告区分：

- `detected_assets`。
- `novelagent_capabilities`。
- `compatibility_claims`。
- detector version、layout version 和 unknown-layout warnings。

检测始终只读，不执行任何 hooks、Node、npm、脚本或 Agent。

真实 E2E 使用显式 opt-in `real_storyproject_e2e`：

- 在脱敏 StoryProject 临时副本中使用真实 OpenAI 执行两章。
- 使用真实本地事务和 file delivery。
- 第二章必须读取第一章正文、managed state 和 Memory V2 revision。
- Claude polish 和 Notion sandbox 分开测试。
- 普通 PR 不需要密钥；manual/nightly 发布门必须通过。
- 报告 redacted，并限制模型、调用次数、token、费用和超时。

## 16. Public Contracts

新增或扩展：

- `ProjectIdentity`、`RuntimePaths`、`RootMap`、`PathRef`。
- `StoryProjectSemanticState`、`SemanticProvenance`、`SemanticConflict`。
- `ManagedProjection`、`ManagedTombstone`、`SemanticMergeResult`。
- `PreviousChapterContext`、`AttemptContext`、`RecoveryContext`。
- `StoryProjectReadSet`、`ContextBudget`、`ContextBudgetReport`。
- `ReadinessDecision`：ok、reasons、next chapter、next-step context digest、checked-at 和 identity。
- `QualityDecision`、`QualityFinding`、`QualityPolicy`。
- `PersistenceManifestV2`、`CommitMarkerV2`、`PublicationReceipt`。
- `DeliveryJob`、`DeliveryOutcome`、`DeliveryAttemptReceipt`。
- Memory Event Batch 2.1、checkpoint、replay report。

兼容规则：

- 历史 RunRecord、Loop Session、Memory 2.0、Persistence v1 只读兼容。
- 新字段对历史 schema 可选，对新记录必填。
- 不做批量隐式迁移。
- 旧 verification/status 通过单一映射函数派生 DeliveryOutcome。
- 旧 append blocks 保留且只做 best-effort evidence。
- `AgentExecutor.run_once()` 和现有顶层导入继续通过 re-export 可用。
- StoryProject apply + `persist=False`、apply + `dry_run=True` 必须拒绝。
- 直接 apply API 不再从顶层包导出；preview API 继续保留。

## 17. 实施顺序与提交边界

每个提交都必须保持现有全量测试和本提交新增测试通过。禁止在语义变更提交里同时进行大规模目录重排。

Commit 1–12 的新生产语义均置于兼容开关或 shadow 路径后；在 Commit 13 完成校准和显式激活前，不改变现有项目的默认权威源。内部 schema 可以提前落地，但不能让半完成的 Parser、Memory 或 Delivery 契约进入默认真实写回。

### Commit 1：契约、状态机和真实 fixtures

- 固定本文状态机、哈希 DAG、authority matrix。
- 增加脱敏真实 fixtures、格式变体、100 章 soak fixture。
- 增加 characterization tests 和 schema skeleton。

完成门：不改变生产行为；基线全绿。

### Commit 2：ProjectIdentity、RuntimePaths、PathRef 安全

- 项目内 `.novelagent/` runtime。
- identity 校验、preview ephemeral identity。
- 只读迁移诊断和显式迁移 manifest。
- path traversal、symlink/junction、UNC preflight。

### Commit 3：只读 Shadow Semantic Parser

- StoryProjectSemanticState、provenance、conflicts、unsupported excerpts。
- 字段级 authority merge。
- 仅输出 shadow semantic diff，不影响生成和写入。

### Commit 4：Managed Block 与三方合并

- 单 managed block、作用域 JSON projection、tombstone、owned fields。
- byte-preserving replacement 和 round-trip/property tests。
- 旧 append block 只读兼容。

### Commit 5：Previous/Attempt/Recovery Context

- 唯一 N-1 解析。
- rejection retry 隔离。
- committed artifact fallback 和 hash 验证。

### Commit 6：Context Budget 与 Prompt Compiler

- 完整读取与 Prompt excerpts 分离。
- model-aware token budget、运行级预算。
- Plan/Scene/Repair compact context。

### Commit 7：Read-set Transaction 与 source drift

- 完整 read-set、membership fingerprint、context digest。
- prepare/replace/marker 前校验。
- rollback CAS 与 recovery_required 测试。

### Commit 8：QualityDecision 与中文修复

- 唯一 accepted 来源。
- policy/severity/finding identity。
- Review/Repair 收敛和 zh-CN strategy registry。

### Commit 9：Memory V2.1

- immutable event batches、hash chain、patch idempotency。
- checkpoints、replay、verify、rebuild。
- 2.0 只读兼容。

### Commit 10：Persistence v2 与 Publication Receipt

- PathRef manifest、无环 hash binding。
- Final RunRecord/Receipt 发布与恢复。
- pending registry、retention 和 GC dry-run。

### Commit 11：Durable Delivery Queue

- job state machine、lease、attempt receipts。
- file export 和 Notion reconcile。
- 统一 `ready_for_next_step` 派生。
- 本提交只实现单次 attempt 与显式 reconcile，不为 Notion create 增加通用自动重试。

### Commit 12：统一 Provider Retry

- operation profiles、deadline、Retry-After、partial-stream guard。
- OpenAI/Claude SDK retry=0。
- attempt telemetry 和兼容配置。

### Commit 13：Strict 激活、真实 E2E 和文档

- shadow 校准报告与显式 strict 激活。
- 两章真实 Provider E2E。
- CLI help 快照、README、runtime/architecture 文档同步。

### Commit 14：最终模块拆分

行为和 schema 稳定后再抽取：

- `StoryProjectContextService`。
- `QualityCoordinator`。
- `PersistenceCoordinator`。
- `DeliveryCoordinator`。
- `main.py` 的参数、配置、命令、输出模块。

只做结构迁移和 re-export，不混入新语义。

## 18. 测试与验收矩阵

### 18.1 单元与属性测试

- Managed projection round-trip、byte idempotency、人工区字节不变。
- tombstone 不复活、rename/supersedes 稳定。
- field authority 和同级 conflict。
- canonical JSON/hash 跨重复运行一致。
- token budget 的 exact/upper-bound 行为。
- Quality finding canonical identity 和去重。
- Retry clock/random 注入后的确定性。

### 18.2 集成测试

- 100k 字上一章和 tracking 文件仍能选中尾部最新事实。
- 所有 Provider payload 都不超过对应模型预算。
- 同章 rejection retry 的 Previous Chapter 始终为 N-1。
- 修改任意 read-set 文件都在 commit marker 前失败。
- 两本书默认参数状态完全隔离。
- StoryProject 与 Snapshot 冲突时只有 StoryProject 权威值进入 Director/Validator。
- shadow state 不进入生成、Snapshot 或 Memory。
- strict 版本漂移 fail closed。
- zh-CN 本地修复不插入英文模板或新剧情事实。

### 18.3 崩溃与恢复矩阵

对每个事务节点注入崩溃：

- stage 前后。
- 每个 target replace 前后。
- read-set 再验证前后。
- marker 前后。
- Final RunRecord 发布前后。
- Receipt 发布前后。
- manifest completed 前后。
- Delivery lease 获取、POST、readback 前后。

验收：

- marker 前只会 CAS rollback 或 recovery_required。
- marker 后只会完成 publication，不回滚主状态。
- 外部编辑永不被 rollback 覆盖。
- orphan Final RunRecord 不被当作 committed。
- 任意 Final RunRecord 不可变字段被修改都会被 Receipt 检出。

### 18.4 性能与保留

- 100 章 soak 记录事实 precision/recall、冲突、伏笔丢失、Prompt tokens、调用放大和每章存储增量。
- 1000 completed receipts 启动时不读取历史 candidates。
- completed/rolled_back 各自完整 journal 数不超过配置值；recovery_required 不被误删。
- checkpoint 后 replay 结果与 canonical cache hash 一致。
- GC dry-run 和真实 GC 删除集合一致。

### 18.5 Delivery 与真实 E2E

- stale delivering lease 可恢复且不会并发重复投递。
- Notion uncertain 不自动二次 POST。
- 分页、重复 ID、payload conflict 和延迟 readback 都有测试。
- 两章真实 StoryProject E2E 中，第二章读取第一章全部新权威状态。
- Preflight、`smoke_v1.py`、provider nightly 和最终全量测试通过。

## 19. 发布门与回退

### Shadow 发布门

- 可先发布 parser、diff、reports 和 fixtures。
- 默认不改变现有生产生成或写入语义。
- 无真实样本时只能发布 shadow，不能发布 strict。

### Standard 写回发布门

- Managed block round-trip 和三方合并全绿。
- Read-set/CAS 崩溃矩阵全绿。
- Publication Receipt 无环哈希验证全绿。
- 两本书隔离测试全绿。

### Strict 发布门

- 满足第 7 节全部校准指标。
- 真实两章 E2E 和 nightly 通过。
- parser/schema/layout profile 已固定。
- 用户显式激活。

### 回退规则

- 未 commit_marked 的事务走 CAS rollback。
- 已 commit_marked 的事务只能 reconcile forward。
- strict 资格失效时 fail closed，不静默换权威源继续生成。
- 外部 Delivery 故障不回滚正文，但 required policy 阻断下一章。

## 20. 最终完成定义

本阶段只有同时满足以下条件才算完成：

1. StoryProject 人工文本可保留、可删除、可三方合并，不被 managed state 复活或覆盖。
2. semantic state 的每个生产字段都有 provenance。
3. Prompt 和整次运行都受确定预算约束。
4. QualityDecision 是 accepted 的唯一来源。
5. Publication Receipt 能无环证明 committed。
6. `ready_for_next_step` 由统一函数派生，required delivery 未成功绝不进入下一章。
7. Memory V2.1 可从 checkpoint/events 完整重放。
8. 任意本地事务崩溃点都能确定前滚、CAS 回滚或安全进入 recovery_required。
9. shadow/strict 的上线和版本升级行为均可预测、可审计、可回退。
10. 全量自动化、smoke、真实 Provider nightly 和文档契约全部通过。

## 21. 已固定决策与实施前输入

已固定决策：

- StoryProject 使用项目内 `.novelagent/` 运行空间。
- 采用结构化解析和全局预算，本阶段不引入向量检索。
- StoryProject 真实写回默认 standard QualityPolicy。
- strict 必须先 shadow 校准并显式激活。
- completed journals 默认保留最近 10 个完整版本。
- Notion 通过本地 durable queue 后同步。
- 本地文件系统只承诺确定性恢复，不承诺严格多文件原子可见性。

实施前必须提供或确认：

- 目标书的脱敏 StoryProject 样本；没有样本时 strict 保持不可发布。
- 实际使用的 provider/model、context window、tokenizer 和输出上限。
- Notion sandbox 是否允许增加 operation id 与 payload hash 属性。
- nightly 的调用次数、token、费用和超时上限。
- fixtures、Run artifacts 和 Provider reports 的脱敏规则。
