# 已恢复来源的原会话控制器：浏览器演练

2026-10-02。结果：**partial**。内置浏览器交互取得证据，完整 Chrome 验收仍待补，T7.2 已接受计数保持 417。

## 环境与版本

隔离样书、私有机器资格账本和只允许 loopback 的实际 CLI。来源为 `http://127.0.0.1:38898/`；实际第二代目标为 `http://127.0.0.1:55631/`。无真实书稿写入、无真实 Provider 调用，重启没有启动 worker。

当前生产指纹 `f9143a26f66faf8a044aee42bd80060acac9b8edead2e6b4b55c97bd58cadb44`。浏览器第一代来源来自历史夹具，不能称为当前版本下重新完成的第一代恢复；本次再归档、第二代恢复与来源控制器启动使用当前指纹。专项测试另用当前版本新建两代夹具。

## 原生操作及页面结果

1. 已启用来源进入维护，实际预览、保存诊断归档，再原生预览并建立第二代恢复会话，完成复制、目录和数据库绑定。
2. 提前结束维护正确拒绝 409，页面自动读回状态。明确交接后隐藏普通写作导航及归档入口，只保留原会话控制；刷新仍只选择原会话。
3. 正常退出来源 CLI，再启动同一已撤销来源。原维护为 interrupted，未自动执行恢复；原生明确接管后，核对正式第二章与第三章作者草稿。
4. 逐步完成用量、预算、任务和执行准备，明确启用第二代目标并结束原维护。来源继续显示已交出写资格，没有恢复普通写作入口。
5. 再次实际启动来源控制器，原生打开已启用目标。目标概览显示正式正文两章、下一章第三章；进入第三章，独立保留草稿只读、11 字、revision 1，SHA 为 `9a3a90b54a2e59d5b8a9342cb17754113e4ea3fcc0e89dfe2f8c3c54deb31680`。第三章尚未成为正式正文。
6. 回到来源，再原生打开副本，复用端口 55631、同一目标实例 `ac043c2d466a494391748237b2d32870`。异步加载期间的自动化定位失败经新页面状态核对后消失，未发现对应产品错误；两次实际 open 请求均为 200。

## 控制台与请求响应

实际来源与目标页面 error/warn 检查均为空。来源 ASGI 观察器记录方法、路径、状态与限定字段的响应内容：原会话、bootstrap、原维护及恢复步骤与页面一致；提前结束维护的 409 为预期负向结果。两次 `POST /api/recovery-sessions/<原会话>/open` 均返回同一个目标地址和原会话 ID。

观察器没有记录 headers、cookies、Authorization 或请求体；目标子进程没有 ASGI 网络观察器。因此不能声称完成目标全部请求和响应的浏览器网络检查。原生操作、限定响应、不可变会话意图和专项 API 测试共同提供局部证据。

## 进程与数据核对

来源原句柄 81049、62844、54620 均已观测正常 exit 0。最后一次来源实际 PID 43068，目标启动 PID 52008、实际服务 PID 31472；父驱动等待目标正常退出并取得 exit 0。所有演练过程的软件指纹、来源完整书稿清单与逻辑 SQL 保持不变；最后一次驱动还核对目标完整书稿和逻辑 SQL 不变。worker 缺席，Provider 调用记录为空。退出使用所属驱动停止标记，未杀死进程或重建原实例。

证据在 `.tmp/t72-restored-controller-iab-evidence.json`、三个 `t72-restored-controller-browser-*-after.json`、`t72-restored-controller-browser-open-child-stop.after.json` 和对应 `*-responses.jsonl`。页面截图为 `.tmp/t72-restored-controller-source.png`、`.tmp/t72-restored-controller-target.png`、`.tmp/t72-restored-controller-target-overview.png`。

## 未完成的 Chrome 门槛与手工清单

Chrome MCP 最新观察 210 在 20 秒内没有返回结果；仅结束观察，没有中止实际服务。按 [browser-change-verifier 技能](C:/Users/Administrator/.codex/skills/browser-change-verifier/SKILL.md) 的明确要求："provide a manual browser verification checklist instead of marking the change verified." 本报告保留 partial，不新增组件接受数量。

- 在独立隔离样书上打开 Chrome DevTools，保留 Network，完成再归档、第二代恢复和明确交接；核对实际请求体只绑定原 operation/session，没有调用方指定的资格或新目标覆盖。
- 重启原来源，核对没有自动步骤、worker 或模型调用；未经明确接管的维护操作拒绝，接管后原会话可继续。
- 完成启用和结束原维护，刷新原页面仍只有原控制范围；新维护、其他会话、普通书稿访问均拒绝。
- 两次打开目标，核对目标地址和实例相同；检查目标 bootstrap、项目、章节、草稿和预算相关 GET 的状态与响应体，以及刷新后的作者草稿 SHA、正式章数和预算。
- 检查双方 console 无新增异常或未处理 rejection；正常退出后比对完整书稿清单与逻辑 SQL。测试只使用隔离数据，保持真实 Provider 禁止。

完整恢复认证、旧书登记升级、金额控制、E/F 和独立真实收益验收仍未完成；本演练不替代这些门槛。
