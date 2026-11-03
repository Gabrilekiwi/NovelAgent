# 项目内历史任务与第二代正式续写：浏览器验证

日期：2026-10-01。工具：Chrome DevTools MCP；地址 `http://127.0.0.1:38898/`。使用独立合成第二代恢复副本，四个执行器、OpenAI/Anthropic SDK 及非本地 socket 全部禁止；恢复 worker 不启动。

## 实际操作与结果

原生进入作品、点击第三章：显示正式稿、r1、本章已正式保存和下一章入口。页面 SHA 为 `cce21d4691b0c7034ba5a69b61283c9701be5f0cca0ad139c86b75fdf69aafa2`；新进程章节 API 的 `current_prose` 实算 SHA 与 `current_sha256` 相同。

原生展开费用：显示三次物理尝试和 `0.000219 USD` 的替身估算/预算占用，保留恢复前两次占用，仅增加一次确定性生成。这不是实际 Provider 账单或真实金额控制验收。

原生进入维护、展开诊断归档。在测试目录内损坏旧 `stdout.json` 后，原生预检显示三项问题；辅助响应检查确认为 `historical_job_artifacts_unverified` 及任务结果/事件结果的两项摘要不符。还原后原生重新预检通过。所有相关 HTTP 均为 200，问题通过预检结果表达；仍明确标为诊断归档。

原生保存 `backup_7d95f1be085e4233a3e8ccc1aab64373`、点击完整性校验、结束维护均通过。新进程再次发现该归档、原生校验与结束维护通过，第三章和预算重新读回。

## 发现的问题与修复核验

首个 27698/PID23524 退出为 1：故障测试只还原字节，改变了 stdout 的 `modified_ns`，完整清单断言正确拦截。根据预浏览器实际归档核对，只有此文件的修改时间不同，原大小、SHA、device/inode 相同；恢复时间后完整 120 文件清单与原归档一致。保留原失败日志及 `browser-initial-after.json`，测试恢复脚本现同时保留字节和时间信息。

第二个 92333/PID34176 正常 exit0，书/任务树/SQL 不变、调用零。首次并发预算读取出现范围 409；四路复测为 `[409,200,409,409]`，实际线程探针定位为机器 OS 状态锁竞争被包装为 `RestoreScopeError`。没有用刷新后成功掩盖该故障。

生产 `_machine_gate` 改为与 `BookOwnership.locked()` 共用每作品 RLock，仍保留跨进程 OS 锁和原资格核验。第三个 6205/PID50372 正常 exit0；原生第三章、费用、旧归档发现/校验及结束维护通过。四路预算请求、原启动预算读取均 200/ready；控制台无 error/warn，仅既有表单缺 id/name issue。最终网络所有相关请求已完成且均 200。书、任务完整清单和 SQL 均不变，worker 未启动，SDK/执行器调用零。

## 实际网页归档的离线验证

对网页保存的同一归档，移走四个完整来源目录：原作品/原 SQL/原机器所在父目录、第一恢复容器、第二恢复容器、独立机器目录；同时禁止 live scope 和机器所有权记录读取。完整离线汇总、任务和引用核验全部通过，第三章 SHA 相同，历史任务定位仍为作品内的原目录。四目录已全部还原。

## 证据与边界

- `.tmp/t72-internal-job-browser-evidence.json`：原生第三章、费用、故障/正常预检、保存与校验、console/network。
- `.tmp/t72-internal-job-browser-restart-evidence.json`：新进程读回、实际 409 及并发复现。
- `.tmp/t72-internal-job-browser-fixed-evidence.json`：最终五文件生产版本的原生操作、四路并发成功及无 pending 的最终网络。
- `.tmp/t72-internal-job-browser-time-repair.json`：完整原清单恢复。
- `.tmp/t72-internal-job-browser-offline.json`：实际网页归档、四来源目录缺失、完整离线证据。
- 三个服务日志分别为 `browser-server.log`、`browser-restart.log`、`browser-fixed.log`，均有原 session 实际退出码记录。

验证状态：本轮原生备份、二次恢复后的正式章读回、并发预算读取及离线核验通过；最终13项自动专项和5项所有权回归终态通过。完整恢复 API/UI/认证尚未交付，归档 recoverable=false，真实 Provider 调用和真实作品写入均为 0。Windows 发布的其他并发关闭问题仍按 T7.3 独立处理。
