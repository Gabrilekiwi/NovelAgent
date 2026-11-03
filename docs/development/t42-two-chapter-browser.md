# T4.2 两章与断点恢复浏览器演练

2026-09-10，已完成工作树验收。使用真实工作台 UI、SQLite、正式文件事务和确定性假 Provider；无真实作品或付费调用。完整 A–F 目标仍未完成。

## 隔离环境

- Chrome page 8，context `t42-two-chapters`，端口 8881。
- 同一作品 `.tmp/iu3/32a483c06ecb/b`；同目录 `workbench.db`、本机登记 `m`。
- project `project_e9d4c351583d4088aee8c2c12654ef3f`；基线第 1 章，演练第 2、3 章。
- `.tmp/t42-browser-server.py --new` 仅初始化一次，随后无参数重开原库。第三、四章章纲在升级前准备；不预建候选或事实审核。
- `.tmp/t42-browser-metrics.json` 持久化实际假 Provider 调用；生成器从实际 input pack 读取章号，校验实际权威状态并输出对应正文。第 2 章警戒值 0→2，第 3 章应 2→3。

## 已取得证据

1. **生成后重启**：PID 21148/session 84796 结束，新 PID 6656/session 28010；浏览器刷新恢复同一 candidate `candidate_1f44161dba010d3cd4ec4af9af4140d1e787a25b` 与 draft `candidate_draft_40ff617e506941d8823305e9e0eac91d`，r1、SHA `8a4eb83e20a9ecddea6f43ac8f437ee84297b1929c9e7291c8b638d648f87d58` 不变，生成一次。
2. **编辑后重启**：通过 Chrome 原生 fill_form 修改正文“先听了片刻，才回应门后的低语”，点击保存，PUT req36/200。PID 6656 结束，新 PID 30004/session 90088；刷新仍为 r2、SHA `42e32ab779bb0993c78db5bd8158021809f04340a3eea525560bd75e2cebf8de`，修改保留。此前旧样本填写被额度审批拒绝；本次隔离样本的原生填写成功，未使用 JS/API 代填。
3. **检查后重启**：事实提取 `fact_extraction_34029f6bb6775c709e04d99f7523ec8fea926f0a`，两项明确接受并确认；review `fact_review_4dc39aafa34fdccba0c3eb022dd358e7` r2。提交预览四项本地检查通过。PID 30004 结束，新 PID 4808/session 17332；原发布页刷新 req73/200 查询原请求，正确显示尚未提交。草稿/审核 r2、修改正文与原 key `ac7678a6-1e0a-452d-96b4-0474a7ff8789` 保留，再预览通过。
4. **提交中断后重启**：双击确认发布只有 req78 一次 POST。注入 `after_commit_marker` 后 `os._exit(42)`；终端 session 17332 返回终态 exit1，PID 4808 不存在，故障文件和实际 commit.marker 均存在。浏览器 req78 `ERR_CONNECTION_RESET` 为预期故障，console 只有该错误。
5. 新 PID 29068/session 13699 以同库重开，先导航作品概览；普通 GET 只展示“存在尚未恢复的章节事务”，未自动恢复。点击“恢复原章节事务”，req93/200 `recovery_verified=true`、next_chapter=3，原 run `commit_ee0acbf7de7aa91ff8bd857977ad84b7`。回到原发布页，req102/200 查询同请求并显示同凭证，发布按钮禁用，可开始下一章；console 为空。此时生成/事实调用仍各一次。

浏览器发现并修正一处静态提示：已有候选时仍显示“尚未生成正文候选”，现改为指向下方实际候选/草稿进度。行为验证随后续正常页面重载进行，不为文案新增镜像测试。

## 第二章连续执行与最终验收

从原发布页“开始下一章”进入第 3 章，确认章意图、生成、提取、接受两项、保存并确认，再预览和发布，均由实际 UI 完成。candidate `candidate_0c0da48943369679806ef89f44df789617ac4eb4`；draft `candidate_draft_2319b0587f9044d3a5e0c4ad85baa68e` r1；review `fact_review_0a92461be710dc9cbfacbba8a8dd9c9a` r2。实际 input pack 的 `Authoritative State` 包含警戒值 2 和上一章事件；表单显示变化前 2、本章 +1。req120 生成、req123 提取、req135 预览、req136 发布均 200。第 3 章 run `commit_77a58eff49887d9c32caaad48e9d293c`；刷新 req141 查询同凭证，按钮禁用，无重复提交。点击下一章进入第 4 章待确认意图。

正常流程及最后刷新 console 为空，发布页没有水平溢出。唯一故障 console 为明确注入的连接中断。修正后的章意图提示在第 3 章实际页面中显示正确。

最后 session 13699/PID 29068 已停止；其余 4 个服务器实例均终态。新 Python 进程只读核验完整历史、提交请求/质量证据和 SQLite：两章各一份 publication，索引内容与已核验凭证完全相等；3 份凭证包括升级基线和 2 次章节提交；两个 journal completed，pending 为空。第 2 章作者修改保留，草稿 r2/r1、审核 r2/r2 及 SHA 与提交请求一致。第 4 章冷读警戒值 3、两个事件，其余未改人物/关系/名单/库存/位置与基线一致。假 Provider 总共生成 2 次、提取 2 次，重启/恢复没有增加调用；真实调用 0。

证据 `.tmp/stage-c-t42-browser-cold.json`、`.tmp/t42-cold-verify.py`、`.tmp/t42-observations.json`、`.tmp/t42-browser-metrics.json`。冷读脚本第一次因 Windows 默认 GBK 读取中文 manifest 失败，已明确 UTF-8 后通过，保留初始及修正日志。静态回归首次 22 通过、1 项旧文案断言失败；删除过时断言后该项通过，总计当前 23 个不同测试通过，新增测试 0。XML 为 `.tmp/stage-c-t42-static.xml` 和 `.tmp/stage-c-t42-static-corrected.xml`。git diff --check 无空白错误（有既有 LF/CRLF 提示）。按主计划不在此重复全套 pytest；上一全量在 T3.2，后续全量在 T6 完整集成与最终候选点。

本演练验证工程闭环、持久恢复和状态传递，不证明模型文学质量、真实使用收益或完整产品恢复。T4.2 与阶段 C 已验收；下一项阶段 D 的 T5.2，T7.1/真实验证、阶段 E–F 仍未完成。
