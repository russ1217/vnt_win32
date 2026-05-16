# VNT VPN Client Wrapper 项目文档

## 📋 项目概述

VNT (Virtual Network Tool) 是一个 Windows 平台下的 VPN 客户端软件，用于登录和管理虚拟专用网络。本项目使用 Python 开发，通过包装 `vnt-cli.exe` 命令行工具实现 VPN 连接功能。

### 核心功能
- VPN 连接管理（基于 vnt-cli.exe）
- 系统服务注册与管理
- GUI 配置界面
- 自动更新机制
- 多语言支持（中文/英文）
- 开机自启动
- 网络连接监控

---

## 🏗️ 项目架构

### 组件概览

```
┌─────────────────────────────────────────────────────────────┐
│                    用户交互层                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ vnt_helper   │  │ vnt_updater  │  │  Task Sched  │      │
│  │   (GUI)      │  │  (更新工具)   │  │  (计划任务)   │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                  │                  │              │
└─────────┼──────────────────┼──────────────────┼──────────────┘
          │                  │                  │
┌─────────┼──────────────────┼──────────────────┼──────────────┐
│         ▼     服务层       ▼                  ▼              │
│  ┌──────────────────────────────────────────────────┐       │
│  │              vnt_service.exe                      │       │
│  │         (Windows 系统服务)                         │       │
│  │                                                   │       │
│  │  ┌─────────────────────────────────────┐         │       │
│  │  │        vnt_daemon.py                │         │       │
│  │  │  • vnt-cli 进程管理                  │         │       │
│  │  │  • IPC 通信服务器                    │         │       │
│  │  │  • 事件监控与推送                    │         │       │
│  │  │  • 网络状态监控                      │         │       │
│  │  └─────────────────────────────────────┘         │       │
│  └──────────────────────────────────────────────────┘       │
│                          │                                  │
│                          ▼                                  │
│              ┌───────────────────────┐                      │
│              │    vnt-cli.exe        │                      │
│              │  (VPN 命令行工具)      │                      │
│              └───────────────────────┘                      │
└─────────────────────────────────────────────────────────────┘
```

---

## 📁 文件结构

### 主要 Python 源文件

| 文件名 | 说明 | 大小 |
|--------|------|------|
| `vnt_daemon.py` | 守护进程核心模块 | ~745 行 |
| `vnt_helper.py` | GUI 主程序 | ~5908 行 |
| `vnt_service.py` | Windows 服务包装器 | ~90 行 |
| `vnt_updater.py` | 自动更新模块 | ~400 行 |
| `update_version.yaml.py` | 版本文件生成工具 | ~60 行 |

### 配置文件

| 文件名 | 说明 |
|--------|------|
| `vnt_helper.yaml` | 主配置文件（IPC端口、语言、自动启动等） |
| `zenbook.yaml` | VPN 连接配置示例 |
| `res/vnt_config_template.yaml` | VPN 配置模板 |
| `res/version.yaml` | 版本信息文件 |

### 资源文件 (res/)

| 文件名 | 说明 |
|--------|------|
| `vnt-cli.exe` | VPN 命令行工具（核心依赖） |
| `vnt_service.exe` | Windows 服务可执行文件 |
| `vnt_updater.exe` | 更新工具可执行文件 |
| `wintun.dll` | Windows TUN 驱动库 |
| `vnt.png` | 托盘图标 |
| `vnt_helper.ico` | 程序图标 |
| `locale/` | 多语言翻译文件 |

### 构建文件

| 文件名 | 说明 |
|--------|------|
| `make.bat` | 主构建脚本 |
| `vnt_helper.spec` | PyInstaller GUI 程序配置 |
| `vnt_service.spec` | PyInstaller 服务程序配置 |
| `vnt_updater.spec` | PyInstaller 更新工具配置 |

---

## 🔧 核心模块详解

### 1. vnt_daemon.py - 守护进程

**职责**: 管理 vnt-cli.exe 进程的生命周期

#### 主要类

##### VNTDaemon
```python
class VNTDaemon:
    """
    核心守护进程类，负责：
    - 启动/停止 vnt-cli 进程
    - 监控 vnt-cli 运行状态
    - 解析 vnt-cli 输出日志
    - 通过 IPC 与 GUI 通信
    - 自动重连机制
    """
    
    # 关键事件匹配规则
    EVENT_PATTERNS = [
        (re.compile(r"register ip=\s*([0-9.]+)"), "IP_assigned"),
        (re.compile(r"Connect Successfully"), "connected"),
        (re.compile(r"Error"), "error"),
        (re.compile(r"Error conf"), "error_conf"),
        (re.compile(r"connect count="), "reconnect_count"),
        (re.compile(r"version "), "version_info"),
        (re.compile(r"Serial:"), "serial_info"),
        (re.compile(r"handshake"), "server_connection"),
    ]
    
    MAX_RECONNECT_COUNT = 10  # 最大重连次数
    
    # 主要方法
    start_vnt_cli()           # 启动 vnt-cli
    stop_vnt_cli_network()    # 停止 vnt-cli 网络
    monitor_vnt_cli()         # 监控循环
    handle_ipc_command()      # 处理 GUI 命令
    ipc_server_loop()         # IPC 服务器循环
```

**IPC 命令协议**:
```json
// GUI -> Daemon
{"cmd": "start"}           // 启动 VPN
{"cmd": "stop_network"}    // 停止 VPN
{"cmd": "restart"}         // 重启 VPN
{"cmd": "status"}          // 查询状态
{"cmd": "subscribe_events"} // 订阅事件
{"cmd": "exit"}            // 退出守护进程

// Daemon -> GUI (事件推送)
{"event": "IP_assigned", "ip": "172.16.0.8"}
{"event": "connected"}
{"event": "error", "message": "..."}
{"event": "version_info", "version": "x.x.x"}
```

##### VNT_Logger
```python
class VNT_Logger:
    """
    日志管理器
    - 使用 RotatingFileHandler
    - 日志文件: vnt_cli.log
    - 最大 1MB，保留 3 个备份
    - 防重复日志机制
    """
```

##### VNT_Config
```python
class VNT_Config:
    """
    YAML 配置文件读写
    - 支持键值对操作
    - 线程安全
    """
    
    # 配置键常量
    KEY_VNT_CONNECTION_CONFIG_YAML
    KEY_IPC_PORT
    KEY_AUTORUN_CLI_ON_STARTUP
    KEY_DISPLAY_LANGUAGE
    KEY_UPDATE_ENABLED
    # ... 等
```

##### Internet_Connectivity_Monitor
```python
class Internet_Connectivity_Monitor:
    """
    网络连接监控器
    - 使用 Windows NLM API
    - 监控互联网连接状态
    - 服务器可达性检测
    """
```

---

### 2. vnt_helper.py - GUI 程序

**职责**: 提供用户界面和配置管理

#### 主要类

##### VNT_Helper_App
```python
class VNT_Helper_App:
    """
    主应用程序
    - 进程管理
    - 服务安装/卸载
    - 更新检查
    - 托盘图标管理
    """
    
    # 常量
    VNT_HELPER_VERSION = "v4_2026.01.25.03"
    VNT_SERVICE_NAME = "VNTDaemonService"
    PIPE_NAME = r'\\.\pipe\vnt_helper_pipe'
    
    # 主要方法
    start()                 # 启动应用程序
    stop()                  # 停止应用程序
    install_service()       # 安装系统服务
    is_service_installed()  # 检查服务是否已安装
```

##### VNT_Connection
```python
class VNT_Connection:
    """
    VPN 连接管理器
    - 与守护进程通信（IPC）
    - 连接状态监控
    - 事件订阅
    """
    
    DEFAULT_IPC_PORT = 58432
```

##### VNT_Main_Window
```python
class VNT_Main_Window(wx.Frame):
    """
    主窗口 GUI
    - 连接状态显示
    - 配置管理
    - 系统托盘
    - 更新检查
    """
```

##### VNT_Update_Window
```python
class VNT_Update_Window(wx.Frame):
    """
    更新窗口
    - 版本检查
    - 下载进度显示
    - 更新执行
    """
```

##### Registry_Taskschedule_for_AutoRun
```python
class Registry_Taskschedule_for_AutoRun:
    """
    开机自启动管理
    - 注册表操作
    - 任务计划程序
    """
```

---

### 3. vnt_service.py - Windows 服务

**职责**: 将守护进程注册为 Windows 系统服务

```python
class VNTService(win32serviceutil.ServiceFramework):
    """
    Windows 服务类
    - 服务名称: VNTDaemonService
    - 在 Session 0 中运行
    - 系统启动时自动运行
    """
    
    _svc_name_ = "VNTDaemonService"
    _svc_display_name_ = "VNT Daemon Service"
    _svc_description_ = "VNT Daemon Service for managing VPN connections"
    
    # 服务生命周期
    SvcStop()   # 服务停止
    SvcDoRun()  # 服务运行
```

**服务管理命令**:
```bash
# 安装服务
vnt_service.exe install

# 启动服务
vnt_service.exe start

# 停止服务
vnt_service.exe stop

# 卸载服务
vnt_service.exe remove
```

---

### 4. vnt_updater.py - 更新工具

**职责**: 自动更新程序

```python
class VNT_Updater:
    """
    更新管理器
    - 下载更新包
    - 停止旧进程
    - 替换文件
    - 启动新版本
    """
    
    DEFAULT_FILES_TO_UPDATE = [
        'vnt_helper.exe',
        'vnt-cli.exe',
        'vnt_service.exe',
        'wintun.dll'
    ]
```

**更新流程**:
```
1. 检查 version.yaml (远程)
2. 对比本地版本
3. 下载 vnt_helper.zip
4. 验证 SHA256 校验和
5. 等待旧进程退出
6. 删除旧文件
7. 解压新版本
8. 启动新程序
```

---

## 🔄 工作流程

### 启动流程

```
用户登录 Windows
    │
    ▼
任务计划程序/注册表触发
    │
    ▼
vnt_helper.exe 启动 (管理员权限)
    │
    ├── 检查是否已有实例运行
    │   └── 如有，提示用户或清理旧进程
    │
    ├── 部署资源文件 (res/ → 工作目录)
    │   └── 通过 SHA256 校验判断是否需要更新
    │
    ├── 安装/启动 Windows 服务
    │   └── vnt_service.exe → VNTDaemonService
    │
    ├── 启动守护进程 (Session 0)
    │   └── vnt_daemon.py
    │       ├── 启动 IPC 服务器 (端口 58432)
    │       ├── 监控网络连接
    │       └── 如配置了 autorun_cli_on_startup
    │           └── 启动 vnt-cli.exe
    │
    ├── 启动 GUI (Session 1+)
    │   └── vnt_helper.py
    │       ├── 连接 IPC 到守护进程
    │       ├── 订阅事件
    │       └── 显示主窗口/托盘图标
    │
    └── 启动更新检查
        └── 定期检查 version.yaml
```

### VPN 连接流程

```
用户点击"连接" (GUI)
    │
    ▼
GUI 发送 IPC 命令
    │
    └── {"cmd": "start"}
        │
        ▼
    守护进程接收命令
        │
        ├── 检查互联网连接
        │
        ├── 读取配置文件 (zenbook.yaml)
        │
        ├── 启动 vnt-cli.exe
        │   └── 命令: vnt-cli.exe -f zenbook.yaml
        │
        ├── 监控 vnt-cli 输出
        │   ├── 解析事件 (IP分配、连接成功等)
        │   └── 推送事件到 GUI
        │
        └── GUI 显示连接状态
            └── 虚拟IP、连接状态等
```

### 断开连接流程

```
用户点击"断开" (GUI)
    │
    ▼
GUI 发送 IPC 命令
    │
    └── {"cmd": "stop_network"}
        │
        ▼
    守护进程接收命令
        │
        ├── 执行: vnt-cli.exe --stop
        │
        ├── 如失败，terminate 进程
        │
        └── 通知 GUI 断开成功
```

### 更新流程

```
定期检查更新 (每 update_cycle_sec 秒)
    │
    ▼
下载 version.yaml
    │
    ├── 对比本地版本
    │   └── 如相同 → 结束
    │
    └── 如不同 → 继续
        │
        ▼
    下载 vnt_helper.zip
        │
        ├── 计算 SHA256
        │   └── 与 version.yaml 中的 checksum 对比
        │
        └── 校验通过 → 启动 vnt_updater.exe
            │
            ├── 等待旧进程退出
            │   ├── vnt_helper.exe (60秒)
            │   ├── vnt_service.exe (11秒)
            │   └── vnt-cli.exe (11秒)
            │
            ├── 停止并卸载服务
            │
            ├── 删除旧文件
            │
            ├── 解压新版本
            │
            ├── 安装新服务
            │
            └── 启动新版本
```

---

## 📝 配置文件详解

### vnt_helper.yaml (主配置)

```yaml
# 开机自动启动 CLI
autorun_cli_on_startup: true

# 上次使用的配置文件
previous_profile: ''

# 配置文件列表
profile_list: zenbook

# 当前使用的 VPN 配置文件路径
config_name: C:\RussDrive\IT\VSCode\VNT\v4\zenbook.yaml

# 启用通知
notification_enabled: true

# 自动更新
auto_update_enabled: true
update_disabled: false

# 更新检查周期 (秒)
update_cycle_sec: 60

# 更新服务器地址
update_version_url: http://172.16.0.200:11061/files/version.yaml

# 显示语言 (en / zh_CN)
display_language: zh_CN
```

### VPN 配置文件 (zenbook.yaml)

```yaml
# 认证信息
token: russraovnt
device_id: zenbook
name: zenbook
password: '011774'

# 服务器配置
server_address: tcp://47.96.165.174:1465
ip: 172.16.0.8  # 分配的虚拟IP

# 加密设置
server_encrypt: true
cipher_model: aes_gcm

# 网络设置
tap: false            # 是否使用 TAP 模式
mtu: 1420             # MTU 大小
tcp: true             # TCP 模式
use_channel: all      # 通道模式 (all/p2p/relay)
parallel: 1           # 并行连接数
ports: [0, 0]         # 端口列表

# 打洞配置
stun_server:          # STUN 服务器
  - stun.miwifi.com
  - stun.chat.bilibili.com
  - stun.hitv.com
  - stun.cdnbye.com
punch_model: all      # 打洞模式

# DNS 设置
dns:
  - 223.5.5.5
  - 114.114.114.114
  - 8.8.8.8

# 其他
compressor: lz4       # 压缩算法
device_name: vnt-tun  # 网卡名称
finger: false         # 数据指纹
first_latency: false  # 优先低延迟
no_proxy: false       # 关闭内置代理
cmd: false            # 控制台输入
disable_stats: false  # 统计功能
allow_wire_guard: false  # WG 接入
packet_loss: 0        # 模拟丢包
packet_delay: 0       # 模拟延迟
```

---

## 🔌 进程间通信 (IPC)

### IPC 架构

```
┌──────────────┐         ┌──────────────┐
│  vnt_helper  │  TCP    │ vnt_daemon   │
│   (GUI)      │◄───────►│  (Service)   │
│  Session 1+  │ 58432   │  Session 0   │
└──────────────┘         └──────────────┘
```

### 通信协议

**传输层**: TCP Socket (127.0.0.1:58432)

**消息格式**: JSON (UTF-8 编码，以 `\n` 分隔)

**命令类型**:

| 命令 | 方向 | 说明 | 响应 |
|------|------|------|------|
| `start` | GUI→Daemon | 启动 VPN | `{"status": "ok/error"}` |
| `stop_network` | GUI→Daemon | 停止 VPN | `{"status": "ok/error"}` |
| `restart` | GUI→Daemon | 重启 VPN | `{"status": "ok/error"}` |
| `status` | GUI→Daemon | 查询状态 | `{"running": "yes/no", "virtual_ip": "...", ...}` |
| `subscribe_events` | GUI→Daemon | 订阅事件 | 保持连接 |
| `exit` | GUI→Daemon | 退出守护进程 | `{"status": "daemon exits"}` |

**事件类型**:

| 事件 | 方向 | 说明 | 数据 |
|------|------|------|------|
| `IP_assigned` | Daemon→GUI | 分配虚拟IP | `{"ip": "172.16.0.8"}` |
| `connected` | Daemon→GUI | 连接成功 | - |
| `error` | Daemon→GUI | 错误 | `{"message": "..."}` |
| `error_conf` | Daemon→GUI | 配置错误 | `{"message": "..."}` |
| `reconnect_count` | Daemon→GUI | 重连计数 | `{"connect_count": "5"}` |
| `version_info` | Daemon→GUI | 版本信息 | `{"version": "x.x.x"}` |
| `serial_info` | Daemon→GUI | 序列号 | `{"serial": "..."}` |
| `server_connection` | Daemon→GUI | 服务器连接 | `{"server_version": "..."}` |

---

## 🛠️ 构建系统

### make.bat 用法

```bash
# 构建所有可执行文件
make.bat

# 构建并部署 (SCP)
make.bat -i

# 仅更新版本并部署 (跳过编译)
make.bat -u
```

### 构建流程

```
1. 查找 UPX (可执行文件压缩工具)
2. 查找 7-Zip (打包工具)
3. 构建 vnt_service.exe
   └── pyinstaller vnt_service.spec
4. 构建 vnt_updater.exe
   └── pyinstaller vnt_updater.spec
5. 构建 vnt_helper.exe
   └── pyinstaller vnt_helper.spec
6. 打包 vnt_helper.zip
7. 更新 version.yaml
   └── 提取版本号 (从 vnt_helper.py)
   └── 计算 ZIP 的 SHA256
8. (可选) SCP 部署到服务器
```

### PyInstaller 配置

**vnt_helper.spec**:
- 包含 `res/` 目录
- 控制台: 隐藏
- UPX 压缩 (排除关键 DLL)
- 图标: vnt_helper.ico

**vnt_service.spec**:
- 无额外数据文件
- 控制台: 隐藏
- 隐藏导入: win32timezone

---

## 🌐 多语言支持

### 支持的语言
- 英语 (en)
- 简体中文 (zh_CN)

### 实现方式
```python
# 使用 gettext
import gettext

# 初始化
_ = self.setup_i18n('zh_CN')

# 使用
title = _("连接状态")
msg = _("VPN 已连接")
```

### 翻译文件位置
```
res/locale/
├── en/
│   └── LC_MESSAGES/
│       └── vnt_helper.mo
└── zh_CN/
    └── LC_MESSAGES/
        └── vnt_helper.mo
```

---

## 🔐 安全特性

### 管理员权限
- 程序启动时请求管理员权限
- 使用 `ShellExecuteEx` + `runas` 提权

### 更新验证
- SHA256 校验和验证
- 版本文件完整性检查

### 进程管理
- 命名管道通信 (退出信号)
- PID 文件管理
- 优雅关闭 (先 SIGINT，后强制 kill)

---

## 📊 监控与日志

### 日志系统
- **文件**: `vnt_cli.log`
- **大小**: 最大 1MB
- **备份**: 3 个历史文件
- **级别**: DEBUG / INFO / CRITICAL

### 日志内容示例
```
2026-05-13 10:30:15,123 - INFO     - PID 12345 : VNT Daemon starting...
2026-05-13 10:30:15,456 - INFO     - PID 12345 : IPC server listening on 127.0.0.1:58432
2026-05-13 10:30:16,789 - INFO     - PID 12345 : Started vnt-cli with config: zenbook.yaml
2026-05-13 10:30:17,012 - INFO     - PID 12345 : Event detected: {'event': 'IP_assigned', 'ip': '172.16.0.8'}
```

### 事件监控
- 互联网连接状态变化
- vnt-cli 进程状态
- VPN 连接事件
- 重连次数监控 (超过 10 次自动重启)

---

## 🚀 升级准备 (vnt-cli 2.0)

### 当前版本依赖
- **vnt-cli.exe**: 1.x 版本
- **交互方式**: 子进程 + 标准输出解析
- **停止方式**: `vnt-cli.exe --stop` 命令

### 2.0 版本可能的变化
需要关注的变化点：

1. **命令行参数变化**
   - 启动参数是否改变？
   - 配置文件格式是否变化？

2. **输出日志格式变化**
   - 事件匹配正则表达式需要更新
   - `EVENT_PATTERNS` 列表需要调整

3. **停止机制变化**
   - `--stop` 命令是否仍然有效？
   - 是否需要新的停止方式？

4. **新增功能**
   - 是否有新的 IPC 命令？
   - 是否有新的事件类型？

5. **配置文件变化**
   - YAML 配置字段是否增减？
   - 字段名称是否改变？

### 升级检查清单

- [ ] 获取 vnt-cli 2.0 的文档/变更日志
- [ ] 测试 2.0 版本的命令行参数
- [ ] 分析 2.0 版本的输出日志格式
- [ ] 更新 `EVENT_PATTERNS` 正则表达式
- [ ] 测试 `--stop` 命令兼容性
- [ ] 检查配置文件格式变化
- [ ] 更新 `vnt_config_template.yaml`
- [ ] 测试 IPC 命令兼容性
- [ ] 更新事件推送逻辑
- [ ] 回归测试所有功能

---

## 📞 技术支持

### 常见问题

**Q: 服务无法启动**
- 检查是否以管理员权限运行
- 查看 Windows 事件日志
- 检查 `vnt_cli.log`

**Q: GUI 无法连接守护进程**
- 检查 IPC 端口 58432 是否被占用
- 重启 vnt_helper.exe
- 查看日志中的 IPC 错误信息

**Q: VPN 连接失败**
- 检查互联网连接
- 验证配置文件中的服务器地址
- 查看 vnt-cli 输出日志

**Q: 更新失败**
- 检查更新服务器可达性
- 验证 version.yaml 格式
- 以管理员权限运行

---

## 📅 版本历史

| 版本 | 日期 | 说明 |
|------|------|------|
| v4_2026.01.25.03 | 2026-01-25 | 当前版本 |
| ... | ... | ... |

---

## 📄 许可证

本项目为内部使用，未公开许可证信息。

---

*文档生成日期: 2026-05-13*
*项目路径: d:\RussDrive\IT\VSCode\VNT\v4*
