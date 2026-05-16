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
        self.toggled_off = False
        self.logger = VNT_Logger(self.working_dir, VNT_LOG_FILE)
        self.ipc_server = None
        self.gui_event_conn = None
        self.event_push_lock = threading.Lock()
        self.virtual_ip = None
        self.ver = '0.0.0'
        self.serial = 'unknown'
        self.server_version = '0.0.0'

        self.inet_monitor = Internet_Connectivity_Monitor(self.logger)
        self.inet_monitor.start()
        self.need_to_check_internet = False
        self.gui_request_vnt_connection = False

        # Read IPC port from config and validate it
        try:
            vnt_conf = VNT_Config(self.working_dir, VNT_HELPER_CONFIG_FILE, self.logger)
            ipc_port_reading = vnt_conf.get_value(VNT_Config.KEY_IPC_PORT)

            # Validate that the IPC port is a reasonable value
            if ipc_port_reading is not None:
                try:
                    port_num = int(ipc_port_reading)
                    if not (1 <= port_num <= 65535):
                        self.logger.write(f"Invalid IPC port value from config: {port_num} (must be between 1-65535), using default", 'critical')
                        self.IPC_PORT = DEFAULT_IPC_PORT  # Use default port
                    else:
                        # Valid port number, use the configured value
                        self.IPC_PORT = port_num
                        self.logger.write(f"Using configured IPC port: {port_num}", 'info')
                except (ValueError, TypeError):
                    self.logger.write(f"Invalid IPC port value from config: {ipc_port_reading} (not a valid number), using default", 'critical')
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
            curpath = os.path.dirname(sys.executable)   # EXE path, in case autorun with REGISTRY, it becomes WINDOWS\SYSTEM32
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

        while not self.inet_monitor.is_connected() or not Internet_Connectivity_Monitor.is_server_connected():  # Wait for Internet Connection ...
            if not self.running:
                return False
            time.sleep(1)

        self.logger.write("Internet connection verified", "info")

        if self.vnt_process and self.vnt_process.poll() is None:
            self.logger.write("VNTDaemon: vnt-cli already running", "info")
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
            self.vnt_process = subprocess.Popen(
                cmd,
                cwd=self.working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=1,              # 行缓冲生效
                text=True,              # ← 关键：启用文本模式（自动 decode）
                encoding='utf-8',       # 显式指定编码（避免系统 locale 问题）
                errors='replace',       # 避免解码错误崩溃
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            self.logger.write(f"vnt2_cli.exe process started with PID: {self.vnt_process.pid}", "info")
            threading.Thread(target=self._log_reader_thread, daemon=True).start()
            self.logger.write(f"Started vnt2_cli.exe with TOML config: {toml_config_path}", "info")
            return True
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            self.logger.write(f"Failed to start vnt2_cli.exe: {e}", "critical")
            self.logger.write(f"Traceback: {error_trace}", "critical")
            return False

    def stop_vnt_cli_network(self):
        """仅关闭网络连接（VNT 2.0不支持--stop命令，直接终止进程）"""
        if self.vnt_process and self.vnt_process.poll() is None:
            try:
                self.logger.write(f"[DEBUG] Attempting to stop vnt-cli process (PID: {self.vnt_process.pid})", "info")
                
                # VNT 2.0不支持--stop命令，直接终止进程
                self.logger.write(f"[DEBUG] Terminating vnt-cli process...", "info")
                self.vnt_process.terminate()
                
                try:
                    # Wait for the process to terminate gracefully (up to 5 seconds)
                    self.logger.write(f"[DEBUG] Waiting for process to terminate (timeout=5s)...", "info")
                    self.vnt_process.wait(timeout=5)
                    self.logger.write(f"[DEBUG] Process terminated successfully", "info")
                except subprocess.TimeoutExpired:
                    # If it doesn't terminate gracefully, force kill it
                    self.logger.write("vnt-cli did not terminate gracefully, forcing kill", "info")
                    self.vnt_process.kill()
                    self.vnt_process.wait()  # Wait for the kill to complete
                    self.logger.write(f"[DEBUG] Process killed forcefully", "info")

                self.virtual_ip = None
                self.logger.write("Successfully stopped vnt-cli process", "info")
                return True

            except Exception as e:
                self.logger.write(f"Could not stop vnt-cli process: {type(e).__name__}: {e}", "critical")
                import traceback
                self.logger.write(f"[DEBUG] Traceback:\n{traceback.format_exc()}", "critical")
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
                        self.server_version = line.split("server version=")[-1].strip() if "server version=" in line else "unknown"
                    payload["server_version"] = self.server_version
                elif event_type == "reconnect_count":
                    count_match = re.search(r'connect count=(\d+)', line)
                    if count_match:
                        payload["connect_count"] = count_match.group(1)
                        count = int(count_match.group(1))
                        if count > self.MAX_RECONNECT_COUNT:
                            self.logger.write(f"Reconnection count {count} exceeds {self.MAX_RECONNECT_COUNT}, restarting vnt_cli.exe", "info")
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

    def monitor_vnt_cli(self):
        self.logger.write("Monitoring vnt-cli process started...", "info")
        while self.running:
            if self.vnt_process and self.vnt_process.poll() is None:
                if self.need_to_check_internet and not Internet_Connectivity_Monitor.is_server_connected():
                    self.logger.write("Internet disconnected. Stopping vnt-cli network...", "info")
                    self.stop_vnt_cli_network()
                    self.virtual_ip = None
                    self.need_to_check_internet = False
                time.sleep(2)
            else:
                if self.inet_monitor.is_connected() and Internet_Connectivity_Monitor.is_server_connected():
                    if not self.toggled_off:
                        vnt_conf = VNT_Config(self.working_dir, VNT_HELPER_CONFIG_FILE, self.logger)
                        if vnt_conf.get_value(VNT_Config.KEY_AUTORUN_CLI_ON_STARTUP) or self.gui_request_vnt_connection:
                            self.logger.write("vnt-cli not found. Try restarting...", "info")
                            self.need_to_check_internet = False
                            self.start_vnt_cli()
                    else:
                        self.virtual_ip = None
                        time.sleep(1)
                else:
                    time.sleep(1)

    def handle_ipc_command(self, conn, addr):
        self.logger.write(f"[DEBUG] ===== IPC Command Handler Entered =====", "info")
        self.logger.write(f"[DEBUG] Connection from: {addr}", "info")
        try:
            self.logger.write(f"[DEBUG] Waiting to receive data...", "info")
            data = conn.recv(1024).decode('utf-8')
            self.logger.write(f"[DEBUG] Received raw data: '{data}'", "info")
            if not data.strip():
                self.logger.write(f"[DEBUG] Empty data received, returning", "info")
                return
            cmd = json.loads(data)
            self.logger.write(f"[DEBUG] Parsed command: {cmd}", "info")
            self.logger.write(f"Received [{cmd.get('cmd')}] command via IPC.", "info")

            if cmd.get("cmd") == "subscribe_events":
                with self.event_push_lock:
                    if self.gui_event_conn:
                        try:
                            self.gui_event_conn.close()
                        except Exception:
                            pass
                    self.gui_event_conn = conn
                self.logger.write("GUI subscribed to events.", "info")
                return  # keep connection open

            elif cmd["cmd"] == "start":
                self.logger.write(f"[DEBUG] Processing 'start' command", "info")
                self.logger.write(f"[DEBUG] Current toggled_off state: {self.toggled_off}", "info")
                self.logger.write(f"[DEBUG] Calling start_vnt_cli()...", "info")
                success = self.start_vnt_cli()
                self.logger.write(f"[DEBUG] start_vnt_cli() returned: {success}", "info")
                if success:
                    self.toggled_off = False
                    self.gui_request_vnt_connection = True
                    self.logger.write(f"[DEBUG] Set toggled_off=False, gui_request_vnt_connection=True", "info")
                resp = {"status": "ok"} if success else {"status": "error", "msg": "start failed"}
                self.logger.write(f"[DEBUG] Sending response: {resp}", "info")
                conn.send(json.dumps(resp).encode())
                self.logger.write(f"[DEBUG] Response sent successfully", "info")

            elif cmd["cmd"] == "stop_network":
                self.logger.write(f"[DEBUG] Processing 'stop_network' command", "info")
                self.logger.write(f"[DEBUG] Current toggled_off state: {self.toggled_off}", "info")
                self.logger.write(f"[DEBUG] Setting toggled_off=True", "info")
                self.toggled_off = True
                self.logger.write(f"[DEBUG] Calling stop_vnt_cli_network()...", "info")
                success = self.stop_vnt_cli_network()
                self.logger.write(f"[DEBUG] stop_vnt_cli_network() returned: {success}", "info")
                resp = {"status": "ok"} if success else {"status": "error", "msg": "stop failed"}
                self.logger.write(f"[DEBUG] Sending response: {resp}", "info")
                conn.send(json.dumps(resp).encode())
                self.logger.write(f"[DEBUG] Response sent successfully", "info")

            elif cmd["cmd"] == "restart":
                self.logger.write(f"[DEBUG] Processing 'restart' command", "info")
                success = True
                running = self.vnt_process is not None and self.vnt_process.poll() is None
                self.logger.write(f"[DEBUG] Current vnt_process running state: {running}", "info")
                if running:
                    self.logger.write(f"[DEBUG] Calling stop_vnt_cli_network()...", "info")
                    success = self.stop_vnt_cli_network()
                    self.logger.write(f"[DEBUG] stop_vnt_cli_network() returned: {success}", "info")

                self.logger.write(f"[DEBUG] Waiting 1 second before restart...", "info")
                time.sleep(1)

                self.logger.write(f"[DEBUG] Calling start_vnt_cli()...", "info")
                success = success and self.start_vnt_cli()
                self.logger.write(f"[DEBUG] start_vnt_cli() returned: {success}", "info")
                if success:
                    self.toggled_off = False
                    self.gui_request_vnt_connection = True
                    self.logger.write(f"[DEBUG] Set toggled_off=False, gui_request_vnt_connection=True", "info")

                resp = {"status": "ok"} if success else {"status": "error", "msg": "restart failed"}
                self.logger.write(f"[DEBUG] Sending response: {resp}", "info")
                conn.send(json.dumps(resp).encode())
                self.logger.write(f"[DEBUG] Response sent successfully", "info")

            elif cmd["cmd"] == "status":
                self.logger.write(f"[DEBUG] Processing 'status' command", "info")
                running = "yes" if self.vnt_process is not None and self.vnt_process.poll() is None else "no"
                status_data = {"status": "ok", "running": running, "virtual_ip": self.virtual_ip, "version": self.ver, "serial": self.serial, "server_version": self.server_version}
                self.logger.write(f"[DEBUG] Status data: {status_data}", "info")
                conn.send(json.dumps(status_data).encode())
                self.logger.write(f"[DEBUG] Status response sent", "info")

            elif cmd["cmd"] == "exit":
                self.logger.write(f"[DEBUG] Processing 'exit' command", "info")
                self.logger.write(f"[DEBUG] Setting running=False", "info")
                self.running = False
                self.logger.write(f"[DEBUG] Calling stop_vnt_cli_network()...", "info")
                self.stop_vnt_cli_network()
                if self.vnt_process:
                    self.logger.write(f"[DEBUG] Waiting for vnt_process to terminate (timeout=5s)...", "info")
                    try:
                        self.vnt_process.wait(timeout=5)
                        self.logger.write(f"[DEBUG] vnt_process terminated successfully", "info")
                    except Exception as e:
                        self.logger.write(f"[DEBUG] vnt_process wait timeout or error: {e}", "warning")
                self.logger.write(f"[DEBUG] Sending exit confirmation", "info")
                conn.send(json.dumps({"status": "daemon exits"}).encode())
                self.logger.write(f"[DEBUG] Exit confirmation sent", "info")
            else:
                conn.send(json.dumps({"status": "error", "msg": "unknown command"}).encode())

        except json.JSONDecodeError:
            self.logger.write(f"[DEBUG] JSON decode error, closing connection", "warning")
            conn.close()
        except Exception as e:
            self.logger.write(f"[DEBUG] IPC exception caught: {type(e).__name__}: {e}", "critical")
            import traceback
            self.logger.write(f"[DEBUG] Traceback:\n{traceback.format_exc()}", "critical")
            if cmd.get("cmd") != "subscribe_events":
                self.logger.write(f"[DEBUG] Closing connection due to error", "info")
                conn.close()
        finally:
            self.logger.write(f"[DEBUG] ===== IPC Command Handler Exited =====", "info")

    def ipc_server_loop(self):
        max_retries = 5
        retry_delay = 1  # 秒
        
        for attempt in range(max_retries):
            try:
                self.ipc_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.ipc_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.logger.write(f"[DEBUG] Attempting to bind IPC server to {IPC_HOST}:{self.IPC_PORT} (attempt {attempt + 1}/{max_retries})", "info")
                self.ipc_server.bind((IPC_HOST, self.IPC_PORT))
                self.ipc_server.listen(5)
                self.logger.write(f"IPC server listening on {IPC_HOST}:{self.IPC_PORT}", "info")
                
                # 设置一个标志，表示IPC服务器已就绪
                self.ipc_ready = True
                self.logger.write(f"[DEBUG] IPC server is ready", "info")
                break  # 成功绑定，退出重试循环
                
            except OSError as e:
                if e.errno == 10048:  # WSAEADDRINUSE - 端口已被占用
                    self.logger.write(f"[DEBUG] Port {self.IPC_PORT} is already in use (attempt {attempt + 1}/{max_retries})", "warning")
                    if attempt < max_retries - 1:
                        self.logger.write(f"[DEBUG] Waiting {retry_delay} seconds before retry...", "info")
                        time.sleep(retry_delay)
                        # 关闭失败的socket
                        try:
                            self.ipc_server.close()
                        except:
                            pass
                    else:
                        self.logger.write(f"[DEBUG] Failed to bind port after {max_retries} attempts", "critical")
                        raise RuntimeError(f"Cannot bind to port {self.IPC_PORT}: Port already in use")
                else:
                    self.logger.write(f"[DEBUG] Socket error during bind: {e}", "critical")
                    raise
            except Exception as e:
                self.logger.write(f"[DEBUG] Unexpected error during IPC server setup: {type(e).__name__}: {e}", "critical")
                import traceback
                self.logger.write(f"[DEBUG] Traceback:\n{traceback.format_exc()}", "critical")
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
                    vnt_log_full_path_fn, maxBytes=1024*1024, backupCount=3
                )
                formatter = logging.Formatter('%(asctime)s - %(levelname)-8s - %(message)s')
                handler.setFormatter(formatter)
                logger.addHandler(handler)

            self._logger = logger
        else:
            self._logger = None

    def write(self, txt, mode='info'):
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
                # 注意：print 是同步的，可以放锁内
                print(full_txt)
                if self._logger is not None:
                    if mode.lower() == "debug":
                        self._logger.debug(full_txt)
                    elif mode.lower() == "critical":
                        self._logger.critical(full_txt)
                    else:
                        self._logger.info(full_txt)

                # 更新状态
                state['last_msg'] = full_txt
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
