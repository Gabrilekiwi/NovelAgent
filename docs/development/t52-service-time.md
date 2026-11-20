# T5.2 服务层主动计时

日期：2026-09-11。T5.2整体尚未完成。

生成、事实提取及显式恢复在服务确认了登记作品/章节/操作标识后建立唯一顶层计时，覆盖章意图验证、可信上下文准备、provenance、执行器、本地草稿/提案处理与服务finalize。作品身份查找、所有权和正式事务恢复属于进入该服务前的边界检查，不借此启动模型。

`ChapterBudgetOperation` 用 ContextVar 跨层传递，finally 关闭并重置。executor 复用同一 interval，核对物理预算目录、book/chapter/冻结摘要、精确 execution_id；嵌套操作拒绝。runtime检查实际章预算、execution及interval对象完全对应，拒绝在服务内部另建未受预算约束的runtime。已完成请求重放选择仅本地计时，保持模型调用数；未知/耗尽时间下显式resume仍只采用原成功响应，计时覆盖服务收尾，原未知区间不清零。

首轮4项专项通过：候选确认2秒+provenance4秒+模型3秒+服务保存5秒，总14秒且只1区间；准备超时零模型请求且保留本地耗时；预算耗尽后重复请求只本地重放；事实准备6秒+收尾7秒计入同一区间，作者等待100000秒独立。后补跨execution借用拒绝、嵌套/异常关闭、仅本地及普通服务均拒绝未绑定runtime，当前共8项新增。

默认链检查 `.tmp/stage-d-t52-service-scope-smoke.xml` 9通过；专项首轮 `.tmp/stage-d-t52-service-time.xml` 4通过；服务+本地恢复+原工作台入口 `.tmp/stage-d-t52-service-regression.xml` 32通过（当时新增7项），session70245 exit0。最后runtime绑定加固回归 `.tmp/stage-d-t52-service-binding-final.xml` 已终态：70项通过、5子测试通过，session63235 exit0。本轮新增不同用例8项，T5.2累计260项。

## 浏览器验证

Chrome page11，isolatedContext `t52-service-scope`，`.tmp/t52-browser-server.py --local-recovery --service-scope`。全新合成作品 `.tmp/iu3/f30a0fcd2447/b`，project `project_6062f4475db34b1b932268f28040683e`，本地8882。默认服务/factory、替身SDK、零真实调用。

原生表单冻结3次/输入100000/输出50000/主动900秒，确认意图和生成。成功Receipt后真实强退服务session39818/PID29884，原time.json仅一个顶层未闭合区间。以原数据库启动session70996/PID24488，刷新出现未知时间和恢复按钮。原生确认resume，req38 POST200、req41 budget GET200，建立1108字符可编辑草稿；总调用仍1，输出由预留128结算24，输入占用14572。原time.json逐字节不变（备份`.tmp/t52-service-original-time.json`），本地恢复只有一个区间2.187秒，覆盖执行器及服务收尾。刷新后同结果、事实提取继续禁用，Console为空。网络响应字段与界面已对照；没有第二次模型调用。两次浏览器服务均已停止并确认终态，metrics在`.tmp/t52-service-browser-metrics.json`。

最后runtime加固增加了绑定前置检查；普通路径由最后专项验证，本地成功响应恢复完全绕过新attempt，浏览器记录仍适用于该恢复路径。后续尚有价格/可信计费输入上界、金额配置、T5.1费用投影及后续检查/修复共享预算；不能由本计时检查点推断金额止损已完成。
