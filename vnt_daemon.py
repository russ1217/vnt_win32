# vnt_daemon.py
import os
import sys
import time
import json
import socket
import logging
import logging.handlers
import subprocess
import threading
import re
import yaml
import toml
import pythoncom
import psutil
from pathlib import Path
from datetime import datetime, timedelta
from win32com.client import Dispatch

VNT_CLI = "vnt2_cli.exe"
VNT_LOG_FILE = "vnt_cli.log"
VNT_HELPER_CONFIG_FILE = "vnt_helper.yaml"
IPC_HOST = "127.0.0.1"
DEFAULT_IPC_PORT = 58432
LOG_LEVEL = logging.DEBUG


class VNTDaemon():
    # ===== 关键事件匹配规则 (VNT 2.0) =====
    EVENT_PATTERNS = [
        # IP地址分配 - VNT 2.0格式: "Registration completed, IP: 10.10.0.18, prefix_len: 24"
        (re.compile(r"Registration completed, IP:\s*([0-9.]+)"), "IP_assigned"),

        # 连接成功 - VNT 2.0中文输出: "已连接服务器:quic://..."
        (re.compile(r"已连接服务器:"), "connected"),

        # 错误信息
        (re.compile(r"ERROR"), "error"),

        # 配置错误
        (re.compile(r"Error conf"), "error_conf"),

        # 重连计数 - VNT 2.0可能使用不同格式，暂时保留
        (re.compile(r"connect count="), "reconnect_count"),

        # 版本信息 - VNT 2.0格式: 'version: "2.0.0"'
        (re.compile(r'version:\s*"([^"]+)"'), "version_info"),

        # 序列号 - VNT 2.0格式待确认，暂时保留原模式
        (re.compile(r"Serial:"), "serial_info"),

        # 服务器连接/版本 - VNT 2.0可能在注册时显示
        (re.compile(r"server version="), "server_connection"),
    ]

    MAX_RECONNECT_COUNT = 10

    def __init__(self):
        self.working_dir = Path(self._get_working_dir())
        self.vnt_cli_path = os.path.join(self.working_dir, VNT_CLI)
        self.config_path = None
        self.vnt_process = None
        self.running = True
        self.vnt_cli_switched_on = False
        self.toggled_off = False  # 初始为False，允许自动启动
        self.logger = VNT_Logger(self.working_dir, VNT_LOG_FILE)
        self.ipc_server = None
        self.gui_event_conn = None
        self.event_push_lock = threading.Lock()
        self.virtual_ip = None
        self.ver = '0.0.0'
        self.serial = 'unknown'
        self.server_version = '0.0.0'

        # GUI 连接跟踪
        self.gui_connections = set()  # 跟踪所有连接的 GUI 客户端
        self.gui_connection_lock = threading.Lock()
        self.last_gui_disconnect_time = None

        self.inet_monitor = Internet_Connectivity_Monitor(self.logger)
        self.inet_monitor.start()
        self.need_to_check_internet = False

        # ⭐ 重构状态管理：清晰区分不同启动模式
        self.auto_start_mode = False  # Session 0 服务模式：服务启动后自动运行CLI
        self.gui_started_cli = False  # Session 1 GUI模式：CLI由GUI的"start"命令启动

        # 读取IPC端口配置
        try:
            vnt_conf = VNT_Config(self.working_dir, VNT_HELPER_CONFIG_FILE, self.logger)
            ipc_port_reading = vnt_conf.get_value(VNT_Config.KEY_IPC_PORT)

            # Validate that the IPC port is a reasonable value
            if ipc_port_reading is not None:
                try:
                    port_num = int(ipc_port_reading)
                    if not (1 <= port_num <= 65535):
                        self.logger.write(
                            f"Invalid IPC port value from config: {port_num} (must be between 1-65535), using default", 'critical')
                        self.IPC_PORT = DEFAULT_IPC_PORT  # Use default port
                    else:
                        # Valid port number, use the configured value
                        self.IPC_PORT = port_num
                        self.logger.write(f"Using configured IPC port: {port_num}", 'info')
                except (ValueError, TypeError):
                    self.logger.write(
                        f"Invalid IPC port value from config: {ipc_port_reading} (not a valid number), using default",
                        'critical')
                    self.IPC_PORT = DEFAULT_IPC_PORT  # Use default port
            else:
                # If no port is configured, use the default
                self.IPC_PORT = DEFAULT_IPC_PORT
                self.logger.write(f"No IPC port configured, using default: {self.IPC_PORT}", 'info')
        except Exception as e:
            self.logger.write(f"Error reading IPC port from config: {e}", 'critical')
            # In case of exception during reading, use default port
            self.IPC_PORT = DEFAULT_IPC_PORT

    def _get_working_dir(self):

        if getattr(sys, 'frozen', False):
            # EXE path, in case autorun with REGISTRY, it becomes WINDOWS\SYSTEM32
            curpath = os.path.dirname(sys.executable)
        elif __file__:
            curpath = os.path.dirname(os.path.abspath(__file__))

        return curpath

    def _convert_yaml_to_toml(self, yaml_path):
        """将YAML配置文件转换为TOML格式，返回TOML文件路径
        YAML已经使用VNT2字段格式，此处仅做格式转换（YAML→TOML）
        注意：VNT2 CLI 要求 server 字段为数组格式
        """
        try:
            self.logger.write(f"Starting YAML to TOML conversion: {yaml_path}", "info")

            # 检查文件是否存在
            if not Path(yaml_path).exists():
                self.logger.write(f"YAML config file does not exist: {yaml_path}", "critical")
                return None

            # 读取YAML文件
            with open(yaml_path, 'r', encoding='utf-8') as f:
                yaml_data = yaml.safe_load(f)

            if not yaml_data:
                self.logger.write("YAML config is empty", "critical")
                return None

            self.logger.write(f"YAML data loaded successfully, keys: {list(yaml_data.keys())}", "info")

            # VNT2 YAML已经使用正确的字段名和格式，直接转换为TOML
            # 只需确保必填字段存在
            if 'network_code' not in yaml_data:
                self.logger.write("Error: network_code is missing in YAML config", "critical")
                return None

            if 'server' not in yaml_data:
                self.logger.write("Error: server is missing in YAML config", "critical")
                return None

            # VNT2 CLI 要求 server 字段为数组格式
            # 如果 server 是字符串，转换为单元素数组
            if isinstance(yaml_data['server'], str):
                yaml_data['server'] = [yaml_data['server']]
                self.logger.write(f"Converted server to array: {yaml_data['server']}", "info")
            elif not isinstance(yaml_data['server'], list):
                self.logger.write(f"Error: server has invalid type: {type(yaml_data['server'])}", "critical")
                return None

            # 生成TOML文件路径（与YAML同目录，扩展名改为.toml）
            toml_path = str(Path(yaml_path).with_suffix('.toml'))

            self.logger.write(f"TOML data prepared, writing to: {toml_path}", "info")
            self.logger.write(f"TOML keys: {list(yaml_data.keys())}", "info")

            # 直接写入TOML文件（字段已符合VNT2规范）
            with open(toml_path, 'w', encoding='utf-8') as f:
                toml.dump(yaml_data, f)

            self.logger.write(f"Successfully converted YAML to TOML: {yaml_path} -> {toml_path}", "info")
            return toml_path

        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            self.logger.write(f"Failed to convert YAML to TOML: {e}", "critical")
            self.logger.write(f"Traceback: {error_trace}", "critical")
            return None

    def start_vnt_cli(self):
        """Start vnt2_cli.exe with robust process management"""
        vnt_conf = VNT_Config(self.working_dir, VNT_HELPER_CONFIG_FILE, self.logger)
        self.config_path = vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)

        self.logger.write(f"Config path from vnt_helper.yaml: {self.config_path}", "info")

        i = 0

        while not self.config_path or not Path(self.config_path).exists():
            if i % 600 == 0:
                self.logger.write(f"VNTDaemon: Waiting for valid config file: {self.config_path}", "info")
            i += 1
            if not self.running:
                return False
            time.sleep(1)

        self.logger.write(f"Config file exists: {self.config_path}", "info")

        while not self.inet_monitor.is_connected() or not Internet_Connectivity_Monitor.is_server_connected():
            if not self.running:
                return False
            time.sleep(1)

        self.logger.write("Internet connection verified", "info")

        # ⭐ 关键修复：彻底检查并清理任何残留的 vnt2_cli.exe 进程
        self._cleanup_orphaned_cli_processes()

        # Double-check: ensure no CLI process is running before starting new one
        if self._is_cli_process_running():
            self.logger.write(
                "VNTDaemon: vnt-cli is already running (verified by process scan), skipping start",
                "info")
            return True

        # 将YAML配置转换为TOML格式
        self.logger.write("Starting YAML to TOML conversion...", "info")
        toml_config_path = self._convert_yaml_to_toml(self.config_path)
        if not toml_config_path:
            self.logger.write("Failed to convert YAML to TOML, cannot start vnt2_cli.exe", "critical")
            return False

        self.logger.write(f"TOML config created: {toml_config_path}", "info")

        # 检查vnt2_cli.exe是否存在
        if not Path(self.vnt_cli_path).exists():
            self.logger.write(f"vnt2_cli.exe not found at: {self.vnt_cli_path}", "critical")
            return False

        self.logger.write(f"vnt2_cli.exe found at: {self.vnt_cli_path}", "info")

        cmd = [str(self.vnt_cli_path), "--conf", str(toml_config_path)]
        self.logger.write(f"Executing command: {' '.join(cmd)}", "info")

        try:
            # Clean up old process object before creating new one
            if self.vnt_process:
                try:
                    # Ensure old process is fully cleaned up
                    if self.vnt_process.poll() is None:
                        self.logger.write("Cleaning up old vnt_process object before starting new one", "warning")
                        self.vnt_process.terminate()
                        try:
                            self.vnt_process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            self.vnt_process.kill()
                            self.vnt_process.wait()
                except Exception as e:
                    self.logger.write(f"Error cleaning up old process object: {e}", "warning")
                finally:
                    self.vnt_process = None

            self.vnt_process = subprocess.Popen(
                cmd,
                cwd=self.working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                text=True,
                encoding='utf-8',
                errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            self.logger.write(f"vnt2_cli.exe process started with PID: {self.vnt_process.pid}", "info")

            # Verify the process actually started and is running
            time.sleep(0.5)  # Give it a moment to initialize
            if self.vnt_process.poll() is not None:
                # Process exited immediately
                exit_code = self.vnt_process.poll()
                self.logger.write(f"CRITICAL: vnt2_cli.exe exited immediately with code {exit_code}", "critical")

                # Try to read any error output
                try:
                    stdout, stderr = self.vnt_process.communicate(timeout=1)
                    if stdout:
                        self.logger.write(f"CLI stdout: {stdout}", "error")
                    if stderr:
                        self.logger.write(f"CLI stderr: {stderr}", "error")
                except Exception:
                    pass

                self.vnt_process = None
                return False

            threading.Thread(target=self._log_reader_thread, daemon=True).start()
            self.logger.write(f"Started vnt2_cli.exe with TOML config: {toml_config_path}", "info")
            return True
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            self.logger.write(f"Failed to start vnt2_cli.exe: {e}", "critical")
            self.logger.write(f"Traceback: {error_trace}", "critical")
            self.vnt_process = None
            return False

    def _is_cli_process_running(self):
        """Check if any vnt2_cli.exe process is actually running in the system"""
        try:
            cli_name = os.path.basename(self.vnt_cli_path)
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    if proc.info['name'] and cli_name.lower() in proc.info['name'].lower():
                        # Found a running CLI process
                        self.logger.write(f"Found existing vnt2_cli.exe process with PID: {proc.info['pid']}", "info")
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return False
        except Exception as e:
            self.logger.write(f"Error checking for CLI processes: {e}", "warning")
            return False

    def _cleanup_orphaned_cli_processes(self):
        """Kill any orphaned vnt2_cli.exe processes that are not managed by this daemon"""
        try:
            cli_name = os.path.basename(self.vnt_cli_path)
            killed_count = 0

            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    if proc.info['name'] and cli_name.lower() in proc.info['name'].lower():
                        # Check if this is our managed process
                        if self.vnt_process and proc.info['pid'] == self.vnt_process.pid:
                            self.logger.write(f"Skipping our own managed process PID: {proc.info['pid']}", "debug")
                            continue

                        # This is an orphaned process, kill it
                        self.logger.write(f"Killing orphaned vnt2_cli.exe process PID: {proc.info['pid']}", "warning")
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                            self.logger.write(
                                f"Successfully terminated orphaned process PID: {
                                    proc.info['pid']}", "info")
                        except psutil.TimeoutExpired:
                            self.logger.write(f"Force killing orphaned process PID: {proc.info['pid']}", "warning")
                            proc.kill()
                            proc.wait()
                        killed_count += 1

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if killed_count > 0:
                self.logger.write(f"Cleaned up {killed_count} orphaned vnt2_cli.exe process(es)", "info")
                # Give system time to clean up
                time.sleep(1)

        except Exception as e:
            self.logger.write(f"Error cleaning up orphaned CLI processes: {e}", "warning")

    def stop_vnt_cli_network(self):
        """仅关闭网络连接（VNT 2.0不支持--stop命令，直接终止进程）"""
        if self.vnt_process and self.vnt_process.poll() is None:
            try:
                self.logger.write(f"Attempting to stop vnt-cli process (PID: {self.vnt_process.pid})", "info")

                # VNT 2.0不支持--stop命令，直接终止进程
                self.logger.write(f"Terminating vnt-cli process...", "info")
                self.vnt_process.terminate()

                try:
                    # Wait for the process to terminate gracefully (up to 5 seconds)
                    self.logger.write(f"Waiting for process to terminate (timeout=5s)...", "info")
                    self.vnt_process.wait(timeout=5)
                    self.logger.write(f"Process terminated successfully", "info")
                except subprocess.TimeoutExpired:
                    # If it doesn't terminate gracefully, force kill it
                    self.logger.write("vnt-cli did not terminate gracefully, forcing kill", "info")
                    self.vnt_process.kill()
                    self.vnt_process.wait()  # Wait for the kill to complete
                    self.logger.write(f"Process killed forcefully", "info")

                self.virtual_ip = None
                self.logger.write("Successfully stopped vnt-cli process", "info")
                return True

            except Exception as e:
                self.logger.write(f"Could not stop vnt-cli process: {type(e).__name__}: {e}", "critical")
                import traceback
                self.logger.write(f"Traceback:\n{traceback.format_exc()}", "critical")
                return False
        else:
            self.logger.write("vnt-cli process is not running or already stopped", "info")
            return True

    def _parse_and_push_event(self, line: str):
        for pattern, event_type in self.EVENT_PATTERNS:
            match = pattern.search(line)
            if match:
                payload = {"event": event_type}
                if event_type == "IP_assigned":
                    ip = match.group(1) if match.groups() else "unknown"
                    payload["ip"] = ip
                    self.virtual_ip = ip
                    self.need_to_check_internet = False
                elif event_type == "version_info":
                    # VNT 2.0格式: version: "2.0.0"，使用正则捕获组提取版本号
                    if match.groups():
                        self.ver = match.group(1)
                    else:
                        self.ver = "unknown"
                    payload["version"] = self.ver
                elif event_type == "serial_info":
                    # VNT 2.0格式待确认，如果有捕获组则使用，否则使用原有逻辑
                    if match.groups():
                        self.serial = match.group(1)
                    else:
                        self.serial = line.split(":")[-1].strip() if "Serial:" in line else "unknown"
                    payload["serial"] = self.serial
                elif event_type == "server_connection":
                    # VNT 2.0格式待确认，如果有捕获组则使用，否则使用原有逻辑
                    if match.groups():
                        self.server_version = match.group(1)
                    else:
                        self.server_version = line.split(
                            "server version=")[-1].strip() if "server version=" in line else "unknown"
                    payload["server_version"] = self.server_version
                elif event_type == "reconnect_count":
                    count_match = re.search(r'connect count=(\d+)', line)
                    if count_match:
                        payload["connect_count"] = count_match.group(1)
                        count = int(count_match.group(1))
                        if count > self.MAX_RECONNECT_COUNT:
                            self.logger.write(
                                f"Reconnection count {count} exceeds {
                                    self.MAX_RECONNECT_COUNT}, restarting vnt_cli.exe", "info")
                            # 重启 vnt_cli.exe
                            self.stop_vnt_cli_network()
                            # 等待一段时间再重启
                            time.sleep(10)
                            self.start_vnt_cli()
                        elif count > 3:
                            self.need_to_check_internet = True
                    else:
                        payload["message"] = line
                elif event_type in ["error", "error_conf"]:
                    payload["message"] = line
                    self.need_to_check_internet = True
                else:
                    payload["message"] = line
                self._push_event_to_gui(payload)
                print(f"Event detected: {payload}")
                break

    def _push_event_to_gui(self, event_dict: dict):
        with self.event_push_lock:
            if not self.gui_event_conn:
                return
            try:
                msg = json.dumps(event_dict) + "\n"
                self.gui_event_conn.send(msg.encode('utf-8'))
            except Exception as e:
                self.logger.write(f"Event push failed: {e}", "critical")
                try:
                    self.gui_event_conn.close()
                except Exception:
                    pass
                self.gui_event_conn = None

    def _log_reader_thread(self):
        if not self.vnt_process or not self.vnt_process.stdout:
            return
        while self.running and self.vnt_process.poll() is None:
            line = self.vnt_process.stdout.readline()  # ← 直接是 str，不是 bytes
            if line:
                line = line.rstrip()
                self.logger.write(f"{line}", "info")
                self._parse_and_push_event(line)
            else:
                time.sleep(0.1)

    def _try_initial_auto_connect(self):
        """已废弃：此方法不再使用，自动连接逻辑已整合到 monitor_vnt_cli() 中

        原来的实现有问题：即使启动失败也会设置标志位，导致后续不再重试。
        新的实现直接在监控循环中持续检查条件并尝试启动，不限制尝试次数。
        """
        pass

    def monitor_vnt_cli(self):
        self.logger.write("Monitoring vnt-cli process started...", "info")

        # ⭐ Session 0 模式：服务启动后自动启用自动启动模式
        # 这样即使没有GUI，也会自动尝试连接
        if not self.gui_started_cli:
            self.auto_start_mode = True
            self.logger.write("Auto-start mode enabled for Session 0", "info")

        while self.running:
            if self.vnt_process and self.vnt_process.poll() is None:
                # vnt2_cli.exe 正在运行
                if self.need_to_check_internet and not Internet_Connectivity_Monitor.is_server_connected():
                    self.logger.write("Internet disconnected. Stopping vnt-cli network...", "info")
                    self.stop_vnt_cli_network()
                    self.virtual_ip = None
                    self.need_to_check_internet = False
                time.sleep(2)
            else:
                # vnt2_cli.exe 未运行，检查是否应该自动启动

                # ⭐ 关键：如果被用户手动关闭（Toggle Off），则不自动重启
                if self.toggled_off:
                    self.logger.write("[MONITOR] VNT manually toggled off, skipping auto-start", "info")
                    self.virtual_ip = None
                    time.sleep(2)
                    continue

                # ⭐ Session 0 自动模式或 Session 1 GUI启动模式：检查条件并自动启动
                should_autostart = self.auto_start_mode or self.gui_started_cli

                if not should_autostart:
                    # 既不是自动模式，也不是GUI启动模式，等待
                    time.sleep(2)
                    continue

                # 检查基本条件：网络和服务器连接
                internet_ok = self.inet_monitor.is_connected()
                server_ok = Internet_Connectivity_Monitor.is_server_connected()

                if not (internet_ok and server_ok):
                    # 网络或服务器不可用，等待后重试（Session 0 持续重试）
                    if not internet_ok:
                        self.logger.write("Internet not connected, will retry...", "info")
                    elif not server_ok:
                        self.logger.write("Server not reachable, will retry...", "info")
                    time.sleep(5)  # 网络问题时等待5秒再重试
                    continue

                # 检查配置文件
                vnt_conf = VNT_Config(self.working_dir, VNT_HELPER_CONFIG_FILE, self.logger)
                config_path = vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)
                has_valid_config = config_path and Path(config_path).exists()

                if not has_valid_config:
                    self.logger.write("No valid config file found, will retry...", "info")
                    time.sleep(10)  # 配置文件缺失时等待更长时间
                    continue

                # ⭐ 所有条件满足，启动CLI
                self.logger.write("All conditions met, starting vnt-cli...", "info")
                self.logger.write(
                    f"Mode: auto={self.auto_start_mode}, gui_started={self.gui_started_cli}", "info")

                success = self.start_vnt_cli()
                if success:
                    self.logger.write("Successfully started vnt-cli", "info")
                    time.sleep(5)
                else:
                    self.logger.write("Failed to start vnt-cli, will retry in 10 seconds", "warning")
                    time.sleep(10)  # 启动失败后等待10秒再重试

    def handle_ipc_command(self, conn, addr):
        self.logger.write(f"===== IPC Command Handler Entered =====", "info")
        self.logger.write(f"Connection from: {addr}", "info")
        try:
            self.logger.write(f"Waiting to receive data...", "info")
            data = conn.recv(1024).decode('utf-8')
            self.logger.write(f"Received raw data: '{data}'", "info")
            if not data.strip():
                self.logger.write(f"Empty data received, returning", "info")
                return
            cmd = json.loads(data)
            self.logger.write(f"Parsed command: {cmd}", "info")
            self.logger.write(f"Received [{cmd.get('cmd')}] command via IPC.", "info")

            if cmd.get("cmd") == "subscribe_events":
                with self.event_push_lock:
                    # 记录 GUI 连接
                    with self.gui_connection_lock:
                        self.gui_connections.add(conn)
                        self.logger.write(f"[GUI TRACK] New GUI connection. Total: {len(self.gui_connections)}", "info")

                    if self.gui_event_conn:
                        try:
                            self.gui_event_conn.close()
                        except Exception:
                            pass
                    self.gui_event_conn = conn
                self.logger.write("GUI subscribed to events.", "info")
                return  # keep connection open

            elif cmd["cmd"] == "start":
                self.logger.write(f"Processing 'start' command", "info")
                self.logger.write(f"Current toggled_off state: {self.toggled_off}", "info")
                self.logger.write(f"Calling start_vnt_cli()...", "info")
                success = self.start_vnt_cli()
                self.logger.write(f"start_vnt_cli() returned: {success}", "info")
                if success:
                    # ⭐ Session 1 GUI模式：标记CLI由GUI启动
                    self.toggled_off = False
                    self.gui_started_cli = True
                    self.auto_start_mode = False  # GUI手动启动时，关闭自动模式
                    self.logger.write(f"Set gui_started_cli=True, auto_start_mode=False", "info")
                resp = {"status": "ok"} if success else {"status": "error", "msg": "start failed"}
                self.logger.write(f"Sending response: {resp}", "info")
                conn.send(json.dumps(resp).encode())
                self.logger.write(f"Response sent successfully", "info")

            elif cmd["cmd"] == "stop_network":
                self.logger.write(f"Processing 'stop_network' command", "info")
                self.logger.write(f"Current toggled_off state: {self.toggled_off}", "info")
                self.logger.write(f"Setting toggled_off=True", "info")
                self.toggled_off = True
                # ⭐ 用户手动停止，清除GUI启动标志
                self.gui_started_cli = False
                self.logger.write(f"Calling stop_vnt_cli_network()...", "info")
                success = self.stop_vnt_cli_network()
                self.logger.write(f"stop_vnt_cli_network() returned: {success}", "info")
                resp = {"status": "ok"} if success else {"status": "error", "msg": "stop failed"}
                self.logger.write(f"Sending response: {resp}", "info")
                conn.send(json.dumps(resp).encode())
                self.logger.write(f"Response sent successfully", "info")

            elif cmd["cmd"] == "restart":
                self.logger.write(f"Processing 'restart' command", "info")
                success = True
                running = self.vnt_process is not None and self.vnt_process.poll() is None
                self.logger.write(f"Current vnt_process running state: {running}", "info")
                if running:
                    self.logger.write(f"Calling stop_vnt_cli_network()...", "info")
                    success = self.stop_vnt_cli_network()
                    self.logger.write(f"stop_vnt_cli_network() returned: {success}", "info")

                self.logger.write(f"Waiting 1 second before restart...", "info")
                time.sleep(1)

                self.logger.write(f"Calling start_vnt_cli()...", "info")
                success = success and self.start_vnt_cli()
                self.logger.write(f"start_vnt_cli() returned: {success}", "info")
                if success:
                    # ⭐ 重启后保持原有模式
                    self.toggled_off = False
                    if not self.auto_start_mode:
                        self.gui_started_cli = True
                    self.logger.write(
                        f"Restart completed, gui_started_cli={
                            self.gui_started_cli}, auto_start_mode={
                            self.auto_start_mode}", "info")

                resp = {"status": "ok"} if success else {"status": "error", "msg": "restart failed"}
                self.logger.write(f"Sending response: {resp}", "info")
                conn.send(json.dumps(resp).encode())
                self.logger.write(f"Response sent successfully", "info")

            elif cmd["cmd"] == "status":
                self.logger.write(f"Processing 'status' command", "info")
                running = "yes" if self.vnt_process is not None and self.vnt_process.poll() is None else "no"
                status_data = {
                    "status": "ok",
                    "running": running,
                    "virtual_ip": self.virtual_ip,
                    "version": self.ver,
                    "serial": self.serial,
                    "server_version": self.server_version}
                self.logger.write(f"Status data: {status_data}", "info")
                conn.send(json.dumps(status_data).encode())
                self.logger.write(f"Status response sent", "info")

            elif cmd["cmd"] == "exit":
                self.logger.write(f"Processing 'exit' command", "info")
                # ⭐ 关键逻辑：根据启动模式决定是否停止CLI
                # Session 1 GUI模式（gui_started_cli=True）：退出时停止CLI
                # Session 0 服务模式（auto_start_mode=True）：退出时不停止CLI

                if self.gui_started_cli and not self.toggled_off:
                    # Session 1: GUI启动的CLI，退出时应该停止
                    self.logger.write(f"GUI exit in Session 1 mode - stopping vnt2_cli.exe", "info")
                    success = self.stop_vnt_cli_network()
                    self.logger.write(f"stop_vnt_cli_network() returned: {success}", "info")

                    if self.vnt_process:
                        self.logger.write(f"Waiting for vnt_process to terminate (timeout=5s)...", "info")
                        try:
                            self.vnt_process.wait(timeout=5)
                            self.logger.write(f"vnt_process terminated successfully", "info")
                        except Exception as e:
                            self.logger.write(f"vnt_process wait timeout or error: {e}", "warning")

                    # 重置状态
                    self.gui_started_cli = False
                    self.toggled_off = True
                else:
                    # Session 0 或已手动停止：只断开连接，不停止CLI
                    self.logger.write(
                        f"GUI exit in Session 0 mode or already stopped - keeping daemon running", "info")
                    success = True

                self.logger.write(f"Sending exit confirmation (daemon stays running)", "info")
                conn.send(json.dumps({"status": "ok", "msg": "vnt2_cli.exe handled, daemon remains running"}).encode())
                self.logger.write(f"Exit confirmation sent", "info")

            elif cmd["cmd"] == "shutdown_daemon":
                # ⭐ 新增命令：完全关闭守护进程（用于重置操作）
                self.logger.write(
                    f"Processing 'shutdown_daemon' command - full daemon shutdown requested", "info")

                # 1. 先停止CLI
                if self.vnt_process and self.vnt_process.poll() is None:
                    self.logger.write(f"Stopping vnt2_cli.exe before daemon shutdown...", "info")
                    self.stop_vnt_cli_network()
                    try:
                        self.vnt_process.wait(timeout=5)
                        self.logger.write(f"vnt2_cli.exe stopped", "info")
                    except Exception as e:
                        self.logger.write(f"Error waiting for CLI: {e}", "warning")

                # 2. 发送确认响应
                self.logger.write(f"Sending shutdown confirmation", "info")
                conn.send(json.dumps({"status": "ok", "msg": "daemon shutting down"}).encode())

                # 3. 设置退出标志，守护进程将在主循环中退出
                self.logger.write(f"Setting running=False to trigger daemon shutdown", "info")
                self.running = False

                # 4. 关闭IPC连接
                try:
                    conn.close()
                except BaseException:
                    pass
            else:
                conn.send(json.dumps({"status": "error", "msg": "unknown command"}).encode())

        except json.JSONDecodeError:
            self.logger.write(f"JSON decode error, closing connection", "warning")
            # 清理 GUI 连接跟踪
            with self.gui_connection_lock:
                if conn in self.gui_connections:
                    self.gui_connections.discard(conn)
                    self.logger.write(
                        f"[GUI TRACK] Connection removed (JSON error). Remaining: {len(self.gui_connections)}", "info")
            conn.close()
        except Exception as e:
            self.logger.write(f"IPC exception caught: {type(e).__name__}: {e}", "critical")
            import traceback
            self.logger.write(f"Traceback:\n{traceback.format_exc()}", "critical")
            if cmd.get("cmd") != "subscribe_events":
                # 清理 GUI 连接跟踪
                with self.gui_connection_lock:
                    if conn in self.gui_connections:
                        self.gui_connections.discard(conn)
                        self.logger.write(
                            f"[GUI TRACK] Connection removed (exception). Remaining: {len(self.gui_connections)}", "info")
                self.logger.write(f"Closing connection due to error", "info")
                conn.close()
        finally:
            # 清理 GUI 连接跟踪（确保连接关闭时被移除）
            with self.gui_connection_lock:
                if conn in self.gui_connections:
                    self.gui_connections.discard(conn)
                    remaining = len(self.gui_connections)
                    self.logger.write(
                        f"[GUI TRACK] Connection handler exited. Remaining GUI connections: {remaining}", "info")

                    # ⭐ 关键逻辑：只有当所有GUI都断开，且CLI是由GUI启动的，才停止CLI
                    # 这确保了：
                    # - Session 0 模式（auto_start_mode=True）：GUI断开不影响CLI运行
                    # - Session 1 模式（gui_started_cli=True）：最后一个GUI断开时停止CLI

                    if (remaining == 0 and
                        self.vnt_process and
                        self.vnt_process.poll() is None and
                        self.gui_started_cli and
                            not self.toggled_off):

                        self.logger.write(
                            "[GUI TRACK] All GUI clients disconnected AND CLI was started by GUI.", "info")
                        self.logger.write("[GUI TRACK] Stopping vnt2_cli.exe as per Session 1 behavior...", "info")
                        self.stop_vnt_cli_network()
                        self.toggled_off = True
                        self.gui_started_cli = False

                    elif remaining == 0:
                        # 其他情况：只记录日志，不改变CLI状态
                        if self.auto_start_mode:
                            self.logger.write(
                                "[GUI TRACK] All GUI disconnected in Session 0 mode - keeping CLI running", "info")
                        elif self.toggled_off:
                            self.logger.write(
                                "[GUI TRACK] All GUI disconnected but CLI already stopped by user", "info")
                        else:
                            self.logger.write("[GUI TRACK] All GUI disconnected, CLI state unchanged", "info")

            self.logger.write(f"===== IPC Command Handler Exited =====", "info")

    def ipc_server_loop(self):
        max_retries = 5
        retry_delay = 1  # 秒

        for attempt in range(max_retries):
            try:
                self.ipc_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.ipc_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.logger.write(
                    f"Attempting to bind IPC server to {IPC_HOST}:{
                        self.IPC_PORT} (attempt {
                        attempt + 1}/{max_retries})", "info")
                self.ipc_server.bind((IPC_HOST, self.IPC_PORT))
                self.ipc_server.listen(5)
                self.logger.write(f"IPC server listening on {IPC_HOST}:{self.IPC_PORT}", "info")

                # 设置一个标志，表示IPC服务器已就绪
                self.ipc_ready = True
                self.logger.write(f"IPC server is ready", "info")
                break  # 成功绑定，退出重试循环

            except OSError as e:
                if e.errno == 10048:  # WSAEADDRINUSE - 端口已被占用
                    self.logger.write(
                        f"Port {
                            self.IPC_PORT} is already in use (attempt {
                            attempt + 1}/{max_retries})",
                        "warning")
                    if attempt < max_retries - 1:
                        self.logger.write(f"Waiting {retry_delay} seconds before retry...", "info")
                        time.sleep(retry_delay)
                        # 关闭失败的socket
                        try:
                            self.ipc_server.close()
                        except BaseException:
                            pass
                    else:
                        self.logger.write(f"Failed to bind port after {max_retries} attempts", "critical")
                        raise RuntimeError(f"Cannot bind to port {self.IPC_PORT}: Port already in use")
                else:
                    self.logger.write(f"Socket error during bind: {e}", "critical")
                    raise
            except Exception as e:
                self.logger.write(
                    f"Unexpected error during IPC server setup: {
                        type(e).__name__}: {e}", "critical")
                import traceback
                self.logger.write(f"Traceback:\n{traceback.format_exc()}", "critical")
                raise

        while self.running:
            try:
                conn, addr = self.ipc_server.accept()
                threading.Thread(target=self.handle_ipc_command, args=(conn, addr)).start()
            except Exception as e:
                if self.running:
                    self.logger.write(f"IPC accept error: {e}", "critical")

    def run(self):
        def _get_process_list(process_name):
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

        def kill_process(proc1, pid_to_exclude=None):
            Has_Process, num, pid = _get_process_list(proc1)

            self.logger.write(f"Killing Process {proc1}: {num} process(es) found")

            if Has_Process:
                for p in pid:
                    if p != pid_to_exclude:
                        try:
                            # First try graceful termination
                            proc = psutil.Process(p)
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)  # Wait up to 5 seconds for graceful termination
                            except psutil.TimeoutExpired:
                                # Force kill if graceful termination fails
                                self.logger.write(f"Process PID {p} did not terminate gracefully, forcing kill", "info")
                                proc.kill()
                                proc.wait()  # Wait for the kill to complete
                        except Exception as e:
                            self.logger.write(f"Killing Process Pid: {p} {e}", 'critical')
                            return False

                return True
            return True

        kill_process(VNT_CLI)

        self.logger.write("VNT Daemon starting...", "info")

        # 初始化IPC就绪标志
        self.ipc_ready = False

        # 启动IPC服务器线程
        ipc_thread = threading.Thread(target=self.ipc_server_loop, daemon=True)
        ipc_thread.start()

        # 等待IPC服务器就绪（最多等待2秒）
        wait_count = 0
        while not self.ipc_ready and wait_count < 20:
            time.sleep(0.1)
            wait_count += 1

        if self.ipc_ready:
            self.logger.write("IPC server is ready", "info")
        else:
            self.logger.write("Warning: IPC server may not be fully ready", "warning")

        # 启动监控线程
        threading.Thread(target=self.monitor_vnt_cli, daemon=True).start()

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.cleanup()

    def cleanup(self):
        self.logger.write("Shutting down daemon...", "debug")
        if self.inet_monitor is not None:
            self.inet_monitor.stop()
        if self.ipc_server:
            self.ipc_server.close()
        if self.vnt_process:
            # Ensure the vnt-cli process is properly terminated
            if self.vnt_process.poll() is None:  # Process is still running
                try:
                    self.vnt_process.terminate()
                    try:
                        # Wait for the process to terminate gracefully (up to 5 seconds)
                        self.vnt_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # If it doesn't terminate gracefully, force kill it
                        self.logger.write("vnt-cli did not terminate gracefully during cleanup, forcing kill", "info")
                        self.vnt_process.kill()
                        self.vnt_process.wait()  # Wait for the kill to complete
                except Exception as e:
                    self.logger.write(f"Error during vnt-cli process cleanup: {e}", "critical")
        self.logger.write("Daemon shut down", "info")


class VNT_Logger():
    def __init__(self, workingdir, fn, no_logger=False):
        self.log_fn = fn
        self.workingdir = workingdir
        self._pid_state = {}
        self._lock = threading.Lock()  # ← 新增锁

        if not no_logger:
            vnt_log_full_path_fn = os.path.join(workingdir, fn)
            # 使用固定名称避免冲突
            logger = logging.getLogger("VNT.Daemon.Logger")
            logger.setLevel(logging.DEBUG)
            logger.propagate = False  # 防止传递给 root logger

            if not logger.handlers:
                handler = logging.handlers.RotatingFileHandler(
                    vnt_log_full_path_fn, maxBytes=1024 * 1024, backupCount=3
                )
                formatter = logging.Formatter('%(asctime)s - %(levelname)-8s - %(message)s')
                handler.setFormatter(formatter)
                logger.addHandler(handler)

            self._logger = logger
        else:
            self._logger = None

    def write(self, txt, mode='info'):
        # 过滤掉打洞相关的冗余日志和警告信息
        filter_keywords = [
            "WARN",                    # 所有警告信息
            "PunchReq",                # 打洞请求
            "PunchRes",                # 打洞响应
            "punching",                # 正在打洞
            "PunchInfo",               # 打洞信息
            "对方回复开始打洞",         # 对方回复打洞
            "对方主动发起打洞",         # 对方主动打洞
            "打洞成功",                # 打洞成功
            "stun tcp read error",     # STUN TCP 读取错误
            "tcp connect timeout",     # TCP 连接超时
            "stun ",                   # STUN 探测日志（注意末尾有空格）
            "nat_type:",               # NAT 类型检测
            "tunnel TCP-Some",         # 隧道连接建立
            "drop tunnel",             # 隧道连接断开
            "INFO tcp_public_addr",    # 公网地址日志
            "INFO try_main_send_to_addr",
            "INFO tunnel UDP-None",
            "INFO Relay probe task completed",
            "INFO local_ipv4s",
        ]

        # 检查是否包含任何需要过滤的关键词
        for keyword in filter_keywords:
            if keyword in txt:
                return  # 直接跳过，不写入日志

        current_pid = os.getpid()
        full_txt = f"PID {current_pid:<6} : {txt}"
        current_time = datetime.now()

        with self._lock:  # ← 整个逻辑加锁
            state = self._pid_state.setdefault(current_pid, {
                'last_msg': None,
                'last_time': None,
                'last_connect_count': -1
            })

            # 判断是否超时（>10分钟）
            time_gap_too_long = (
                state['last_time'] is not None and
                (current_time - state['last_time'] > timedelta(minutes=10))
            )

            should_log = True

            if not time_gap_too_long:
                if state['last_msg'] == full_txt:
                    should_log = False
                elif "connect count=" in txt:
                    match = re.search(r'connect count=(\d+)', txt)
                    if match:
                        count = int(match.group(1))
                        if count % 50 != 0:
                            should_log = False
                        else:
                            if count == state['last_connect_count']:
                                should_log = False
                            else:
                                state['last_connect_count'] = count

            if should_log:
                # 处理 Rust/vnt2_cli 日志格式：提取 INFO/WARN/ERROR 后的实际消息
                processed_txt = full_txt
                # 匹配 ISO 8601 时间戳 + 日志级别格式（不要求行首，因为前面有 PID 前缀）
                rust_log_pattern = re.compile(
                    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+[+-]\d{2}:\d{2}\s+(INFO|WARN|ERROR|DEBUG|TRACE)\s+'
                )
                match = rust_log_pattern.search(full_txt)
                if match:
                    # 找到日志级别，提取其后的内容
                    log_level_end = match.end()
                    actual_message = full_txt[log_level_end:].strip()
                    # 保留 PID 前缀，替换为简洁格式
                    pid_prefix = f"PID {current_pid:<6} : "
                    processed_txt = pid_prefix + actual_message

                # 注意：print 是同步的，可以放锁内
                print(processed_txt)
                if self._logger is not None:
                    if mode.lower() == "debug":
                        self._logger.debug(processed_txt)
                    elif mode.lower() == "critical":
                        self._logger.critical(processed_txt)
                    else:
                        self._logger.info(processed_txt)

                # 更新状态
                state['last_msg'] = processed_txt
                state['last_time'] = current_time

    def get_log_fn(self):
        return self.log_fn


class VNT_Config():
    KEY_VNT_CONNECTION_CONFIG_YAML = 'config_name'
    KEY_VNT_NOTIFICATION_ENABLED = 'notification_enabled'
    KEY_CHECKSUM = "checksum"
    KEY_EXCLUDE = "exclude"
    KEY_UPDATE_ENABLED = "update_enabled"
    KEY_UPDATE_URL = "update_file_url"
    KEY_VERSION = "version"
    KEY_VERSION_FILE_URL = "update_version_url"
    KEY_UPDATE_CYCLE_SEC = "update_cycle_sec"
    KEY_TOTAL_NUMBER_OF_PID = "total_number_of_pid"
    KEY_DISPLAY_LANGUAGE = "display_language"
    KEY_AUTORUN_CLI_ON_STARTUP = "autorun_cli_on_startup"
    KEY_IPC_PORT = "ipc_port"

    def __init__(self, workingdir, fn, logger):
        self.working_dir = workingdir
        self.config_fn = fn
        self.logger = logger

    def get_value(self, key):
        fn = os.path.join(self.working_dir, self.config_fn)
        try:
            with open(fn, 'r', encoding='utf-8') as file:
                data = yaml.safe_load(file)
            return data[key]
        except Exception as e:
            print(f"Error in yaml get_value {e}, return None value")
            return None

    def get_data(self):
        fn = os.path.join(self.working_dir, self.config_fn)
        try:
            with open(fn, 'r', encoding='utf-8') as file:
                data = yaml.safe_load(file)
            return data
        except Exception as e:
            print(f"Error in yaml get_data {e}")
            return None

    def set_value(self, key_name, key_value):
        fn_config = os.path.join(self.working_dir, self.config_fn)
        if not os.path.exists(fn_config):
            try:
                with open(fn_config, 'w', encoding='utf-8') as file:
                    yaml.safe_dump({key_name: key_value}, file, allow_unicode=True, sort_keys=False)
                    return True
            except Exception as e:
                print(f"error in yaml set_value to new file: {e}")
            return False
        else:
            try:
                with open(fn_config, 'r', encoding='utf-8') as file:
                    data = yaml.safe_load(file)
                    data.update({key_name: key_value})
            except Exception as e:
                print(f"Error in yaml set_value read from exisiting file {e}")
                return False

            try:
                with open(fn_config, 'w', encoding='utf-8') as file:
                    yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
                    return True
            except Exception as e:
                print(f"Error yaml set_value write to existing file: {e}")
                return False

    def set_data(self, data):
        fn_config = os.path.join(self.working_dir, self.config_fn)
        if not os.path.exists(fn_config):
            try:
                with open(fn_config, 'w', encoding='utf-8') as file:
                    yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
                    file.flush()
                    os.fsync(file.fileno())
                    return True
            except Exception as e:
                print(f"Error in yaml set_data write to new file: {e}")
            return False
        else:
            try:
                with open(fn_config, 'r', encoding='utf-8') as file:
                    exiting_data = yaml.safe_load(file)
                    exiting_data.update(data)
            except Exception as e:
                print(f"Error in yaml set_data read from existing file {e}")
                return False

            try:
                with open(fn_config, 'w', encoding='utf-8') as file:
                    yaml.safe_dump(exiting_data, file, allow_unicode=True, sort_keys=False)
                    file.flush()
                    os.fsync(file.fileno())
                    return True
            except Exception as e:
                print(f"Error in yaml set_data write to existing file: {e}", 'critical')
                return False


class Internet_Connectivity_Monitor():

    _CLSID_NetworkListManager = "{DCB00C01-570F-4A9B-8D69-199FDBA5723B}"
    EXTERNAL_SITE_FOR_INTERNET_CHECK = "114.114.114.114", 53

    def __init__(self, logger):
        self.connected = False
        self.running = False
        self.thread = None
        self.logger = logger

    def __del__(self):
        pass

    def _monitor(self):
        pythoncom.CoInitialize()
        try:
            # nlm = Dispatch("NLM.NetworkListManager")
            nlm = Dispatch(self._CLSID_NetworkListManager)
            self.connected = nlm.IsConnectedToInternet

            while self.running:
                new_status = nlm.IsConnectedToInternet
                if new_status != self.connected:
                    self.connected = new_status
                    self.logger.write(f"Internet is {'Connected' if self.connected else 'Disconnected'}")
                pythoncom.PumpWaitingMessages()
                time.sleep(0.05)
        except Exception as e:
            self.logger.write(f"Internet Monitor: {e}", 'critical')
        finally:
            pythoncom.CoUninitialize()

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._monitor)
            self.thread.daemon = True
            self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)

    def is_connected(self):
        return self.connected

    @staticmethod
    def is_server_connected(s1=EXTERNAL_SITE_FOR_INTERNET_CHECK):

        if s1 is not None:
            try:
                conn = socket.create_connection(s1, 5)
                conn.close()
                return True
            except OSError:
                print(f"cannot connect {s1}")
                return False
        else:
            return False

    @staticmethod
    def is_valid_IP(ipAddr):
        if ipAddr is None:
            return False

        if '.' not in ipAddr:
            return False
        elif ipAddr.count('.') != 3:
            return False
        else:
            flag = True
            addr_list = ipAddr.split('.')

            for one in addr_list:
                try:
                    one_num = int(one)
                    if one_num >= 0 and one_num <= 255:
                        pass
                    else:
                        flag = False
                except Exception:
                    flag = False
            return flag

    @staticmethod
    def is_valid_domain_port(domain_port_txt):
        # 正则表达式匹配 domain_name:port
        # domain_name 可以是域名或 IP 地址
        pattern = re.compile(
            r'^'  # 开始
            r'('  # 开始捕获 domain_name
            r'(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}'  # 域名部分，例如 example.com
            r'|'  # 或者
            r'(?:\d{1,3}\.){3}\d{1,3}'  # IP 地址部分，例如 192.168.1.1
            r')'  # 结束捕获 domain_name
            r':'  # 冒号分隔
            r'(\d+)'  # port 部分，必须是数字
            r'$'  # 结束
        )

        match = pattern.match(domain_port_txt)
        if match:
            domain_name, port = match.groups()
            # 检查 port 是否在合法范围内 (1-65535)
            if 1 <= int(port) <= 65535 and port.isdigit():
                return True
        return False


if __name__ == "__main__":
    daemon = VNTDaemon()
    daemon.run()
    sys.exit(0)
