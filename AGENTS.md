# AGENTS.md

## 项目

本仓库是一个 Windows 专用 Python GUI 启动器,用于管理 Codex workspaces 和 Codex chat threads。

主要文件:

- `launcher.py`: PySide6 主程序和 thread repository 逻辑
- `launcher.pyw`: windowed Python 入口
- `build_exe.py`: PyInstaller 构建入口
- `CodexWorkspaceLauncher.spec`: PyInstaller spec
- `launcher_config.json`: 本地启动器配置
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
python -m py_compile launcher.py launcher.pyw build_exe.py
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
python -m pip install -r requirements-build.txt
python build_exe.py
```

预期输出:

- `dist/CodexWorkspaceLauncher.exe`

不要提交生成的 `build/`,`dist/` 目录。除非有意修改 PyInstaller 配置,否则不要改动 `CodexWorkspaceLauncher.spec`。

## PowerShell

- 优先在当前 shell 中直接执行 PowerShell 命令。
- 除非需要独立进程语义,否则不要再包一层 PowerShell 进程。
- 如果新增或修改任何 `.ps1` 脚本,必须运行 `Invoke-ScriptAnalyzer -Path <path> -Recurse -Severity Warning,Error` 并修复所有 warnings/errors。如果未安装 `PSScriptAnalyzer`,需要告知用户。
