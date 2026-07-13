# codex-cli-startup

这是一个 Windows 专用的 Codex workspace 启动工具仓库,包含两个相互独立但共享配置的入口:

- `codex-cli-startup.py`: PySide6 GUI 启动器,用于管理常用 workspaces 和 Codex chat threads。
- `list_project.py`: Textual CLI 项目选择器,用于在终端中搜索、选择和管理 workspaces,并配合 `list-project.ps1` 切换当前目录。

两个入口默认读取同一份 `codex-cli-startup_config.json`,其中的 workspace 顺序会同时反映在 GUI 和 CLI 项目列表中。

## 安装

```powershell
python setup_env.py
```

依赖会安装到仓库根目录的 `.venv`,不要安装到宿主 Python 环境。
`requirements.txt` 包含 `PySide6>=6.7,<7`、`textual>=8.2,<9` 和 Codex rollout 压缩文件支持。PySide6 用于 GUI,Textual 用于 CLI 项目导航 TUI。
这些范围兼容当前 `Python 3.13.6`,同时避免自动升级到可能带来兼容变化的大版本。

## 启动

```powershell
.\.venv\Scripts\python.exe codex-cli-startup.py
```

Windows 下如果你不想看到额外的空白控制台窗口,优先使用:

```powershell
.\.venv\Scripts\pythonw.exe codex-cli-startup.pyw
```

也可以直接双击 [codex-cli-startup.pyw](G:\Project\codex-cli-startup\codex-cli-startup.pyw)。

仓库根目录还包含已入库的 `codex-cli-startup.exe`,双击它会通过 `.venv\Scripts\pythonw.exe` 拉起 `codex-cli-startup.py`。如果 `.venv` 不存在,它会回退到 `pythonw.exe`。这个 wrapper 依赖同目录的 `codex-cli-startup.dll`,`codex-cli-startup.deps.json`,`codex-cli-startup.runtimeconfig.json`。

## CLI 项目导航

在 PowerShell 中运行以下命令,可以打开 Textual TUI,按 `codex-cli-startup_config.json` 的顺序查看项目,并在当前终端中选择目标项目:

```powershell
.\list-project.ps1
```

TUI 支持搜索过滤、方向键导航和鼠标选择。按 `Enter` 确认当前项目,按 `Esc` 或 `Ctrl+Q` 取消。选择成功后,当前 PowerShell 会通过 `Set-Location` 切换到目标路径。

在主选择界面按 `F2` 可以进入独立管理模式:

- `A`: 把启动 `list-project.ps1` 时的当前工作目录加入配置,并弹出 Display name 输入框。默认名称为目录名,可以直接输入中文。
- `R`: 修改当前选中项目的 Display name,项目路径保持不变。
- `T`: 把当前选中项目移动到配置和项目列表的顶端。
- `D`: 请求确认后从配置中移除当前选中项目。
- `Esc`: 返回主选择界面并立即刷新项目列表。

管理操作只修改配置中的 `workspaces`,其他配置字段会保留。配置为空或尚未创建时,无参数启动仍会进入 TUI,可以通过管理模式添加第一个项目。

也可以直接传入项目名称或配置路径,跳过交互列表:

```powershell
.\list-project.ps1 codex-cli-startup
.\list-project.ps1 G:/Project/codex-cli-startup
```

直接选择时,项目名称匹配不区分大小写。如果配置中存在同名项目,请改用完整路径。目标路径不存在时不会切换目录。

这个入口不会修改 PowerShell Profile 或用户 `PATH`。由于目录切换由 PowerShell 脚本自身完成,请直接执行脚本,不要在独立子进程中调用它。

构建不依赖 Python 的独立 CLI exe:

```powershell
python setup_env.py --build
.\.venv\Scripts\python.exe build_list_project.py
```

预期输出为 `dist/list-project.exe`。将 `list-project.exe` 和 `list-project.ps1` 放在同一目录后,PowerShell 脚本会优先调用 exe;仓库开发环境中没有 exe 时才回退到 `.venv` Python 入口。

仓库级 `$deploy-list-project` skill 位于 `.agents/skills/deploy-list-project`。激活该 skill 会重新构建 exe,并通过 `LOCALAPPDATA` 环境变量部署 exe 和 ps1 到 `%LOCALAPPDATA%\codex-cli-startup`;现有配置文件不会被修改。

重新生成这个轻量 wrapper:

```powershell
.\.venv\Scripts\python.exe build_wrapper.py
```

wrapper 和 PyInstaller 产物使用同一个图标文件: [assets/codex-cli-startup.ico](G:\Project\codex-cli-startup\assets\codex-cli-startup.ico)。

## 构建 exe

如果需要无 Python 命令行的 GUI 入口,先安装打包依赖:

```powershell
python setup_env.py --build
```

然后执行:

```powershell
.\.venv\Scripts\python.exe build_exe.py
```

生成的入口位于 [dist/codex-cli-startup.exe](G:\Project\codex-cli-startup\dist\codex-cli-startup.exe)。

exe 使用 `--windowed` 模式,启动时不会显示控制台窗口。源码版和打包版使用同一个 `%LOCALAPPDATA%` 配置文件,移动 exe 不会改变配置位置。

## 配置文件位置

配置文件统一保存在 `%LOCALAPPDATA%\codex-cli-startup\codex-cli-startup_config.json`。程序通过 `LOCALAPPDATA` 环境变量解析当前用户目录,不会硬编码用户名。源码版、轻量 wrapper、CLI 和打包版使用同一个位置。

程序不会读取或自动迁移仓库目录中的旧配置文件。首次启动 GUI 时,如果新位置尚无配置,会自动创建目录和默认配置。需要保留现有 workspace 时,请手动把旧 JSON 复制到上述位置。

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
