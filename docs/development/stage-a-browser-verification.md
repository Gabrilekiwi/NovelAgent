# 阶段 A 浏览器验证

日期：2026-09-08。状态：通过，范围为 T1.2 新建作品的保存目录错误及重试。

## 环境

- Windows，Chrome 152，Chrome DevTools MCP，隔离 context `stage-a-verification`。
- 地址 `http://127.0.0.1:58976/`；命令为 `.venv/Scripts/python.exe -B run_workbench.py --port 0 --no-browser --no-active-import --data-dir .tmp/stage-a/browser-data --workspace .tmp/stage-a/browser-workspace`。
- 数据库、作品均在 `.tmp/stage-a`；没有生产任务、Provider 调用或正式书籍修改。

## 实际操作与结果

1. 打开书架，点击新建作品，填写“阶段A 隔离验证”、科幻、简介。
2. 将保存父目录填写为隔离 workspace 下不存在的 `不存在的目录`，点击提交。
3. 页面显示“保存父目录不存在，请选择已有文件夹后重试。”；弹窗保留名称、题材、简介和路径。
4. 只将保存父目录改为已存在的隔离 workspace，再次提交，进入作品概览，显示原名称、简介、0 章及下一章为第 1 章。
5. 强制刷新页面，作品信息仍在，详情、健康和章节列表均能加载。

## Console 与 Network

- 初始 console 无消息；负向提交只有一条与预期 HTTP 400 对应的资源错误，没有未捕获 JavaScript 异常；刷新后 console 无消息。
- 请求 8：`POST /api/projects` → 400，响应为 `error.code=project_registry_error`，message 为上述中文提示。
- 请求 9：同一路径 → 201；请求只更改 `root_path`，名称、题材、语言、简介完全保留。响应 project id 为 `project_b697ada0b8d647d7aa5b3f7d0a70c7e7`，路径为隔离 workspace 下的“阶段A 隔离验证”，metadata 与表单一致。
- 刷新后的请求 20、21、23–26 均为 200；不存在隐藏的 500、CORS 失败或请求循环。项目详情在入口和概览各读取一次。
- 原始 DOM、网络、console 证据：`.tmp/stage-a/browser-evidence.json`，会话 cookie 已去除。

## 收尾与限制

隔离标签页已关闭。验证服务 PID 34396 经实例标识和活动进程核对后停止，工具 session 66437 已返回终止退出码 1。读取 Win32_Process 命令行曾被本地权限拒绝，未据此执行停止；随后使用已知实例标识、精确 PID 与解释器路径完成核对。未操作系统原有 pythonw 进程。

本记录不代表候选、事实审核、正式提交和恢复等后续网页闭环已验收。T1.1 的并发启动、父死子活和监控异常采用独立实际进程及故障注入测试。
