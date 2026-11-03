# T7.1 嵌套引用与 JSON 产物图

日期：2026-09-11。状态：组件已验证；完整可恢复认证仍未完成。累计 142 项 T7.1 新增不同用例。

原范围预检仅查看 config/result 中六个顶层文件字段，会遗漏 compact result 保留的 persistence、publication、memory_outbox、writeback/artifacts 及数组中的引用。新增 `backup_references.py` 遍历这些声明，并接入任务配置、结果、全部事件、已知最终运行记录和已收集的子进程 stdout.json。

文件与目录分别验证。相对暂存/前像路径只按当前事务 journal_path 解析；指定根 UUID 的 PathRef 仅按已有、已核验且已纳入来源清单的 root registry 解析，歧义和未知根拒绝。已声明 JSON 产物加入引用图继续扫描，每份文件最多解析一次，支持循环引用；读取前后与原清单大小/哈希绑定。已提供的不可变产物 SHA-256 也必须相符。所有文件解析仅限已有清单，不因记录提供新路径而读取新根。

正文字符串不作为 JSON 再解析，逻辑字段路径与文件路径区分。没有分类的引用、结构不全、缺文件/目录、凭据排除项、上级路径和图/递归数量或体积超限均成为明确阻断项，不返回截断成功。本次覆盖不是所有历史格式与全部工具的完整引用契约；归档仍固定 diagnostic/recoverable=false。

验证：

- `.tmp/stage-d-t71-reference-graph-final.xml` / `.log`：79 项通过，73.10 秒，session 2426 exit 0；含当时 16 个引用用例与归档/历史/范围回归。
- 后补结构损坏 `.tmp/stage-d-t71-reference-shape-final.xml` / `.log`：20 项通过，3.78 秒，exit 0；其中新增 4 项结构不全/未知引用检查。累计为此前 122 + 本组件 20 = 142，不重复累计。
- `.tmp/stage-d-t71-d3-reference-probe.json`：既有 D3 合成执行器样本的 117 个直接元数据引用通过，作品清单未变。
- `.tmp/stage-d-t71-d3-scope-graph-probe.json`：同一样本执行完整只读预检，包含 JSON 图的 130 个引用，无阻断。来源目录清单比较为 false，原因查证如下，不能宣称整个目录无变化。
- `.tmp/stage-d-t71-d3-scope-graph-source-check.json`：mode=ro SQLite 连接在合成 source 目录新建 0 字节 workbench.db-wal 与 32768 字节 workbench.db-shm。复核原 D3 proof，作品全部文件与目录相符，数据库逻辑摘要相符；附属文件保留，没有清理旧实验资料。后续读取确定离线且空 WAL 的样本使用 immutable 连接；不把 immutable 用于活跃工作台数据库。

无 Provider 调用，无真实书写入；测试均终态，无活动服务。当前开始外部写入口审计：main 的 init-runtime/force-init-runtime 直接调用未持锁的 `init_runtime_state`；legacy 身份激活仅持 `.novelagent/authority/cas` 与身份状态锁，而备份来源尚未持有这两类锁。需先补这两处，并用真实独立进程证明竞争被拒绝；不能仅以现有 canonical runs/Provider 竞争测试宣称覆盖全部 CLI。
