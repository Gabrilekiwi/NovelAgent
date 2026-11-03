# T3.2：真实新版作品的候选与审核准备

日期：2026-09-09。基于 `bbedefc` 的未提交工作区修改。此检查点接通实际 identity 3.0 升级后的候选、作者事实审核和标准质量准备；完整 T3.2、正式发布、公共入口与任务契约仍未完成。

后续进度：内部正式章节事务已在 [新版章节事务](reviewed-chapter-transaction.md) 实现并验证，本篇保留候选检查点的原始范围；最新待办以开发交接和stage-c-progress为准。

## 接通的实际路径

`ReviewedChapterCandidateContextBuilder` 首先通过 `load_verified_reviewed_identity` 核验真实升级凭证、完整保留历史、当前状态末端及身份绑定，检查人工来源、静态引用和正文游标，再读取实际大纲、语义解析和 Strict 激活资格。当前七类事实来自已审核文档；人工 Markdown 保留审计用途，运行缓存不被采用。构建前后比较完整来源集合、物理根身份、真实 identity 字节和历史；新进程构建得到相同上下文与哈希。

`ReviewedChapterCandidateExecutor` 显式使用上述 reader，复用原 Candidate 的作者确认、不可变 intent、实际输入/正文/执行来源、预算、不确定调用和生成前后来源检查。它只保存候选材料。相同候选请求复用记录；生成期间来源改变时保留草稿并标记 stale。旧确认卡遇到大纲修改会在生成前被拒绝。

`ReviewedProjectRegistry` 只为已登记且实际升级的作品提供内部读取适配。默认 registry 与公共 import/create 行为继续使用旧格式门。章节卡、事实审核和提交准备服务可通过内部构造依赖使用新版 builder，客户端请求没有切换身份或注入来源的参数。事实提取上下文也实际读取到同一已审核状态。

隔离联通测试依次执行旧格式登记、真实原子升级、新版章节卡确认、确定性候选生成、SQLite 草稿保存、作者事实审核/接受/确认、服务端事实准备和真实标准质量检查。standard 可完整检查并通过；strict 因缺模型检查仍阻断。所有准备输出 `publication_authorized=false`，不改正式正文、已审核状态或游标。

## 本轮修正的来源缺口

初轮负测发现：原升级冷读绑定了升级时的完整 read-set，却未把基线之前已有正文的当前字节与该集合逐项比较。修改旧正文后重新捕获候选输入，可能继续沿用修改前确认的基线事实。

`reviewed_state_store._upgrade_checkpoint` 现在对原始已验证 manifest 中每个 `正文/` 成员重新核验实际安全读取的大小和 SHA-256；缺失或修改均拒绝。升级后新增章节仍由各自的发布凭证、正文归档和当前正式文件核验。不能通过重新捕获 read-set 将历史正文变化自动视作已接受。

## 范围与续接

普通身份 schema 仍只支持 1/2，global writer contract 仍为 1；默认 Candidate、普通 registry 和公共 read-set verifier 对新版继续拒绝。新版 reader 只支持读取检查阶段，不接受声明写入，不授权事务过渡。尚无作品锁或 pending 事务处理能力；正式服务必须先取得所有权并完成恢复，再在锁内重验。

下一步实现实际 identity 3.0 的正式章节事务及写入期间 read-set 过渡核验，接作品所有权/DB 事务、pending 与回滚原意图幂等，再接 worker/recovery/API/UI。必须实现升级后的候选—审核—提交—下一章冷读完整链；不能拿旧身份加内部 loader 的测试代替，也不能单独放宽普通 writer。

真实旧书升级仍依赖维护检查、未知 Provider/事务处置、T7.1 可验证备份和 T2.2 基线接入。合成 Strict 报告仅供隔离测试；没有新增真实 Provider 调用或真实作品写入。独立复核代理因额度错误未执行，本轮由主代理实现、测试和自查。

## 验证结果

- `.tmp/stage-c-v3-workflow-regression.xml`：92 passed、14 subtests passed，包含本轮15项真实升级候选测试，以及候选质量、本地质量、工作台项目/候选/事实/提交准备回归。
- `.tmp/stage-c-v3-context-regression.xml`：86 passed、6 subtests passed，覆盖普通候选、上下文、read-set、升级与已审核文档历史。
- `.tmp/stage-c-v3-format-ea-regression.xml`：46 passed、3 subtests passed，覆盖 D2 格式门、既有 EA runtime 和持久化屏障。

本轮三组共224项测试及23项子测试通过。session29929、80143、73256均已 exit0，无活跃测试或服务。新增15项测试。具体校验哈希见 `stage-c-progress.json`。初轮10通过/2失败定位上述历史正文缺口与旧 registry 格式门；第二轮12通过；扩展阶段14通过/1失败是负测误要求异常类型和消息，实际生成前拒绝已生效，已更正为真实 `ChapterCandidateConflictError`。早期 XML 保留用于追溯，不计入最终通过证据。

上轮本地 `git add` 被自动审批拒绝，原因为工作区额度耗尽；未绕过审批，代码、测试和交接材料均保存在工作区。
