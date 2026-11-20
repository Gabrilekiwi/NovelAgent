# T6.1：批次最终质量阶段与持久处置

2026-11-05，批次最终质量与持久处置内部组件已验证。完整 T6、正式自动发布/readback、后续章和一次修复仍未完成；没有真实 Provider、真实作品写入或新增正式接受。

## 实现与范围

`workbench/reviewed_policy_quality.py` 读取原生成/完整提取 typed 结果和控制链 finish 证明，核对实际 Candidate、响应凭据、完整初始稿 revision/SHA、当前提取上下文和原 grant。quality 步骤输入包含这些完整来源；不能只拿客户端“检查通过”布尔值或一份结果 SHA 推进。

阶段复用已有 `ReviewedPolicyReviewService` 的 policy 事实准备、最终质量和处置生产器。原 `ChapterExecutionBudget` 的本地步骤统一计时，禁止任何模型发送；不是在自动步骤内另开一个计时区间。新报告保存完整 policy review、preparation、quality/disposition 和原提取结果绑定；磁盘读取验证原 typed preparation 和处置重算，只有 continue 才要求完整发布质量门槛。repair_once 只表示有原文证据的修复建议，不消耗修复资格、不发修复调用；当前暂停等待后续修复/人工适配器。

开始检查与保存结果前都再次检查原步骤许可，作者暂停/停止会阻止新结果落盘和阶段推进。作者改稿会使输入核对失败，保留原稿。只有真正 continue、主动时间已解释且未超限才调用原 finish_step 进入 publication。其他处置通过服务器 `pause_step` 降低权限，保留原 pending 身份，不传 author_confirmed=true 冒充作者操作。

结果落盘后的冷进程仍无执行许可；明确继续才重跑完整本地质量，与原完整报告比较，仅忽略生产器实际评估时间戳和由此改变的 quality hash，不重写原结果。重启发生于区间关闭前，原 time.json 保持 unknown；可本地核对原报告，但不得因此继续发布。新服务的 get 只读，既不重检也不新建许可。

## 实际故障及修复

首轮原 74459 exit 1，1 fail / 62.18 秒，`.tmp/t61-policy-quality-first.log/.xml/events`：质量服务在原步骤已持有机器作品独占 lease 时再次 `_owned_project`，Windows 正确拒绝，抛 InstanceAlreadyRunning。没有通过放宽锁或重复启动绕过。

现原活跃 PolicyStepPermit 仅在当前线程、原服务/作品和未关闭 scope 内借用原持有的 owner；内层取得新的 Provider/SQL 边界，前后核对同一 registration、ownership、原已结清事务和活跃许可。离开原 scope 后借用函数也因 permit 关闭拒绝。原人工质量服务没有活跃 permit 时仍走原完整 owned 边界。

第二轮原 60077 明确 exit 0，47 passed / 1078.48 秒，构建 5c21d4716b7ebb5244a2ca9f73a727f3741698a9f10f17a7acb22c2f43832378，`.tmp/t61-policy-quality-second-source.json`。新实际场景包括模型调用被本地质量 scope 拒绝、policy continue、人工可放行警告仍自动暂停、真实重复正文 repair_once 暂停/第三章零调用、作者改稿、生产器中暂停、两处真实 os._exit86 后冷恢复与原时间字节保持；另含既有 policy 审核与控制回归。

补查 `.tmp/t61-policy-quality-invalid-hash-before.log` 保存实际问题：便携质量结果读取未验证 input_sha256/extraction_result_sha256 的字符串类型，重算外层 hash 后可接受 bool；真正执行另有原输入比较而不能据此推进。现结果读取严格核对所有 SHA 类型；补反例覆盖。

完成步骤机器记录写入、作品副本尚未保存时，普通 get/新 resume 因原控制副本不同拒绝，不能直接重新跑质量步骤。新增内部明确 recover_finish：必须已存在相同键、原 finish payload/step/result 与原 pending 证明；真实来源/结果及当前时间通过后只续办原副本。不追加 finish、不创建 handle，并撤销本实例旧 handle。即使重复续办，也先验实际原证据。没有原完成命令时，不能从质量结果文件创建新进度。

最终推进前在持有原作品边界时再次核对时间，防止质量计算后其他本地工作耗尽额度仍写入推进。此类变化会拒绝完成，保留原 pending 和完整报告；全流程驱动的统一失败转暂停仍需后续接入。

最终原 50929 明确 exit 0，28 passed / 700.31 秒。包括：5 个质量场景（含新增真实 os._exit87 双副本中断、完成前真实注入901秒本地耗时、已关闭scope拒绝借用、SHA类型损坏及原两处os._exit86）、21 policy审核与2调用边界回归。源 7f700b3351a9ea25240e7a37423644db39b247756f3238061644607320a7eeb6，`.tmp/t61-policy-quality-final-source.json`。不能用第二轮全部通过代替最终构建的证据。

`.tmp/t61-policy-quality-audit.json` 已复核两轮 collected、全部 setup/call/teardown、sessionfinish 0 和 XML；当前完整生产指纹、最终全部测试 SHA、第二轮未变化的 policy/control 测试 SHA 均一致。第二轮到最终仅原控制本地续办与质量类型/时间复核两个文件变化。49 个不同 pytest 节点只作不同构建的历史覆盖索引，最终构建实际重跑 28 项；旧失败轮和重复通过不累计。

独立 `.tmp/t61-policy-quality-no-new-finish.json` 在真实已完成的最终构建合成作品中证明：即使结果存在，缺原完成命令也拒绝进入结果回调，原结果与两份控制字节不变、无新 handle。该诊断独立列出，不加进 pytest 节点计数。

生产和测试运行期间冻结；原测试句柄均已观察终态，当前本组件无活动测试或服务。详细结果见 `.tmp/t61-policy-quality-progress.json`。此组件无公开新 API/UI，未声称浏览器自动批次已经验收。

## 下一步

原发布 Intent/事务/索引恢复及正式 readback 必须接原 publication/readback 步骤，下一章只能从实际前章正式状态构造上下文。完整一次修复需在原账本消耗资格并全量重提取、重检；作者接管、明确继续、三章/失败零下一章和最终流程恢复/Windows 发布仍待完成。原 D 真实效果验证和 F 发布验收不因内部组件通过而完成。
