# T7.1 备份服务启动版本约束

日期：2026-10-19。此项补足有限的磁盘版本约束，不是内存代码证明，不签发可恢复认证。完整 A–F 目标继续执行。

## 问题与实现

原归档流程在预览时读取磁盘软件指纹，并在保存期间复核。服务启动后若代码或依赖锁被替换，重新预览仍可接受新的磁盘版本，不能解释运行进程是否仍加载旧模块。60787 的真实来源预览测试已复现此缺口：修改隔离软件树后未拒绝，原运行 exit 1，日志保留。

`SoftwareSourceSession` 在备份服务构造时固定内部软件根、代码/依赖锁指纹和本次 OS 实例 ID。预览前、生成预览时、保存前及发布完成凭据前检查来源未偏离；中途变化拒绝并要求重启后重新预览。原保存期间的版本复核保留。该约束不接受请求中的软件根或检查结果。

新预览记录 `source_session`，协议 `backup_source_session_v1`，明确 scope 为 `disk_source_stable_since_backup_service_construction`。它绑定本次实例和指纹，只记录哈希，不导出源文件或环境内容。服务构造发生在模块导入之后，不能据此证明所有已加载模块的字节；`loaded_modules_match_proven` 保持 false。重新构造服务也不等于证明整个进程已重载，受支持启动仍需实际重启。

归档只读核验对新增证据执行精确字段/协议/摘要/指纹及 false 内存声明检查。旧 diagnostic 包可以没有此字段，继续按旧协议核验；它们没有因此获得认证。重新计算 ZIP、manifest、preview、completion 和证据全部摘要，仍不能放行不匹配的启动指纹或擅自声称的内存证明。

pending 中原来的 `loaded_code_version_proof` 改为 `certified_software_compatibility`：后续认证须核对受支持格式、写入契约、验证器和版本，不能无限要求任意 Python 内存一致性，也不能将此项单独当作可恢复认证。

## 验证证据

生产指纹：`30f140828848a71aa9dc41568885ac46c335658568c4b2fcb811e75eecba45ec`。全部运行终态后已重算一致。

| 批次 | 终态 | XML 时长 | 范围 |
|---|---|---|---|
| 新增 `final` | exit 0，10 项 | 6.138 秒 | 代码/依赖锁/新增/丢失软件文件拒绝；释放来源后两处变更不发布 completion；自洽重哈希的错误证据拒绝；旧包兼容；实际 HTTP 重新预览拒绝和应用重启后恢复保存 |
| 29269 `regression` | exit 0，47 项 | 149.184 秒 | 原打包、预览、强退、HTTP、完整离线组合与原目录不可用读取 |
| 34187 `restore-regression` | exit 0，1 项 | 92.537 秒 | 公共旧书登记、实际升级、升级前归档恢复/启用、双启动草稿读回 |
| 34403 `restart-regression` | exit 0，2 项 | 83.529 秒 | 原登记 admission 真实强退后在新应用继续；v5 pending rollback 的原 HTTP session 重启后明确续接，真实监听线程不执行恢复/任务 |

共 10 项不同新增、50 项相关回归通过。软件变更只发生在测试专用软件树，未修改真实安装或用户配置。原失败 60787 不改称成功；其源码指纹为前项的 `4c9351be3d0073e43a8e25180dd164ed10edbdf8a9b776a25fb5098a639dd2a8`。

完整启动版本测试与机器进度为 `.tmp/t71-source-version-progress.json`，原日志/XML 前缀 `.tmp/t71-source-version-`。归档仍为 `diagnostic` / `recoverable=false`；真实 Provider 调用、真实作品写入均为 0。

## Browser verification result

Status: blocked

Changed behavior tested: 本地 HTTP 预览/保存遇到服务启动后的软件变更时应显示重启原因；实际重启后可继续原维护并重新预览保存。

Browser environment: 本机工作台。Chrome DevTools `list_pages` 返回已有配置正在运行，无法建立会话；内置浏览器连接返回 Work 设置禁用本地浏览器。没有关闭用户浏览器、修改配置或另起绕过连接。

Actions performed: 两个浏览器连接均已实际尝试。未启动额外网页测试服务，未宣称发生原生网页点击。

Observed UI result: 未取得。Console/runtime check: 未取得。Network check: 未取得原生浏览器证据。

Additional checks: 实际 TestClient HTTP 请求验证预览及保存均为 409，包含重启原因；新 create_app/lease、原维护显式接管后预览与保存为 200，归档证据绑定新实例。这不替代真实浏览器。

Evidence: 本轮工具连接错误及 `.tmp/t71-source-version-final.{log,xml}`。机器检查点保留连接限制。

Remaining risks: 需在浏览器可用后，用隔离软件树运行应用，进入维护并预览；替换该合成树后再次预览/保存，核对可见中文错误、409 请求与无新运行异常；实际重启后明确继续维护、重新预览/保存并核对 200。不得为了测试改动真实生产代码或依赖锁。
