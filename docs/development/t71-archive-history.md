# T7.1 归档内新旧事务历史核验

日期：2026-09-12。实现位于 `workbench/backup_archive_history.py`，专项45项通过；本文不是可恢复认证。

`verify_archived_persistence_history(package, project_id=..., book_id=...)` 先调用现有 `verify_archive` 验证完成凭据、ZIP 和绑定清单，随后通过显式 `read_bytes` 接口复用新旧事务发现与统一逆向目标图。历史中的绝对路径只用于原来源清单内的逻辑定位；读取只指向已验证 ZIP 的作品成员。不解压、不访问原作品、不打开工作台数据库或 runtime 对象、不执行 recovery、不调用 Provider，也不改动原 manifest / marker / receipt / PathGuard。

读取绑定来源根和成员大小/哈希，单文件32 MiB、累计256 MiB、20000次读取上限。ZIP 打开描述符和文件物理身份前后核对，完成时重新验证整个包与原结果相同，发现变化拒绝返回成功。结果为 `archived_persistence_history_v1`，绑定包完成凭据、作品、清单和历史摘要；固定 `recoverable=false`、`restore_plan_ready=false`、`write_activation_allowed=false`。

这一步重验的是保留的 v1/v2 文件事务链。它没有离线重验全部 reviewed 业务状态、全库关系、预算和辅助状态，也没有生成恢复根映射或写入资格。旧 diagnostic 包不会升级用途；一个包通过完整性校验，也仍可能因其内部历史本来损坏而被本组件拒绝。完整下一包边界见 `t71-recovery-certification-contract.md`。

验证完成：`tests/test_backup_archive_history.py` 10项，包括真实重叠 v1、升级及新章的混合历史，源目录临时不可用、禁用默认 live reader、重复核验确定性、包/源字节保持不变、缺 marker/staged/manifest/orphan registry、旧 marker/新回执损坏、显式目标不匹配。与历史发现/v1组合45项全部通过（session9440 exit0），`.tmp/stage-d-t71-archive-history-first.log` / XML同名。新增10项不同用例，T7.1累计272项。新测试与临时源目录移动均已结束，源目录原位及原清单恢复，没有运行中的任务测试或服务。此组件未新增 HTTP 或 UI，当前浏览器历史发现证据见 `t71-history-discovery.md`。
