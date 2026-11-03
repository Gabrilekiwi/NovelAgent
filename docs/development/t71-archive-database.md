# T7.1 归档 SQLite 离线证据

日期：2026-09-12。组件定向验证完成；所有归档仍 diagnostic / recoverable=false。

新增 `backup_archive_view.py` 将归档内读取的完成凭据核验、ZIP 物理身份检查、成员大小/哈希、读取预算及结束后整包复核统一起来；已有归档历史组件改用该接口，验证语义未放宽。

新增 `backup_archive_database.py` 从已验证 ZIP 只复制 workbench.sqlite 到独占创建的临时文件，再使用 mode=ro / immutable=1、query_only 和 SQL authorizer 打开。拒绝依赖 WAL 的头、整库完整性/外键异常、逻辑摘要不符、未知 workbench schema；打开过程中不实例化 WorkbenchDatabase、不迁移、不分类恢复、不调度。临时文件结束后核对原包声明的文件哈希，关闭连接后仅清理创建的原物理文件。

数据库目标必须同时匹配调用参数、归档 scope、真实 projects 行和归档项目身份。目标直接表及 job_events / 审核编辑 / 批次评估 / 提交索引按当前关系查询，保留每行摘要；作者草稿重新核对正文 SHA 与修订。审核引用和批次任务另外检查作品归属，避免单纯 foreign_key_check 放过跨作品链接。

输出 `archived_database_evidence_v1`，列出其他项目/任务默认禁用集合和目标任务重新确认集合；明确 worker / auto-import / Provider / 激活全部关闭，`enforced_in_restored_app=false`。这些是恢复政策的输入，尚未在恢复工作台强制执行。行摘要证明资料保留，并不证明所有业务 JSON、文件引用、预算或 reviewed 状态合法；这些检查和完整恢复绑定计划继续实施。

首轮26项为21通过/5失败（session44893 exit1，121.42秒）；由于多数数据库反例可能仅触发临时目录权限错误，不能把这些表面通过计入功能证据。第二轮改到工作区仍为14失败/2通过/16清理错误（session77486 exit1，22.35秒），说明仅改路径未解决根因。`tests/conftest.py` 已记录 Python 3.12 mode=0700 私有 Windows DACL 会排斥 managed child；组件改用独占临时文件，测试使用显式工作区 temporary_root，不修改全局 tempdir，不改变共享锁所在位置。源目录夹具也使用不同名称，反例断言具体校验原因，防止权限错误假阳性。

第三轮31项（新21项+已有离线历史10项）29通过/2夹具失败（session13722 exit1，142.93秒），`.tmp/stage-d-t71-archive-database-final.log` / XML同名。两项失败使用 reviewed 专用 Registry 创建 legacy 其他作品，已改用普通 ProjectRegistry；关系补测5项全部通过（session81486 exit0，56.21秒），`.tmp/stage-d-t71-archive-database-relations-fixed.log` / XML同名。并集31个不同用例已通过，新增21项，T7.1累计293项。不是声称首轮或第三轮全绿。

新增关系验证涵盖真实 reviewed 草稿/审核跨作品关系，以及真实 schema 下批次/评估位置与 session job 归属。前两次权限失败可能留下无法由当前令牌访问的空临时目录，未复制出SQLite；位置保留于原日志，未修改其ACL或宣称清理完成。新实现每项测试均确认独占临时文件清理，原源目录与包未改变；数据库和历史测试均终态。没有UI/API改动，本组件不宣称恢复应用已实施其他作品访问隔离。
