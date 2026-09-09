# D3：同机可写恢复有限试验

日期：2026-09-09。状态：**T7.0 进行中，尚未通过。** 起始代码 `8868e6b`。不升级真实作品，不调用真实 Provider。

## 试验目标与边界

在同机新空目录恢复一个有正式事务提交记录的作品和一份 SQLite 作者草稿。保留原 book_id、正文、不可变 manifest/marker/receipt；建立独立可验证的新物理绑定。重启进程后通过假 Provider 提交下一章，并验证原目录失去写入资格、恢复目录默认不自动执行。

试验先核对当前两类历史：目标 legacy 路径目前仍有 v1 绝对路径事务；v2.1 另有 logical root UUID、目录 device/inode 与控制锁身份。不能只证明其中一种复制成功，就宣称整个作品可恢复。

## 已观察机制

- `project_root_remap.py` 明确只支持同卷 rename；原路径仍存在、复制后的目录/锁文件身份不同都会拒绝。它不是恢复导入 API。
- `SafePathResolver` 在 prepare 时绑定父目录 lineage、device/inode、mode/reparse，复制文件内容不会复制这些物理身份。旧准备计划必须失效。
- v2.1 的 root registry 是可变控制面；immutable manifest 绑定 registry_id 与 logical UUID。已完成 receipt 的验证允许显式改变 registry revision，但活动事务要求同 revision/digest，不能改旧 manifest 来放行。
- 工作台数据库还包含作者草稿、任务和登记根。使用整份 SQLite backup API 快照；后续恢复应只启用所恢复目标，其他登记作品默认禁用。

## 基线观察步骤

1. 隔离创建 legacy Strict 样本；用真实 v2.1 持久化事务保存一章及游标、运行记录和 receipt，保留人工草稿于 SQLite。
2. 先核验源 receipt，再复制到新空目录，并用 SQLite backup API 复制数据库。
3. 逐文件哈希确认复制内容相同；分别调用未改的 remap 和 SafePathResolver，记录拒绝原因与操作前后清单。不删除原目录以诱导通过。
4. 这只是观察现有边界，尚未构成新恢复方案，也不声称经真实 AgentExecutor 提交或通过模型质量门。

## 第一候选方案需满足的条件

- 来源证明绑定一致快照清单、源 identity、所有可变 registry 与不可变历史、作者草稿、目标空目录和新物理身份；缺文件/额外外部根/未知事务均拒绝启用。
- 明确区分“核验历史已提交内容”与“继续执行旧计划”。旧计划永不复活；不修改旧 manifest/hash、放宽普通 PathGuard 或将全部归档视为可信。
- 在锁定与停写条件下追加恢复意图/凭证，更新可变物理 registry 或建立新的执行控制面；失败可以重入检查，不能产生两个可写根。
- 本机作品级所有权覆盖不同工作台数据目录，不能仅沿用T1数据目录锁。交接期间零活跃写入，提交前复核当前资格。
- fresh process 使用相同 book_id 读取完整旧正文、草稿与当前状态，重新生成准备绑定，假 Provider 提交下一章。保存旧源/恢复副本两边的拒绝和成功证据。
- 损坏归档、目录替换、重复启用、原实例持锁与跨数据目录竞争有拒绝证据。

## 基线结果与第一候选假说

最终基线：`.tmp/stage-b/d3-baseline/39e167b6308742d4a61f22153b87d215/report.json`，由 `scripts/experiments/run_d3_restore_baseline.py` 产生。源 v2.1 receipt 有效；复制后拒绝 `verification root requires explicit remap-roots: chapter_artifacts`；remap 拒绝原路径仍存在；旧准备 guard 拒绝父目录身份改变。v1 candidate 字节可读，但 target 仍是源目录绝对路径，未对复制的 v1 journal 执行 reconcile。数据库草稿全部字段与正文保留，登记根仍是源路径。两份作品的文件清单在拒绝操作前后完全一致。

第一候选假说：保留已完成历史原字节，用可验证的恢复凭证绑定新物理控制面；旧 v1 journal 只作为历史读取，续写采用新的执行目录，不能重新投喂给旧 reconcile。所有书籍实例通过独立于工作台数据目录的同机作品所有权记录交接写入资格。此假说仍需实际进程与故障验证；不得把显式实验包装器当作普通生产入口已经接入。

第一候选尚未实现。基线观察不计作候选失败；如两个候选仍过不了同一门槛，先记录原因并调整设计，不扩大为通用跨机器恢复系统。产品维护状态、备份 UI 与完整恢复入口仍属于 T7.1/T7.2。
