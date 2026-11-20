# 作者基线审核修订与归档

最新日期：2026-10-30。作者审核服务、公开 API 与七类表单已接入；原生 Chrome 已验证保存、确认、刷新找回、历史版本和来源失效。文件选择器上传因工具目录权限仍待原生验证，**正式升级及完整阶段 D–F 尚未完成**。下文保留早期组件证据，以本节最新记录为准。

## 2026-10-30：公开作者审核入口

### 后续正式升级服务正在验证

新增 `workbench/onboarding_upgrade.py`，尚未安装公开 route/UI。它只接受实际作品、审核/归档标识和修订摘要，从实际 SQL 修订链读取已确认基线及完整原校准报告，核对当前上下文和原机器登记，复验实际认证及归档，并比较当前 scope、整库、配置和软件。预览给出明确差异、七类数量和条件恢复策略；提交还要求明确确认及相同的恢复策略摘要。`OnboardingPreviewService.can_upgrade` 仍未放开，完整 T7.1 未完成。

原锁内执行边界增加私有 `_on_verified_source`：只有快照期间所有来源、数据库、登记、见证和恢复证明再次通过一致性检查后才调用；原 OS 锁和 SQL writer fence 继续持有。普通备份不传回调，其原有检查不变。升级复用 `prepare_reviewed_upgrade_transaction`，将产品升级意图保存在机器作品控制目录，并保留原书稿、草稿和数据库；登记迁移仍使用既有 registered_identity_upgrade。

首次 26004 observed exit1/1 setup error/31.21秒：新测试漏导入 `private_temporary_directory` autouse 夹具，scratch 不存在，真实数据库/jobs/references 离线核验正确拒绝认证；没有进入升级。第二次 65484 observed exit1/1失败/58.45秒：实际到达事务 commit，来源快照持有的身份 state lock 被事务再次申请，Windows 报 PersistenceLockBusy。原身份未修改，但原事务准备及机器 fence 已留下，仅在隔离样本中，不能称为完成。

随后增加经过验证的同线程原句柄借用：辅助 state fence 可以由其当前持有者借用；运行根仅在私有 `_borrow_held_run_locks` 范围内、且属于范围开始时已经持有的原句柄才可借用。普通嵌套运行锁继续拒绝；不能跨线程、在原锁结束后或对后取得的新根借用，也不释放原 OS 锁。原33834 observed exit0，锁读取/作者写入回归19项12.06秒通过（含5项新增边界）。

构建 `29d62a4e6f2c7a04e1518c466ceab4580df3f2d872687c01092ff05cc798c98a`：原35591 observed exit0，7项410.02秒；原76788 observed exit0，29项401.99秒。之后增加未结束旧任务门禁，构建 `cd9d4f0916e0abb41a6fb3d89364de779ccbf55c87189853ee99a8091372058c`：原85971 observed exit0，3项通过/6项未选139.73秒，覆盖queued、held及正常1.0升级；未在新构建重跑全部旧检查。当前无活动测试。记录见 `.tmp/onboarding-upgrade-progress.json`。

下一步必须完成实际升级结果核验、原操作查询/丢失响应/明确续接，再接公开预览/确认及原生浏览器。准备阶段与提交标记前后中断继续使用原事务规则；不能凭机器意图文件存在便声称已完成，也不能让新请求替代原续接。这里没有真实 Provider、真实作品写入或新正式接受，完整 A–F 继续 active。

工作台“诊断归档”中选择作品后，可进入 `/baseline-review?project_id=…`。页面提供七类普通表单、已有资料保留、原文核对与摘录、草稿修订、逐类明确确认、保存记录发现及历史版本查看。旧版本只读；确认后编辑重新成为未确认草稿。页面刷新不自动保存或确认。

`author_baseline_service` 在真实备份维护许可、BookOwnership、作品状态锁、Provider 执行锁及同一 SQL 事务内保存；插入后、提交前重新核对来源、机器登记、维护许可和软件版本。缺失、停用或丢失见证的登记不会被此入口重建。历史读取独立于当前来源，历史确认不能自动成为当前有效确认。

`author_baseline_api` 接入 `/api/projects/{project_id}/onboarding/baselines`，提供列表、历史版本、建议、限定来源读取、预览、新建、编辑、核对及确认。列表明确 `history_verified=false/source_checked=false`；选定记录才核验完整修订链，当前有效性另行核对。所有结果继续 `can_upgrade=false/publication_authorized=false`。

建议仅复用确定性人物、位置、计数种子，七类一律标为未核对。来源读取限当前 capture_sources 的设定/追踪文件。单文件 1 MiB；审核记录 2 MiB、单链 1000 修订、项目 100000 行/256 MiB，追加失败保留原历史。完整原校准报告仍需匹配当前 Strict 身份，不能从身份摘要补造。

### 校准报告在浏览器中的真实缺陷

首次 Chrome 的编辑、确认、刷新和旧版查看成功，但“以旧内容新建审核”预览返回 400 `calibration_report_hash_mismatch`：JavaScript 解析再发送会将原报告的 `1.0` 改成 `1`，与 Python 的原校准摘要不同。

现以原 JSON 文本传递校准报告，后端解析并执行原有完整校验；历史、保存及核对响应额外返回 `calibration_report_json`，供新建审核继续使用。上传处理也保留 `file.text()` 原文。没有重算报告资格或放宽摘要校验。集成测试同时重现数值归一化拒绝与原文传递成功。

### 自动检查与构建边界

- 原 67864 exit1：25 通过、1 失败，61.96 秒。测试将 Windows CRLF 与 `read_text()` 归一化结果比较，而来源 API 正确保留原字节；仅修此断言。原日志/XML 保留。
- 原 18103 observed exit0：41 通过，414.24 秒；构建 `a606ef0ef9b4e116ffc1237d7e88ec88ed929cde91a94911c0a0029d7a08e8f5`。覆盖公开 API、24 项存储/来源检查及 15 项真实维护/事务/并发服务检查。此构建后来由原生浏览器发现报告数值传递缺陷，41 项不能证明当时新建页面正确。
- 原 72722 observed exit0：35 通过，331.70 秒；最终构建 `056ec93348902feee6a9d8fb693e2748cce25070e41d069f816f38201897f958`。覆盖修复后的 2 项公开 API 实际流程、6 项作者审核归档检查、5 项公开认证回归及 22 项既有表单检查。服务/存储的前一批没有冒称在此构建完整重跑；两批的 2 项 API 重复不累计为新检查。
- 最终 `node --check` 通过。两个样本服务与测试均已终态，关闭后源指纹仍一致；端口 65071、65479 已关闭。

### 浏览器验证结果

状态：核心审核流程通过；文件选择器上传部分待验。

环境：本机原生 Chrome / Chrome DevTools MCP；最终隔离服务原 88423、PID 3600、端口 65479。合成 Strict 资格及 `.tmp` 样本，SDK、worker、自动导入与非回环网络均被禁止。

实际操作及结果：

1. 从保存列表加载历史，复制为新审核；读取原资料、添加真实原文摘录，预览/新建均 200（req14/15）。请求中的校准报告为原 JSON 文本，原摘要校验通过。
2. 查看七类表单并分别勾选，加上总确认后，明确确认 200（req17），记录从 draft1 变为 confirmed2，勾选消费。
3. 整页刷新后只请求列表/维护状态，不重放确认；重新找到同一记录。选择旧版1显示只读，保存/确认禁用；重新加载最新版后修改人物目标，保存产生 draft3（req32），清空旧确认。
4. 七类及总确认全部勾选、按钮可用后，由隔离驱动明确修改现有原资料。确认 req34 返回409；七类和总确认均清除并禁用，draft3 保留。显式核对 req35 返回200、`source_current=false/can_confirm=false`，不能沿用旧确认。
5. 回到真实主工作台，选择作品后的“核对作者七类事实基线”链接指向正确项目并实际进入页面；重新进入只有 GET 列表和维护状态200，列表仍显示 draft3，无自动保存/确认。

控制台：正常创建/确认、刷新、主工作台入口及最后重新进入均无 warn/error；负向演练仅预期409资源错误。第一次失败构建有对应400资源错误，未抹除。

网络：检查上述请求正文及响应，核对正确项目、原修订/hash、七类明确确认、报告原文、新修订和失效结果；证据保存时剔除全部请求/响应头，避免会话 Cookie 写入文件。

视觉：原生截图确认表单、数值、逐类确认和最终操作按钮可读。未将截图工具受限路径改写为其他文件系统位置。

限制：`upload_file` 对项目 `.tmp` 报告文件返回“不在工具配置的 workspace roots”，所以文件选择器真实上传仍未验证；没有换路径或其他工具绕过。已验证从已保存审核读取完整原报告及实际浏览器原文传递；服务端原文解析另有集成检查。

隔离审计：原 88423 observed exit0，书内全部文件 SHA 不变、除作者审核行外整库 SQL 不变、原登记不变、软件不变、无 worker、外部调用0；实际保留 `[draft1]` 与 `[draft1,confirmed2,draft3]` 两条链。完整 stat 清单不相等，因为负向演练修改并恢复原资料；仅声称文件字节一致。第一次驱动19915也 exit0，但只证明隔离，不代表其失败的浏览器新建流程通过；其清理无条件重写原资料导致 stat 差异，当时尚未执行来源负向探针。

证据：`.tmp/author-baseline-public-progress.json`、`author-baseline-public-{first,second,final}.{log,xml}`、`author-baseline-public-browser-first-http.json`、`author-baseline-public-browser-final-http.json`、两个 browser-after.json。正式新增接受0，历史 T7.1/T7.2 计数不变。

### 下一步与未完成门槛

继续将“当前有效且已确认的作者基线 → 新归档 → 有效认证 → 独立升级预览与确认 → 原正式升级事务/机器登记迁移”接入公开升级流程。不能只修改 onboarding 的 `can_upgrade` 或删除阻断。文件选择器原生上传仍待工具允许访问该文件时完成验证。完整 T7.1、来源缺失/最新用量未知的可写恢复、可信金额、真实收益及 E/F 保持未完成；没有真实 Provider、真实作品写入或外部发送，完整 A–F 目标继续 active。

## 流程顺序

先核对并保存作者基线，再保存整个工作台数据库的备份并认证，最后预览/确认正式升级。保存审核会改变 SQL，因此旧备份必须变为过期，不能继续使用旧整库凭据，也不能把审核文件放到备份范围之外规避该条件。

新增 `workbench/author_baselines.py`，使用既有 WorkbenchDatabase 保存 `author_baseline_revisions`。它是审核草稿/确认的历史，不是第二套权威状态；原书的身份、正文及已审核事实文件不会由此模块修改。

每次修订保留原行，记录前序 SHA，按实际 revision/hash 做并发检查。确认必须逐类覆盖 event、character_state、relationship、roster、location、inventory、counter，且明确作者确认；确认操作不能同时偷偷替换草稿内容。确认后再编辑会产生新的 draft，清除七类确认，旧确认仍作为历史保留。

来源绑定来自实际 TrustedChapterCandidateContext、原身份字节、当前章节 read-set、人工资料摘要及完整原 Strict 校准报告。校准报告必须与实际激活信息一致，不能只提供一个“已校准”布尔值或从身份 hash 补造报告。保存前及 SQL 插入后再查来源；变化时事务回滚。缺章、同章重复文件和不连续正文不允许生成当前基线。

读取历史不会打开历史记录中的路径。`assert_baseline_current` 才判断确认现在是否有效：原资料、正文、身份、作品/物理根、章节或上下文变化后，旧确认保留，但不能继续用于升级。恢复副本保留原 source/read-set，不能静默改写为新根并沿用作者确认。

## 备份证明

`backup_archive_database` 将新表明确列入目标范围，按 id/revision 读取并独立核对全部原修订链。除了 payload hash，还检查实际 SQL 列与记录一致、项目/book_id 联合归属、确认内容等于上一版草稿，以及修订前序/连续性。查询结果在累积前限制单记录、单链、总行数和总字节；不能只在全部加载后再检查上限。

原数据库完整快照和其他项目禁用规则不变。原记录可在来源不可用时离线核验，恢复时保留字节和历史绑定；不因此授予写资格或把 draft/confirmed 行作为正式作品状态。

## 证据与原失败

- 原 93195 exit 0，19 项单测12.60秒，构建48400f41；后续增加边界后需要按最终构建验证。
- 原 20383 exit 1，20通过1失败105.86秒：19 单测及实际归档/认证/恢复正向通过，第一损坏用例被生产代码正确拒绝，但测试期待 ValueError 而不是 ArchivedDatabaseError。仅修测试异常类型；原46860四个负向 exit0/182.41秒。
- 原 84454 exit0，25项数据库备份/恢复既有回归303.15秒，在 c1771324 构建通过；后续变化仅新基线表处理及模块自己的检查，这些旧场景不含该表。
- 原28621 exit1/37.40秒复现真实归属漏检：确认行改挂另一项目、保留原 book_id，原按 project_id 查询会漏掉该行。补联合 SQL 归属后，原16090在a7aa0c4a构建25项全部通过245.76秒，包含实际认证和完整只读恢复。
- 首个“未来章节”夹具在上下文构造阶段已被既有上一章缺失检查拒绝，原日志保留。改为前一章存在但中间缺章的实际样本后，原 chapter-gap-first 真实复现基线未检查整段连续性；现增加与正式升级一致的完整章节检查。

最终构建 `6fbb28a87caf3ceaa08f7998c8926866d743567200427993697af970d83b18f8` 的完整专项原6994已观测 **exit0，26项通过，205.55秒**；原执行结束后指纹复核一致。不同新增26项：20项修订/来源/状态/完整章节单测，1项确认→旧包过期→新包实际认证/来源暂不可用复验/实际恢复保留历史，以及5项归档破坏拒绝。旧批次重复项不再累计。

25项既有数据库备份/恢复检查在前述c1771324构建通过，未冒称它们在最终构建重跑；其场景不含新作者基线表，后续生产变化仅该表处理及新模块的边界。所有本批执行均已终态、无活动测试或服务。日志/XML、源码摘要与原失败记录见 `.tmp/author-baseline-progress.json`。

## 后续接入

当前原语不取代调用者的维护/机器资格边界，尚无公开 route/UI。下一步用维护许可、实际 BookOwnership、作品状态锁、Provider 边界包住当前登记和 Store 的 SQL 事务；不能用要求 SQL 不变的 `backups._source` 包住保存操作。然后提供七类正常表单与原资料核对、过期提示，再绑定新归档、有效认证、原校准报告及单独升级确认。正式提交仍走已有升级事务及机器资格迁移，不另建权威存储。

来源丢失/最新用量未知时的可写恢复、可信金额止损、真实收益和完整 E/F 继续待完成；未执行真实 Provider、真实书写入或远端发送，正式接受数量不增加。


## 2026-11-02：原升级结果查询与重复提交（私有专项已通过）

私有服务新增 `completed_result`：从只读 SQL 连接定位实际项目和作品目录，验证原数据库文件/父路径及作品路径的物理绑定、见证、原已确认修订、产品意图、登记升级意图、真实事务完成和发布凭证，以及已发布准备资料。它不申请写锁、不创建控制文件、不恢复事务；历史完成结果不授予当前写权限。提交先检查同一原预览是否已经完成，全部请求字段及恢复策略摘要相同才能返回原结果；存在未完成或损坏的原意图时拒绝，不能落入第二次升级。

原82222句柄在中断后丢失，日志/XML显示1失败/8未选71.19秒，退出码未观察；失败来自只读连接缺少实际修订链所需sqlite3.Row。修复后原21195 observed exit0，2通过/7未选133.85秒，实际核验1.0/2.0：找回原结果、归档认证缺失时重复提交、错误恢复策略拒绝、已发布准备资料损坏时拒绝且现场不变。当前构建 `6f31b9924ccecccbbd1fd16f3f33dd07124ba8230c0745a657834123edc25a70`，终态后指纹复核相同，当前无活动进程。尚未实现准备阶段及提交标记前后的产品明确续接，也未安装公开升级入口。


## 2026-11-02：中断状态查询及原登记事务续办（验证中）

私有 `operation` 读取实际SQL/原产品意图，区分意图已保存、准备中、已准备、标记前待回退、标记后待发布及已完成。`continue_operation` 要求原维护操作、明确确认、原恢复策略SHA，原BookOwnership、作品state锁、Provider锁和真实SQLite writer fence；实际复验原认证后调用既有原登记事务恢复，不扫描其他事务。当前仅接入已有原登记升级意图的事务；尚未接入准备阶段续办、实际子进程强退/重启验证，不能称为完整恢复能力或公开验收。原90049运行中，日志/XML为 `.tmp/onboarding-upgrade-continuation.log/.xml`，构建d27e6977e800e2ac60411a197871d827a08e349f327353492c9bd47d0e5297c3。生产和测试保持冻结。


续接：原90049句柄丢失，日志/XML为2通过2失败/7未选240.33秒，退出码未观察；普通RuntimeError被事务正常恢复捕获，测试没有模拟强退。改用BaseException后原46344 observed exit0，2通过/9未选139.28秒，生产构建bc2cea9fa61cf70cafd646912417c4e10e44c544f5b01f12b2c2199bb10f82b8复核相同。这仅证明异常绕过普通恢复后的服务续办。当前原6365实际子进程退出检查运行：实际产品准备原事务，独立进程在after_apply_target/after_commit_marker用os._exit(92)，父服务续办；日志/XML `.tmp/onboarding-upgrade-process-exit.log/.xml`。工作台整体重启及准备阶段恢复仍未验证/完成。


实际子进程检查终态：原6365 observed exit0，2通过/11未选126.55秒，生产指纹bc2cea9fa61cf70cafd646912417c4e10e44c544f5b01f12b2c2199bb10f82b8相同。独立子进程确实在产品原准备事务的apply/commit marker位置退出92，父服务原操作续办分别回退/完成；读取状态和错误确认不变更现场，SQL逻辑不变，登记回到active且generation为1/2。当前无活动进程。这不是工作台整体重启证明；准备阶段恢复、整个服务提交进程强退/重启和其余负向验收仍待完成，公开升级仍未安装。


准备阶段续办实现新增：原意图/不完整staging/完整staging/准备完成journal均绑定同一run与preparation；创建新暂存前核对归档整库/config和原read_set。原27485 observed exit1，1失败/13未选64.71秒：缺少借用原锁helper的局部import，核对后、重新暂存前NameError；修正后原5251运行，构建daed8c5351ecbe863e7b9f1c4bbd91f7648918235de595a9f86a3790c934efb6，.tmp/onboarding-upgrade-preparation-second.log/.xml。生产/测试冻结；4个中断位置并保留其他run目录检查。暂未通过完整准备阶段/真实准备子进程退出/整体工作台重启验收。


准备专项终态：原5251 observed exit1/1失败62.95秒，未知staging在首次root registry前出现，原引擎正确阻止登记；生产未放宽。intent-only测试改为其他根事务保留，其余三个已登记根保留同根其他run。原83814 observed exit0，4通过/13未选249.55秒，构建daed8c5351ecbe863e7b9f1c4bbd91f7648918235de595a9f86a3790c934efb6复核相同，当前无活动进程。仅证明BaseException模拟准备中断，真实准备进程强退、整体重启及缺损证明拒绝还待做，公开升级不开放。


准备目录凭证改进（验证中）：原机器产品意图另有oup进度凭证，在首次意图后故障点前创建；每次临时目录真实创建后、调用测试故障前记录device/inode。查询与续办核对这些原目录仍在原staging、journal或精确保留abandoned位置，不能因目录消失重新暂存。原73564六项检查运行，构建7c5e4c094cdfe3f5f7aaa4bfeb3d4b845b877b51ffedaa736cff3f9475d3d6d7，日志/XML `.tmp/onboarding-upgrade-proof-loss.log/.xml`。尚未验收；整体重启和真实准备强退仍待做。


目录丢失检查终态：原73564 observed exit0，6通过/13未选364.81秒，构建7c5e4c094cdfe3f5f7aaa4bfeb3d4b845b877b51ffedaa736cff3f9475d3d6d7终态复核相同；四个准备路径继续正常，移走原暂存/日志目录或机器进度凭证均拒绝且无书/机器/SQL写。当前无活动进程。尚须记录完整暂存manifest的已完成/内容证明，防止删掉目录内manifest被当成未完成而重建；还需未知未登记staging负向、实际服务强退/工作台冷启动与耦合回归。此处不表示正式升级产品或D阶段已验收。


完整准备清单专项：原69105 observed exit0，7通过/13未选419.19秒，构建06db740c66fa19d0b60f58072580a81202a0b89306f643f9ecee308d5a67a413复核相同；完整staging/journal的manifest缺失/损坏拒绝且无现场写，4个正常准备续办仍通过。当前原84557真实服务子进程退出及新工作台冷启动检查运行，日志/XML `.tmp/onboarding-upgrade-cold-restart.log/.xml`，先显式接管原维护再续办；尚无结果，生产/测试保持冻结。


实际服务进程强退和新实例冷启动检查终态：原84557 observed exit0，2通过/20未选133.63秒，构建06db740c66fa19d0b60f58072580a81202a0b89306f643f9ecee308d5a67a413复核相同。独立子工作台create_app后显式resume原维护，再经真实service.preview/commit于before_journal_publish或after_commit_marker退出92；父端新工作台启动后先只读operation、确认SQL/书不变，未接管维护的continue正确拒绝，显式resume同一op后continue完成原run。SDK/worker调用禁止。当前无活动进程。这次覆盖完整服务提交及工作台新实例恢复，区别于先前只在core事务子进程强退。还需未知未登记staging/缺失root registry提前拒绝、耦合回归、公开API/UI和原生浏览器；完整T7.1/D–F未验收。


## 2026-11-02：公开格式升级API已接入，确认页面待做

`/api/projects/{project_id}/onboarding/upgrade` 下提供 POST preview/commit/continue 和 GET operations/{preview_sha256}。服务仅接收作品/原审核/归档/修订摘要及明确确认；调用原维护run_capture_async保持后台完整生命周期，GET在线程池只读原证明，不推进事务。已将路由文件加入认证writer覆盖清单。原77385 observed exit0，2项118.62秒，构建3f290a5a8a0236bc8615c15ecf956fd1d2e0a295702425df624c2fed814fbfe1终态复核相同。实际HTTP正常升级、重复提交同结果、刷新只读查询、prepared原意图明确续办、严格布尔与字段拒绝、损坏manifest 409及现场/SQL保留均通过。当前无活动进程。页面未接入，完整T7.1/D–F未验收。

### 浏览器验证记录

Status: partial（仅实际HTTP专项通过，浏览器尚未开始）。

Changed behavior tested: 真实路由预览、明确提交、只读结果查询与原操作续办。

Browser environment: 本轮无浏览器页面/服务器；工具发现未提供Chrome DevTools，IAB工具可用，后续须按control-in-app-browser技能启动。

Actions performed: 两个隔离TestClient实际HTTP工作流；未执行浏览器交互。

Observed UI result: 未验证，作者确认页面尚未开发。

Console/runtime check: 未验证浏览器控制台。

Network check: HTTP测试已核验200/409及原结果字段；未检查浏览器网络。

Additional checks / Evidence: `.tmp/onboarding-upgrade-public-api.log/.xml`，原77385 observed exit0，2通过118.62秒；当前构建同上。

Remaining risks / 后续浏览器检查：选择已确认基线及新认证归档，预览差异/数量/可读恢复前提，双项明确确认提交；刷新只发GET找回原操作；错误/改选/维护/刷新清除确认；未显式接管原维护不能续办；console无新错误且network请求/状态/响应与界面一致。尚无证据证明这些UI要求已完成。


## 2026-11-03：公开升级确认页面功能验证

`/upgrade?project_id=…`已接通作者审核页的所选作品。页面读取已确认最新审核与归档列表，实际预览展示七类数量、升级差异及可读恢复前提；双项明确确认后提交。预览引用提交前写入URL，刷新仅GET查询原操作，pending必须明确继续；改选、维护刷新、错误和页面重载清除确认，完成后禁用新升级预览。没有自动模型调用或章节发布授权。

### Browser verification result

Status: passed（功能交互、console与network）；截图视觉QA未完成。完整T7.1/D–F仍未验收。

Changed behavior tested: 作者审核链接保留所选作品、真实预览/升级、原prepared事务明确续办、错误清除确认及刷新只读原结果。

Browser environment: Chrome DevTools；隔离正常样本127.0.0.1:63606、续办样本127.0.0.1:63256；仅合成作品/校准及假调用保护，真实SDK与worker禁止。当前服务器均已退出，无监听。

Actions performed:
1. 正常样本原生点击审核页升级链接，选择审核/认证归档，核对七类数量及恢复前提。分别改选归档和刷新维护，双确认清空且提交禁用。
2. 真实移走隔离认证文件后点击提交，409且清空确认；还原原文件、重新预览和双确认后commit200，GET原操作completed200。
3. 完成后刷新页面只发GET（原reqid84/85/87/88均200），确认清空且新升级禁用；不重放POST。
4. 原prepared样本先双确认，再刷新，仍prepared且清空确认，只发GET；再次双确认POST continue200，GET completed200。

Observed UI result: 正常/续办均显示“原升级已完成。后续章节需要重新准备。”；完成后确认和升级按钮禁用。未知/缺失认证不授予写权限。

Console/runtime check: 正常样本只有人为缺认证409对应的资源错误；刷新后console干净。prepared样本console干净，无未捕获异常。

Network check: 正常预览req70/71/73/75为200；故意缺认证commit74为409；明确commit76为200、原结果77为200；刷新原结果88为200且无POST。prepared刷新原结果48为200；明确continue49为200；原结果50为200。服务器保留脱敏response字段，无请求headers或session cookie。

Additional checks: 最终构建`4d4f5a800ebba49130f0e8686b5b2cde551a9903737668a1432efa77c410dd6c`，原34684 observed exit0，2项HTTP回归125.24秒；node --check通过。原98671（续办）与原23419（正常）均observed exit0，最终软件指纹相同。

Evidence: `.tmp/ub-50244f0c/http.json`、`audit.json`及`.tmp/upgrade-browser-prepared.log`；`.tmp/ub-04ebc3e5/http.json`、`audit.json`及`.tmp/upgrade-browser-final.log`。两份审计均证明原文件（除预期身份升级）/SQL/登记编号/软件保留、generation2、worker不存在且调用0。首次normal78185因夹具TestClient和Uvicorn双lifespan关闭lease而exit1，审计与日志保留；夹具改为单生命周期后两最终样本正常退出，未因夹具错误修改生产生命周期。

Remaining risks: 截图及关闭page2的自动审批因409 thin_wrong_thread_or_session无法完成，动作未执行，未用其他工具绕过；因此无截图视觉QA，page2仍指向已停止的服务器。此为审批会话故障，不是安全性判定。公开组件通过不代表来源丢失/未知最新用量的可写恢复、完整T7.1或发布验收完成。正式新增接受计数保持0，真实作者作品/付费Provider写入与调用均0。


## 2026-11-03：公开升级与升级前后认证包实际恢复

原61360 observed exit0，2项314.20秒，当前构建19eadaec9dfdcc19eccabb4c86d99b1e4ee897664bb59efe6b2a9daae273ffa0：公开升级后原认证包可恢复旧格式；公开再归档/认证的新包可恢复审核格式。两条均实际复制/绑定整库/机器交接/写启用，多次新工作台读回原确认审核和作者草稿，源书/SQL/包不变，历史确认未重新绑定为当前有效确认。

本轮发现并修正真实跨流程缺陷：后续恢复交接推进/迁移所有权后，原升级GET不能再要求owner等于旧active_record而误显示等待发布。只有原升级自己的pending fence仍需显式续办，原completed+done历史证明不被后续交接抹去；写权限校验不变。

Browser verification result: passed（原历史状态查询功能）。原42467 observed exit0；Chrome访问真实交接后原工作台升级链接，GET23/200显示completed、升级/续办禁用、console干净、无POST。`.tmp/ub-83070031/http.json`/`audit.json`记录原书/SQL/登记编号/软件保留、generation4、SDK/worker0。首次61407历史状态bug失败及2133夹具代次错误失败均保留。截图限制延续前次记录，未重试或绕过。当前无活动进程，完整T7.1/D–F尚未全部验收。
