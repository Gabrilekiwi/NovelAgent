# 工作台单实例与执行所有权

阶段 A / T1.1，2026-09-08。

## 边界与选择

每个工作台数据目录有一个操作系统所有权锁。应用工厂先取锁，再初始化数据库、分类中断任务、导入活动书和启动 worker。测试使用 `start_worker=False` 也必须取锁。锁不依赖 PID 文件、端口探测或 SQLite 中的运行标记。

Windows 使用 `CreateFileW` 以读写访问、只共享读打开 `workbench.lock`，后续读写打开冲突；锁文件保持原位。执行子进程通过明确的可继承 handle list 取得同一个句柄。父进程退出后，执行进程的句柄继续阻止新实例，直至最后一个句柄关闭。API 语义依据 [Microsoft CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) 和 [Python 3.12 subprocess](https://docs.python.org/3.12/library/subprocess.html#subprocess.STARTUPINFO)。POSIX 分支使用 `flock` 与 `pass_fds`，关闭时不显式解除共享锁，也不删除锁文件；本轮实际平台为 Windows。

本轮采用执行子进程继承持锁方案，没有引入新的 Windows 进程管理框架。测试通过真正的 `JobSupervisor` 启动边界运行本地 Python 假执行者，使用子进程心跳证明父进程强制退出后仍活着。虚拟环境启动器 PID 与脚本实际 PID 可能不同，测试以脚本写出的实际 PID 核对。

## 地址与关闭

`workbench-instance.json` 只是地址发现记录。重复启动仅接受本机 HTTP 地址，必须实时请求 `/api/instance` 并核对本次随机实例标识；旧记录或不能验证的地址不能成为接管依据。持锁但服务不可访问时返回中文诊断与退出码 2，保持任务原状。

启动时直接 bind/listen 并把原 socket 交给 Uvicorn，避免先探测空闲端口再绑定的竞争。默认端口已占用时尝试后续端口，最后由系统分配；所有地址仅监听 127.0.0.1。

正常退出等待 worker 结束；未确认线程或执行子进程退出时保留所有权。关闭后的应用请求返回 503。应用构造失败释放本次所有权；没有进入 lifespan 的隔离应用通过 finalizer 关闭自身 supervisor 和锁。

## 监控异常

独立审查发现原监控循环会在异常时无条件忘记子进程，worker 随后可继续调度。现在遇到监控异常立即停止调度，尝试终止、强制结束并等待执行进程；任务记为需要恢复，Provider 状态未知。数据库同时失效时保留原运行记录供重启分类，不转为普通可重试失败。

只有确认本地进程退出才清除活动句柄。无法确认时，启动/关闭均拒绝释放所有权或继续调度；同一 supervisor 也不能启动第二个执行者。本地退出不推断远端请求是否完成或收费，恢复仍检查 Intent/Receipt。

## 验证与后续

验证结果统一记录在 `stage-a-progress.json`：并发两个启动只保留一个服务；再次启动不修改 running 任务；端口被独立监听器占用时自动换端口；父进程被强制结束后旧执行者持锁；执行者退出后任务恢复为 needs_recovery，不自动重跑；监控故障保持不确定状态并阻止后续调度。

本任务保护同一数据目录及其执行进程。不同数据目录登记同一作品、恢复副本的 book_id 所有权交接属于 T7，不能把当前锁宣称为已完成作品副本协调。EA 的新工作流对等、真实 Provider 生产效果也不在本项验证范围内。
