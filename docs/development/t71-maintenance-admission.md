# T7.1 维护写入准入接入

日期：2026-09-11。状态：本接入检查点通过；完整T7.1备份尚未完成。

## 已实现行为

`MaintenanceRoute`作为工作台默认API路由类，覆盖所有POST/PUT/PATCH/DELETE以及后续按默认路由类注册的写入口。现有会话/同源检查继续在外层执行；维护拒绝返回423及`workbench_maintenance_active`，普通GET可以读取。`/api/maintenance`只读返回状态和在途写数量。尚无产品维护开始/结束按钮或备份控制接口，测试通过服务端协调器显式进入/退出。

HTTP实际handler由保留强引用的独立Task执行，外层等待使用shield。浏览器取消请求时，底层`asyncio.to_thread`不会被误当作结束；许可等实际handler完成才释放。应用正常shutdown等待这些独立任务，关闭OS实例所有权前再次检查在途写入。此实现仍依赖各handler不私自派发无人持有的后台写线程；当前入口通过实际调用测试。

JobSupervisor在认领任务之前获得相同协调器的worker许可，覆盖任务与子进程收尾。维护中不认领、不中止既有写入。监控异常或`poll()`不能证明子进程结束时保存未释放许可；只有后续权威poll确认退出，才能释放并允许取得静止状态。实例锁及子进程继承机制保留。

维护请求跨重启保留；app的旧规划/润色状态恢复、自动导入以及worker的重启恢复均延后。显式resume保持维护，finish后下一次合法写操作/worker许可内才执行DeferredStartup，线程锁保证只执行一次。构造服务时既有schema初始化仍沿用原行为；归档预检需要独立核对版本和SQLite/文件一致性，不能将此协调器当作完整备份认证。

## 验证

- 原维护协调器9项通过；新增`tests/test_maintenance_integration.py`6项通过（.tmp/stage-d-t71-admission.xml，2.98秒）。覆盖所有产品写路由的统一类型、真实HTTP423/GET200、取消真实HTTP后`to_thread`仍完成、维护跨实例重启延后恢复、后台认领暂停与已有任务收尾、真实本地子进程存活时许可保留。
- 初始相关网页/费用API/维护回归29通过，.tmp/stage-d-t71-integration-smoke.xml，session28878 exit0，21.59秒。
- 最终后台任务、OS实例、预算API/服务及维护回归66通过、1跳过，.tmp/stage-d-t71-admission-regression.xml，session47538 exit0，86.81秒。跳过的是旧provenance符号链接测试，本Windows账户创建文件symlink缺少权限；没有将其标记通过。
- JS语法与本次diff空白检查通过。浏览器之后仅移除测试文件未使用的import，未改业务实现。T7.1当前新增不同用例累计15项。

## 浏览器验证结果

状态：通过。

- 环境：Chrome DevTools MCP page2，isolatedContext `t71-maintenance`，本地8884。新合成作品project_27237b758a6447eca3ef91ee2547f8d2，`.tmp/iu3/42b93eb8a69c/b`。独立脚本`.tmp/t71-maintenance-browser-server.py`，真实SDK与生成入口全部拒绝外部调用。
- 操作：正常读取章节卡；本地测试控制文件发起维护后，原生点击章意图确认并确认弹窗；刷新页面，再填入作者备注并确认；本地显式结束维护，重复同一表单提交；最后刷新读取。
- 发现并修正：首次后端正确返回维护423，但旧`workbench.js`把所有423误显示为另一个写任务。增加维护error.code优先分支；原有其他423提示保持。
- 结果：修正后req35 POST card/confirm 423显示“工作台正在维护，暂停新任务和编辑”，作者备注仍保留；维护状态GET200显示quiescent、active_writes=0。结束后req36 POST200返回confirmed=true且包含原作者备注，界面显示“当前输入已确认”“章节意图已确认”。此时维护GET200为idle且在途0。
- Console：故障期间仅对应预期423的资源错误；最终刷新为空，无JS异常。最后各只读GET均200，未出现自动模型请求。
- 数据：全过程合成作品所有文件哈希与验证前一致，未新增执行/模型文件。维护记录位于合成数据库的数据目录。真实作品未写入，真实Provider调用0。
- 进程：浏览器服务session7131/PID6580已停止，Stop-Process后session终态exit1；全部测试终态，无本轮运行中进程。

## 下一步与界限

实现目标作品及必要外部runtime的预检、来源/版本/排除项清单、受控文件和整份SQLite快照、归档最终凭据与只读验证。备份控制入口需要拥有独立的维护/快照许可，避免把自身算作待排空写请求，也必须阻止复制期间退出维护。作品所有权→Provider→数据库等锁序仍须与现有入口兼容；GET中为一致读取取得的控制锁也需考虑。

当前后台许可覆盖整个既有会话，维护会等其结束；尚未专门实现长批次在下一个章节边界为备份让出许可。CLI/其他进程的目标文件写入需要靠实际作品/Provider/持久化锁及预检覆盖，不能只看HTTP计数。没有取得一致快照、没有备份包和恢复证明，不能宣称已经完成T7.1或T7.2。
