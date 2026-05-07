# Codex Workspace Launcher

这是一个 Windows 专用的 Python GUI 启动器,用于管理常用工作区,并一键恢复 Codex chat threads。

## 安装

```powershell
python setup_env.py
```

依赖会安装到仓库根目录的 `.venv`,不要安装到宿主 Python 环境。
`requirements.txt` 固定为 `PySide6>=6.7,<7`。
选择这个范围是因为它兼容当前 `Python 3.13.6`, API 已经稳定,同时避免锁到未来可能带来兼容变化的大版本。

## 启动

```powershell
.\.venv\Scripts\python.exe launcher.py
```

Windows 下如果你不想看到额外的空白控制台窗口,优先使用:

```powershell
.\.venv\Scripts\pythonw.exe launcher.pyw
```

也可以直接双击 [launcher.pyw](G:\Project\codex-cli-startup\launcher.pyw)。

## 构建 exe

如果需要无 Python 命令行的 GUI 入口,先安装打包依赖:

```powershell
python setup_env.py --build
```

然后执行:

```powershell
.\.venv\Scripts\python.exe build_exe.py
```

生成的入口位于 [dist/CodexWorkspaceLauncher.exe](G:\Project\codex-cli-startup\dist\CodexWorkspaceLauncher.exe)。

exe 使用 `--windowed` 模式,启动时不会显示控制台窗口。打包后配置文件会读写在 exe 同目录的 `launcher_config.json`,方便把 `dist` 目录整体挪到固定位置后直接双击运行。

## 配置文件位置

配置文件保存在入口所在目录的 `launcher_config.json`。
源码运行时对应仓库根目录的 [launcher_config.json](G:\Project\codex-cli-startup\launcher_config.json),exe 运行时对应 exe 同目录的 `launcher_config.json`。

结构包含:

- `workspaces`: 常用工作区列表,元素结构为 `{ "name": "...", "path": "..." }`
- `terminal`: 固定写为 `wt`
- `ui_state`: 保存最近选中的工作区或 `All`、`Archived only` 状态、窗口尺寸、分栏宽度和表格列宽

如果配置文件不存在,启动器会在首次启动时自动创建默认配置。

## 线程来源与匹配规则

- 线程数据直接读取 Codex 的本地 SQLite 索引 `state_5.sqlite`
- 路径解析优先级:
  - 先读 `CODEX_HOME`
  - 如果未设置,则使用 Windows 的 `USERPROFILE\\.codex`
  - 再退回 `Path.home() / ".codex"`
- 使用 SQLite `mode=ro` 只读连接
- `Archived only` 关闭时只展示 `archived=0` 的 threads
- `Archived only` 开启时只展示 `archived=1` 的 threads
- 左侧工作区列表第一项固定为 `All`,用于接近 `codex resume --all` 的跨目录视图,不按 `cwd` 过滤
- 选择具体工作区时,启动器会对工作区路径和线程里的 `cwd` 做绝对路径、规范化和大小写归一化后再匹配
- 线程列表固定展示 Codex interactive chat sources: `cli`,`vscode`,`codex`,`atlas`,`chatgpt`
- 子代理线程始终过滤掉,不会出现在列表中
- 子代理判定优先读取 `threads.source`,并用 `thread_spawn_edges.child_thread_id` 做二次校验
- 列表按 `updated_at` 倒序展示

表格展示字段:

- `title`
- `updated_at`
- `source`: 显示为 `CLI` 或 `Non-CLI`

选中线程时,表格会使用整行高亮,并在首列左侧显示竖向 accent bar。

选中线程后,右下方详情面板展示:

- `cwd`
- `model`
- `thread id`
- `first_user_message`

## 启动行为

- `Resume Selected`: 通过 `Windows Terminal` 新开独立窗口,在其中执行 `codex -C <thread cwd> resume <thread_id>`
- `Fork Selected`: 通过 `Windows Terminal` 新开独立窗口,在其中执行 `codex -C <thread cwd> fork <thread_id>`;如果 `logs_2.sqlite` 显示该 thread 仍有 live Codex process,会先提示确认,但允许继续
- `Archive Selected`: 将非归档线程的 rollout 移入 `archived_sessions`,并更新 SQLite 归档状态
- `Unarchive Selected`: 将归档线程的 rollout 从 `archived_sessions` 移回 `sessions/YYYY/MM/DD`,并更新 SQLite 归档状态
- `Delete Selected`: 永久删除已归档线程的 rollout 和 SQLite 记录,仅对归档线程启用
- `Delete All Archived`: 在当前左侧 `All` 或具体 workspace 范围内,永久删除当前 `Archived only` 列表里的全部归档线程
- `New Thread`: 通过 `Windows Terminal` 新开独立窗口,在其中执行 `codex -C <workspace>`
- 选中左侧 `All` 时不能新建线程,只能恢复已有线程
- 归档线程不能直接 resume/fork,必须先 `Unarchive Selected`
- 非归档线程不能删除,必须先 `Archive Selected`
- 当前模式下不适用的操作按钮会直接隐藏,不会常驻显示为灰化按钮
- 正在活动或疑似活动的线程不能归档;检测依据包括 `logs_2.sqlite` 中仍存活的 Codex process pid,以及最近 90 秒内更新过的 rollout mtime
- `Fork Selected` 的 active 提示只使用 live Codex process pid,不使用 rollout mtime

启动前会先检查:

- `wt.exe` 是否在 `PATH`
- `pwsh` 是否在 `PATH`
- `codex` 是否可被 `pwsh` 解析
- 新建线程时检查选中的工作区路径是否仍然存在
- 恢复线程时检查该线程记录的 `cwd` 是否仍然存在

如果检查失败,会直接弹出 GUI 错误提示。

## 已知限制

- 目前只支持 Windows,并固定使用 `Windows Terminal + pwsh`
- 线程列表依赖本机 Codex 的 `threads` 表结构; 已对字段缺失做降级处理,但如果未来数据库结构大改,仍可能需要更新脚本
- `title` 或 `first_user_message` 的编码显示效果取决于本地 Codex 数据库中的原始内容
- exe 打包依赖 PyInstaller,构建产物在本机生成,不会提交 `dist` 或 `build` 目录
