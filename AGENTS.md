# AGENTS.md

## 项目

本仓库是一个 Windows 专用 Python GUI 启动器,用于管理 Codex workspaces 和 Codex chat threads。

主要文件:

- `codex-cli-startup.py`: PySide6 主程序和 thread repository 逻辑
- `codex-cli-startup.pyw`: windowed Python 入口
- `setup_env.py`: 项目 `.venv` 准备脚本
- `build_wrapper.py`: 轻量 exe wrapper 构建入口
- `codex-cli-startup.exe`: 已入库的轻量源码启动 wrapper
- `codex-cli-startup.dll`,`codex-cli-startup.deps.json`,`codex-cli-startup.runtimeconfig.json`: wrapper 运行所需的 .NET sidecar files
- `assets/codex-cli-startup.ico`: Windows executable icon
- `assets/codex-cli-startup.png`: source app icon image
- `launcher_wrapper/`: 轻量 exe wrapper 的 C# 源码
- `build_exe.py`: PyInstaller 构建入口
- `codex-cli-startup.spec`: PyInstaller spec
- `codex-cli-startup_config.json`: 本地启动器配置
- `requirements.txt`: runtime dependencies
- `requirements-build.txt`: build dependencies

## 沟通

- 除非用户明确要求,否则使用中文回复。
- 应用内 UI labels 和 user-facing text 保持英文。
- 生成的文字和注释使用半角标点。
- 引用本地文件时优先使用 Markdown link。

## 代码风格

- 改动保持小而聚焦,遵循现有单文件 PySide6 结构。
- 优先使用 standard library APIs 和项目中已有的 PySide6 widgets。
- 注释和 docstrings 使用英文。
- 新增或修改 public functions/methods 时,保留简洁 docstring,并包含 `@param` 和 `@returns`。
- 避免无关 refactor 和格式化噪音。

## 环境与依赖

- 项目依赖必须安装到仓库根目录的 `.venv`,不要安装到宿主 Python 环境。
- 首次准备或刷新 runtime dependencies 时运行:

```powershell
python setup_env.py
```

- 需要构建 exe 时运行:

```powershell
python setup_env.py --build
```

- 运行,验证和构建命令默认使用 `.venv\Scripts\python.exe`,除非任务明确要求验证宿主 Python 行为。
- `requirements-build.txt` 中的 `Pillow` 仅用于生成和检查 app icon assets,不属于 runtime dependency。
- 双击源码版 GUI 时优先使用已入库的 `codex-cli-startup.exe`; 该 wrapper 会通过 `.venv\Scripts\pythonw.exe` 启动 `codex-cli-startup.py`,如果 `.venv` 不存在则回退到 `pythonw.exe`。
- `codex-cli-startup.exe` 依赖同目录的 `codex-cli-startup.dll`,`codex-cli-startup.deps.json`,`codex-cli-startup.runtimeconfig.json`,更新 wrapper 时需要一起入库。
- 重新生成轻量 wrapper exe 时运行:

```powershell
.\.venv\Scripts\python.exe build_wrapper.py
```

## UI 规则

- 当前 UI 状态下不适用的控件应隐藏,不要长期显示为 disabled buttons。
- `New Thread` 放在右上 toolbar,因为它只依赖选中一个具体 workspace。
- 依赖选中 thread 的操作放在底部 action row。
- thread table 的 `source` 列只显示 `CLI` 或 `Non-CLI`。
- thread table 固定展示 interactive chats; 除非真正实现新 view,否则不要重新加入可见的 `View = Chats` 控件。
- 选中的 thread row 应保持清晰可辨: 使用轻微整行高亮,并且只在整行最左侧显示一个 accent bar。

## Codex 数据安全

- 测试或探索脚本不得修改用户真实 `%USERPROFILE%\.codex` 下的 Codex state。
- 涉及 archive,unarchive,delete threads 的 repository 方法必须使用临时 fake `CODEX_HOME` 和临时 SQLite `state_5.sqlite` 测试。
- archive/delete 行为必须限定在解析出来的 `CODEX_HOME` 内。
- active rollout files 位于 `sessions/YYYY/MM/DD`。
- archived rollout files 位于 `archived_sessions`。
- 只有 archived threads 允许删除。
- active 或疑似 active threads 不允许 archive;测试时需要覆盖 live `logs_2.sqlite` process pid 和近期 rollout mtime 两类 guard。
- fork active warning 只允许基于 live `logs_2.sqlite` process pid,不要使用 rollout mtime。

## 验证

普通代码改动运行:

```powershell
.\.venv\Scripts\python.exe -m py_compile codex-cli-startup.py codex-cli-startup.pyw build_exe.py setup_env.py build_wrapper.py
```

GUI 行为改动还需要使用:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
```

然后用临时 config,临时 fake Codex home,以及最小临时 SQLite database 初始化 `MainWindow`。

archive/delete 改动需要包含以下 smoke coverage:

- `archive_thread`
- `unarchive_thread`
- `delete_archived_thread`
- `delete_archived_threads` scoped to one workspace
- `delete_archived_threads` scoped to all workspaces
- active archive guard for live log process and recent rollout mtime
- fork active warning uses live log process only

## 构建

构建 exe:

```powershell
python setup_env.py --build
.\.venv\Scripts\python.exe build_exe.py
```

预期输出:

- `dist/codex-cli-startup.exe`

不要提交生成的 `build/`,`dist/`,`.launcher_wrapper_publish/` 目录。轻量 wrapper 的 `codex-cli-startup.exe`,`codex-cli-startup.dll`,`codex-cli-startup.deps.json`,`codex-cli-startup.runtimeconfig.json` 是例外,需要跟随 wrapper 源码更新并入库。除非有意修改 PyInstaller 配置,否则不要改动 `codex-cli-startup.spec`。

## PowerShell

- 优先在当前 shell 中直接执行 PowerShell 命令。
- 除非需要独立进程语义,否则不要再包一层 PowerShell 进程。
- 如果新增或修改任何 `.ps1` 脚本,必须运行 `Invoke-ScriptAnalyzer -Path <path> -Recurse -Severity Warning,Error` 并修复所有 warnings/errors。如果未安装 `PSScriptAnalyzer`,需要告知用户。
