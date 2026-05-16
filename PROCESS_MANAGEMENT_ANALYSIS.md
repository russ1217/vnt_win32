# VNT 进程处理机制分析

## 📋 概述

VNT 软件采用了**多层进程管理策略**，对已运行的进程进行智能检测、优雅关闭和强制清理。该机制涉及三个层次的进程：

1. **vnt_helper.exe** - GUI 主程序（用户层）
2. **vnt_service.exe** - Windows 系统服务（服务层）
3. **vnt-cli.exe** - VPN 命令行工具（核心层）

---

## 🏗️ 进程架构

```
┌─────────────────────────────────────────────────────────────┐
│                    进程启动流程                               │
└─────────────────────────────────────────────────────────────┘

用户双击 vnt_helper.exe
    │
    ├─ 1. 请求管理员权限 (_run_as_admin)
    │   └── 如果不是管理员 → 重新以管理员身份启动
    │
    ├─ 2. 记录当前进程 PID (_process_PID "set")
    │   └── 写入 vnt_pid.yaml
    │
    ├─ 3. 检测已存在的 vnt_helper.exe 进程 (_clear_existing_process)
    │   ├── 无其他进程 → 继续
    │   ├── 有其他进程 → 提示用户或强制清理
    │   └── 用户选择"否" → 退出
    │
    ├─ 4. 部署资源文件 (_deploy_resource_files)
    │   └── 通过 SHA256 校验判断是否需要更新
    │
    ├─ 5. 设置退出信号监听 (_process_exit_signal)
    │   └── 创建命名管道 \\.\pipe\vnt_helper_pipe
    │
    ├─ 6. 等待网络连接
    │
    ├─ 7. 启动气泡消息处理器 (Bubble_Message)
    │
    ├─ 8. 启动 VPN 连接管理器 (VNT_Connection)
    │
    └─ 9. 启动 GUI 主循环 (_main_GUI_loop)
```

---

## 🔍 详细机制分析

### 1️⃣ 进程检测机制

#### 方法：`_get_process_list(process_name)`

**位置**: `vnt_helper.py` 第 282-296 行

**实现**:
```python
def _get_process_list(self, process_name):
    i = 0
    pid = []
    HasInstance = False
    for proc in psutil.process_iter():
        try:
            pinfo = proc.as_dict(attrs=['pid', 'name'])
        except psutil.NoSuchProcess:
            return False, 0, pid
        else:
            if process_name in pinfo["name"]:
                HasInstance = True
                pid.append(pinfo['pid'])
                i = i + 1
    
    return HasInstance, i, pid
```

**功能**:
- 使用 `psutil` 遍历所有系统进程
- 通过进程名称模糊匹配
- 返回：(是否存在, 进程数量, PID列表)

**特点**:
- ✅ 支持多实例检测
- ✅ 异常处理完善 (NoSuchProcess)
- ⚠️ 模糊匹配可能误判 (如 `vnt_helper.exe.bak`)

---

### 2️⃣ PID 文件管理

#### 方法：`_process_PID(cmd)`

**位置**: `vnt_helper.py` 第 381-420 行

**文件**: `vnt_pid.yaml`

**PID 记录格式**:
```yaml
total_number_of_pid: 3
0: 12345
1: 12346
2: 12347
```

**操作模式**:

##### 模式 1: `"set"` - 注册当前 PID
```python
# 逻辑流程
1. 读取现有的 vnt_pid.yaml
2. 获取当前所有 vnt_helper.exe 进程
3. 对比新旧 PID 列表
4. 只记录新增的 PID
5. 备份旧文件为 vnt_pid_backup.yaml
```

**关键代码**:
```python
if last_total_num_of_pid is None:
    # 首次运行，记录所有进程
    vnt_pid.set_value(VNT_Config.KEY_TOTAL_NUMBER_OF_PID, num)
    for i in range(num):
        self.PIDs.append(pid[i])
        vnt_pid.set_value(str(i), pid[i])
else:
    # 增量更新，只记录新进程
    shutil.copy(PID_FILE, PID_FILE_BACKUP)
    last_PIDs = [...]  # 读取旧 PID 列表
    for i in range(num):
        if pid[i] not in last_PIDs:
            self.PIDs.append(pid[i])
            vnt_pid.set_value(str(j), pid[i])
```

##### 模式 2: `"remove"` - 清理 PID 文件
```python
# 程序退出时调用
os.remove(vnt_pid.yaml)
os.remove(vnt_pid_backup.yaml)
```

**用途**:
- 📌 区分"自己的进程"和"其他实例"
- 📌 避免误杀自己启动的进程
- 📌 支持 PyInstaller 多进程特性

---

### 3️⃣ 已存在进程处理机制

#### 方法：`_clear_existing_process(exe_nm, background_run)`

**位置**: `vnt_helper.py` 第 147-226 行

**这是核心逻辑！**

#### 处理流程图

```
检测到已存在的进程
    │
    ├─ 判断: 进程数 > 2 ?
    │   └── PyInstaller 生成 2 个进程（临时清理 + 主程序）
    │
    ├─ NO (≤2) → 正常启动
    │
    └─ YES (>2) → 进入处理逻辑
        │
        ├─ 判断: 是否后台运行 OR 使用 -k 参数?
        │   │
        │   ├─ YES → can_clean_processes = True
        │   │
        │   └─ NO → 弹出对话框询问用户
        │       │
        │       └─ "已有 N 个实例运行，是否启动新会话？"
        │           ├── 用户选"是" → can_clean_processes = True
        │           └── 用户选"否" → 恢复 PID 文件并退出
        │
        └─ can_clean_processes = True
            │
            ├─ 步骤 1: 发送优雅退出信号
            │   └── notify_vnt_helper_to_exit()
            │       ├── 连接命名管道 \\.\pipe\vnt_helper_pipe
            │       ├── 发送 "VNT_HELPER_EXIT" 信号
            │       ├── 等待 "VNT_HELPER_EXIT_ACK" 响应
            │       └── 超时: 10 次重试，每次间隔 1 秒
            │
            ├─ 步骤 2: 等待进程退出 (10 秒超时)
            │   └── t.join(timeout=10)
            │
            ├─ 步骤 3A: 优雅退出成功
            │   │
            │   ├─ 遍历所有旧进程 PID
            │   │   └── 排除当前进程 PID
            │   │
            │   ├─ 检查每个 PID 是否仍在运行
            │   │   └── is_process_running(p)
            │   │
            │   └─ 如有残留 → 等待 10 秒后强制杀进程
            │
            └─ 步骤 3B: 优雅退出失败 (超时/管道错误)
                │
                └─ 强制杀进程
                    └── kill_process(exe_nm, os.getpid())
```

---

### 4️⃣ 进程间通信 (命名管道)

#### 优雅退出信号机制

**管道名称**: `\\.\pipe\vnt_helper_pipe`

**信号定义**:
```python
EXIT_SIGNAL = "VNT_HELPER_EXIT"
EXIT_SIGNAL_ACK = "VNT_HELPER_EXIT_ACK"
CLOSE_EXIT_SIGNAL_PROCESS = "SIGNAL_PROCESS_EXIT"
CLOSE_EXIT_SIGNAL_ACK = "SIGNAL_PROCESS_ACK "
```

#### 发送端 (新进程)

**位置**: `notify_vnt_helper_to_exit()` 内部函数

```python
def notify_vnt_helper_to_exit(normal_exit_flag):
    # 1. 连接管道 (最多重试 10 次)
    for i in range(10):
        try:
            self.client_pipe = win32file.CreateFile(
                self.PIPE_NAME,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None
            )
            break
        except Exception as e:
            time.sleep(1)
    
    # 2. 发送退出信号
    win32file.WriteFile(self.client_pipe, EXIT_SIGNAL.encode('utf-8'))
    
    # 3. 等待确认
    data = win32file.ReadFile(self.client_pipe, 65536)[1].decode('utf-8')
    if data == EXIT_SIGNAL_ACK:
        normal_exit_flag.set()  # 标记成功
```

#### 接收端 (旧进程)

**位置**: `_process_exit_signal()` 方法

```python
def _process_exit_signal(self):
    # 1. 创建命名管道服务器
    self.server_pipe = win32pipe.CreateNamedPipe(
        self.PIPE_NAME,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1, 65536, 65536, 0, None
    )
    
    # 2. 等待客户端连接
    win32pipe.ConnectNamedPipe(self.server_pipe, None)
    
    # 3. 读取信号
    data = win32file.ReadFile(self.server_pipe, 65536)[1].decode('utf-8')
    
    # 4. 处理信号
    if data == EXIT_SIGNAL:
        # 回复确认
        win32file.WriteFile(self.server_pipe, EXIT_SIGNAL_ACK.encode('utf-8'))
        # 触发退出流程
        self.stop()
    elif data == CLOSE_EXIT_SIGNAL_PROCESS:
        win32file.WriteFile(self.server_pipe, CLOSE_EXIT_SIGNAL_ACK.encode('utf-8'))
```

**通信时序图**:
```
新进程                          旧进程
  │                              │
  ├── CreateFile(PIPE) ─────────>│
  │                              │
  ├── WriteFile("VNT_HELPER_EXIT")>
  │                              │
  │  <── ConnectNamedPipe ───────┤
  │  <── ReadFile ───────────────┤
  │                              │
  │                              ├─ 执行清理逻辑
  │                              ├─ stop()
  │                              │
  ├── WriteFile("VNT_HELPER_EXIT_ACK")
  │  <───────────────────────────┤
  │                              │
  ├─ normal_exit_flag.set()      │
  └─ 继续后续流程                └─ 退出
```

---

### 5️⃣ 强制杀进程机制

#### 方法：`kill_process(proc1, pid_to_exclude)`

**位置**: `vnt_helper.py` 第 508-522 行

**实现**:
```python
def kill_process(self, proc1, pid_to_exclude=None):
    is_vnt_helper_on, num, pid = self._get_process_list(proc1)
    
    if is_vnt_helper_on:
        for p in pid:
            if p != pid_to_exclude:  # 排除自己
                try:
                    os.kill(p, signal.SIGINT)  # 发送 SIGINT
                except Exception as e:
                    self.logger.write(f"Killing Process Pid: {p} {e}", 'critical')
                    return False
        return True
    return False
```

**特点**:
- ✅ 使用 `signal.SIGINT` (而非 SIGKILL)
- ✅ 支持排除特定 PID (避免自杀)
- ⚠️ Windows 下 SIGINT 等同于 Ctrl+C
- ⚠️ 可能需要管理员权限

---

### 6️⃣ 守护进程中的进程管理

#### vnt_daemon.py 中的 vnt-cli 管理

**位置**: `vnt_daemon.py` 多处

##### 启动 vnt-cli
```python
def start_vnt_cli(self):
    cmd = [str(self.vnt_cli_path), "-f", str(self.config_path)]
    self.vnt_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW  # 隐藏窗口
    )
    # 启动日志读取线程
    threading.Thread(target=self._log_reader_thread, daemon=True).start()
```

##### 停止 vnt-cli (两步策略)
```python
def stop_vnt_cli_network(self):
    # 步骤 1: 尝试优雅停止
    cmd = [str(self.vnt_cli_path), "--stop"]
    p = subprocess.Popen(cmd, ...)
    
    if p.poll() is None:  # --stop 失败
        # 步骤 2: 终止进程
        self.vnt_process.terminate()
        try:
            self.vnt_process.wait(timeout=5)  # 等待 5 秒
        except subprocess.TimeoutExpired:
            self.vnt_process.kill()  # 强制杀
```

##### 监控循环
```python
def monitor_vnt_cli(self):
    while self.running:
        if self.vnt_process.poll() is None:  # 仍在运行
            # 检查网络状态
            if self.need_to_check_internet and not is_server_connected():
                self.stop_vnt_cli_network()
        else:  # 进程已退出
            # 自动重启 (如配置了 autorun)
            if is_connected() and autorun_enabled():
                self.start_vnt_cli()
```

---

### 7️⃣ 更新器中的进程管理

#### vnt_updater.py 中的进程清理

**位置**: `vnt_updater.py` 多处

##### 等待进程退出
```python
def wait_for_process_exit(self, process_name, max_wait_sec=60, force_after_sec=30):
    for i in range(max_wait_sec):
        if not self.get_running_pids(process_name):
            return  # 进程已退出
        if i == force_after_sec:
            self.kill_processes(process_name)  # 强制杀
        time.sleep(1)
```

##### 更新流程中的清理
```python
def run(self):
    # 1. 等待主程序退出 (60秒超时，30秒后强制杀)
    self.wait_for_process_exit(self.main_exe, max_wait_sec=60, force_after_sec=30)
    
    # 2. 等待服务退出 (11秒超时，10秒后强制杀)
    self.wait_for_process_exit(self.service_exe, max_wait_sec=11, force_after_sec=10)
    
    # 3. 等待 CLI 退出
    self.wait_for_process_exit(self.cli_exe, max_wait_sec=11, force_after_sec=10)
    
    # 4. 删除旧文件
    self.delete_old_files()
    
    # 5. 部署新版本
    self.deploy_update()
    
    # 6. 启动新版本
    self.launch_updated_program()
```

---

## 📊 进程状态转换图

```
┌─────────────────────────────────────────────────────────────┐
│                    vnt_helper.exe 生命周期                   │
└─────────────────────────────────────────────────────────────┘

启动
  │
  ├─ 请求管理员权限
  │   └── 失败 → 退出
  │
  ├─ 注册 PID (vnt_pid.yaml)
  │
  ├─ 检测已存在实例
  │   ├── 无 → 继续
  │   ├── 有 → 发送退出信号
  │   │   ├── 成功 → 等待退出
  │   │   └── 失败 → 强制杀进程
  │   └── 用户拒绝 → 退出
  │
  ├─ 创建退出信号监听管道
  │
  ├─ 正常运行
  │   ├── 接收退出信号
  │   │   └── 执行 stop()
  │   │       ├── 关闭网络连接
  │   │       ├── 停止守护进程
  │   │       ├── 关闭 GUI
  │   │       ├── 删除 PID 文件
  │   │       └── 退出
  │   │
  │   └── 用户关闭窗口
  │       └── 执行 stop()
  │
  └─ 退出
```

---

## 🔐 安全特性

### 1. 防多实例机制
```python
# 检测条件
if is_vnt_helper_on and num > 2:
    # PyInstaller 生成 2 个进程
    # 实际实例数 = num - 2
```

**原因**:
- PyInstaller --onefile 模式会生成临时进程
- 正常情况: 2 个进程 (临时清理 + 主程序)
- 多实例: > 2 个进程

### 2. PID 白名单
```python
# 只杀不在白名单中的进程
for p in pid:
    if p not in self.PIDs:  # 旧进程
        kill(p)
    # 跳过 self.PIDs 中的进程 (自己)
```

### 3. 优雅关闭优先
```python
# 三级退出策略
Level 1: 命名管道信号 (VNT_HELPER_EXIT)
   ↓ 超时 10 秒
Level 2: signal.SIGINT
   ↓ 超时 10 秒
Level 3: 强制终止 (os.kill)
```

---

## ⚠️ 潜在问题

### 问题 1: 竞态条件
**场景**: 两个实例同时启动
```
实例 A 启动 → 检测到实例 B → 发送退出信号
实例 B 启动 → 检测到实例 A → 发送退出信号
结果: 互相杀死
```

**缓解**: PID 文件管理 + 用户确认对话框

---

### 问题 2: 管道连接失败
**场景**: 旧进程未创建管道或已崩溃
```python
# 超时机制
for i in range(10):
    try:
        CreateFile(PIPE_NAME)
        break
    except:
        time.sleep(1)

if i >= 9:
    # 降级为强制杀进程
    kill_process()
```

---

### 问题 3: 权限不足
**场景**: 非管理员无法杀进程
```python
try:
    os.kill(p, signal.SIGINT)
except Exception as e:
    # 可能需要管理员权限
    self.logger.write(f"Killing Process Pid: {p} {e}", 'critical')
```

**解决**: 程序启动时请求管理员权限

---

### 问题 4: 僵尸进程
**场景**: 进程退出但 PID 文件未清理
```python
# 启动时检查
if not os.path.exists(PID_FILE):
    if self._process_PID("set"):
        self.logger.write(f"Re-establish {PID_FILE}")
```

---

## 📝 日志示例分析

### 正常启动 (无其他实例)
```
PID 12345 : ******************* Start New Session *******************
PID 12345 : Temp Folder: C:\Users\...\Temp\_MEI12345\
PID 12345 : Reource file vnt-cli.exe already exists
PID 12345 : Exit_Daemon Established. Waiting for data...
```

### 检测到其他实例
```
PID 12346 : ******************* Start New Session *******************
PID 12346 : Currently total pid in running: [12340, 12341, 12342]
PID 12346 : PIDs of this instance: [12346, 12347]
PID 12346 : Last PID not in Current PID Table= 12340
PID 12346 : Signal Received: VNT_HELPER_EXIT, reply ACK...
PID 12346 : Exit signal server thread ends...
PID 12346 : ******************* Session Ends *******************
```

### 强制杀进程
```
PID 12346 : Signal Failed. Force killing process .......
PID 12346 : Killing Process vnt_helper.exe: 3 process(es) found
PID 12346 : Killed PID 12340 : vnt_helper.exe
PID 12346 : Killed PID 12341 : vnt_helper.exe
PID 12346 : Killed PID 12342 : vnt_helper.exe
```

---

## 🎯 总结

### 设计优点

✅ **多层次清理**: 信号 → SIGINT → 强制杀  
✅ **PID 追踪**: 避免误杀自己的进程  
✅ **用户友好**: 图形界面询问用户  
✅ **容错机制**: 超时降级策略  
✅ **日志完善**: 详细记录每个步骤  

### 适用场景

- ✅ 单机多实例管理
- ✅ 程序更新时的进程替换
- ✅ 异常崩溃后的清理
- ✅ 系统服务与 GUI 的协调

### 改进建议

1. **增加进程心跳检测**: 定期写入 PID 文件，判断进程是否存活
2. **使用互斥体 (Mutex)**: Windows 原生防多实例机制
3. **增加超时配置**: 允许用户自定义等待时间
4. **改进日志**: 区分"自己的进程"和"其他实例"的日志

---

*分析完成日期: 2026-05-13*  
*分析文件: vnt_helper.py, vnt_daemon.py, vnt_updater.py*
