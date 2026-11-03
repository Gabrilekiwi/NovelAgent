# T3.2 验收记录

2026-09-10。依据 `docs/development-plan-final-2026-09-08.md` v1.0；T3.2已按本节范围完成验收；阶段C及完整A–F目标仍未完成。代码在工作树，尚未提交。

## 范围与证据

| 最终计划要求 | 实际实现与验证入口 | 当前证据 |
|---|---|---|
| 服务端绑定作品、章节、最终规范化正文、事实修订、read-set、权威版本及幂等键 | `chapter_commits.py`、`chapter_commit_prepare.py`、`chapter_commit_request.py`；`test_chapter_commit_prepare.py`、`test_workbench_chapter_commits.py`、`test_chapter_commit_index.py` | 组件及最终全量回归通过 |
| 工作台预览和作者确认提交 | 主工作台明确的Reviewed路由→事实确认链接→publication页；`test_reviewed_workbench_workflow.py`、`test_chapter_publication_api.py` | 实际主API及浏览器交互通过；报告见下文 |
| 双击仅一个逻辑提交、同键不同内容冲突 | `test_two_concurrent_commits_publish_one_logical_request`；API同请求重试；浏览器双击网络记录 | 组件回归通过；浏览器一次POST且一份提交 |
| 改稿、手写来源变化、章号占用拒绝旧计划 | `test_changed_author_draft_is_rejected_inside_commit_transaction`、`test_source_changes_after_staging_are_rechecked_before_file_commit`、`test_occupied_chapter_rejects_old_read_set` | 组件回归通过；实际来源变化HTTP409 |
| 正文、当前状态、游标和凭证进入正式事务 | `reviewed_chapter_commit.py`、`reviewed_chapter_transition.py`；`test_actual_v3_chapter_commits_and_next_context_cold_reads_full_state` | 实际v2.1事务与下一章冷读通过 |
| 标记前失败不推进；标记后完成原事务恢复 | `test_actual_v3_power_loss_reconciles_original_transaction`、`test_service_recovers_partial_state_before_reading_current_identity`、`test_lost_response_or_process_retries_original_request` | 实际故障注入通过；浏览器部分apply回滚与丢失响应查询通过 |
| 文件已提交、DB索引缺失时由凭证重建，不重新生成 | `test_fresh_process_rebuilds_missing_commit_index_without_preparing_or_generating`、`test_cold_process_rebuild_uses_archive_without_candidate_or_draft_loading` | 新进程重建通过；DB原生草稿另行持久化 |
| 恢复失败不得换键绕过；损坏证据不得隐藏 | `test_persisted_recovery_failure_blocks_repeated_and_new_requests`、`test_unregistered_damaged_journal_cannot_be_skipped_by_new_key`及receipt/quality篡改测试 | 两处实际复现的恢复缺口已修复并回归；阻断证据保留 |
| EA已有行为相关回归 | `test_event_authority_runtime.py`、`test_event_authority_persistence_barrier.py`、`test_executor_event_v2.py`、`test_persistence_v21.py`及既有集成测试 | 最终全量覆盖这四个文件及其余既有测试，无失败；相关EA覆盖44项 |
| 全套pytest与本地smoke | `.tmp/stage-c-t32-full.xml` / `.log`；`.tmp/stage-c-t32-smoke.log` | 最终session77718 exit0：2266通过、578子测试通过、8跳过；修复后smoke exit0。首次7失败/1清理错误及修复后116项结果保留 |

浏览器证据：`chapter-publication-browser-verification.md`、`reviewed-workbench-browser-verification.md`。主页面截图因自动审批额度不足未取得；实际交互、DOM、console及network证据已记录。所有演练在隔离书和DB上运行，未调用真实Provider，未写真实作品。

## 阶段顺序

T3.2通过完整验收后推进T2.2稳定身份声明、已有别名与缺失基线处理；再按T2.3、T4.1、T5.1、T4.2推进阶段C。两章及四类重启点的完整浏览器演练属于T4.2；自动接受策略、批次授权和自动worker共享提交适配属于阶段E/T6。此前续接记录误把这些作为T3.2下一项，已纠正，不改变两模式最终交付范围。

真实作品升级仍需T7.1备份、接入预检和有效Strict校准。当前测试夹具的合成资格不能证明真实校准。

## 最终结果与剩余范围

最终XML `.tmp/stage-c-t32-full-final.xml`，日志同名前缀；修复后smoke `.tmp/stage-c-t32-smoke-final.log`。8项跳过：6项Windows符号链接权限限制、1项需显式开启的50章长测、1项需要费用授权的真实Provider测试。它们没有被计为通过。

T2.2身份独立组件在本轮全量收集之后新增，另有20项通过，尚未进入主流程；不能用其通过结果证明T2.2完成。已按原计划转入T2.2，后续C–F仍待推进。
