# T5.2 实施记录

2026-09-10，统一模型调用预算协议与 SDK 主动时间接入已通过组件验证，**T5.2 未完成**。阶段 C 已验收，本项是阶段 D 的当前主任务。以本文末尾最新检查点为准；前面组件记录保留历史，不代表当前仍未接统一运行时。

## 当前组件

`core/chapter_budget_contract.py` 提供冻结单章预算、完整规范/hash校验、精确服务/模型/端点身份选价及 Decimal 费用计算。金额和价格为规范十进制字符串，拒绝 float/bool/非有限值。单章只使用一个明确币种，不隐式换汇；有金额上限必须有价格。输入计费明确包含或不包含缓存；读取、5分钟写缓存、1小时写缓存分开，未知非零缓存没有价格时拒绝计算。预留按已支持的最高输入费率乘本次全部可计费输入上界。这里的上界由后续受信调用适配器证明，不能把现有近似 token 估算改名为保证上界。

`core/chapter_execution_time.py` 提供独立持久执行区间：先写入开始，再执行，最后保存实测 monotonic_ns 耗时；普通异常也记录已耗时间，进程死亡留下未闭合区间。下一次执行发现未闭合区间会暂停，不能自动归零或把离线时间猜成主动执行时间。真实 OS 锁区分仍运行与已失去执行者；只读报告不修改记录。每个账本绑定 book/chapter/冻结预算 hash，换章、换金额或时间上限均不能复用旧时间账本。

正常结束后，调用方必须明确是否进入作者等待。报告分别显示累计主动执行、作者等待、其他未执行间隔及端到端历时。作者等待包括该明确等待状态持续期间的离线时间，不宣称是实际阅读耗时；系统时钟倒退使墙钟统计为 unknown，已记录的单调时钟耗时保留。活动句柄提供当前剩余主动时间，供后续 SDK 超时/重试预算使用。

两个组件尚未接入工作台生成/提取或 API，不得从组件通过推断当前工作台已经能够按金额止损。时间账本的 initialize 只允许在首次创建冻结预算时使用；恢复必须只读已有记录，缺失账本不可自动新建。

## 接下来必须完成

1. 持久账本组件已实现，接下来由服务器按 book_id+chapter 选择唯一目录；物理 attempt 使用 execution_id+attempt_id 复合键。不同 Candidate、稿件修订、新幂等键必须接入这一目录，当前尚未接产品服务。
2. 冻结价格与 ModelCall Intent/Receipt 用 hash绑定；旧 ModelCall 1.0 凭证保持可读，不能把币种字符串硬塞入原数值 budget_reservation 字段。所有收费类错误、缺回执和无法解释的用量保留占用；确实未发送/本地预算拒绝才释放。
3. 为预留/Intent/响应/Receipt/结算每个崩溃窗口定义恢复；复用原 Receipt 只结算一次、零额外 Provider 调用。缺失或互相冲突的证据暂停，不凭当前配置重建新价或清零。
4. `ModelCallRuntimeContext.execute_attempt()` 接入请求前持久预留与回执后幂等结算；OpenAI/Claude 适配器传入安全的端点指纹、真实请求对应的输入计费上界和剩余主动时间。未知 tokenizer 的现有校准估计不是保证上界；缺乏价格或上界时仅明确 token/调用限制，金额模式应拒绝请求。
5. Candidate 与 fact extraction 共享同章账本。当前 fact provider 自建无 tracker runtime，必须修正接入；本地检查与后续一次修复使用相同预算服务和时间边界。持久执行计时放在顶层操作边界，避免嵌套重复计时。
6. 工作台冻结/预检/消耗/暂停原因展示，以及未知时间区间的明确恢复处理。T5.1 的估算展示需要接冻结价格与受验证结算证据，不能抹去 actual/estimated/unknown 分类。
7. 验证跨生成/提取总额、换幂等键、重启、相同 fact-a1 不同 execution、所有中断窗口、缓存计费、改端点与返回模型不符、累计主动耗时和人工等待。随后实际浏览器验证超限零请求及重启不重置。

价格全部为合成测试输入，未查询或采用真实供应商报价，未调用真实 Provider。

## 组件验证检查点

纯契约 102 项通过，计时最终 14 项通过；XML `.tmp/stage-d-t52-contract.xml`、`.tmp/stage-d-t52-time-verified.xml`，均终态 exit0。新增共116项，不等于T5.2整体通过。计时初轮将目录安全校验误用于文件，修正后12项通过；缺失目录错误类型规范后单项通过。独立复核又发现中途monotonic回退仍高于起点时可能返还剩余时间；现与最近一次观测比较，并永久标记本区间计时失败。即使业务捕获异常、时钟回升，finally仍保留未闭合未知区间；新增回归覆盖此路径。低Decimal上下文精度由组件局部高精度计算隔离。初始失败报告保留，最终以verified XML为准。

目前无测试或服务活动。协作审查在交付上述问题后遇到额度错误，父线程完成修正与验证。后续从持久金额账本继续，勿重做阶段C浏览器演练。

## 持久金额账本检查点

新增 `core/chapter_budget_store.py`：独立不可变冻结配置与原子替换的预算账本，所有变更持锁；每次读回重验冻结hash、账本hash、完整原Intent/Receipt、请求和端点绑定、按冻结价格重算的预留。配置存在而账本缺失时拒绝重新初始化，不能换上限或换章继续用旧目录。Intent与Receipt仍由调用运行时负责真实持久化；本组件不调用Provider。

同章所有execution使用同一账本，复合attempt键避免多个fact-a1混同。同一次预留或结算可重复读取而不重计。未发送和本地预算拒绝释放额度；缺回执、部分响应失败、计费数量不全、缓存细分冲突、返回模型/端点变化都保留占用并阻断新开支。用量超出预留仍如实记入，再拒绝下一次开支。金额模式必须提供绑定实际request_digest的可计费输入上界及证据hash，不接受校准估算标签。上界正确性必须由下一步受信适配器证明，当前仅校验绑定与形状。

缺价格模式仅控制调用/token，charged_amount明确null。没有冻结缓存计费语义时不释放输入预留，避免把可能排除缓存的input计数误当总输入。金额模式只使用明确Provider计费数量释放未用预留；缺失字段保留unknown。此保守状态将需要明确恢复入口，不能把用户“继续”按钮直接做成清零。

新增 `core/chapter_budget_lock.py`，创建已落盘锁后使用现有要求文件身份的锁入口，拒绝硬链接或重解析锁文件；计时组件也改用此入口。真实子进程持锁/退出与硬链接边界已验证。

首轮账本27项通过，加入锁边界后29项；与计时14项最终合跑43项通过（session43188 exit0，`.tmp/stage-d-t52-storage-verified.xml`）。加纯契约102项，当前组件共145个不同新增用例通过。没有重跑阶段C或全套回归，没有活动进程。当前仍是T5.2组件验收，未接ModelCallRuntime、真实预算配置、SDK输入上界/超时或页面。

下一具体入口为 `core/model_call_runtime.py::ModelCallRuntimeContext.execute_attempt`。必须先确定预留与Intent写入之间的崩溃语义，再接各终态分支和`_replay_receipt`；不能只在成功分支扣账，也不能把已有无预算的旧Intent在重启时当成新预算。建议为execution建立冻结预算绑定，并把恢复已有Receipt的幂等结算与创建新预留分开；缺预算记录的在途旧调用需要明确分类，不能因本地预算拒绝把可能已发出的调用免费化。新运行时路径需同时测试所有故障窗口后再接工作台。

## 统一调用入口与主动时间检查点（最新）

`core/chapter_model_budget.py` 为实际 ModelCallStore 建立不可变 execution/book/chapter/预算 hash 绑定，恢复、预留、发送、结算全程持有同一 execution 的 OS 锁。锁放在章预算目录，避免另一个 runtime 把仍在工作的准备记录提前关闭。已有无预算 Intent/Receipt/响应目录不得绑定空预算；绑定文件或已发送/已结算物理证据缺失时拒绝重建。已绑定目录也不能通过不传 chapter_budget 绕过检查。实际作品所有权、execution 目录选择仍须由下一步产品服务核验；此组件不授予作品写权限。

预算账本升为 `chapter_budget_ledger_v2`，每个 attempt 新增一次性的 `dispatch_claimed`，旧的仅用于组件试验的 v1 账本不自动升级或改写。物理 Intent/Receipt 1.0 格式保持不变。顺序为：持久预留 → 原 Intent → 本地 tracker/时间检查 → 持久发送标记 → Provider → 原响应/Receipt → 验证原文件后结算。所有终态与回放经过统一结算入口，预算错误明确为不可自动重试的 local_budget。

| 中断位置 | 恢复行为 |
|---|---|
| 预留后、Intent 前 | 从原预留中的完整 Intent 恢复同一 hash/时间戳，关闭为未发送，不直接重发该 attempt |
| Intent 后、发送标记前 | 持有 execution 锁后确认未发送，关闭旧 attempt；上层重试只能创建新 attempt |
| 发送标记后、Provider/响应/Receipt 前 | 可能已发出；保留金额/token 占用，停止新开支，不把已有正文碎片当成功回执 |
| Receipt 后、预算结算前 | 验证原 Intent、Receipt、响应文件后结算一次，返回原结果 |
| 本地超限拒绝过程中 | 原拒绝保持未发送、零消费；刷新/换幂等键不会突破章总额 |

`ModelCallRuntimeContext.active_interval` 使用顶层操作已经建立的持久主动区间，校验其与章预算一致。新请求在预留前和发送前检查剩余主动时间；本地耗尽不发请求，若已预留则用预算拒绝 Receipt 释放未发送占用。已完成回执仍可本地回放。

`api/execution_deadline.py` 与 OpenAI/Anthropic 适配器按当前剩余主动时间、逻辑调用 deadline、SDK 配置和已有额外限制中的最小值设置每次请求的 timeout。重试失败耗时、退避等待不归零；退避睡过头后再次检查。流式响应逐段检查累计时间，关闭流并保存实际已收到的部分正文，不自动重试。SDK timeout 是阻塞 I/O 约束，不能声称操作系统或远端会在精确毫秒截止；已发生的超时/调度超额会如实计入主动耗时，下一次请求被阻断。

验证：最终预算运行时 37 项、账本 31 项合跑 **68 通过**（`.tmp/stage-d-t52-runtime-verified.xml`，session76056 exit0）；含六个真实子进程 `os._exit(42)` 断点和发送标记前/后并发恢复拒绝。首次运行 87 通过、1 项失败、5 子测试通过，失败为测试错误构造 ContextBudgetError，修正后单项通过；历史 XML 保留。

主动时间适配器新增 15 项，与计时 14 项、原模型调用 25 项、重试 10 项合跑 **64 通过、8 子测试通过**（`.tmp/stage-d-t52-deadline.xml`，session1639 exit0）。两家 SDK 均用合成替身，无真实连接。加未变更的价格契约 102 项，T5.2 目前新增不同用例共 **199 项**。这些是组件及统一调用边界验证，尚非工作台端到端预算验收。

下一步直接接产品路径：服务器按作品/章节唯一选择预算与时间目录，冻结配置与首次时间账本必须一致建立；Candidate、fact extraction、本地检查与修复共享这一实例。随后为真实 SDK 请求提供已验证的端点指纹与可计费输入上界。当前 SDK 金额模式还没有默认可信上界/端点适配器，传入 chapter_budget 而缺指纹时会主动拒绝，不能误称已能使用真实金额控制。API 预算冻结、消耗/暂停、未知时间恢复及浏览器验证仍待完成。作者明确恢复必须保留未知消费/时间，不能清零；旧运行时间账本也不能在恢复时 initialize。

原 Candidate、fact extraction 及两者工作台服务回归 **37 通过、2 子测试通过**（`.tmp/stage-d-t52-callers.xml`，session63542 exit0）。所有本轮测试已终态，未启浏览器服务或真实 Provider。原有 pythonw PID13636 未操作。

服务层只读续接调查发现两个必须处理的顺序问题（尚未修改这些执行器）：Candidate 的 `_execute_claimed` 在创建 ModelCallRuntime 前执行全局未决 Provider 门禁，fact extraction 也先把任何 Intent 无 Receipt 判为未决。这会提前挡住新预算协议可证明“发送标记前未发送”的恢复。因此服务接入须在保持作品/Provider/执行所有权锁的前提下，先用原 execution 绑定完成本地预算凭证核对，再评估未决调用；不能直接放宽全局未决门禁。fact extraction 还会直接重用已有成功响应、跳过 Provider/runtime，必须同步核对结算，避免遗留占用。已有 completed/failed record 的快速返回也需区分只读展示与显式恢复，不能让 GET 偷做预算修改。

具体入口：`core/engine/chapter_candidate.py` 的构造函数、`_execute_claimed`、`resume`；`core/engine/chapter_fact_extraction.py` 的 `_execute_claimed` 与 `_durable_openai_fact_provider`（后者目前另建无预算 runtime）；两者 Workbench 服务的 executor factory 以及 `workbench/reviewed_workflow.py::install_reviewed_workflow`。顶层主动区间覆盖实际本地检查/生成/解析，显式审核等待放在区间之外；不能在每个 SDK 调用内新建计时区间。

## 执行器接入检查点（后续进展）

`core/chapter_execution_budget.py` 现在按作品目录中的 `.novelagent/runtime/budgets/c{chapter}` 读取唯一冻结预算与计时账本，不自动创建；核对实际执行 Intent 的 book/chapter/execution、model_calls_ref 和物理目录。它在开启主动区间前调用 `ModelCallRuntimeContext.reconcile_chapter_evidence()`，局部恢复原执行的未发送记录/回执结算，再交给原全局未决门禁。其他执行的不确定调用仍会阻断。目录的作品所有权必须由 Workbench 外层验证，此类不替代所有权服务。

Candidate 与 fact executor 新增 `execution_budget` 注入，主动区间覆盖 `_execute_claimed` 的来源复核、实际生成/提取、结果解析与记录。fact 默认 Provider 复用当前绑定 runtime，并核对 evidence_root；不再丢失其预算和时间。对预算协议确认全为 request_not_sent 的旧尝试，fact resume 可进入同一调用的重试逻辑，先回放未发送终态，再创建新的物理 attempt。已有成功响应走本地解析前也先完成预算结算。

执行结束后 runtime 保留已关闭的时间句柄，避免调用者保存旧对象后因句柄被置空而绕过时间限制。缺计时文件、不同章预算、不同物理执行目录均不继续调用。真实 abrupt process death 的时间区间仍保持 unknown；本组 BaseException 模拟的是可运行 finally 的中断，仅用于执行器恢复顺序，不能替代此前真实子进程故障证据或宣称未知主动时间已可恢复。

新增执行器测试 **8 通过**（`.tmp/stage-d-t52-executors-corrected.xml`）。真实合成 Strict Candidate→fact 共享一次调用上限、作者等待 100000 秒不计主动耗时、未发送窗口恢复、成功回执直接复用、默认 Provider 复用原 runtime、关闭句柄/缺时间账本/错章阻断全部通过。初轮 4 项夹具失败（错误猜测事实目录、从 record 而非原 Intent 读取 source），已修正；初轮还因导入 unittest 类重复收集了原用例，已改用模块导入，重复项不计新增。原 Candidate/fact 不同回归 27 项通过；同轮 XML 中重复收集的 Candidate 项不重复计数。

T5.2 新增不同用例累计 **207 项**。当前仍未把 execution_budget 注入 Workbench 默认 executor factories；首次冻结事务、SDK 可信端点/输入上界、预算API/UI、未知时间恢复、浏览器门槛仍待完成。

执行器变更后的工作台两项服务回归 **10 通过**（`.tmp/stage-d-t52-executor-workbench.xml`，session61458 exit0）；先前31136已exit1、修正后的8项exit0，所有测试当前均终态。不要再次启动同一回归。

## 首次冻结与默认工作台入口（最新）

新增 `core/chapter_budget_freeze.py`。首次冻结持久化不可变初始化 Intent，随后逐个创建完整空预算/调用账本/主动时间，最后发布 ready 凭证。显式重试仅接受相同配置，只补尚未完成的初始化；已存在的非空/改变记录、未知执行痕迹不得覆盖。ready 已存在后始终只读验证，缺任意账本不再 initialize。`ChapterExecutionBudget` 构造和进入执行前均要求 ready 凭证及原预算/时间核验。

新增 `workbench/chapter_budgets.py`，安装 GET/POST `/api/projects/{project_id}/chapters/{chapter}/budget`。GET 只读返回 not_configured/initializing/ready 与已验证消费和计时；POST 要求明确 author_confirmed、冻结配置和 expected_context_sha256，由服务器绑定 book/chapter。使用现有机器作品所有权、Provider 锁及数据库登记边界，要求当前待写章、来源摘要未变、正式事务已稳定，且本章没有此前 Candidate/fact 执行。不能给已经发生但未受预算约束的工作添加空额度。现有调用记录须先按原流程处理，不能通过本接口消除消费。

默认 reviewed 工作台 Candidate/fact factories 现在提供 `execution_budget_factory=ChapterExecutionBudget`。执行器在发布新业务 Intent 前先要求冻结预算，恢复时再核对原执行；默认 Fact Provider 使用同一绑定 runtime。显式测试/内部替换 factory 保持其自行提供依赖的约定，普通产品默认路径已经启用预算要求；不是只测绕过默认入口的定制 executor。

`api/budget_endpoint.py` 对实际 SDK base_url 规范化并计算 SHA-256，区分 Provider/协议/主机/端口/路径/query，仅保存摘要。拒绝含用户密码、控制字符、无效端口和fragment的端点，不在错误里回显原URL。OpenAI/Anthropic SDK 在预算调用时传入实际 endpoint 指纹；token/调用模式现可运行，金额模式仍需真实输入上界与完整计费规则，当前缺这些依据会阻断。

当前官方接口核验发现 OpenAI 文档还包含缓存写入用量及服务档位。已将 `prompt_tokens_details.cache_write_tokens` 纳入只读用量；与旧字段冲突/缺TTL费率时保留未知占用，不能视为免费。依据见 `t52-provider-budget-evidence.md`。未使用真实价格、密钥或Provider调用。

本轮新增：冻结协议14项（包含5个真实子进程强退窗口）、预算API8项（含默认真实工作台服务→替身SDK→事实提取共用调用限额）、端点6项、缓存写入2项，共30项；T5.2新增不同用例累计 **237 项**。证据：`.tmp/stage-d-t52-freeze.xml` 22通过（含旧executor8）；`.tmp/stage-d-t52-budget-api.xml` 7通过；`.tmp/stage-d-t52-default-chain.xml` 初轮29通过1失败，缺失预算根目录的 SafePathError 未规范为产品409，已修正，默认链单项 `.tmp/stage-d-t52-default-chain-corrected.xml` 通过（session92052 exit0）；`.tmp/stage-d-t52-cache-write.xml` 27通过。默认链首次缺失budget时SDK零调用、冻结后生成1调用、fact限额拒绝零额外SDK调用，GET账本一致。

T5.2仍未完成：预算页面和浏览器验证、真实金额模式输入上界/价格档位/缓存规则、T5.1费用投影关联、未知时间区间显式恢复、后续检查/修复共享预算待做。主动计时当前覆盖 `_execute_claimed`；更外层输入准备/provenance捕获的耗时尚需纳入顶层操作，以免遗漏本地工作。不要从现有测试推断已经完整统计全部主动耗时。

默认工作台与原 ModelCallRuntime 最后回归已完成：`.tmp/stage-d-t52-default-regression.xml`，31 项通过、5 子测试通过，session46742 exit0。当前无活动测试，不重新启动同一回归。

## 预算页面检查点

调用/token/时间限额表单、预算占用及暂停原因、读取失败清空、切章过期响应保护已接。Chrome 首次冻结双击一请求、默认服务生成一次后提取暂停、刷新不清零、损坏账本409保留草稿、同库重启读回通过，详见 `t52-budget-browser.md`。新增UI6项，相关最终34通过；T5.2新增不同用例累计243。金额界面和可信输入/价格、未知时间恢复、准备和后续检查/修复主动时间仍待做。

## 成功回执本地恢复检查点（2026-09-11）

独立审查发现并修复未知/耗尽时间阻断成功回执本地完成。显式resume要求唯一验证成功响应，跳过generator/provider，runtime禁止attempt；原time.json保持，恢复操作独立计时并计入已测主动总时。真实子进程和Chrome强退→同库重启→点击恢复草稿通过，详见 `t52-local-recovery.md`。新增9项，最后85项/5子测试通过，T5.2累计252项。所有测试/服务终态。顶层准备/provenance/finalize计时、金额接口/界面和后续检查修复仍未完成。

## 服务顶层主动时间（2026-09-11）

默认工作台generate/start/resume已覆盖确认验证、上下文准备、provenance、executor及服务finalize，同一操作复用唯一interval；精确book/chapter/root/冻结预算/execution绑定，未绑定runtime和跨操作借用拒绝。完成请求重放用仅本地计时，未知/耗尽成功回执恢复不发新请求。8项新专项，相关32回归及最终70项/5子测试通过；浏览器强退→原库恢复单区间2.187秒、原未知时间保留、唯一SDK调用通过。详情t52-service-time.md。T5.2累计260新增不同用例，仍未完成金额及后续检查/修复。全部服务与测试终态。


## 2026-11-03：真实金额请求计划组件

新增 `core/provider_billing_request.py`，尚未接入SDK或价格冻结。依据已实际获取的官方模型、上下文及Chat Completions文档，首个可选能力条目为精确快照gpt-5.4-mini-2026-03-17；不修改应用默认值、不替换用户模型，也不把这一条目当作整个Provider支持目标。

计划约束实际官方标准端点、纯文本消息、明确单份输出、max_completion_tokens、default服务档位、显式缓存模式/30m TTL及流式实际用量。完整最终逻辑请求hash与固定能力hash一起绑定。使用文档上下文能力上限作为保守capability值，不把prompt估算当实际用量，也不据此自行授予发送或花费权限。兼容/地区端点、模型别名、工具/多模态费用、未知字段、额外输出和缺失流式用量选项均拒绝；修改请求或重新hash伪造计划不能通过重派生验证。

`.tmp/t52-billing-request-plan-final.log/.xml`：observed exit0，34通过0.42秒，其中28个请求计划新用例、6个既有端点用例；包括字符串/类型、未知role、巨大数值、NaN、端点query/region/credentials、快照/别名、复数输出、request mutation及伪造摘要等。首次30项通过记录保留，随后补4个对抗用例并修严类型/错误规范化。无活动进程、无Provider或真实作品写入。

**T5.2仍未完成。** 该组件是完整金额接入的前置，当前dispatch_authorized=false，旧SDK缺input_bound仍然阻断金额模式。下一步将请求计划绑定版本化冻结价格（含档位、30m缓存写入等维度），随物理attempt持久化原能力和计划，在发送前核对实际payload，并保存/核验实际响应的模型、档位和缓存用量；未知或不匹配继续占用。不能只把plan_sha填到旧input_bound就称可信金额控制。旧已冻结预算/Intent/Receipt/账本不改写，后续Provider与工作台生成/提取、价格UI/浏览器仍按全计划接入。
