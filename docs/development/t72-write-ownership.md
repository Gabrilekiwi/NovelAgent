# T7.2 同机写资格：接入中的写边界

日期：2026-09-14。完整所有权交接尚未实现；恢复副本保持只读。本文件记录将正常写入口接入现机账本的实际进度，不能替代最终双实例停写/启用/下一章场景。

最新补充：执行器（含Provider凭据及连续执行会话汇总）与v1直接 prepare/commit/complete/reconcile 已接入并验收，新增20项，详见 `t72-legacy-write-ownership.md`。下方验证记录属于辅助边界当时的验收；剩余表中执行器与v1两行由新文档更新。v2 discovery、其他普通写入口与实际所有权交接仍未完成。

再次补充：v2 prepare/commit/complete、reconcile discovery与实际GC已接入，基础62通过100子测试、真实进程1通过、正式工作流71通过3子测试；新增15项，见`t72-v2-write-ownership.md`。当前后续为RootRegistry/迁移/章纲/修改建议等上层早期写入及真实机器交接，下方历史状态不替代最新文档。

## 已验收的辅助写入边界

`workbench.book_ownership.book_output_write_lock` 根据实际输出位置逐层识别项目身份，只读取身份的book_id以寻找本机账本，不使用身份中声明的路径。已登记作品必须处于active，根路径、根/数据库父目录物理绑定、数据库文件身份与作品身份摘要都必须匹配。坏身份或坏账本不能退化为“尚未登记”。未登记的现有legacy辅助写入保持原契约，不自动创建所有权记录。

`BookOwnership.locked` 与辅助写入口先取得同一个由账本规范路径确定的OS状态锁。即使尚未有首次登记，这道锁也存在，因此首次交接不会穿过正在写入的legacy辅助操作；不会为此创建默认机器账本目录。已登记所有权操作随后仍保留原来的机器目录InstanceLease。公开的竞争失败类型保持InstanceAlreadyRunning；辅助入口保留PersistenceLockError。

同线程上下文只借用现有真实锁句柄，继承已有测试注入的机器目录；复制上下文到另一线程不能借用原锁。核心 `project_output_lock` 在原有项目/输出锁之前取得机器锁，落在已识别项目目录内的审核输出、autonomy JSON/队列和file delivery路径因而共享资格检查。项目外部输出仍需要其调用者按任务book_id接入，不能仅靠输出路径识别。通过惰性导入复用现有BookOwnership，未建立第二套账本；最终交接仍使用固定的默认机器目录，不增加用户可随意切换的生产账本参数。

## 验证记录

- 首批session66260：15通过、1失败。新的11个测试通过；旧竞争测试发现新共享锁改变了BookOwnership的异常类型，已保持原有公开约定。
- session86273：41通过、1失败，涵盖新边界、旧BookOwnership、审核/autonomy/投递写入回归。剩余失败是旧锁测试用`{}`伪装项目身份；现改为真实`ensure_project_identity`创建身份，不放宽损坏身份的拒绝。
- `.tmp/stage-f-t72-output-first-enrollment.xml`：1通过，1.61秒，真实子进程不能在未登记legacy辅助写入期间取得首次机器资格，且未创建机器账本目录。
- `.tmp/stage-f-t72-output-identity-final.xml`：3通过，4.04秒，损坏身份/账本拒绝及修正后的真实审核输出嵌套锁测试。
- 现有正式提交与备份源边界回归session88787 exit0：20通过，297.00秒，`.tmp/stage-f-t72-output-publication-regression.xml` / `.log`。

当前新增13个不同用例均已通过，按当前收集ID对照成功XML核对。原无参数的坏账本测试后来分为record/identity两个参数，旧无参数ID不重复计数。辅助与旧所有权/审核/autonomy/投递成功并集44项；另有上述20项正式提交/源锁回归。T7.2基础累计64（此前51+本次13），T7.1仍405。所有运行已终态，无浏览器服务；没有新增网页交接入口，不声称浏览器已验证完整所有权交接。全为合成作品，真实Provider与真实书写入均0。

实际测试调用autonomy的原子追加入口；覆盖停用、restoring、同身份复制书、同字节替换数据库、坏身份/账本、同线程嵌套、跨线程上下文和两个方向的跨进程争锁。尚未增加激活API、机器generation CAS或恢复启用凭据，不声称辅助边界等于所有入口覆盖。

## 剩余覆盖与顺序

| 实际入口 | 现状 | 下一步 |
|---|---|---|
| `ChapterPublicationService._owned_project` | reviewed正式提交已在机器锁内校验实际DB登记，新增共享锁须保持兼容 | 验证现机代际交接后原登记拒绝、新登记允许 |
| `project_output_lock` 的审核/autonomy/投递输出 | 本次已接机器边界 | 收集相关回归，保留真实进程证据 |
| `AgentExecutor._invoke_once` / `_invoke_persist_once` | legacy章节执行当前只持目录/状态/Provider相关锁 | 在准备、调用与正式写入之前接机器资格；新恢复执行目录不能复用旧reconcile |
| `LocalPersistenceTransaction.prepare/commit/complete` 与v1reconcile | 直接调用可绕过上层执行器；外部runtime不一定能从自身定位书 | 以真实目标路径/项目身份接边界，保持跨根顺序及旧未提交计划失效 |
| `PersistenceTransactionV2`、正常迁移和润色/章纲 | 多个路径使用persistence_run_lock，部分在更早阶段已创建文件或Provider资料 | 区分读锁与写操作，不能把任意锁一律当写入；逐入口验证真正落盘前的资格 |
| BookOwnership交接服务 | 未实现当前记录CAS、持久restoring和故障续接/回退 | 覆盖正常写入口后，接实际重绑定副本复核，不用归档generation直接加一 |
| 正常首次登记 | 对main/scripts/api/workbench/core的调用检索发现仅有enroll定义，生产入口尚未调用；此前T3文档将正式升级连接列为T7.1/T2.2后续条件 | 接正式导入/升级准入；最终验收不能直接调用测试夹具登记代替真实用户路径 |
| 登记缺失 | 当前辅助guard对不存在记录保留原有未登记流程；这本身不能区别从未登记与已登记后丢失 | 完整legacy交接要建立可验证的持久登记/停用依据并测试账本丢失，不能用缺失记录自动恢复旧资格；reviewed正式提交现有require仍拒绝缺失记录 |

最终还需激活后的目标范围限制、旧候选/任务准备失效和预算保留、新代际运行目录、零自动调用重启、最终新流程确定性下一章、恢复后再次备份的来源证明和完整恢复向导。不得删除或缩小这些交付条件。

本次自审纠正：共享锁最初改变了旧竞争异常契约，修复时保留原异常并重跑真实子进程；旧测试用空JSON标记无法支撑新的book_id边界，改用真实身份构造并补上坏身份拒绝用例。新增门禁仅覆盖上表的辅助输出，未把其通过结果扩大为完整交接。
