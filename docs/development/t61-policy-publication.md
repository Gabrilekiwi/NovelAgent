# T6.1：拥有者边界内的真实 policy 发布

2026-11-06，发布加固与三章验证中。原正式 readback 和后续章生成门禁已有内部证据；完整三章链路、自动调度、一次修复和接管尚未验收。所有调用/作品为隔离合成数据，没有实际 Provider、账户或真实作品写入。

## 当前实现

`workbench/reviewed_policy_publication.py` 要求原质量 typed 结果及控制链 finish 证明。进入原 publication local-only scope 后，从实际原稿和原 policy review 重建同一事实准备，复用 `ChapterPublicationService._publish_new`、原请求/Intent、persistence-v2、正式 receipt 和 SQLite 可重建索引。内部 prepare 适配与按次传入的 persistence_fault 回调保留原人工默认行为；原人工入口保留默认作者准备行为，没有把自动调用伪装为作者确认。

正式写入前继续检查完整原输入。事务自身开始修改正式文件后，apply/marker 前只执行原活跃控制许可的撤销和主动时间检查，允许的文件变化仍由原 typed transaction/read-set 校验。它不允许因 source drift 关闭全部来源门禁，也不创建第二种持久化方案。

正式提交后，原章节上下文已被合法改变。`ReviewedBatchService._require_current_execution` 仅对 publication/readback 阶段，使用 `read_verified_commit_requests` 的实际原请求、preparation、正文、当前正式 tip 证明替代初始上下文条件；冻结人工来源、全部章纲、模型、策略和软件仍逐项校验。没有真实原提交时仍回到原严格开始检查。后续章要求本批次所有前章实际提交、readback 和控制 finish 证明，并核对当前新上下文。原控制命令双副本中断时，parent 证明可核验 machine 原链；实际 step/dispatch 仍需双副本一致。

`resolve` 是明确原事务恢复，不授予 handle、不发调用、不重新准备已中断章节。标记前回退保留原 unpublished 请求及“新键需明确处理”状态；标记后完成原事务并重建原索引。已有正式结果的明确 publish 续办只核对原凭证、补原阶段结果/进度；读取或构造冷服务不会自动继续。

## 验证与接续

首轮原 64264 明确 exit 0，41 passed / 1154.95 秒，`.tmp/t61-policy-publication-first.log/.xml/events/source`，构建 5d334135023460d18119d9d1d79704a46afe08db14afbef3fa8e4a76fa3fd602。包括 5 个实际 policy 发布场景（真实首章提交及冷索引、作者改稿、三处真实 os._exit88），17 个原人工发布服务回归、19 个原批次控制回归。

真实正式正文/状态原子提交、policy history 来源、SQL 作者 review 为 0、原稿不变、冷索引/原结果重复查询、合成生成/提取各 1 次均通过。提交标记前真实强退恢复回滚，原 unpublished 记录保留且不自动重试；标记后和文件完成/SQL 未提交两处强退均恢复同一原正式记录，明确继续仅补发布进度。

## 独立 readback 与后续章门禁

新增 `workbench/reviewed_policy_readback.py` 进入独立本地 readback scope，实际核对发布阶段完成证明、原正式 receipt/history、正式正文 SHA、当前完整状态 SHA，并构造下一章真实可信上下文。保存 typed readback 后，主动时间已解释且未超限才推进下一章；unknown 时间只保留核验结果并暂停。没有新模型调用或正式写入。

下一章自动服务改用阶段/章号适配的 grant 检查。首章仍保留原初始上下文；后续章要求所有前序本批次 readback 与控制 finish、实际原提交历史，以及即时下一章上下文等于前章实际核验结果。不是仅按控制日志章号或书目录里的文件存在就允许付费。当前完整跨章闭环尚未验收，不把下一章生成通过替代三章连续提交。

原 96240，`.tmp/t61-policy-publication-readback.log/.xml/events/source`，构建 5759e774a56d3e771b874cc828a75a7f9d8892255339001e6dc8e241f459cf4d，9 项全部通过，原 96240 明确 exit 0 / 963.47 秒：7 个发布/readback场景，另有真实批次质量和完整提取衔接。新增实际下一章生成检查和缺 readback 零调用、正式正文损坏拒绝。两轮全部 phase/collected/sessionfinish/XML 与最终构建生产/测试 SHA 在 `.tmp/t61-policy-publication-readback-audit.json` 已复核。45 个不同节点仅不同构建历史索引，第二构建实际重跑 9 项。两原测试均已退出，无活动服务。随后用户明确要求将 Claude 模型切换为 claude-opus-5-5，已改本机 .env、示例配置及对应检查，16 项配置检查通过，无实际 Provider 调用；上列发布证据保留为配置修改前验证记录。完整三章、readback 冷恢复、共享服务局部回调和历史只读解析边界仍待补齐，不能称完整 T6 完成。

## 后续

接入独立 readback 步骤：必须实际重读正式正文、完整 receipt/history 和提交后状态，核对同一发布结果，才能推进下一章。随后需要从前章 readback 证明导出新权威上下文，不能沿用原首章上下文或仅信任控制日志章号。完成原单次修复全提取重检、作者接管/明确继续、三章和失败后零下一章、最终流程恢复及 Windows 发布；D 真实收益与 F 验收仍保留原独立授权条件。

## 当前加固验证

发布回调按次传递，避免修改共享服务属性；历史 resolve 通过完整原 receipt 找回已提交章节，允许当前 tip 或模型配置已改变，但不授予执行许可。readback 最后完成写前再次核对原主动时间。

首轮原34422明确exit1，1项失败/107.76秒：新增测试用原草稿字节比对规范化正式正文，末尾换行不同。只将断言修正为质量准备中的 final_prose；生产源码不变，保留失败日志/XML。第二轮原25611正在运行8项，证据 `.tmp/t61-policy-publication-hardening-second.log/.xml`、同前缀 events/source；包含真实 readback os_exit89、下一章 torn 原命令续办、最终时间门禁、三章完整提交，以及共享回调/模型变化历史查回和原发布强退回归。以终态为准，不将运行中部分结果作为验收。

第三轮原6947已明确exit0：8 passed /1237.47秒。完整2–4章依次实际生成/提取/最终质量/原事务提交/readback，一次start、无中途resume，下一章模型输入匹配前章实际新context/state/document，每章counter+2累计到+6，三个policy receipt可历史找回，SQL作者review0。合成模型调用生成/提取各3。另原10524明确exit0，人工发布服务17 passed /287.57秒。两个独立audit核对所有节点setup/call/teardown、XML、sessionfinish、完整生产及test哈希，均f60c3d...；8+17为同生产构建两轮检查，不是完整项目验收。强退readback89/原字节复用、后续章torn原resume、readback最终时间门禁、历史model变化查回但resume拒绝、按次callback不改shared属性和三处原发布强退通过。后续原publication/readback finish双副本中断恢复、marker暂停边界仍待补；调度/UI/修复/接管和完整T6未完成。

## 最新组件验收（2026-11-06）

原75001明确exit0，最终6 passed /1093.10秒，生产11c57c39499d6ff5496af4be3673af5b773ed71b5bbf9ef53f5b4ba84c767d69。`.tmp/t61-policy-publication-finish-final-audit.json`已核对每个node的setup/call/teardown、XML、sessionfinish、完整生产指纹和两个测试文件SHA。新get读取typed结果不授许可；recover_finish只为已经写入machine的同一原finish补book镜像，核对真实质量/原receipt/正式正文与状态/原pending，不能从结果文件新增完成命令。真实os_exit90在publication和readback两个完成命令双副本之间退出后恢复成功；重复恢复原字节不变，无handle、下一章零调用。缺原命令和有效外层hash下的bool SHA拒绝。before-marker暂停回滚，after-marker暂停保留已提交原结果，两者都停止后续推进/保留稿件。最终时间门禁和2–4章完整流程在当前构建重跑通过。

原25976的2pass1fail328.29秒保留：pause测试错误解包返回dict；原事务manifest记录ValueError并回滚，修正测试取dict后最终两边界通过，未改生产门禁。前f60c3d的8+17历史检查不能算作当前构建全部重跑。

此处仅验收内部发布/readback组件。没有公开批次route/UI或持续调度器；一次修复完整执行/全量重提取重检、原位作者接管、最终新流程恢复与Windows发布、独立真实收益/生产验收尚未完成，完整T6/D/E/F保持未完成。

## 有限调度器内部验证（2026-11-06）

`workbench/reviewed_batch_driver.py`通过显式原handle/CAS顺序调用真实五阶段服务，每步重读原进度及开始/继续命令SHA，按原章数×5有界执行，不从构造/get/重启开始工作。共享control服务mutex阻止重复driver；异常只撤销原运行，不能撤销之后的新resume，即便同服务沿用epoch。启动冻结来源变化在零调用时转为暂停；过期CAS不撤销原许可。

首28311 exit1/1pass1fail104.68秒为测试异常类型预期错误，实际未知调用已正确保存provider_uncertain；修正精确异常/状态后，原86443 exit0/22项965.83秒（3个driver+19原控制回归）并审计。随后只改driver入口暂停与原命令SHA保护，最终原49049 exit0/7项883.73秒，生产70ad7b653db868e329be5a4a6db857b9d1f596376c2065aa67463e5a90ad6adc。`.tmp/t61-batch-driver-final-audit.json`核对全节点phase、XML、sessionfinish、完整生产/test hash；22项不是最终构建全部重跑。最终含真正driver一次start连续三章十五阶段/state+6/前章readback门禁、重复调度与pause/明确resume不重发、未知物理调用不重发、来源变化零调用、旧driver不继续或撤销新resume，以及真实os_exit91后冷get零执行、明确resume从extraction完成正式readback而不重生成/不调用下一章。

当前无活动测试/服务。调度器是同步内部组件，尚无公开batch jobs/API/UI；完整一次修复/全量重提取重检、原位人工接管、全阶段用户恢复和最终发布/真实验证仍待，完整T6/D/E/F未完成。

## T6.2 完整单次修复内部组件（2026-11-06）

原首次完整质量仅有具体原文重复证据并投影为repair_once时，原拥有者服务再次核对完整Candidate/draft/模型提取/质量输入，在原chapter ledger持久消耗一次资格，再记录begin_repair控制命令。修复使用repair_generation、repair_extraction、repair_quality三个独立阶段及各自不可覆盖结果；另一Candidate/draft保存完整修复正文，初稿、原提取和失败报告保持原字节。policy生成/提取v2绑定原claim及失败来源，不伪造author_confirmed。修复后完整模型重提取与所有最终检查，通过后复用同一正式事务/readback再构造下一章；失败暂停，下一章零调用。原chapter/batch费用、调用、token、主动时间门禁继续共用，driver最多8阶段/章、一次修复分支。原独立quality入口默认仍暂停，只有原有效批次driver启用修复分支。

历史原claim重建使用冻结action_policy快照，独立反例保留.tmp/t62-repair-history-policy-before.json。recover_begin仅核对已记录原begin的前序pending、失败完整输入和账本claim后补原副本，不从claim-only造命令，不创建handle或清原未知时间。repair结果完整保存/完成前os_exit93可明确继续复用原稿不重发；消耗或begin写入期间os_exit94保留原time字节unknown，即便明确resume也不能新增付费调用。作者在消耗前/后改稿均保留并阻止自动修复，资格已消耗时不退回。未知repair调用保留provider_uncertain与原资格，冷resume不重发。缺claim/失败报告/原提取结果时阻断且不重建。

证据按构建保留：原57538 exit0/24项1232.16秒（d140f033，初始成功/失败/改稿、原ledger/core、正常driver三章）；原26763 exit0/28项1400.76秒（d37ff7b1，真实exit93、call budget2、postclaim改稿、unknown修复、冻结策略历史及初检/失败/default质量回归）；原24203 exit0/4项784.11秒（1fced95c，三处exit94和完整修复成功）；原93885 exit1/1pass1fail182.97秒为缺原质量结果泄漏ReviewedBatchError，原调用已被阻断，修正为BatchControlError保留cause；最终原19224 exit0/7项1013.39秒，生产5b1d2a1225599282a7e3b2d83130cbfb2bccb61e3ef75602973c33a86bb5d64f，缺证据3、exit94三边界和完整修复成功/冻结历史均重跑通过。每轮成功时已核对phase/node/XML/sessionfinish/生产及测试SHA；最终audit仍与当前源/test一致。`.tmp/t62-full-repair-audit.json`是36个不同历史节点索引，最终实际重跑7个，不能合称最终36全通过。

当前无活动测试/服务；所有作品、调用、费用为合成隔离演练，无真实Provider/真实作品写入。内部一次修复组件已验证，完整T6/T6.3和阶段D/E/F仍未验收。下一步原位人工接管/明确继续，随后公开batch tasks/API/UI和全阶段typed恢复、最终新流程恢复/Windows/独立真实验证。

## T6.3 原位作者接管内部组件（2026-11-06）

`workbench/reviewed_batch_takeovers.py`仅在原批次暂停、当前章未正式提交时，明确选定本批次实际生成的初稿或修复稿，保存原控制/授权/Candidate/draft/context快照并记录author_takeover，撤销自动handle。author_review阶段拒绝普通resume跳过作者处理。作者通过既有真实fact create/update/confirm及ChapterPublicationService.commit提交；complete核验实际不可变author request/receipt/preparation及选定Candidate/draft/pre-state，实际prose/state/下一章context，保存独立author readback和finish_author_chapter，下一章仍暂停，必须再次明确resume。不会将人工来源伪造成policy。

get只读原快照与副本状态，旧takeover/complete请求仅按同一原key/前序CAS核验原快照/凭证/回读后补副本或找回结果，不打开新计时/handle，也不删除后来resume的许可。真实os_exit95覆盖接管snapshot/机器命令/作品命令、作者readback/机器finish/作品finish六边界；原未知主动时间字节保持。即使author finish可补镜像，后续章仍核对所有前章主动时间可解释且未超限，不能借新章预算绕过未知旧计时。snapshot-only无接管命令时原控制可明确resume，但实际执行仍在时间门禁前阻断，零新模型调用。

最终原84703 exit0/14项1240.63秒，生产d5e714923131b15cb9f2b49ce9688a3d9d9228bc3735d63b6619468479e2bd37；.tmp/t63-author-takeover-final-audit.json核对全部phase/node/XML/sessionfinish/当前生产及测试SHA。包括六真实强退、原稿作者编辑真实author提交/回读/明确下一policy章、旧请求重复保留新handle/进度/作者修改、确认/pause/CAS、实际失败repair稿接管保留初稿/原claim并续写、末章完成不启新章、四项实际成功author提交后的负向（错请求/缺原凭证/正文损坏/其他Candidate不能替代）。同构建先前原10693 exit0/6项642.00秒也已审计，但已被最终14实际覆盖。

历史原48891 exit0/20项520.11秒含原人工发布17/core1及初版主流程；原35033 exit0/8项612.82秒在71a82c构建覆盖恢复及重试。首66892 exit1为夹具误调用publish（正确commit）；原38660 exit1为测试误期待snapshot-only控制resume就拒绝，修正为实际未知时间执行门禁；原31050 exit1为测试误期待ValueError（实际BatchControlError），随后明确成功author提交断言并统一缺失/损坏证据领域错误包装。所有失败保留，未放宽门禁。`.tmp/t63-author-takeover-audit.json`是32个不同历史节点索引，最终实际重跑14，不能把历史32当新构建全通过。

当前无活动测试/服务。组件验证仍为隔离合成书/假Provider，无真实付费/真实书写入。完整T6.3/E未完成：公开batch tasks/API/UI/浏览器、全阶段typed claim/finish恢复（尤其generation/extraction及repair变体）、最终新流程恢复/Windows和独立真实验证仍待。

## T6.3 全阶段原记录恢复内部验证（2026-11-06）

最新接续（2026-11-06，全阶段原记录恢复内部验证完成）：原9260明确exit0/4项1607.18秒；原77884句柄已失效但完整log/XML/events明确sessionfinish0/六项799.90秒。两组同一生产46656a62b283f40a07f0bcd8e793968e2fd4aab175852b0d82644fd868278bb1，审计逐节点setup/call/teardown、XML、sessionfinish、生产与test哈希一致。最终三矩阵实际文件独立核验：八claim exit96、四模型finish exit97和repair_quality finish exit97，共13处真实强退，三本合成书均正式提交2/counter2/原repair claim1/charged calls4/两生成两提取。另六作者编辑finish真实强退可补原副本、保留修改稿，正常执行仍拒绝未检查新稿且零新调用。正常直接三章全部提交并readback。证据.tmp/t63-stage-recovery-progress.json及各final/after-audit，原8058复现失败保留。
完整T6.3/E尚未完成；后台任务、公开API/UI/browser及最终恢复/发布/真实验证仍待。

## T6.3 公开批次与后台任务（2026-11-07，未完整验收）

最新接续（2026-11-07，公开批次API/页面集成，浏览器部分通过）：原44799明确exit0/5项385.14秒，生产cfe886cb5f64d12a1df6378f1056331db256ea96f260e2e52b96c59a8d326c46；逐phase/node/XML/sessionfinish/source/test audit通过。覆盖真实app预览/授权并初始化零调用、后台generation→pause→原稿选择→author接管、普通resume不能绕过作者、真实作者facts确认/原commit→公开complete-author实际回读→下一章仍paused且零调用→明确resume实际policy下一章完成；原start机器副本中断只读诊断、错key拒绝、原recover-command无handle/零调用、旧提交不派发/多余caller字段拒绝/不存在原stage恢复拒绝/stop。另同构建原后台三项实际重跑：重复请求/冷读/明确续办、关闭保持实例与维护直到阻塞阶段退出、真实exit98保存ticket后强退。原43338先前3项db4d03a3留作历史，不与当前5重复累计。

已安装workbench/reviewed_batch_api.py、原后台任务服务与reviewed-batches.html/js；app lifecycle及finalizer先关闭任务再释放lease；作品概览新增自动连续写作，作者接管链接可明确选择初稿或修复稿。金额/用量来自原chapter ledger聚合，不信caller通过标记；质量原文依据、原记录恢复、原author发布请求均有入口。

真实Chrome157：原85394、最终25639均已exit0且server日志明确关闭，当前无活动测试/服务。最终隔离书970b724038e3，最终生产72b9206da67d490374a1723a3b55abfcb5f99166a2c118eedeb8a973ccb97a97；与已通过5项的构建仅reviewed-batches.js空列表状态文案不同，测试文件与backend字节未变，不称5项在最终指纹重跑。实际打开页面/脚本/列表200，核对响应items=[]/fixture演练价格/execution_permit=false，真实刷新仅GET，空表单原生必填阻止POST，console无error/warn；修正加载结束仍显示正在读取并在最终源真实复核。填写价格/限额的fill_form被自动审批检查409 thin_wrong_thread_or_session拦截，未执行，不盲重试或用脚本/其他工具绕过。DevTools写证据文件路径也被其工作区检查拒绝，改为正常只读结果并在.tmp/t63-public-browser-evidence.json保存去cookie证据。完整确认/开始/暂停/继续/接管/重启UI尚未浏览器验收，不能称完整T6.3/E或稳定上线。

下一步：审批会话恢复或用户指导后补完整浏览器；可独立增加公开stage恢复实际场景和真实workbench冷启动覆盖，再最终新流程恢复、Windows/全套相关回归、独立授权真实质量/费用/阅读收益与生产验收。所有本轮书、价格、模型为隔离演练，无真实Provider/真实书/git写，Claude保持claude-opus-5-5。详细.tmp/t63-public-progress.json，whole goal保持active。


## 公开恢复与真实工作台冷启动（2026-11-07）

最新接续（2026-11-07，真实工作台冷启动及争用修复最终验证）：原8540明确exit0/14项344.77秒，生产4877022315691cb7aea56d77a4f7a17c2a5bcd4414a76f0cf2f1abc315fee446。逐node/phase/XML/sessionfinish/source/test audit通过，无warnings；当前无活动测试/服务。真实SQLite第二writer争用后原worker存活/原队列任务只执行一次、实际语法错误不当busy吞掉；完整工作台公共API授权/后台generation后真实exit99、新实例start_worker=True/GET/旧请求零dispatch、明确新resume复用原生成稿实际提取/质量/提交/readback后暂停，原worker仍alive（线程异常升error），维护6/实例5相关回归全部通过。

独立.tmp/t63-public-cold-artifact-audit.json核对最终合成书8a0ac871a0f6实际原start ticket无result、明确resume ticket有真实returned记录，原命令在真实control链、当前chapter3/generation/paused且pending空、第一章新调用2/下一章0、各生成提取计数1、formal committed2，fixture仅非敏感config字段。原43472在72b9206d虽核心1pass但线程warning1（旧worker sqlite locked退出）仍保留为未完整接受；修复只限jobs._run_one_job中claim_next返回前SQLite BUSY/LOCKED原生错误码，本轮未领取并等待，不吞任意异常/不重放运行任务。

原八阶段公共claim exit96矩阵在72b9206d通过并在test改前审计，book0d036381eef2实际8观察/repair claim1/calls4/两生成两提取/formal2/counter2/author reviews0；原47253整轮exit1/1pass1fail因另一个cold fixture漏endpoint，不能说整轮2pass。原36209生命周期20也是72b9206d历史；最终4877022只称本轮14实际重跑，不混算全部历史。详细.tmp/t63-public-recovery-progress.json。

下一步：完整浏览器确认/开始/暂停/继续/接管/重启仍因fill_form自动审批409 thin_wrong_thread_or_session待会话修复/用户指导，不盲重试或绕过。可独立推进最终新流程备份/可写恢复，尤其暂停批次中未来章已冻结预算但未使用时，保留原用量/控制/任务后能明确继续的真实边界；不能用旧样本或重建空预算代替。随后Windows发布、全套相关回归与独立真实质量/费用/人工阅读验收。完整T6.3/E/D/F与whole goal未完成，active；无真实Provider/真实作品/git写，Claude保持claude-opus-5-5。


## 最终自动流程恢复：实际续接缺口（2026-11-07）

最新接续（2026-11-07，最终自动流程真实恢复缺口已复现）：原37422明确exit1/1fail487.80秒，源4877022315691cb7aea56d77a4f7a17c2a5bcd4414a76f0cf2f1abc315fee446，现场.tmp/iu3/8d198b05e732，package backups/backup_9779e92e14934c8aa290d8a9df18de9c、target restored。真实公共批次2–4，第2章正式policy提交/readback，第三章生成后暂停并作者改稿r2，第4章冻结未用；公开受控归档、实际materialize/root/整库绑定、真实同机handoff/activate及冷启动作者稿精确读回均到达。测试在第70行GET原batch时409 reviewed_batch_invalid失败：原不可变grant的source_root及ownership_sha256仍绑定旧物理作品，现有服务正确拒绝将其直接当新位置许可，但缺少显式恢复续接机制。不能把此测试改成期待409来冒充完整恢复通过。

独立.tmp/t72-final-batch-restore-repro-audit.json已实际核对activation active、formal committed2、作者draft r2末尾修改保留、原grant副本字节未变、原control chapter3/extraction/paused、预算配置原样且c2/c3/c4调用2/1/0，恢复后0新调用；源/test指纹和失败sessionfinish1一致。当前无活动测试/服务，生产/tests可编辑。完整T7.2仍未完成，原generic first-audit不适用于失败轮，不伪造通过证据。

下一实现：先增加只读的恢复批次历史，再通过真实active restored qualification/current machine owner核验、原machine grant/control与归档及handoff最新来源一致后，明确preview/作者确认记录恢复续接。保留原grant/control/task/budget/repair证明，用新的合格续接控制命名空间承接原逻辑批次，不能把导入的旧执行路径重新放行；新的物理context需有显式、可核验的旧正式readback→新trusted context连接，不改旧证明或信caller标记。restart/GET/旧请求不授handle，不重置原计费/修复次数/有限章范围。全流程测试须加入明确续接预览/确认后，实际保留编辑稿、真实作者处理第3章、回读后第4仍paused，再明确继续用原未用预算完成第4；现有test末段保留作为完整目标，尚未执行到。细节.tmp/t72-final-batch-restore-progress.json。

浏览器fill_form自动审批409 thin_wrong_thread_or_session仍待会话修复/用户指导，不重试/绕过。完整T6.3/T7.2/E/F及whole goal未完成，active；无真实Provider/真实作品/git写，Claude保持claude-opus-5-5。


## 恢复批次历史与明确准备（2026-11-07，执行接入待完成）

最新接续（2026-11-07，恢复批次只读历史与明确准备分项通过）：原90226明确exit0/2项496.31秒，生产0342e818b4e91570c2117ba5f0660be15365138e8849dc861d2911087439f3e8；audit核对所有node/phase/XML/sessionfinish/source及test SHA。实际章纲迁移映射正向/全文或hash/核心节拍变化拒绝/错相对成员及越界拒绝通过；实际完整最终样本重新生成并归档、materialize/root/整库绑定、同机handoff/activate后，原批次history GET200、list200、preview真实冻结来源核验通过，缺确认/过期摘要409，明确prepare及重复确认同结果，无handle/worker/新dispatch，原稿r2/授权/控制/预算用量均不变。

独立.tmp/t72-restored-batch-preparation-artifact-audit.json核对实际feafd5b79132/restored/book：原grant及control与旧来源副本字节相同；chapter3/extraction/paused；new namespace六json（intent/grant/anchor/control/control-origin/ready）及独立machine brc记录均校验hash/资格/同一原grant，new control精确等于原链；原c2/c3/c4预算配置及calls2/1/0保持，formal committed2；六处原章纲坐标到新位置明确映射。无模型调用，未声称执行集成完成。此前原18071失败498.32秒和确切未持锁owner原因保留；修正精确借用publication已持锁owner，并仅对声明的blueprint源坐标建立映射。旧73ab7170维护5项另作历史，不与新2合称新7通过。

当前无活动测试/服务，生产/tests可编辑。下一步必须实现新continuation-ready冷读认证和有效执行路由（control_paths/grant路径、_stored/_require_frozen不能仅去掉旧root检查），仍使用同一原grant及budget/repair ledger；旧正式readback/current physical context必须显式校验连接，不改旧artifact或信caller flags。原histories保持只读，导入执行路径拒绝不放开，新qualified控制命名空间承接同一前序链；公开UI明确确认后仍不授运行handle，之后才明确resume。tests/test_final_batch_restore.py完整test保留（helper默认preparation_only=False），还需实际作者处理3→原回读→4 paused→明确继续实写4通过；当前仅命名清晰的preparation分项完成，不能以其return替代全目标。再补丢凭证/篡改/强退续办与恢复再归档代际等，最终T7.2/Windows/真实验证仍待。细节.tmp/t72-restored-batch-preparation-progress.json。

浏览器fill_form审批409 thin_wrong_thread_or_session仍不重试/绕过；完整T6.3/T7.2/E/F/whole goal未完成，保持active。Claude保留claude-opus-5-5，无真实Provider/真实书/git写。
