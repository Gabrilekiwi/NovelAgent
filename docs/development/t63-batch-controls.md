# T6.3：明确控制与当前步骤许可

2026-11-05，明确控制与调用范围内部组件已验证。没有真实 Provider 调用、真实作品写入或新增正式接受。完整 T6、公开控制页面、三章连续执行及单次修复仍未完成。

## 持久进度与本次许可

core/engine/reviewed_batch_control.py 实现绑定原 grant/批次预算/所有权的控制命令链。每条记录包含序号、原前序哈希、幂等键摘要、动作与完整数据；状态由整条链重放计算，不信任独立 status 或 next_chapter 标志。开始、继续、暂停、停止和领取/完成步骤均留下原记录。

机器与作品各保存同一份控制链。初始化还有独立机器初始锚点，原 grant ready 从 v1 显式升级 v2 并绑定该初始 SHA。读取 v1 授权仍可核对原资料；开始控制前须明确初始化，核对各章没有旧 Candidate/提取、费用预留、修复消耗或主动区间，不能把已发生的工作变成空批次。v2 锚点或任一控制副本丢失后拒绝重建。

命令先保存机器链，再保存作品链；中间进程退出时两份不同，执行检查拒绝。只有同一原命令的动作、数据、键与前序 CAS 能显式续办第二份保存；新命令不能跳过缺口。同一完成命令重放不再次改变进度。停止为终态。暂停/明确继续保留 pending step 的原 ID、execution ID 和输入 SHA，不新增一次生成或修复身份。

以上完整性约束不代替整套最新消费/机器控制的恢复证明，不声称能够识别所有机器文件一起回滚。仍保留原可写恢复资格门禁。

workbench/reviewed_batch_controls.py 在实际作者 grant 和机器作品所有权下处理命令。明确开始/继续会产生本服务实例内的 opaque handle；构造新服务、进程冷启动或单纯读取“running”磁盘记录都不会创建它。暂停、停止、实例变化、离开步骤范围和跨线程使用会使旧许可失效。没有启动后台 worker，也没有在读取或恢复时发 Provider。

## 实际物理调用的再次检查

进入步骤时保留原作品所有权，短暂领取阶段后释放 Provider/SQL 锁，让真实执行器自行取得它的调用边界。整个步骤接原章主动执行计时；local 阶段禁止 Provider。PolicyStepPermit 绑定原 book/chapter/batch/grant/execution/stage/输入和冻结模型/端点，必须在当前线程的 scope 内使用，退出即关闭。

ModelCallRuntimeContext 在每次 attempt 入口和物理 dispatch 之前核对当前自动许可；现有手工路径没有被误标为自动。当前允许模型阶段仅 generation/extraction 的明确物理阶段名称，其他阶段不接受模型调用。每次核对还读取原控制链和实际输入/配置，不靠一份已经失效的内存布尔值继续。

暂停/停止只降低权限，使用独立短控制锁和原注册/机器见证的只读核对，不等待正在执行的模型调用持有的整部书锁。已发出的调用仍正常记录原响应/Receipt/费用；暂停不会假造免费或取消凭据。后续物理调用被拒绝。若停止发生于预留/Intent 已保存、dispatch 之前，运行时生成本地未发送终态并按原预算协议释放占用。

完成阶段必须由内部服务器 verify_result 回调核对真实结果 SHA；该扩展口不是客户端结果上传接口。本组件尚未接好各阶段的 typed Candidate/extraction/quality/publication/readback 结果适配器，不能凭测试回调的常量 SHA 宣称完成三章、正式 readback 或完整自动流程。还需将控制 scope 接到真正的 policy 生成/提取入口；当前授权控制没有复用 author_confirmed=true 冒充自动调用。

## 验证证据与续接

首轮 `.tmp/t63-controls-first.log/.xml`：9 pass、1 setup error，exit 1。服务测试遗漏共享 case fixture 的显式导入，未执行该服务场景；已补导入，原失败保留。

第二轮原 32393 明确 exit 0，73 passed / 109.23 秒，`.tmp/t63-controls-second.log/.xml` 与逐项 events。覆盖控制链/损坏拒绝/真实进程退出、实际拥有者步骤和原调用重放、明确继续保留原 pending ID、停止于物理发送前产生 budget_rejected 且原费用占用释放，以及原 chapter runtime / batch 预算回归。没有把旧失败轮的重复 pass 再累计。

第三轮原 9719 明确 exit 0，28 passed / 557.78 秒，`.tmp/t63-controls-third.log/.xml`、`.tmp/t63-controls-third-events.jsonl`、`.tmp/t63-controls-third-source.json`：错误模型/端点/阶段/execution/线程/输入/配置拒绝；另一线程在实际合成调用中暂停，不丢本次实际费用且下一调用为零；真正冷进程只读 running 记录而没有 live handle；ready 锚点/机器/作品控制丢失不重建；实际归档恢复旧控制路径隔离及原发布 API。

随后补真实服务冷重试测试，发现旧 start 落盘后进程退出，新服务重试却使用新 epoch，不能续办原命令。`.tmp/t63-controls-cold-failure.log/.xml` 保留原 62772 exit 1、1 fail / 38.89 秒。现从原机器命令读取相同动作的原 payload，只补原副本；原 epoch 不等于当前实例时返回无 handle、explicit_resume_required，不把恢复落盘当作新许可。继续需要一个新的明确 resume 命令。

最终 `.tmp/t63-controls-final.log/.xml` 与逐项 events：6 passed / 207.74 秒，pytest sessionfinish 0（跨中断丢失原进程句柄，未冒称观察到进程退出码）。包括 after_control_machine / after_control_book 两个真实 os._exit(80) 子进程、冷服务原 start 续办不发新许可、明确 resume、实际调用/停止边界/并发暂停和冷进程。最终构建 d27299ea179dc40abae74d8acf3faec833e6cdeb387bf918efe421e0ba3160e2，`.tmp/t63-controls-final-source.json`；较第三轮仅 workbench/reviewed_batch_controls.py 的冷重试修复。`.tmp/t63-controls-audit.json` 复核三轮全部 collected / setup-call-teardown / sessionfinish / XML、当前生产指纹及最终测试 hash。92 个不同节点仅为不同构建的历史覆盖索引，最终构建实际重跑 6 个。当前本组件没有活动测试或服务。

恢复执行策略新增对归档已有 reviewed/batch_controls/*.json 的阻断。旧路径及原字节保留作历史，新路径检查本身不创建或授权。与旧 grant 一样，恢复出的副本不凭磁盘控制状态自动继续。

## 后续完整集成

仍需实现真正 policy 生成/提取请求来源、各阶段 typed 结果核对、原位作者稿与提取审核接管、原发布 Intent/索引幂等续办与 receipt readback、原单次修复资格及全量重提取重检、有限三章 success 与修复失败后下一章零调用、显式恢复未知调用/未知时间、公开 UI 与原生浏览器。当前 start/source 校验限定原批次首章输入；后续章推进还必须核验前章正式 receipt/readback 后的新权威上下文，不能直接按控制日志中的章号开始。

整套 T2.4/T5.2/T5.3/T6/T7.1/T8.1 及 D/E/F 保持未完成，金额表单原自动审批 409 与 Windows 长路径发布门禁仍是原限制。
