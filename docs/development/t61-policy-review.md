# T6.1：策略事实审核与正式领域提交

2026-11-05，内部工程验证中；完整自动模式和 D/E/F 未完成。真实 Provider、真实作品写入及新增正式接受均为 0。所有正式提交测试仅使用隔离合成作品。

## 实际输入与审核来源

ReviewedPolicyReviewService 在现有机器作品所有权、Provider 排他和 SQLite 边界内读取原批次的机器/作品/ready 授权，核对初始输入、章纲、模型、策略和软件版本。服务只接受原 draft/extraction ID 与 CAS；完整事实来自实际已完成 ChapterFactExtractionStore 原提取凭据和 proposal。没有从客户端接收事实、质量结论或授权对象，没有调用作者审核表的 create/confirm 接口。

policy_fact_review 记录 review_source=policy、author_confirmed=false、accepted_for_quality_check，绑定原批次授权/策略/预算、精确最终正文版本、候选、上下文/权威状态及提取 Intent/record/proposal。只接受完整且未编辑的 model/pending 建议，不把作者编辑过或部分挑选的建议静默变为自动审核。原作者草稿和作者事实审核表不被修改。

未知身份、无初始数值、事实顺序不确定等仍由公共事实准备/状态归约器拒绝；当前策略不猜测身份声明或作者基线补充。模型完整提取和精确引文不证明没有语义遗漏，实际有效性仍须样例和阅读评估。

策略审核阶段是原章预算内的本地主动执行，禁止 Provider dispatch；准备结束核对时间、原草稿、原提取、所有权和读集后保存独立不可变 policy_review。该记录不包含发布许可或批次 step 执行许可，服务构造/读取不派发模型。

## 公共领域结果与可核验历史

prepare_policy_reviewed_chapter 是明确的 typed 内部适配器，使用与人工相同的绑定检查、正文规范化和事实计算。人工适配器仍只接受真实 author 记录，不将 policy 记录制造为 confirmation_sha256。策略准备包含 policy_acceptance 与原 policy_review，而非作者确认字段。

正式状态转换保留 acceptance_kind=policy、完整原审核证据与批次/策略/提取哈希；冷历史验证重放事实并核对全部来源。人工记录仍是 acceptance_kind=author，原字段和旧报告不因引入自动审核重写。两种来源在相同正文/事实下应生成相同领域状态。

prepare_candidate_chapter_quality 仍核对实际 Candidate、章纲、最终正文、完整性、叙事及事实状态。正式 policy 事务还要求自动处置为 continue；人工允许作者核对的警告不能被自动路径当作无问题。未知/暂停/单次修正建议本身不能授予发布。

## 首轮实际失败与验证证据

首轮原 94164 exit 1，13 pass、1 fail / 77.72 秒，`.tmp/t61-policy-first.log/.xml`。合成好章节来自另一前章语境，本地人工质量虽通过，continues_previous_ending 及对应规则实际报告警告，自动策略正确暂停。原处置保留 `.tmp/t61-policy-first-disposition-failed.json`。负向用例保留这个开头；正向合成输入补入本项目前章的具体闸门/旧伤衔接，没有降低质量策略或把断言改成接受暂停。

第二轮原 70881 明确 exit 0，69 passed / 408.99 秒：`.tmp/t61-policy-second.log/.xml`、`.tmp/t61-policy-second-events.jsonl`、`.tmp/t61-policy-second-source.json`，验证策略 typed 来源、实际原提取绑定、人工/策略领域结果相同、伪造/重新哈希/类型损坏拒绝、实际 policy 事务与冷历史、原人工事实准备/候选质量/正式事务恢复回归。运行时生产及测试冻结。

随后将旧策略 codec 与“当前最新策略”校验拆开。core/quality/reviewed_policy_versions.py 保存明确支持的原 action/standard 策略快照；校验完整字段、值及 SHA，不只信任一个版本号。新审核建立时仍必须符合当前策略；历史验证按原快照解释，不能调用 latest 配置把历史重算成新政策。未来策略改变必须新增 codec，不能改写旧 codec 的语义。

第三轮原 7131 明确 exit 0，90 passed / 145.81 秒：`.tmp/t61-policy-third.log/.xml`、逐项 events 和源清单。新增有限章号/策略字段重哈希、布尔冒充数值、未知策略拒绝；新进程将当前 standard 版本和 action 选择换成未来版本后，原实际已提交的 policy 历史仍能完整验证，新审核则拒绝旧批次。另覆盖原质量分级、修复周期和事实归约回归。

最终原 95042 明确 exit 0，32 passed / 397.25 秒：`.tmp/t61-policy-final.log/.xml`、`.tmp/t61-policy-final-events.jsonl`、`.tmp/t61-policy-final-source.json`，源指纹 `0d504de346131a653a156a81b8aab1769f702f42b146f879a1ccbe93782f6544`。21 项策略专项含质量计算后再次核对原授权、真实 os._exit(78) 发生于 actual apply 后/commit marker 后，原事务分别恢复为回滚/提交成功，不重新运行审核器或模型，原作者稿不变；另有原公开发布 API 10 项、原 grant 正常流程 1 项。

三轮全部 node/setup/call/teardown、终态 sessionfinish 0 和 XML 已复核；当前源与最终清单一致。去重 159 个节点只作为分构建历史覆盖索引，不能当作最终构建全套重跑的声明。原策略测试文件后续加测，旧 hash 保留，最终文件匹配运行前 hash；其他相关测试文件仍匹配各自记录。终态 `.tmp/t61-policy-review-progress.json`，当前无活动测试或服务，没有新增公开路由或后台自动 driver。

## 尚未完成的授权与执行边界

本服务目前是显式调用的内部本地准备，不是完整自动执行入口。测试先以明确的合成作者请求生成/提取实际原证据，再调用策略审核；没有将 author-only 生成/提取入口冒充成自动调用。自动批次驱动的持久化显式 start/resume/stop、原位人工接管、policy 生成/提取调用来源、逐章提交服务的原 Intent/索引续办、单次修复与完整重提取重检、三章及失败后下一章零调用、工作台启动和浏览器流程仍需集成。

已验证策略快照解码的版本分离和未来当前选择变化后的冷读取。仍需更广的未来版本迁移/归档兼容验收，不能把“历史可读”当作“当前可执行”。源缺失的可写恢复、金额表单原生审批 409 及 Windows 长路径发布门禁仍是原未完成范围。
