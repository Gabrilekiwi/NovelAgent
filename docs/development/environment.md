# 开发环境与B0基线

本轮执行入口：[最终计划](D:/Pycharm/PythonProject/NovelAgent/docs/development-plan-final-2026-09-08.md)。进度入口：[开发交接](D:/Pycharm/PythonProject/NovelAgent/docs/development-handoff.md)。

## B0

基线代码提交：`090d0aa191f7aae96ea1c979b6725d7f87d811ae`，父提交`fbf6b8ffbd7385bbdd35a33ba8f6f74b88973e8f`。43个相互依赖的代码、schema、测试与工作台文件一起保存为可运行基线，按目录分组清单见[b0-manifest.json](D:/Pycharm/PythonProject/NovelAgent/docs/development/b0-manifest.json)。该提交不代表新生产流程已经验收完成。

原始48个工作文件（含5份计划/审查文档）的逐字节归档：`.tmp/development-b0/working-files.zip`；tracked差异：`.tmp/development-b0/tracked.patch`。归档逐文件SHA256已与清单核验。Git保存规范化文本，归档保留原始换行；未把正式书稿、数据库、.env或密钥加入本次基线。

以后验证升级前行为应直接使用B0版本与本轮锁定环境，不修改旧程序来适配新格式。原有1905项测试通过等成绩来源于9月7日审查；环境变更后的检查另行记录。

## 安装与检查

Windows使用CPython 3.12，PowerShell中在仓库根目录运行：

```powershell
py -3.12 scripts/setup_environment.py
.venv/Scripts/python.exe -B scripts/check_environment.py --dev
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe -B -m pytest -q
.venv/Scripts/python.exe -B scripts/smoke_v1.py --skip-tests
```

没有`py`启动器时，用已安装的Python 3.12绝对路径运行setup脚本。Linux使用`python3.12 scripts/setup_environment.py`，之后将解释器替换为`.venv/bin/python`。脚本只创建或更新仓库`.venv`，不安装到全局环境、不启动服务、不调用Provider。已有虚拟环境若版本错误，脚本停止并报告原因；先确认目录用途，再另存旧环境并重建，不能直接覆盖未知数据。

只安装运行依赖可加`--runtime-only`。Windows双击启动器会核对Python、虚拟环境和运行依赖版本；不满足时显示修复命令。首次安装需要包源网络，常规启动只做本地版本检查。

当前固定30个运行依赖及5个开发附加依赖，来自已有通过测试环境的项目依赖闭包，不包含全局环境中的无关包。`colorama`仅Windows安装。锁定版本可以减少依赖升级导致的环境漂移；这是版本锁，尚不是带全部分发包哈希的离线安装包。[pip可重复安装文档](https://pip.pypa.io/en/stable/topics/repeatable-installs/)

`requirements.txt`和`requirements-dev.txt`保留顶层允许范围；`requirements.lock`和`requirements-dev.lock`为安装入口。更新依赖时在隔离Python 3.12环境中按允许范围解析，检查直接及传递依赖，更新两个锁及原因，执行`pip check`、相关测试和smoke，再通过CI验证。不要将整个开发者全局环境的`pip freeze`原样覆盖项目锁。

Ubuntu CI已接入相同锁与项目环境；Windows发布及平台场景完整验收属于T7.3，不能用本机环境检查代替尚未运行的远端CI。

## 中断续接

按[任务模板](D:/Pycharm/PythonProject/NovelAgent/docs/development/task-template.md)更新交接。测试进程仍活时先读取原session结果；终止时记录退出码。每个子任务只运行相关检查，代码未变不重复完整基线审查。检查器只读取包元数据，不读取.env，不接触作品或数据库。
