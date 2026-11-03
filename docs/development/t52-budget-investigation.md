当前续接（2026-11-03）：本文主体是实施前调查，不代表当前实现缺口。价格契约/章账本/主动时间/默认工作台入口已有实现；当前金额模式缺真实SDK可信输入上界及完整计费绑定，见 `t52-budget-implementation.md`、`t52-provider-budget-evidence.md`。不要重做已完成框架，T5.2仍未完成。

# T5.2 单章预算与执行时间：实施前调查

日期：2026-09-10。性质：只读代码调查与实施建议。**T5.2 尚未完成，本记录不是功能验收或真实调用授权。**

本次阅读最终计划、物理调用运行时、候选生成与事实提取入口、用量展示及现有预算/时间实现；未修改实现、未运行测试、未启动服务、未调用真实 Provider。本文只保存调查结论，不更新主计划或机器进度。

## 1. 目标与判断

唯一主计划 [development-plan-final-2026-09-08.md](D:/Pycharm/PythonProject/NovelAgent/docs/development-plan-final-2026-09-08.md) 的 T5.2 要求：冻结单章价格，发请求前进行金额预留，按证据结算；生成、提取、检查及修复共用预算，重启不清零。累计主动执行时间、作者等待和端到端历时应分别记录；无法解释的未闭合执行区间在恢复时标记未知并暂停。

现有物理调用 Intent/Receipt、重放、token 预留和用量来源契约可以复用。但是，在本次调查的调用路径中，尚未找到满足上述要求的持久化单章金额账本或主动执行时间账本。仅设置 `RunBudgetLimits.max_estimated_cost` 不能完成 T5.2。

缺价格时，逐章工程可以使用明确的 token/调用限制，但不能宣称具备金额止损。T8.1 仍须另外满足预算预检、真实作品资格和独立调用授权。

## 2. 现有复用点

| 文件与函数 | 已有能力 | T5.2 的接入方向或限制 |
|---|---|---|
| [core/model_call_runtime.py](D:/Pycharm/PythonProject/NovelAgent/core/model_call_runtime.py) `ModelCallRuntimeContext.execute_attempt()` | OpenAI/Claude 实际请求经过统一运行时；已有 Intent → 请求 → Receipt 顺序、本地预算拒绝、部分响应和调用不确定分类 | 适合作为每个物理 attempt 的统一金额预检、预留与结算入口；必须在实际请求前完成持久化预算判定 |
| 同文件 `hydrate_tracker_from_store()`、`_replay_receipt()`、`_restore_tracker_reservation()` | 根据不可变调用证据恢复 token 占用；同请求重放响应，不重复调用 Provider | 金额应按原冻结价格及原 attempt 恢复，不能按重启后的当前配置重新计价 |
| [core/context_budget.py](D:/Pycharm/PythonProject/NovelAgent/core/context_budget.py) `RunBudgetLimits`、`RunBudgetTracker` | 调用、输入/output token、预留、结算和弹性修复接口；`ensure_model_call()`/`restore_model_call()` 支持幂等恢复 | 可复用接口形状；当前金额为内存 float，计时从构造时重新开始，不具备单章持久化语义 |
| [core/engine/chapter_candidate.py](D:/Pycharm/PythonProject/NovelAgent/core/engine/chapter_candidate.py) `ChapterCandidateExecutor._execute_claimed()` | 从本 Candidate execution 的 ModelCallStore 恢复调用预算；通过 ContextVar 向生成过程传递 runtime | 需要注入服务器确定的单章预算服务，覆盖同章的不同 Candidate、新幂等键和 resume |
| [core/engine/chapter_fact_extraction.py](D:/Pycharm/PythonProject/NovelAgent/core/engine/chapter_fact_extraction.py) `ChapterFactExtractionExecutor._execute_claimed()`、`_durable_openai_fact_provider()` | 提取已有独立 execution、稳定逻辑 call ID、原响应复用和不确定调用阻断 | 默认 Provider 目前自行创建无 tracker 的 runtime，必须接入与生成相同的单章预算 |
| [workbench/chapter_candidates.py](D:/Pycharm/PythonProject/NovelAgent/workbench/chapter_candidates.py) `ChapterCandidateService.generate()`、`resume()`；[workbench/chapter_fact_extractions.py](D:/Pycharm/PythonProject/NovelAgent/workbench/chapter_fact_extractions.py) `ChapterFactExtractionService.start()`、`resume()` | 已有服务器校验的作品、章节、Candidate、草稿绑定及 executor factory | 可从这些绑定解析预算归属；不能让客户端指定任意预算账户，或因换草稿/幂等键获得新额度 |
| [core/model_call_usage.py](D:/Pycharm/PythonProject/NovelAgent/core/model_call_usage.py) `_cost()`、`project_model_attempt()`、`summarize_model_attempts()` | Decimal、币种、actual/estimated/unknown、费用来源及计算项验证；按 execution/attempt 去重 | 费用契约适合共享；该模块当前明确只读，不应把报告汇总误当执行止损账本 |
| [workbench/model_usage.py](D:/Pycharm/PythonProject/NovelAgent/workbench/model_usage.py) `read_project_model_usage()` | 从受限路径读取项目物理调用证据，覆盖生成和提取 execution | 可作为展示与账本核对来源；目前是项目范围报告，不能直接证明某个单章预算余额 |
| [core/engine/execution_activity.py](D:/Pycharm/PythonProject/NovelAgent/core/engine/execution_activity.py) `execution_activity()`、`execution_is_active()` | 操作系统持锁可观察某个执行是否仍活动；进程退出自动释放 | 可辅助判断未闭合时间区间；锁文件本身不是持续时间凭证，也不能授权接管 |
| [api/openai_client.py](D:/Pycharm/PythonProject/NovelAgent/api/openai_client.py) `chat_completion()`；[api/claude_client.py](D:/Pycharm/PythonProject/NovelAgent/api/claude_client.py) `polish_chapter()`；[api/retry.py](D:/Pycharm/PythonProject/NovelAgent/api/retry.py) `RetryPolicy.execute()` | 已有 `retry_budget_remaining`/`budget_remaining_seconds` 回调；SDK 重试关闭，物理 attempt 由统一边界记录 | 当前章节路径未将单章累计剩余主动时间接入该回调；请求超时和重试等待也应受剩余时间约束 |

## 3. 金额与归属缺口

### 3.1 当前 tracker 不构成金额账本

`RunBudgetTracker` 有 `max_estimated_cost` 与 `estimated_cost` 字段，但 `ModelCallRuntimeContext._reserve_tracker()` 未传入金额，恢复逻辑也不重建金额。现有报告中的零不能解释为实际费用为零或可用金额充足。

当前候选生成每次进入 `_execute_claimed()` 都新建 tracker，仅恢复本 execution 的存储。同章重新生成或换幂等键会得到另一份 execution 预算。事实提取默认 runtime 没有 tracker。若只在生成按钮或 Candidate executor 上加金额预检，提取及后续修复会漏算。

预算归属应由服务器绑定 `book_id + chapter`，关联该章所有生成、重提取、检查与修复 execution。Candidate、草稿修订和请求幂等键是来源证据，不能各自成为自动重置额度的依据。预算授权发生调整时，也必须保留既有消耗和修订记录。

### 3.2 跨 execution 的 attempt ID 会碰撞

事实提取 `_model_call_id()` 固定返回 `fact`，不同提取 execution 都可能产生 `fact-a1`。现有 `RunBudgetTracker._model_reservations` 仅以 `attempt_id` 为键，因此不能直接将多个存储灌入一个 tracker。

单章账本至少使用 `(execution_id, attempt_id)` 或等价的复合命名空间，并绑定对应的 Intent hash。T5.1 的 `summarize_model_attempts()` 已使用 execution/attempt 组合，可参考其去重原则。

### 3.3 冻结价格不能直接塞入旧 reservation 字段

[core/model_calls.py](D:/Pycharm/PythonProject/NovelAgent/core/model_calls.py) 的 `_normalize_budget_reservation()` 仅接受非负数值或由这些数值构成的对象，不能容纳 `currency`、价格快照 ID 等字符串。

建议建立独立版本化价格/预算记录，通过 execution、attempt、Intent hash 与原凭证绑定，保留旧 ModelCall 凭证的读取规则。金额采用 Decimal 或明确的精确数值表示，不沿用 float 作为持久化金额依据。

ModelCall Intent 目前没有 `endpoint_type`，`_require_expected_intent()` 也不比较端点。不同兼容服务可能都记录为 `provider=openai`、`endpoint_type=openai_compatible`。冻结价格还应绑定实际配置端点的安全标识，不能把“兼容 OpenAI”当作具体服务商计价身份，也不应保存密钥等敏感配置。

若返回模型与冻结计价模型不同，只有存在明确、经过验证的映射时才可结算；否则保留未知状态。重启后改价或换端点不能重算、覆盖旧 attempt 的冻结依据。

### 3.4 结算分类和复杂计价

按主计划，确定未发送及本地预算拒绝可释放金额预留；可解释用量可结算；已经尝试但费用未知、部分响应或调用状态未知，应保留占用和原因。普通 HTTP 错误、进程消失或作者“放弃不确定调用”本身不能证明免费。

`core/model_call_usage.py::_cost()` 当前刻意拒绝未明确表示的 cache 复杂计价，避免将缓存明细与 input 总量重复相加。实施应先声明支持的计价形状；不支持的形状显示未知并保持相应预算约束，不能为了得到金额而跳过来源校验。

预留与 ModelCall Intent/Receipt 位于不同持久化记录时，需要定义并验证每个崩溃窗口。恢复必须识别原 attempt 的状态，不能因为结算记录暂缺而漏算，也不能重复预留同一条已结算凭证。

## 4. 主动执行时间缺口

`RunBudgetTracker.__init__()` 每次保存新的 `monotonic()` 起点；`remaining_seconds()`、`report()` 只计算当前对象存活期间的耗时，恢复 token 不会恢复此前时间。直接增加一个持久化“章节创建时间”再相减也不符合要求，因为作者等待和离线时间会被算入执行时间。

[core/autonomy/runtime.py](D:/Pycharm/PythonProject/NovelAgent/core/autonomy/runtime.py) 的 `_assert_session_wall_budget()` 从 session 开始时间计算总历时；它可作为已有 wall-clock 限制的参考，但不能直接充当 T5.2 的累计主动执行时间。

建议在明确的服务执行操作边界记录持久化开始/结束区间，覆盖生成、提取、主动检查和后续修复。区间应使用实际运行时测量的时长，并与对应操作及进程执行证据关联。若同时记录外层阶段和内部 Provider 区间，汇总必须避免嵌套重复计时。

正常结束后累加已确认区间；重启从持久化区间恢复累计值。未闭合且无法根据持久证据解释的区间标记未知并暂停，不能当作零，也不能把整个离线间隔猜作执行时间。仍活动的执行必须先按既有所有权规则处理。

当前 [core/engine/reviewed_chapter_quality.py](D:/Pycharm/PythonProject/NovelAgent/core/engine/reviewed_chapter_quality.py) 的 `prepare_reviewed_chapter_quality()`、`prepare_candidate_chapter_quality()` 是无 Provider 的纯本地检查。计时应放在服务操作边界，不在纯准备函数或普通 GET 中偷偷写账；未来模型检查和修复进入同一预算服务。页面停留和作者审阅应单列为等待/人工活动，不触发模型执行超时。

## 5. 建议最小实施顺序

1. **冻结契约。** 定义单章预算、币种、服务/端点/模型、价格快照及支持的计价项；明确调用/token/累计主动时间上限，缺价格时的能力边界，以及预算变更如何保留历史消耗。
2. **实现持久化账本与恢复。** 保存物理 attempt 的预留、结算、释放、未知原因和来源绑定。先定义预留与 Intent/Receipt 之间的崩溃窗口及幂等恢复，不先接 UI 掩盖语义缺口。
3. **接入物理调用边界。** 在 `execute_attempt()` 实际发请求前完成持久化预算判定；Receipt 落盘后幂等结算。实际用量超过预留时仍如实记录，再阻断后续调用，不篡改已发生的费用。
4. **统一章节路径。** Candidate 与 fact extraction 注入相同的单章预算服务，覆盖新幂等键、改稿后重提取及 resume；为后续模型检查和修复保留同一接口。
5. **实现执行区间与剩余时间。** 重启重建已知累计主动时间；未解释区间暂停。将剩余时间用于请求前检查、请求超时和重试等待，作者等待单列。
6. **接入预检和展示。** 工作台展示冻结配置、已结算金额、仍占用预留、未知项和时间状态，复用 T5.1 的来源区分。旧未知费用不回填为零。

范围建议：先完成逐章路径的可验证单章预算与时间规则；T5.3 的跨章批次预算仍按主计划后续实施，不能借本项暗中改变批次或自动模式范围。

## 6. 有价值的新增测试边界

以下是实施后的测试建议，本次均未执行：

| 边界 | 应证明的行为 |
|---|---|
| 生成后再提取 | 生成已消耗额度，提取因单章剩余额度不足在实际请求前被挡住；改稿、换幂等键、重启不能获得新额度 |
| attempt 命名空间 | 两个 execution 都出现 `fact-a1` 时分别记账；同一个 Receipt 重放多次只结算一次 |
| 崩溃窗口 | 在持久化预留前后、Intent 后、Receipt 后结算前分别中断；恢复的金额占用与实际调用证据一致 |
| 失败分类 | 确认未发送可释放；普通 HTTP 拒绝、部分响应、未知调用保留占用；放弃调用不等于费用归零 |
| 价格身份 | 调价、换兼容服务、返回模型不匹配、不支持的 cache 计价均不能错误套用价格或覆盖旧快照 |
| 实际超额 | Receipt 已存在且实际用量超过预留时，如实记账并阻断下一请求；重启不能抹去超额 |
| 累计时间 | 长时间人工等待不消耗主动执行预算；多次执行累计超限不能靠 tracker 重建或进程重启清零 |
| 未闭合区间 | 进程退出、时钟变化、执行仍持锁时均按证据分类；无法解释的区间保持未知并暂停 |
| 重试与超时 | 物理 retry 使用同章额度；剩余时间不足时不开始下一请求或过长等待；Receipt 重放不冒充新的 Provider 执行 |
| 只读与历史 | 普通状态查询不创建新费用、执行区间或调用；既有 ModelCall 凭证、未知费用和旧审核数据不被迁移伪造 |

已有 `tests/test_model_call_runtime.py`、`tests/test_context_budget.py`、`tests/test_model_call_usage.py`、候选与提取测试可作为回归基础；它们此前覆盖的 token/重放行为不能替代上述单章金额与主动时间验收。无新实现前不需要为本调查重复运行。

## 7. 完成状态

本记录完成了 T5.2 的实施前定位。尚未交付价格冻结、持久化单章金额预留/结算、统一跨路径预算、持久化主动执行区间或对应验收，因此 **T5.2 继续保持未完成**。
