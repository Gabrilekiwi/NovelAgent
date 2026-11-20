# Browser verification result

Status: passed（限定回退后原恢复副本的活动范围、浏览器 API 保存和重启读取）。日期：2026-10-01。

Changed behavior tested:
- 已启用恢复来源经历第二次资格撤销与显式回退后，保留原启用凭据，用完整资格续接链授权新机器代际；工作台实际读写仍受单作品范围约束。

Browser environment:
- Chrome DevTools MCP page 3，隔离上下文 `second-handoff-20261001`，URL `http://127.0.0.1:38895/`。
- 来源 `.tmp/rm-753a8a`，机器 `.tmp/sg-c4560e77-machine`；合成数据、四种生成/提取执行器和 SDK 均禁止调用，socket 仅允许本机。

Actions performed:
1. 原生点击书架作品、概览和已提交第三章，观察活动范围提示和正式正文。
2. 浏览器正常会话内辅助 fetch 保存第四章独立作者草稿：PUT 200，revision 1→2，文本与 SHA 精确检查。
3. 导航另一登记作品，观察中文拒绝与 GET 409。
4. 新进程启动并忽略缓存刷新，核对原生正文页、scope/草稿/预算实际响应、控制台与全部页面请求终态，再离开页面并正常停服务。

Observed UI result:
- “恢复副本已启用，只访问目标作品。”与后端 `state=active/read_only=false` 一致，第三章正式正文/r1/SHA 保留。
- 其他作品显示“此恢复副本只包含目标作品，其他登记作品不可访问。”。
- 独立作者草稿的页面展示仍未实现；本次保存和读回是实际浏览器 API，不能称原生编辑器已验收。

Console/runtime check:
- 保存流程及最终新进程页面加载没有 error/warn；原有 form field 缺 id/name issue 1 条单列。
- 跨作品 GET 409 的资源错误为预期负向结果。
- 中途一次过早关闭产生预算请求 ERR_CONNECTION_REFUSED、Windows Proactor ConnectionResetError，服务未正常退出。确认其仅为自建浏览器验证进程 PID12308 后强制清理，session74680 实测 exit1，原日志与失败结果保留。
- 只修改测试服务器的 15 秒 graceful shutdown 上限，并等待页面请求全部完成、离开页面后再停服务。最终重验无运行错误；没有声称已修复生产 Windows 并发关闭行为。

Network check:
- 正常维护/bootstrap/列表/概览/健康/章节/第三章预算 GET 200；草稿 PUT 200，SHA `537358b30741d6be62578e68990a9b2cc9465a43ce99142b61fa0fa73fb39e91`。
- 最终新进程 scope/草稿/预算 GET 200，revision2/text/SHA相同；合成预算仍3次、0.000219估算、无未结算调用。没有真实费用或金额控制验收。
- 另一作品 GET 409；未读取或保存 Cookie/token/Authorization headers。

Additional checks:
- 最终19项专项（含7个真实强退）与21项旧路径回归均exit0；七个生产文件与启动哈希一致。
- 首个服务15031/PID13052和最终服务16236/PID49024均正常exit0；两份有效停止证明书籍文件不变、worker未启动、执行器0，最终SQL与第一次保存后的SQL相同。当前无Python进程或38895监听。

Evidence:
- `.tmp/t72-second-handoff-browser-evidence.json`、`*-browser-first-after.json`、`*-browser-after.json`、三份服务器日志。
- `.tmp/t72-second-handoff-case.json`记录原实际测试副本；测试日志/JUnit/生产哈希见对应检查点。

Remaining risks:
- 生产Windows加载并发请求时的关闭行为留待T7.3验证；中途关闭失败不删去。
- 第二副本旧任务代际隔离、重新启用及确定性下一章、完整恢复向导和独立作者草稿展示仍未交付。归档仍diagnostic/recoverable=false，全A–F目标active，真实Provider/真实书写入0。
