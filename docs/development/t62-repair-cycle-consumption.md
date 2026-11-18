# T6.2：单次修复资格持久化

2026-11-04。本轮实现单章原预算内的不可重复领取记录及恢复关系检查。**尚未接入批次授权验证、修复调用、事实重提取或自动发布，完整 T6.2 及 D/E/F 未完成。**

## 实际链路与前置条件

Reviewed Candidate 的 max_repair_attempts 固定为 0。当前事实提取服务、事实审核、提交准备与发布入口均要求作者确认；prepare_reviewed_chapter 显式拒绝非 author 的 review_source，绑定 acceptance_kind=author。自动调用这些入口并传 author_confirmed=true 会伪造审核来源，不符合计划。

因此本轮先补原章账本中的周期消耗记录，后续必须先实现真实持久化批次授权、冻结策略及 T5.3 统一批次预算，再加入 policy 审核与修复执行链。不能直接复用旧 worker/scene repair 跨过 Reviewed v3 身份与读集校验。

## 已实现的存储约束

- build_repair_cycle_claim 重新计算服务器原准备/质量的 disposition；只有整体 repair_once 才能建立记录。暂停、无缺陷、短语义疑似重复、必要检查缺失均拒绝。绑定原正文、准备、质量、处置与策略 SHA、批次和预算 SHA，生成确定的 claim/execution 身份。
- 批次授权 SHA 目前是内部绑定，**不是授权真伪证明**。execution_authorized 始终 false。没有新 HTTP 入口；该原语不会发 Provider、改作者稿或接受事实。
- ChapterBudgetStore.claim_repair_cycle 在原章预算锁内，要求调用方提供真实所有权/来源/授权的 before_claim 复核；未结算调用或已超限预算拒绝首次领取。锁内先原子持久化，才返回给后续执行器。同一章原预算已消耗后，换批次或换请求不能获得第二次资格；原记录可只读核对及幂等读取。
- 首次明确领取才由 ledger v2 写入 v3；旧账本不自动升级。周期直接存在 ledger.json 中，不依赖可丢失后被当成“从未领取”的可选边文件。v3 缺失/null/损坏周期或账本缺失均拒绝。普通预留、发送标记、结算保留原消耗。
- 离线归档用量读取复用同一 v3 校验，不初始化、不结算。恢复只允许消费记录保持原样；归档已有 v3 周期时，不能用金额相同的旧 v2 空资格账本或另一个有效批次记录替换。

原账本及最新外部控制凭据全部丢失的问题仍属于整套恢复认证范围，不能靠本次 SHA 或单文件记录解决；没有放松现有源缺失/消费未知的可写恢复门禁。

## 已发现的实际失败

第一轮 `.tmp/t62-cycle-first.log/.xml`：16 通过、1 失败，exit 1。两个真实 Windows 子进程首次创建预算锁文件时，MoveFileExW 将已存在错误包装为 OSError(183)，旧代码只处理 FileExistsError，后到进程异常失败。core/chapter_budget_lock.py 现仅接受确切的 Windows 已存在代码 80/183，随后仍校验普通文件、硬链接数和 reparse 属性，不吞掉其他 I/O 错误。

第二轮 `.tmp/t62-cycle-second.log/.xml`：19 个新存储/进程/离线恢复专项、33 个原章预算存储、37 个原预算 runtime 用例逐项通过；整轮因第一个旧恢复回归失败而 exit 1，不能称整轮通过。共享 ready 夹具之前已增加第 2 章 token-only 预算，旧恢复断言错误按列表首项读取第 3 章金额。现改为按 chapter=3 选取，仍核对精确 0.000769 和 2 个时间区间，并加强非零检查，拒绝 None；同类激活测试也改为按章号定位。

第二轮中的真实专项包括：os._exit(74) 在账本持久化后强退、冷进程读取原资格；两个真实子进程竞争不同批次只有一个成功；删除/损坏原凭据不生成空额度；实际原调用预留/发送/结算仍保留资格；使用真实账本字节的离线投影和恢复消费单调性检查。不是完整修复执行或完整工作台重启演练。

## 验证续接

生产源清单 `.tmp/t62-cycle-current-source-second.json`，指纹 `df7f7830dbdb5a0a3cb612f5ce762999f9b08c50cb57ff09276f782be8c6c70c`。

修正断言后的恢复/激活专项原 session 70156 明确 exit 0，5 passed / 123.88 秒；`.tmp/t62-cycle-restore.log/.xml`，逐项凭证 `.tmp/t62-cycle-restore-events.jsonl`，全部 5 项 setup/call/teardown 与 sessionfinish 0 已核对。此前 89 个已通过节点的凭证在 `.tmp/t62-cycle-second-events.jsonl`，整轮失败照实保留，不能把它改写成 exit 0。共 94 个不同验证节点，包含 19 项新专项与 75 项相关回归，不累计第一轮重复通过项。

生产源在两轮验证前后相同，前 89 项的测试文件未改、最终 5 项测试文件与运行前 SHA 一致，已由 `.tmp/t62-finalize-progress.py` 核对。生产与测试运行时冻结。机器终态 `.tmp/t62-repair-cycle-progress.json`：当前无活动测试或服务，完整任务未完成。

没有真实 Provider/真实作品写入、没有新增正式接受。没有公开页面行为变动，本轮未声称浏览器修复执行已验证。上一组件的原文证据 UI 验证保持其历史构建范围。下一步建立可验证的真实批次授权与批次统一预算，并接原周期的修复、全量事实重提取及全检查；重启后只允许显式续办原执行，不自动发起新付费调用。
