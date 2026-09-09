# T3.2：正式提交服务实施记录

状态：进行中。阶段 B 检查点 `edea18d`；完整 T3.2 尚未验收，正常入口仍拒绝 identity 3.0。

## 已实现的准备边界

`core/engine/chapter_commit_prepare.py` 提供只读 `prepare_reviewed_chapter`、当前输入重验和同幂等键请求比较。输入必须由服务端核验后的草稿/审核记录及真实上下文 builder 提供，不能将客户端 JSON 作为 trusted context、作者确认或基线来源。

最终正文沿用现有 writer 的 `rstrip() + "\n"`；先形成最终字节，再检查事实引用，既不挪动偏移，也不静默修改引用。绑定原始草稿内容哈希与修订、规范化正文哈希、完整事实决定及修订、候选 intent、read-set、identity/authority、已审核状态源及人工修订。即便改稿后恢复同文，修订不同仍需重新准备。只允许下一个未提交章；准备前后重验完整 read-set。

已实现的是 `facts_prepared` 阶段，`publication_authorized=false`。作者审核沿用现有 `chapter_fact_review` 记录类型；自动策略接受必须使用后续独立适配器，不能伪造作者布尔确认。哈希只能绑定完整性，该层没有锁、正式写入、幂等记录占位、质量批准或升级操作。

定向回归：`.tmp/stage-c-commit-prepare-final.xml`，75 项通过，含 22 项新准备检查及现有事实/D1上下文检查。覆盖真实磁盘 builder、状态推进预览、无文件写入、来源后改、章号占用、同键换内容、正文/审核修订、准备结果伪改、重算hash仍跨authority、自动接受不能冒充作者确认，以及原文引用有效但规范化后失效的具体案例。早期测试夹具误把 proposal 的 evidence_quotes 当成已解析 spans、误读 v1 的内存 authority 投影，已修正；早期失败 XML 保留，不作为通过证据。未运行全套 pytest 或浏览器。

## 服务端准备适配

`workbench/chapter_commits.py` 已实现 `ChapterCommitPreparationService`。请求只接受登记、章节、审核 ID、预期修订/哈希及幂等键；服务端从同一 SQLite 读取快照取得草稿与审核，复用存储层确认哈希校验和完整 Candidate 证据核验。来自模型提取的建议须保留实际提取 intent、record、source 及其绑定。计算后重新取得数据库快照、候选证据与 read-set，拒绝期间发生的编辑或文件损坏；不重调模型。

提交准备复用独立的 `validate_trusted_chapter_context`，不经过仅供模型提取使用的提示词字符预算。默认 builder 仍不发现 D1 文件；测试使用内部显式注入。预览不占用长期写锁，正式提交仍必须在锁内重验。

定向回归 `.tmp/stage-c-adapter-regression-final.xml`：88 项通过，含 20 项新适配测试。覆盖真实候选/草稿/审核流程、WAL 并发编辑、同一读取快照、确认和候选文件损坏、保留模型建议来源、跨登记/修订 CAS、默认入口拒绝以及无正式文件/数据库变化。早期 `.tmp/stage-c-adapter-regression.xml` 因指定不存在的测试文件未收集测试，不作为通过证据。session 51842 已结束，最终 XML 已核验。没有 UI 接入或全套测试。

## 后续实现顺序

正式 codec 和凭证历史 reader 已实现，详见 [格式实施记录](reviewed-state-format.md)。它们仍仅由内部注入使用；已通过实际 v2.1 隔离发布及候选→服务端准备→下一章上下文桥接。当前状态必须匹配唯一历史末端，逐章重算事实，并绑定完整基线。这个检查点没有启用 identity 3.0，也没有把测试发布 helper 接入产品。

1. 已完成服务端读取适配；后续 commit 在作品锁和 DB 锁内复用并重验，禁止直接信任客户端传入完整计划。
2. codec 与来源/历史验证、identity 3.0 内部格式及升级事务已完成隔离验证，详见 [身份升级实施](reviewed-identity-upgrade.md)。实际校准报告、旧身份、状态与完整准备 evidence 绑定，标记前回滚/标记后完成通过。普通 schema/writer/job 契约仍未启用新版；产品升级还须维护、备份和接入预检。
3. 在作品锁和固定顺序下使用现有 v2.1 持久化事务绑定正文、七类当前状态、游标与提交凭证。升级保持 book_id 与旧正文/不可变历史；真实作品升级仍待 T7.1 备份和 T2.2 接入预检。
4. 幂等记录从文件事务/凭证识别；同键不同请求冲突。DB 提交索引可由凭证重建，DB 原生草稿不因此免除持久化/备份。
5. 预览与提交 API/UI 接入、标记前后故障、DB 丢索引恢复、相关 EA 回归及实际浏览器。到 T3.2 完整验收时运行全套 pytest/smoke。

D3恢复约束继续适用：不可修改旧历史 hash 或放宽旧物理 guard；新格式完成后必须重新通过 T7.2。长路径预算与交接中断继续/回退列入 T7，当前实验通过不能代替产品实现。
