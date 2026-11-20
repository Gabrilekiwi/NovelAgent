# T5.2：质量检查与发布准备共用章计时

日期：2026-11-04。整体T5.2和D–F尚未完成。本轮是现有本地质量检查与发布准备的计时接入，不代表自动修复或真实收益验收。

## 实现边界

实际已有接口为明确的POST `publication-preview`，此前交接把它称为GET不准确。本轮直接计入作者触发的该操作，不增加平行且未被使用的检查入口。`publication-input` GET、预算GET、纯`prepare_quality`仍不创建执行区间或质量凭证。

`ChapterPublicationService.preview`在既有作品所有权、Provider边界与同一SQL事务内读取原冻结章预算，用`ChapterExecutionBudget.operation`计量准备、来源复核及本地质量检查。响应的`execution_budget`在计时区间关闭后读出；动态时间不写入原不可变质量报告或改变其哈希语义。重复明确检查会再次计量实际工作，作者在两次操作之间等待单列。

新发布同样使用原章预算，从准备、质量重检、暂存到发布完成共用一个区间。检查完成、创建发布Intent前及正式commit前检查剩余主动时间。暂存后耗尽时保留原事务，允许原请求核对/回滚；标记后不会因时间已耗尽而把已经发布的结果冒称未发布。已发布结果与原Intent的查找先于新执行时间门禁，未知或耗尽时间不阻断原提交结果查询。原事务恢复/查询继续按原恢复流程，不在本轮补造已丢失的主动时长。

章操作增加明确的`provider_dispatch_allowed`，本地质量/发布设为false。标准ModelCallRuntime在任何Intent/预留/调用之前拒绝该作用域内的Provider attempt，包括借用未绑定runtime的情况。未来模型检查/修复仍须接原章的物理调用预算；本轮没有把本地检查静默改成模型调用。

默认服务要求已有原冻结预算；不会给已发生的旧无预算工作初始化空额度。合成`ready`夹具在确定性生成之前冻结预算，测试仍没有真实Provider。没有修改已冻结价格、旧Intent/Receipt或历史时间凭证。

## 页面行为

发布页显式GET读取当前预算，校验作品/章及时间与预算hash绑定。时间未知、仍执行、耗尽或记录不可读时禁用新的预览和发布准备；保留原草稿及原结果查询。新增“重新读取审核与预算”只读操作，清空预览与作者确认，不运行质量检查、不增加区间。刷新后重新读取预算，不能靠重新载入页面重新启用超时操作。

## 自动证据

- 首轮`.tmp/t52-quality-budget-first.log/.xml`：原session98000 observed exit0，44通过，622.77秒；构建`75987b1010c2b7d1864944f99be086d725826fd52a349a7dbc19977d058c5b8a`。包括9项新服务专项、原发布服务/API和服务计时回归。专项含累计/作者等待、耗尽不换key绕过、本地检查拒绝借用runtime、发布重检/完成计时、标记前回滚/标记后原结果以及2个真实子进程`os._exit(73)`。
- UI修正后构建`21b6f863162f4f417f98a880439c06dc43193b9978b243a4fd5781970f15e88e`，全源清单`.tmp/t52-quality-current-source.json`。新增5项Node状态检查覆盖未知/执行/耗尽、只读刷新、读取失败保留草稿和检查中超时清确认。
- 原session66792的`.tmp/t52-quality-final-regression.log`只保留72个点/85%进度，工具会话切换后句柄缺失，没有最终XML，**不接受为整组通过**。84项收集顺序保存在`.tmp/t52-quality-regression-collection.txt`。
- 原session86706同构建补验证已observed exit0，**84通过，333.28秒**；日志`.tmp/t52-quality-final-regression-resumed.log`、XML同名。`.tmp/t52-quality-regression-resumed-events.jsonl`含84个唯一nodeid的完整setup/call/teardown通过记录和sessionfinish exitstatus0，各阶段即时flush+fsync；后续若中断先读原句柄/逐项日志，不能把进度猜成终态或无依据重复启动。涵盖5项新UI状态、37项原预算runtime、Candidate/纯本地质量、默认工作台全链及作者补充发布/恢复。

不合并重复回归为新增接受数。新正式任务接受0；真实Provider调用/真实作品写入0。最终结果以`.tmp/t52-quality-time-progress.json`补充。

## Browser verification result

Status: passed，范围仅为质量预览计时、超时提示与刷新/重读控制。金额表单原生填写仍是另外一项partial，不由本轮结果替代。

Browser environment:

- Chrome 156 / DevTools，page4，isolatedContext `t52-quality-final`。
- URL：`http://127.0.0.1:8894/publication?project_id=project_5f473ee7a3184ff0bcbf24262620fb65&chapter=2&review_id=fact_review_b57b707666c6d797b1b4ed62b6fead0f`。
- 合成作品`.tmp/iu3/344c637329c1/b`，实际默认工作台服务；SDK/worker禁止，准备时确定性generator一次。夹具`.tmp/t52-quality-browser.py`，记录`.tmp/t52-quality-browser-second-state.json`及`-second-audit.json`。

Actions performed and observed UI result:

1. 初始GET input及budget均200，预览可点，发布禁用。原生点击“生成提交预览”，req10 POST preview 200，质量完整且通过，页面显示累计主动6.719秒/900秒；原章账本恰有一个已关闭区间，Provider调用0，未建发布Intent。
2. 原生勾选发布确认后发布按钮可用；只点击“重新读取审核与预算”，没有点击发布。req11/12只有GET input/budget，预览与确认清空、发布禁用，原time.json字节完全不变。
3. 仅在合成夹具内模拟额外901主动秒，然后原生点击预览。req15 POST preview 409，body为`chapter_active_time_exceeded`；请求只有原review/CAS字段，没有客户端质量/时间结果。页面显示暂停，清空预览与确认，新预览及发布均禁用，原结果查询可用。
4. 原生查询原结果、刷新。req21 POST status 200，req23 GET input 200，req24 GET budget 200；没有自动POST preview/publication。刷新后仍显示主动时间耗尽、两个新操作继续禁用。原time.json保持相同字节，累计916.235秒、2个闭合区间，章游标仍2、Provider调用0、未创建发布Intent。

Console/runtime check: 正向为空；负向只有对应预期409资源错误；最后刷新为空。没有未捕获异常。

Network check: 原生正向POST、负向POST及请求CAS/错误body已检查；初始、明确重读和刷新读取均200。正向响应较大，DevTools展开文本会截断末尾；时间字段另由实际页面与原账本一致性核对，未把截断内容冒称完整抓包。

Evidence: `.tmp/t52-quality-browser-second-audit.json`、`.tmp/t52-quality-second-positive-clock.json`、`.tmp/t52-quality-second-exhausted-clock.json`、当前检查点及本会话原生snapshot/console/network输出。前一构建page3发现刷新后预览仍可点的问题，其记录保留在`browser_first`，新构建已纠正并重新验证。

Remaining risks: 未截图/布局QA；浏览器未点击实际发布，实际发布与强退恢复由服务测试覆盖。服务器79468在工具会话切换后句柄缺失，原PID20580不存在、8894无监听，未取得退出码，不虚构exit0/1。新工具会话曾移除并重建DevTools/Chrome上下文，随后在独立合成金额夹具中按同一原生fill_form入口重新验证，仍返回自动审批复核409 thin_wrong_thread_or_session、动作未执行；没有改用其他入口绕过。新金额夹具70121明确Ctrl-C停止、exit1、SDK调用0。完整修复路径、原事务恢复耗时范围、进一步Provider/model能力及全T5.2仍待完成。
