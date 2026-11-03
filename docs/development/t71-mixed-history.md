# T7.1 新旧事务统一历史检查

日期：2026-09-11。此次相关回归和实际页面验证通过，尚未完成可恢复认证。新增19项不同用例，T7.1累计245项；完整A–F目标保持active。API/UI初次验收见 `t71-backup-api-ui.md`。

发现的缺口：原 scope 按当前 identity 选择历史检查。升级到 3.0 后检查 reviewed receipts，却跳过保留的 v1 事务；直接把旧 v1 after-image 与当前 3.0 身份比较又会误报，因为身份由已经完成的 v2 升级合法替换。

`verify_persistence_history` 将两类事务纳入同一个有界逆向目标图，从当前文件摘要完整回退各事务的全部目标，不按文件名或时间猜顺序。新 v2 读取器核对清单字节、schema/immutable digest、候选、staged 与必要 before-image、逻辑根 UUID、marker、receipt、回滚凭据与终态登记。缺失、错归属、非终态、未完成 staging/pending/recovery 记录、未知外部根都会拒绝通过。只读读取不会 apply 旧 PathGuard、重建登记或恢复事务，也不授予写资格。

原 `verify_legacy_history` 保留独立旧格式行为；scope 使用联合检查，同时保留现有 reviewed 类型化状态、章正文、升级与准备证据验证。联合目标图不能替代这些业务校验。

已确认：首轮 scope/history/source 36 项通过，74.02 秒，session11376 exit0。新增组合首轮因 fixture 未创建 runtime 父目录出现14个setup错误，已修正。修正后14项全部通过，134.05秒，session82048 exit0，覆盖真实旧事务→升级→新章及12种旧/新证据损坏，检查前后文件清单不变。

最终组合增加缺失/错回执终态登记、pending登记、staging，以及真实v2回滚与损坏failure receipt。`.tmp/stage-d-t71-mixed-history-final.xml` / `.log`：95项全部通过，286.54秒，session84940 exit0；包括新增19项与scope/history/source/archive/API。不是全项目测试结果。新旧事务正向测试检查来源清单不变；旧v1单独检查会因当前身份已升级而拒绝，联合图可以证明其经由真实升级合法衔接。

## Browser verification result

Status: passed（此次新旧历史预检和诊断保全范围）。

Browser environment: Chrome152 / DevTools MCP，page5，t71-mixed-history隔离上下文，`http://127.0.0.1:38884/`。`.tmp/t71_mixed_browser.py` 在全新 `.tmp/iu3/c270ded173d0/b` 生成旧v1事务，完成真实格式升级及第2章正式提交。仅1次本地确定性生成器调用，真实Provider为0；worker、自动导入关闭，后续生成factory禁用、非本机socket连接拒绝。

Actions performed / Observed UI result:

1. 原生进入维护、展开、预览：69文件/42目录、总量604.9KiB，范围检查通过。
2. 仅损坏这个合成书的升级前旧commit.marker，原生重新预览：明确显示1项未解决问题，响应 scope_checks_passed=false、identity_or_history_unverified；保持 diagnostic/recoverable=false。
3. 点击保存诊断归档及校验：均200。读取真实ZIP确认保留了损坏marker原字节和scope blocker，完成凭据仍 recoverable=false；未以归档完整性通过代替历史合法性。
4. 恢复合成书marker原字节并核对SHA-256回到6b2099e2a89dc5225f5a040373732a343feacbd272deb17e60bdaf0f3a0b85fe；再次点击预览，blocker清空、范围重新通过。诊断包继续保留原故障现场。
5. 原生结束维护200，归档写按钮禁用。页面初次和最终console均为空，无异常请求循环或隐藏5xx。

Network check: 实际读取请求/响应体，preview req19/21/28均200，分别 scope通过/失败/通过；save req23→200、verify req26→200、finish req30→200。所有恢复属性仍为false。此次预检失败作为诊断范围200返回，与损坏ZIP校验409语义不同；UI正确显示其问题。

Evidence: `.tmp/t71-mixed-browser-evidence.json` 保存脱敏的响应、DOM、network和console；`.tmp/t71-mixed-artifact-verify.log` 保存ZIP故障字节与来源还原验证。归档 backup_613980c7480c4bcb9ef42266d729b1c6 保留在合成目录。服务session96053/PID15052已Ctrl-C退出（exit1中断退出），进程与端口另行核对；所有测试均终态。

仍未认证：任意自定义布局和外部必要根、所有内容历史、加载代码版本、恢复新绑定及同机资格交接。本检查证明保留的 v1/v2 文件事务目标链可衔接，不证明整个作品已经可恢复，也不替代阶段F最终新流程续写验收。真实Provider调用和真实作品写入均为0。
