# T6.1：真实 policy 生成来源与阶段结果

2026-11-05，真实 policy 生成与阶段结果内部组件已验证。完整自动三章、policy 提取、发布/readback、原位人工接管和一次修复仍未完成。测试只操作隔离合成作品，不调用实际账户、API 或真实作品。

## 实现范围

`core/engine/chapter_candidate.py` 将纯可信上下文/input pack 准备提取为共用逻辑。原人工 execute 仍验证原作者确认并生成原 1.1 Intent；新增 execute_policy 必须存在当前线程、当前实例的原批次 generation scope，并使用原章冻结预算。policy Intent 为 1.2，包含严格 policy_authorization，没有 author_confirmation、作者卡片或伪造 author_confirmed=true。对应的 Candidate record 同为 1.2；原 1.1 字段及请求 hash 不变。

`core/engine/reviewed_policy_generation.py` 绑定原 grant/manifest/章纲、上下文、完整生成输入、确定 step/execution。磁盘读取按新 schema 重建并核对完整输入 digest；可搬运的 hash 只证明绑定，不创建执行许可。execute_policy 和未终态 policy resume 都在实际执行前再次要求活跃 scope。已有已完成结果可只读验证，不会因此重新执行。

`workbench/reviewed_policy_generation.py` 在真实拥有者/原批次来源校验下，从冻结章纲核心事件导出章意图，使用确定 Candidate/execution/key；进入原 generation step 后调用实际执行器并要求已完成且模型响应证据完整。独立 SQL 作者稿只执行 create_from_candidate，已有稿保留。结果凭据绑定实际 Candidate Intent/record、原正文和初始作者稿 revision/SHA；finish_step 的服务器核验器实际重新读取完整原证据和当前输入。作者改稿、来源变动或凭据变化会阻止阶段推进，不覆盖稿件，也不换 id 再生成。

结果凭据保存于 reviewed/batch_results。生成结果已落盘、finish 尚未完成时冷读不恢复许可；新的明确 resume 使用原 pending step / execution / Candidate，已完成的模型证据不重发。真实 dispatch 后未知调用仍受原全书 unresolved-call 门禁约束，明确 resume 也不能绕过它。

现有 Candidate 读取接口对 policy 返回 confirmation=null 和 generation_source=policy，不杜撰作者确认。最终质量读取支持 1.2：携带 candidate_policy_authorization_sha256；policy 最终审核还要求原 grant 相同。人工接管的最终作者审核可读取同一真实 Candidate 并获得相同领域结果，但完整批次接管/继续尚未接好。现有页面仅增加 policy 来源提示和上下文变化提示；不新增自动生成按钮、控制 API 或后台 worker。

## 验证与中断续接

首轮原 95020，exit 1：3 pass、1 fail / 259.65 秒。真实 inside-dispatch 强退留下未知调用后，既有 owned boundary 已在新 resume 之前拒绝，测试错误地预期走到更晚的计时 guard；修正断言到真正拒绝边界，没有弱化生产门禁。原日志/XML/逐项事件保留。

第二轮原 60386，exit 0：41 pass、2 个子断言 / 583.99 秒。包括真实 policy 模型凭据/无作者卡片、作者改稿保留并阻止推进、真实 os._exit81（结果落盘后）/82（dispatch 中）、policy Candidate 到完整最终质量及同输入人工领域一致、原 Candidate 与控制回归。子断言不额外累加节点。

原生页面首轮实际读取 policy 草稿、调用与费用均为原记录，所有相关 API 200，但沿用作者标签误示旧输入。已修正展示。第一次浏览器服务器夹具将 Windows asyncio 本机 socketpair 误拦为网络，未启动；第二次允许本机连接、禁止外网。首次猜测 /chapters 路由得到 404，按实际 SPA 入口进入书架/作品/章节后确认草稿可读。这些失败不计通过。

最终原 85651 明确 exit 0，62 passed、2 个子断言 / 502.54 秒，`.tmp/t61-policy-generation-final.log/.xml`、逐项 events。覆盖新增实际生成 5 项、原 Candidate 17 项、policy 审核及真正正式事务/cold history、原 Candidate API、最终质量证据/作者与章纲变化回归。完整节点清单以 journal collected 为准。较第二轮仅修改完整输入 digest 的磁盘重建核对和页面 policy 来源标签。`.tmp/t61-policy-generation-audit.json` 核对全部 collected/每项 setup-call-teardown（兼容同一项多个 call 子断言）/sessionfinish/XML、当前完整生产指纹和最终全部测试 SHA；第二轮 control 等未变化测试 SHA 仍一致。81 个不同节点仅作不同构建的历史覆盖索引，最终构建重跑 62 个，不累加子断言或旧失败轮重复 pass。

最终指纹 0c4eb4cbd1f795427002bb18fd69c6ccec3087a5dd27608d5e9708e62eb7c809，`.tmp/t61-policy-generation-final-source.json`；生产与测试运行期间冻结。最终浏览器服务原 19553 手动结束、exit 1，PID 37376 已不存在；原 85651 已 exit 0，当前本组件无活动测试/服务。恢复合成章纲原字节，物理合成调用计数仍 1。当前详细结果见 `.tmp/t61-policy-generation-progress.json`。

## Browser verification result

Status: passed（仅现有候选读取/来源提示/刷新与输入变化展示，不代表完整自动批次 UI）。

- 环境：Chrome DevTools MCP，隔离合成作品，http://127.0.0.1:8895；未启动 worker，禁止外部网络。
- 操作：从书架点击作品和章节、点击“生成与编辑”、刷新；在合成章纲加入文字后刷新，再恢复原字节并刷新。没有点击实际生成/提取操作。
- 正常 UI：来源“来自已授权自动批次”和“自动批次草稿”；原正文、r1/SHA 保持。新生成仍要求作者明确确认，读取不创建许可。
- 负向 UI：章纲变化后显示“输入已变化的批次草稿”和核对提示，原稿保留；恢复原字节后返回正常来源标签。
- 控制台：无新增 error/warn 或未捕获异常；原表单缺 id/name 的 issue 在改动前已存在，仍保留为非运行错误，未宣称无任何提示。
- 网络：实际 Candidate/list/card/budget/fact 列表 GET 全部 200，无 POST。Candidate response 为 generation_source=policy、confirmation=null、author_confirmed=false；费用仍为原占用，不触发第二次调用。
- 证据：`.tmp/t61-policy-generation-browser-{good,changed,restored}-snapshot.json`、`*-{changed,restored}-network.json`、`*-candidate-response.json`、`*-console.json`；上述 browser audit 同时核对最终构建与原调用计数。
- 限制：原位作者接管并继续批次、付费调用和完整自动批次页面尚未验证。

## 尚未交付

真正 policy 提取来源、所有阶段 typed 结果核验、原正式发布 Intent/索引恢复和 readback、后续章权威上下文、单次修复全提取重检、连续三章和失败后下一章零调用、公开自动批次 UI 仍待完成。当前原 grant/start 校验限定首章。本组件不代替真实阅读/收益实验，也不宣称 T6 或 D/E/F 全部通过。
