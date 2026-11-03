# T7.1 受管历史发现修复

日期：2026-09-12。当前包仍为 diagnostic / recoverable=false。

修复了两种相反的发现错误：仅凭任意目录段名 `transactions` / `journals` 会误拒普通作者资料；仅从剩余日志或 root_registry 寻找 v2 控制根又会漏掉只剩终态登记的残缺历史。

`backup_history_discovery.py` 现在一次索引锁定清单，按保留的 `.novelagent/runtime` 命名空间、`.persistence.lock` / `root_registry.json` 控制标记、可识别 v1/v2 manifest 或 registry 记录确认受管根。普通作者同名目录不产生事务；识别出的受管命名空间接受完整校验，缺日志、非目录的 registry、未知状态子项、非终态和矛盾登记均拒绝。识别不是 schema 验证通过，损坏机器记录不能因为识别后被当作空目录而放行。

自定义根没有任何控制标记，且所有机器证据已彻底消失时，无法从普通资料推断其原来存在。该限制不会被包装成完整历史证明；实际必要根仍须由受支持布局或已保留的来源引用确定。没有扫描外部来源，也没有修改旧历史或 PathGuard。

测试：新增 `tests/test_backup_history_discovery.py`。独立首跑 13 项为 5 通过 / 8 失败，证实两个原缺陷；产品修复后与既有 v1 组合 31 项通过（session31962 exit0，10.30秒）。随后独立只读复核指出 registry 自身的类型和未知子项漏查，已补实现及 4 个反例。最终 112 项组合全部通过（session38365 exit0，481.04秒）；日志 `.tmp/stage-d-t71-history-discovery-final.log` / XML同名。新增17项不同用例已验收，T7.1累计262项。后续离线读取接口改动以其专项证据为准。

## 浏览器验证

状态：通过。Chrome 152 / DevTools page6，隔离上下文 `t71-history-discovery`，URL `http://127.0.0.1:38885`。独立合成样书，禁止非本地网络和 Provider 构造，worker / auto-import 均关闭。

1. 原生进入维护、展开诊断归档、点击预览；作品内两种普通作者目录均保留，req19 返回 200、scope_checks_passed=true；页面显示 8 文件 / 17 目录、范围通过。
2. 仅在本次合成样书写入可识别的 `custom-control/registry/completed/lost.json`，没有 journal、lock 或 root_registry；原生重新预览 req21 返回 200、scope_checks_passed=false、identity_or_history_unverified，页面显示 1 个问题。
3. 浏览器刷新、重新展开和预览 req40，仍检出同一问题，摘要一致。原始正向结果未被当作刷新后的成功状态。初始及最终 console 均为空，相关网络无重复 POST 或隐藏 5xx。
4. 原生结束维护后停止合成服务器：session48434 exit1（Ctrl+C）、PID25628 已退出、38885 无监听。没有生成新归档或启用恢复，没有真实作品写入和真实 Provider 调用。

证据：`.tmp/t71-discovery-browser-evidence.json`（本地会话头已脱敏）、`.tmp/t71-discovery-browser-info.json`、`.tmp/t71_discovery_browser.py`。原有 page4/page5 及其归档证据未改动。本轮未重验归档保存/恢复；上轮保存/校验结果见既有归档文档。
