# T7.1 delivery / autonomy 严格只读状态预检

日期：2026-09-11。新增 35 项不同用例，T7.1 累计 216 项；完整 A–F 目标仍 active。

## 已接入的检查

`workbench/backup_auxiliary.py` 与 `backup_autonomy_controls.py` 已接入 `inspect_backup_scope`，通过既有清单中的相对路径读取文件。每份文件核对清单中的字节数和 SHA-256；总量限 10,000 次文件读取、256 MiB，单文件 32 MiB。损坏文档同样消耗读取预算。只读取已归档作品根中的文件，不从 JSON 声明发现外部授权根。

- delivery：核对 job 文件名、作品、操作摘要、载荷、回执 hash、任务/运行/发布/载荷/目标类型绑定、回执文件名与序号、attempt_count、latest receipt 及其结果。缺回执、孤立回执、错误计数或结果冲突阻止认证。`delivering` / `uncertain` 明确为未结束，过期时间不自动解除未知状态。无尝试的 pending 保留为待处理；未调用 adapter，不改 job。原显式确认远端不存在后重新进入 pending 的协议仍允许其特定旧回执状态。
- autonomy：使用纯验证函数核对租约历史的连续性与 hash、作品/路径绑定、current 是否等于最新历史；缺失或落后的 current 标记需要恢复，历史末端仍 active 时即使过期也阻断。核对 session genesis、instruction plan、事件的顺序、前驱、允许的状态转换和作用域；活动 session、缺失或错误 latest 指针阻断。
- operation：核对确定性 intent ID、作用域、source verification 与 lease、commit marker 的类型和证据、result 对应的 session event/lease 和原前置事件。缺 result 作为未完成；rollback 与 commit 矛盾、completed 缺 marker、缺事件等拒绝通过。
- root 范围：规范 deliveries/autonomy 根，以及现有清单中具有原局部 `.persistence.lock` 和已知控制命名空间的自定义根。不是任意自定义历史根的完整发现协议，未认证所有可能丢失锁/未知布局的根。

检查不创建运行时存储对象，不调用 `BookLeaseStore.load`、`AutonomySessionStore.status` 或自动重建 latest 的路径，也不取得会创建锁文件的运行时恢复锁。纯 schema/hash 校验不代表已完成整套内容历史和恢复证明；结果保留 `complete_history_certified=false`。

## 验证

- `.tmp/stage-d-t71-delivery-settlement.xml` / `.log`：41 项通过，48.51 秒，session 21136 exit 0；首批 delivery 检查、scope 与 writer 组合。
- `.tmp/stage-d-t71-auxiliary-settlement.xml` / `.log`：32 项通过，17.47 秒，session 89085 exit 0；首批全部新只读检查。
- `.tmp/stage-d-t71-auxiliary-settlement-final.xml` / `.log`：100 项通过，140.73 秒，session 73708 exit 0。最终包含新增 35 项、scope、来源、归档，以及 delivery/autonomy 真实进程写入竞争回归。此轮无跳过、无失败。

实际进程死亡注入：delivery 在远端变更边界（替身，无远端发送）和 receipt 写完之后分别 `os._exit(73)`；真实备份持锁预检保留原 job 字节，列出未结束状态，不补 receipt、不重发。autonomy 在 lease history 原子写完后 `os._exit(79)`，current 未产生；只读检查报告需要恢复，前后清单完全一致，未重建 current。

故障覆盖：缺失/损坏/错归属/孤立回执、错误 latest hash、极大 attempt_count、伪造空成功状态、清单后文件变化；active session/lease、缺/旧 current、缺/错 latest、缺 result/marker/event、孤立 operation、租约历史缺口与极大 generation。测试使用实际记录并选择性破坏，检查前后比对完整文件/目录清单。另将运行时恢复构造和写 API 替换为直接失败，证明预检未进入这些路径。

归档集成额外验证：未完成投递和活动租约可进入用途明确的 diagnostic 包，ZIP 及清单校验通过但 `recoverable=false`，对应 scope blocker 保留，原任务和租约字节不变。没有把资料保存等同于可恢复认证。

全部测试和注入子进程终态，无活动服务。未调用真实 Provider、未向远端发送、未改真实作品。没有本轮新的浏览器或产品 API 验收；既往浏览器创建表单自动审批 credits 拒绝与并行代理 credits 中断仍待验。

## 后续与局限

这一组件证明已识别控制存储的只读状态与相关记录绑定，不证明所有 stage/completion/arc 内容历史、模型调用证据与事件权威后端完全一致，也不证明投递目标的全部外部文件随包完整。自定义根缺锁、任意布局、未知脚本写入口仍须由完整支持范围清单与根/引用检查补齐。

T7.1 尚未完成：剩余支持入口、完整历史/必要根与加载代码版本认证、归档产品 API/UI 都待实现。归档仍 diagnostic。T7.2 恢复新绑定、草稿/预算/批次恢复和同机所有权交接待完成；T5.2 金额依据与后续共享预算、完整 A–F 仍未完成。

后续产品入口必须追踪后台线程真实生命周期：HTTP 取消不能释放 snapshot 许可，关机须等待实际归档结束；不能直接把普通 `run_async('http_write',...)` 用于维护期间归档，也不能只 await 一个可提前取消的 to_thread。现有正常写请求的 shield/drain 机制可复用思路，但快照许可需要独立接入并验证。
