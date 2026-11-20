# T7.1 delivery / autonomy 写入生命周期协调

日期：2026-09-11。新增 17 项不同用例，T7.1 累计 181 项。完整 A–F 目标仍 active。

## 实现

- 将审阅输出已经验证的作品路径锁逻辑提取到 `core/engine/project_output.py`；`core/review/storage.py` 保留兼容名称。检查真实父目录的身份标记，使用共享状态锁，不根据内容声明的根路径授权访问。目录层级不会因拒锁而被创建。
- `DeliveryQueue.enqueue`、`attempt`、`resolve_confirmed_absent` 在整个公共操作期间持有包含该队列的作品锁。特别是 attempt 覆盖 claim、adapter、receipt、finalize 和异常退出。队列不在已标识作品内部时，保留原队列锁与租约机制，不凭 job.book_id 猜测外部作品路径。
- enqueue 的 jobs 目录创建移入原队列锁内部。FileDeliveryAdapter 的 legacy 分支与 SafeFileDeliveryAdapter 分别锁实际输出目标及其包含作品，防止从作品外部的队列写进正在备份的作品。UUID 分支取得锁后再次核对原路径 guard；legacy 不补写 UUID 或改写历史载荷。
- `core/autonomy/common.py` 的 state_lock 先取得作品边界，再创建和取得局部根锁；同一操作内的不可变记录、当前状态头仍由原局部锁保护。保留原 5 秒重试及 `autonomy_state_locked` 错误。
- autonomy 的 atomic_append_json / atomic_replace_json 也参与输出与作品锁协调，覆盖独立预览及没有外层 state_lock 的公共存储调用。append 的相同内容幂等和冲突拒绝语义保留。生成或上层操作已经持有作品锁时，同步嵌套调用借用当前有效锁。

## 实践证据

- `.tmp/stage-d-t71-delivery-writers.xml` / `.log`：45 通过、1 跳过、2 个子测试通过，21.69 秒，session 46749 exit 0。含 9 项新增 delivery 用例，既有队列、旧凭证黄金样例和审阅写入回归。跳过为既有 Windows 目录符号链接创建权限限制。
- `.tmp/stage-d-t71-autonomy-writers.xml` / `.log`：51 通过、20 个子测试通过，39.19 秒，session 1549 exit 0。含 8 项新增 autonomy 用例，规划、session、lease、receipt、recovery 回归。
- `.tmp/stage-d-t71-auxiliary-writers-verified.xml` / `.log`：67 通过、42 个子测试通过，106.07 秒，session 53308 exit 0。覆盖全部 17 项新增用例、来源、归档、autonomy outline/CLI/多进程竞争，以及实际 AgentExecutor 的一章提交和必要投递失败后的恢复。
- 最初组合命令误写既有测试类名，收集阶段没有运行测试，exit 1；`.tmp/stage-d-t71-auxiliary-writers-final.log` 保留，不能作为通过证据。修正为实际 `AutonomyRunnerE2ETest` 后运行的是上述 verified 结果。

独立子进程竞争覆盖：备份持锁时，默认或自定义作品内队列 enqueue/attempt、真实 main reconcile CLI 均在创建目录、claim 和 adapter 前拒绝；退出维护后同一命令成功。作品外队列使用 legacy/UUID 文件适配器也不能写进备份作品。全程使用文件投递或替身 adapter，CLI 网络入口被测试替身拒绝，无真实远端发送。

正在执行的 adapter、receipt 写入前、receipt 写入后分别暂停真实子进程：备份被拒绝且作品清单不变，放行后仅一次 attempt、一个 receipt，最终 succeeded、lease 清空。autonomy 则覆盖 append、覆盖状态头、独立预览、arc、lease、恢复式打开和自定义根；备份期间源文件/目录清单不变，结束维护后可正常写入。另将 arc 暂停在不可变修订已发布、head 尚未发布的位置，备份无法进入且没有创建 runs，原操作随后正常发布 head。

全部测试终态，无活动网页服务或测试子进程。未调用真实 Provider，未改真实作品。没有新的产品页面或浏览器结论；旧浏览器创建表单的自动审批 credits 拒绝、并行代理 credits 中断仍保留待验。

## 仍需完成

1. 作品锁解决了正在执行的写入竞争，不替代持久状态核验。投递进程死亡可留下 delivering / uncertain、无回执调用或 receipt 已写而 job 未更新；autonomy 也可能留下活动租约/session/operation。备份预检必须严格只读识别这些状态并阻止可恢复认证，不自动对账、修复或发送。
2. 不能直接复用 `core/engine/root_registry.py::_active_or_invalid_autonomy_sessions`：它调用的 `BookLeaseStore.load` 会进入 state_lock 并运行 `_reconcile_history_head`；这是可能写入的恢复边界。它只适用于原重定位协议，不能未经处理用作快照预检。`resolve_session_id` 在缺 latest 时也可能重建指针。
3. 本次证明队列/存储位于已标识作品内的默认与自定义路径，以及指向作品内的文件输出。没有认证任意外部路径中仅靠 book_id 声明归属的队列，也没有认证所有脚本及私有底层函数。未知根保持预检阻断，完整支持入口清单、全部历史/根证据仍待收束。
4. 加载代码版本证据、产品归档 API/UI、T7.2 新绑定与同机所有权交接仍未完成。现有包继续 `diagnostic`、`recoverable=false`。T5.2 金额依据与后续预算工作未完成；本轮测试不代表完整阶段 D 或完整开发计划已经交付。

下一步：实现 delivery/autonomy 严格只读的未完成状态与证据检查，验证崩溃后留存状态不会被误认证或改写，再完成剩余支持入口和归档产品化。
