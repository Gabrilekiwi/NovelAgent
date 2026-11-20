# T7.2 实际副本数据库重绑定与只读工作台

日期：2026-09-14。数据库重绑定组件已验收，新增 9 个不同用例。T7.2 基础组件累计 51（隔离13、复制17、物理根12、数据库9）。**完整可写恢复、同机资格交接、恢复向导与下一章验收仍未完成。** T7.1 仍为405，完整 A–F 目标保持 active。

## 实现

`workbench/backup_restore_database.py::prepare_restored_workbench` 接受已经实际解包并完成根绑定的容器。再次从原归档重验目标计划、实现指纹、复制凭据、原历史及当前物理根，持有新数据目录的单实例锁；不访问原书路径。不是替换 root 字符串后直接启动应用。

从归档原数据库复制临时预测库，计算限定 SQL 变更后的完整逻辑摘要。仅修改目标 `projects.root` 与明确的可变文件索引：`drafts.source_path`、`outline_batches.approval_manifest_path`、`amendments.content_path`；相对书路径保持原值，绝对路径必须属于实际归档成员。历史 JSON、原任务事件、候选引用、预算、作者文本与其他作品记录不重写。拒绝不属于支持契约的触发器、视图和虚拟表。

实际副本只接受完整原始 SQL 状态或本次预测的完整 SQL 状态，不能用局部行检查掩盖其他表被改动。意图 `database-binding.json` 先落盘，目标索引修改与 `workbench_restore_scope` 登记在同一事务提交；独立 scope 凭据随后发布。任一中断位置均不能以普通工作台启动，只能显式重验并继续。完成凭据为 `database-binding-done.json`，保持 `restored=false`、`recoverable=false`、`ownership_handoff_completed=false` 和 `write_activation_allowed=false`。

显式续接允许 SQLite 处理本次意图下的回滚日志，打开前拒绝链接和不支持的日志形态。提交前若数据库仍是完整原始状态，允许未被只读打开清除的非 hot journal 暂存；本次事务完成后的完整清单核验必须证明它已消失。WAL/SHM 不在此独立快照恢复契约中。没有自行删除日志或手工改数据库页面。

只读启动复用已有 restore_scope，强制覆盖 `start_worker`、`auto_import_active`、允许作品目录和任务根。`JobSupervisor` 在受限副本构造时不再创建空任务目录。所有非读 API 拒绝，作品/任务/草稿读取限于目标项目，队列不能认领；完整数据库仍保留其他项目资料，内部原始 SQL 连接不是数据库行级权限隔离。

## 自动验证

| 证据 | 结果 |
|---|---|
| `.tmp/stage-f-t72-database-positive.xml`，session71729 exit0 | 实际完整应用/混合历史归档→复制→根绑定→SQL绑定→普通只读应用，1通过，59.88秒 |
| `.tmp/stage-f-t72-database-faults.xml`，session54646 exit1 | 6通过、1失败；发现非 hot journal 的清单拒绝问题，保留失败记录 |
| `.tmp/stage-f-t72-database-crash-final.xml`，session38428 exit0 | 修复后两个真实退出边界通过，65.40秒；其中提交后边界与上一批重复 |
| `.tmp/stage-f-t72-database-flushed.xml`，session94141 exit0 | 真实写出未提交数据库页面后退出，1通过，43.23秒 |
| `.tmp/stage-f-t72-database-regression.xml`，session69469 exit0 | 隔离、任务与恢复计划回归55通过、1跳过，78.72秒；跳过是该Windows主机无文件符号链接权限 WinError1314 |

新增验收按成功 testcase 集合并集计算，共9个。首轮 `.tmp/stage-f-t72-database-first.xml` 的8项准备错误未计入：夹具将 `PathResolution` 对象字符串当成文件路径，已改用 `.path`，未放宽生产校验。

覆盖证据包括：原目标与另一作品目录不可用；目标正文、导出、作者草稿正常读取，下一章位置3；其他作品/任务/草稿409、维护写操作409、队列不认领、后台线程不启动；整库只出现指定行和范围表的变化。重复准备返回同一凭据且数据库字节和完整书清单不再变化。作者稿、另一作品记录、新增表及 scope 凭据被改动时拒绝，不自动修复。

三个实际子进程退出均为 `os._exit(77)`：提交前未写出页面、提交前强制小缓存写出页面、提交后独立scope文件发布前。第三个数据库故障测试明确先比较实际主文件字节已不同于原快照，再用正常 SQLite 显式续接，最终整库摘要符合预测、回滚日志消失。普通 `WorkbenchDatabase` 在中间状态均拒绝启动。

## 实际浏览器

Chrome page11 / context `t72-actual-database-restore`，端口38890；服务session88728、PID16444，现已停止且无监听。

`.tmp/t72-database-browser-server.py` 构造完整真实应用schema、另一作品/排队任务、作者草稿和旧事务→升级→第二章提交，再实际归档、解包、绑定根与数据库。原目标和另一作品目录在整个恢复及浏览器访问期间不可用。SDK构造与非本机网络被禁止；启动参数故意请求worker/自动导入，由scope覆盖。

原生操作从书架进入作品、打开第二章并切换版本页；页面显示已提交2章、下一章3、已审核事实和恢复只读提示。书架接口仅返回新目录下的目标作品，章节接口返回实际正式稿。直接导航另一作品返回409及明确提示，未返回其他作品资料；错误提示下仍保留上一目标页内容，这是既有页面错误展示行为。返回目标概览并忽略缓存刷新后7个读取请求均200，保留控制台只有本次预期409。

证据：`.tmp/t72-database-browser-info.json`、`-before.json`、`-evidence.json`、`-after.json`。浏览器后独立核对新书70个文件及整库逻辑摘要完全不变；未创建任务根或误传入的runtime目录。原作品恢复到原名后完整清单与归档原始清单一致。真实Provider调用、真实作品写入均为0。

Windows工具的Ctrl-C终止了原生进程，未执行Python清理；确认服务终态后，对两个明确验证位于workspace `.tmp` 内的合成目录执行了原名恢复，再做上述独立核对。不能仅凭 `finally` 代码声称清理已经发生。浏览器正向/隔离检查发生在最终回滚日志分支修复之前；该修复没有改变已检查的应用读路径，三个真实退出检查在最终代码上通过。

## 工作审视与下一步

本次两个实质纠错：夹具误用返回类型导致源引用无效；将SQL状态已回滚等同于回滚日志必已删除。前者修夹具并保持拒绝规则，后者按SQLite实际中断状态建立受限续接规则，补验主文件已写出页面的真实强退。所有测试和服务已终态。

下一步是本机当前所有权的持久交接，不能用归档generation加一替代现机CAS。当前 `BookOwnership.require` 只覆盖reviewed正式提交，legacy及其他正常写入口的覆盖仍需补齐；启用前停用原注册写资格，新实例继续只读直到执行计划失效/新运行目录与预算保留完成。随后验证重启零自动调用、最终新流程确定性下一章及恢复后再次备份的来源证明，收束T7.1认证。恢复向导仍是完整交付的必要部分。
