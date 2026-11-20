# T7.2 生成执行器与 v1 写入资格边界

2026-09-14。仅验收本组件；完整恢复交接、恢复启用和 T7.1 认证尚未完成。

## 已实现

`AgentExecutor` 在 Provider 锁与调用凭据落盘之前取得当前机器作品边界，范围包括实际配置的作品根、snapshot、memory、章节及执行输出路径。即使 `persist=False`，只要运行可能调用模型或保存凭据，也检查资格；纯 dry-run 仍可只读运行。直接 `_invoke_persist_once` 还在身份准备、恢复及生成前检查。连续执行的外围也持有边界，覆盖各章结束后的会话汇总与失败诊断。

`LocalPersistenceTransaction.prepare/commit/complete_publication` 与 v1 单事务 reconcile/complete 接入同一机器记录及 OS 锁。事务目录可在作品之外，因此检查日志路径、声明根和实际目标，不仅检查 runtime 目录。声明 book_id 不匹配实际项目时拒绝；已登记 ID 没有实际项目根不能单凭账本推断授权。无声明 ID 的写入仍从目标路径定位作品。

准备好的事务在两次操作间停用后，提交不能落盘；已部分应用而未留下 marker 的事务也不能绕过停用做回滚。reconcile 的已完成／已回滚分支保持只读，不要求重新取得写资格；真正改变 manifest、回滚文件或完成发布的分支才取得写锁。函数式 reconcile/complete 在取得锁后重读并比较 manifest。

没有修改任意 `persistence_run_lock` 为全局写门禁，也没有改写旧 manifest 格式或归档历史。

## 验证

- 首批 session32924 exit0：32 passed、6 subtests，9.41秒，`.tmp/stage-f-t72-legacy-machine-first.xml`。其中新增17项。
- 生成、EA、身份、旧机器边界回归 session85416 exit0：111 passed、1 skipped、14 subtests，60.98秒，`.tmp/stage-f-t72-legacy-machine-regression.xml`。跳过项为 Windows 不支持创建目录 symlink。
- 自审后补入连续执行汇总保护及3个用例；最终 session51756 exit0：117 passed、16 subtests，25.12秒，`.tmp/stage-f-t72-legacy-machine-final.xml`。包括全部当前新增20项、v1事务、read-set、旧凭证投递及执行器回归。

新增测试使用真实 legacy 身份、隔离 SQLite 文件及显式合成机器记录，不把测试中的登记构造称为真实 onboarding。实际执行器以 dry-run 替代模型完成 snapshot 前进，真实子进程在调用凭据创建前和实际目标替换期间均不能取得机器锁。停用、外置日志、ID错配、身份文件缺失、无ID目标定位、部分应用恢复与只读历史均有检查；拒绝前后比较整个样本文件清单。纯只读 dry-run 不写文件。

当前新增20项计入 T7.2 基础累计84（此前64），T7.1仍405。三个测试session均终态，没有测试／浏览器服务。未新增浏览器恢复激活入口，不声称本轮验证了完整网页恢复交接。真实 Provider 调用和真实作品写入均0。

## 下一步与限制

1. 新版 `PersistenceV2Transaction` 的 prepare/commit/complete，以及 `reconcile_pending_persistence_v2` 的 discovery 都有实际写入。尤其 discovery 会整理 staging/journal/registry，必须在动作之前识别当前作品并取得边界，不能仅保护目标替换。
2. RootRegistry remap、迁移、章纲批准、修改建议等上层入口仍有提前落盘；逐入口覆盖，不用通用读锁替代操作授权。
3. 机器记录的当前 generation CAS、持久 restoring、原副本停用、显式继续／回退仍未接入实际恢复副本。现有 gate 只检查现机记录，并不等于新旧代际交接已经完成。
4. 缺失账本与从未登记的区别、正常首次导入／升级登记、旧计划失效并保留预算、新执行目录、恢复后再次备份、最终向导和续写验收保持未完成。

自审纠正：只在每章 `_execution_scope` 加门禁会遗漏章后会话汇总，因此已补公共 `run_loop` 外围保护，并用停用前后完整清单验证没有额外诊断写入。开发检查点同时补充此前 D 阶段文件清单未单独纳入的 `executor.py` 和 `book_ownership.py` 哈希。
