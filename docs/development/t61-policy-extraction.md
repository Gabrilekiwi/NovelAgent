# T6.1：真实 policy 完整事实提取

2026-11-05，真实 policy 完整提取与阶段结果内部组件已验证，尚未完成 T6 或 D/E/F。所有输入与模型响应来自隔离合成作品，不访问实际 Provider、账户或真实作品。

## 已实现范围

`ChapterFactExtractionExecutor` 共用纯请求标准化，保留人工 execute 的 author_confirmed=True 必填检查、1.0 Intent 和原请求 hash。新 execute_policy 使用独立 1.1 Intent/record、严格 policy_authorization，无 author_confirmation。提取 proposal 仍用原完整事实协议，所有 model 建议经原解析/标准化及实际响应凭据验证；没有部分提取、筛选隐藏或伪造作者审核。

`core/engine/reviewed_policy_extraction.py` 绑定原 grant/manifest、前一生成结果 SHA、确定 step/execution、完整草稿 revision/SHA、Candidate、权威上下文及实际提取视图。磁盘读取从实际 Intent 字段重建 input digest；这些 hash 只证明绑定，执行仍须当前实例/线程的活跃 extraction scope 及原章预算。

`workbench/reviewed_policy_extractions.py` 在真实拥有者边界读取原生成结果文件与控制链 finish 证明，再核对实际 Candidate/模型响应/初始作者稿、当前可信上下文；已编辑草稿转人工，不能自动覆盖、改用新 execution 或默默重新提取。完整提取完成后，结果凭据绑定实际 Intent/record/proposal/原正文和生成结果；服务器完成核验才把阶段推进到 quality。不会调用人工提取 start、create_from_extraction 或创建作者 SQL review。

明确继续仍保留原 pending step/execution/input。实际模型成功响应已保存但解析前强退时，服务器先验证这一原响应，再进入 dispatch-forbidden 的本地恢复 scope；原未闭合执行时间继续为 unknown，不估算、关闭或归零。正在 dispatch 时强退造成的未知调用仍由原拥有者门禁拒绝新 resume，不能自动再发。只读构造服务不恢复执行许可。

`ReviewedPolicyReviewService` 对新 policy 提取额外核对原 grant/manifest；不允许同一正文的其他批次提取替代本批次策略来源。原人工提取凭据与真正作者接管保留既有行为。

## 当前验证

首轮原 48902 明确 exit 0，35 passed / 683.45 秒，生产指纹 60e97d025d99fe7b2014f57d8035eb9231257d36dd8a790087b8271f684b5673，`.tmp/t61-policy-extraction-first-source.json`。包括真实完整 policy 生成/提取/最终质量、作者编辑前后阻断、三处真实 os._exit83/84/85、原人工事实提取及批次控制回归。

补查保留了 `.tmp/t61-policy-extraction-invalid-input-before.log`：空 list 泄漏 AttributeError；None/bool 已正确包装。`.tmp/t61-policy-extraction-policy-error-before-correct-hash.log` 证明实际重算原 recursive Intent hash 后，损坏 policy_authorization 泄漏 BatchControlError，不能进入既有提取完整性错误边界。现统一包装为 ChapterFactExtractionIntegrityError（生成来源同类问题包装为 ChapterCandidateIntegrityError）。没有放行损坏数据或把读变成修复。

初版负向测试直接 digest 去掉顶层 intent_hash，未遵循提取原协议递归排除同名字段；会先在外层 checksum 被拒绝。已改用实际 _record_hash 重算后验证真正策略绑定，且真实损坏文件经现有 Workbench get 返回已定义完整性错误，原文件字节随后恢复。此前弱断言不当作深入策略拒绝的证据。强化的强退恢复检查还核对原 time.json 字节不变及新普通执行仍被 unknown 时间阻断。

最终原 25494 明确 exit 0，45 passed / 603.83 秒，`.tmp/t61-policy-extraction-final.log/.xml` 与 events/source。包括强化后的真实提取 6、无效输入 3、原 core 提取 10、原 API 4、policy 审核/正式事务历史 21、真实 policy 生成 1。生产指纹 3797003f0e2ee8f8ca310f4e7d53e5cf0cab932e3ee38e7d98fd5d47bb7d5426。生产与测试运行期间冻结；`.tmp/t61-policy-extraction-audit.json` 已复核两轮 collected/全部 setup-call-teardown/sessionfinish/XML、当前生产源、最终所有测试 hash 及首轮未变化的控制/core 测试 hash。首轮到最终仅 core Candidate/提取的上述错误包装变化。64 个不同节点仅为不同构建的历史覆盖索引，最终构建重跑 45 项；未把旧轮重复通过累加。

原两次测试已退出，浏览器服务已结束，当前本组件没有活动测试或服务。详细证据与接续位置见 `.tmp/t61-policy-extraction-progress.json`。

## Browser verification result

Status: passed（现有已完成提取读取、刷新和来源变化展示；完整批次接管/继续未验证）。

- 环境：Chrome DevTools MCP，http://127.0.0.1:8896，隔离合成作品，无 worker；SDK 和外部网络均禁止。
- 操作：打开真实章节、点击“事实审阅”定位已保存结果、刷新；合成章纲加字后刷新，再恢复原字节并刷新。
- 正常 UI：显示“事实已提取／继续审阅已保存的事实”、原 r1/SHA 草稿；采用结果仍需要明确动作，读取没有创建 SQL 作者 review。
- 负向 UI：章纲变化后原提取为 stale，显示来源变化、仅保留证据及重新提取提示，原稿/SHA 保留；恢复章纲后回到当前已完成提取。
- 网络：Candidate/card/budget/提取列表/detail/事实 review 列表的 GET 均为 200，无 POST。实际提取 response 包含完整两条 model pending 建议、原证据跨度和 proposal hash，fact_review=null；变化时 source_state=stale、stale_reason=authority_or_source_changed。
- 控制台：无新增 error/warn 或未捕获异常，保留原表单 id/name issue。
- 数据：生成与提取物理合成计数各 1，SQL 作者 review 计数 0；原章纲字节恢复。服务器原 93288 明确手动停止 exit 1，PID 32020 不存在。
- 证据：`.tmp/t61-policy-extraction-browser-{good,changed,restored}-snapshot.json`、`*-{good,changed}-response.json`、`*-{changed,restored}-network.json`、`*-console.json`、`*-state.json`。

## 后续交付

仍需把最终质量计算接到原 batch local step、正式发布 Intent/索引恢复与 readback、下一章权威上下文、单次修复的全量重提取重检、三章与失败后下一章零调用、原位作者接管后明确继续以及公开自动控制 UI。当前首章 grant 开始检查不能代替前章提交/readback 证明。Windows 长路径发布、金额表单既有浏览器限制和实际效果实验仍保留原门禁。
