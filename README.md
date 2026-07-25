# Yohaku Companion Windows

本项目改自 [YohakuCompanion](https://github.com/Innei/YohakuCompanion), 在Codex辅助下使用pyhton重构了一个适用于Windows的客户端

---

Yohaku Companion Windows 是适用于 Windows 10/11 的 Yohaku Live Desk 客户端。它使用 Python、PySide6 和 Companion Protocol v2 直接连接 Yohaku Core，在明确征得用户同意后发布当前前台应用、可选窗口标题、系统媒体元数据和播放时间线。

当前客户端版本为 **1.7.10**，界面语言为简体中文。

## 功能概览

- 简体中文设置窗口和系统托盘，支持单实例运行及再次启动时唤起窗口。
- 使用 Win32 API 识别前台应用，并在隐私策略允许后才读取窗口标题。
- 使用 Windows Global System Media Transport Controls 读取媒体标题、艺术家、专辑、播放器、播放状态、时长、位置和倍率。
- Live Desk 发布前必须先查看当前净化预览并明确开启；配对成功不会自动发布。
- 应用、窗口标题和媒体可以分别继承默认规则、分享或隐藏，并支持公开别名。
- 正则敏感词规则支持图形化构建、高级表达式、字段范围、三种处理动作、排序和 5ms 超时保护。
- 可选 VRChat 集成可从本地 Discord RPC 捕获 VRChat/VRCX Activity，用净化后的世界名称替换标题，并上传白名单状态。
- 内置运行与上报日志中心，支持筛选、搜索、复制和可选的按日文件日志。
- 设备令牌仅保存在 Windows Credential Locker，不提供明文文件回退。
- SQLite 事务保存非秘密配置及设备序列，避免崩溃后复用序列。
- 监听 Windows 锁屏、休眠和恢复事件；无法建立锁屏监听时拒绝开启发布。
- 支持局域网私有地址 HTTP 和公网 HTTPS。
- 提供 PyInstaller 单目录、无控制台窗口构建及打包后自检。

## 产品边界

本项目只实现第一方 Yohaku Live Desk Presence，不包含：

- Slack、Discord 社交状态发布或云端机器人集成（VRChat 功能只监听本机 Discord RPC 管道）；
- Legacy MixSpace 传输；
- 历史统计、Moments 或阅读会话；
- 媒体封面上传和播放链接；
- 安装器、代码签名、自动更新或 ARM64 构建。

即使服务器声明支持媒体封面或播放链接，客户端也只会发送显式 JSON `null`。

## 系统要求

- Windows 10 1809（build 17763）或更高版本，或者 Windows 11；
- x64 处理器和操作系统；
- 源码运行需要 Python 3.12；
- 支持 Companion Protocol v2 且启用了 Live Desk 的 Yohaku Core；
- 播放器若要分享媒体信息，必须注册 Windows 系统媒体会话。

目前主要在 Windows 11 x64 实机验证。Windows 10 1809 通过 API 版本守卫和兼容行为测试覆盖。

## 快速开始

### 运行已构建版本

PyInstaller 构建产物是一个完整目录，而不是单个可独立复制的 EXE。请保留 `YohakuCompanion.exe` 与同目录下的 `_internal` 文件夹，然后运行：

```powershell
YohakuCompanion.exe
```

关闭设置窗口只会隐藏窗口，程序仍常驻系统托盘。请通过托盘菜单退出程序。

### 从源码运行

环境由使用者手动创建和安装：

```powershell
conda create -n yohaku-companion-win python=3.12 pip
conda activate yohaku-companion-win
cd YohakuCompanionWindows
python -m pip install -e ".[dev]"
python -m yohaku_companion_windows
```

如果拉取了包含新依赖的更新，请再次执行：

```powershell
python -m pip install -e ".[dev]"
```

## 服务器地址

输入的是 Yohaku Core 的 **API 基础地址**，不要手动追加 `/companion/capabilities`。

单域名 HTTPS 示例：

```text
https://example.com/api/v3
```

客户端会检查：

```text
https://example.com/api/v3/companion/capabilities
```

局域网 HTTP 示例：

```text
http://192.168.3.36:2333/api/v3
```

HTTP 仅允许主机解析到私有、回环或链路本地 IP；公网服务器必须使用 HTTPS。局域网 HTTP 会绕过环境代理直接连接，避免配对码或设备令牌被转发给代理，但 HTTP 本身没有传输加密，同一网络中的攻击者仍可能窃听数据。条件允许时应优先部署 HTTPS。

可以先在 PowerShell 验证能力接口：

```powershell
curl.exe -i http://192.168.3.36:2333/api/v3/companion/capabilities
```

预期返回 HTTP 200、`liveDesk: true`，并包含 Presence schema v2。

## 配对与日常操作

1. 在 Yohaku 或 Mix Space 管理端生成一次性 Companion 配对码。
2. 打开客户端，填写 API 基础地址、一次性配对码和设备名称。
3. 客户端依次检查 Windows Credential Locker、服务器地址和能力接口，然后才会消费配对码。
4. 配对成功后 Live Desk 仍保持关闭。
5. 点击“重新采集预览”，检查净化后的应用、窗口标题和媒体信息。
6. 确认无敏感内容后，点击“开启 Live Desk”。
7. 临时停止公开可点击“暂停”；恢复时会等待旧清除请求结束并重新采集。
8. 修改任何隐私设置后，客户端会先清除 Presence、关闭 Live Desk，并要求重新预览和明确开启。

当设置窗口自身位于前台时，预览会尝试识别其后方最近使用的外部窗口，不会把 Yohaku Companion 自身作为当前公开应用。

### 运行状态

| 状态 | 含义 |
| --- | --- |
| 已公开 | 已连接并正在维护 Live Desk Presence |
| 正在连接 | 正在协商能力、验证凭据或建立连接 |
| 已暂停 | 用户暂停、会话锁定或系统生命周期暂停 |
| 连接降级 | 网络异常，客户端会按策略重新连接 |
| 需要更新 | 服务器要求的最低客户端版本高于当前版本 |
| 服务不可用 | 服务器未启用 Live Desk 或不支持所需协议 |
| 已关闭 | Live Desk 未开启，不会发布 Presence |

状态始终同时显示图标、颜色和中文文字，不依赖颜色传达唯一含义。

## 隐私设置

### 来源与默认规则

默认行为是分享应用和媒体，不采集、不分享窗口标题。

窗口标题必须同时满足以下条件才会调用 Win32 `GetWindowTextW`：

1. 全局“允许采集窗口标题”已开启；
2. 默认标题规则或对应应用标题规则允许分享；
3. 应用本身没有被隐藏。

原始路径、窗口句柄和 PID 仅用于当前进程识别，不会进入协议或持久化数据库。

### 应用规则

应用、标题和媒体规则相互独立：

| 模式 | 颜色 | 行为 |
| --- | --- | --- |
| 继承 | 蓝色 | 继承上方对应的默认规则 |
| 分享 | 绿色 | 明确允许该字段 |
| 隐藏 | 红色 | 明确隐藏该字段，优先级高于显示别名 |

“显示别名”只改变对外公开的应用名称，不会改变应用识别标识。“自定义标题”用于为单个应用指定固定公开标题；只有全局窗口标题来源和该应用的标题规则均允许时才生效。标题优先级为“自定义标题 → VRChat 世界名称 → 普通窗口标题”，因此设置自定义标题后不会读取原始窗口标题。自定义标题仍会经过 NFC 规范化、长度限制和窗口标题敏感词规则。

应用规则、敏感词规则和日志中的所有表格列均支持拖动表头分隔线调整宽度。

### 敏感词规则

每条规则可以选择以下字段：

- 应用名；
- 窗口标题；
- 媒体标题；
- 艺术家；
- 专辑；
- 播放器名。

处理动作包括：

- **替换命中**：把全部命中片段替换为 `•••`；
- **隐藏字段**：隐藏命中的字段；应用名被隐藏时隐藏整个应用，媒体标题和艺术家都被隐藏时隐藏整个媒体；
- **隐藏上下文**：应用字段命中时隐藏整个应用，媒体字段命中时隐藏整个媒体。

表达式编辑器提供两种方式：

- **图形化构建**：组合“包含文字、完整等于、开头、结尾、任一关键词、数字序列、邮箱地址、网页地址”等模块；任一模块命中即执行规则；
- **高级正则**：直接编辑完整正则表达式。

最多保存 50 条规则，每条图形表达式最多 20 个模块，单个最终表达式最多 256 字符。规则按列表顺序执行，可启停、双击编辑和上下移动。每次正则匹配最多运行 5ms；运行时超时会 fail-closed 隐藏对应字段，提示中不包含原文。

正则表达式、模块配置、命中内容和诊断信息不会发送给服务器。

“任一关键词”模块支持半角逗号 `,`、中文逗号 `，`、顿号 `、`、换行或竖线 `|` 分隔。每个词会先执行 NFC 规范化、去空和顺序去重，再进行正则安全转义；其他文字模块仍把输入视为一个完整字符串。高级正则页会同步显示最终表达式。

## VRChat 集成

“VRChat 集成”标签页默认关闭。启用总开关后，可以分别控制：

- 当前前台应用为 `VRChat.exe` 时，以 Activity 的 `details` 作为世界名称候选窗口标题；
- 将净化后的 VRC Activity POST 到自定义完整端点。

捕获器监听本机 `discord-ipc-0..9`，只接受 `SET_ACTIVITY` 中 PID 可验证为 `vrchat.exe` 或 `vrcx.exe` 的消息。世界名称不会绕过隐私设置：必须同时允许窗口标题来源和 VRChat 应用标题规则，并继续经过长度限制及敏感词规则。没有有效世界名称时回退到普通窗口标题。

VRC 上传端点示例：

```text
https://example.com/api/vrc/activity
http://192.168.3.36:2333/api/vrc/activity
```

端点是完整 POST URL。公网必须使用 HTTPS；HTTP 只允许私有、回环或链路本地地址，并会在每次请求前重新验证 DNS、绕过环境代理。请求使用 `X-API-Key`，正文只包含 `capture_at`、`nonce` 和净化后的 `activity`。Activity 白名单仅保留 `details`、`state`、合法 `timestamps` 及 `assets.large_image`/`assets.small_image`；party、secrets、buttons 和未知字段不会上传。

API 密匙使用独立的 Windows Credential Locker 账户保存。界面不会回显已有值：密匙框留空后保存表示保留，使用“清除已保存密匙”才会删除。总开关和两个子功能只在 Live Desk 已公开、未暂停且会话未锁定/休眠时运行。修改集成设置会清除 Presence、关闭 Live Desk 并要求重新预览确认。

## 日志中心

“日志”标签页、未配对页面和托盘菜单均可打开日志。内存中最多保留最近 1000 条，支持按级别、类别和文本筛选，以及暂停刷新、自动滚动、复制和清空。

文件日志默认关闭。启用后写入当前应用身份目录的 `logs\companion.log`，午夜轮转，仅保留一个旧文件。Debug 与 Release 使用不同目录。界面和文件使用同一份脱敏记录；日志不会记录 Bearer token、VRC API 密匙、配对码、请求 Header、原始窗口标题、原始 Activity 或响应正文。

“记录 VRChat 调试日志”默认关闭，仅排查集成问题时开启。关闭时仍会保留警告和错误，但不会为高频 Activity 或成功上传创建日志。开启后 Activity 日志按五秒聚合，未占用的 Discord 管道最多每 30 秒记录一次；日志表格也只在可见且内容变化时刷新。

VRC Activity 上传采用“最新状态优先”：突发更新会合并为队列中的最新一项，发送频率最多每秒一次，不会在 VRChat 长时间运行后追赶过期状态。

## 媒体时间线

媒体总时长优先使用 WinRT `EndTime - StartTime`；结果不可用或为零时回退到 `MaxSeekTime - MinSeekTime`。播放器两组数据都不提供时显示“未知时长”，不会伪造为 `0:00`。

播放位置以 WinRT `Position` 和 `LastUpdatedTime` 为锚点：

```text
当前位置 = position + (当前时间 - lastUpdatedTime) × playbackRate
```

播放中自然推进，暂停时不推进，并始终钳制到有效总时长。客户端发送 `positionMs`、`sampledAt` 和 `rate`；服务器公开投影对应使用 `anchorAt`。网页端应根据锚点本地逐秒插值，而不是要求客户端每秒发送网络请求。客户端设置窗口的一秒预览定时器只更新本地显示，不增加请求频率。

同一媒体语义保持同一个会话 UUID；位置自然推进不会被视为新的隐私披露。暂停超过五分钟或系统媒体会话消失后会清除媒体 Presence。

## 安全与本地数据

### 凭据

- 配对码只出现在 `/companion/pairings/claim` 的 JSON 请求体中，不进入 URL 或请求头；
- 设备令牌只写入当前用户的 Windows Credential Locker；
- 网络请求只通过 Bearer Header 使用令牌；
- 客户端会验证 `keyring` 实际使用 Windows 后端，拒绝文件型或明文回退；
- 令牌不会写入 SQLite、日志、预览或界面。

### 数据目录

源码 Debug 和打包 Release 使用完全独立的数据和凭据：

| 模式 | 应用标识 | SQLite 位置 |
| --- | --- | --- |
| 源码 Debug | `dev.innei.YohakuCompanion.windows.debug` | `%LOCALAPPDATA%\dev.innei.YohakuCompanion.windows.debug\state.sqlite3` |
| 打包 Release | `dev.innei.YohakuCompanion.windows` | `%LOCALAPPDATA%\dev.innei.YohakuCompanion.windows\state.sqlite3` |

数据库只保存非秘密连接元数据、来源设置、应用规则、敏感词规则、VRChat 非秘密开关与端点、日志开关、暂停状态和设备序列。设备令牌与 VRC API 密匙均不进入 SQLite。客户端不会读取、迁移或删除 macOS Yohaku Companion 或 ProcessReporter 的数据。

### 生命周期清除

暂停、锁屏、休眠、关闭 Live Desk、退出和移除连接时，客户端会执行有序、最长约 500ms 的 Presence 清除。服务器租约是网络不可达时的最终兜底。恢复后会等待旧清除结束，重新协商能力并采集全新快照。

## 托盘、后台与命令行

```powershell
# 正常启动
python -m yohaku_companion_windows

# 打包版本的后台启动参数
YohakuCompanion.exe --background

# 打包后无界面凭据后端冒烟测试
YohakuCompanion.exe --self-test
```

再次启动同一模式只会通过 Qt 本地 IPC 唤起已有设置窗口，不会创建第二个进程。

“登录 Windows 后在后台启动”默认关闭，只在 PyInstaller Release EXE 中开放，使用当前用户注册项：

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
```

源码 Debug 模式不会写入 Release 开机启动项。

`--self-test` 会使用随机名称在 Credential Locker 中写入、读取并立即删除临时探针，不读取或修改正式设备令牌。

## 项目结构

```text
YohakuCompanionWindows/
├─ src/yohaku_companion_windows/
│  ├─ main.py                 # 进程入口与 Qt/qasync 事件循环
│  ├─ service.py              # 进程级 ApplicationService
│  ├─ capture.py              # 原始数据到净化快照的唯一边界
│  ├─ win32_capture.py        # 前台应用和窗口标题采集
│  ├─ media_capture.py        # WinRT 系统媒体采集
│  ├─ privacy.py              # 应用策略与敏感词过滤
│  ├─ vrchat.py               # Discord RPC 捕获、白名单净化与 VRC 上传
│  ├─ logging_service.py      # 脱敏内存日志与按日文件日志
│  ├─ sensitive_rules_ui.py   # 正则规则图形编辑器
│  ├─ coordinator.py          # 采集、合并、心跳和生命周期协调
│  ├─ protocol.py             # Companion Protocol v2 DTO 与校验
│  ├─ writer.py               # 序列化发送和幂等重试
│  ├─ storage.py              # SQLite 非秘密状态
│  ├─ credentials.py          # Windows Credential Locker
│  ├─ lifecycle.py            # 锁屏与电源消息
│  ├─ single_instance.py      # Qt 本地 IPC 单实例
│  └─ ui.py                   # 设置窗口与系统托盘
├─ tests/                     # 行为测试
├─ packaging/version_info.txt
├─ YohakuCompanion.spec       # PyInstaller 单目录配置
├─ build.ps1                  # 构建并执行 EXE 自检
└─ pyproject.toml             # 依赖、工具和项目元数据
```

`ApplicationService` 是配对元数据、凭据、预览同意、采集器和 Live Desk 协调器的唯一进程级所有者。任何预览、网络 DTO 和最近发布视图都只能接收 `SanitizedPresenceSnapshot`。

## 开发与测试

激活环境后运行：

```powershell
conda activate yohaku-companion-win
ruff check .
mypy src
pytest
```

如果当前工作目录的工具缓存存在 Windows ACL 问题，可以临时禁用 Pytest 缓存验证测试本身：

```powershell
pytest -p no:cacheprovider
```

自动测试使用伪服务器、伪传输和伪采集器，不会消费真实配对码，也不会向真实服务器发布。目前覆盖协议校验、安全连接、隐私过滤、正则超时、媒体时间线、序列持久化、生命周期、并发协调、Qt 界面和单实例行为。

真实服务器配对、Windows 锁屏/解锁、休眠/恢复、foobar2000 与浏览器媒体会话仍属于最终人工验收。

## 构建 Release

```powershell
conda activate yohaku-companion-win
cd YohakuCompanionWindows
.\build.ps1
```

脚本会：

1. 使用 `YohakuCompanion.spec` 执行 PyInstaller 单目录、无控制台构建；
2. 将结果写入 `dist\YohakuCompanion\`；
3. 执行生成的 `YohakuCompanion.exe --self-test`；
4. 仅在自检成功后报告构建完成。

`build/`、`dist/`、工具缓存、editable-install 元数据、数据库和本地环境均已被 `.gitignore` 排除。不要只分发 EXE；应分发整个 `dist\YohakuCompanion\` 目录。

## 故障排查

### 能力检查返回 HTTP 403

先直接请求完整能力 URL。如果 `curl.exe` 返回 200 而客户端返回 403，请确认运行的是最新客户端；客户端会发送 `YohakuCompanion/1.7.10 (Windows)` User-Agent，并对局域网 HTTP 绕过环境代理。还应检查反向代理、WAF 或爬虫防护是否按 User-Agent、来源或路径拦截。

### VRChat 集成提示缺少 pywin32

激活 conda 环境后重新执行 `python -m pip install -e ".[dev]"`。版本 1.7.10 精确依赖 `pywin32==312`；更新依赖后需要重新构建 EXE。

### VRC Activity 没有出现

确认 Live Desk 状态为“已连接并公开”，没有暂停、锁屏或休眠，并确认 VRChat 或 VRCX 在启动时连接到了本客户端创建的 `discord-ipc-0..9` 管道。日志中心的“VRChat 捕获”类别会显示握手和捕获状态，但不会显示原始世界名称。

### 无法监听 Windows 锁屏事件

不要绕过该检查。确认程序运行在正常交互式用户会话中，然后完整退出并重新启动。锁屏监听不可用时客户端按设计拒绝发布。

### Windows 凭据后端不可用

确认安装了 `keyring`，没有使用环境变量强制选择其他后端，并且程序运行在正常 Windows 用户会话。客户端拒绝文件型 keyring。

### 媒体为空或状态不正确

- 确认播放器出现在 Windows 音量/媒体浮层中；
- 部分播放器只发布可跳转范围，客户端会使用它作为总时长后备；
- 播放器完全不提供时长时会显示“未知时长”；
- 媒体能力不可用时，客户端会继续发布应用 Presence；
- 网页进度只在心跳时跳变通常是前端没有根据 `anchorAt` 和 `rate` 插值，不应通过客户端每秒上传解决。

### 修改隐私规则后无法立即开启

这是安全行为。隐私策略改变会使旧预览失效并关闭 Live Desk；请重新采集预览、核对内容，然后明确开启。

### 无法开启开机启动

该开关只对 PyInstaller Release EXE 开放。源码 Debug 模式按设计禁用，且使用独立应用标识。

### 构建目录拒绝访问

先退出正在运行的旧 EXE，并确认没有杀毒软件或资源管理器占用 `build/`、`dist/` 中的文件。必要时使用新的构建目录；不要用破坏性的 Git 命令清理用户数据。

## 发布前检查清单

- `ruff check .` 通过；
- `mypy src` 通过；
- `pytest` 通过；
- `build.ps1` 完成；
- 生成的 EXE `--self-test` 返回 0；
- 使用真实 Yohaku Core 完成配对、预览、开启、暂停和移除；
- 使用与浏览器验证播放、暂停、进度和时长；
- 验证锁屏、解锁、休眠和恢复；
- 确认分发目录不包含数据库、日志、凭据或一次性配对码。

## 目前已知问题
- [ ] foobar2000 播放器 无法正确获取歌曲时长
- [ ] 图形化正则式编辑分割多个词语应为`|`而非`,`
