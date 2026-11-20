# T7.2 路径登记、迁移与 EA 全局写入边界

2026-09-14。基础、相关回归及正式并发提交／实际恢复只读联通检查均已通过。完整恢复交接与启用仍未完成。

## 本次实现

`RootRegistryService.ensure` 在建立事务目录、锁文件及根移动保护目录之前取得机器资格；同时检查调用方 root map 和已保存 registry 的实际路径，不能通过省略原作品根绕过检查。锁内重读 registry 与摘要，发现变化直接拒绝。单 registry remap 先取得相同边界，进入原事务锁后再比较初始 registry；原逻辑 UUID、CAS 与移动限制保持不变。

整本 `remap_story_project_roots` 在取得原 EA／迁移锁及创建自己的日志之前检查新位置的实际作品。已登记作品移动后不能用旧路径重绑定入口绕过现机登记；最终恢复必须走尚待完成的正式资格交接。

`migrate_story_project_runtime` 在目标操作前取得机器资格与原项目状态锁。历史运行记录表明具体 book_id 时，在创建身份和暂存目录前再按该ID核对；身份文件丢失不能让已登记作品变成未登记迁移。预检仍只读；成功迁移仍保留源文件。

EA普通写操作与全局reconcile现在在 `_barrier_layout` 之前检查，而不是只在后续事务前检查。该函数调用resolver.ensure_parent，会创建空目录，本次故障测试确实抓到了原实现放错位置造成的空目录。已前移检查并保留拒绝后目录不存在的断言。初始化失败时ExitStack会释放已取得的机器锁。

## 验证

- session89100 exit1：65 passed、3 failed、22 subtests，41.32秒。两处失败暴露EA解析创建空目录，已修正；另一处是新remap负测给出不存在的导出根，先被原路径规则拒绝，现给有效已存在目录后测试资格拒绝，不放宽路径规则。
- session90606 exit0：68 passed、22 subtests，41.10秒，`.tmp/stage-f-t72-registration-final.xml`。包括全部14项新测试、runtime迁移、CLI根登记／整书移动、EA屏障及备份与迁移争锁。
- session94070 exit0：76 passed、1 skipped、6 subtests，39.86秒，`.tmp/stage-f-t72-registration-regression.xml`。包括v2.1、EA实际执行器、上一检查点v1/v2机器保护及memory历史修订。跳过项为Windows不支持目录symlink。
- session53606 exit0：2 passed，53.31秒，正式并发提交和混合历史实际恢复只读联通通过，`.tmp/stage-f-t72-registration-integration.xml`。双提交只产生一个逻辑请求；真实恢复副本在旧目录不可用时可正常只读验证，旧路径写guard仍拒绝。

新增用例使用实际身份、文件、SQLite物理文件及合成机器记录，不将夹具构造称为生产登记。真实子进程在根锁文件创建前、registry实际替换时、迁移发布前及EA操作期间均不能取得机器资格。失效登记的反向检查比较完整样本文件清单，并明确检查EA/runtime/remap日志目录未新增。

本组件不涉及新的网页入口，未声明完整浏览器恢复交接已通过。真实Provider与真实作品写入均0；所有样本位于.tmp。

新增14项计入T7.2基础累计113（此前99），T7.1仍405。全部测试session终态，无活动测试／服务。

## 下一步

工作台 `OutlineBatchService` 的 create/update/approve/adopt 与 `AmendmentService.submit/recover_after_restart` 仍会在正式事务前写SQLite、建立目录或保存调用凭据。需在进入DB写事务之前取得机器资格，并同时核对调用工作台的实际数据库和project_id，不能只验证作品路径。多项目启动恢复不能无条件批量更改已停用作品的记录。关联检查必须在锁内重读，已批准结果／版本列表等纯读行为保持清晰。

此外仍需正常登记公共入口、缺失账本与从未登记的区别、当前generation CAS与持久restoring、原副本撤销、显式续接／回退、旧计划失效与预算保留、新执行目录、实际恢复后下一章、恢复来源再次备份及最终向导。现有root registry没有自身book_id，本次路径识别不能声称解决了身份与所有关联证据同时丢失的情形；后续持久登记证明必须覆盖这些情况。
