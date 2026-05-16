# VNT Daemon IPC命令调试日志添加说明

## 背景

由于 vnt_service.exe 是编译后的可执行文件，作为 Windows 服务运行时无法直接监控其内部执行情况。为了诊断 Toggle on/off VNT 功能失灵的问是，需要在关键位置添加详细的调试日志。

## 修改内容

### 文件: [vnt_daemon.py](file://c:\RussApp\v4\vnt_daemon.py)

**位置**: `handle_ipc_command()` 方法（第480-570行）

### 添加的调试日志点

#### 1. 方法入口和出口
```python
self.logger.write(f"[DEBUG] ===== IPC Command Handler Entered =====", "info")
self.logger.write(f"[DEBUG] Connection from: {addr}", "info")
# ... 处理逻辑 ...
self.logger.write(f"[DEBUG] ===== IPC Command Handler Exited =====", "info")
```

#### 2. 数据接收和解析
```python
self.logger.write(f"[DEBUG] Waiting to receive data...", "info")
data = conn.recv(1024).decode('utf-8')
self.logger.write(f"[DEBUG] Received raw data: '{data}'", "info")
cmd = json.loads(data)
self.logger.write(f"[DEBUG] Parsed command: {cmd}", "info")
```

#### 3. start 命令处理
```python
self.logger.write(f"[DEBUG] Processing 'start' command", "info")
self.logger.write(f"[DEBUG] Current toggled_off state: {self.toggled_off}", "info")
self.logger.write(f"[DEBUG] Calling start_vnt_cli()...", "info")
success = self.start_vnt_cli()
self.logger.write(f"[DEBUG] start_vnt_cli() returned: {success}", "info")
self.logger.write(f"[DEBUG] Sending response: {resp}", "info")
self.logger.write(f"[DEBUG] Response sent successfully", "info")
```

#### 4. stop_network 命令处理
```python
self.logger.write(f"[DEBUG] Processing 'stop_network' command", "info")
self.logger.write(f"[DEBUG] Current toggled_off state: {self.toggled_off}", "info")
self.logger.write(f"[DEBUG] Setting toggled_off=True", "info")
self.toggled_off = True
self.logger.write(f"[DEBUG] Calling stop_vnt_cli_network()...", "info")
success = self.stop_vnt_cli_network()
self.logger.write(f"[DEBUG] stop_vnt_cli_network() returned: {success}", "info")
self.logger.write(f"[DEBUG] Sending response: {resp}", "info")
self.logger.write(f"[DEBUG] Response sent successfully", "info")
```

#### 5. restart 命令处理
```python
self.logger.write(f"[DEBUG] Processing 'restart' command", "info")
self.logger.write(f"[DEBUG] Current vnt_process running state: {running}", "info")
self.logger.write(f"[DEBUG] Calling stop_vnt_cli_network()...", "info")
self.logger.write(f"[DEBUG] Waiting 1 second before restart...", "info")
self.logger.write(f"[DEBUG] Calling start_vnt_cli()...", "info")
self.logger.write(f"[DEBUG] start_vnt_cli() returned: {success}", "info")
```

#### 6. status 命令处理
```python
self.logger.write(f"[DEBUG] Processing 'status' command", "info")
self.logger.write(f"[DEBUG] Status data: {status_data}", "info")
self.logger.write(f"[DEBUG] Status response sent", "info")
```

#### 7. exit 命令处理
```python
self.logger.write(f"[DEBUG] Processing 'exit' command", "info")
self.logger.write(f"[DEBUG] Setting running=False", "info")
self.logger.write(f"[DEBUG] Calling stop_vnt_cli_network()...", "info")
self.logger.write(f"[DEBUG] Waiting for vnt_process to terminate (timeout=5s)...", "info")
self.logger.write(f"[DEBUG] vnt_process terminated successfully", "info")
self.logger.write(f"[DEBUG] Sending exit confirmation", "info")
self.logger.write(f"[DEBUG] Exit confirmation sent", "info")
```

#### 8. 异常处理
```python
except json.JSONDecodeError:
    self.logger.write(f"[DEBUG] JSON decode error, closing connection", "warning")
except Exception as e:
    self.logger.write(f"[DEBUG] IPC exception caught: {type(e).__name__}: {e}", "critical")
    import traceback
    self.logger.write(f"[DEBUG] Traceback:\n{traceback.format_exc()}", "critical")
```

## 验证结果

### 调试日志点统计
```
✅ 12/12 个关键调试日志点已添加
✅ 3/3 个异常处理日志点已添加
✅ Python语法检查通过
```

### 覆盖范围
- ✅ 方法入口/出口
- ✅ 数据接收和解析
- ✅ 所有IPC命令处理分支（start/stop_network/restart/status/exit）
- ✅ 关键函数调用前后
- ✅ 状态变更
- ✅ 响应发送
- ✅ 异常情况

## 使用指南

### 1. 重启服务
```powershell
net stop VNTDaemonService
net start VNTDaemonService
```

### 2. 执行Toggle操作
- 右键托盘图标
- 点击 "Toggle on VNT Connection" 或 "Toggle off VNT Connection"

### 3. 查看日志
```powershell
Get-Content vnt_cli.log -Tail 100 | Select-String "[DEBUG]"
```

### 4. 预期日志输出示例

#### Toggle OFF 场景
```
[DEBUG] ===== IPC Command Handler Entered =====
[DEBUG] Connection from: ('127.0.0.1', 58433)
[DEBUG] Waiting to receive data...
[DEBUG] Received raw data: '{"cmd": "stop_network"}'
[DEBUG] Parsed command: {'cmd': 'stop_network'}
Received [stop_network] command via IPC.
[DEBUG] Processing 'stop_network' command
[DEBUG] Current toggled_off state: False
[DEBUG] Setting toggled_off=True
[DEBUG] Calling stop_vnt_cli_network()...
[DEBUG] stop_vnt_cli_network() returned: True
[DEBUG] Sending response: {'status': 'ok'}
[DEBUG] Response sent successfully
[DEBUG] ===== IPC Command Handler Exited =====
```

#### Toggle ON 场景
```
[DEBUG] ===== IPC Command Handler Entered =====
[DEBUG] Connection from: ('127.0.0.1', 58434)
[DEBUG] Waiting to receive data...
[DEBUG] Received raw data: '{"cmd": "start"}'
[DEBUG] Parsed command: {'cmd': 'start'}
Received [start] command via IPC.
[DEBUG] Processing 'start' command
[DEBUG] Current toggled_off state: True
[DEBUG] Calling start_vnt_cli()...
[DEBUG] start_vnt_cli() returned: True
[DEBUG] Set toggled_off=False, gui_request_vnt_connection=True
[DEBUG] Sending response: {'status': 'ok'}
[DEBUG] Response sent successfully
[DEBUG] ===== IPC Command Handler Exited =====
```

## 诊断要点

### 如果看到这些日志，说明IPC通信正常：
- ✅ `[DEBUG] Received raw data:` - 收到数据
- ✅ `[DEBUG] Parsed command:` - 成功解析JSON
- ✅ `[DEBUG] Processing 'xxx' command` - 进入对应命令处理
- ✅ `[DEBUG] Response sent successfully` - 响应发送成功

### 如果缺少这些日志，说明问题在：
- ❌ GUI端未发送命令
- ❌ IPC连接建立失败
- ❌ 数据格式错误导致JSON解析失败

### 如果看到这些日志但功能仍失灵：
- ⚠️ 检查 `[DEBUG] xxx returned: False` - 函数执行失败
- ⚠️ 检查是否有异常日志 `[DEBUG] IPC exception caught:`
- ⚠️ 检查 `stop_vnt_cli_network()` 或 `start_vnt_cli()` 的内部日志

## 相关文件

- 📄 [vnt_daemon.py](vnt_daemon.py) - 添加调试日志
- 📄 [vnt_helper.py](vnt_helper.py) - GUI端IPC发送逻辑（已修复）
- 📄 [TOGGLE_IPC_FIX.md](TOGGLE_IPC_FIX.md) - Toggle功能修复说明

---

**添加完成时间**: 2026-05-15  
**状态**: ✅ 已完成  
**影响范围**: IPC命令处理流程  
**风险等级**: 🟢 低（仅添加日志，不影响功能）  
**日志级别**: INFO（确保会被记录）
