# 恢复副本启动入口

日期：2026-10-01。状态：启动边界验收通过；阶段 D、完整恢复向导和认证仍未完成。

普通启动器此前固定使用 `workbench.db`，完整恢复容器却保存 `workbench.sqlite`。现在新增 `--restore-dir`，并让 `--data-dir` 发现恢复文件时走相同核验入口，避免错误打开空工作台。显式恢复路径必须存在，数据库和独立范围凭据必须完整；同时存在普通数据库则拒绝启动。不会自动创建丢失的恢复目录或替代数据库。

范围核验沿用 `load_restore_scope` 的物理绑定、目标作品和启用资格验证；取得实例所有权后，应用工厂再次核验。恢复启动关闭 worker 和自动导入，已启用副本也不会重启自动调用。重复启动沿用现有实时实例身份验证和单实例锁。

首次专项/回归为 12 通过、4 失败，日志 `.tmp/t72-restore-launcher-tests.log` 和 XML 保留：两项断言误用未规范化草稿字符串；两项发现只读 SQLite 打开仍可能创建或修改 WAL 的共享内存文件。前者改为核对实际保存草稿；后者在 SQLite 打开前检查数据库和独立范围凭据，保留原完整目录不变断言。最终 `.tmp/t72-restore-launcher-final.log` / XML：实际 exit0，16 项通过，8.53 秒，含 11 新增专项及 5 原单实例回归。

## Browser verification result

Status: passed

Changed behavior tested:
- 实际 `run_workbench.main()` 打开原已验收的第二代恢复副本，未创建空数据库。
- 第二进程用 `--data-dir` 打开同一副本，复用相同活实例地址。

Browser environment:
- URL：`http://127.0.0.1:38898/`；Chrome DevTools MCP，page 1。
- 独立 `.tmp` 样本；私有机器账本仅由测试驱动注入。驱动禁止 SDK、全部四类章节执行器及远程连接。
- Uvicorn 使用真实启动器和监听 socket；测试驱动限制 graceful shutdown 为 15 秒，此设置没有修改生产关闭行为。

Actions performed:
1. 原生点击书架作品卡，再点击正式第三章。
2. 新进程重复启动，返回原实例 URL 且 exit0。
3. 浏览器强制刷新，读回第三章和当前恢复范围。

Observed UI result:
- 仅展示目标作品；第三章为已提交 r1，提示可以开始下一章。
- 正文实际 SHA256 与原记录均为 `cce21d4691b0c7034ba5a69b61283c9701be5f0cca0ad139c86b75fdf69aafa2`。

Console/runtime check:
- 无 error/warn。

Network check:
- 维护、bootstrap、作品列表、目标作品、第三章、单章预算和恢复范围 GET 全部 200；最终无待完成请求。
- JSON 章节哈希与浏览器计算正文哈希一致；scope 为 active、目标 project_id 一致。

Additional checks:
- 主服务 session13215 / PID42176 观测正常 exit0；重复启动 PID45548 exit0。
- 完整书稿/历史任务文件清单、数据库逻辑摘要保持不变；没有 `workbench.db`；worker 线程与模型调用均为零。
- 负向自动检查覆盖空目录、缺数据库、未核验数据库、两库冲突和范围凭据丢失，全部拒绝且目录清单不变；中文/空格目录与缺目录不创建已验。

Evidence:
- `.tmp/t72-restore-launcher-browser-evidence.json`、`browser-info.json`、`browser-after.json`、`browser-duplicate.json`、浏览器/重复启动日志及 `.tmp/t72-restore-launcher-progress.json`。
- 浏览器 MCP 的文件保存路径不被其工具根接受，改由普通工作区文件工具保存返回结果；没有省略浏览器检查。

Remaining risks:
- 启动边界的只读测试使用合成 scope，不冒充完整归档恢复认证；原生浏览器使用实际已启用第二代恢复样本。
- 完整持久化恢复会话、只读资料核对、通用作者草稿 UI、显式交接/继续/启用和公开 API/UI 向导仍待实现。
- 归档用途仍是 diagnostic，recoverable=false；正常旧书升级、金额控制、E–F 与真实 Provider 效果验收仍未完成。
