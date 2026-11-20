# 已启用恢复副本的诊断归档浏览器验证

日期：2026-10-01。Chrome、本机 127.0.0.1:38897、独立合成作品。第二代 scope active；旧任务 completed/r4、执行契约 restored_job_history_v1、execution_compatible=false、原事件 2 条。所有执行 factory、OpenAI/Anthropic SDK 和非本机 socket 连接均禁止。未使用真实 Provider、真实书或机密请求头。

## 原生操作与结果

修改前首次观察：原生书架→作品概览→正式第二章可读，r1/SHA `6368997d68d3a2e19d4c3e8386a943bee3a37ed57967dd011ffe3af8135af8e4`。维护和备份被前端统一隐藏；辅助 HTTP 测试确认 active 的 maintenance/begin 409。因此修改前不能验收用户归档流程。

修改后，57725/PID5384：原生点击进入维护、展开诊断归档、预览、保存、校验完整性、结束维护。作品下拉只含目标作品；预览明确列出 book/87 文件及历史 jobs/job_73e4de7dc6db4bf18a7f9750d9ac4b44/2 文件，预检通过，未把 fresh next-jobs 作为旧产物来源。保存 `backup_9a2ceca90be84a6597a0cef8fdbdbd66`，显示诊断归档及恢复尚未验收。维护 begin、目标 preview、capture、目标 verify、finish 均 200；相关 GET 均 200。

51864/PID48308 同数据库新进程：原生展开归档发现此前保存的目录，重新进入维护→校验完整性→结束维护均成功。最后作品刷新 GET 曾观察为 pending，导航空页取消后服务正常 exit0；没有把该请求写成已成功。两进程关闭前完成原维护，均 observed exit0；书、原任务产物与全部 SQL 逻辑状态不变，worker 无线程，调用列表为空。关闭使用测试服务 15 秒上限，不代表生产 Windows 并发关闭缺陷已修复。

Console 两次均无 error/warn，只有改动前已有的表单缺少 id/name issue（count 1）。网络读取仅列表和安全响应字段；无 Cookie、Authorization 或启动 token 输出。一次等待使用了错误的目标提示文字而超时，随后按实际提示观察成功，没有重发校验动作。

## 实际保存内容核验

浏览器保存的真实归档再做完整离线汇总：原书、原外部任务目录、原机器 store、第一恢复容器、第二恢复容器、当前机器 store 共六个目录全部物理不存在，scope loader 和机器 record 查询被禁止，jobs/references/offline_data 均 verified、blockers 为空。六个目录均原样还原。

本项验证已启用副本的目标诊断归档能力。归档仍 recoverable=false；完整恢复向导/认证、项目内任务目录的多次恢复、新任务原生历史页、下一章及其余 A–F 不据此验收。

证据：`.tmp/t72-job-artifact-browser-evidence.json`、`browser-restart-evidence.json`、`browser-offline.json`；三个服务退出日志及 before/after 文件保留。起初测试脚本读取不存在的 scope.book_id，未启动服务；失败日志也保留。
