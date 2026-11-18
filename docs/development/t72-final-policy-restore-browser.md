# 最终连续写作流程恢复：原生浏览器验收

## 2026-11-12 c097新构建：核心恢复续写浏览器通过

**最终结果：上述原生主流程passed，完整T7.2/T7.3及目标仍未整体验收。** 第四章已正式完成，最终刷新GET列表和原批次均200，显示“已完成 · 第4章 · 核对正式正文和事实”，刷新Console无error/warn。独立final读回正式committed_through=4、源预算2/1/0、目标2/2/2、作者稿r3/SHA不变、源作品/SQL/归档/原grant及control保持。原始归档预检仅新增三个一字节`.persistence.lock`，未删除或改变任何原文件；该差异保留，未把归档前全清单相等写成true。父27116退出0，真实CLI子进程退出0、worker不存在、SDK调用为空；两个私有服务均停止。最终结果`.tmp/resume-20261112-v4-browser-result.json`，原生完成/刷新证据`-v4-native-complete.json`，最终读回`-v4-final-readback.json`。

环境为Windows/Chrome DevTools MCP、独立合成作品、源码`c0972a5fd44f7b823ac7e956f6bbe21845e6cae9995204fc4b876f4766eb0259`。新v4从源59221完成原生归档、同机同版本认证、恢复副本、写资格交接、保留用量/预算/历史任务、新执行目录及明确启用；实际CLI目标61293。独立activated及continued读回证明源作品/逻辑SQL/归档/原grant和control均不变，目标预算仍2/1/0，第三章原r2稿逐字相同，恢复与续接未自动调用。

在目标原生页面明确接管第三章，保存r3/SHA2332f8630fb88bb6423df421c2409ba22b37839f614e3b3daab70415aeb1cc44；提取确认取消时无POST，重新明确确认后真实应用提取入口返回200，预算仅至2次，审核两项事实、保存及确认均200。发布预览的正文完整性/事实/叙事/检查项均通过；勾选发布后POST200，commit_e07ec9891f2824fb965bfdf870268748，刷新publication-status200且相同凭证读回verified=true。

发布后返回原批次GET200，显示author_review及完成作者本章按钮；明确complete-author200，推进至第四章generation并暂停；再次明确resume200，后台执行中。最初在发布页刷新与新批次页同时核对时，列表409 `restore_scope_busy`，页面显示重试提示；点击刷新再选原批次成功。该瞬时资格锁及其资源错误单独保留，不隐去，也不混称此前`reviewed_next_chapter_mismatch`。第四章运行期间状态读取也可能暂忙，未重复调度模型。

发布页Console无error/warn；批次页Console的资源409对应上述已记录资格锁，没有观察到JS异常。证据`.tmp/resume-20261112-v4-native-{state,entry,archive-and-negative,handoff-usage-budget,author-extraction-review,publication-and-resume}.json`，独立核对`-v4-{activated,continued,final}-readback.json`。专项67项exit0与原生流程证据分别保留。新构建未再次原生操作损坏归档，旧085a的损坏元数据拒绝证据保持其原范围；当前完整回归及发布/真实模型验证仍待验，不提前宣布稳定版。

前一5fe构建v3演练已实际完成第三章发布和刷新，但批次GET409 `reviewed_next_chapter_mismatch`阻断，证据`-v3-native-post-commit-failure.json`，终态committed3/预算2-2-0。根因是已提交章节的旧审核新鲜度检查构建next上下文时异常未转换为stale。修复统一恢复来源错误为ChapterFactReviewConflict并在新鲜度检查范围捕获，补正式提交后GET及旧审核stored_state=confirmed/state=stale、禁止旧提取重发的断言。该失败演练保持失败。v3父清理重复lifespan退出1记录保留；v4使用新app单lifespan避免演练助手问题。

## 085a历史演练（失败，以下不代表新构建当前状态）

状态：进行中，未验收完整T7.2。2026-11-12，Windows/Chrome DevTools MCP，固定源码085a92a54177e9e8839f3b9fcbeb30040ebef34c4057266387f7fbacd047b984。

本轮最终判定：**发现失败，未通过完整恢复续写**。归档/启用/零自动调用/原稿预算保留/明确续接/接管已实际通过，作者保存r3后从原生提取按钮确认，POST fact-extractions返回409 `chapter_fact_authority_changed`，没有发起替身或真实模型调用，预算仍2/1/0。原因：作者审核已支持经确认的恢复来源桥接，但提取入口、提取结果导入和新鲜度判断仍比较旧物理上下文。旧测试直接创建审核行，漏测真实提取入口。本轮失败证据`-native-extraction-failure.json`保留，禁止把后续修复或旧自动测试结果改写为本轮通过。

旧源/目标服务均已正常停止，44855退出0、真实CLI子进程退出0、worker_absent=true、SDK calls=[]，源作品/SQL/预算全程不变、原归档不变。停止读回`-reopen-v2-after.json`及`-child-v2-stop.after.json`。当前生产修复已开始，需在新指纹下重新验证，旧认证/旧批次不能用新源码重放。另发现概览自动入口hidden/href=#，已单独记录并修复候选；完整发布回归在固定源下24模块286节点终态无失败后主动结束，非完整4311通过。

本次使用最终审核策略链路的隔离合成作品：第二章已正式提交，第三章生成后暂停，保留作者未审核修改的r2草稿及其SHA；第四章尚未调用。原章预算调用为2/1/0。只使用替身执行器，真实SDK及外网被拒绝，不能作为真实模型效果或收益证据。

原生页面已完成归档范围预览、保存诊断归档、核对和签发同机同版本有条件认证。认证响应明确recoverable=true、write_activation_allowed=false、automatic_provider_calls_allowed=false。已进入恢复维护，核对目标、建立会话，完成复制和目录绑定，正在准备只读工作台。尚未完成交接、启用、作者接管及明确续写。

浏览器环境及续接状态：`.tmp/resume-20261112-final-restore-native-state.json`；当前源服务44855/PID13820，URL见reopen-v2-state.json。恢复会话restore_fd62ffaeb9b148ed9da7d595d63d7d63，目标`.tmp/resume-20261112 恢复副本`。操作前须核对实际页面/会话状态，不重复已完成步骤。

负向检查：目标在工作台数据目录中时，POST preview返回409，页面保留输入并阻止建立会话。首次演练脚本只改备份服务目录，恢复服务仍使用初始化的旧目录，因此合法归档被提示找不到完成凭据；这是隔离脚本配置遗漏，记录保留，v2在启动前同步两服务目录后，同归档preview返回200。未修改生产程序或测试。正常源码配置不受该演练设置影响。

维护服务重启后页面显示“上次维护尚未结束”，必须点击继续原维护；没有自动恢复操作。源作品与原SQL核对中，只有归档预检新增三个一字节持久化锁文件，原有文件无变更、SQL逻辑摘要不变，精确差异`.tmp/resume-20261112-restore-preservation-before-session.json`。归档签发后清单`.tmp/resume-20261112-final-restore-archive-seal.json`及恢复交接前源清单`-pre-handoff-source.json`已保存，用于最终核对，不将早期笼统source_book_unchanged=false隐去。

Console：当前v2页面没有error/warn。Network：maintenance resume、preview、create、materialize和bind_roots均200；每步仍需读回终态，不能仅依据点击成功。后续须保存剩余步骤、作者稿/预算/提交凭据读回及Console/Network证据，并补损坏归档拒绝。当前状态partial，不代替完整回归、Windows发布或T8验收。

证据：`.tmp/resume-20261112-final-restore-native-*`、`-reopen*.log`及`-after.json`。原失败过程及当前合法过程分开保存。

补充负向结果：将合法归档复制到独立命名的临时目录，仅截断该副本的manifest JSON，原归档五文件哈希全部保持。原生选择损坏副本并预检，返回409，页面保留输入、禁用建立会话；目标目录没有创建。随后从原会话下拉框选回只读就绪的合法副本，继续原步骤。证据`-native-damaged-rejection.json`及`-damaged-clone.json`。本例验证损坏元数据，未声称单独覆盖所有ZIP成员损坏。Console仅有这次预期409资源错误，无JS异常；与先前正常页面无error/warn分开记录。

只读核对：原生选择正式第二章并查看内容，POST readback为200，响应与快照保存于`-native-steps-before-handoff.json`及`-readonly-snapshot.json`。此入口当前列出正式章节和旧类型作者稿；最终流程的CandidateDraft保留仍须在启用后的章节/接管页面核对，未把下拉列表未列出的稿件当作丢失或已验收。

后续：原生核对交接、勾选确认交接、保留最新消费、保留原预算及隔离历史任务均返回200，usage_settled=true、blockers为空、automatic_provider_calls_allowed=false。第三章CandidateDraft在恢复SQL中只读查询，正文逐字一致，仍为r2/SHA d4e65f274e84。尚未启用、尚未执行第三章提取/提交或第四章续写。响应证据`-native-handoff-and-usage.json`、`-native-budget-and-jobs.json`。
