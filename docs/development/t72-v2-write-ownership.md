# T7.2 v2 事务、恢复扫描及清理的机器写入边界

2026-09-14。本组件的基础、真实进程检查和正式升级／章节工作流回归均已通过。完整恢复交接与启用仍未完成。

## 实现

`core/engine/persistence_v2_ownership.py` 复用现有 `book_output_write_lock`，不新建账本或替代事务协议。

- prepare 在创建事务目录之前，按 root map、实际目标、最终记录和 receipt 路径取得机器边界；commit/complete 根据实际 manifest 与当前 registry 取得边界，在原事务锁内再核对 manifest 没有变化。
- 已完成事务的 `complete_publication` 保留纯凭证验证；需要补建登记的 discovery 仍是写操作。没有让所有持久化读锁都要求写资格。
- 恢复前只读收集当前 root registry、staging/journal manifest 和四类 registry entry 的哈希、目录项、作品 ID 与实际路径。临时 manifest 不完整时仍能由当前 registry 定位项目；manifest 和项目身份损坏时，已保存的任务登记 ID 也参与检查。
- 先取得实际作品机器锁，再核对关联快照；取得原事务锁后再次核对，随后才搬移 staging、补 pending/终态登记或改恢复状态。逐事务处理还检查当前 manifest 的实际边界。资格拒绝与竞争异常直接传播，不转成会写登记的 recovery_required 分支。
- 既没有 registry、也没有有效登记／manifest 的不明外部 staging，不自动隔离或删除。已登记 ID 缺少实际作品根也不能通过。
- GC 预览仍只读；实际清理取得机器边界和原事务锁，检查关联未变化后才删除原策略选出的内容。

## 验证记录

- session91150 exit1：75 passed、3 failed、100 subtests，64.29秒。新测试误把 unittest helper 类作为模块属性导入，导致整组被重复收集；两个相同失败是新的身份边界提前拒绝错误作品，另一个为旧GC夹具只有registry entry。现改为模块导入，旧错配测试明确检查BookOwnershipError，缺read-set测试使用正确book_id以仍然验证原read-set门禁；恢复关联增加实际registry entry ID，不放宽已登记作品根检查。
- session82332 exit0：62 passed、100 subtests，60.91秒，`.tmp/stage-f-t72-v2-machine-final.xml`。其中当前基础新增14项，不再重复收集 helper。
- 独立真实强退检查 exit0：1 passed，2.03秒，`.tmp/stage-f-t72-v2-machine-fresh-process.xml`。子进程在完整staging落盘、发布journal之前 `os._exit(79)`；停用后另一新进程调用正常reconcile，以BookOwnershipError退出80，样本文件清单逐字节不变。
- 正式工作流session50655 exit0：71 passed、3 subtests，531.16秒，`.tmp/stage-f-t72-v2-machine-workflow.xml`，覆盖reviewed升级、正式章节事务、工作台提交、EA执行器及原EA事务屏障。运行期间以同一session和PID23124持续增长的CPU时间确认存活，未因输出缓冲而重启。预先独立收集也确认71项。

本组件当前15个新增用例的成功结果已落盘，按最终14项加真实进程1项去重核对，T7.2基础累计99（此前84），T7.1仍405。全部session终态，无活动测试／服务。测试均为合成作品／合成登记；真实Provider和真实作品写入0。没有新增恢复激活网页，不声称完整网页恢复交接已验证。新模块位于backup_version的core扫描树内，自动纳入软件来源指纹；无需另造版本清单。

## 后续要求

继续覆盖 `RootRegistryService.remap/ensure`、runtime迁移，以及章纲／修改建议等更早的文件和数据库写入入口。这些上层步骤不能仅依赖后续事务已受保护。例如章纲批准会先进入SQLite事务并创建runtime，修改建议会先保存文件及调用凭据，必须将整个写操作纳入一致顺序。

本组件不建立新的代际：当前记录CAS、持久restoring、原副本撤销、显式恢复续接／回退和正常首次登记都未完成。缺失账本与从未登记、所有关联证据都丢失、旧计划失效且保留预算、新执行目录、恢复来源再次备份、最终向导及下一章验收仍按原计划推进。恢复出的工作台继续只读。
