# T5.2 成功回执的本地恢复

日期：2026-09-11。结果：成功回执本地完成及独立恢复计时已通过专项、回归和实际浏览器验证。T5.2 尚未完成。

## 原问题与处理

独立审查发现，原 `ChapterExecutionBudget.activate` 虽先对账回执，但随后未知/耗尽主动时间会在成功响应解析前阻断。完整终态记录可直接返回，成功回执尚未建立终态的窗口却无法恢复。旧测试用 BaseException 模拟中断，会运行 finally 闭合计时，没有覆盖真实死亡。

现在仅 executor 的显式 `resume` 允许申请本地恢复。时间未知或耗尽时，核验原冻结预算、execution 绑定、所有原调用、回执及响应哈希；要求唯一成功响应，其余仅允许确定未发送。缺失、损坏、partial、uncertain 不进入新请求。Candidate 直接采用原响应并完成原来源/语言核验及草稿落盘，完全跳过 generator；Fact 复用原成功响应解析。运行时设置 `dispatch_forbidden`，`execute_attempt` 首行拒绝任何调用，即使句柄逃逸也不能补发请求。

保留原 `time.json`，不补零、不用恢复时间闭合旧未知区间。原消费按真实回执对账。恢复操作另以 `local-recovery.json` 初始化凭证及 `local-recovery/time.json` 记录实际单调时钟耗时，独立 recovery 锁避免把旧未知区间误报 running。报告加入恢复已测秒数，并纳入主动总已测量；未知总时仍不完整，后续 Provider 继续暂停。恢复目录/计时记录缺失不重新初始化；恢复中再死亡保留未闭合区间并阻止再次执行。

## 自动证据

新增 `tests/test_chapter_budget_local_recovery.py` 9 个不同用例。真实子进程 os._exit(42) 验证成功 Receipt 后的 Candidate/Fact 恢复，未预留 Intent、发送标记后、响应文件后但无 Receipt 的窗口拒绝；响应损坏拒绝；耗尽时间本地采用；逃逸 runtime 不得调用；独立恢复计时计入总时且保持原 unknown；本地计时丢失不得清零。

初轮 `.tmp/stage-d-t52-local-recovery.xml` 14通过1失败，失败为测试使用不存在故障名，已修正为 `after_response_artifact_before_receipt`。修正7项通过 `.tmp/stage-d-t52-local-recovery-corrected.xml`。随后加独立计时两项，相关42通过 `.tmp/stage-d-t52-local-time.xml`。最后增加恢复初始化凭证后，`.tmp/stage-d-t52-local-recovery-final.xml` **85通过、5子测试通过**，session30927 exit0，含9新增、14原计时、37预算runtime、25原runtime；其他报告有重叠，不能相加。

T5.2 新增不同用例累计252项（原243 + 本组9）。没有全量重跑或真实 Provider 调用。

## 实际浏览器

Chrome DevTools page10，隔离上下文 `t52-local-recovery`，`.tmp/t52-browser-server.py --local-recovery`。合成 reviewed 作品 `.tmp/iu3/2fd5995e8401/b`，project `project_dfb8b3cdbb184ec8a47802880a4f13d3`，端口8882。实际默认工作台服务链，替身SDK，测试第二次进入generator会主动失败。

原生填写并确认预算3次/输入100000/输出50000/主动900秒，确认章意图，点击生成。成功回执落盘后真实os._exit(42)，服务session86743/PID30200终态。验证原计时未闭合且只有一个成功Receipt。原时间字节保存于 `.tmp/t52-local-original-time.json`。

以原数据库重启session8359/PID14212。刷新显示等待作者恢复、调用预留1/3、未知主动区间。原生点击“确认继续生成”并确认：POST resume req38返回200，原成功响应建立1108字符可编辑草稿。GET budget req41返回200，调用仍1、输出由预留128结算24、输入保留14572，原主动区间unknown；新增恢复计时0.843秒。原 `time.json` 与保存字节完全相同，`.tmp/t52-local-browser-metrics.json` 仅一次SDK调用。再次刷新仍同草稿/消费/计时，事实提取保持禁用。

Console：强退时预期连接reset/refused；重启和恢复后无Console消息。网络：恢复POST与预算GET均200，刷新均为GET，没有第二次模型请求。已读取预算响应字段与界面对应关系。最后服务14212已停止并确认session终态，没有活动测试/任务服务。

## 尚未覆盖

外层确认验证、上下文准备、provenance捕获及服务finalize仍未全部纳入同一主动区间。当前独立恢复覆盖 executor 内本地完成部分，不能宣称完整工作台全程耗时。下一步以服务generate/start的可信project/chapter建立顶层区间、executor复用并校验其绑定，防嵌套、重计及跨作用域借用；完整幂等结果和仅本地恢复不能被新请求时间门槛误伤。

未知时间没有可靠证据时仍保持未知；本地采用成功结果不构成解除后续模型暂停。金额价格/输入上界、金额配置界面、后续检查修复共用预算及T5.1费用关联仍待完成。
