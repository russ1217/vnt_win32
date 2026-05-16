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
import win32process
import win32ts
import pythoncom
import signal
import psutil
from pathlib import Path
from datetime import datetime, timedelta
from win32com.client import Dispatch

VNT_CLI = "vnt-cli.exe"
VNT_LOG_FILE = "vnt_cli.log"
VNT_HELPER_CONFIG_FILE = "vnt_helper.yaml"
IPC_HOST = "127.0.0.1"
IPC_PORT = 58432
LOG_LEVEL = logging.DEBUG


class VNTDaemon():
    # ===== 关键事件匹配规则 =====
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

    RUNNING_SESSION_0 = 'running_session_0'

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

        self.running_session_id = win32ts.ProcessIdToSessionId(win32process.GetCurrentProcessId())
        self.logger.write(f"Daemon running in session {self.running_session_id}", "info")

        # Update helper config about session 0 running status
        vnt_conf = VNT_Config(self.working_dir, VNT_HELPER_CONFIG_FILE, self.logger)
        if self.running_session_id == 0:
            vnt_conf.set_value(self.RUNNING_SESSION_0, True)
        else:
            vnt_conf.set_value(self.RUNNING_SESSION_0, False)

        self.inet_monitor = Internet_Connectivity_Monitor(self.logger)
        self.inet_monitor.start()
        self.need_to_check_internet = False

    def _get_working_dir(self):

        if getattr(sys, 'frozen', False):
            curpath = os.path.dirname(sys.executable)   # EXE path, in case autorun with REGISTRY, it becomes WINDOWS\SYSTEM32
        elif __file__:
            curpath = os.path.dirname(os.path.abspath(__file__))

        return curpath

    def start_vnt_cli(self):
        vnt_conf = VNT_Config(self.working_dir, VNT_HELPER_CONFIG_FILE, self.logger)
        self.config_path = vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)

        i = 0

        while not self.config_path or not Path(self.config_path).exists():
            if i % 600 == 0:
                self.logger.write(f"Waiting for valid config file: {self.config_path}", "info")
            i += 1
            if not self.running:
                return False
            time.sleep(1)

        while not self.inet_monitor.is_connected() or not Internet_Connectivity_Monitor.is_server_connected():  # Wait for Internet Connection ...
            if not self.running:
                return False
            time.sleep(1)

        if self.vnt_process and self.vnt_process.poll() is None:
            self.logger.write("vnt-cli already running", "info")
            return True

        cmd = [str(self.vnt_cli_path), "-f", str(self.config_path)]
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
            threading.Thread(target=self._log_reader_thread, daemon=True).start()
            self.logger.write(f"Started vnt-cli with config: {self.config_path}", "info")
            return True
        except Exception as e:
            self.logger.write(f"Failed to start vnt-cli: {e}", "critical")
            return False

    def stop_vnt_cli_network(self):
        """仅关闭网络连接（示例：发送信号或模拟输入）"""
        if self.vnt_process and self.vnt_process.poll() is None:
            try:
                self.vnt_process.terminate()  # 或根据 vnt-cli 实际支持方式调整
                self.logger.write("Sent terminate signal to vnt-cli (network stop)", "info")
                return True
            except Exception as e:
                self.logger.write(f"Could not stop network gracefully: {e}", "critical")
                return False

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
                    self.ver = line.split("version")[-1].strip() if "version" in line else "unknown"
                    payload["version"] = self.ver
                elif event_type == "serial_info":
                    self.serial = line.split(":")[-1].strip() if "Serial:" in line else "unknown"
                    payload["serial"] = self.serial
                elif event_type == "server_connection":
                    self.server_version = line.split("server version=")[-1].strip() if "server version=" in line else "unknown"
                    payload["server_version"] = self.server_version
                elif event_type == "reconnect_count":
                    count_match = re.search(r'connect count=(\d+)', line)
                    if count_match:
                        payload["connect_count"] = count_match.group(1)
                        if int(count_match.group(1)) > 3:
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
        if self.running_session_id != 0:
            while not self.vnt_cli_switched_on:
                time.sleep(1)
        self.logger.write("Monitoring vnt-cli process started...", "info")
        while self.running:
            if self.vnt_process and self.vnt_process.poll() is None:
                if self.need_to_check_internet and not Internet_Connectivity_Monitor.is_server_connected():
                    self.logger.write("Internet disconnected. Stopping vnt-cli network...", "info")
                    self.stop_vnt_cli_network()
                    self.need_to_check_internet = False
                time.sleep(2)
            else:
                if self.inet_monitor.is_connected() and Internet_Connectivity_Monitor.is_server_connected():
                    if not self.toggled_off:
                        self.logger.write("vnt-cli not found. Try restarting...", "info")
                        self.need_to_check_internet = False
                        self.start_vnt_cli()
                    else:
                        time.sleep(1)
                else:
                    time.sleep(1)

    def handle_ipc_command(self, conn, addr):
        try:
            data = conn.recv(1024).decode('utf-8')
            if not data.strip():
                return
            cmd = json.loads(data)
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
                success = self.start_vnt_cli()
                if success:
                    self.toggled_off = False
                    self.vnt_cli_switched_on = True
                resp = {"status": "ok"} if success else {"status": "error", "msg": "start failed"}
                conn.send(json.dumps(resp).encode())

            elif cmd["cmd"] == "stop_network":

                self.toggled_off = True
                self.logger.write("Received [stop_network] command via IPC.", "info")
                success = self.stop_vnt_cli_network()
                resp = {"status": "ok"} if success else {"status": "error", "msg": "stop failed"}
                conn.send(json.dumps(resp).encode())

            elif cmd["cmd"] == "restart":

                success = True
                running = self.vnt_process is not None and self.vnt_process.poll() is None
                if running:
                    success = self.stop_vnt_cli_network()

                time.sleep(1)

                success = success and self.start_vnt_cli()
                if success:
                    self.toggled_off = False
                resp = {"status": "ok"} if success else {"status": "error", "msg": "restart failed"}
                conn.send(json.dumps(resp).encode())

            elif cmd["cmd"] == "status":

                running = "yes" if self.vnt_process is not None and self.vnt_process.poll() is None else "no"
                conn.send(json.dumps({"status": "ok", "running": running, "virtual_ip": self.virtual_ip, "version": self.ver, "serial": self.serial, "server_version": self.server_version}).encode())

            elif cmd["cmd"] == "exit":

                self.running = False
                self.stop_vnt_cli_network()
                if self.vnt_process:
                    self.vnt_process.wait(timeout=5)
                conn.send(json.dumps({"status": "daemon exits"}).encode())
            else:
                conn.send(json.dumps({"status": "error", "msg": "unknown command"}).encode())

        except json.JSONDecodeError:
            conn.close()
        except Exception as e:
            self.logger.write(f"IPC error: {e}", "critical")
            if cmd.get("cmd") != "subscribe_events":
                conn.close()

    def ipc_server_loop(self):
        self.ipc_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ipc_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ipc_server.bind((IPC_HOST, IPC_PORT))
        self.ipc_server.listen(5)
        self.logger.write(f"IPC server listening on {IPC_HOST}:{IPC_PORT}", "info")

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
                            os.kill(p, signal.SIGINT)
                        except Exception as e:
                            self.logger.write(f"Killing Process Pid: {p} {e}", 'critical')
                            return False

                return True
            return True

        kill_process(VNT_CLI)

        self.logger.write("VNT Daemon starting...", "info")
        threading.Thread(target=self.ipc_server_loop, daemon=True).start()
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
            self.vnt_process.terminate()
            try:
                self.vnt_process.wait(timeout=5)
            except Exception:
                self.vnt_process.kill()
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
                print(f"Error in yaml set_data write to existing file {e}", 'critical')
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
