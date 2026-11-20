# T7.1 有条件恢复认证公开入口

日期：2026-10-21。公开 API/UI 组件已验证；完整 T7.1、阶段 D 和 A–F 均未完成。生产指纹：`00a81e2e207ea1f09f1e0cf3cea9640c77248e8034444fc51d925be8409ab8c9`。

## 行为

工作台“诊断归档”中的所选作品与归档，现在可以核对认证前提、明确确认签发、复验已有认证。新路由使用原 `BackupControlRoute`、本地会话/Origin/Host、精确请求字段、当前备份维护操作和取消后仍由服务拥有的快照工作线程；没有另建写入边界。

- `POST /api/projects/{project_id}/backups/{archive_id}/certification/preview`：实际读取原归档、历史和当前来源，返回真实评估摘要；只读，不签发。
- `POST .../certification`：必须 `confirmed=true` 和原 `expected_assessment_sha256`。重新核验后附加不可变凭据，保留原 diagnostic 三文件；过期/伪造摘要或未确认拒绝。
- `POST .../certification/verify`：从当前数据库及恢复范围取得实际 book_id，只读复验归档；原作品目录不可用也不改为打开来源或重建凭据。

页面明确说明同机 Windows、相同构建、单部已登记作品；可写恢复仍需原工程、原 DB、本机凭证和最新用量核对。来源丢失或最新用量未知仍不能启用写入。页面和结果均不授权模型调用、格式升级或写资格转移。此功能没有把主计划的完整可写恢复目标改成只读恢复。

签发前消耗当前确认；失败、刷新归档状态、作品/归档切换、维护失效和登记完成都会清除旧认证预览/勾选。刷新页面不会自动重放签发。作者七类基线与正式升级仍各自未完成，`OnboardingPreviewService.can_upgrade` 保持 false。

## 本地测试

| 原执行 | 终态 | 结果 |
|---|---|---|
| 29415 | exit 0 | 新增 5 项，87.85 秒；实际公共登记后预览/签发/不可变重试/来源暂不可用复验，未确认/错误摘要/越权字段/会话/Origin/Host/维护/来源后改拒绝 |
| 24735 | exit 1 | 既有 43 项中 42 通过、1 失败，456.34 秒；软件变更场景仍按旧契约期待 HTTP 200，被已有启动版本保护正确拒绝 409 |
| corrected | exit 0 | 六种旧来源变化场景全部通过；仅修正软件变更应要求重启的测试预期，未改生产行为 |

不同新增 5 项、相关既有 43 项通过；六场景重跑与原批重叠的五项不重复计数。详细 XML、日志、源码摘要与最终指纹复核见 `.tmp/t71-public-certification-progress.json`。

前一组件的大纲/章节完成历史 34 新增及 51 相关检查在指纹 `ef11a39a...` 下通过，详见 `t71-outline-completion-history.md`；本轮没有改那些验证器。旧证书不会被默认为兼容新构建，实际签发均用本轮构建重新生成的归档。

## 浏览器验证结果

状态：**通过所列交互；保留原驱动审计断言失败及独立复核说明**。

环境：Chrome DevTools MCP，`http://127.0.0.1:65505/`，隔离浏览器 context；合成 Strict 测试书、实际首次登记、禁用 worker/importer，两家 SDK 构造禁用，网络仅允许 loopback。身份校准为合成测试证据，不代表真实作者作品已校准。

操作与结果：

1. 原生点击进入维护、打开归档、选择已登记后归档、核对认证。HTTP 200，确认框可用而签发按钮在勾选前不可用。
2. 勾选后在隔离样本增添诊断 note，再原生点击签发：req22 HTTP 409，响应“认证依据已偏离预览”；勾选清除、签发禁用、没有凭据。原数据文件不变。
3. 删除该 note 后，原目录 stat 已变化，原生重新核对 req27 返回 HTTP 200 的明确 source_changed 阻断。未篡改清单或放宽检查；通过原生预览/保存按钮取得新归档 req29/31 HTTP 200。
4. 选择新归档、核对、勾选，再点击“刷新归档状态”：勾选清除、签发禁用，无签发请求。重新核对 req39、明确签发 req41、手动复验 req43 均 HTTP 200。
5. 整页 reload 后仅出现 bootstrap/projects/maintenance 读取，没有认证自动重放。重新打开面板、选择同一归档、手动复验 req63 HTTP 200，认证 SHA 与 req41 相同，确认仍未勾选、签发禁用。
6. 原生结束维护，关闭本次 page2，保留原 about:blank。原服务 PID30204 已结束，65505 端口关闭。

控制台：初始和 reload 后 warn/error 均空。故障阶段唯一 console error 为预期 req22 的 HTTP 409 资源提示；没有 JavaScript 异常或未处理 Promise。网络请求体核对了实际维护 ID、当前评估摘要和 `confirmed:true`；预览/签发/复验响应保留 `write_activation_allowed=false`、`automatic_provider_calls_allowed=false` 和原来源丢失不支持写启用的策略。

证据：`.tmp/t71-public-certification-browser-http.json` 保存经检查的 req22/39/41/43/63 请求/响应（不含 cookie）；页面截图已原生查看，MCP 保存图片路径被其 workspace 配置拒绝，未绕过该限制。原驱动及独立审计见 `.tmp/t71-public-certification-browser-after.json`、`-audit.json`、`-audit.log`。

原驱动 96898 **exit 1**：其终态断言要求整个 book inventory 与故障前完全相等；本次显式增删文件探针使 Windows 根目录 size 从 0 到 4096、modified_ns 改变，所以该断言失败。独立审计 **exit 0** 确认唯一差异为 root_identity 及其派生 inventory hash；全部文件记录和目录清单相等，根 device/inode 相同、SQL/软件相同、原登记 active/generation1、worker/外部调用为零。新归档的原诊断用途保留，认证在关闭服务后再次独立复验成功。未把原驱动改写为通过或声称目录 stat 未变。

## 剩余工作

作者七类基线确认、过期失效与正式升级；原来源/机器凭据丢失和未知最新用量的受支持可写恢复；可信金额止损、独立真实收益与完整 E/F。真实 Provider、真实作品写入和远端发送均未授权/未执行；正式接受数量不因本组件用例增加而改变。
