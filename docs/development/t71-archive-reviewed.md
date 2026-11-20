# T7.1 归档 reviewed 业务状态核验

日期：2026-09-12。组件49项定向回归及普通页面回归通过；完整离线恢复尚未验收。

`core/story_project/reviewed_state_store.py` 私有历史读取器现可接收显式证据读取接口；默认公共读流程仍使用本地路径安全检查和原 publication receipt 验证。状态链、初始锚点、升级前后身份/校准/准备凭证、正文归档、七类事实转换和 typed chapter bundle 核验规则共用，未全局替换 PathGuard 或文件API。

`workbench/backup_archive_reviewed.py` 的归档读取器只按已验证成员清单解释原来源坐标，不访问源目录。先核验统一v1/v2事务链，publication必须来自已验证completed v2.1事务，回执定位、manifest/marker/registry、发布内容摘要和Final RunRecord指针绑定后，才执行原reviewed业务规则。结果固定restore_plan_ready=false / recoverable=false / write_activation_allowed=false。

7项新测试与原升级/正式章节回归共49项全部通过（session62204 exit0，536.43秒），`.tmp/stage-d-t71-archive-reviewed-first.log` / XML同名。正例要求原源目录消失、默认live reader禁用后与实际普通冷读结果相同；5个反例用额外的正式v1事务改写状态/质量/基线正文或新增孤立状态/正文别名，证明通用文件事务链本身完整仍不能替代reviewed语义合法性。另检查显式目标。原升级、提交前/后中断及原事务恢复回归一起通过；没有运行全套。新增7项不同用例，T7.1累计300项。

## 普通页面与网络回归

状态：通过。Chrome152 / DevTools page7，独立上下文 `t71-reviewed-offline-regression`，URL `http://127.0.0.1:38886`。新合成样书由真实旧事务→升级→新章构造，仅1次确定性本地生成，真实Provider为0，worker与auto-import关闭。它验证共享核心改动后的普通live入口，不是把离线组件冒称已经接入网页。

原生进入维护、展开诊断归档并预览：req19 200、scope通过，69文件/42目录。仅在本次样书将 `追踪/已审核事实.md` 换成损坏文本，再预览req21 200、scope=false、identity_or_history_unverified，页面提示1项问题。恢复原字节、刷新网页重新展开预览req40 200、scope再次通过；预览摘要与最初一致。原文件SHA256恢复为 `47ff1e18546455a9d693e8b41136b9dfa1dcfb3997c9cb01b9e2b72412bf4e61`。初始与含保留记录的最终console均为空，相关POST无重复或隐藏5xx。结束维护req42 200。

服务session99014/PID31444已Ctrl+C停止（exit1），38886无监听。证据 `.tmp/t71-reviewed-browser-evidence.json`、`.tmp/t71-reviewed-browser-info.json`、`.tmp/t71-reviewed-state-original.md`、`.tmp/t71_reviewed_browser.py`。没有改动既有浏览器样本或真实作品，没有创建新归档或启用恢复。

仍待：归档中当前人工来源、完整文件引用、预算/调用与辅助状态的统一离线核验；实际恢复位置与新物理绑定计划、其他作品访问隔离、机器所有权交接与最终下一章验收。此组件不能替代T7.1认证或T7.2恢复启用。
