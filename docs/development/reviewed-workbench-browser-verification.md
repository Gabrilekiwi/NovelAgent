# Browser verification result

Status: passed for the main workbench navigation, author publication handoff and recovery paths tested. Full T3.2 and automatic mode remain incomplete.

## Changed behavior tested

- 新版作品书架 → 概览 → 继续逐章写作 → 章节卡中的已确认事实 → 发布页 → 返回正式章节。
- 部分 apply 的新版作品可以显示概览、暂停进度并恢复原事务。
- 已发布章节在没有该章节本地提交 key 的主工作台中从正式凭证读取。

## Browser environment

- Chrome 152，Chrome DevTools MCP；独立 context `main-reviewed-workflow`。
- URL `http://127.0.0.1:8876/`；实际 FastAPI app，worker与active-book导入均关闭。
- 正常样本 `.tmp/iu3/7078bac8bdb4/b`；恢复样本 `.tmp/iu3/f378c3dcd5ff/b`。
- 真实升级、SQLite与本机所有权台账；正文与事实来自合成fixture，无真实Provider费用或真实作品写入。

## Actions performed / observed UI

1. 点击书架新版作品，概览显示已审核事实、已写第1章、下一章第2章。
2. 点击继续写作，进入第2章；切换章节卡，显示真实草稿与作者确认的两项事实。
3. 初次发现发布链接被旧readonly footer样式隐藏，移到审核结果下方；刷新后实际点击“预览并发布本章”，正确打开带真实project/chapter/review参数的发布页。
4. 点击生成预览、勾选作者确认、点击发布；返回 `commit_73c520731b3b3ffa9b8b1b429e3ec049` 的成功凭证。
5. 点击“返回本章”，主工作台显示已提交与正式正文，只请求chapter详情，不再请求已提交旧章的生成card/Candidate/事实数据。
6. 换用真实 after_apply_target(0) 中断样本，概览仍正常打开，显示待核验进度和恢复原章节事务按钮。
7. 服务重启后未刷新页面的第一次恢复被403会话令牌拒绝，页面提示刷新；刷新后取得新会话，点击恢复成功。提示变为已核验，下一章恢复为2，恢复按钮消失。

## Console/runtime check

正常书架、概览、章节、发布、返回和恢复后无控制台错误或未处理异常。服务重启后的旧会话仅有与预期403一一对应的资源错误，刷新后不再出现。没有请求循环或隐蔽重复发布。

## Network check

- 书架 req6 GET projects → 200；新版overview/health/chapters → 200。
- 第2章 req13/15/16/17/18/19/20 依次为chapter、card、Candidate集合及详情、extraction集合、review集合及详情，全部200；真实审核详情含服务器构造publication_url。
- 发布页 req49 POST preview → 200，req50 POST publication → 200；网络工具直接读取成功响应 `publication_verified=true`、chapter=2及上述run_id。
- 返回本章 req59 GET chapter → 200；无重新生成请求。正式正文/receipt绑定另由自动化测试精确核验。
- 恢复样本 req65 POST recovery → 403，响应code=session_token_required；刷新后req77 → 200，请求正文严格为 `author_confirmed=true`，响应 `recovery_verified=true, publication_authorized=false, next_chapter=2, publications=[]`。
- 恢复后req78–81 project/health/chapters全部200。磁盘独立核验 next_chapter=2、章节receipt=0、pending=0。
- 概览初次及恢复后各有两次有限project GET（现有loadProject/overview更新），不是循环；没有重复POST。

## Additional checks

本轮主工作台相关57个不同自动化用例按最新结果均通过；新增6个含完整API生成→提取→作者确认→发布、不确定Provider处理、所有权阻断、恢复与正式读取/导出。候选和事实提取使用可验证持久记录的假Provider，网络IO为0。浏览器本轮没有点击模型生成按钮，生成/提取/确认完整链由实际API测试覆盖。

## Evidence / limitations

精确过程、snapshot与请求编号记录于本报告；XML/hash和进程终态见stage-c-progress。截图工具被自动审批以“workspace out of credits”拒绝，未绕过，**没有本轮主工作台截图证据**；交互、可见a11y树、控制台、网络均已核验。未再次尝试先前被拒绝的关页操作。

自动生产、任务历史、规划、润色等新版能力仍待对应阶段接入，界面依据capabilities隐藏未支持入口；不代表从计划中移除。全量T3.2 pytest/smoke、两模式和真实作品验收未完成。真实升级/接入/备份门保持。
