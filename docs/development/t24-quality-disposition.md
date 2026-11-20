# T2.4：质量分级与正文依据组件

2026-11-04。当前组件经过本地与实际浏览器验证，**整个 T2.4、自动修复、D/E/F 尚未验收**。总计划保持原范围。没有真实 Provider 调用、真实作品写入或新增正式接受。

实际 `publication-preview` 现在返回独立的 `quality_disposition`。策略及正文、准备记录、原质量记录分别绑定版本和 SHA；原质量结果仍按原人工检查及发布事务规则处理。这份处置结果的修复、自动接受和发布授权始终为 false。

| 情况 | 自动处置建议 |
|---|---|
| 必要检查缺失、状态未通过、检查失败、未知语义问题或证据无效 | 暂停，作者核对 |
| 明确列出的重复类问题，原完整性生产者给出有效的两处正文范围 | 进入一次修正评估；执行仍需独立的持久化资格和预算 |
| 无事实或连贯性影响的已列明篇幅建议 | 留下记录，可继续 |

支持的重复代码为 duplicate_scene_text、near_duplicate_scene_text、duplicate_opening、duplicate_ending_hook；repetition_or_stalling 仅在实际完整性证据匹配时关联。不能凭消息、摘要、模糊评分或客户端自报结果授予资格。新策略没有接入修复执行，也没有自动替作者确认。

正文摘录使用完整最终正文 SHA、Unicode 字符区间和精确子串，每处最多 512 字符。非法、越界、布尔类型冒充整数、同一位置冒充两处、重新计算外层 SHA 后的伪造证据，均不能取得自动修复建议。页面通过 textContent 显示原文。语义检查的摘录只证明本次检查实际评估的文字，不证明语义推断本身正确。

Reviewed 路径显式启用原文依据，旧调用默认关闭。叙事规则仅沿其实际 quality_check_codes 映射携带对应失败/警告检查的摘录，不从说明文字生成引文。浏览器曾发现规则转述丢失摘录，最终已修正并由真实 API 与定向用例覆盖。

## 保留的离线失败及修正范围

第一版 44 项通过后，新增离线样例实际发现：两段短正文只替换“手电光”为“灯光”，低于原近似重复检查的 60 个规范化字符下限，自动建议错误地继续。原失败日志 `.tmp/t24-quality-disposition-final.log`、XML 和 `.tmp/t24-offline-policy-evaluation-first-failed.json` 保留：1 个分级不匹配、1 个引文不匹配、1 个漏检。没有删除样例或调低原完整性门槛。

独立的 short_prose_repetition_scope_v1 风险探针检查 32–59 个规范化字符的行，相似度至少 0.92；最多 64 个候选、8 对结果，摘录上限 512。超过支持上限就记录不完整并暂停。它补充两处精确原文供人判断，**不改变原完整性判定、人工质量门禁或旧报告**。对话及标点不能借原始字符长度绕过检查。

原短近似重复样例现在从“继续”变为“暂停人工核对”，而非直接获得修复资格；新增足够长度的近似重复样例仍由原完整性检查定位并给出单次修正评估建议。修改期望值反映证据强度的区别，原检测缺口继续单独记录。

最终离线样例共 11 项，分级和摘录不匹配均为 0。仅 4 个刻意构造、独立带检测标签的本地样例统计到联合检查误报 0、漏检 0；原完整性检查的短近似重复漏检仍列入 known_integrity_gate_misses。额外模型调用 0。受控语义检查输出只验证分流，不测量模型或语义生产者准确率；这些小样本不能证明真实阅读质量、普遍低误报或上线效果。

## 自动检查证据

最终构建：`f1f29d171ce8c6b1b9959b4c3e0b2ab4f9f517181f47c11d526a16b3a31afbdb`，完整清单 `.tmp/t24-quality-current-source-third.json`。

原 session 89958 明确 exit 0：**81 passed，197.80 秒**。包含新分级与证据 30 项、原章质量 9 项、质量决策 5 项、审查流水线 9 项、候选绑定 13 项、发布 API 10 项、预算 UI 5 项。逐项 journal 已核对全部 81 个独立节点 setup/call/teardown 和最终 sessionfinish 0，XML 无错误、失败或跳过。测试前后源指纹一致；生产及测试文件在运行时冻结。

文件：`.tmp/t24-quality-third.log`、`.tmp/t24-quality-third.xml`、`.tmp/t24-quality-third-events.jsonl`。旧构建 44 项和中间构建 75 项通过仅作为历史证据，不与最终 81 项相加。

## Browser verification result

Status: passed（限问题展示、质量失败阻断与只读刷新路径）。使用 browser-change-verifier skill，通过原生 Chrome DevTools MCP 操作。

- Browser environment：Windows Chrome 156，127.0.0.1:8894/publication，合成项目 project_6fdd664fbeec412aace44d0d83a8ef78，第 2 章。真实 SDK 禁止、后台 worker 关闭；fixture 只有一次确定性生成。
- Actions performed：原生点击“生成提交预览”，再点击“重新读取审核与预算”。没有点击发布或执行修复。
- Observed UI result：原完整性重复显示两处正文 1291–1351、1354–1414；短疑似重复同时显示 1417–1476 的变体；衔接检查与对应叙事规则都显示 1–512 的实际摘录。整体提示作者核对，质量失败时“确认发布本章”保持禁用。刷新后清空旧预览与确认。
- Console/runtime check：基线、预览后、刷新后均无 console 消息或运行时异常。
- Network check：实际 POST publication-preview 200，JSON 请求包含原 review/CAS 哈希；正文预览、质量展示与该响应一致。刷新只有 publication-input 与 budget GET 200，共 1 次预览 POST，无发布或修复请求。响应 no-store；DevTools 正文输出在 80674 字节处被截断，未声称捕获完整 disposition；完整返回结构及原文匹配由真实 API 测试核对。
- Additional checks：本章仅 1 个质量区间，6.734 秒；只读刷新前后 time.json 字节一致；章账本 Provider calls 0，无发布 Intent、无第 2 章正式正文。
- Evidence：`.tmp/t24-disposition-browser-third-state.json`、`.tmp/t24-browser-third-audit.json`，本聊天原生快照 page 3 / uid 5_12–5_38、请求 10–12。Chrome 将快照文件写入仓库路径拒绝为不在其配置根，使用工具正常返回的内联快照观察；未更换路径绕过拒绝。
- Remaining risks：无真实模型、真实作者阅读、自动修复或完整自动批次验证。金额表单之前的自动审批 409 仍为 partial，未重试或绕过。

原服务 session 85453 已显式 Ctrl-C，exit 1；中间服务 77207 同样已退出。当前无本组件活动测试或服务。完整状态见 `.tmp/t24-quality-disposition-progress.json`。

## 后续工作

T2.4 还需要更广的叙事/语义生产者证据覆盖和代表性误报/漏检评估。自动批次须冻结策略及授权；T6.2 须在执行前持久化消耗一次资格，预算覆盖修复及完整事实重提取、重检，并证明重启不增发额度、修复失败不开始下一章。当前 reviewed Candidate 明确零自动修复，旧 worker/scene repair 不能跨 v3 身份路径直接复用。

继续完成 T5.2 剩余恢复/模型/修复计时范围、T7.1 最终审核和经授权的 T8.1。不能将本组件验证等同于整个开发计划或上线准备完成。
