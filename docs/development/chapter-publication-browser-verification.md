# Browser verification result

Status: passed for the direct publication page paths below; not a complete T3.2/main-workbench acceptance.

## Changed behavior tested

- 真实作者审核 → 标准质量预览 → 勾选确认 → 发布。
- 来源变更时冲突阻断，保留草稿。
- 实际双击只发送一次提交；刷新显示原凭证，发布后草稿修改不冒充已发布正文。
- 提交 after_prepare 注入响应丢失后，查询原请求完成回滚；刷新仍显示未发布；显式重新准备后才能换 key。

## Browser environment

- URL: `http://127.0.0.1:8876/publication?project_id=...&chapter=2&review_id=...`
- Chrome 152 / Chrome DevTools MCP；独立浏览器 context `publication-verification`。
- 实际 FastAPI app，worker=false、auto_import_active=false；本机台账、数据库、真实升级和确定性候选均位于 `.tmp/iu3`，未调用付费 Provider，未修改真实作品。
- 第一份样本：`.tmp/iu3/ea475cd6dfeb/b`；第二份响应中断样本：`.tmp/iu3/3095c1dc674d/b`。

## Actions performed / observed UI

1. 初次进入仅可生成预览，发布禁用。
2. 修改隔离人工来源后点击预览：显示来源变更错误，发布禁用，原草稿仍可查看；恢复原字节后刷新。
3. 点击生成预览：显示本地标准质量通过，最终正文、接受的事实及七类状态。展开数值记录并勾选作者确认。
4. 双击发布：显示“第 2 章已发布”，发布按钮禁用。磁盘与 SQLite 核验为 next_chapter=3、1 个提交意图、1 条章节发布索引、2 个 receipt（含升级）。
5. 刷新：查询原结果后仍显示同一凭证 `commit_55f2940fedf983fa585931e2bf41bd18`，没有再发 publication 请求。
6. 修改隔离的后续草稿并刷新：仍返回原凭证；页面说明草稿已有后续修改，并清空最终正文展示，不把新草稿当成已发布内容。
7. 在第二份样本通过实际页面发布，after_prepare 的一次性故障导致 HTTP 500 非 JSON 响应：页面保留原请求、提示查询，发布禁用。
8. 点击查询原提交结果：HTTP 200 但 `state=rolled_back`、`committed=false`、`publication_verified=false`、`retry_requires_new_key=true`。页面明确显示尚未发布；磁盘 next_chapter=2，章节 receipt=0。
9. 刷新仍显示回滚结果；仅显式“重新准备提交”后才恢复预览入口。
10. 重新预览、勾选并发布：req69 使用新 key `a9ca56f9-846a-4394-923d-27f5333457bd`，返回已发布凭证 `commit_e9e0ab307388033e37850c6d2f8c73c4`。页面标题为“第 2 章 · 已发布”，无控制台错误；磁盘 next_chapter=3、2 个意图（含原回滚意图）、1 个章节 receipt、1 条发布索引。

## Console/runtime check

正常预览、发布与刷新没有控制台错误、未处理异常或请求循环。负例的 409 和人工注入的 500 各产生一条浏览器 `Failed to load resource`，与预期响应一一对应；没有额外 JavaScript 未处理异常。故障后页面正常处理非 JSON 响应并保留原请求。

## Network check

- req8: POST publication-preview → 409，`chapter_fact_authority_changed`，请求仅审核编号/两个 revision/两个 SHA。
- req26: POST publication-preview → 200；浏览器响应观察器确认 `publication_authorized=false`，`local_quality_passed=true`，`quality_coverage_complete=true`，`pending_gates=[]`，最终正文 SHA 为 `6368997d68d3a2e19d4c3e8386a943bee3a37ed57967dd011ffe3af8135af8e4`。
- req27: POST publication → 200；双击仍只有一个请求。请求头 key 为 `fca4d57d-331e-4f05-982f-9a0b41a470b7`，正文只有固定 CAS 字段及 `author_confirmed=true`；响应完整核验 `publication_verified=true`、原 run_id、原请求和 receipt_hash。
- req32: 刷新 POST publication-status → 200，随后 GET publication-input → 200；没有重复 publication。
- req50: 第二样本 POST publication → 故障注入 500；req51: POST publication-status → 200，使用同一 key `7cc5499a-877d-4c70-b4ff-20d5f3e63dba`，返回原 run `commit_d17ceb8d163eba0d6b1e38ce5ede10b3` 的回滚结果。
- 所有请求均同源；无隐藏的成功发布、无 Provider 请求。

## Additional checks / evidence

应用 API、共享服务、旧 app、准备层、新版上下文及静态契约的本轮最新证据共覆盖 104 个不同用例，最新结果均通过。原 XML 保留早期失败：初轮测试错用 `coverage_complete` 字段，修正为真实 `quality_coverage_complete`；新增未登记损坏日志负例暴露 `PersistenceV2Error` 未被转换的问题，补精确捕获后通过。不是删除失败或改写原 XML。文件、SHA、进程终态见 `stage-c-progress.json`。

截图通过 MCP 内联输出人工查看，页面布局与文字无遮挡。DevTools 的文件保存配置拒绝本项目路径，因此未保存截图或网络原始大包；预览响应在工具中被截断时，用浏览器 fetch 响应克隆观察器读取真实响应字段，未替代实际点击。小型提交/回滚响应直接由网络工具完整核验。未保存会话 Cookie。

## Remaining risks

尚未把新版项目导航、Candidate/提取/审核和自动 worker 全链条接到发布页；真实作品升级与本机登记入口仍待后续阶段。单独“恢复原事务”按钮的服务路径由实际 API 测试覆盖，本次浏览器恢复经“查询原提交结果”路径。新浏览器无 localStorage 的历史发布展示应在主章节详情接入。T3.2 全量 pytest/smoke 与两模式最终验收未执行。

清理：两次隔离服务 session76033/PID31344、session56116/PID32308 均已停止；原有无关进程未操作。关闭 Chrome 测试页2被自动审批以workspace out of credits拒绝，未绕过；该页暂保留，服务已不可写。
