# Browser verification result

Status: passed（持久恢复会话控制组件）；完整恢复产品仍未通过。

日期：2026-10-02。冻结完整软件指纹：`46f95edceb5055c5fcfdb57cf2afa52992957912570c152dd21eebc49d5520e8`。

## Changed behavior tested

维护动作失败后读取真实状态，恢复控件保持可用；会话选择器随步骤更新中文标签；恢复流程保留正文、作者草稿和预算，明确交接后启用，无自动步骤或模型调用。

## Browser environment

- URL：`http://127.0.0.1:38898/?verification=v3`。
- Chrome，通过 DevTools MCP 原生点击、表单选择与页面刷新。
- 独立合成作品、SQLite、私有机器账本；SDK 构造器及四类执行器禁止调用，仅允许 loopback 网络。
- 实际服务 session27516 / PID49536；全部动作后正常 exit0。没有真实作品写入。

## Actions performed

1. 打开恢复区域，进入恢复维护，选择归档和独立目录，预览并建立会话。
2. 原生点击结束维护，收到预期409；未点击任何刷新按钮，直接取消恢复。
3. 建立第二个会话，依次复制、绑定、准备只读数据库；原生选择并查看正式第二章及作者草稿。
4. 核对交接，明确勾选确认，转移原写资格；逐步保留消费、预算、历史任务及执行目录，明确确认启用。
5. 结束原维护，整页刷新并选择原启用会话，检查两种中文终态和零自动执行。

## Observed UI result

- 未完成会话的维护退出被拒绝后，取消和下一步仍可用；维护状态仍为暂停编辑和新任务。
- 取消会话立即显示“已取消并保留副本”；步骤标签逐次更新，启用后显示“已启用”，刷新后仍正确。
- 正式第二章1289字符，SHA `6368997d68d3a2e19d4c3e8386a943bee3a37ed57967dd011ffe3af8135af8e4`。
- 作者草稿11字符，SHA `9a3a90b54a2e59d5b8a9342cb17754113e4ea3fcc0e89dfe2f8c3c54deb31680`。两者均由原生选择和查看后对实际只读文本框计算SHA，内容与来源一致。

## Console/runtime check

基线无 error/warn。负向退出产生一条预期409资源错误，响应为明确的恢复会话未结束提示。正向流程无其他错误；最终刷新后 console 无 error/warn。

## Network check

- req215：`POST /api/maintenance/finish` →409，未完成会话拒绝；req216：自动 `GET /api/maintenance` →200，`quiescent`、`purpose=restore`，原 operation_id 未变。
- 原生取消、两次预览、会话建立、复制、绑定、数据库准备、三次资料读取、交接/消费/预算/任务/执行/启用均200。
- req266启用响应：`phase=active`、`activated=true`、`pending_action=null`、`next_action=null`、消费证据已结算、`automatic_provider_calls_allowed=false`、`recoverable=false`。
- req269结束维护200，`state=idle`。刷新后的10个相关GET均200，无自动步骤POST、无pending。
- 仅保存过滤后的响应体与网络摘要，没有保存 Cookie、Authorization 或原始请求头。

## Additional checks

最新原session79153 exit0，19项会话/生命周期/边界专项通过957.23秒；原24611 exit0，70项相关回归通过134.98秒。旧v2的7项复制续接测试对应未变化的复制生产文件，单列版本依据，未冒充最新完整软件指纹下的运行。

v2另已验证原控制器正常停止后新进程继续原维护与原会话，并明确启用；未知消费页面禁用启用按钮、允许明确回退，原3次调用记录和原账本SHA保留。v3本轮未重复这两条未变化的页面路径，其证据按版本分别保存。

## Evidence

`.tmp/t72-recovery-session-browser-v3-evidence.json` 保存原生动作、页面快照、实际文本框SHA、过滤响应体与console/network结果；`*-before.json`、`*-info.json`、`*-after.json` 保存独立来源基线和退出核对。退出报告确认原书与完整SQL逻辑不变、worker不存在、SDK/执行器调用为空、完整软件指纹不变。

## Remaining risks

本组件验收不替代完整T7.2：直接打开启用目标、已恢复来源再次恢复的控制器、正常作品登记、最终可恢复认证尚待完成。金额控制、自动模式、Windows发布及真实质量/成本/用时验证也未完成。v1缺失终态报告、v2三项幂等失败的原证据全部保留。
