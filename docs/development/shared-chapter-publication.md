# T3.2：有所有权和原意图约束的共享提交服务

日期：2026-09-09。基于 `bbedefc` 未提交工作区。实际v3共享提交后端已实现；公共应用装配、登记启用、worker/API/UI和完整T3.2验收仍待完成。

## 请求与锁

`ChapterPublicationService.commit` 只接受项目/章节/审核ID、作者稿和审核的预期修订/哈希及幂等键。先读取登记以定位作品所有权，再按机器作品锁→Provider边界锁→SQLite BEGIN IMMEDIATE→既有持久化锁的顺序执行。锁内重读登记，验证活动所有权，阻止未知Provider调用，确认新格式持久化根与已登记作品完全相同；恢复原reviewed事务后才冷读当前身份、状态及发布索引。

`BookOwnership` 默认固定使用当前用户的LocalAppData/NovelAgent/machine-books，独立于可配置工作台数据库目录。内部测试可注入隔离机器目录，公共请求没有该参数。所有权绑定book_id、作品物理目录链、DB物理文件及父目录链、登记ID、identity字节和代次，使用既有InstanceLease的实际OS锁；同进程线程在同一机器作品键上排队，跨进程竞争被OS拒绝。

`enroll` 必须在锁内、同DB事务下显式执行并验证真实新版历史；它不会替换已有的不匹配或停用登记。commit自身不自动认领或重建缺失所有权。恢复转移、变更代次和停用原登记的公共流程留给T7；普通应用尚未调用这个登记入口。

项目登记读取、Candidate来源核验、草稿/事实对照和最终事实准备可复用同一个SQLite连接。只读preview仍打开独立快照以检测并发修改；commit则持有写事务，阻止作者稿、事实和登记被另一个数据库写操作改变。事务准备后再次重验所有权和完整准备，再执行正文/状态原子提交。源码或人工来源的最后窗口变化仍由实际事务read-set检查阻断。

## 持久原请求与重试

在真实上下文、请求和标准质量已验证，但尚未创建事务journal时，服务先以atomic create保存 `reviewed/intents/<book-scoped-key-hash>.json`。其中包含完整原请求准备证据、确定的run_id、当时所有权hash及自身校验。原始幂等键不归档。`build_reviewed_chapter_transaction` 只构建和验证；原有 `prepare_reviewed_chapter_transaction` 包装器继续立即stage，因此既有内部调用不变。

每次重试先恢复已存在的reviewed事务，再从真实凭证重建并查找发布请求，早于可能已前进的章游标和可变作者稿读取。已发布的同一请求返回原发布记录；相同key换ID/修订/内容hash会冲突。文件发布后SQLite结果未提交的中断，通过凭证重建索引，无需重新生成或准备作者稿。

若原意图已记录但尚未stage，重试返回 `interrupted_before_stage`；若marker前中断，先按原journal回滚，再返回 `rolled_back`。这两种结果都保持 `committed=false`、`retry_requires_new_key=true`。服务不会默默换run或重开同一逻辑请求；后续UI需让用户明确重新提交并使用新key。损坏的意图、错配的journal或未解决恢复不被当成全新请求。原journal与请求绑定会重新核验。

marker后恢复完成原publication，即使当前状态在启动时尚不能通过普通冷读。返回成功只能来自严格验证的发布凭证及请求/质量归档；SQLite行和本地意图文件自身都不能授予成功状态。文件状态是发布依据，作者稿等DB原生内容仍依赖DB持久化与后续T7备份。

## 剩余接入

T3.2仍需：将新版登记读取与有所有权的恢复接到实际应用/worker，确保旧writer不能因普通身份门放宽而写新版作品；接真实preview/commit API和页面，映射busy、原请求回滚、待恢复和作者稿/来源变化等状态；完成浏览器和T3.2全套pytest/smoke。所有权目前只覆盖该共享服务，尚不能宣称每个产品写入口已装配完成。真实升级的旧事务/活动进程维护预检、T7.1备份和T2.2基线接入仍是后续必要条件。

本轮测试仅使用 `.tmp` 真实v3隔离事务、实际SQLite、OS锁与确定性Candidate。未知Provider测试注入边界查询结果以验证服务拒绝分支；没有制造或发送真实Provider调用。没有真实作品写入、UI/服务启动或独立代理复核。此前代理与Git自动审批的额度错误仍保留在交接中，文件与证据落盘，HEAD仍为bbedefc。

## 验证结果

- `.tmp/stage-c-shared-commit-initial.xml`：9 passed；同进程并发两次提交只发布一次、同key换请求冲突、四处服务中断重试、两个原事务中断恢复、过期作者稿、缺少机器登记拒绝。
- `.tmp/stage-c-shared-commit-boundaries-final.xml`：6 passed；同一数据库连接及竞争写入被锁、请求中所有权代次变化、stage后来源变更、未知Provider阻断、意图损坏拒绝、新进程恢复缺索引且禁止调用准备函数。
- `.tmp/stage-c-book-ownership.xml`：5 passed；不同DB同book_id不能重新认领、同路径目录替换被拒绝、停用/损坏记录不自动修复、另一进程被实际OS锁阻挡。
- `.tmp/stage-c-shared-prepare-regression.xml`：53 passed、14 subtests passed；原提交准备、事实审核、项目登记和候选质量回归。
- `.tmp/stage-c-shared-commit-transaction-regression.xml`：53 passed；实际v3正式事务、工作台实例所有权和D2格式门回归。

合计126项测试、14项子测试通过；新增20项测试。session1890、57084、17142、46958均exit0，ownership短任务exit0，无活跃测试/服务。早期边界session65971与 `.tmp/stage-c-shared-commit-boundaries.xml` 为5通过/1失败：来源变更已正确被拒绝，但测试误要求ValueError，而现有事实核验层实际包装为ChapterFactReviewIntegrity；改为真实异常类型后最终6项通过。该包装在公共API接入时须明确映射“来源过期”与“证据损坏”，不能直接向作者返回笼统500。
