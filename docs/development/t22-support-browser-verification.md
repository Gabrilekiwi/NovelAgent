# T2.2 发布预览浏览器验证

2026-09-10，Chrome DevTools实际页面3，隔离context `t22-support`。所有数据在`.tmp/iu3/3fb247895399`，没有真实Provider调用或真实作品写入。

准备：沿用实际升级及机器登记的测试夹具，确定性Candidate产生正文；新草稿增加赵原/北门/铜钥匙片段，使用实际审核服务保存三项声明和明确库存基线0，再确认审核。补充输入在服务器夹具中完成；本次浏览器不冒称已经验证T2.3的补充表单。

项目`project_25f94bced255474d883831d33b5eb86d`，审核`fact_review_a9dc159c0d2b9070ed73d7a5438cbdbc`，草稿修订2/审核修订3。测试服务session91739、PID3408已通过Ctrl+C停止，退出码1为主动终止。页面3暂保留；未重试此前被额度拒绝的截图/关页操作。

实际操作与观察：

1. 打开publication页，读取已确认审核。点击“生成提交预览”，req8 POST publication-preview为200。质量四项通过；正文尚未发布。
2. DOM显示真实事实proposal和逐字原文引用；“作者补充的身份与初始情况”可见赵原、北门、铜钥匙、初始数量0及作者说明，不再显示空payload/evidence。
3. 点击作者确认复选框并双击“确认发布本章”，仅有一次publication请求req9，HTTP200。页面显示第2章已发布，提交按钮禁用，凭证`commit_ac475a598aed2eac82f19e31198d28a0`。
4. 刷新：req14 POST publication-status为200，随后req16 GET publication-input为200，显示同一提交凭证，未再次发布。console始终无消息。
5. 独立读取磁盘并执行完整reviewed identity/history核验：next_chapter=3；三个实体名称/ID保留，赵原持有铜钥匙数量1，last_transition保留原作者声明与初始库存0。

页面快照证明操作与文本内容；未取得新截图，不声称完成截图版式验收。下一步T2.3需把补充输入与消歧做成作者可用的表单，并验证整个审核操作，不用本次发布预览替代。
