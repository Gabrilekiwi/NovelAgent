# T7.2：打开已启用恢复副本的浏览器验证（partial）

## 2026-10-02：旧格式作者草稿补验（partial）

本节是较新的补验；下方原报告保留其原指纹与范围。当前生产指纹为 `4a1225a90a8aae10c11660704a0387c9c7cd4a2800a1b92cd1083cd26aefc2f1`。

Changed behavior tested：旧格式 1.0/2.0 章节详情共用已核验作者草稿的只读返回逻辑；统一身份读取器按已验证的原始版本字段选择 reviewed 冷读，避免访问 reviewed 元数据不存在的属性。页面不将作者草稿当作正式正文或已审核候选。

Browser environment：Codex IAB，实际 CLI `http://127.0.0.1:59854/`，恢复书 `.tmp/lr-b602a28f`，私有机器目录 `.tmp/lb-b53abcdd/machine`。所有内容及登记均为明确合成测试材料；SDK、worker、自动导入和外网连接被测试护栏禁止。普通旧书登记没有借用本测试完成。

Actions performed：从书架原生打开作品，读取正式第一章，点击“开始下一章”；核对第二章的作者草稿，刷新；仅在隔离目标 DB 中故意改坏草稿 SHA，刷新观察错误；还原原 SHA，原生点击“重试”。最后正常退出 CLI 并关闭临时浏览器标签。

Observed UI result：第一章正文 98 字，SHA `ae15bfcc32ccda854fe55da59fc2a9b29654e50a3bcdb56f854557fa21750063`；第二章仍未提交。作者草稿文本为“旧书作者保留的第二章草稿”及末尾换行，r1，textarea 为 readonly；DOM 内容计算出的 SHA 与接口及来源均为 `15ac59ddcbe352cd2e6c88111bafb4aa07b06ed93540231e6fe5f6d1e3a74317`。刷新与重试后相同。损坏 SHA 时显示明确的“保留的作者草稿内容或修订无法核验”错误，没有伪装为无草稿。

Console/runtime check：正常、刷新、故障和重试各阶段的 IAB error/warn 观察均为空。Network check：实际目标 ASGI 响应观察记录章节详情 `GET 200 → 200 → 409(job_error) → 200`，成功体包含完整 `retained_author_draft` 及 `has_prose=false`；无写请求。已提交第一章没有保留旧章纲，card GET400 是夹具既有条件，正文 GET200 单列，不记为本次修复失败。

Additional checks：原 33146 exit0，35 项通过、293.98 秒（旧格式实际恢复 15，新格式实际打开/损坏拒绝 2，工作台回归 18）。原 40885 的失败明确复现旧格式 API 缺字段；原 70755 为 33 通过、2 setup error，暴露 reviewed 元数据属性错误；两轮失败均保留，修复发生在其原句柄终态后。未重算既有 T7.2 新增用例数。

Evidence：`.tmp/t72-legacy-draft-v2.xml` / `.log`；`.tmp/t72-legacy-draft-browser-v2-evidence.json`、`-responses.jsonl`、`-after.json`；页面 `.tmp/t72-legacy-draft-browser-v2-restored.png` 与故障页面 `-negative.png`。原 63250/PID45828 正常 exit0；原/目标书完整清单、逻辑 SQL 及软件前后一致，worker/真实 Provider 调用 0。

Remaining risks：Chrome DevTools MCP 页面列表观察 cell351 在 20 秒内没有返回，已停止该观察，故未取得 Chrome 完整网络证据；ASGI 观察保存有界响应体/状态，没有请求头或请求体。因此结果为 partial，不标记完整 Chrome 验收。正常未登记旧书接入、升级、完整恢复认证及确定性下一章闭环仍待完成。

Manual browser verification checklist：在新建的等效隔离恢复样本中用 Chrome 进入书架→作品→已提交章→下一章；确认草稿内容/r1/SHA/readonly，刷新仍一致；在测试 DB 中改坏草稿 SHA 后确认 GET409 和明确提示、正式正文不变；还原后点击重试确认 GET200 与原草稿一致；检查控制台及请求/响应，确认没有 Provider 或写请求。不要在正式作品上注入故障。

## 最新浏览器验证结果

日期：2026-10-02。Status: **partial**。最新指纹 `e6165c62ee92ae032c4501c17eaae098a2905edbac8625dc5bd438b0c21083d0`。原生交互、控制台与实际接口观察在 Codex IAB 完成；最新 Chrome MCP 页面列表读取仍无返回，观察 cell 176 已停止。这不是应用进程终止的证据，应用另外按所属停止标记正常退出。

Changed behavior tested：原恢复会话打开目标、复用同一目标实例、下一章保留作者草稿、资格读取竞争的处理。实现说明见 `t72-recovery-target.md`。

Browser environment：来源此次为 `http://127.0.0.1:38898/`，目标为 `http://127.0.0.1:52864/`，均已正常退出；浏览器为 Codex In-app Browser。来源与 CLI 子进程使用原持久隔离样本、私有机器账本、禁止 Provider/worker/自动导入的夹具，不接触真实作品。

Actions performed：

1. 原生展开恢复面板，选择已启用的原会话，点击打开目标。
2. 返回来源，重新选择并原生打开，确认同一端口/实例。
3. 原生进入“继续写作”，核对第三章保留草稿与只读属性，保存页面图像。
4. 关闭本轮创建的 IAB 测试标签；通过所属停止标记正常结束来源与实际 CLI。

Observed UI result：两章已提交、下一章为第三章；草稿 11 字、r1，SHA 与来源一致；正式第三章 SHA 仍为空。最新完整页面图像 `.tmp/t72-recovery-target-v4-iab.png`。正式第二章文字和 SHA 曾在前一 v3 的实际 UI 核验，此次完整目标书稿清单和逻辑 SQL 再次证明不变；不重复宣称在 v4 进行第二章 DOM 哈希计算。

Console/runtime check：最新原生首次/重复打开及草稿检查的 error/warn 记录为空。独立 HTTP 持锁负例返回的忙碌 409 不在这些浏览器交互中产生控制台错误，夹具中有明确标签。

Network check：目标只观察中间件原样转发响应，保存方法、路径、状态，以及维护状态必要字段；首次和重复打开后的维护 GET 200/idle，bootstrap/projects/目标读取均 200。来源打开 POST 的跨导航响应体没有由 IAB 直接保存，结构与绑定另由实际 HTTP/CLI 专项测试证明，原生导航证明页面使用返回目标。所有 Cookie/Authorization 均未记录或输出。

Additional checks：原 10604 exit 0，14 项/141.43 秒；原 45905 exit 0，72 项和 6 项子测试/202.90 秒。最新 12 组来源与目标并发读取全部成功。长时间持锁返回明确 `restore_scope_busy`，释放后原实例正常；等待期间损坏资格凭据仍拒绝。来源 45807 exit 0，所属目标 child exit 0，书稿/逻辑 SQL/软件均不变，worker 不存在、Provider 调用为空。

Evidence：`.tmp/t72-recovery-target-v4-iab-evidence.json`、`t72-recovery-target-observer-v4-{info,before,after}.json`、`t72-recovery-target-observer-v4-target-stop.responses.jsonl`、`t72-recovery-target-observer-v4-target-stop.after.json`、`t72-recovery-target-contention-v4-results.json`、最新专项/回归日志和 JUnit。

Remaining risks：[browser-change-verifier 技能](C:/Users/Administrator/.codex/skills/browser-change-verifier/SKILL.md)明确要求：“If Chrome DevTools MCP is unavailable, say so plainly, run the best available local checks, and provide a manual browser verification checklist instead of marking the change verified.” 因此本组件保留 partial，接受计数仍 417；不把 IAB 证据冒充最新 Chrome MCP 验收，也不把该限制当作停止其他工程任务的理由。

Chrome 恢复连接后的复验清单：

1. 在同一合格隔离会话选择原目标，原生点击首次及重复打开；确认 URL 和 `/api/instance` 身份一致，未启动第二个可写实例。
2. 保存此次打开 POST 及维护 GET 的请求/响应，维护状态为 200/idle，控制台无新增错误。
3. 原生打开下一章，确认独立保留草稿为原文本/r1/SHA、readonly，正式正文仍未生成；不执行模型生成。
4. 正常停止所属测试进程，确认原句柄终态、无 worker/Provider 调用、来源和目标书稿/逻辑 SQL 不变。

## 历史 v2 现场（原始证据保留）

日期：2026-10-02。当前源码指纹：`0d72d64cb9eda4ee442f232b5e8322a17dd4b0e66eb02faf451fb9c271cf5d30`。该组件尚未验收，T7.2 已接受的不同新增用例仍为 417；本组件的 11 项尚未加入计数。

## 已取得的证据

- 最新专项测试：原进程 49289 已观察 exit 0，11 项通过，152.00 秒；最新相关回归：原进程 47224 已观察 exit 0，39 项通过，186.51 秒。记录在 `.tmp/t72-recovery-target-final.{log,xml}` 与 `.tmp/t72-recovery-target-final-regression.{log,xml}`。
- Chrome 原生操作完成恢复准备、显式启用和结束维护，再点击“打开已启用副本”，真实 CLI 子实例在端口 49191 打开指定作品。工作台展示两章已提交、下一章为第三章。
- 正式第二章 DOM 正文为 1289 字，SHA256 为 `6368997d68d3a2e19d4c3e8386a943bee3a37ed57967dd011ffe3af8135af8e4`，与来源一致。
- 第三章独立保留的作者草稿可见，readonly 文本为 11 字，修订为 1，SHA256 为 `9a3a90b54a2e59d5b8a9342cb17754113e4ea3fcc0e89dfe2f8c3c54deb31680`。实际章节 GET 200 返回 `current_prose=null`、`has_prose=false` 和单独的 `retained_author_draft`；草稿未被自动提升为正式正文或审核候选。
- 返回来源控制器再次原生点击打开按钮，POST 200，导航仍使用原端口 49191。测试另行覆盖同实例复用、启动观察超时不重启原子进程、控制器重启后复用实例和拒绝用户指定目标/URL/授权参数。

## 未解决问题与证据限制

第二次打开后，Chrome 网络记录中的 `/api/maintenance` GET 513 与 514 返回 409，控制台记录两次资源错误。刷新页面后旧响应体已无法取回；当前直接读取同一接口为 200，不能据此解释或消除此前错误。Chrome 观察工具随后持续无法返回页面列表/快照，CUA 仅暴露空的 IAB/MCP Apps 浏览器，未提供当前 Chrome 连接。本次组件因此保留未验收状态。

跨端口导航后部分打开接口的响应体不可取回。已保留原生操作、导航 URL、请求状态、目标 DOM、可取回的章节响应体及实际 CLI 测试证据；不声称所有跨导航响应体均已保存。

## 正常退出与不变性

原隔离来源进程 64015（PID 25876）已通过所属停止标记正常退出，观察 exit 0；其管理的目标 CLI 子进程 PID 51184 正常 exit 0。子进程报告 worker 不存在、Provider 调用为空。来源书稿、来源逻辑 SQL 和软件指纹保持不变；目标书稿完整清单及逻辑 SQL 与打开前相等，目标 SQL 摘要为 `5d64d08fa81847f95a190684af91413ad5d60fe4d36af05fa1a821c8ef38a33f`。

证据：`.tmp/t72-recovery-target-browser-v2-evidence.json`、`-info.json`、`-before.json`、`-after.json`、`-target-before.json`、`-target-after.json`，以及 `.tmp/t72-recovery-target-browser-v2-target-stop.after.json`。无真实作品写入或付费 Provider 调用。

## 续接

在同一持久恢复会话和隔离目标上补充仅观察维护错误响应的测试夹具，复现重复打开并取得 409 的结构化原因，再完成必要修复和受影响验证。不要重复创建已恢复目标或把一次后续 GET 200 当作此前错误已解决。后续仍需已经恢复的来源控制器支持、正常旧书登记/升级、完整恢复认证、金额控制、E/F 阶段及独立真实使用验收。
