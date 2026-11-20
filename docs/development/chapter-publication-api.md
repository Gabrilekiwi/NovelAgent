# T3.2：新版作品提交 API 与发布页

## 2026-11-12 整合回归与浏览器验证

修复范围：空间回退原先在上一场景缺失时任选人物位置，将稳定地点ID变成必须写入正文的开场桥，导致已审章节预览和正式提交误失败。当前只修复非空且不可用的地点词；没有声明的场景保持未知，不降低已声明连续性或正式提交的证据要求。增加未知场景不被推测、原快照不被改写的检查，原“最后”伪地点回归继续保留。

浏览器状态：该准备完成的合成发布路径通过；完整工作台与恢复验收仍为partial。当前源码 `085a92a54177e9e8839f3b9fcbeb30040ebef34c4057266387f7fbacd047b984`，Chrome DevTools page3，隔离上下文 `resume-20261112-publication`。地址 `http://127.0.0.1:8898/publication?project_id=project_00762b427e5a459fbb4f94cd039a215d&chapter=2&review_id=fact_review_bcec9373015a9089b6c066ed2056bd4e`。独立合成作品、预先准备且确认的草稿/事实、默认应用服务、无worker、外部连接被拒绝；不是从空作品开始，也未使用真实模型或真实作品。

实际点击生成提交预览：req10 POST publication-preview 为200，质量覆盖完整且本地通过；仍显示作者需核对的前后衔接提醒，没有隐去非阻断问题。实际勾选作者确认并点击发布：req11 POST publication 为200，`publication_verified=true`，凭证 `commit_f4abc6c277a6834b605eed22159fcf06`；正文、事实及凭证同时核验。刷新后req16 POST publication-status与req18 GET publication-input均200，页面仍显示原凭证及已发布，未重复提交；点击下一章进入第3章，八条读取请求均200且未自动生成。最终Console无error/warn。合成夹具的第三章复制第二章标题，不据此声称标题展示或真实章节内容质量已验收。

自动化负向范围覆盖来源变更、伪造结果、缺失所有权、恢复标记和证据损坏等拒绝，见当前202项专项；本次浏览器未再注入这些发布故障，也未做截图布局验收。原生金额预算故障交互是另一个构建，单独记录于`t52-budget-browser.md`。原始浏览器快照/响应在`.tmp/resume-20261112-publication-{preview,after-commit,after-refresh,next-chapter,refresh-network,response-10,response-11}.json`。服务52149/PID24128已停止。全套回归、完整恢复流程和真实收益门槛保持待验。

当前补充（2026-11-04）：POST `publication-preview`的本地检查和新发布的复检/准备/完成已接原冻结章的主动计时，新增时间未知/耗尽门禁；它只会写原预算的执行区间，纯准备函数仍只读。GET input/budget不开始检查；原请求查询/恢复仍可用，不因新时间门禁丢失原发布结果。发布页增加当前预算读取、暂停状态与只读重读。见[质量检查计时证据](t52-quality-time.md)。下文为T3.2历史设计，“只读预览”指不改正式正文/事实/发布凭证，不能据此认定当前明确检查不计时。

2026-09-09；组件检查点，T3.2 与总计划仍未完成。

`workbench/app.py` 装配独立的 `chapter_publication_api`。仅这些路由持有显式 `ReviewedProjectRegistry`、真实新版上下文及 `ChapterPublicationService`；旧项目导入、章节卡、Candidate、worker 的格式限制没有放开。已完成真实升级与本机登记的作品，可以用实际 SQLite 草稿和作者确认审核调用本接口。没有自动升级或自动登记。

## 请求与结果

路径前缀为 `/api/projects/{project_id}/chapters/{chapter}`。

| 接口 | 行为 |
| --- | --- |
| GET `/publication-input?review_id=...` | 在作品锁和同一数据库内读取指定审核与草稿；不要求可变来源仍新鲜，因而中断期间仍能显示原草稿。 |
| POST `/publication-preview` | 固定标准质量策略；只读预览；有 pending、staging、损坏或未解决日志时阻断，不在预览中恢复事务。 |
| POST `/publication` | 重验真实输入并提交；发布结果以 `publication.publication_verified=true` 为准。 |
| POST `/publication-status` | 在所有权边界内恢复并查找原请求；从未提交返回 `not_started`，不会启动新尝试。 |
| POST `/api/projects/{project_id}/publication-recovery` | 只恢复原事务、核验历史并重建索引；不生成正文、不创建新提交。 |

预览、提交及原请求查询必须携带 `Idempotency-Key` 请求头。请求正文严格只接受 `review_id`、两个预期 revision 和两个预期 SHA；提交额外要求 `author_confirmed=true`。拒绝客户端提供的质量结果、状态、策略、路径或 preparation。恢复请求只接受 `author_confirmed=true`。沿用本机 Host、Origin 和会话令牌限制。

原请求在标记前回滚或在暂存前中断时，即使 HTTP 为 200，也明确返回 `publication_verified=false`、`committed=false`、`retry_requires_new_key=true`。新尝试必须由作者重新准备。确认已发布的重试先读原凭证，不读取新的草稿版本来重做提交。

## 修复的恢复边界

独立复核用真实隔离事务复现：after_prepare 中断后损坏 candidate_result，第一次恢复留下持久 `registry/recovery_required`，但第二次调用的瞬时恢复报告可能 `ok=true`，导致换 key 绕过阻断。

现于恢复前后检查持久阻断目录，任何条目（包括非 JSON 损坏条目）都保留并阻断。此外，恢复后的全部 journal 必须可读取且处于 completed/rolled_back，pending/staging 必须清空；这覆盖 journal 已发布、pending 尚未登记的中断窗口。损坏证据不会被删除或靠换 key 跳过。已知人工来源与章节游标变化现在转换为明确的 source drift/conflict；真正损坏的事实证据仍保留完整性错误。

## 发布页

`/publication?project_id=...&chapter=...&review_id=...` 读取真实作者审核，显示最终正文、标准质量检查、接受的事实与提交后七类完整状态。检查通过后仍须勾选并点击发布。页面在发送前持久保存原请求与 key；刷新后先查询原结果，成功后禁用发布，回滚后显式“重新准备提交”才换 key。没有可用浏览器存储时不发送提交。服务器响应损坏或丢失时保留原请求，提供查询操作。发布后草稿若继续变化，不把新草稿显示成已发布正文。

浏览器证据见 `chapter-publication-browser-verification.md`。自动测试使用真实升级、确定性 Candidate、SQLite、文件事务与显式隔离所有权，真实 Provider 调用和真实作品写入均为 0。

## 尚未完成的主流程

当前是可直接访问的发布页及实际 API，**尚未接上主工作台导航中的新版作品完整操作链**。下一步必须完成新版作品的只读展示与 owned recovery、Candidate/事实提取/作者审核路由的明确装配及所有权边界，再把已确认审核自然衔接到本页；旧 worker 不能因此获得新版写入许可。自动模式仍需自己的真实接受策略及共享提交调用。

新浏览器没有本地请求时，应由主章节详情从凭证展示历史发布状态；不能只依赖本页 localStorage。真实升级/登记仍待维护预检、T7.1 可验证备份、T2.2 接入与真实 Strict 校准。T3.2 完整主流程、全量 pytest/smoke、两模式及后续阶段验收尚未完成。
