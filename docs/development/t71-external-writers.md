# T7.1 已发现的外部写入口修复

日期：2026-09-11。T7.1 累计 152 项新增不同用例；当前仍无认证可恢复备份。

## 修改与实际边界

- `init_runtime_state` 原来在没有写锁时复制 snapshot 和 memory；现在整个初始化/强制覆盖过程持有原运行目录锁及两个目标状态锁。保留原复制和不覆盖行为，没有将两文件复制改称原子事务；复制失败后的恢复策略仍按已有初始化语义处理。
- `recover_latest_chapter_draft` 原来直接输出恢复草稿；现在读取候选到输出结束都持有运行目录和章节目录状态锁。缺少运行目录仍报错，不因恢复请求而新建运行根。
- `locked_backup_source` 新增 legacy 身份 authority/cas 锁及身份文件状态锁，并重读身份；canonical runs 锁同时覆盖作品根、snapshot、两种默认 memory 位置和章节目录的共享状态锁。独立运行目录也不能绕过这些目标的状态锁。
- 备份在 EA 根原先不存在时按既有锁组件预先建立并持有全局锁；已存在的根仍要求原武装锁，不能静默重建。
- 真实进程竞争测试发现 `authority_persistence._barrier_layout` 在取得全局锁前创建状态登记目录，导致虽然写操作最终被拒绝，源清单仍发生变化。已将这些目录的初始化移到两个公共 EA 写入/恢复入口成功取得全局锁之后；持锁失败不会创建这些登记目录。

这些修复证明下列入口在快照持锁期间被阻止，不意味着维护预览前后所有 CLI 均受工作台 HTTP 准入管理。仍须完整核对其余写入口、自定义根、脚本/库入口与加载代码证据；不能据此把 external_cli_full_coverage_certified 改为 true。

## 验证

- `.tmp/stage-d-t71-external-writer-final.xml` / `.log`：65 项通过、3 子测试通过，71.62 秒，session 89451 exit 0。覆盖 10 项新增外部写入口用例，以及来源锁、归档、EA barrier、草稿恢复、初始化与身份激活回归。
- 真实 `main.main` 独立进程执行 force-init-runtime 和 activate-story-state：备份持锁时准确因写锁失败，文件清单不变；退出维护后再次运行成功，分别覆盖状态初始化和 Strict 身份激活。网络连接被替身阻断，全部为合成书，校准报告不代表真实作品资格。
- 独立 run_dir 竞争身份、作品根、snapshot、memory 状态锁均拒绝；初始化的每次实际 copyfile 期间，两个状态目标均不能被另一进程取得锁。
- 草稿恢复、首次 EA 写入和 EA 全局恢复均在产生输出/登记目录之前被拒绝，来源清单不变。
- `.tmp/stage-d-t71-cli-writer-regression.xml` / `.log`：既有 CLI init/recover/activate 8 项通过，50 deselected，0.56 秒，exit 0。
- 最初两项失败仅因新测试未创建 runtime 父目录，已修正；后续首次 EA 竞争失败揭示了上述真实先写后锁问题，已修复并完成回归。失败日志保留，不能当作通过证据。

全部测试终态，无活动测试或网页服务。本轮未改产品页面，无新的浏览器结论。未调用真实 Provider、未改真实作品。旧浏览器创建表单自动审批 credits 拒绝、并行审查 credits 中断仍留待验。

## 下一步

完成其余外部入口审计，尤其 review dashboard 文件输出、delivery/autonomy 子入口、自定义运行根和旧迁移命令；当前已观察 `core/review/dashboard.py` 的输出函数直接写文件，尚未与快照锁协调。继续完整根/历史认证和产品归档 API/UI，再完成 T7.2 恢复后的新绑定与同机交接。T5.2 金额证据和完整 A–F 仍未完成。
