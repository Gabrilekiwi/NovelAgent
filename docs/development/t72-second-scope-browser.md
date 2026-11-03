# Browser verification result

Status: passed（限定第二代数据库范围的只读行为；完整恢复产品未验收）。日期：2026-10-01。

Changed behavior tested:
- 已启用恢复副本再次归档后，第二个实际副本绑定第二代范围；保留正式正文和作者草稿，仅允许目标作品读取，拒绝写入。

Browser environment:
- URL：`http://127.0.0.1:38894/`，Chrome DevTools MCP page 2，隔离上下文 `second-scope-20261001`。
- 合成样本 `.tmp/rs-7236d911`，机器证据 `.tmp/rm-5eb2b3-machine`；SDK/全部生成与提取执行器禁止调用，socket 只允许本机。

Actions performed:
1. 原生点击书架目标作品、已提交第三章、开始下一章、第四章正文页。
2. 辅助浏览器 fetch 核对 scope/作者草稿 JSON；正常会话内 PUT 草稿测试拒写。未读取或保存认证 headers。
3. 导航到库中另一作品，观察中文隔离错误和 GET 409。
4. 正常停止第一个服务后，以同一副本启动新进程；整页忽略缓存刷新，第三章正式正文及草稿接口再次读回，再正常停止。

Observed UI result:
- 页面持续显示“恢复副本尚未启用，当前只供核对资料。”；第三章正文及 r1/SHA 展示一致。
- 其他作品显示“此恢复副本只包含目标作品，其他登记作品不可访问。”，没有展示其数据。
- 第四章尚无章纲，页面明确显示无法构建可信输入，生成按钮禁用。独立作者草稿在 API 保留，但章节正文页没有展示该草稿；这项 UI 缺口未交付。

Console/runtime check:
- 正常第三章读取与新进程刷新无 error/warn、无运行时异常；原有 form field 缺 id/name 的 issue 1 条单列保留。
- 错误路径仅出现对应两次章节卡 400、一次只读 PUT 409、一次跨作品 GET 409 的资源错误；未发现其他错误。

Network check:
- 维护/bootstrap/作品列表、目标概览/健康/章节、第三章及预算 GET 均 200；新进程刷新相关请求均 200。
- scope GET 200，`state=restoring/read_only=true`；第四章草稿 GET 200，r1/text/SHA 保持；PUT 409，`restore_scope_read_only`。
- 第四章 card GET 400，`chapter_card_context_not_ready`，样本没有章纲；其他作品 GET 409，页面解释与后端一致。

Additional checks:
- 新增 10 个不同测试用例通过（首轮 9 通过和补正请求后单项通过的并集），相关回归 24 通过；详见 `t72-second-scope-generation.md`。
- session 90152/PID 36344 和 session 29918/PID 44388 实测正常 exit 0。两次停止证明书籍文件及 SQL 逻辑快照均不变、worker 未启动、调用列表为空。最终无 Python 进程、38894 无监听。

Evidence:
- `.tmp/t72-second-scope-browser-evidence.json` 保存不含认证 headers 的网络/控制台结果与检查结论。
- `.tmp/t72-second-scope-browser-first-after.json`、`.tmp/t72-second-scope-browser-after.json`、两个 server log；恢复测试日志/JUnit 和生产哈希见检查点。

Remaining risks:
- 二次来源资格交接、重复任务隔离、再次可写启用与显式回退资格链尚未完成。
- 完整恢复入口、独立作者草稿展示与后续续写仍需产品验收。归档仍为 diagnostic，recoverable=false；未调用真实 Provider，未修改真实作品。
