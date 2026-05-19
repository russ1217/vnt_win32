# -*- coding: utf-8 -*-
import win32api
import win32con
import win32com
import win32com.client
import win32gui
import win32pipe
import win32file
import win32ts
import win32process
import win32service
from win32com.client import Dispatch
from win32comext.shell.shell import ShellExecuteEx
from winotify import Notification
import wx
import wx.adv
import wx.grid

import sys
import os
import re
import shutil
import time
import json
import subprocess
import ctypes
import psutil
import signal
import yaml
import threading
import queue
import hashlib
import logging
import logging.handlers
from argparse import ArgumentParser
from ctypes import wintypes
from datetime import datetime, timedelta
from typing import Set, Union, Dict, List
from pathlib import Path

import socket
import requests
import validators
import pythoncom
import webbrowser
import uuid
import gettext
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed


VNT_HELPER_VERSION = "v4_2026.05.19.15"
VNT_CLI_LOG_FILE = 'vnt_cli.log'
VNT_HELPER_CONFIG_FILE = "vnt_helper.yaml"
VNT_CLIENT_NAME = "vnt2_cli.exe"  # VNT 2.0 客户端
WIN_TUNE_DLL = "wintun.dll"
VNT_CONFIG_TEMPLATE_FILE = 'vnt_config_template.yaml'
VNT_CONFIG_ALL_FILE = 'vnt_config_all.yaml'
VNT_TRAY_ICON = 'vnt.png'
VNT_SERVICE_EXE = "vnt_service.exe"
VNT_HELPER_ICON = 'vnt_helper.ico'
VNT_CTRL_EXE = "vnt2_ctrl.exe"  # VNT 2.0 控制工具

RESOURCE_FILE_NAMES = [VNT_CLIENT_NAME, VNT_CTRL_EXE, VNT_SERVICE_EXE, WIN_TUNE_DLL, VNT_CONFIG_TEMPLATE_FILE, VNT_CONFIG_ALL_FILE, VNT_TRAY_ICON, VNT_HELPER_ICON]
DEFAULT_LANG = 'en'  # 或 'zh_CN' 默认语言（可从配置、系统或用户选择读取）

_ = None


class VNT_Helper_App():

    EXIT_SIGNAL = "VNT_HELPER_EXIT"
    EXIT_SIGNAL_ACK = "VNT_HELPER_EXIT_ACK"
    CLOSE_EXIT_SIGNAL_PROCESS = "SIGNAL_PROCESS_EXIT"
    CLOSE_EXIT_SIGNAL_ACK = "SIGNAL_PROCESS_ACK "
    PIPE_NAME = r'\\.\pipe\vnt_helper_pipe'
    PID_FILE = "vnt_pid.yaml"
    PID_FILE_BACKUP = "vnt_pid_backup.yaml"

    def __init__(self):
        global VNT_CLIENT_NAME, RESOURCE_FILE_NAMES, VNT_CLI_LOG_FILE, VNT_HELPER_CONFIG_FILE, VNT_HELPER_VERSION
        global _

        self.current_version = VNT_HELPER_VERSION
        self.vnt_cli_version = '0.0.0'
        self.vnt_server_version = '0.0.0'
        self.vnt_cli_serial = 'Unknown Serial'
        self.config_fn = VNT_HELPER_CONFIG_FILE

        self._run_as_admin()
        _ = self.setup_i18n('en')
        self.args = self._handle_args()
        self.workingdir = self._get_working_dir()
        self.logger = self._handle_logging_method(self.workingdir, VNT_CLI_LOG_FILE)
        self.running_session_id = win32ts.ProcessIdToSessionId(win32process.GetCurrentProcessId())

        if self.running_session_id == 0:
            # In session 0, but daemon should be running as service, not as process
            # Just exit since GUI shouldn't run in session 0
            sys.exit(0)

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)

        display_lang = vnt_conf.get_value(VNT_Config.KEY_DISPLAY_LANGUAGE)
        if display_lang in ['en', 'zh_CN']:
            self.logger.write(f"UI langugage {display_lang}", 'debug')
            DEFAULT_LANG = display_lang
        else:
            DEFAULT_LANG = 'en'

        _ = self.setup_i18n(DEFAULT_LANG)

        self.PIDs = []
        self._process_PID("set")

        if self.args.kill is True:
            time.sleep(5)

        self.logger.write("******************* Start New Session *******************")
        self.logger.write(f"Current VNT Helper Version is {self.current_version}")

        self._clear_existing_process(os.path.split(sys.argv[0])[1], self.args.no_gui)

        self._deploy_resource_files(RESOURCE_FILE_NAMES)
        self.reg_task_autorun = Registry_Taskschedule_for_AutoRun(self, self.workingdir, self.logger, self.args.no_gui, self.args.debug, self.args.kill)

        self.main_gui_app = None
        self.main_window = None
        self.vnt_connection = None
        self.bubble_msg_handler = None
        self.process_exit_signal_thread = None
        self.server_pipe = None
        self.client_pipe = None
        self.exit_status = False
        self.update_process_started = False

        self.inet_monitor = Internet_Connectivity_Monitor(self.logger)
        self.inet_monitor.start()

        self.logger.write(f"Temp Folder: {self._resource_path("")}")

    def __del__(self):
        pass

    def _clear_existing_process(self, exe_nm, background_run=False):

        def is_process_running(pid):
            try:
                process = psutil.Process(pid)
                return process.is_running()
            except psutil.NoSuchProcess:
                return False

        def notify_vnt_helper_to_exit(normal_exit_flag):
            nonlocal self
            for i in range(10):
                try:
                    self.client_pipe = win32file.CreateFile(
                        self.PIPE_NAME,
                        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                        0, None, win32file.OPEN_EXISTING, 0, None
                    )
                    break
                except Exception as e:
                    self.logger.write(f"Setting up Notifying Client# {i} {e}", 'critical')
                    time.sleep(1)

            if i >= 9:
                return

            try:
                win32file.WriteFile(self.client_pipe, self.EXIT_SIGNAL.encode('utf-8'))
                data = win32file.ReadFile(self.client_pipe, 65536)[1].decode('utf-8')
                print(f"Signal Received: {data}")
                if data == self.EXIT_SIGNAL_ACK:
                    normal_exit_flag.set()
            except Exception as e:
                self.logger.write(f"Pipe client {e}", 'critical')
            finally:
                win32file.CloseHandle(self.client_pipe)
                self.logger.write("Exit signal client thread ends...", 'info')
                return

        if exe_nm is not None:
            is_vnt_helper_on, num, pid = self._get_process_list(exe_nm)
            if is_vnt_helper_on and num > 2:  # pyinstaller makes two exe processes. one for tmp cleanning, one for main program

                can_clean_processes = True

                if not background_run and self.args.kill is False:
                    t = _("There are ") + str(num - 2) + _(" of ") + exe_nm + _(" session(s) already running\n") + _("Start New Sessions?")
                    can_clean_processes = (win32api.MessageBox(0, t, _("Status"),  win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL) == win32con.IDYES)

                if not can_clean_processes:
                    try:
                        shutil.copy(os.path.join(self.workingdir, self.PID_FILE_BACKUP), os.path.join(self.workingdir, self.PID_FILE))
                        os.remove(os.path.join(self.workingdir, self.PID_FILE_BACKUP))
                    except Exception as e:
                        self.logger(f"Error restor backgup pid file {e}")
                    finally:
                        sys.exit(0)
                else:
                    normal_exit_flag = threading.Event()
                    t = threading.Thread(target=notify_vnt_helper_to_exit, args=(normal_exit_flag,))
                    t.daemon = True
                    t.start()
                    t.join(timeout=10)

                    if normal_exit_flag.is_set():
                        self.logger.write(f"Currently total pid in running: {pid}")
                        self.logger.write(f"PIDs of this instance: {self.PIDs}")
                        for p in pid:
                            if p not in self.PIDs:
                                self.logger.write(f"Last PID not in Current PID Table= {p}")
                                start_time = time.time()
                                while is_process_running(p):
                                    if time.time() - start_time > 10:
                                        self.logger.write("Time out. Force killing process .......", 'info')
                                        self.kill_process(exe_nm, os.getpid())
                                        break
                                    time.sleep(0.1)
                        return
                    else:
                        self.logger.write("Signal Failed. Force killing process .......", 'info')
                        self.kill_process(exe_nm, os.getpid())

    def _close_exit_signal_thread(self):
        try:
            if self.client_pipe is None:
                try:
                    self.client_pipe = win32file.CreateFile(
                        self.PIPE_NAME,
                        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                        0, None, win32file.OPEN_EXISTING, 0, None
                    )
                except Exception as e:
                    print(f"Error Setup Notifying Client {e}")
                    return

            win32file.WriteFile(self.client_pipe, self.CLOSE_EXIT_SIGNAL_PROCESS.encode('utf-8'))
            data = win32file.ReadFile(self.client_pipe, 65536)[1].decode('utf-8')
            if data == self.CLOSE_EXIT_SIGNAL_ACK:
                self.logger.write("Close exit signal thread: ACK received ...")

        except Exception as e:
            print(f"Error in pipe client {e}")
        finally:
            return

    def _deploy_resource_files(self, file_names):

        res_path = self._resource_path("res")

        for fn in file_names:
            check_sum_target = None
            check_sum_source = None

            target_fn = os.path.join(self.workingdir, fn)
            if os.path.exists(target_fn):
                check_sum_target = VNT_Update_Window.calculate_SHA256(target_fn)

            from_fn = os.path.join(res_path, fn)
            if not os.path.exists(from_fn):
                self.logger.write(f"Resource file cannot be found at: {from_fn}")
            else:
                check_sum_source = VNT_Update_Window.calculate_SHA256(from_fn)
                if check_sum_source != check_sum_target and check_sum_source is not None:
                    try:
                        shutil.copy(from_fn, target_fn)
                        self.logger.write(f"Successfully copied {from_fn} to {target_fn}")
                    except PermissionError:
                        self.logger.write(f"Permission denied copying {target_fn}", 'critical')
                        continue
                    except Exception as e:
                        self.logger.write(f"Error copying resource: {e}", 'critical')
                        continue
                else:
                    self.logger.write(f"Reource file {fn} already exists")

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

    def _get_working_dir(self):

        if getattr(sys, 'frozen', False):
            curpath = os.path.dirname(sys.executable)   # EXE path, in case autorun with REGISTRY, it becomes WINDOWS\SYSTEM32
        elif __file__:
            curpath = os.path.dirname(os.path.abspath(__file__))

        return curpath

    def _handle_args(self):

        HELP_TXT = '''\nCommand Line Options:\n
        usage: vnt_helper.exe [-h] [-d] [-b] [-u] [-k] [-v]
        options:
        -h, --help                show this help message and exit
        -d, --debug            set DEBUG mode for logging information
        -b, --background  run in background
        -u, --update           update VNT helper and associate programs
        -k, --kill                   kill the VNT process
        -v, --version           get version information'''

        parser = ArgumentParser()
        parser.add_argument("-d", "--debug", help="set DEBUG mode, with console shown", action="store_true", dest="debug", default=False)
        parser.add_argument("-b", "--background", help="run in background", action="store_true", dest="no_gui", default=False)
        parser.add_argument("-u", "--update", help="update VNT helper and associate programs", action="store_true", dest="update", default=False)
        parser.add_argument("-k", "--kill", help="kill the VNT process", action="store_true", dest="kill", default=False)
        parser.add_argument("-v", "--version", help="get version information", action="store_true", dest="version", default=False)

        args = parser.parse_args()

        if args.version is True:
            print(self.current_version)
            if not args.no_gui:
                win32api.MessageBox(0, _("VNT Helper Version {version}").format(version=self.current_version) + '\n' + HELP_TXT,
                                    _("Information"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
            sys.exit(0)

        return args

    def _handle_logging_method(self, workingdir, fn):

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()

        if hwnd != 0:
            if not self.args.debug:
                ctypes.windll.user32.ShowWindow(hwnd, win32con.SW_HIDE)
                ctypes.windll.kernel32.CloseHandle(hwnd)
            else:
                print("\nVNT Helper Debug Console Started...\n")
            _logger = VNT_Logger(workingdir, fn, True, self.args.debug)
        else:
            try:
                _logger = VNT_Logger(workingdir, fn, False, self.args.debug)
            except Exception as e:
                win32api.MessageBox(0, f"{e}", "LOGGER ERROR", win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
                sys.exit(0)

        return _logger

    def _main_GUI_loop(self):

        def start_GUI(show_main_window=True, fresh_start_after_update=False):
            nonlocal self

            self.main_gui_app = wx.App()
            self.main_window = VNT_Main_Window(None, self, fresh_start_after_update)
            self.main_window.Show(show_main_window)
            self.main_gui_app.SetTopWindow(self.main_window)
            self.main_gui_app.MainLoop()

        if self.args.update:
            self._process_post_update_task()

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)

        try:
            vnt_config_file = vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)
            start_GUI(not (vnt_config_file is not None and os.path.exists(vnt_config_file)), self.args.update)  # Not showing configuration main window

        except Exception as e:
            self.logger.write(f"Main GUI loop {e}", 'critical')

    def _process_PID(self, cmd):
        if cmd.lower() == "set":
            vnt_pid = VNT_Config(self.workingdir, self.PID_FILE, self.logger)
            last_total_num_of_pid = vnt_pid.get_value(VNT_Config.KEY_TOTAL_NUMBER_OF_PID)
            is_vnt_helper_on, num, pid = self._get_process_list(os.path.split(sys.argv[0])[1])

            if not is_vnt_helper_on:
                return False

            if last_total_num_of_pid is None:
                vnt_pid.set_value(VNT_Config.KEY_TOTAL_NUMBER_OF_PID, num)
                for i in range(num):
                    self.PIDs.append(pid[i])
                    vnt_pid.set_value(str(i), pid[i])
            else:
                shutil.copy(os.path.join(self.workingdir, self.PID_FILE), os.path.join(self.workingdir, self.PID_FILE_BACKUP))
                last_PIDs = []
                for i in range(last_total_num_of_pid):
                    last_PIDs.append(vnt_pid.get_value(str(i)))

                j = 0
                for i in range(num):
                    if pid[i] not in last_PIDs:
                        self.PIDs.append(pid[i])
                        vnt_pid.set_value(str(j), pid[i])
                        j = j + 1
                vnt_pid.set_value(VNT_Config.KEY_TOTAL_NUMBER_OF_PID, j)
            return True

        elif cmd.lower() == "remove":
            try:
                os.remove(os.path.join(self.workingdir, self.PID_FILE))
                if os.path.exists(os.path.join(self.workingdir, self.PID_FILE_BACKUP)):
                    os.remove(os.path.join(self.workingdir, self.PID_FILE_BACKUP))
                return True
            except Exception as e:
                self.logger.write(f"Delete PID file {e}", 'critical')
                return False
        else:
            return False

    def _resource_path(self, relative_path=""):
        """ 获取资源文件的绝对路径，兼容开发环境和 PyInstaller 打包 """
        if getattr(sys, 'frozen', False):
            # 打包后的环境
            if hasattr(sys, '_MEIPASS'):
                # PyInstaller --onefile 模式
                base_path = sys._MEIPASS
            else:
                # PyInstaller --onedir 模式，或 cx_Freeze 等
                base_path = os.path.dirname(sys.executable)
        else:
            # 开发环境：基于当前脚本所在目录
            base_path = os.path.dirname(os.path.abspath(__file__))

        return os.path.join(base_path, relative_path)

    def _run_as_admin(self, run_any_way=False):
        if not ctypes.windll.shell32.IsUserAnAdmin() or run_any_way:
            if getattr(sys, 'frozen', False):
                script = ''
            else:
                script = os.path.abspath(sys.argv[0])

            args = ' '.join(sys.argv[1:]) if len(sys.argv) > 1 else ''
            try:
                ShellExecuteEx(lpFile=sys.executable, lpParameters=f"{script} {args}", nShow=1, lpVerb='runas')
            except Exception:
                print("\nFail to run as admin\n")
            sys.exit(0)

    def _process_exit_signal(self):
        if not os.path.exists(os.path.join(self.workingdir, self.PID_FILE)):  # in case deleted by earler vnt_helper on EXIT request from this vnt_helper
            if self._process_PID("set"):
                self.logger.write(f"Re-establish {self.PID_FILE}")

        for i in range(10):
            try:
                self.server_pipe = win32pipe.CreateNamedPipe(
                    self.PIPE_NAME,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                    1, 65536, 65536, 0, None
                )
                self.logger.write("Exit_Daemon Established. Waiting for data...", 'info')
                break
            except Exception as e:
                self.logger.write(f"Error # {i} {e}. Will retry...", 'debug')
                time.sleep(1)

        if i >= 9:
            return

        try:
            win32pipe.ConnectNamedPipe(self.server_pipe, None)
            data = win32file.ReadFile(self.server_pipe, 65536)[1].decode('utf-8')

            if data == self.EXIT_SIGNAL:
                self.logger.write(f"Signal Received: {data}, reply ACK and will stop VNT Helper", 'info')
                win32file.WriteFile(self.server_pipe, self.EXIT_SIGNAL_ACK.encode('utf-8'))
            elif data == self.CLOSE_EXIT_SIGNAL_PROCESS:
                self.logger.write(f"Signal Received: {data}, reply ACK...", 'info')
                win32file.WriteFile(self.server_pipe, self.CLOSE_EXIT_SIGNAL_ACK.encode('utf-8'))
            else:
                self.logger.write(f"Unknown Signal Received: {data}, reply ACK...", 'info')
                win32file.WriteFile(self.server_pipe, "UNKOWN Signal".encode('utf-8'))

            win32file.FlushFileBuffers(self.server_pipe)
            win32pipe.DisconnectNamedPipe(self.server_pipe)
            win32file.CloseHandle(self.server_pipe)
            self.server_pipe = None
            self.logger.write("Exit signal server thread ends...", 'info')

        except Exception as e:
            self.logger.write(f"Process exit signal {e}", 'critical')

        finally:
            if data == self.EXIT_SIGNAL:
                self.stop()

    def _process_post_update_task(self):
        self.logger.write("Post update task started...", 'info')

        # Remove VNT_SESSION_0 task from task scheduler if it exists
        # ret = self.reg_task_autorun._remove_task("VNT_SESSION_0")
        # self.logger.write(f"Remove VNT_SESSION_0 task from task scheduler: {'Success' if ret else 'Failed'}", 'info')

    def kill_process(self, proc1, pid_to_exclude=None):
        is_vnt_helper_on, num, pid = self._get_process_list(proc1)

        self.logger.write(f"Killing Process {proc1}: {num} process(es) found")

        if is_vnt_helper_on:
            for p in pid:
                if p != pid_to_exclude:
                    try:
                        os.kill(p, signal.SIGINT)
                    except Exception as e:
                        self.logger.write(f"Killing Process Pid: {p} {e}", 'critical')
                        return False

            return True
        return True

    def start(self):

        self.process_exit_signal_thread = threading.Thread(target=self._process_exit_signal)
        self.process_exit_signal_thread.daemon = True
        self.process_exit_signal_thread.start()

        while not self.inet_monitor.is_connected():  # Wait for Internet Connection ...
            if self.exit_status is True:
                return
            time.sleep(1)

        self.bubble_msg_handler = Bubble_Message(self)
        self.bubble_msg_handler.start()

        self.vnt_connection = VNT_Connection(self)
        self.vnt_connection.start()

        self._main_GUI_loop()

    def stop(self, force_daemon_session0_out=False):
        global VNT_CLIENT_NAME
        self.exit_status = True
        try:
            if self.server_pipe is not None:
                self._close_exit_signal_thread()

            if self.inet_monitor is not None:
                self.inet_monitor.stop()

            if self.vnt_connection is not None:
                self.vnt_connection.stop()
                while self.vnt_connection.is_running():
                    time.sleep(0.1)

            if self.main_window is not None:
                self.main_window.vnt_info.exit_flag.set()
                self.main_window.vnt_update_window.vnt_update_daemon_exit_flag.set()

            self._process_PID("remove")

            if self.bubble_msg_handler is not None:
                self.bubble_msg_handler.stop()
                while self.bubble_msg_handler.is_alive():
                    time.sleep(0.1)

            time.sleep(0.5)

            if self.main_gui_app is not None:
                wx.GetApp().ExitMainLoop()

            if os.path.exists(os.path.join(self.workingdir, self.config_fn)):  # in case is removed by reset
                vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
                vnt_conf.set_value(VNT_Config.KEY_VNT_PREV_PROFILE, '')

            self.logger.write("******************* Session Ends *******************")
        except Exception as e:
            self.logger.write(f"Exit_VNT_Helper {e}", 'critical')

    def is_service_installed(self, write_log=True):
        """Check if the VNT daemon service is installed"""
        try:
            # Use pywin32 to check if the service exists
            scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ENUMERATE_SERVICE)
            try:
                service = win32service.OpenService(scm, 'VNTDaemonService', win32service.SERVICE_QUERY_STATUS)
                win32service.CloseServiceHandle(service)
                win32service.CloseServiceHandle(scm)
                return True
            except Exception as e:
                if write_log:
                    self.logger.write(f"Cannot open service: {e}")
                else:
                    print(f"Cannot open service: {e}")
                win32service.CloseServiceHandle(scm)
                return False
        except Exception as e:
            if write_log:
                self.logger.write(f"Error in is_service_installed, service control manager: {e}")
            else:
                print(f"Error in is_service_installed, service control manager: {e}")
            return False

    def install_service(self):
        """Install the VNT daemon service"""
        try:
            # Install the service using the command line interface
            service_path = os.path.join(self.workingdir, "vnt_service.exe")

            # Check if the service executable exists
            if not os.path.exists(service_path):
                self.logger.write(f"Service executable not found: {service_path}", 'critical')
                return False

            # Use pywin32 to install the service
            # Open the service manager with create service rights
            scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CREATE_SERVICE)

            try:
                # Create the service
                service = win32service.CreateService(
                    scm,
                    "VNTDaemonService",  # Service name
                    "VNT Daemon Service",  # Display name
                    win32service.SERVICE_ALL_ACCESS,  # Desired access
                    win32service.SERVICE_WIN32_OWN_PROCESS,  # Service type
                    win32service.SERVICE_AUTO_START,  # Start type
                    win32service.SERVICE_ERROR_NORMAL,  # Error control
                    service_path,  # Binary path
                    None,  # Load order group
                    0,  # Tag ID
                    None,  # Dependencies
                    None,  # Account name
                    None  # Password
                )

                win32service.CloseServiceHandle(service)
                self.logger.write("VNT daemon service installed successfully", 'info')
                return True
            except Exception as e:
                self.logger.write(f"Error installing VNT daemon service: {e}")
                # If service already exists, try to open and update it
                try:
                    service = win32service.OpenService(scm, "VNTDaemonService", win32service.SERVICE_CHANGE_CONFIG)
                    # Update the service configuration
                    win32service.ChangeServiceConfig(
                        service,
                        win32service.SERVICE_WIN32_OWN_PROCESS,
                        win32service.SERVICE_AUTO_START,
                        win32service.SERVICE_ERROR_NORMAL,
                        service_path,
                        None, None, None, None, None, None
                    )
                    win32service.CloseServiceHandle(service)
                    self.logger.write("VNT daemon service updated successfully", 'info')
                    return True
                except Exception as e:
                    self.logger.write(f"Failed to install VNT daemon service: {e}", 'critical')
                    return False
            finally:
                win32service.CloseServiceHandle(scm)
        except Exception as e:
            self.logger.write(f"Error installing VNT daemon service: {e}", 'critical')
            return False

    def uninstall_service(self):
        """Uninstall the VNT daemon service"""
        try:
            # Check if service is running first and stop it
            if self.is_service_running():
                self.stop_service()

            # Use pywin32 to uninstall the service
            scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)

            try:
                service = win32service.OpenService(scm, "VNTDaemonService", win32service.SERVICE_ALL_ACCESS)

                # Delete the service
                win32service.DeleteService(service)
                win32service.CloseServiceHandle(service)

                self.logger.write("VNT daemon service uninstalled successfully", 'info')
                return True
            except Exception as e:
                self.logger.write(f"Failed to uninstall VNT daemon service: {e}", 'critical')
                return False
            finally:
                win32service.CloseServiceHandle(scm)
        except Exception as e:
            self.logger.write(f"Error uninstalling VNT daemon service: {e}", 'critical')
            return False

    def start_service(self):
        """Start the VNT daemon service"""
        try:
            if not self.is_service_installed():
                self.logger.write("VNT daemon service is not installed", 'critical')
                return False

            # Use pywin32 to start the service
            scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)

            try:
                service = win32service.OpenService(scm, "VNTDaemonService", win32service.SERVICE_START)

                win32service.StartService(service, None)
                win32service.CloseServiceHandle(service)

                self.logger.write("VNT daemon service started", 'info')
                return True
            except Exception as e:
                self.logger.write(f"Failed to start VNT daemon service: {e}", 'critical')
                return False
            finally:
                win32service.CloseServiceHandle(scm)
        except Exception as e:
            self.logger.write(f"Error starting VNT daemon service: {e}", 'critical')
            return False

    def stop_service(self):
        """Stop the VNT daemon service"""
        try:
            if not self.is_service_installed():
                self.logger.write("VNT daemon service is not installed", 'critical')
                return False

            # Use pywin32 to stop the service
            scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)

            try:
                service = win32service.OpenService(scm, "VNTDaemonService", win32service.SERVICE_STOP)

                win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
                win32service.CloseServiceHandle(service)

                self.logger.write("VNT daemon service stopped", 'info')
                return True
            except Exception as e:
                self.logger.write(f"Failed to stop VNT daemon service: {e}", 'critical')
                return False
            finally:
                win32service.CloseServiceHandle(scm)
        except Exception as e:
            self.logger.write(f"Error stopping VNT daemon service: {e}", 'critical')
            return False

    def is_service_running(self, write_log=True):
        """Check if the VNT daemon service is running"""
        try:
            # Use pywin32 to check if the service is running
            scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)

            try:
                service = win32service.OpenService(scm, "VNTDaemonService", win32service.SERVICE_QUERY_STATUS)

                status = win32service.QueryServiceStatus(service)
                win32service.CloseServiceHandle(service)

                # Check if service state is running (4)
                return status[1] == win32service.SERVICE_RUNNING
            except Exception as e:
                if write_log:
                    self.logger.write(f"Error in is_service_running: {e}", 'critical')
                else:
                    print(f"Error in is_service_running: {e}")
                return False
            finally:
                win32service.CloseServiceHandle(scm)
        except Exception as e:
            if write_log:
                self.logger.write(f"Error open service contorl manager: {e}", 'critical')
            else:
                print(f"Error open service contorl manager: {e}")
            return False

    def get_service_status(self):
        """Get the current status of the VNT daemon service"""
        try:
            # Use pywin32 to get the service status
            scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)

            try:
                service = win32service.OpenService(scm, "VNTDaemonService", win32service.SERVICE_QUERY_STATUS)

                status = win32service.QueryServiceStatus(service)
                win32service.CloseServiceHandle(service)

                # Service state codes:
                # SERVICE_STOPPED = 1
                # SERVICE_START_PENDING = 2
                # SERVICE_STOP_PENDING = 3
                # SERVICE_RUNNING = 4
                # SERVICE_CONTINUE_PENDING = 5
                # SERVICE_PAUSE_PENDING = 6
                # SERVICE_PAUSED = 7
                state = status[1]

                if state == win32service.SERVICE_RUNNING:
                    return "RUNNING"
                elif state == win32service.SERVICE_STOPPED:
                    return "STOPPED"
                elif state == win32service.SERVICE_PAUSED:
                    return "PAUSED"
                elif state == win32service.SERVICE_START_PENDING:
                    return "STARTING"
                elif state == win32service.SERVICE_STOP_PENDING:
                    return "STOPPING"
                else:
                    return "UNKNOWN"
            except Exception as e:
                self.logger.write(f"Error checking service status: {e}", 'critical')
                return "NOT_INSTALLED"
            finally:
                win32service.CloseServiceHandle(scm)
        except Exception as e:
            self.logger.write(f"Error checking service status: {e}", 'critical')
            return "ERROR"

    def set_service_startup_type(self, startup_type="auto"):
        """Set the startup type of the VNT daemon service"""
        try:
            # Determine the startup type parameter
            if startup_type.lower() in ["auto", "automatic"]:
                start_type = win32service.SERVICE_AUTO_START
            elif startup_type.lower() in ["demand", "manual"]:
                start_type = win32service.SERVICE_DEMAND_START
            elif startup_type.lower() == "disabled":
                start_type = win32service.SERVICE_DISABLED
            else:
                self.logger.write(f"Invalid startup type: {startup_type}", 'critical')
                return False

            # Use pywin32 to change the service startup type
            scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)

            try:
                service = win32service.OpenService(scm, "VNTDaemonService", win32service.SERVICE_CHANGE_CONFIG)

                # Change the service configuration
                win32service.ChangeServiceConfig(
                    service,
                    win32service.SERVICE_NO_CHANGE,  # Service type
                    start_type,  # Start type
                    win32service.SERVICE_NO_CHANGE,  # Error control
                    None,  # Binary path
                    None,  # Load order group
                    0,  # Tag ID
                    None,  # Dependencies
                    None,  # Account name
                    None,  # Password
                    None   # Display name
                )

                win32service.CloseServiceHandle(service)
                self.logger.write(f"VNT daemon service startup type set to {startup_type}", 'info')
                return True
            except Exception as e:
                self.logger.write(f"Failed to set VNT daemon service startup type: {e}", 'critical')
                return False
            finally:
                win32service.CloseServiceHandle(scm)
        except Exception as e:
            self.logger.write(f"Error setting VNT daemon service startup type: {e}", 'critical')
            return False

    @staticmethod
    def setup_i18n(lang_code):
        """
        根据 lang_code 初始化 gettext 翻译
        :param lang_code: 如 'en', 'zh_CN'
        :return: gettext 的翻译函数 _
        """
        # 获取资源目录（支持 PyInstaller 打包后的情况）
        if getattr(sys, 'frozen', False):
            # 打包后的 exe 路径
            base_path = sys._MEIPASS
        else:
            # 开发时的脚本路径
            base_path = os.path.dirname(os.path.abspath(__file__))

        localedir = os.path.join(base_path, 'res\\locale')

        print(f"locale dir {localedir}")

        try:
            lang = gettext.translation('vnt_helper', localedir=localedir, languages=[lang_code])
            lang.install()
            _ = lang.gettext
        except FileNotFoundError:
            # 如果找不到指定语言，回退到默认（不翻译）
            _ = gettext.gettext
            print(f"Warning: Translation file for '{lang_code}' not found. Using default language.")

        return _


class VNT_Connection():
    MAX_RETRY_CONNECTION = 5
    VIRTUAL_IP_TEXT = "VNT virtual IP = "
    IPC_HOST = "127.0.0.1"
    IPC_PORT = 58432

    def __init__(self, vnt_app, toggled_off=False):
        self.running = False
        self.toggled_off = toggled_off
        self.thread = None
        self.config_fn = vnt_app.config_fn
        self.workingdir = vnt_app.workingdir
        self.logger = vnt_app.logger
        self.vnt_app = vnt_app
        self.virtual_IP = None
        self.vnt_comm_thread = None
        self.vnt_monitor_thread = None
        self.connection_profile_ready = False
        self._connected_notified = False  # 标记是否已显示过连接成功通知

    def __del__(self):
        pass

    def _send_ipc_command(self, cmd_dict, timeout=5):
        try:
            with socket.create_connection((self.IPC_HOST, self.IPC_PORT), timeout=timeout) as sock:
                sock.send(json.dumps(cmd_dict).encode('utf-8'))
                resp = sock.recv(1024).decode('utf-8')
                return json.loads(resp)
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    def _event_listener(self, event_sock):
        """直接从 socket 读取，避免 makefile 的 timeout 陷阱"""
        buffer = b""
        try:
            while getattr(self, '_event_listening', False):
                try:
                    # 每次只 recv 一小块数据（非阻塞式读取）
                    chunk = event_sock.recv(4096)
                    if not chunk:
                        # 对端关闭连接
                        break
                    buffer += chunk

                    # 处理所有完整行
                    while b'\n' in buffer:
                        line_bytes, buffer = buffer.split(b'\n', 1)
                        line_str = line_bytes.decode('utf-8', errors='replace').strip()
                        if line_str:
                            try:
                                event = json.loads(line_str)
                                self._handle_vnt_event(event)
                            except json.JSONDecodeError:
                                continue  # 忽略非法 JSON
                    # 继续读取下一块
                except socket.timeout:
                    # 超时是正常的，继续循环（recv 会重新开始计时）
                    continue
                except (OSError, ConnectionError) as e:
                    # 真正的连接错误
                    if self._event_listening:
                        self.logger.write(f"Event stream disconnected: {e}", 'critical')
                    break
        except Exception as e:
            if self._event_listening:
                self.logger.write(f"Event listener fatal error: {e}", 'critical')
        finally:
            try:
                event_sock.close()
            except Exception:
                pass
            self._event_listening = False

    def _handle_vnt_event(self, event: dict):
        etype = event.get("event")
        if etype == "IP_assigned":
            ip = event.get("ip", "unknown")
            self.virtual_IP = ip
            self.logger.write(f"[IPC] IP Assigned. Virtual IP: {self.virtual_IP}", 'debug')
            
            # 当IP分配时，如果已连接但未通知，立即显示通知
            if not self._connected_notified and self.vnt_app.bubble_msg_handler is not None and self.vnt_app.args.no_gui is False:
                self.vnt_app.bubble_msg_handler.msg(f"Connected#{self.VIRTUAL_IP_TEXT}{self.virtual_IP}")
                self._connected_notified = True
                self.logger.write("[IPC] IP assigned notification displayed.", 'debug')

        elif etype == "connected":
            self.logger.write("[IPC] VNT Server Connected Successfully.")
            if self.virtual_IP is None:
                resp = self._send_ipc_command({"cmd": "status"})
                if resp.get("status") == "ok":
                    self.virtual_IP = resp.get("virtual_ip")
                    self.logger.write(f"[IPC] Get Virtual IP from Daemon: {self.virtual_IP}")
            
            # 如果已有IP但未通知，显示通知
            if not self._connected_notified and self.vnt_app.bubble_msg_handler is not None and self.virtual_IP is not None and self.vnt_app.args.no_gui is False:
                self.vnt_app.bubble_msg_handler.msg(f"Connected#{self.VIRTUAL_IP_TEXT}{self.virtual_IP}")
                self._connected_notified = True
                self.logger.write("[IPC] Connection notification displayed.", 'debug')

        elif etype == "server_connection":
            self.vnt_app.vnt_server_version = event.get("server_version", "0.0.0")
            print(f"VNT Server Version: {self.vnt_app.vnt_server_version}")

        elif etype == "version_info":
            self.vnt_app.vnt_cli_version = event.get("version", "0.0.0")
            print(f"VNT CLI Version: {self.vnt_app.vnt_cli_version}")

        elif etype == "serial_info":
            self.vnt_app.vnt_cli_serial = event.get("serial", "Unknown Serial")
            print(f"VNT CLI Serial: {self.vnt_app.vnt_cli_serial}")

        elif etype == "error" or etype == "error_conf":
            self.logger.write(f"[IPC] VNT error {event.get('message')}", 'info')

        elif etype == "reconnect_count":
            count = event.get("connect_count", 0)
            print(f"VNT Reconnect Count: {count}")
            self.logger.write("VNT Reconnection in progress...")

        else:
            self.logger.write(f"[IPC] Unknown VNT event: {etype}")

    def _maintain_vnt_daemon(self):

        def initialize_vnt_daemon():
            """Initialize VNT daemon with retry logic for robustness"""
            max_retries = 3
            retry_delay = 2  # seconds
            
            for attempt in range(max_retries):
                try:
                    self.logger.write(f"Attempting to initialize VNT daemon (attempt {attempt + 1}/{max_retries})...", 'info')
                    
                    # First, check if daemon is already running
                    resp = self._send_ipc_command({"cmd": "status"}, timeout=3)
                    
                    if resp.get("status") == "ok" and resp.get("running") == "yes":
                        # Daemon is already running, check virtual IP
                        ip = resp.get("virtual_ip")
                        if Internet_Connectivity_Monitor.is_valid_IP(ip):
                            self.virtual_IP = ip
                            self.logger.write(f"VNT Daemon already running. Virtual IP: {self.virtual_IP}")
                            self.toggled_off = False
                            return True  # Success
                        else:
                            # Running but no valid IP, restart
                            self.logger.write("Daemon running but no valid IP, restarting...", 'info')
                            resp = self._send_ipc_command({"cmd": "restart"}, timeout=5)
                            if resp.get("status") != "ok":
                                raise RuntimeError(f"Daemon restart failed: {resp.get('msg', 'unknown')}")
                            self.logger.write("Daemon restarted successfully", 'info')
                            return True
                    
                    else:
                        # Daemon not running, start it
                        if not self.toggled_off:
                            self.logger.write("Daemon not running, sending start command...", 'info')
                            resp = self._send_ipc_command({"cmd": "start"}, timeout=5)
                            
                            if resp.get("status") != "ok":
                                error_msg = resp.get('msg', 'unknown')
                                self.logger.write(f"Daemon start command failed: {error_msg}", 'warning')
                                
                                # If this is not the last attempt, wait and retry
                                if attempt < max_retries - 1:
                                    self.logger.write(f"Retrying in {retry_delay} seconds...", 'info')
                                    time.sleep(retry_delay)
                                    continue
                                else:
                                    raise RuntimeError(f"Daemon start failed after {max_retries} attempts: {error_msg}")
                            else:
                                self.logger.write("Daemon start command succeeded", 'info')
                                return True
                        else:
                            # User has toggled off, don't start
                            self.stop_vnt_network()
                            return True
                            
                except Exception as e:
                    self.logger.write(f"Initialize vnt daemon error (attempt {attempt + 1}): {e}", 'warning')
                    
                    # If this is not the last attempt, wait and retry
                    if attempt < max_retries - 1:
                        self.logger.write(f"Retrying in {retry_delay} seconds...", 'info')
                        time.sleep(retry_delay)
                    else:
                        # Last attempt failed
                        self.logger.write(f"Failed to initialize VNT daemon after {max_retries} attempts", 'critical')
                        return False
            
            return False

        vnt_config_file = None
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)

        while vnt_config_file is None or (not os.path.exists(vnt_config_file)):
            try:
                vnt_config_file = vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML, False)  # Do not write log here to prevent log spam
                if vnt_config_file is None or (not os.path.exists(vnt_config_file)):
                    # First run and vnt_service exists from other copies of vnt_helper, uninstall it
                    if self.vnt_app.is_service_installed(False):  # Do not write log here to prevent log spam
                        self.logger.write("VNT daemon service exists from other vnt_helper copies, uninstalling first...", 'info')

                        if self.vnt_app.get_service_status() == "RUNNING":
                            resp = self.vnt_app.vnt_connection._send_ipc_command({"cmd": "exit"})
                            # 新的 exit 命令只停止 vnt2_cli.exe，不终止守护进程
                            if resp.get("status") == "ok":
                                self.logger.write("Exit command succeeded: vnt2_cli.exe stopped")
                            else:
                                self.logger.write(f"Daemon exit command response: {resp.get('msg', 'unknown')}")

                            self.logger.write("Stopping VNT daemon service from another session...", 'info')
                            self.vnt_app.stop_service()

                        self.vnt_app.uninstall_service()
            except Exception as e:
                self.logger.write(f"While initializing first run service and config check {e}", 'critical')
            finally:
                if not self.running:
                    return
                time.sleep(0.1)

        self.connection_profile_ready = True

        while self.running:
            # Check if the service is running instead of checking for a process
            service_running = self.vnt_app.is_service_running()
            service_installed = self.vnt_app.is_service_installed()

            if not service_installed and not self.vnt_app.update_process_started:  # Service not installed
                try:
                    self.logger.write("VNT daemon service not installed, installing now...", 'info')
                    install_success = self.vnt_app.install_service()
                    if install_success:
                        self.logger.write("VNT daemon service installed successfully", 'info')
                        # Set the service to auto-start
                        auto_start_success = self.vnt_app.set_service_startup_type("auto")
                        if not auto_start_success:
                            self.logger.write("Warning: Failed to set VNT daemon service to auto-start", 'warning')

                        # Now start the service
                        start_success = self.vnt_app.start_service()
                        if start_success:
                            self.virtual_IP = None
                            initialize_vnt_daemon()
                        else:
                            self.logger.write("Failed to start VNT Daemon service after installation", 'critical')
                    else:
                        self.logger.write("Failed to install VNT daemon service", 'critical')
                except Exception as e:
                    self.logger.write(f"Error installing VNT daemon service: {e}", 'critical')
            elif not service_running and not self.vnt_app.update_process_started:  # Service installed but not running
                try:
                    # Start the service instead of launching a process
                    success = self.vnt_app.start_service()
                    if success:
                        self.virtual_IP = None
                        # Wait for the service to fully initialize before sending IPC commands
                        # The daemon needs time to start up and begin listening on the IPC port
                        self.logger.write("Service started, waiting for daemon initialization...", 'info')
                        time.sleep(3)  # Give daemon time to initialize IPC listener
                        
                        # Verify service is actually running before proceeding
                        if self.vnt_app.is_service_running(False):
                            initialize_vnt_daemon()
                            self.logger.write("initialize_vnt_daemon() successfully", 'info')
                        else:
                            self.logger.write("Service failed to stay running after start", 'critical')
                    else:
                        self.logger.write("_maintain_vnt_daemon, self.vnt_app.start_service() failed", 'critical')
                except Exception as e:
                    self.logger.write(f"_maintain_vnt_daemon Exception: {e}", 'critical')
            else:
                if self.virtual_IP is None and self.toggled_off is False and not self.vnt_app.update_process_started:
                    initialize_vnt_daemon()
            time.sleep(10)

    def _gui_daemon_comm(self):
        try:
            while not self.vnt_app.is_service_running(False) or not self.connection_profile_ready:
                #                        ^                                         ^
                #                        |                                         |
                #                  avoid log spam                   only proceed when connection profile is ready on first run
                if not self.running:
                    return
                time.sleep(0.1)

            time.sleep(2)
            # 订阅事件流
            event_sock = socket.create_connection((self.IPC_HOST, self.IPC_PORT), timeout=5)
            event_sock.settimeout(60)  # 60秒无数据算超时（但我们会 continue）
            event_sock.send(json.dumps({"cmd": "subscribe_events"}).encode('utf-8'))

            # 启动事件监听线程
            self._event_listening = True
            self._event_thread = threading.Thread(
                target=self._event_listener,
                args=(event_sock,),  # ← 传 socket 本身
                daemon=True
            )
            self._event_thread.start()

            self.logger.write("Listener started. Event subscription active.")

        except Exception as e:
            self.logger.write(f"Failed to subscribe evnts in gui: {e}", 'critical')
            if hasattr(self, '_event_listening'):
                self._event_listening = False

    def stop_vnt_network(self):
        """仅通知 daemon 关闭网络（退出前调用）"""
        if not self.is_toggled_off() and self.running:
            self._send_ipc_command({"cmd": "stop_network"})
            self.toggled_off = True
            self.virtual_IP = None
            self._connected_notified = False  # 重置连接通知标志，下次连接时可再次显示
            self.logger.write("VNT network stopped (daemon remains).")

    def cleanup(self):
        """清理资源（可选）"""
        self._event_listening = False

    def start(self):
        if not self.running:
            self.running = True
            time.sleep(0.5)  # wait for daemon process to run
            self.vnt_comm_thread = threading.Thread(target=self._gui_daemon_comm)
            self.vnt_comm_thread.daemon = True
            self.vnt_comm_thread.start()
            time.sleep(0.5)
            self.vnt_monitor_thread = threading.Thread(target=self._maintain_vnt_daemon)
            self.vnt_monitor_thread.daemon = True
            self.vnt_monitor_thread.start()
            time.sleep(0.5)

    def stop_vnt_network(self):
        """仅通知 daemon 关闭网络（Toggle OFF 或退出前调用）"""
        self.logger.write(f"stop_vnt_network() called - toggled_off={self.toggled_off}, running={self.running}", "debug")
        
        # 只要还在运行，就发送停止命令（不检查 toggled_off 状态）
        if self.running:
            try:
                self._send_ipc_command({"cmd": "stop_network"}, timeout=3)
                self.toggled_off = True
                self.virtual_IP = None
                self._connected_notified = False  # 重置连接通知标志，下次连接时可再次显示
                self.logger.write("VNT network stopped (daemon remains).")
            except Exception as e:
                self.logger.write(f"Failed to send stop_network command: {e}", "warning")
        else:
            self.logger.write("VNT connection is not running, skipping stop_network", "debug")

    def stop(self):
        """停止 VNT 网络连接（GUI 退出时调用）"""
        self.logger.write("VNT_Connection.stop() called - GUI is exiting", "info")
        self.running = False
        
        if self.vnt_comm_thread:
            # GUI 退出时必须停止 vnt2_cli.exe，但保持守护进程运行
            self.logger.write("GUI exiting, sending stop_network command to daemon...", "info")
            
            # 直接发送 stop_network 命令，不检查状态
            try:
                resp = self._send_ipc_command({"cmd": "stop_network"}, timeout=3)
                self.logger.write(f"stop_network command response: {resp}", "info")
                self.toggled_off = True
                self.virtual_IP = None
                self._connected_notified = False
                self.logger.write("VNT network stopped successfully (daemon remains running).")
            except Exception as e:
                self.logger.write(f"Failed to send stop_network command: {e}", "warning")
                self.logger.write("This may be because IPC connection is already closed", "warning")
            
            # 等待通信线程结束
            self.vnt_comm_thread.join(timeout=2)
            self.logger.write("VNT connection communication thread stopped.")

    def is_running(self):
        return self.running

    def restart_vnt_network(self):
        try:
            resp = self._send_ipc_command({"cmd": "restart"})
            if resp.get("status") != "ok":
                raise RuntimeError(f"Daemon restart failed: {resp.get('msg', 'unknown')}")
        except Exception as e:
            self.logger.write(f"{e}", 'critical')

    def toggle(self, status):
        self.toggled_off = status

        if self.is_toggled_off():
            # Toggle OFF: stop_vnt_network 会重置 _connected_notified
            self.stop_vnt_network()
        else:
            # Toggle ON: 重置连接通知标志，允许下次连接时显示通知
            self._connected_notified = False
            resp = self._send_ipc_command({"cmd": "start"})
            if resp.get("status") != "ok":
                self.logger.write(f"Daemon start failed: {resp.get('msg', 'unknown')}")
            else:
                self.logger.write("VNT network started (daemon remains).")

    def is_toggled_off(self):
        return self.toggled_off


class VNT_Logger():
    def __init__(self, workingdir, fn, no_logger=False, debug_mode=False):
        self.log_fn = fn
        self.workingdir = workingdir
        self.debug_mode = debug_mode  # 保存调试模式标志
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
        # 如果不是调试模式，过滤掉打洞相关的冗余日志和警告信息
        if not self.debug_mode:
            # 过滤条件列表
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
    KEY_VNT_PREV_PROFILE = 'previous_profile'
    KEY_VNT_PROFILE_LIST = 'profile_list'
    KEY_VNT_NOTIFICATION_ENABLED = 'notification_enabled'
    KEY_CHECKSUM = "checksum"
    KEY_EXCLUDE = "exclude"
    KEY_AUTO_UPDATE_ENABLED = "auto_update_enabled"
    KEY_UPDATE_DISABLED = "update_disabled"
    KEY_UPDATE_URL = "update_file_url"
    KEY_VERSION = "version"
    KEY_VERSION_FILE_URL = "update_version_url"
    KEY_UPDATE_CYCLE_SEC = "update_cycle_sec"
    KEY_TOTAL_NUMBER_OF_PID = "total_number_of_pid"
    KEY_DISPLAY_LANGUAGE = "display_language"
    KEY_AUTORUN_CLI_ON_STARTUP = "autorun_cli_on_startup"

    def __init__(self, workingdir, fn, logger):
        self.working_dir = workingdir
        self.config_fn = fn
        self.logger = logger

    def get_value(self, key, write_log=True):
        fn = os.path.join(self.working_dir, self.config_fn)
        try:
            with open(fn, 'r', encoding='utf-8') as file:
                data = yaml.safe_load(file)
            return data[key]
        except Exception as e:
            if write_log:
                self.logger.write(f"get_value {e}, return None", "debug")
            else:
                print(f"get_value {e}, return None")
            return None

    def get_data(self):
        fn = os.path.join(self.working_dir, self.config_fn)
        try:
            with open(fn, 'r', encoding='utf-8') as file:
                data = yaml.safe_load(file)
            return data
        except Exception as e:
            self.logger.write(f"get_data {e}, return None", "debug")
            return None

    def set_value(self, key_name, key_value):
        fn_config = os.path.join(self.working_dir, self.config_fn)
        if not os.path.exists(fn_config):
            try:
                with open(fn_config, 'w', encoding='utf-8') as file:
                    yaml.safe_dump({key_name: key_value}, file, allow_unicode=True, sort_keys=False, default_style=None)
                    return True
            except Exception as e:
                self.logger.write(f"set_value {e}, return False", "debug")
            return False
        else:
            try:
                with open(fn_config, 'r', encoding='utf-8') as file:
                    data = yaml.safe_load(file)
                    if data is None:
                        data = {}
                    # Handle specific keys that should be stored as specific types
                    if key_name == VNT_Config.KEY_UPDATE_CYCLE_SEC:
                        # Ensure update_cycle_sec is stored as integer
                        try:
                            data[key_name] = int(key_value)
                        except (ValueError, TypeError):
                            data[key_name] = 60  # default value
                    else:
                        data[key_name] = key_value
            except Exception as e:
                self.logger.write(f"set_value {e}, return False", "debug")
                return False

            try:
                with open(fn_config, 'w', encoding='utf-8') as file:
                    yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False, default_style=None)
                    return True
            except Exception as e:
                self.logger.write(f"set_value {e}, return False", "debug")
                return False

    def set_data(self, data):
        fn_config = os.path.join(self.working_dir, self.config_fn)
        if not os.path.exists(fn_config):
            try:
                # Process data to ensure correct types for specific keys
                processed_data = self._process_data_types(data)
                with open(fn_config, 'w', encoding='utf-8') as file:
                    yaml.safe_dump(processed_data, file, allow_unicode=True, sort_keys=False, default_style=None)
                    file.flush()
                    os.fsync(file.fileno())
                    return True
            except Exception as e:
                self.logger.write(f"set_data {e}, return False", "debug")
            return False
        else:
            try:
                with open(fn_config, 'r', encoding='utf-8') as file:
                    exiting_data = yaml.safe_load(file)
                    if exiting_data is None:
                        exiting_data = {}
                    # Process the incoming data to ensure correct types
                    processed_data = self._process_data_types(data)
                    exiting_data.update(processed_data)
            except Exception as e:
                self.logger.write(f"existing_data {e}, return False", "debug")
                return False

            try:
                with open(fn_config, 'w', encoding='utf-8') as file:
                    yaml.safe_dump(exiting_data, file, allow_unicode=True, sort_keys=False, default_style=None)
                    file.flush()
                    os.fsync(file.fileno())
                    return True
            except Exception as e:
                self.logger.write(f"set_data {e}, return False", "debug")
                return False

    def _process_data_types(self, data):
        """Process data to ensure correct types for specific keys"""
        processed = data.copy() if data else {}

        # Handle specific keys that should be stored as specific types
        if VNT_Config.KEY_UPDATE_CYCLE_SEC in processed:
            try:
                processed[VNT_Config.KEY_UPDATE_CYCLE_SEC] = int(processed[VNT_Config.KEY_UPDATE_CYCLE_SEC])
            except (ValueError, TypeError):
                processed[VNT_Config.KEY_UPDATE_CYCLE_SEC] = 60  # default value

        return processed


class VNT_Main_Window(wx.Frame):

    def __init__(self, parent, vnt_app, fresh_start_after_update=False):
        # === 动态计算缩放因子 ===
        screen_dc = wx.ScreenDC()
        dpi_x = screen_dc.GetPPI()[0]
        scale_factor = dpi_x / 96.0
        scale_factor = max(1.0, scale_factor)

        def scale(val):
            return int(val * scale_factor)

        win_width = scale(670)
        win_height = scale(530)

        wx.Frame.__init__(self, parent, id=wx.ID_ANY, title=_("VNT Setting"), pos=wx.DefaultPosition, size=wx.Size(win_width, win_height), style=wx.CAPTION | wx.STATIC_BORDER | wx.TAB_TRAVERSAL)

        self.SetSizeHints(wx.DefaultSize, wx.DefaultSize)
        self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_SCROLLBAR))

        panel = wx.Panel(self, style=wx.TAB_TRAVERSAL)
        self.panel = panel

        bSizer1 = wx.BoxSizer(wx.VERTICAL)

        LEFT_MARGIN = scale(30)
        HORIZONTAL_GAP = scale(10)

        # === Configuration Name ===
        self.m_staticText1 = wx.StaticText(panel, wx.ID_ANY, _("Configuration Name"), style=wx.ALIGN_CENTRE)
        self.m_staticText1.Wrap(-1)
        bSizer1.Add(self.m_staticText1, 0, wx.TOP | wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, scale(5))

        hSizer_config = wx.BoxSizer(wx.HORIZONTAL)
        self.ConfigName = wx.TextCtrl(panel, wx.ID_ANY, wx.EmptyString, style=wx.TE_CENTRE)
        hSizer_config.Add(self.ConfigName, 1, wx.RIGHT, scale(5))
        self.m_select_button = wx.Button(panel, wx.ID_ANY, label="...", style=wx.BU_EXACTFIT)
        btn_height = self.ConfigName.GetSize().GetHeight()
        self.m_select_button.SetMinSize((btn_height, btn_height))
        hSizer_config.Add(self.m_select_button, 0, wx.LEFT, 0)
        bSizer1.Add(hSizer_config, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        bSizer1.AddSpacer(scale(5))
        self.m_staticline0 = wx.StaticLine(panel, wx.ID_ANY, style=wx.LI_HORIZONTAL)
        bSizer1.Add(self.m_staticline0, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        # === Basic Parameters ===
        self.m_staticText4 = wx.StaticText(panel, wx.ID_ANY, _("Basic Parameters"), style=wx.ALIGN_CENTRE)
        self.m_staticText4.Wrap(-1)
        bSizer1.Add(self.m_staticText4, 0, wx.TOP | wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, scale(5))

        # === Token & Device ID ===
        hSizer_token_device = wx.BoxSizer(wx.HORIZONTAL)

        vSizer_token = wx.BoxSizer(wx.VERTICAL)
        self.m_staticText5 = wx.StaticText(panel, wx.ID_ANY, _("Token"), style=wx.ALIGN_LEFT)
        self.m_staticText5.Wrap(-1)
        vSizer_token.Add(self.m_staticText5, 0, wx.TOP, scale(5))
        self.Token = wx.TextCtrl(panel, wx.ID_ANY, wx.EmptyString, style=0)
        vSizer_token.Add(self.Token, 1, wx.TOP | wx.BOTTOM | wx.EXPAND, scale(5))
        hSizer_token_device.Add(vSizer_token, 1, wx.EXPAND | wx.RIGHT, HORIZONTAL_GAP)

        vSizer_device = wx.BoxSizer(wx.VERTICAL)
        self.m_staticText7 = wx.StaticText(panel, wx.ID_ANY, _("Device ID"), style=wx.ALIGN_LEFT)
        self.m_staticText7.Wrap(-1)
        vSizer_device.Add(self.m_staticText7, 0, wx.TOP, scale(5))
        self.DeviceID = wx.TextCtrl(panel, wx.ID_ANY, wx.EmptyString, style=0)
        vSizer_device.Add(self.DeviceID, 1, wx.TOP | wx.BOTTOM | wx.EXPAND, scale(5))
        hSizer_token_device.Add(vSizer_device, 1, wx.EXPAND)

        bSizer1.Add(hSizer_token_device, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        bSizer1.AddSpacer(scale(15))

        # === Server Address List / Compression (2 columns aligned with row above) ===
        hSizer_row2 = wx.BoxSizer(wx.HORIZONTAL)

        col4 = wx.BoxSizer(wx.VERTICAL)
        self.m_staticText10 = wx.StaticText(panel, wx.ID_ANY, _("Protocol"), style=wx.ALIGN_LEFT)
        self.m_staticText10.Wrap(-1)
        col4.Add(self.m_staticText10, 0, wx.TOP, scale(5))
        # VNT2 协议选项（严格按照文档）：quic, tcp, wss, dynamic
        ProtocolChoices = [_("QUIC"), _("TCP"), _("WSS"), _("DYNAMIC")]
        self.Protocol = wx.Choice(panel, choices=ProtocolChoices)
        self.Protocol.SetSelection(0)
        col4.Add(self.Protocol, 1, wx.TOP | wx.BOTTOM | wx.EXPAND, scale(5))
        hSizer_row2.Add(col4, 1, wx.EXPAND | wx.RIGHT, HORIZONTAL_GAP)


        col5 = wx.BoxSizer(wx.VERTICAL)
        self.m_staticText11 = wx.StaticText(panel, wx.ID_ANY, _("Compression"), style=wx.ALIGN_LEFT)
        self.m_staticText11.Wrap(-1)
        col5.Add(self.m_staticText11, 0, wx.TOP, scale(5))
        CompressionChoices = [_("none"), _("lz4"), _("zstd")]
        self.Compression = wx.Choice(panel, choices=CompressionChoices)
        self.Compression.SetSelection(0)
        col5.Add(self.Compression, 1, wx.TOP | wx.BOTTOM | wx.EXPAND, scale(5))
        hSizer_row2.Add(col5, 1, wx.EXPAND)

        bSizer1.AddSpacer(scale(15))
        bSizer1.Add(hSizer_row2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        bSizer1.AddSpacer(scale(15))

        # === Virtual IP / Server IP:Port / Network Password ===
        hSizer_row1 = wx.BoxSizer(wx.HORIZONTAL)

        col1 = wx.BoxSizer(wx.VERTICAL)
        self.m_staticText8 = wx.StaticText(panel, wx.ID_ANY, _("Virtual IP"), style=wx.ALIGN_LEFT)
        self.m_staticText8.Wrap(-1)
        col1.Add(self.m_staticText8, 0, wx.TOP, scale(5))
        self.VirtualIP = wx.TextCtrl(panel, wx.ID_ANY, wx.EmptyString, style=0)
        col1.Add(self.VirtualIP, 1, wx.TOP | wx.BOTTOM | wx.EXPAND, scale(5))
        hSizer_row1.Add(col1, 1, wx.EXPAND | wx.RIGHT, HORIZONTAL_GAP)

        col2 = wx.BoxSizer(wx.VERTICAL)
        self.m_staticText80 = wx.StaticText(panel, wx.ID_ANY, _("Server IP:Port"), style=wx.ALIGN_LEFT)
        self.m_staticText80.Wrap(-1)
        col2.Add(self.m_staticText80, 0, wx.TOP, scale(5))
        self.ServerIPPort = wx.TextCtrl(panel, wx.ID_ANY, wx.EmptyString, style=0)
        col2.Add(self.ServerIPPort, 1, wx.TOP | wx.BOTTOM | wx.EXPAND, scale(5))
        hSizer_row1.Add(col2, 1, wx.EXPAND | wx.RIGHT, HORIZONTAL_GAP)

        col3 = wx.BoxSizer(wx.VERTICAL)
        self.m_staticText15 = wx.StaticText(panel, wx.ID_ANY, _("Network Password"), style=wx.ALIGN_LEFT)
        self.m_staticText15.Wrap(-1)
        col3.Add(self.m_staticText15, 0, wx.TOP, scale(5))
        self.Network_Password = wx.TextCtrl(panel, wx.ID_ANY, wx.EmptyString, style=0)
        col3.Add(self.Network_Password, 1, wx.TOP | wx.BOTTOM | wx.EXPAND, scale(5))
        hSizer_row1.Add(col3, 1, wx.EXPAND)

        bSizer1.Add(hSizer_row1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        bSizer1.AddSpacer(scale(15))

        # === Focus handlers for labels ===
        def make_focus_handler(static_text, is_focus):
            def handler(event):
                if is_focus:
                    static_text.SetForegroundColour(wx.BLUE)
                else:
                    static_text.SetForegroundColour(wx.BLACK)
                static_text.Refresh()
                event.Skip()
            return handler

        self.Protocol.Bind(wx.EVT_SET_FOCUS, make_focus_handler(self.m_staticText10, True))
        self.Protocol.Bind(wx.EVT_KILL_FOCUS, make_focus_handler(self.m_staticText10, False))
        self.Compression.Bind(wx.EVT_SET_FOCUS, make_focus_handler(self.m_staticText11, True))
        self.Compression.Bind(wx.EVT_KILL_FOCUS, make_focus_handler(self.m_staticText11, False))

        # === Checkboxes ===
        class FocusableCheckBox(wx.CheckBox):
            def __init__(self, parent, label, pos=wx.DefaultPosition, size=wx.DefaultSize, style=0):
                super().__init__(parent, label=label, pos=pos, size=size, style=style)
                self.Bind(wx.EVT_SET_FOCUS, self.on_focus)
                self.Bind(wx.EVT_KILL_FOCUS, self.on_kill_focus)

            def on_focus(self, event):
                self.SetForegroundColour(wx.BLUE)
                self.Refresh()
                event.Skip()

            def on_kill_focus(self, event):
                self.SetForegroundColour(wx.NullColour)
                self.Refresh()
                event.Skip()

        gSizer4 = wx.BoxSizer(wx.HORIZONTAL)
        gSizer4.AddStretchSpacer(1)

        self.auto_start = FocusableCheckBox(panel, _("Auto Start"))
        self.auto_start.SetValue(True)
        self.auto_start.Bind(wx.EVT_CHECKBOX, self.on_auto_start_changed)
        gSizer4.Add(self.auto_start, 0, wx.ALL, scale(5))

        self.Notification = FocusableCheckBox(panel, _("Show Notification"))
        self.Notification.SetValue(True)
        self.Notification.Bind(wx.EVT_CHECKBOX, self.on_notification_changed)
        gSizer4.Add(self.Notification, 0, wx.ALL, scale(5))

        gSizer4.AddStretchSpacer(1)
        bSizer1.Add(gSizer4, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        # === More Options Section ===
        self.m_staticline6 = wx.StaticLine(panel, wx.ID_ANY, style=wx.LI_HORIZONTAL)
        bSizer1.Add(self.m_staticline6, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        self.m_staticText12 = wx.StaticText(panel, wx.ID_ANY, _("More Options"), style=wx.ALIGN_CENTRE)
        self.m_staticText12.Wrap(-1)
        bSizer1.Add(self.m_staticText12, 0, wx.TOP | wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, scale(5))

        self.Advanced = wx.Button(panel, wx.ID_ANY, _("Manually edit yaml file setting for VNT (Advanced User Only) ..."), style=wx.BU_EXACTFIT)
        self.Advanced.Enabled = False
        bSizer1.Add(self.Advanced, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        bSizer1.AddSpacer(scale(5))

        self.m_staticline3 = wx.StaticLine(panel, wx.ID_ANY, style=wx.LI_HORIZONTAL)
        bSizer1.Add(self.m_staticline3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        # === Help Message ===
        self.help_msg = wx.StaticText(panel, wx.ID_ANY, _("Set up your connection ..."), style=wx.ALIGN_CENTER)
        initial_width = scale(600)
        self.help_msg.SetMinSize((initial_width, -1))
        self.help_msg.Wrap(initial_width)
        bSizer1.Add(self.help_msg, 0, wx.TOP | wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, scale(5))

        self.m_staticline7 = wx.StaticLine(panel, wx.ID_ANY, style=wx.LI_HORIZONTAL)
        bSizer1.Add(self.m_staticline7, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        bSizer1.AddSpacer(scale(5))

        # === Bottom Buttons ===
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        button_sizer.AddStretchSpacer(1)
        BUTTON_WIDTH = scale(150)

        self.m_sdbSizer1OK = wx.Button(panel, wx.ID_OK, label=_("Save && Connect"))
        self.m_sdbSizer1OK.SetMinSize((BUTTON_WIDTH, -1))
        self.m_sdbSizer1OK.Enabled = False
        button_sizer.Add(self.m_sdbSizer1OK, 0, wx.ALL, scale(5))

        self.m_sdbSizer1Cancel = wx.Button(panel, wx.ID_CANCEL, label=_("Cancel"))
        self.m_sdbSizer1Cancel.SetMinSize((BUTTON_WIDTH, -1))
        button_sizer.Add(self.m_sdbSizer1Cancel, 0, wx.ALL, scale(5))

        self.m_exit_button = wx.Button(panel, wx.ID_ANY, label=_("Exit VNT Helper"))
        self.m_exit_button.SetMinSize((BUTTON_WIDTH, -1))
        button_sizer.Add(self.m_exit_button, 0, wx.ALL, scale(5))

        button_sizer.AddStretchSpacer(1)
        bSizer1.Add(button_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, LEFT_MARGIN)

        panel.SetSizer(bSizer1)
        panel.Layout()

        frame_sizer = wx.BoxSizer(wx.VERTICAL)
        frame_sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(frame_sizer)

        #  ==== GUI Elements Initialization ====
        if not vnt_app.args.no_gui:
            self.taskBarIcon = VNT_TaskBar_Icon(self, vnt_app)

        self.vnt_info = VNT_Information_Window(self, vnt_app)
        self.vnt_update_window = VNT_Update_Window(self, vnt_app, fresh_start_after_update)
        self.vnt_log_win = VNT_Log_Window(self, vnt_app)
        self.vnt_yaml_editor = None
        #  ====================================

        self.Centre(wx.BOTH)

        # === Event Bindings ===
        self.Bind(wx.EVT_SHOW, self.on_activate_loadsetting)

        self.ConfigName.Bind(wx.EVT_TEXT, self.on_text_config_name)
        self.ConfigName.Bind(wx.EVT_SET_FOCUS, self.on_text_config_name)
        self.m_select_button.Bind(wx.EVT_BUTTON, self.on_button_select)

        self.Token.Bind(wx.EVT_SET_FOCUS, self.help_token)
        self.Token.Bind(wx.EVT_TEXT, self.on_text_token)
        self.DeviceID.Bind(wx.EVT_SET_FOCUS, self.help_ID)
        self.DeviceID.Bind(wx.EVT_TEXT, self.on_text_deviceI_ID)
        self.VirtualIP.Bind(wx.EVT_SET_FOCUS, self.help_IP)
        self.VirtualIP.Bind(wx.EVT_TEXT, self._is_valid_virtual_IP)
        self.ServerIPPort.Bind(wx.EVT_SET_FOCUS, self.help_server_port)
        self.ServerIPPort.Bind(wx.EVT_TEXT, self._is_valid_server_port)
        self.Network_Password.Bind(wx.EVT_SET_FOCUS, self.help_password)
        self.Network_Password.Bind(wx.EVT_TEXT, self.on_text_password)

        self.Compression.Bind(wx.EVT_CHOICE, self.on_compression_change)
        self.Protocol.Bind(wx.EVT_CHOICE, self.on_protocol_change)

        self.auto_start.Bind(wx.EVT_CHECKBOX, self.on_auto_start_changed)
        self.Notification.Bind(wx.EVT_CHECKBOX, self.on_notification_changed)

        self.Advanced.Bind(wx.EVT_BUTTON, self._advanced_yaml_edit)
        self.m_sdbSizer1OK.Bind(wx.EVT_BUTTON, self.on_button_ok)
        self.m_sdbSizer1Cancel.Bind(wx.EVT_BUTTON, self.on_button_cancel)
        self.m_exit_button.Bind(wx.EVT_BUTTON, self.on_exit)

        panel.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

        # === Tab Order ===
        self.m_select_button.MoveAfterInTabOrder(self.ConfigName)
        self.Token.MoveAfterInTabOrder(self.m_select_button)
        self.DeviceID.MoveAfterInTabOrder(self.Token)
        self.Protocol.MoveAfterInTabOrder(self.DeviceID)
        self.Compression.MoveAfterInTabOrder(self.Protocol)
        self.VirtualIP.MoveAfterInTabOrder(self.Compression)
        self.ServerIPPort.MoveAfterInTabOrder(self.VirtualIP)
        self.Network_Password.MoveAfterInTabOrder(self.ServerIPPort)
        self.auto_start.MoveAfterInTabOrder(self.Network_Password)

        self.Advanced.MoveAfterInTabOrder(self.Notification)
        self.m_sdbSizer1OK.MoveAfterInTabOrder(self.Advanced)
        self.m_sdbSizer1Cancel.MoveAfterInTabOrder(self.m_sdbSizer1OK)
        self.m_exit_button.MoveAfterInTabOrder(self.m_sdbSizer1Cancel)

        # === Instance Variables ===
        self.good_config_name = False
        self.good_virtual_IP = False
        self.good_server_domainport = False
        self.config_fn = vnt_app.config_fn
        self.workingdir = vnt_app.workingdir
        self.logger = vnt_app.logger
        self.res_path = vnt_app._resource_path("res")
        self.vnt_app = vnt_app

    def refresh_ui(self):
        print("Refresh all UI text for i18n.")

        self.SetTitle(_("VNT Setting"))

        self.m_staticText1.SetLabel(_("Configuration Name"))
        self.m_staticText4.SetLabel(_("Basic Parameters"))
        self.m_staticText5.SetLabel(_("Token"))
        self.m_staticText7.SetLabel(_("Device ID"))
        self.m_staticText8.SetLabel(_("Virtual IP"))
        self.m_staticText80.SetLabel(_("Server IP:Port"))
        self.m_staticText15.SetLabel(_("Network Password"))
        self.m_staticText10.SetLabel(_("Protocol"))
        self.m_staticText11.SetLabel(_("Compression"))
        self.m_staticText12.SetLabel(_("More Options"))

        # Refresh choice options (must reassign to update display)
        self.Protocol.SetItems([_("QUIC"), _("TCP"), _("WSS"), _("DYNAMIC")])
        self.Compression.SetItems([_("none"), _("lz4"), _("zstd")])

        self.auto_start.SetLabel(_("Auto Start"))
        self.Notification.SetLabel(_("Show Notification"))

        self.Advanced.SetLabel(_("Manually edit yaml file setting for VNT (Advanced User Only) ..."))

        self.m_sdbSizer1OK.SetLabel(_("Save && Connect"))
        self.m_sdbSizer1Cancel.SetLabel(_("Cancel"))
        self.m_exit_button.SetLabel(_("Exit VNT Helper"))

        # Refresh help message placeholder
        self.help_msg.SetLabel(_("Set up your connection ..."))
        self._load_settings_to_main_window()
        self.panel.Layout()
        self.Refresh()

    def __del__(self):
        pass

    def on_char_hook(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.Hide()
        else:
            event.Skip()

    def on_activate_loadsetting(self, event):
        print("VNT Main Window activated, loading settings...")
        self._load_settings_to_main_window()
        self._can_save()
        event.Skip()

    def on_text_deviceI_ID(self, event):
        self._can_save()
        self.Refresh()
        event.Skip()

    def on_text_token(self, event):
        self._can_save()
        self.Refresh()
        event.Skip()

    def on_text_password(self, event):
        pwd = self.Network_Password.GetValue()
        if len(pwd) < 8:
            self.help_msg.SetForegroundColour("BLACK")
            self.help_msg.Label = _("Default AES128-GCM encryption when password length < 8, or your choice of E2E encryption")
        else:
            self.help_msg.SetForegroundColour("BLACK")
            self.help_msg.Label = _("Default AES256-GCM encryption when password length >= 8, or your choice of E2E encryption")

    def on_text_config_name(self, event):

        def Is_Valid_Windows_Filename(filename):
            if not filename:
                return False

            if filename.startswith(' ') or filename.endswith(' '):
                return False
            if filename.startswith('.') or filename.endswith('.'):
                return False

            invalid_chars = r'[<>:"/\\|?*]'
            if re.search(invalid_chars, filename):
                return False

            reserved_names = {
                "CON", "PRN", "AUX", "NUL",
                "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
            }
            name, _, ext = filename.partition('.')
            if name.upper() in reserved_names:
                return False

            if len(filename) > 260:
                return False

            return True

        t = self.ConfigName.GetValue()

        if t is not None and t != "" and Is_Valid_Windows_Filename(t) and t.lower() != self.config_fn:
            self.help_msg.SetForegroundColour("BLACK")
            self.help_msg.Label = _("Valid profile name: %s ") % t
            self.good_config_name = True
        else:
            self.help_msg.SetForegroundColour("RED")
            self.help_msg.Label = _("Invalid profile name: %s ") % t
            self.good_config_name = False

        self._can_save()
        self.Refresh()
        event.Skip()

    def on_client_encryption_change(self, event):
        self.help_msg.SetForegroundColour("RED")
        self.help_msg.Label = _("Nodes with different encryption CANNOT connect!")
        self.Refresh()
        m = _("1. All nodes must use the same encryption method.\n2. Encryption method is NONE if password is empty.\n3. Default is AES_GCM.")
        win32api.MessageBox(0, m, _("Information on Client Encryption"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)

        event.Skip()

    def on_compression_change(self, event):
        self.help_msg.SetForegroundColour("BLACK")
        self.help_msg.Label = _("LZ4 is recommended. ZSTD needs compile with --features zstd")
        self.Refresh()
        event.Skip()

    def on_compression_change(self, event):
        self.help_msg.SetForegroundColour("BLACK")
        self.help_msg.Label = _("LZ4 is recommended. ZSTD needs compile with --features zstd")
        self.Refresh()
        event.Skip()

    def on_protocol_change(self, event):
        selected_protocol = self.Protocol.GetStringSelection()
        if selected_protocol == "WSS":
            self.help_msg.SetForegroundColour("RED")
            self.help_msg.Label = _("WSS protocol requires server to use reverse proxy with valid SSL certificate")
        elif selected_protocol == "DYNAMIC":
            self.help_msg.SetForegroundColour("BLACK")
            self.help_msg.Label = _("DYNAMIC protocol uses DNS TXT record for server address resolution")
        elif selected_protocol == "QUIC":
            self.help_msg.SetForegroundColour("BLACK")
            self.help_msg.Label = _("QUIC protocol selected (optimized UDP transport)")
        else:
            self.help_msg.SetForegroundColour("BLACK")
            self.help_msg.Label = _("%s protocol selected") % selected_protocol
        self.Refresh()
        event.Skip()

    def on_auto_start_changed(self, event):
        is_checked = self.auto_start.GetValue()
        self.help_msg.SetForegroundColour("BLACK")
        if is_checked:
            self.help_msg.Label = _("Will apply autoboot via registry and task scheduler")
        else:
            self.help_msg.Label = _("Auto start disabled")
        self.Refresh()
        event.Skip()

    def on_notification_changed(self, event):
        is_checked = self.Notification.GetValue()
        self.help_msg.SetForegroundColour("BLACK")
        if is_checked:
            self.help_msg.Label = _("Bubble Message notifications are enabled")
        else:
            self.help_msg.Label = _("Bubble Message notifications are disabled")
        self.Refresh()
        event.Skip()

    def on_button_cancel(self, event):
        self.Hide()
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        previous_conf = vnt_conf.get_value(VNT_Config.KEY_VNT_PREV_PROFILE)
        if previous_conf is not None and previous_conf != '':
            vnt_conf.set_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML, previous_conf)
            vnt_conf.set_value(VNT_Config.KEY_VNT_PREV_PROFILE, '')
            dlg = VNT_ManageProfile_Frame(self.vnt_app.main_window, self.vnt_app)
            if dlg.ShowModal() == wx.ID_OK:
                selected = dlg.get_selected_items()
                print("Selected profiles:", selected)
            dlg.Destroy()

        event.Skip()

    def on_button_ok(self, event):
        global VNT_CLIENT_NAME, WIN_TUNE_DLL

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        previous_conf = vnt_conf.get_value(VNT_Config.KEY_VNT_PREV_PROFILE)
        if previous_conf is not None and previous_conf != '':
            vnt_conf.set_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML, previous_conf)
            vnt_conf.set_value(VNT_Config.KEY_VNT_PREV_PROFILE, '')

        t = _("Are you sure to \n(1) OVERWRITE the settings\n(2) RECONNECT to virtual network?")

        if win32api.MessageBox(0, t, _("Confirmation"), win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL) != win32con.IDYES:
            return

        vnt_conf.set_value(VNT_Config.KEY_VNT_NOTIFICATION_ENABLED, self.Notification.GetValue())

    def on_button_cancel(self, event):
        self.Hide()
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        previous_conf = vnt_conf.get_value(VNT_Config.KEY_VNT_PREV_PROFILE)
        if previous_conf is not None and previous_conf != '':
            vnt_conf.set_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML, previous_conf)
            vnt_conf.set_value(VNT_Config.KEY_VNT_PREV_PROFILE, '')
            dlg = VNT_ManageProfile_Frame(self.vnt_app.main_window, self.vnt_app)
            if dlg.ShowModal() == wx.ID_OK:
                selected = dlg.get_selected_items()
                print("Selected profiles:", selected)
            dlg.Destroy()

        event.Skip()

    def on_button_select(self, event):
        self.Hide()
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        previous_conf = vnt_conf.get_value(VNT_Config.KEY_VNT_PREV_PROFILE)
        if previous_conf is not None and previous_conf != '':
            vnt_conf.set_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML, previous_conf)
            vnt_conf.set_value(VNT_Config.KEY_VNT_PREV_PROFILE, '')

        dlg = VNT_ManageProfile_Frame(self.vnt_app.main_window, self.vnt_app)
        if dlg.ShowModal() == wx.ID_OK:
            selected = dlg.get_selected_items()
            print("Selected profiles:", selected)
        dlg.Destroy()
        self.Show()
        event.Skip()

    def on_button_ok(self, event):
        global VNT_CLIENT_NAME, WIN_TUNE_DLL

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        previous_conf = vnt_conf.get_value(VNT_Config.KEY_VNT_PREV_PROFILE)
        if previous_conf is not None and previous_conf != '':
            vnt_conf.set_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML, previous_conf)
            vnt_conf.set_value(VNT_Config.KEY_VNT_PREV_PROFILE, '')

        t = _("Are you sure to \n(1) OVERWRITE the settings\n(2) RECONNECT to virtual network?")

        if win32api.MessageBox(0, t, _("Confirmation"), win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL) != win32con.IDYES:
            return

        vnt_conf.set_value(VNT_Config.KEY_VNT_NOTIFICATION_ENABLED, self.Notification.GetValue())

        if self.auto_start.GetValue() is True:
            self.vnt_app.reg_task_autorun.add_autorun()
        else:
            self.vnt_app.reg_task_autorun.remove_autorun()

        fn = os.path.join(self.workingdir, self.ConfigName.GetValue() + ".yaml")

        if not vnt_conf.set_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML, fn):
            self.logger.write(f"Error writing {self.config_fn}", "critical")
            win32api.MessageBox(0, _("Error Updating VNT config"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
            return

        if self._write_vnt_connection_config():
            self.logger.write(f"Connection profile {self.config_fn} established")

            if VNT_ManageProfile_Frame.update_profile_list(os.path.join(self.workingdir, self.config_fn), VNT_Config.KEY_VNT_PROFILE_LIST, self.ConfigName.GetValue(), 'add'):
                self.logger.write(f"Profile list updated with {self.ConfigName.GetValue()}")
            else:
                self.logger.write(f"Error updating profile list with {self.ConfigName.GetValue()}, probably already exists", "debug")

            if self.vnt_app.vnt_connection.is_toggled_off():
                self.vnt_app.bubble_msg_handler.msg("Attention#VNT connection currently toggled off")
        else:
            win32api.MessageBox(0, _("Error establishing connection profile"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
            return

        self.Hide()

        if self.vnt_app.vnt_connection.is_running():
            self.vnt_app.vnt_connection.restart_vnt_network()
            time.sleep(2)

        if not self.vnt_update_window._update_initialize_parameters():
            self.vnt_update_window.Show(True)

        event.Skip()

    def help_token(self, event):
        self.help_msg.Label = _("Token needs to be registered in your server. Nodes with same token form a virtual LAN.")
        event.Skip()

    def help_ID(self, event):
        self.help_msg.Label = _("Assign an ID for your machine. Cannot be the same with other nodes.")
        event.Skip()

    def help_IP(self, event):
        self.help_msg.Label = _("Assign an IP for your machine. Server will assign one if left blank")
        event.Skip()

    def help_server_port(self, event):
        self.help_msg.Label = _("FORMAT: [Server Name or IP:PORT] Example: vnt.wherewego.top:29872")
        event.Skip()

    def help_password(self, event):
        self.help_msg.SetForegroundColour("BLACK")
        self.help_msg.Label = _("Password for end-to-end encryption, NOT for connection with server. Leave blank for no encryption.")
        event.Skip()

    def _advanced_yaml_edit(self, event):

        fn = os.path.join(self.workingdir, self.ConfigName.GetValue() + ".yaml")

        t = "Connection profile established before advanced editing" if self._write_vnt_connection_config() else "Error establishing connection profile before advanced editing"
        self.logger.write(t)

        self.vnt_yaml_editor = VNT_YamlConfigEditor_Window(self, fn)

        try:
            self.vnt_yaml_editor.ShowModal()
            VNT_Main_Window.set_window_topmost(self.vnt_yaml_editor)
            self.vnt_yaml_editor.Destroy()
        except Exception as e:
            self.logger.write(f"yaml editor error {e}", 'critical')
            return
        self._load_settings_to_main_window()
        event.Skip()

    def _can_save(self):
        can_save = self.good_config_name and self.good_virtual_IP and self.good_server_domainport
        can_save = can_save and self.Token.GetValue() is not None and self.DeviceID.GetValue() is not None
        can_save = can_save and self.Token.GetValue() != '' and self.DeviceID.GetValue() != ''
        if can_save:
            self.m_sdbSizer1OK.Enabled = True
        else:
            self.m_sdbSizer1OK.Enabled = False

    def _write_vnt_connection_config(self):
        global VNT_CONFIG_TEMPLATE_FILE

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)

        fn = vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)
        if fn is None:
            return False
        elif not os.path.exists(fn):
            # 新配置文件，从模板加载
            data = VNT_Config(self.workingdir, VNT_CONFIG_TEMPLATE_FILE, self.logger).get_data()
            if data is None:
                data = {}
        else:
            # 现有配置文件，完全重建为 VNT2 格式（清除所有旧字段）
            data = {}

        # VNT2 协议映射：UI选择 -> YAML存储格式（严格按照文档）
        # UI显示: QUIC, TCP, WSS, DYNAMIC -> YAML存储: quic://, tcp://, wss://, dynamic://
        protocol_mapping = {
            'quic': 'quic://',
            'tcp': 'tcp://',
            'wss': 'wss://',
            'dynamic': 'dynamic://'
        }
        
        selected_protocol = self.Protocol.GetString(self.Protocol.GetSelection()).lower()
        server_prefix = protocol_mapping.get(selected_protocol, 'quic://')

        # VNT2 核心字段（覆盖所有旧字段）
        data['network_code'] = self.Token.GetValue()
        data['device_id'] = self.DeviceID.GetValue()
        data['device_name'] = self.DeviceID.GetValue()
        data['server'] = server_prefix + self.ServerIPPort.GetValue()
        data['password'] = self.Network_Password.GetValue()

        # IP 地址（可选）
        ip_txt = self.VirtualIP.GetValue()
        if ip_txt is not None and ip_txt != "" and Internet_Connectivity_Monitor.is_valid_IP(ip_txt):
            data['ip'] = ip_txt
        else:
            self.vnt_app.bubble_msg_handler.msg(_("Error#Invalid IP %s, Server assigns IP") % ip_txt)

        # VNT2 compress 字段为布尔值
        compression_method = self.Compression.GetString(self.Compression.GetSelection()).lower()
        data['compress'] = (compression_method == 'lz4')

        # VNT2 安全配置：cert_mode 由编辑器管理，这里不设置默认值
        # 如果 YAML 文件中不存在 cert_mode，vnt_daemon 转换时会使用默认值

        connction_conf = VNT_Config(self.workingdir, os.path.basename(fn), self.logger)
        self.logger.write(f"Connection file to write {fn}")
        self.logger.write(f"VNT2 config data keys: {list(data.keys())}")
        return connction_conf.set_data(data)

    def _load_settings_to_main_window(self):

        if not os.path.exists(os.path.join(self.workingdir, self.config_fn)):
            self.vnt_app._deploy_resource_files([self.config_fn])
            self.Advanced.Enabled = False
            return

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        connection_config_fn = vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)
        if connection_config_fn is None or not os.path.exists(connection_config_fn) or connection_config_fn == "":
            self.ConfigName.SetValue('')
            self.Token.SetValue('')
            self.DeviceID.SetValue('')
            self.ServerIPPort.SetValue('')
            self.VirtualIP.SetValue('')
            self.Network_Password.SetValue('')
            self.Protocol.SetSelection(0)
            self.Compression.SetSelection(0)
            self.auto_start.SetValue(True)
            self.Notification.SetValue(True)
            self.Advanced.Enabled = False
            return
        else:
            self.Advanced.Enabled = True

        connection_config_fn_base_nm = os.path.splitext(os.path.basename(connection_config_fn))[0]
        self.ConfigName.SetValue(connection_config_fn_base_nm)

        self.auto_start.SetValue(self.vnt_app.reg_task_autorun.is_autorun_on())

        if vnt_conf.get_value(VNT_Config.KEY_VNT_NOTIFICATION_ENABLED) is not None:
            self.Notification.SetValue(vnt_conf.get_value(VNT_Config.KEY_VNT_NOTIFICATION_ENABLED))
        else:
            self.Notification.SetValue(True)

        connection_conf = VNT_Config(self.workingdir, os.path.basename(connection_config_fn), self.logger)
        data = connection_conf.get_data()
        if data is None:
            return

        try:
            # VNT2 字段：network_code（优先）或 token（向后兼容）
            network_code = data.get('network_code') or data.get('token', '')
            self.Token.SetValue(network_code)
            
            self.DeviceID.SetValue(data.get('device_id', ''))
            self.Network_Password.SetValue(data.get('password', ''))
            
            # VNT2 server 格式可能是字符串或数组（TOML要求数组，YAML可以是字符串）
            # 向后兼容：如果没有 server 字段，尝试从 server_address 读取
            server_data = data.get('server') or data.get('server_address', '')
            
            # 如果 server 是数组，取第一个元素
            if isinstance(server_data, list):
                server_address = server_data[0] if len(server_data) > 0 else ''
            else:
                server_address = server_data
            
            if isinstance(server_address, str) and '://' in server_address:
                service_address_port = server_address.split("://")[1].strip()
                protocol_prefix = server_address.split("://")[0].strip().lower()
                
                # VNT2 协议映射回 UI 显示（严格按照文档）
                # YAML存储: quic, tcp, wss, dynamic -> UI显示: QUIC, TCP, WSS, DYNAMIC
                protocol_mapping = {
                    'quic': 'QUIC',
                    'tcp': 'TCP',
                    'wss': 'WSS',
                    'dynamic': 'DYNAMIC'
                }
                ui_protocol = protocol_mapping.get(protocol_prefix, 'QUIC')
                
                index = self.Protocol.FindString(ui_protocol)
                if index == -1:
                    index = 0
                self.Protocol.SetSelection(index)
                
                self.ServerIPPort.SetValue(service_address_port)
            elif isinstance(server_address, str):
                # 没有协议前缀的情况
                self.ServerIPPort.SetValue(server_address)
                self.Protocol.SetSelection(0)
            else:
                # server_address 不是字符串的异常情况
                self.ServerIPPort.SetValue('')
                self.Protocol.SetSelection(0)
            
            self.VirtualIP.SetValue(data.get('ip', ''))
        except Exception as e:
            self.logger.write(f"Reading yaml: {e}", 'critical')

            if str(e) == "'ip'":
                self.VirtualIP.SetValue('')
            else:
                win32api.MessageBox(0, _("Error %s loading connection profile! Consider RESET") % str(e), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)

        try:
            # VNT2 compress 是布尔值，转换为 UI 显示
            compress_enabled = data.get("compress", False)
            compression_method = "lz4" if compress_enabled else "none"
            index = self.Compression.FindString(compression_method)
            if index == -1:
                index = 0
        except Exception:
            index = 0
        self.Compression.SetSelection(index)

    def _is_valid_virtual_IP(self, event):
        if Internet_Connectivity_Monitor.is_valid_IP(self.VirtualIP.GetValue()):
            self.help_msg.Label = _("Valid IP entered")
            self.help_msg.SetForegroundColour("BLACK")
            self.good_virtual_IP = True
        else:
            self.help_msg.Label = _("Invalid or Empty IP...")
            self.help_msg.SetForegroundColour("RED")
            self.good_virtual_IP = False

        self._can_save()
        self.Refresh()
        event.Skip()

    def _is_valid_server_port(self, event):
        if Internet_Connectivity_Monitor.is_valid_domain_port(self.ServerIPPort.GetValue()):
            self.help_msg.Label = _("Valid Domain:Port entered")
            self.help_msg.SetForegroundColour("BLACK")
            self.good_server_domainport = True
        else:
            self.help_msg.Label = _("Invalid or Empty Domain:Port ...")
            self.help_msg.SetForegroundColour("RED")
            self.good_server_domainport = False

        self._can_save()
        self.Refresh()
        event.Skip()

    @staticmethod
    def set_window_topmost(window):
        hwnd = window.GetHandle()
        if hwnd:
            ctypes.windll.user32.SetWindowPos(
                wintypes.HWND(hwnd),
                wintypes.HWND(-1),
                0, 0, 0, 0,
                0x0001 | 0x0002
            )

    def on_exit(self, event):
        hwnd = self.GetHandle()
        if win32api.MessageBox(hwnd, _("Are you sure to close VNT network and exit?"), _("Confirmation"),  win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL) == win32con.IDYES:
            self.vnt_app.stop()


class VNT_Update_Window(wx.Frame):
    update_package_fn = "vnt_helper.zip"
    version_control_fn = "version.yaml"
    updater_exe_nm = "vnt_updater.exe"
    updater_related_files = [update_package_fn, version_control_fn, updater_exe_nm]
    NOT_FOUND_INFO = "404 page not found"  # Check your web server settings, make sure it appears "not found" message in this text!!

    def __init__(self, parent, vnt_app, fresh_start_after_update=False):
        wx.Frame.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title=_(f"VNT Helper Version Update : {VNT_HELPER_VERSION}"),
            pos=wx.DefaultPosition,
            size=(700, 248),  # 逻辑尺寸，后续会缩放
            style=wx.CAPTION | wx.STATIC_BORDER,
        )

        # 立即应用 DPI 缩放（必须在 __init__ 之后！）
        self.SetSize(self.FromDIP((700, 248)))
        self.SetMinSize(self.FromDIP((700, 248)))  # 可选：设置最小尺寸

        # 其他 DPI 相关尺寸（现在可以安全使用 self.FromDIP）
        host_txt_size = self.FromDIP((140, -1))
        port_txt_size = self.FromDIP((60, -1))
        cycle_txt_size = self.FromDIP((60, -1))
        gauge_size = self.FromDIP((-1, 20))

        self.SetSizeHints(wx.DefaultSize, wx.DefaultSize)

        panel = wx.Panel(self, style=wx.TAB_TRAVERSAL)

        # 主垂直布局
        bSizer1 = wx.BoxSizer(wx.VERTICAL)

        # === 第一行：Update Mode Radio Buttons ===
        update_mode_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.auto_update_radio = wx.RadioButton(
            panel, wx.ID_ANY, _("Auto Update"), wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.manual_update_radio = wx.RadioButton(
            panel, wx.ID_ANY, _("Manual Update"), wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.disable_update_radio = wx.RadioButton(
            panel, wx.ID_ANY, _("Disable Update"), wx.DefaultPosition, wx.DefaultSize, 0
        )

        # Set Auto Update as default initially - will be overridden after instance vars are set
        self.auto_update_radio.SetValue(True)

        update_mode_sizer.Add(self.auto_update_radio, 0, wx.ALL, 5)
        update_mode_sizer.Add(self.manual_update_radio, 0, wx.ALL, 5)
        update_mode_sizer.Add(self.disable_update_radio, 0, wx.ALL, 5)
        bSizer1.Add(update_mode_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        # === 第二行：Server Address + URL | Update Cycle ===
        server_cycle_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.server_label = wx.StaticText(
            panel, wx.ID_ANY, _("Server Address:  "), wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.server_label.Wrap(-1)
        server_cycle_sizer.Add(self.server_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.m_staticText_http = wx.StaticText(
            panel, wx.ID_ANY, _("http://"), wx.DefaultPosition, wx.DefaultSize, 0
        )
        server_cycle_sizer.Add(self.m_staticText_http, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)

        self.version_host_txt = wx.TextCtrl(
            panel, wx.ID_ANY, _("IP or DOMAIN"), wx.DefaultPosition, host_txt_size, 0
        )
        server_cycle_sizer.Add(self.version_host_txt, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)

        self.m_staticText_colon = wx.StaticText(
            panel, wx.ID_ANY, _(":"), wx.DefaultPosition, wx.DefaultSize, 0
        )
        server_cycle_sizer.Add(self.m_staticText_colon, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)

        self.version_port_txt = wx.TextCtrl(
            panel, wx.ID_ANY, _("11061"), wx.DefaultPosition, port_txt_size, 0
        )
        server_cycle_sizer.Add(self.version_port_txt, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 15)

        separator = wx.StaticLine(panel, style=wx.LI_VERTICAL)
        server_cycle_sizer.Add(separator, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 5)

        self.m_staticText4 = wx.StaticText(
            panel, wx.ID_ANY, _("   Update Cycle (sec)"), wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.m_staticText4.Wrap(-1)
        server_cycle_sizer.Add(self.m_staticText4, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 5)

        self.cycle_time_txt = wx.TextCtrl(
            panel, wx.ID_ANY, _("60"), wx.DefaultPosition, cycle_txt_size, wx.TE_CENTER
        )
        server_cycle_sizer.Add(self.cycle_time_txt, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)

        bSizer1.Add(server_cycle_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 15)

        # 隐藏的固定路径
        self._fixed_version_path = f"/files/{self.version_control_fn}"

        # === Progress Section ===
        self.progress_box = wx.StaticBox(panel, wx.ID_ANY, _("Progress"))
        self.progress_sizer = wx.StaticBoxSizer(self.progress_box, wx.HORIZONTAL)

        self.update_progress = wx.Gauge(
            panel, wx.ID_ANY, 100, wx.DefaultPosition, gauge_size, wx.GA_HORIZONTAL
        )
        self.update_progress.SetValue(0)
        self.progress_sizer.Add(self.update_progress, 1, wx.EXPAND | wx.ALL, 5)

        bSizer1.Add(self.progress_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 25)

        # === Bottom Buttons ===
        gSizer6 = wx.GridSizer(1, 3, 8, 8)
        self.manually_check_update = wx.Button(
            panel, wx.ID_ANY, _("Check Update"), wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.update_OK = wx.Button(
            panel, wx.ID_ANY, _("Save Settings"), wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.update_Cancel = wx.Button(
            panel, wx.ID_ANY, _("Cancel"), wx.DefaultPosition, wx.DefaultSize, 0
        )

        gSizer6.Add(self.manually_check_update, 0, wx.EXPAND)
        gSizer6.Add(self.update_OK, 0, wx.EXPAND)
        gSizer6.Add(self.update_Cancel, 0, wx.EXPAND)

        bSizer1.Add(gSizer6, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # 应用 sizer
        panel.SetSizer(bSizer1)
        panel.Layout()

        frame_sizer = wx.BoxSizer(wx.VERTICAL)
        frame_sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(frame_sizer)
        self.Layout()
        self.Centre(wx.BOTH)

        # 设置 Tab 遍历顺序
        self.version_host_txt.MoveAfterInTabOrder(self.disable_update_radio)
        self.version_port_txt.MoveAfterInTabOrder(self.version_host_txt)
        self.cycle_time_txt.MoveAfterInTabOrder(self.version_port_txt)
        self.manually_check_update.MoveAfterInTabOrder(self.cycle_time_txt)
        self.update_OK.MoveAfterInTabOrder(self.manually_check_update)
        self.update_Cancel.MoveAfterInTabOrder(self.update_OK)

        self.auto_update_radio.SetFocus()

        # Bind Events
        self.Bind(wx.EVT_SHOW, self.on_show)
        self.update_Cancel.Bind(wx.EVT_BUTTON, self.on_button_cancel)
        self.update_OK.Bind(wx.EVT_BUTTON, self.on_button_ok)
        self.auto_update_radio.Bind(wx.EVT_RADIOBUTTON, self.on_radio_button)
        self.manual_update_radio.Bind(wx.EVT_RADIOBUTTON, self.on_radio_button)
        self.disable_update_radio.Bind(wx.EVT_RADIOBUTTON, self.on_radio_button)
        self.manually_check_update.Bind(wx.EVT_BUTTON, self.on_check_update)
        self.cycle_time_txt.Bind(wx.EVT_CHAR, self.on_key_press)
        self.cycle_time_txt.Bind(wx.EVT_TEXT, self.if_update_input_param_ok)
        self.version_host_txt.Bind(wx.EVT_TEXT, self.if_update_input_param_ok)
        self.version_port_txt.Bind(wx.EVT_TEXT, self.if_update_input_param_ok)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

        # Initialize instance variables
        self.update_package_url = None
        self.version_control_file_url = None
        self.update_check_cycle_sec = 60
        self.update_server_IP = None
        self.update_server_port = None
        self.version_control_server_IP = None
        self.version_control_server_port = None

        self.vnt_app = vnt_app
        self.logger = vnt_app.logger
        self.config_fn = vnt_app.config_fn
        self.workingdir = vnt_app.workingdir
        self.default_updatepackage_url = ''
        self.default_version_control_file_url = (
            'http://usr:passwd@server_ip:port/path_to/version.yaml'
        )
        self.vnt_update_daemon_exit_flag = threading.Event()
        self.vnt_update_cancel_flag = threading.Event()
        self.vnt_update_in_progress_flag = threading.Event()

        if fresh_start_after_update:
            self._fresh_start_actions()

        # Initialize the UI state based on saved configuration after instance variables are set
        self._initialize_state_from_config()

        # 只有在Auto Update被启用，或者没有设置任何更新模式（首次运行）的情况下才启动daemon
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        can_update = vnt_conf.get_value(VNT_Config.KEY_AUTO_UPDATE_ENABLED)
        update_disabled = vnt_conf.get_value(VNT_Config.KEY_UPDATE_DISABLED)

        # 如果AUTO_UPDATE_ENABLED为True，或者所有配置都是None/False（首次运行），则启动daemon
        if can_update or (can_update is None and update_disabled is None):
            self.vnt_update_daemon = threading.Thread(target=self._vnt_update_daemon, args=())
            self.vnt_update_daemon.daemon = True
            self.vnt_update_daemon.start()

    def __del__(self):
        pass

    def _initialize_state_from_config(self):
        """Initialize the UI state based on saved configuration values"""
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        can_update = vnt_conf.get_value(VNT_Config.KEY_AUTO_UPDATE_ENABLED)

        # Set radio button based on configuration
        if can_update is None or can_update is False:
            # Check if update is disabled completely
            update_disabled = vnt_conf.get_value(VNT_Config.KEY_UPDATE_DISABLED)
            if update_disabled:
                # Reset all radio buttons first to avoid conflicts
                self.auto_update_radio.SetValue(False)
                self.manual_update_radio.SetValue(False)
                self.disable_update_radio.SetValue(False)

                self.disable_update_radio.SetValue(True)
                # Don't call _disable_ui_elements() anymore, just set the appropriate state
                # Inputs should be enabled so user can save, but we'll disable them in on_show if needed
            else:
                # Default to manual update if not enabled
                # Reset all radio buttons first to avoid conflicts
                self.auto_update_radio.SetValue(False)
                self.manual_update_radio.SetValue(False)
                self.disable_update_radio.SetValue(False)

                self.manual_update_radio.SetValue(True)
                # Inputs should be enabled
            # Update UI based on current state
            wx.CallAfter(self.if_update_input_param_ok, None)
        else:
            # Auto update is enabled
            # Reset all radio buttons first to avoid conflicts
            self.auto_update_radio.SetValue(False)
            self.manual_update_radio.SetValue(False)
            self.disable_update_radio.SetValue(False)

            self.auto_update_radio.SetValue(True)
            # Inputs should be enabled
            # Update UI based on current state
            wx.CallAfter(self.if_update_input_param_ok, None)

    def refresh_ui(self):
        """
        刷新界面以应用新的语言设置。
        所有通过 _() 标记的字符串将被重新翻译。
        用户当前输入内容会被保留。
        """
        # 1. 保存用户当前输入（避免被默认值覆盖）
        current_host = self.version_host_txt.GetValue()
        current_port = self.version_port_txt.GetValue()
        current_cycle = self.cycle_time_txt.GetValue()

        # Determine which radio button was selected
        auto_update_selected = self.auto_update_radio.GetValue()
        manual_update_selected = self.manual_update_radio.GetValue()
        disable_update_selected = self.disable_update_radio.GetValue()

        # 2. 更新窗口标题
        self.SetTitle(_(f"VNT Helper Version Update : {VNT_HELPER_VERSION}"))

        # 3. 更新控件标签文本
        self.auto_update_radio.SetLabel(_("Auto Update"))
        self.manual_update_radio.SetLabel(_("Manual Update"))
        self.disable_update_radio.SetLabel(_("Disable Update"))

        self.server_label.SetLabel(_("Server Address:  "))
        self.m_staticText_http.SetLabel(_("http://"))
        self.m_staticText_colon.SetLabel(":")  # 冒号通常无需翻译，但保留一致性
        self.m_staticText4.SetLabel(_("   Update Cycle (sec)"))

        # 4. 更新按钮文本
        self.manually_check_update.SetLabel(_("Check Update"))
        self.update_OK.SetLabel(_("Save Settings"))
        self.update_Cancel.SetLabel(_("Cancel"))

        # 5. 更新 StaticBox 标题（关键！wx.StaticBox 需要 SetLabel + Refresh）
        self.progress_box.SetLabel(_("Progress"))
        self.progress_box.Refresh()  # 确保标题重绘

        # 6. 恢复用户输入内容（防止翻译时被重置为默认提示）
        self.version_host_txt.ChangeValue(current_host)
        self.version_port_txt.ChangeValue(current_port)
        self.cycle_time_txt.ChangeValue(current_cycle)

        # Restore radio button selections
        self.auto_update_radio.SetValue(auto_update_selected)
        self.manual_update_radio.SetValue(manual_update_selected)
        self.disable_update_radio.SetValue(disable_update_selected)

        # 7. 刷新布局（适应新文本长度）
        self.Layout()
        self.FitInside()  # 如果嵌套在 ScrolledWindow 中有用；否则可省略
        # 注意：不要调用 self.Fit()，否则窗口大小会变，破坏你设定的尺寸逻辑

        # 8. （可选）重新居中（如果窗口大小变化较大）
        self.Centre(wx.BOTH)

    def on_char_hook(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.Hide()
        else:
            event.Skip()  # 允许其他按键正常处理

    def on_key_press(self, event):
        keycode = event.GetKeyCode()

        if (keycode >= 48 and keycode <= 57) or keycode in [wx.WXK_BACK, wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_DELETE]:
            event.Skip()
        else:
            return

    def on_show(self, event):
        self.CenterOnScreen()

        # Use the same initialization logic as constructor to ensure consistency
        self._initialize_state_from_config()

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)

        t = vnt_conf.get_value(VNT_Config.KEY_VERSION_FILE_URL)
        if t is None:
            t = self.default_version_control_file_url

        # 解析 URL 提取 host 和 port
        try:
            parsed = urlparse(t)
            netloc = parsed.netloc
            if '@' in netloc:
                netloc = netloc.split('@', 1)[1]  # 去掉 usr:passwd@
            if ':' in netloc:
                host_part, port_part = netloc.rsplit(':', 1)
                port_val = port_part
            else:
                host_part = netloc
                port_val = "80"
            self.version_host_txt.SetValue(host_part or "server_ip_or_domain")
            self.version_port_txt.SetValue(port_val or "80")
        except Exception as e:
            self.logger.write(f"Failed to parse version URL: {e}", 'warning')
            self.version_host_txt.SetValue("server_ip_or_domain")
            self.version_port_txt.SetValue("80")

        cycle_time = vnt_conf.get_value(VNT_Config.KEY_UPDATE_CYCLE_SEC)
        if cycle_time is not None:
            try:
                # Ensure it's stored as integer, convert if necessary
                cycle_time_int = int(cycle_time)
                self.cycle_time_txt.SetValue(str(cycle_time_int))
            except (ValueError, TypeError):
                # If conversion fails, use default
                self.cycle_time_txt.SetValue('60')
        else:
            self.cycle_time_txt.SetValue('60')

        # 触发UI更新以反映当前状态
        wx.CallAfter(self.if_update_input_param_ok, None)

        event.Skip()

    def on_check_update(self, event):
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        # Temporarily set update enabled to False for manual check
        if not vnt_conf.set_value(VNT_Config.KEY_AUTO_UPDATE_ENABLED, False):
            self.logger.write(f"Error writing {self.config_fn}", 'critical')
            win32api.MessageBox(0, _("Error Updating VNT config"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
            return
        manual_update_thread = threading.Thread(target=self._update_manually_operation, args=())
        manual_update_thread.daemon = True
        manual_update_thread.start()

        event.Skip()

    def on_button_ok(self, event):
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)

        # Update the update enabled/disabled status based on radio button selection
        if self.disable_update_radio.GetValue():
            # Disable update completely
            auto_update_enabled = False
            update_disabled = True
        elif self.auto_update_radio.GetValue():
            # Enable auto update
            auto_update_enabled = True
            update_disabled = False
        else:
            # Manual update - not auto, but updates are not completely disabled
            auto_update_enabled = False
            update_disabled = False

        # Save the update settings
        write_success = vnt_conf.set_value(VNT_Config.KEY_AUTO_UPDATE_ENABLED, auto_update_enabled)
        write_success = write_success and vnt_conf.set_value(VNT_Config.KEY_UPDATE_DISABLED, update_disabled)

        # Save cycle time - convert to integer to avoid YAML quoting issues
        try:
            cycle_time_int = int(self.cycle_time_txt.GetValue())
            write_success = write_success and vnt_conf.set_value(VNT_Config.KEY_UPDATE_CYCLE_SEC, cycle_time_int)
        except ValueError:
            # If conversion fails, use default value
            write_success = write_success and vnt_conf.set_value(VNT_Config.KEY_UPDATE_CYCLE_SEC, 60)

        try:
            self.update_check_cycle_sec = int(self.cycle_time_txt.GetValue())
        except Exception:
            self.update_check_cycle_sec = 60
        finally:
            self.logger.write(f"Update cycle time {self.update_check_cycle_sec} seconds written to {self.config_fn}", 'info')

        # 拼接完整 URL
        host = self.version_host_txt.GetValue().strip()
        port = self.version_port_txt.GetValue().strip()
        full_url = f"http://{host}:{port}{self._fixed_version_path}"

        # 此时应已通过 if_update_input_param_ok 验证，但再保险一下
        if not (host and port.isdigit() and 1 <= int(port) <= 65535):
            win32api.MessageBox(0, _("Invalid Host or Port"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
            return

        write_success = write_success and vnt_conf.set_value(VNT_Config.KEY_VERSION_FILE_URL, full_url)

        if not write_success:
            win32api.MessageBox(0, _("Error saving the settings"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
            return

        if not self._update_initialize_parameters():
            win32api.MessageBox(0, _("Error Initialize Update Settings, check your URL or file name"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
            return

        # Handle daemon based on update mode
        if self.auto_update_radio.GetValue():
            # 自动更新打开 daemon （如果没有开）
            if not self.vnt_update_daemon.is_alive():
                self.vnt_update_daemon_exit_flag.clear()
                self.vnt_update_daemon = threading.Thread(target=self._vnt_update_daemon, args=())
                self.vnt_update_daemon.daemon = True
                self.vnt_update_daemon.start()
        elif self.manual_update_radio.GetValue():
            # 手动更新不打开 daemon （如果开了要关上）
            if self.vnt_update_daemon.is_alive():
                self.vnt_update_daemon_exit_flag.set()
        elif self.disable_update_radio.GetValue():
            # disable update 关闭 daemon（如果开了）
            self.vnt_update_daemon_exit_flag.set()

        # 根据不同的更新模式处理窗口行为
        if self.manual_update_radio.GetValue():
            # Manual Update模式下，窗口不隐藏，check update按钮可用
            self.manually_check_update.Enable(True)
        else:
            # Auto update和disable update模式下，check update按钮不可用，窗口隐藏
            self.manually_check_update.Enable(False)
            self.Hide()
        event.Skip()

    def on_radio_button(self, event):
        """Handle radio button selection"""

        if self.auto_update_radio.GetValue():
            # Auto Update selected
            # Enable UI elements
            self._enable_ui_elements()

            # Update button states
            self.manually_check_update.Enable(False)
            self.cycle_time_txt.Enable(True)

            # Save Settings按钮不应变灰，因为需要点击Save才能保存
            # 保持Save按钮可用以便用户保存更改
            # 重新评估按钮状态
            self.if_update_input_param_ok(None)

        elif self.manual_update_radio.GetValue():
            # Manual Update selected
            # Enable UI elements
            self._enable_ui_elements()

            # Update button states
            self.manually_check_update.Enable(True)
            self.cycle_time_txt.Enable(False)  # Manual Update模式下，周期输入框应该禁用

            # 重新评估按钮状态
            self.if_update_input_param_ok(None)

        elif self.disable_update_radio.GetValue():
            # Disable Update selected - UI elements remain enabled so user can save
            # Enable UI elements (don't disable like before)
            self._enable_ui_elements()

            # Update button states - keep Save Settings enabled
            self.manually_check_update.Enable(False)  # Check Update不可用
            self.cycle_time_txt.Enable(False)  # Disable Update模式下，周期输入框应该禁用

            # 重新评估按钮状态
            self.if_update_input_param_ok(None)

    def on_button_cancel(self, event):
        if self.vnt_update_in_progress_flag.is_set():
            self.vnt_update_cancel_flag.set()
        else:
            self.Hide()
        event.Skip()

    def if_update_input_param_ok(self, event):
        # 检查 cycle time
        cycle_text = self.cycle_time_txt.GetValue().strip()
        try:
            cycle_time_ok = cycle_text.isdigit() and (10 <= int(cycle_text) <= 86400)
        except (ValueError, AttributeError):
            cycle_time_ok = False

        # 检查 host（IP 或域名）
        host = self.version_host_txt.GetValue().strip()
        host_ok = False
        if host:
            try:
                # 尝试 IPv4
                validators.ip_address.ipv4(host)
                host_ok = True
            except validators.ValidationError:
                try:
                    # 尝试域名
                    validators.domain(host)
                    host_ok = True
                except validators.ValidationError:
                    host_ok = False

        # 检查 port
        port_str = self.version_port_txt.GetValue().strip()
        try:
            port_ok = port_str.isdigit() and (1 <= int(port_str) <= 65535)
        except (ValueError, AttributeError):
            port_ok = False

        # 整体有效？
        url_ok = host_ok and port_ok

        # 控制按钮状态
        enable_save = cycle_time_ok and url_ok

        # vnt_update_in_progress_flag 一旦为 True，除了 cancel 之外所有按钮都不可用
        if self.vnt_update_in_progress_flag.is_set():
            enable_save = False
            # 禁用相关按钮
            self.auto_update_radio.Enable(False)
            self.manual_update_radio.Enable(False)
            self.disable_update_radio.Enable(False)
            self.version_host_txt.Enable(False)
            self.version_port_txt.Enable(False)
            self.cycle_time_txt.Enable(False)
        else:
            # 如果不在更新过程中，启用按钮
            self.auto_update_radio.Enable(True)
            self.manual_update_radio.Enable(True)
            self.disable_update_radio.Enable(True)

            # 根据当前选中的单选按钮状态来设置输入框的启用状态
            if self.disable_update_radio.GetValue():
                # Disable Update被选中时，输入框应该禁用
                self.version_host_txt.Enable(False)
                self.version_port_txt.Enable(False)
                self.cycle_time_txt.Enable(False)
            elif self.manual_update_radio.GetValue():
                # Manual Update被选中时，cycle time输入框应该禁用
                self.version_host_txt.Enable(True)
                self.version_port_txt.Enable(True)
                self.cycle_time_txt.Enable(False)
            else:
                # Auto Update模式下，所有输入框启用
                self.version_host_txt.Enable(True)
                self.version_port_txt.Enable(True)
                self.cycle_time_txt.Enable(True)

        # 根据当前选中的单选按钮状态来设置check update按钮的启用状态
        if self.disable_update_radio.GetValue() or self.auto_update_radio.GetValue():
            # Disable Update或Auto Update模式下，check update按钮不可用
            self.manually_check_update.Enable(False)
        else:
            # Manual Update模式下，check update按钮可用
            self.manually_check_update.Enable(True)

        # 确保传入的是 bool 类型！
        self.update_OK.Enable(bool(enable_save))

    def _display_update_progress(self, progress, msg, speed_kbps=None):
        """更新进度条和状态标签，支持显示网速"""
        try:
            self.update_progress.SetValue(int(progress))  # 修正拼写
            if speed_kbps is not None:
                if speed_kbps >= 1024:
                    speed_str = f"{speed_kbps / 1024:.1f} MB/s"
                else:
                    speed_str = f"{speed_kbps:.1f} KB/s"
                label = f"{msg}   |   {speed_str}"
            else:
                label = msg
            self.progress_box.SetLabel(label)
        except Exception as e:
            self.logger.write(f"Update Progress Bar Error: {e}", 'critical')

    def _monitor_download_speed(self, size_getter, total_size, background_running):
        """后台线程：定期估算并更新下载速度"""
        last_bytes = 0
        last_time = time.time()
        while self.vnt_update_in_progress_flag.is_set():
            time.sleep(0.5)
            try:
                current_bytes = size_getter()
                now = time.time()
                elapsed = now - last_time
                if elapsed > 0.1 and current_bytes > last_bytes:
                    speed_kbps = ((current_bytes - last_bytes) / elapsed) / 1024  # KB/s
                    progress = min((current_bytes / total_size) * 100, 100.0) if total_size > 0 else 0
                    if not background_running:
                        wx.CallAfter(
                            self._display_update_progress,
                            int(progress),
                            f"Progress: {progress:.2f}%",
                            speed_kbps
                        )
                    last_bytes = current_bytes
                    last_time = now
            except Exception as e:
                self.logger.write(f"Speed monitor error: {e}", 'warning')
                break

    def _update_manually_operation(self):
        global VNT_CLIENT_NAME
        version_conf = VNT_Config(self.workingdir, self.version_control_fn, self.logger)
        try:
            if not self._update_initialize_parameters():
                print("Update parameters error")
                win32api.MessageBox(0, _("Update parameters error"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
                return

            if not Internet_Connectivity_Monitor.is_server_connected((self.version_control_server_IP, self.version_control_server_port)):
                self.logger.write("Version control server Not Connected", 'info')
                self.vnt_app.bubble_msg_handler.msg("Error#Version control server not connected")
                return

            if not os.path.exists(os.path.join(self.workingdir, self.update_package_fn)):
                ver_diff, allowed = self._update_version_check()
                if allowed and ver_diff:
                    if self._update_download():
                        if win32api.MessageBox(0, _("Update package download completed, update now?"), _("Confirmation"), win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL) == win32con.IDYES:
                            self._update_and_exit()
                    else:
                        win32api.MessageBox(0, _("Error in downloading new package"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
                else:
                    if not ver_diff:
                        win32api.MessageBox(0, _("No new version for an update found"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
                    else:
                        if not allowed:
                            win32api.MessageBox(0, _("Update temporarily not allowed for this IP"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
            else:
                checksum_remote = version_conf.get_value(VNT_Config.KEY_CHECKSUM)
                checksum_local = self.calculate_SHA256(os.path.join(self.workingdir, self.update_package_fn))

                if checksum_remote == checksum_local and (checksum_remote is not None and checksum_local is not None):
                    self.logger.write("Checksum: " + str(checksum_local))
                    if win32api.MessageBox(0, _("An update package is already downloaded, update now?"), _("Confirmation"), win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL) == win32con.IDYES:
                        self._update_and_exit()
                    else:
                        return
                else:
                    win32api.MessageBox(0, _("Downloaded package seems to be corrupted, will be deleted"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
                    os.remove(os.path.join(self.workingdir, self.update_package_fn))
                    self.logger.write("Checksum not same. Download file removed")
                    return
        except Exception as e:
            self.logger.write(f"Manually update {e}", 'critical')
            return

    def _update_and_exit(self):
        global RESOURCE_FILE_NAMES

        def run_vnt_updater(updater_nm, working_dir, res_path, zip_nm, exe_nm, file_nms, log_fn):
            nonlocal self
            self.vnt_app._deploy_resource_files([self.updater_exe_nm])
            fn_string = ",".join(file_nms)  # 更简洁的拼接

            if self.vnt_app.args.no_gui:
                cmd = [updater_nm, "-d", working_dir, "-r", res_path, "-n", fn_string, "-f", zip_nm, "-e", exe_nm, "-l", log_fn, "-b"]
            else:
                cmd = [updater_nm, "-d", working_dir, "-r", res_path, "-n", fn_string, "-f", zip_nm, "-e", exe_nm, "-l", log_fn]

            self.logger.write(f"Updater command : {cmd}")
            try:
                process = subprocess.Popen(cmd)
            except Exception as e:
                self.logger.write(f"Error starting updater: {e}", 'critical')
                return
            self.logger.write(f"Updater PID: {process.pid}")

        # Check if service is running and stop it before update
        self.vnt_app.update_process_started = True
        if self.vnt_app.is_service_installed():
            if self.vnt_app.get_service_status() == "RUNNING":
                resp = self.vnt_app.vnt_connection._send_ipc_command({"cmd": "exit"})
                if resp.get("status") != "daemon exits":
                    self.logger.write(f"Daemon probably already exited: {resp.get('msg')}")
                else:
                    self.logger.write("Daemon exit command replies with success...")
                self.logger.write("Stopping VNT daemon service before update...", 'info')
                self.vnt_app.stop_service()
            # Uninstall the old service
            self.logger.write("Uninstalling old VNT daemon service...", 'info')
            self.vnt_app.uninstall_service()

        run_vnt_updater(
            os.path.join(self.workingdir, self.updater_exe_nm),
            self.workingdir,
            self.vnt_app._resource_path(),
            self.update_package_fn,
            os.path.split(sys.argv[0])[1],
            RESOURCE_FILE_NAMES,
            self.logger.get_log_fn()
        )
        self.vnt_app.stop(True)

    def _estimate_bandwidth(self, url, test_size=256 * 1024, timeout=15):
        try:
            start_time = time.time()
            resp = requests.get(
                url,
                headers={"Range": f"bytes=0-{test_size - 1}", "X-Download-ID": "bandwidth-test-" + str(int(time.time()))},
                timeout=timeout,
                stream=True
            )
            if resp.status_code not in (200, 206):
                resp.raise_for_status()

            total_read = 0
            for chunk in resp.iter_content(chunk_size=32 * 1024):
                total_read += len(chunk)
                if total_read >= test_size:
                    break

            elapsed = time.time() - start_time
            if elapsed <= 0:
                return None
            bandwidth_kbps = (total_read / elapsed) / 1024
            self.logger.write(f"Estimated bandwidth: {bandwidth_kbps:.1f} KB/s ({bandwidth_kbps * 8:.1f} kbps)")
            return bandwidth_kbps
        except Exception as e:
            self.logger.write(f"Bandwidth estimation failed: {e}", 'warning')
            return None

    def _update_download(self, background_running=False):
        version_conf = VNT_Config(self.workingdir, self.version_control_fn, self.logger)
        download_id = str(uuid.uuid4())
        self.logger.write(f"Assigned Download-ID: {download_id}")

        def add_download_id_header(headers=None):
            if headers is None:
                headers = {}
            headers["X-Download-ID"] = download_id
            return headers

        try:
            self.logger.write(self.update_package_url)
            self.logger.write("Downloading update starts...")
            if not Internet_Connectivity_Monitor.is_server_connected((self.update_server_IP, self.update_server_port)):
                self.logger.write("Update server Not Connected")
                if not background_running:
                    self.vnt_app.bubble_msg_handler.msg("Error#Update server NOT Connected")
                return False

            # Step 1: 获取总文件大小
            total_size = None
            file_exists = True
            try:
                resp = requests.get(
                    self.update_package_url,
                    headers=add_download_id_header({"Range": "bytes=0-0"}),
                    timeout=(15, 60)
                )
                if resp.status_code == 206:
                    content_range = resp.headers.get("Content-Range", "")
                    if "/" in content_range:
                        total_size = int(content_range.split("/")[-1])
                elif resp.status_code == 200:
                    total_size = len(resp.content)
                    if total_size > 2 * 1024 * 1024:
                        self.logger.write("Warning: Server ignored Range request. Using single-thread mode.")
                        return self._single_thread_download(background_running, total_size, download_id=download_id)
                elif resp.status_code == 404:
                    file_exists = False
                else:
                    resp.raise_for_status()
            except requests.exceptions.RequestException as e:
                self.logger.write(f"Error probing file size: {e}", 'critical')
                if not background_running:
                    self.vnt_app.bubble_msg_handler.msg("Update#Failed to connect to update server")
                return False

            if total_size is None or total_size <= 1024 or not file_exists:
                try:
                    resp = requests.get(self.update_package_url, headers=add_download_id_header(), timeout=(15, 60))
                    if resp.status_code == 404 or self.NOT_FOUND_INFO in resp.text.strip():
                        self.logger.write("Update: Update Package not found")
                        if not background_running:
                            self.vnt_app.bubble_msg_handler.msg("Update#Update package not found")
                        return False
                    else:
                        total_size = len(resp.content)
                except Exception as e:
                    self.logger.write(f"File existence check failed: {e}", 'critical')
                    return False

            if total_size <= 0:
                self.logger.write("Invalid file size received")
                return False

            self.logger.write(f"Total update file size {total_size:,} bytes")

            # Step 2: 决定是否使用多线程
            if total_size < 2 * 1024 * 1024:
                return self._single_thread_download(background_running, total_size, download_id=download_id)

            bandwidth_kbps = self._estimate_bandwidth(self.update_package_url)
            if bandwidth_kbps is None:
                num_threads = 2
            else:
                if bandwidth_kbps < 128:
                    num_threads = 1
                elif bandwidth_kbps < 640:
                    num_threads = 2
                elif bandwidth_kbps < 1280:
                    num_threads = 3
                else:
                    num_threads = 4
            num_threads = min(max(num_threads, 1), 4)
            self.logger.write(f"Using {num_threads} download thread(s) based on estimated bandwidth")

            # Step 3: 多线程分段下载
            chunk_size = total_size // num_threads
            ranges = [(i * chunk_size, (i + 1) * chunk_size - 1 if i < num_threads - 1 else total_size - 1) for i in range(num_threads)]
            temp_files = [os.path.join(self.workingdir, f"{self.update_package_fn}.part{i}") for i in range(num_threads)]

            downloaded_size = 0
            parts_to_download = []
            download_lock = threading.Lock()

            for i, (start, end) in enumerate(ranges):
                expected_part_size = end - start + 1
                part_file = temp_files[i]
                if os.path.exists(part_file):
                    actual_size = os.path.getsize(part_file)
                    if actual_size == expected_part_size:
                        with download_lock:
                            downloaded_size += actual_size
                        self.logger.write(f"Part {i} already downloaded (size={actual_size})")
                        continue
                    else:
                        os.remove(part_file)
                        self.logger.write(f"Part {i} incomplete (expected {expected_part_size}, got {actual_size}), will re-download")
                parts_to_download.append((start, end, i))

            self.vnt_update_cancel_flag.clear()
            if not background_running:
                wx.CallAfter(self.auto_update_radio.Disable)
                wx.CallAfter(self.manual_update_radio.Disable)
                wx.CallAfter(self.disable_update_radio.Disable)
                wx.CallAfter(self.version_host_txt.Disable)
                wx.CallAfter(self.version_port_txt.Disable)
                wx.CallAfter(self.cycle_time_txt.Disable)
                wx.CallAfter(self.update_OK.Disable)
                wx.CallAfter(self.manually_check_update.Disable)
            self.vnt_update_in_progress_flag.set()

            # 启动速度监控线程（仅前台模式）
            speed_monitor = None
            if not background_running and parts_to_download:
                speed_monitor = threading.Thread(
                    target=self._monitor_download_speed,
                    args=(lambda: downloaded_size, total_size, background_running),
                    daemon=True
                )
                speed_monitor.start()

            def download_part(start, end, part_id, max_retries=3):
                nonlocal downloaded_size
                headers = add_download_id_header({'Range': f'bytes={start}-{end}'})
                temp_file = temp_files[part_id]
                expected_size = end - start + 1

                for attempt in range(max_retries + 1):
                    if self.vnt_update_cancel_flag.is_set():
                        return
                    try:
                        resp = requests.get(self.update_package_url, headers=headers, stream=True, timeout=(15, 90))
                        resp.raise_for_status()
                        with open(temp_file, 'wb') as f:
                            for chunk in resp.iter_content(chunk_size=64 * 1024):
                                if self.vnt_update_cancel_flag.is_set():
                                    return
                                if chunk:
                                    f.write(chunk)
                                    with download_lock:
                                        downloaded_size += len(chunk)
                        if os.path.getsize(temp_file) == expected_size:
                            return
                        else:
                            raise IOError(f"Size mismatch after download for part {part_id}")
                    except Exception as e:
                        self.logger.write(f"Part {part_id} attempt {attempt + 1}/{max_retries + 1} failed: {e}", 'warning')
                        if os.path.exists(temp_file):
                            try:
                                os.remove(temp_file)
                            except OSError:
                                pass
                        if attempt < max_retries and not self.vnt_update_cancel_flag.is_set():
                            time.sleep(2 ** attempt)
                        else:
                            self.logger.write(f"Part {part_id} permanently failed", 'error')
                            return

            if parts_to_download:
                with ThreadPoolExecutor(max_workers=num_threads) as executor:
                    futures = [executor.submit(download_part, start, end, i) for (start, end, i) in parts_to_download]
                    for future in as_completed(futures):
                        if self.vnt_update_cancel_flag.is_set():
                            break

            self.vnt_update_in_progress_flag.clear()
            # Trigger UI update to reflect current state after update finishes
            if not background_running:
                wx.CallAfter(self.if_update_input_param_ok, None)

            # Step 4: 取消处理
            if self.vnt_update_cancel_flag.is_set():
                for tf in temp_files:
                    if os.path.exists(tf):
                        os.remove(tf)
                self.logger.write("Downloaded partial package file removed")
                if not background_running:
                    wx.CallAfter(self._display_update_progress, 0, "Progress")
                    wx.CallAfter(self.if_update_input_param_ok, None)  # Update UI based on current state
                return False

            # Step 5: 合并文件
            final_path = os.path.join(self.workingdir, self.update_package_fn)
            try:
                with open(final_path, 'wb') as outfile:
                    for temp_file in temp_files:
                        if os.path.exists(temp_file):
                            with open(temp_file, 'rb') as infile:
                                outfile.write(infile.read())
                            os.remove(temp_file)
            except Exception as e:
                self.logger.write(f"Failed to merge parts: {e}", 'critical')
                for tf in temp_files:
                    if os.path.exists(tf):
                        try:
                            os.remove(tf)
                        except OSError:
                            pass
                if not background_running:
                    wx.CallAfter(self.auto_update_radio.Enable)
                    wx.CallAfter(self.manual_update_radio.Enable)
                    wx.CallAfter(self.disable_update_radio.Enable)
                    wx.CallAfter(self.version_host_txt.Enable)
                    wx.CallAfter(self.version_port_txt.Enable)
                    wx.CallAfter(self.cycle_time_txt.Enable)
                    wx.CallAfter(self.update_OK.Enable)
                    wx.CallAfter(self.manually_check_update.Enable)
                return False

            if not background_running:
                wx.CallAfter(self.if_update_input_param_ok, None)  # Update UI based on current state

            self.logger.write("Update File Finished Downloading")

            # Step 6: 校验和验证
            checksum_remote = version_conf.get_value(VNT_Config.KEY_CHECKSUM)
            checksum_local = self.calculate_SHA256(final_path)
            if checksum_remote and checksum_local and checksum_remote == checksum_local:
                self.logger.write(f"Checksum OK: {checksum_local}")
                self.vnt_app.bubble_msg_handler.msg("Update#Update package downloaded")
                return True
            else:
                os.remove(final_path)
                self.logger.write("Checksum mismatch. Download file removed")
                return False

        except Exception as e:
            self.logger.write(f"Downloading Update: {e}", 'critical')
            for i in range(4):
                tf = os.path.join(self.workingdir, f"{self.update_package_fn}.part{i}")
                if os.path.exists(tf):
                    try:
                        os.remove(tf)
                    except Exception:
                        pass
            # Re-enable UI elements in case of error
            if not background_running and self.vnt_update_in_progress_flag.is_set():
                self.vnt_update_in_progress_flag.clear()
                wx.CallAfter(self.if_update_input_param_ok, None)  # Update UI based on current state
            else:
                self.vnt_update_in_progress_flag.clear()
            return False

    def _single_thread_download(self, background_running, total_size, download_id=None):
        try:
            headers = {"X-Download-ID": download_id} if download_id else {}
            response = requests.get(self.update_package_url, headers=headers, stream=True, timeout=(10, 60))
            downloaded_size = 0
            self.vnt_update_cancel_flag.clear()

            if not background_running:
                wx.CallAfter(self.auto_update_radio.Disable)
                wx.CallAfter(self.manual_update_radio.Disable)
                wx.CallAfter(self.disable_update_radio.Disable)
                wx.CallAfter(self.version_host_txt.Disable)
                wx.CallAfter(self.version_port_txt.Disable)
                wx.CallAfter(self.cycle_time_txt.Disable)
                wx.CallAfter(self.update_OK.Disable)
                wx.CallAfter(self.manually_check_update.Disable)
            self.vnt_update_in_progress_flag.set()

            final_path = os.path.join(self.workingdir, self.update_package_fn)
            last_time = time.time()
            last_downloaded = 0

            with open(final_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)

                        # 每0.5秒计算一次速度
                        now = time.time()
                        if now - last_time >= 0.5:
                            delta = downloaded_size - last_downloaded
                            speed_kbps = (delta / (now - last_time)) / 1024
                            last_time = now
                            last_downloaded = downloaded_size
                        else:
                            speed_kbps = None

                        progress = min((downloaded_size / total_size) * 100, 100.0) if total_size > 0 else 0
                        if not background_running:
                            wx.CallAfter(
                                self._display_update_progress,
                                int(progress),
                                f"Progress: {progress:.2f}%",
                                speed_kbps
                            )

                        if self.vnt_update_cancel_flag.is_set():
                            if win32api.MessageBox(0, _("Stop downloading update package?"), _("Confirmation"), win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL) == win32con.IDYES:
                                break
                            else:
                                self.vnt_update_cancel_flag.clear()

            self.vnt_update_in_progress_flag.clear()
            # Trigger UI update to reflect current state after update finishes
            if not background_running:
                wx.CallAfter(self.if_update_input_param_ok, None)

            if self.vnt_update_cancel_flag.is_set():
                if os.path.exists(final_path):
                    os.remove(final_path)
                self.logger.write("Downloaded partial package file removed")
                if not background_running:
                    wx.CallAfter(self._display_update_progress, 0, "Progress")
                    wx.CallAfter(self.if_update_input_param_ok, None)  # Update UI based on current state
                return False

            return True
        except Exception as e:
            self.logger.write(f"Single-thread download error: {e}", 'critical')
            final_path = os.path.join(self.workingdir, self.update_package_fn)
            if os.path.exists(final_path):
                os.remove(final_path)
            # Re-enable UI elements in case of error
            if not background_running and self.vnt_update_in_progress_flag.is_set():
                self.vnt_update_in_progress_flag.clear()
                wx.CallAfter(self.if_update_input_param_ok, None)  # Update UI based on current state
            else:
                self.vnt_update_in_progress_flag.clear()
            return False

    def _update_initialize_parameters(self):

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        t = vnt_conf.get_value(VNT_Config.KEY_VERSION_FILE_URL) if vnt_conf.get_value(VNT_Config.KEY_VERSION_FILE_URL) is not None else self.default_version_control_file_url
        self.version_control_file_url = t

        try:
            cycle_time = vnt_conf.get_value(VNT_Config.KEY_UPDATE_CYCLE_SEC)
            if cycle_time is not None:
                # Convert to int in case it's stored as string
                self.update_check_cycle_sec = int(cycle_time)
                self.logger.write(f"Update cycle time is set to {self.update_check_cycle_sec} seconds")
            else:
                self.update_check_cycle_sec = 60  # default is 1 minute
                self.logger.write(f"Cycle time error, set to default {self.update_check_cycle_sec} seconds")
        except (ValueError, TypeError) as e:
            self.update_check_cycle_sec = 60
            self.logger.write(f"Set VNT update cycle error {e}, using default 60 seconds", 'critical')

        if self.version_control_file_url == '' or self.version_control_file_url is None or (not validators.url(self.version_control_file_url)):
            return False
        else:
            username, password, self.version_control_server_IP, port, self.version_control_fn = self._url_parse(self.version_control_file_url)

            if port is None or not port.isdigit():
                self.version_control_server_port = 80
            else:
                self.version_control_server_port = int(port)

        if self.version_control_server_IP == '' or self.version_control_server_IP is None or \
           self.version_control_fn is None or self.version_control_fn == '':
            return False
        else:
            self.updater_related_files = [self.update_package_fn, self.version_control_fn, self.updater_exe_nm]
            return True

    def _update_version_check(self, background_running=False):
        version_conf = VNT_Config(self.workingdir, self.version_control_fn, self.logger)
        if not self.vnt_app.inet_monitor.is_connected() or \
           not Internet_Connectivity_Monitor.is_server_connected((self.version_control_server_IP, self.version_control_server_port)):
            return False, False
        try:
            response_yaml = requests.get(self.version_control_file_url, stream=True)  # 假设服务器上有一个version.yaml文件
            total_size = int(response_yaml.headers.get("content-length", 0))

            if self.NOT_FOUND_INFO in response_yaml.text.strip().lower() or total_size > 102400:  # 100 KB max size for version file
                self.logger.write("Version file not found or incorrect")
                if not background_running:
                    self.vnt_app.bubble_msg_handler.msg("Update#Version file not found or incorrect")
                return False, False

            with open(os.path.join(self.workingdir, self.version_control_fn), "w") as f:
                print(response_yaml.text.strip())
                f.write(response_yaml.text.strip())

            print(f"{self.version_control_fn} Downloaded")

            self.update_package_url = version_conf.get_value(VNT_Config.KEY_UPDATE_URL)
            if self.update_package_url is None or self.update_package_url == '':
                self.logger.write(f"Update package URL not found in {self.version_control_fn}")
            else:
                username, password, self.update_server_IP, port, self.update_package_fn = self._url_parse(self.update_package_url)

                if port is None or not port.isdigit():
                    self.update_server_port = 80
                else:
                    self.update_server_port = int(port)

                self.updater_related_files = [self.update_package_fn, self.version_control_fn, self.updater_exe_nm]
                print(f"Update related files package: {self.updater_related_files}")

            latest_version = version_conf.get_value(VNT_Config.KEY_VERSION)

            if latest_version is None:
                self.logger.write(f"Version data not found in {self.version_control_fn}")
                return False, False

            excluded_IPs = version_conf.get_value(VNT_Config.KEY_EXCLUDE)

            if self.vnt_app.vnt_connection.virtual_IP is None:
                allowd_to_update = False
            else:
                if self.vnt_app.vnt_connection.virtual_IP in excluded_IPs:
                    self.logger.write(f"Current IP {self.vnt_app.vnt_connection.virtual_IP} is excluded from update")
                    allowd_to_update = False
                else:
                    allowd_to_update = True

            if latest_version != self.vnt_app.current_version and not background_running:
                if allowd_to_update:
                    self.vnt_app.bubble_msg_handler.msg(f"Update#Version {latest_version} announced")
                    self.logger.write(f"Current Version: {self.vnt_app.current_version}, New Version {latest_version} announced")
            else:
                print(f"New version: {latest_version}, Current Version: {self.vnt_app.current_version}")

            if not Internet_Connectivity_Monitor.is_server_connected((self.version_control_server_IP, self.version_control_server_port)):
                return False, False

            return latest_version != self.vnt_app.current_version, allowd_to_update

        except Exception as e:
            self.logger.write(f"Check for update: {e}", 'critical')
            return False, False

    def _vnt_update_daemon(self):

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        version_conf = VNT_Config(self.workingdir, self.version_control_fn, self.logger)
        i = 1

        if not self._update_initialize_parameters():
            self.logger.write("Error Initialize Update Parameters, automatic update will not work.", 'critical')
            while not self._update_initialize_parameters() and not self.vnt_update_daemon_exit_flag.is_set():
                time.sleep(5)
        else:
            self.logger.write("Update parameters initialized, Entering update daemon loop...", 'info')

        time.sleep(5)  # Give VNT connection time to set up

        while (not Internet_Connectivity_Monitor.is_server_connected((self.version_control_server_IP, self.version_control_server_port))):
            if self.vnt_update_daemon_exit_flag.is_set():
                return
            time.sleep(1)

        while True:
            if self.vnt_update_daemon_exit_flag.is_set():
                self.logger.write("VNT update daemon exit flag set, exiting update daemon thread")
                return

            # Check if auto update is enabled before proceeding
            can_update = vnt_conf.get_value(VNT_Config.KEY_AUTO_UPDATE_ENABLED)
            update_disabled = vnt_conf.get_value(VNT_Config.KEY_UPDATE_DISABLED)
            
            # If update is disabled or auto update is not enabled, skip the update check
            if update_disabled or not can_update:
                time.sleep(1)
                continue

            if i % self.update_check_cycle_sec == 0:  # check periodically seconds
                if not self.vnt_update_in_progress_flag.is_set():
                    if not os.path.exists(os.path.join(self.workingdir, self.update_package_fn)):
                        ver_diff, allowed = self._update_version_check(self.vnt_app.args.no_gui)
                        if ver_diff and allowed:
                            if self._update_download(self.vnt_app.args.no_gui):
                                # Double check auto update is still enabled before proceeding with update
                                can_update = vnt_conf.get_value(VNT_Config.KEY_AUTO_UPDATE_ENABLED)
                                if can_update:
                                    self._update_and_exit()
                    else:
                        # Double check auto update is still enabled before proceeding with update
                        can_update = vnt_conf.get_value(VNT_Config.KEY_AUTO_UPDATE_ENABLED)
                        if can_update:

                            checksum_remote = version_conf.get_value(VNT_Config.KEY_CHECKSUM)
                            checksum_local = self.calculate_SHA256(os.path.join(self.workingdir, self.update_package_fn))
                            if checksum_remote == checksum_local and (checksum_remote is not None and checksum_local is not None):
                                self.logger.write(f"Checksum: {checksum_local}")
                                self._update_and_exit()
                            else:
                                os.remove(os.path.join(self.workingdir, self.update_package_fn))
                                self.logger.write("Checksum not same. Download file removed")

            i += 1  # Increment counter for periodic update checks
            if i > 32768:
                i = 1
            time.sleep(1)

    def _fresh_start_actions(self):
        def _clear_update_related_files(files):
            nonlocal self
            for fn in files:
                try:
                    if fn is None:
                        return False
                    full_fn = os.path.join(self.workingdir, fn)
                    os.remove(full_fn)
                    self.logger.write(f"{full_fn} deleted after update", 'info')
                except OSError as e:
                    self.logger.write(f"Error remove {full_fn} {e}", "debug")
                    return False
            return True

        if _clear_update_related_files(self.updater_related_files):
            self.vnt_app.bubble_msg_handler.msg(f"Update#Latest Version {self.vnt_app.current_version} Updated")
            self.logger.write(f"Latest Version {self.vnt_app.current_version} Updated")
        else:
            self.logger.write("Error Deleting Update Packages on New Versions", 'debug')

    def _disable_ui_elements(self):
        """Disable all UI elements except radio buttons and cancel button"""
        # Disable server address fields
        self.version_host_txt.Enable(False)
        self.version_port_txt.Enable(False)

        # Disable update cycle field
        self.cycle_time_txt.Enable(False)

        # Disable progress bar
        self.update_progress.Enable(False)

        # Disable other buttons
        self.manually_check_update.Enable(False)
        self.update_OK.Enable(False)

        # Keep only radio buttons and cancel button enabled
        self.auto_update_radio.Enable(True)
        self.manual_update_radio.Enable(True)
        self.disable_update_radio.Enable(True)
        self.update_Cancel.Enable(True)

    def _enable_ui_elements(self):
        """Enable all UI elements"""
        # Enable server address fields
        self.version_host_txt.Enable(True)
        self.version_port_txt.Enable(True)

        # Enable update cycle field
        self.cycle_time_txt.Enable(True)

        # Enable progress bar
        self.update_progress.Enable(True)

        # Enable other buttons
        self.manually_check_update.Enable(True)
        self.update_OK.Enable(True)

        # Ensure radio buttons and cancel button remain enabled
        self.auto_update_radio.Enable(True)
        self.manual_update_radio.Enable(True)
        self.disable_update_radio.Enable(True)
        self.update_Cancel.Enable(True)

    @staticmethod
    def _url_parse(url):
        try:
            if not validators.url(url):
                return None, None, None, None, None

            parsed_url = urlparse(url)

            file_name = parsed_url.path.split('/')[-1]
            netloc = parsed_url.netloc

            if '@' in netloc:
                auth_part, domain_port_part = netloc.split('@')
                username, password = auth_part.split(':')
            else:
                domain_port_part = netloc
                username = None
                password = None

            if ':' in domain_port_part:
                domain_ip, port = domain_port_part.split(':')
            else:
                domain_ip = domain_port_part
                port = None
            return username, password, domain_ip, port, file_name
        except Exception as e:
            print(f"URL Parser: {e}", 'critical')
            return None, None, None, None, None

    @staticmethod
    def calculate_SHA256(file_path):
        sha256_hash = hashlib.sha256()

        try:
            with open(file_path, 'rb') as file:
                for chunk in iter(lambda: file.read(4096), b""):
                    sha256_hash.update(chunk)
            return sha256_hash.hexdigest()
        except FileNotFoundError:
            print(f"File not found: {file_path}")
            return None
        except Exception as e:
            print(f"Hash calculating error: {e}")
            return None


class VNT_Information_Window(wx.Frame):

    def __init__(self, parent, vnt_app):
        # Step 1: 使用逻辑尺寸初始化父类
        wx.Frame.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title=_(u"VNT Network Information"),
            pos=wx.DefaultPosition,
            size=(896, 600),  # 逻辑尺寸（100% DPI），调整为原 1120px 的 80%
            style=wx.CAPTION | wx.STATIC_BORDER | wx.TAB_TRAVERSAL,
        )

        # Step 2: 应用 DPI 缩放到窗口大小
        logical_window_size = (896, 600)
        scaled_window_size = self.FromDIP(logical_window_size)
        self.SetSize(scaled_window_size)
        self.SetMinSize(scaled_window_size)

        self.SetSizeHints(wx.DefaultSize, wx.DefaultSize)

        # Step 3: 计算 DPI 缩放后的列宽（原始列宽来自代码）
        col_widths_logical = [162, 163, 80, 90, 279]  # 前5列有显式设置
        col_widths_scaled = [int(self.FromDIP(w)) for w in col_widths_logical]

        bSizer5 = wx.BoxSizer(wx.VERTICAL)
        bSizer6 = wx.BoxSizer(wx.VERTICAL)

        gSizer3 = wx.GridSizer(1, 3, 0, 0)

        self.NetworkList = wx.RadioButton(
            self, wx.ID_ANY, _("Network List"), wx.DefaultPosition, wx.DefaultSize, 0
        )
        gSizer3.Add(self.NetworkList, 0, wx.ALL, 5)
        self.NetworkList.SetValue(False)

        self.Route = wx.RadioButton(
            self, wx.ID_ANY, _("Route Table"), wx.DefaultPosition, wx.DefaultSize, 0
        )
        gSizer3.Add(self.Route, 0, wx.ALL, 5)
        self.Route.SetValue(False)

        self.Node = wx.RadioButton(
            self, wx.ID_ANY, _("My Node Info"), wx.DefaultPosition, wx.DefaultSize, 0
        )
        gSizer3.Add(self.Node, 0, wx.ALL, 5)
        self.Node.SetValue(False)

        bSizer6.Add(gSizer3, 0, wx.EXPAND, 5)
        bSizer5.Add(bSizer6, 0, wx.EXPAND, 5)

        bSizer7 = wx.BoxSizer(wx.VERTICAL)

        self.m_grid3 = wx.grid.Grid(self, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize, 0)

        # Grid
        self.m_grid3.CreateGrid(20, 10)
        self.m_grid3.EnableEditing(False)
        self.m_grid3.EnableGridLines(True)
        self.m_grid3.EnableDragGridSize(False)
        self.m_grid3.SetMargins(0, 0)

        # Columns - 使用缩放后的宽度
        self.m_grid3.SetColSize(0, col_widths_scaled[0])
        self.m_grid3.SetColSize(1, col_widths_scaled[1])
        self.m_grid3.SetColSize(2, col_widths_scaled[2])
        self.m_grid3.SetColSize(3, col_widths_scaled[3])
        self.m_grid3.SetColSize(4, col_widths_scaled[4])
        # self.m_grid3.AutoSizeColumns()  # 保持注释
        self.m_grid3.EnableDragColMove(False)
        self.m_grid3.EnableDragColSize(True)
        self.m_grid3.SetColLabelSize(int(self.FromDIP(30)))
        self.m_grid3.SetColLabelAlignment(wx.ALIGN_CENTRE, wx.ALIGN_CENTRE)

        # Rows
        self.m_grid3.EnableDragRowSize(True)
        self.m_grid3.SetRowLabelSize(int(self.FromDIP(80)))
        self.m_grid3.SetRowLabelAlignment(wx.ALIGN_CENTRE, wx.ALIGN_CENTRE)

        # Label Appearance
        self.m_grid3.SetLabelTextColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_INACTIVECAPTIONTEXT)
        )

        # Cell Defaults
        self.m_grid3.SetDefaultCellAlignment(wx.ALIGN_LEFT, wx.ALIGN_CENTRE)  # 改为左对齐，更易阅读
        self.m_grid3.SetRowLabelSize(0)
        self.m_grid3.SetColLabelSize(0)

        bSizer7.Add(self.m_grid3, 1, wx.ALL | wx.EXPAND, 5)  # 注意：改为 proportion=1 以允许伸缩
        bSizer5.Add(bSizer7, 1, wx.EXPAND, 5)

        bSizer8 = wx.BoxSizer(wx.VERTICAL)

        self.m_button2 = wx.Button(self, wx.ID_ANY, _("Close"), size=wx.Size(-1, 40))
        bSizer8.Add(self.m_button2, 0, wx.ALL | wx.EXPAND, 5)  # 按钮通常不需要伸缩

        bSizer5.Add(bSizer8, 0, wx.EXPAND, 5)

        self.SetSizer(bSizer5)
        self.Layout()
        self.Centre(wx.BOTH)

        # Connect Events
        self.Bind(wx.EVT_SHOW, self.on_show)
        self.NetworkList.Bind(wx.EVT_RADIOBUTTON, self.on_networkList_selected)
        self.Route.Bind(wx.EVT_RADIOBUTTON, self.on_routetable_selected)
        self.Node.Bind(wx.EVT_RADIOBUTTON, self.on_nodeinfo_selected)
        self.m_button2.Bind(wx.EVT_BUTTON, self.on_close_infowin)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

        # Initialize instance variables
        self.logger = vnt_app.logger
        self.workingdir = vnt_app.workingdir
        self.vnt_app = vnt_app
        self.check_vnt_info_daemon = None
        self.exit_flag = threading.Event()
        self.last_data_hash = {}  # 用于检测数据是否变化

    def __del__(self):
        pass

    def refresh_ui(self):
        """
        刷新界面以应用新的语言设置。
        所有通过 _() 标记的字符串将被重新翻译。
        用户当前选择状态会被保留。
        """
        # 1. 保存当前 RadioButton 选中状态
        network_list_selected = self.NetworkList.GetValue()
        route_selected = self.Route.GetValue()
        node_selected = self.Node.GetValue()

        # 2. 更新窗口标题
        self.SetTitle(_(u"VNT Network Information"))

        # 3. 更新 RadioButton 标签
        self.NetworkList.SetLabel(_("Network List"))
        self.Route.SetLabel(_("Route Table"))
        self.Node.SetLabel(_("My Node Info"))

        # 4. 更新按钮文本
        self.m_button2.SetLabel(_("Close"))

        # 5. 恢复 RadioButton 选中状态（SetLabel 可能影响焦点但不会改变值，保险起见重设）
        self.NetworkList.SetValue(network_list_selected)
        self.Route.SetValue(route_selected)
        self.Node.SetValue(node_selected)

        # 6. 刷新布局（适应可能变化的文本宽度）
        self.Layout()

        # 注意：Grid 内容（如列标题、单元格）若包含可翻译文本，
        #       需要额外逻辑重新填充（当前代码未设置列标题文本，故暂不处理）。
        #       如果你后续使用 SetColLabelValue() 设置了列名，也需在此刷新。

    def on_char_hook(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.exit_flag.set()
            time.sleep(0.5)
            self.Hide()
        else:
            event.Skip()  # 允许其他按键正常处理

    def on_show(self, event):
        self.CenterOnScreen()
        event.Skip()

    def on_networkList_selected(self, event):

        if self.check_vnt_info_daemon is not None:
            self.exit_flag.set()
            while self.check_vnt_info_daemon.is_alive():
                time.sleep(0.1)
        self.exit_flag.clear()

        self.check_vnt_info_daemon = threading.Thread(target=self._check_vnt_info, args=("--list", self.exit_flag,))
        self.check_vnt_info_daemon.start()
        event.Skip()

    def on_routetable_selected(self, event):

        if self.check_vnt_info_daemon is not None:
            self.exit_flag.set()
            while self.check_vnt_info_daemon.is_alive():
                time.sleep(0.1)
        self.exit_flag.clear()

        self.check_vnt_info_daemon = threading.Thread(target=self._check_vnt_info, args=("--route", self.exit_flag,))
        self.check_vnt_info_daemon.start()
        event.Skip()

    def on_nodeinfo_selected(self, event):

        if self.check_vnt_info_daemon is not None:
            self.exit_flag.set()
            while self.check_vnt_info_daemon.is_alive():
                time.sleep(0.1)
        self.exit_flag.clear()

        # 初始化数据哈希记录字典，用于检测数据变化
        if not hasattr(self, 'last_data_hash'):
            self.last_data_hash = {}
        
        self.check_vnt_info_daemon = threading.Thread(target=self._check_vnt_info, args=("--info", self.exit_flag,))
        self.check_vnt_info_daemon.start()
        event.Skip()

    def on_close_infowin(self, event):
        self.exit_flag.set()
        time.sleep(0.5)
        self.Hide()
        event.Skip()

    def _write_grid(self, row, col, value):
        try:
            self.m_grid3.SetCellValue(row, col, value)
        except Exception as e:
            self.logger.write(f"{e} row: {row} col: {col}", 'debug')

    def _redraw_grid(self, row_count, col_count, cmd):
        if self.m_grid3.GetNumberRows() != 0 and self.m_grid3.GetNumberCols() != 0:
            self.m_grid3.ClearGrid()
            self.m_grid3.DeleteRows(0, self.m_grid3.GetNumberRows())
            self.m_grid3.DeleteCols(0, self.m_grid3.GetNumberCols())

        self.m_grid3.AppendRows(row_count)
        self.m_grid3.AppendCols(col_count)
        
        # 根据命令类型设置合理的列宽（针对 VNT2 输出格式优化）
        if cmd == "--list":
            # clients 命令：IP | Name | Version | Online | P2P | RTT | Loss | Last Connected Time
            grid_cols_size = [120, 180, 80, 70, 70, 60, 70, 180]
        elif cmd == "--route":
            # route 命令：Destination IP | Metric | RTT (ms) | Remote Address
            grid_cols_size = [140, 80, 100, 350]
        elif cmd == "--info":
            # info 命令：Key | Value
            grid_cols_size = [250, 500]
        else:
            grid_cols_size = [140, 240, 60, 80, 230]

        for i in range(col_count):
            if i < len(grid_cols_size):
                self.m_grid3.SetColSize(i, grid_cols_size[i])
            else:
                # 如果列数超过预设，使用默认宽度
                self.m_grid3.SetColSize(i, 120)

    def _check_vnt_info(self, cmd, exit_flag):
        """
        VNT2 信息查询方法
        使用 vnt2_ctrl.exe 替代 vnt-cli.exe
        
        命令映射:
        - --list → clients (客户端信息列表)
        - --route → route (路由信息)
        - --info → info (程序信息)
        """
        import hashlib
        global VNT_CTRL_EXE

        working_vnt_ctrl = os.path.join(self.workingdir, VNT_CTRL_EXE)
        last_row_count = 0
        last_col_count = 0

        # VNT1 到 VNT2 的命令映射
        cmd_mapping = {
            "--list": "clients",
            "--route": "route",
            "--info": "info"
        }
        
        vnt2_cmd = cmd_mapping.get(cmd, cmd)
        self.logger.write(f"[GUI DEBUG] Info Daemon started for cmd={cmd}, mapped to vnt2_cmd={vnt2_cmd}", 'debug')

        while not exit_flag.is_set():
            
            try:
                # VNT2 使用子命令格式: vnt2_ctrl.exe <command>
                self.logger.write(f"[GUI DEBUG] Starting process: {working_vnt_ctrl} {vnt2_cmd}", 'debug')
                p = subprocess.Popen(
                    "\"" + working_vnt_ctrl + "\" " + vnt2_cmd,
                    shell=True,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                
                # 等待进程完成（VNT2 命令是瞬时执行的）
                stdout, stderr = p.communicate(timeout=5)
                
                if p.returncode != 0:
                    self.logger.write(f"[GUI DEBUG] Command failed with return code {p.returncode}: {stderr.decode('utf-8', 'ignore')}", 'error')
                    time.sleep(2)  # 失败时延长重试间隔
                    continue
                
                # 解码输出
                text = stdout.decode('utf-8', 'ignore')
                self.logger.write(f"[GUI DEBUG] Received {len(text)} bytes of output", 'debug')
                
                if not text or len(text.strip()) == 0:
                    self.logger.write(f"[GUI DEBUG] Empty output received", 'debug')
                    time.sleep(2)  # 空输出时延长重试间隔
                    continue
                
                # 清理 ANSI 转义码（颜色代码）
                # ANSI 转义码格式: \x1b[...m 或 \x1b(...\)
                ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
                text = ansi_escape.sub('', text)
                self.logger.write(f"[GUI DEBUG] Cleaned ANSI escape codes, now {len(text)} bytes", 'debug')

                # 根据命令类型选择不同的解析策略
                data = []
                
                if cmd == "--info":
                    # info 命令输出的是键值对格式，需要特殊处理
                    lines = text.strip().split("\n")
                    for line in lines:
                        line = line.strip()
                        # 跳过装饰行和空行
                        if not line or line.startswith('---') or line.startswith('==='):
                            continue
                        
                        # 尝试解析 "Key: Value" 格式
                        if ':' in line:
                            parts = line.split(':', 1)  # 只分割第一个冒号
                            if len(parts) == 2:
                                key = parts[0].strip()
                                value = parts[1].strip()
                                if key and value:
                                    data.append([key, value])
                    
                    self.logger.write(f"[GUI DEBUG] Parsed {len(data)} key-value pairs from info output", 'debug')
                    
                else:
                    # clients 和 route 命令输出的是表格格式
                    lines = text.strip().split("\n")
                    self.logger.write(f"[GUI DEBUG] Parsed {len(lines)} lines from output", 'debug')
                    
                    # 解析表格数据
                    for line in lines:
                        line = line.strip()
                        # 跳过空行和表格边框行
                        if not line or line.startswith('+') or line.startswith('---'):
                            continue
                        
                        # 处理表头行和数据行
                        if line.startswith('|'):
                            # 移除首尾的 | 符号，然后按 | 分割
                            cells = [cell.strip() for cell in line.split('|')[1:-1]]
                            if cells and any(cell for cell in cells):  # 确保不是全空行
                                data.append(cells)
                    
                    # 对 clients 命令的数据按 IP 地址排序（保留表头）
                    if cmd == "--list" and len(data) > 1:
                        header = data[0]  # 保存表头
                        data_rows = data[1:]  # 获取数据行
                        
                        # 定义 IP 地址排序函数
                        def ip_sort_key(row):
                            """将 IP 地址转换为可排序的元组"""
                            try:
                                if row and len(row) > 0:
                                    ip_str = row[0]  # IP 在第一列
                                    # 将 IP 地址转换为元组用于排序 (例如: "10.10.0.5" -> (10, 10, 0, 5))
                                    parts = ip_str.split('.')
                                    if len(parts) == 4:
                                        return tuple(int(p) for p in parts)
                            except (ValueError, IndexError):
                                pass
                            return (999, 999, 999, 999)  # 无效 IP 排到最后
                        
                        # 按 IP 地址排序数据行
                        data_rows.sort(key=ip_sort_key)
                        
                        # 重新组合：表头 + 排序后的数据
                        data = [header] + data_rows
                    
                    self.logger.write(f"[GUI DEBUG] Extracted {len(data)} rows of table data", 'debug')

                # 计算数据的哈希值，用于检测是否变化
                data_str = str(data)
                current_hash = hashlib.md5(data_str.encode('utf-8')).hexdigest()
                
                # 如果数据没有变化，跳过更新（减少刷屏）
                if hasattr(self, 'last_data_hash') and cmd in self.last_data_hash and self.last_data_hash[cmd] == current_hash:
                    time.sleep(2)  # 数据未变化，延长刷新间隔到 2 秒
                    continue
                
                # 数据已变化，更新哈希值
                if not hasattr(self, 'last_data_hash'):
                    self.last_data_hash = {}
                self.last_data_hash[cmd] = current_hash

                # 根据数据调整 Grid 的大小
                row_count = len(data)
                col_count = max(len(row) for row in data) if data else 0

                if (last_col_count != col_count or last_row_count != row_count) and row_count != 0 and col_count != 0:
                    self.logger.write(f"[GUI DEBUG] Redrawing grid: {row_count} rows, {col_count} cols", 'debug')
                    wx.CallAfter(self._redraw_grid, row_count, col_count, cmd)

                last_row_count = row_count
                last_col_count = col_count

                # 将数据写入 Grid
                if len(data) > 0:
                    for row, row_data in enumerate(data):
                        for col, value in enumerate(row_data):
                            if col < self.m_grid3.GetNumberCols():  # 确保不超出列范围
                                wx.CallAfter(self._write_grid, row, col, value)
                    
                    self.logger.write(f"[GUI DEBUG] Wrote {len(data)} rows to grid", 'debug')

            except subprocess.TimeoutExpired:
                self.logger.write(f"[GUI DEBUG] Command timed out", 'error')
                if p.poll() is None:
                    p.kill()
            except Exception as e:
                self.logger.write(f"[GUI DEBUG] Failed to execute vnt2_ctrl.exe: {e}", 'critical')
            
            # 正常刷新间隔：1.5 秒（平衡实时性和可读性）
            time.sleep(1.5)

        self.logger.write(f"Info Daemon {cmd} Exit ...")


class VNT_TaskBar_Icon(wx.adv.TaskBarIcon):

    TRAY_TOOLTIP = 'VNT Helper'

    def __init__(self, frame, vnt_app):
        self.frame = frame
        self.logger = vnt_app.logger
        self.config_fn = vnt_app.config_fn
        self.workingdir = vnt_app.workingdir
        # self.res_path = vnt_app._resource_path("res")
        self.vnt_app = vnt_app
        super(VNT_TaskBar_Icon, self).__init__()
        self.set_icon(os.path.join(self.workingdir, VNT_TRAY_ICON))
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DOWN, self.on_left_mouse)

    def __del__(self):
        pass

    def on_left_mouse(self, event):
        self.on_vnt_change_settings(event)

    def set_icon(self, path):
        icon = wx.Icon(path)
        self.SetIcon(icon, self.TRAY_TOOLTIP)

    def CreatePopupMenu(self):
        menu = wx.Menu()
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)

        # --- About and Info ---
        about_label = _("About")
        self.create_menu_item(menu, f"📢 {about_label}", self.on_about)

        menu.AppendSeparator()

        show_log_label = _("Show log info")
        self.create_menu_item(menu, f"📝 {show_log_label}", self.on_show_vnt_log_Info)

        conn_info_label = _("Connection Information")
        self.create_menu_item(menu, f"📊 {conn_info_label}", self.on_show_network_info)

        menu.AppendSeparator()

        # --- Autostart toggle ---
        if self.vnt_app.reg_task_autorun.is_autorun_on():
            autostart_label = _("Remove Autostart")
            t = f"🚫 {autostart_label}"
        else:
            autostart_label = _("Set Autostart")
            t = f"⭕ {autostart_label}"
        self.create_menu_item(menu, t, self.on_autostart)

        # --- VNT Connection toggle ---
        if self.vnt_app.vnt_connection.toggled_off:
            conn_toggle_label = _("Toggle on VNT Connection")
            t = f"🔗 {conn_toggle_label}"
        else:
            conn_toggle_label = _("Toggle off VNT Connection")
            t = f"⛓️‍💥 {conn_toggle_label}"
        self.create_menu_item(menu, t, self.on_toggle_vnt_connection)

        # --- Notification toggle ---
        enabled = vnt_conf.get_value(VNT_Config.KEY_VNT_NOTIFICATION_ENABLED)
        # Treat None as enabled (default)
        if enabled or enabled is None:
            notif_label = _("Disable Notification")
            t = f"🔇 {notif_label}"
        else:
            notif_label = _("Enable Notification")
            t = f"🔊 {notif_label}"
        self.create_menu_item(menu, t, self.on_toggle_notification)

        # --- Display Language 二级菜单 ---
        lang_menu = wx.Menu()
        # Note: Language names are usually NOT translated (user selects by native name)
        current_lang = vnt_conf.get_value(VNT_Config.KEY_DISPLAY_LANGUAGE)
        if current_lang == "en":
            self.create_menu_item(lang_menu, 'English ✅', self.on_lang_english)
            self.create_menu_item(lang_menu, '简体中文', self.on_lang_zh_cn)
        elif current_lang == "zh_CN":
            self.create_menu_item(lang_menu, 'English', self.on_lang_english)
            self.create_menu_item(lang_menu, '简体中文 ✅', self.on_lang_zh_cn)
        else:
            current_lang = "en"
            self.create_menu_item(lang_menu, 'English ✅', self.on_lang_english)
            self.create_menu_item(lang_menu, '简体中文', self.on_lang_zh_cn)

        display_lang_label = _("Display Language")
        lang_item = wx.MenuItem(menu, -1, f"🚩 {display_lang_label}", subMenu=lang_menu)
        menu.Append(lang_item)

        # --- Other settings ---
        menu.AppendSeparator()

        settings_label = _("Settings")
        self.create_menu_item(menu, f"⚙️ {settings_label}", self.on_vnt_change_settings)

        profiles_label = _("Manage Profiles")
        self.create_menu_item(menu, f"📚 {profiles_label}", self.on_vnt_manage_profiles)

        update_label = _("Update")
        self.create_menu_item(menu, f"🔀 {update_label}", self.on_update_vnt_helper_config)

        reset_label = _("Reset")
        self.create_menu_item(menu, f"🔄️ {reset_label}", self.on_reset)

        menu.AppendSeparator()

        exit_label = _("Exit")
        self.create_menu_item(menu, f"🔚 {exit_label}", self.on_exit)

        return menu

    def create_menu_item(self, menu, label, func):
        item = wx.MenuItem(menu, -1, label)
        menu.Bind(wx.EVT_MENU, func, id=item.GetId())
        menu.Append(item)
        return item

    def on_lang_english(self, event):
        global _
        _ = VNT_Helper_App.setup_i18n('en')
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        vnt_conf.set_value(VNT_Config.KEY_DISPLAY_LANGUAGE, "en")
        self.vnt_app.main_window.refresh_ui()
        self.vnt_app.main_window.vnt_update_window.refresh_ui()
        self.vnt_app.main_window.vnt_info.refresh_ui()
        if ctypes.windll.kernel32.GetConsoleWindow() == 0:
            self.vnt_app.main_window.vnt_log_win.refresh_ui()

    def on_lang_zh_cn(self, evnt):
        global _
        _ = VNT_Helper_App.setup_i18n('zh_CN')
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        vnt_conf.set_value(VNT_Config.KEY_DISPLAY_LANGUAGE, "zh_CN")
        self.vnt_app.main_window.refresh_ui()
        self.vnt_app.main_window.vnt_update_window.refresh_ui()
        self.vnt_app.main_window.vnt_info.refresh_ui()
        if ctypes.windll.kernel32.GetConsoleWindow() == 0:
            self.vnt_app.main_window.vnt_log_win.refresh_ui()

    def on_toggle_notification(self, event):
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        s = vnt_conf.get_value(VNT_Config.KEY_VNT_NOTIFICATION_ENABLED) if vnt_conf.get_value(VNT_Config.KEY_VNT_NOTIFICATION_ENABLED) is not None else False
        vnt_conf.set_value(VNT_Config.KEY_VNT_NOTIFICATION_ENABLED, not s)

    def on_toggle_vnt_connection(self, event):
        global VNT_CLIENT_NAME

        if self.vnt_app.vnt_connection.is_toggled_off():
            # Toggle ON: 启动VNT网络
            self.vnt_app.vnt_connection.toggle(False)
            self.logger.write("VNT Daemon toggled on ...")
        else:
            # Toggle OFF: 停止VNT网络
            self.vnt_app.vnt_connection.toggle(True)
            time.sleep(0.5)
            self.vnt_app.vnt_connection.virtual_IP = None
            # _connected_notified 已在 toggle() 方法中通过 stop_vnt_network() 重置

            self.vnt_app.bubble_msg_handler.msg("Status#Virtual IP turned off")
            self.logger.write("VNT Daemon toggled off ...")

    def on_show_vnt_log_Info(self, event):

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd != 0:
            if not win32gui.IsWindowVisible(hwnd):
                ctypes.windll.user32.ShowWindow(hwnd, True)
                ctypes.windll.kernel32.CloseHandle(hwnd)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            else:
                ctypes.windll.user32.ShowWindow(hwnd, False)
                ctypes.windll.kernel32.CloseHandle(hwnd)
                time.sleep(0.1)
        else:

            self.vnt_app.main_window.vnt_log_win.Show(True)
            VNT_Main_Window.set_window_topmost(self.vnt_app.main_window.vnt_log_win.Show)

    def on_update_vnt_helper_config(self, event):
        self.vnt_app.main_window.vnt_update_window.Show(True)
        VNT_Main_Window.set_window_topmost(self.vnt_app.main_window.vnt_update_window)

    def on_show_network_info(self, event):
        self.vnt_app.main_window.vnt_info.Show(True)
        VNT_Main_Window.set_window_topmost(self.vnt_app.main_window.vnt_info)

    def on_vnt_change_settings(self, event):
        self.vnt_app.main_window.Show(True)
        VNT_Main_Window.set_window_topmost(self.vnt_app.main_window)

    def on_vnt_manage_profiles(self, event):
        dlg = VNT_ManageProfile_Frame(self.vnt_app.main_window, self.vnt_app)
        selected_id = dlg.ShowModal()
        VNT_Main_Window.set_window_topmost(dlg)
        if selected_id == wx.ID_OK:
            selected = dlg.get_selected_items()
            print("Selected profiles:", selected)
        dlg.Destroy()

    def on_autostart(self, event):
        to_turn_on = not self.vnt_app.reg_task_autorun.is_autorun_on()

        if to_turn_on:
            result = self.vnt_app.reg_task_autorun.add_autorun()
            t = _("AutoRun is turned ON, VNT Helper will automatically run on next boot")
        else:
            result = self.vnt_app.reg_task_autorun.remove_autorun()
            t = _("AutoRun is turned OFF, VNT Helper will need to be manually run on next boot")

        if result:
            win32api.MessageBox(0, t, _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK)
        else:
            win32api.MessageBox(0, _("Fail to change AutoRun setting, Try manually change regedit and taskscheduler!"), _("Status"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)

        event.Skip()

    def on_reset(self, event):
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        t = _("Are you sure to clean VNT network settings?\n\nVNT Helper will stop running. Next time run will be a fresh start.")
        if win32api.MessageBox(0, t, _("Confirmation"),  win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL) != win32con.IDYES:
            return

        connection_config_file = vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)
        try:
            self.vnt_app.vnt_connection.stop()
            time.sleep(1)

            # ⭐ 重置操作：完全关闭守护进程
            self.vnt_app.update_process_started = True
            if self.vnt_app.is_service_installed():
                if self.vnt_app.get_service_status() == "RUNNING":
                    # 使用新的 shutdown_daemon 命令完全退出守护进程
                    self.logger.write("Sending shutdown_daemon command to fully exit daemon...", 'info')
                    resp = self.vnt_app.vnt_connection._send_ipc_command({"cmd": "shutdown_daemon"})
                    if resp.get("status") == "ok":
                        self.logger.write("Shutdown daemon command succeeded, waiting for daemon to exit...")
                        # 等待守护进程完全退出
                        time.sleep(2)
                    else:
                        self.logger.write(f"Shutdown daemon command response: {resp.get('msg', 'unknown')}")
                    
                    # 停止Windows服务
                    self.logger.write("Stopping VNT daemon service...", 'info')
                    self.vnt_app.stop_service()
                
                # 卸载服务
                self.logger.write("Uninstalling VNT daemon service...", 'info')
                self.vnt_app.uninstall_service()

            # 删除配置文件
            if os.path.exists(connection_config_file):
                os.remove(connection_config_file)
                self.logger.write(f"Removed config file: {connection_config_file}")
            
            if os.path.exists(os.path.join(self.workingdir, self.config_fn)):
                os.remove(os.path.join(self.workingdir, self.config_fn))
                self.logger.write(f"Removed helper config file")
            
            # 移除自启动
            self.vnt_app.reg_task_autorun.remove_autorun()
            time.sleep(0.1)

            # 停止GUI应用
            self.vnt_app.stop()
        except Exception as e:
            self.logger.write(f"Reset {e}", 'critical')
            win32api.MessageBox(0, _("Error Cleaning VNT Setting. You may consider (1) manually remove\
                          {self.config_fn} in the VNT folder. (2) REGEDIT to find MANAGE_VNT key and remove it"),
                                _("Manage VNT not Initialized"), win32con.MB_OK | win32con.MB_ICONASTERISK | win32con.MB_SYSTEMMODAL)
        return

    def on_about(self, event):
        resp = self.vnt_app.vnt_connection._send_ipc_command({"cmd": "status"})
        if resp.get("status") == "ok" and resp.get("running") == "yes":
            ip = resp.get("virtual_ip")
            self.vnt_app.vnt_cli_version = resp.get("version")
            self.vnt_app.vnt_cli_serial = resp.get("serial")
            self.vnt_app.vnt_server_version = resp.get("server_version")
            self.logger.write(f"About: ip {ip}; cli ver {self.vnt_app.vnt_cli_version}; serial {self.vnt_app.vnt_cli_serial}; server ver {self.vnt_app.vnt_server_version}", 'debug')
        time.sleep(0.5)

        CMD_LINE_HELP = _('Command line parameters:\n') + f'{os.path.basename(sys.argv[0])} [-h] [-d] [-b] [-v]\n\n' + _('optional arguments:\n') + \
            "-h, --help:".ljust(20) + _("     show this help message and exit\n") + \
            "-d, --debug:".ljust(20) + _("   set DEBUG mode in console version\n") + \
            "-b, --background:".ljust(20) + _("run in background, no tray icon\n") + \
            "-v, --version:".ljust(20) + _("    get the version information\n")

        ver_info = f"\nVNT Helper version:  {self.vnt_app.current_version}\n"
        ver_info += f"vnt-cli.exe version:    {self.vnt_app.vnt_cli_version}\n"
        ver_info += f"vnt-cli.exe serial:       {self.vnt_app.vnt_cli_serial}\n"
        ver_info += f"vnt server version:    {self.vnt_app.vnt_server_version}\n\n{CMD_LINE_HELP}"

        about = AboutDialog(
            parent=self.vnt_app.main_window,
            title=_("About"),
            icon_path=os.path.join(self.workingdir, VNT_HELPER_ICON),
            app_name="VNT Helper",
            version=ver_info,
            description="Virtual IP: %s\n" % self.vnt_app.vnt_connection.virtual_IP,
            url="https://rustvnt.com",
            quote=_("\"This is a crazy world. By coding sometimes we ignore it...\"") + "\n- " + _("By the Author in an unforgettable spring of 2022 in Shanghai.")
        )
        about.ShowModal()
        VNT_Main_Window.set_window_topmost(about)
        about.Destroy()

    def on_exit(self, event):
        if win32api.MessageBox(0, _("Are you sure to close VNT network and exit?"), _("Confirmation"),  win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL) == win32con.IDYES:
            self.vnt_app.stop()


class VNT_Log_Window(wx.Frame):

    def __init__(self, parent, vnt_app):
        global VNT_CLI_LOG_FILE

        # Step 1: 先调用父类初始化（使用逻辑尺寸，100% DPI 设计值）
        logical_size = (900, 600)  # 仅用于初始化，后续会调整
        super().__init__(
            parent,
            title=_("VNT Log Viewer"),
            pos=(0, 0),  # 临时位置，稍后 Move
            size=logical_size,
            style=wx.STAY_ON_TOP | wx.CAPTION | wx.STATIC_BORDER | wx.TAB_TRAVERSAL
        )

        # Step 2: 现在 self 已是有效 wx.Frame，可以安全使用 FromDIP
        x, y, screen_width, screen_height = wx.ClientDisplayRect()

        # 将逻辑最大宽度 900 转为当前 DPI 下的实际像素
        scaled_max_width = self.FromDIP(900)
        win_width = min(scaled_max_width, screen_width)
        win_height = screen_height
        win_x = x + screen_width - win_width
        win_y = y

        # 应用最终位置和大小
        self.SetSize(int(win_width), int(win_height))
        self.Move(int(win_x), int(win_y))

        # Step 3: 构建界面
        self.Bind(wx.EVT_CLOSE, self.on_close)

        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.BORDER_SUNKEN | wx.LC_HRULES | wx.LC_VRULES
        )

        # 列宽：基于逻辑设计值，经 DPI 缩放
        col_widths_logical = [180, 70, 80, 550]
        col_widths_scaled = [self.FromDIP(w) for w in col_widths_logical]

        self.list_ctrl.InsertColumn(0, _("Time"), width=int(col_widths_scaled[0]))
        self.list_ctrl.InsertColumn(1, _("Level"), width=int(col_widths_scaled[1]))
        self.list_ctrl.InsertColumn(2, _("PID"), width=int(col_widths_scaled[2]))
        self.list_ctrl.InsertColumn(3, _("Message"), width=int(col_widths_scaled[3]))

        self.list_ctrl.Bind(wx.EVT_KEY_DOWN, self.on_key_down)
        self.list_ctrl.Bind(wx.EVT_CONTEXT_MENU, self.on_right_click)

        vbox.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 5)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.close_btn = wx.Button(panel, label=_("Close"))
        self.save_btn = wx.Button(panel, label=_("Save"))
        self.close_btn.Bind(wx.EVT_BUTTON, self.on_close)
        self.save_btn.Bind(wx.EVT_BUTTON, self.on_save)
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(self.close_btn, 0, wx.RIGHT, 5)
        btn_sizer.Add(self.save_btn, 0)
        btn_sizer.AddStretchSpacer()
        vbox.Add(btn_sizer, 0, wx.EXPAND | wx.BOTTOM, 10)

        panel.SetSizer(vbox)

        # 捕获 ESC 键以隐藏窗口
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

        # 初始化应用相关属性
        self.vnt_app = vnt_app
        self.working_dir = self.vnt_app.workingdir
        self.LOG_FILE = os.path.join(self.working_dir, VNT_CLI_LOG_FILE)

        self.last_position = 0
        self.running = True
        self.update_thread = threading.Thread(target=self.watch_log_file, daemon=True)
        self.update_thread.start()

        self.list_ctrl.SetFocus()

    def refresh_ui(self):
        """
        刷新界面以应用新的语言设置。
        所有通过 _() 标记的字符串将被重新翻译。
        日志内容和用户交互状态（如滚动位置）将被保留。
        """
        # 1. 更新窗口标题
        self.SetTitle(_("VNT Log Viewer"))

        # 2. 更新 ListCtrl 列标题
        headers = [_("Time"), _("Level"), _("PID"), _("Message")]
        for i, text in enumerate(headers):
            item = wx.ListItem()
            item.SetMask(wx.LIST_MASK_TEXT)
            item.SetText(text)
            self.list_ctrl.SetColumn(i, item)

        # 3. 更新按钮文本
        self.close_btn.SetLabel(_("Close"))
        self.save_btn.SetLabel(_("Save"))

        # 4. 刷新布局（适应新文本长度）
        self.Layout()

    def on_char_hook(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.Hide()
        else:
            event.Skip()  # 允许其他按键正常处理

    def on_close(self, event):
        self.Hide()

    def on_button_cancel(self, event):
        self.Hide()

    def on_key_down(self, event):
        if event.GetKeyCode() == wx.WXK_DELETE:
            self.delete_selected_items()
        else:
            event.Skip()

    def on_right_click(self, event):
        menu = wx.Menu()
        copy_item = menu.Append(wx.ID_ANY, "Copy Selected")
        del_item = menu.Append(wx.ID_ANY, "Delete Selected")
        self.Bind(wx.EVT_MENU, lambda e: self.copy_selected_to_clipboard(), copy_item)
        self.Bind(wx.EVT_MENU, lambda e: self.delete_selected_items(), del_item)
        menu.AppendSeparator()
        clear_view = menu.Append(wx.ID_ANY, "Clear View Only")
        clear_file = menu.Append(wx.ID_ANY, "Clear Log File and View")
        self.Bind(wx.EVT_MENU, lambda e: self.clear_all_gui(), clear_view)
        self.Bind(wx.EVT_MENU, lambda e: self.clear_log_file_and_gui(), clear_file)
        self.PopupMenu(menu)
        menu.Destroy()

    def on_save(self, event):
        dlg = wx.MessageDialog(
            self,
            _("Save current log content?\nThe original log file will be overwritten with the lines currently displayed."),
            _("Confirm Save"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION
        )
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            return
        dlg.Destroy()

        try:
            log_lines = []
            count = self.list_ctrl.GetItemCount()
            for i in range(count):
                time_val = self.list_ctrl.GetItemText(i, 0)
                level_val = self.list_ctrl.GetItemText(i, 1)
                pid_val = self.list_ctrl.GetItemText(i, 2)
                msg_val = self.list_ctrl.GetItemText(i, 3)

                if time_val and level_val and pid_val.isdigit():
                    line = f"{time_val} - {level_val} - PID {pid_val} : {msg_val}"
                else:
                    line = msg_val if msg_val else time_val

                sub_lines = line.split('\n')
                log_lines.extend(sub_lines)

            with open(self.LOG_FILE, 'w', encoding='gbk') as f:
                for line in log_lines:
                    f.write(line + '\n')

            if os.path.exists(self.LOG_FILE):
                self.last_position = os.path.getsize(self.LOG_FILE)
            else:
                self.last_position = 0

            win32api.MessageBox(None, _("Log saved successfully!"), _("Success"), win32con.MB_OK | win32con.MB_ICONINFORMATION | win32con.MB_SYSTEMMODAL)

        except Exception as e:
            win32api.MessageBox(None, _("Failed to save log:\n{e}").format(e=e), _("Error"), win32con.MB_OK | win32con.MB_ICONERROR | win32con.MB_SYSTEMMODAL)

    def read_log_lines(self):
        if not os.path.exists(self.LOG_FILE):
            return []

        for encoding in ['gbk']:
            try:
                with open(self.LOG_FILE, 'r', encoding=encoding, errors='replace') as f:
                    f.seek(self.last_position)
                    lines = f.readlines()
                    self.last_position = f.tell()
                    return lines
            except Exception:
                continue
        return []

    def parse_log_line(self, line):
        pattern = r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s*-\s*(\w+)\s*-\s*PID\s+(\d+)\s*:\s*(.*)$'
        match = re.match(pattern, line.strip())
        if match:
            timestamp, level, pid, message = match.groups()
            return timestamp, level, pid, message
        else:
            # Fallback for unparseable lines
            if re.match(r'^\d{4}-\d{2}-\d{2}', line):
                return (line.strip(), "", "", "")
            else:
                return (None, "", "", line.rstrip('\r\n'))

    def add_log_entry(self, timestamp, level, pid, message):
        def _append():
            idx = self.list_ctrl.InsertItem(self.list_ctrl.GetItemCount(), timestamp or "")
            self.list_ctrl.SetItem(idx, 1, level)
            self.list_ctrl.SetItem(idx, 2, pid)
            self.list_ctrl.SetItem(idx, 3, message)
            self.list_ctrl.EnsureVisible(idx)
        wx.CallAfter(_append)

    def watch_log_file(self):
        while self.running:
            try:
                lines = self.read_log_lines()
                for line in lines:
                    parsed = self.parse_log_line(line)
                    if parsed[0] is not None:
                        self.add_log_entry(*parsed)
                    else:
                        # Append to last message if it's a continuation line
                        if self.list_ctrl.GetItemCount() > 0:
                            last = self.list_ctrl.GetItemCount() - 1
                            old_msg = self.list_ctrl.GetItemText(last, 3)
                            new_msg = old_msg + "\n" + line.rstrip('\r\n')
                            wx.CallAfter(self.list_ctrl.SetStringItem, last, 3, new_msg)
                        else:
                            self.add_log_entry("", "", "", line.rstrip('\r\n'))
            except Exception as e:
                wx.CallAfter(wx.MessageBox, _(f"Error reading log: {e}"), _("Error"), wx.OK | wx.ICON_ERROR)
            time.sleep(1)

    def destroy_thread(self):
        self.running = False

    def get_selected_indices(self):
        indices = []
        item = -1
        while True:
            item = self.list_ctrl.GetNextItem(item, wx.LIST_NEXT_ALL, wx.LIST_STATE_SELECTED)
            if item == -1:
                break
            indices.append(item)
        return indices

    def delete_selected_items(self):
        selected = self.get_selected_indices()
        if not selected:
            return
        for i in sorted(selected, reverse=True):
            self.list_ctrl.DeleteItem(i)

    def clear_all_gui(self):
        self.list_ctrl.DeleteAllItems()

    def clear_log_file_and_gui(self):
        try:
            open(self.LOG_FILE, 'w').close()
            self.last_position = 0
            self.clear_all_gui()
            win32api.MessageBox(None, _("Log file and view cleared."), _("Info"), win32con.MB_OK | win32con.MB_ICONINFORMATION | win32con.MB_SYSTEMMODAL)
        except Exception as e:
            win32api.MessageBox(None, _("Clear failed: {e}").format(e=e), _("Error"), win32con.MB_OK | win32con.MB_ICONERROR | win32con.MB_SYSTEMMODAL)

    def copy_selected_to_clipboard(self):
        selected = self.get_selected_indices()
        if not selected:
            return
        texts = []
        for idx in sorted(selected):
            time_stamp = self.list_ctrl.GetItemText(idx, 0)
            info_level = self.list_ctrl.GetItemText(idx, 1)
            pid_number = self.list_ctrl.GetItemText(idx, 2)
            msg_record = self.list_ctrl.GetItemText(idx, 3)
            if time_stamp and info_level and pid_number.isdigit():
                texts.append(f"{time_stamp} - {info_level} - PID {pid_number} : {msg_record}")
            else:
                texts.append(msg_record or time_stamp)

        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject("\n".join(texts)))
            wx.TheClipboard.Close()


class VNT_YamlConfigEditor_Window(wx.Dialog):

    def __init__(self, parent, yaml_path: str):
        # Step 1: 使用逻辑设计尺寸初始化（100% DPI 下的理想值）
        logical_size = (600, 750)
        super().__init__(
            parent,
            title=_("VNT Config Editor (Advanced User Only)"),
            size=logical_size,
            style=wx.CAPTION | wx.STATIC_BORDER | wx.TAB_TRAVERSAL,
        )

        # Step 2: 获取屏幕客户区（不含任务栏等）
        x, y, screen_width, screen_height = wx.ClientDisplayRect()

        # Step 3: 将逻辑尺寸转换为当前 DPI 下的实际像素
        scaled_width, scaled_height = self.FromDIP(logical_size)

        # Step 4: 限制窗口不超过屏幕客户区（留一点边距更友好）
        margin = self.FromDIP(20)  # 留 20 逻辑像素边距
        max_width = min(scaled_width, screen_width - margin)
        max_height = min(scaled_height, screen_height - margin)

        # 至少保证最小可用尺寸
        min_width, min_height = self.FromDIP((400, 300))
        final_width = max(min_width, max_width)
        final_height = max(min_height, max_height)

        # Step 5: 应用最终尺寸
        self.SetSize(int(final_width), int(final_height))

        # 可选：设置最小尺寸防止过度缩小
        self.SetMinSize((int(min_width), int(min_height)))

        self.yaml_path = yaml_path
        self.original_data = {}
        self.current_full_key = None  # e.g., ('server_address',) or ('dns', 0)
        self.parent_win = parent

        self.load_yaml()

        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # ===== 第1行：Tree =====
        self.tree = wx.TreeCtrl(self, style=wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT)
        main_sizer.Add(self.tree, 1, wx.EXPAND | wx.ALL, 5)

        # ===== 第2行：单行文本编辑框 =====
        self.value_text = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)

        # 安全地计算单行高度（必须在窗口初始化后）
        font = self.value_text.GetFont()
        dc = wx.ClientDC(self.value_text)
        dc.SetFont(font)
        text_height = dc.GetTextExtent("Wy")[1]
        single_line_height = text_height + self.FromDIP(12)  # 12 是逻辑像素间距
        self.value_text.SetMinSize((-1, single_line_height))
        self.value_text.SetMaxSize((-1, single_line_height))
        main_sizer.Add(self.value_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)

        # ===== 第3行：三个等宽按钮，横向居中 =====
        btn_labels = [_("Save and exit..."), _("Save As ..."), _("Close")]
        self.save_btn = wx.Button(self, label=btn_labels[0])
        self.save_as_btn = wx.Button(self, label=btn_labels[1])
        self.close_btn = wx.Button(self, label=btn_labels[2])

        if not self.original_data:
            self.save_btn.Disable()
            self.save_as_btn.Disable()
        else:
            self.save_btn.Enable()
            self.save_as_btn.Enable()

        # 计算最大按钮宽度（在窗口初始化后安全）
        temp_dc = wx.ClientDC(self)
        temp_dc.SetFont(wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT))
        max_width = 0
        padding = self.FromDIP(30)  # 逻辑 padding 30 → 缩放
        for label in btn_labels:
            w, k = temp_dc.GetTextExtent(label)
            total_w = w + padding
            if total_w > max_width:
                max_width = total_w

        for btn in [self.save_btn, self.save_as_btn, self.close_btn]:
            btn.SetMinSize((int(max_width), -1))

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.Add(self.save_btn, 0, wx.RIGHT, 5)
        btn_sizer.Add(self.save_as_btn, 0, wx.RIGHT, 5)
        btn_sizer.Add(self.close_btn, 0)

        center_wrapper = wx.BoxSizer(wx.HORIZONTAL)
        center_wrapper.AddStretchSpacer()
        center_wrapper.Add(btn_sizer, 0, wx.ALIGN_CENTER_VERTICAL)
        center_wrapper.AddStretchSpacer()
        main_sizer.Add(center_wrapper, 0, wx.EXPAND | wx.ALL, 5)

        self.SetSizer(main_sizer)

        # Bind events
        self.Bind(wx.EVT_TREE_SEL_CHANGED, self.on_tree_select, self.tree)
        self.save_btn.Bind(wx.EVT_BUTTON, self.on_save)
        self.save_as_btn.Bind(wx.EVT_BUTTON, self.on_save_as)
        self.close_btn.Bind(wx.EVT_BUTTON, self.on_close)
        self.value_text.Bind(wx.EVT_TEXT_ENTER, self.on_text_enter)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

        # Build tree
        root_label = os.path.splitext(os.path.basename(self.yaml_path))[0]
        if not root_label.strip():
            root_label = _("root")
        self.root = self.tree.AddRoot(root_label)
        self.populate_tree(self.original_data, self.root, ())
        self.tree.Expand(self.root)

        self.Layout()
        self.CenterOnScreen()

    def on_char_hook(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.Hide()
        else:
            event.Skip()  # 允许其他按键正常处理

    def load_yaml(self):
        try:
            with open(self.yaml_path, 'r', encoding='utf-8') as f:
                self.original_data = yaml.safe_load(f) or {}
        except Exception as e:
            win32api.MessageBox(None, _("Failed to load YAML:\n{e}").format(e=e), _("Error"), win32con.MB_OK | win32con.MB_ICONERROR | win32con.MB_SYSTEMMODAL)
            self.original_data = {}

    def is_tcp_key(self, key_path: tuple) -> bool:
        return len(key_path) == 1 and key_path[0] == 'tcp'

    def get_server_address_value(self) -> str:
        val = self.get_nested_value(self.original_data, ('server_address',))
        return val if isinstance(val, str) else ""

    def compute_tcp_value(self) -> bool:
        addr = self.get_server_address_value()
        return addr.startswith("tcp://")

    def populate_tree(self, data: Union[Dict, List], parent_item, key_path: tuple):
        # VNT2 完整参数字段列表（基于 vnt2_conf_toml_example.toml）
        all_important_keys = {
            # 核心必填字段
            'network_code', 'server',
            # 设备配置
            'device_id', 'device_name', 'password', 'ip',
            # 网络优化
            'compress', 'rtx', 'fec', 'no_punch',
            # 高级网络配置
            'input', 'output', 'no_nat', 'port_mapping', 'allow_mapping',
            # 端口和MTU
            'ctrl_port', 'tunnel_port', 'mtu',
            # TUN网卡
            'tun_name', 'no_tun',
            # 安全配置
            'cert_mode',
            # STUN配置
            'udp_stun', 'tcp_stun',
        }

        
        # 主UI中直接显示的字段（需要标红）
        main_ui_keys = {
            'network_code',      # Token
            'device_id',         # DeviceID
            'device_name',       # DeviceID (同名)
            'server',            # ServerIPPort + Protocol
            'password',          # Network_Password
            'ip',                # VirtualIP
            'compress',          # Compression
            'cert_mode',         # 证书验证模式（高级编辑器管理）
        }

        if isinstance(data, dict):
            # 如果是根节点（key_path为空），确保所有重要字段都显示
            if len(key_path) == 0 and parent_item == self.root:
                # VNT1 遗留字段列表（需要过滤掉）
                vnt1_legacy_keys = {'token', 'server_address', 'name', 'cipher_model', 'server_encrypt', 'compressor'}
                
                # 先显示已存在的字段（过滤掉VNT1遗留字段）
                existing_keys = set()
                for key, value in data.items():
                    # 跳过VNT1遗留字段
                    if key in vnt1_legacy_keys:
                        continue
                    
                    existing_keys.add(key)
                    new_key_path = key_path + (key,)
                    is_main_ui = key in main_ui_keys

                    if len(new_key_path) == 1 and key == 'tcp':
                        computed_tcp = self.compute_tcp_value()
                        display_val = str(computed_tcp)
                        item = self.tree.AppendItem(parent_item, f"{key}: {display_val}")
                        if is_main_ui:
                            self.tree.SetItemTextColour(item, wx.RED)

                    else:
                        display_val = self.preview_value(value)
                        item = self.tree.AppendItem(parent_item, f"{key}: {display_val}")

                        is_leaf = not isinstance(value, (dict, list))
                        if is_leaf and is_main_ui:
                            self.tree.SetItemTextColour(item, wx.RED)

                        if isinstance(value, (dict, list)) and not (len(new_key_path) == 1 and key == 'tcp'):
                            self.populate_tree(value, item, new_key_path)

                # 再添加缺失的重要字段（使用默认值）
                for key in sorted(all_important_keys):
                    if key not in existing_keys and key != 'tcp':  # tcp是计算字段，不手动添加
                        default_value = self._get_default_value(key)
                        new_key_path = (key,)
                        display_val = self.preview_value(default_value)
                        item = self.tree.AppendItem(parent_item, f"{key}: {display_val}")
                        
                        # 只有主UI字段才标红
                        if key in main_ui_keys:
                            self.tree.SetItemTextColour(item, wx.RED)
                        
                        # 将默认值添加到原始数据中
                        data[key] = default_value
            else:
                # 非根节点，按原逻辑处理
                for key, value in data.items():
                    new_key_path = key_path + (key,)
                    is_main_ui = key in main_ui_keys

                    if len(new_key_path) == 1 and key == 'tcp':
                        computed_tcp = self.compute_tcp_value()
                        display_val = str(computed_tcp)
                        item = self.tree.AppendItem(parent_item, f"{key}: {display_val}")
                        if is_main_ui:
                            self.tree.SetItemTextColour(item, wx.RED)
                    else:
                        display_val = self.preview_value(value)
                        item = self.tree.AppendItem(parent_item, f"{key}: {display_val}")

                        is_leaf = not isinstance(value, (dict, list))
                        if is_leaf and is_main_ui:
                            self.tree.SetItemTextColour(item, wx.RED)

                        if isinstance(value, (dict, list)) and not (len(new_key_path) == 1 and key == 'tcp'):
                            self.populate_tree(value, item, new_key_path)

        elif isinstance(data, list):
            for idx, value in enumerate(data):
                new_key_path = key_path + (idx,)
                item = self.tree.AppendItem(parent_item, f"[{idx}]: {self.preview_value(value)}")
                if isinstance(value, (dict, list)):
                    self.populate_tree(value, item, new_key_path)

    def _get_default_value(self, key: str):
        """根据字段名返回合理的默认值"""
        defaults = {
            # 字符串类型
            'network_code': '',
            'server': 'quic://',
            'device_id': '',
            'device_name': '',
            'password': '',
            'ip': '',
            'tun_name': 'vnt-tun',
            'cert_mode': 'skip',
            # 布尔类型
            'compress': False,
            'rtx': False,
            'fec': False,
            'no_punch': False,
            'no_tun': False,

            'no_nat': False,
            'allow_mapping': False,
            # 整数类型
            'ctrl_port': 11233,
            'tunnel_port': 0,
            'mtu': 1400,
            # 列表类型
            'input': [],
            'output': [],
            'port_mapping': [],
            'udp_stun': [],
            'tcp_stun': [],
        }
        return defaults.get(key, '')

    def preview_value(self, value) -> str:
        if isinstance(value, (dict, list)):
            return "<...>"
        elif isinstance(value, str):
            return f'"{value}"'
        else:
            return str(value)

    def on_tree_select(self, event):
        item = event.GetItem()
        if not item or item == self.root:
            self.value_text.Clear()
            self.current_full_key = None
            self.value_text.SetEditable(True)
            return

        key_path = []
        current = item
        while current != self.root:
            label = self.tree.GetItemText(current)
            if ': ' in label:
                key_part = label.split(': ', 1)[0]
                if key_part.startswith('[') and key_part.endswith(']'):
                    try:
                        idx = int(key_part[1:-1])
                        key_path.insert(0, idx)
                    except ValueError:
                        key_path.insert(0, key_part)
                else:
                    key_path.insert(0, key_part)
            current = self.tree.GetItemParent(current)

        self.current_full_key = tuple(key_path)

        if self.is_tcp_key(self.current_full_key):
            tcp_val = self.compute_tcp_value()
            self.value_text.SetValue(str(tcp_val))
            self.value_text.SetEditable(False)
        else:
            value = self.get_nested_value(self.original_data, self.current_full_key)
            if value is not None:
                self.value_text.SetValue(str(value) if not isinstance(value, str) else value)
            else:
                self.value_text.Clear()
            self.value_text.SetEditable(True)

    def get_nested_value(self, data, key_path: tuple):
        try:
            for key in key_path:
                data = data[key]
            return data
        except (KeyError, IndexError, TypeError):
            return None

    def set_nested_value(self, data, key_path: tuple, new_value):
        try:
            for key in key_path[:-1]:
                data = data[key]
            last_key = key_path[-1]
            original = data[last_key]
            if isinstance(original, bool):
                if new_value.lower() in ('true', '1', 'yes'):
                    data[last_key] = True
                elif new_value.lower() in ('false', '0', 'no'):
                    data[last_key] = False
                else:
                    data[last_key] = new_value
            elif isinstance(original, int):
                data[last_key] = int(new_value)
            elif isinstance(original, float):
                data[last_key] = float(new_value)
            else:
                data[last_key] = new_value
        except Exception as e:
            win32api.MessageBox(None, _("Failed to update value:\n{e}").format(e=e), _("Error"), win32con.MB_OK | win32con.MB_ICONERROR | win32con.MB_SYSTEMMODAL)

    def on_text_enter(self, event):
        self.apply_value_change()
        event.Skip()

    def apply_value_change(self):
        if not self.current_full_key or self.is_tcp_key(self.current_full_key):
            return

        new_val = self.value_text.GetValue()
        current_val = self.get_nested_value(self.original_data, self.current_full_key)

        if str(current_val) == new_val:
            return

        self.set_nested_value(self.original_data, self.current_full_key, new_val)

        if self.current_full_key == ('server_address',):
            self.original_data['tcp'] = self.compute_tcp_value()

        # === 关键：保存当前展开状态 ===
        expanded_paths = self._get_expanded_paths()

        # 重建树（不展开）
        self.tree.DeleteChildren(self.root)
        self.populate_tree(self.original_data, self.root, ())

        # === 恢复展开状态 ===
        def expand_by_path(parent_item, path):
            if not path:
                return parent_item
            first = path[0]
            child, cookie = self.tree.GetFirstChild(parent_item)
            while child.IsOk():
                label = self.tree.GetItemText(child)
                key_part = label.split(': ', 1)[0] if ': ' in label else label

                match = False
                if isinstance(first, int) and key_part.startswith('[') and key_part.endswith(']'):
                    try:
                        idx = int(key_part[1:-1])
                        if idx == first:
                            match = True
                    except ValueError:
                        pass
                elif isinstance(first, str) and key_part == first:
                    match = True

                if match:
                    if len(path) == 1:
                        self.tree.Expand(child)
                        return child
                    else:
                        return expand_by_path(child, path[1:])
                child, cookie = self.tree.GetNextChild(parent_item, cookie)
            return None

        for path in expanded_paths:
            expand_by_path(self.root, path)

        # 重新选中当前项（可选）
        self.reselect_current_item()

    def reselect_current_item(self):
        if not self.current_full_key:
            return

        def find_item_by_path(parent_item, path):
            if not path:
                return parent_item
            first = path[0]
            child, cookie = self.tree.GetFirstChild(parent_item)
            while child.IsOk():
                label = self.tree.GetItemText(child)
                key_part = label.split(': ', 1)[0] if ': ' in label else label

                match = False
                if isinstance(first, int) and key_part.startswith('[') and key_part.endswith(']'):
                    try:
                        idx = int(key_part[1:-1])
                        if idx == first:
                            match = True
                    except ValueError:
                        pass
                elif isinstance(first, str) and key_part == first:
                    match = True


                if match:
                    return find_item_by_path(child, path[1:])
                child, cookie = self.tree.GetNextChild(parent_item, cookie)
            return None

        item = find_item_by_path(self.root, self.current_full_key)
        if item and item.IsOk():
            self.tree.SelectItem(item)

    def on_save(self, event):
        self.apply_value_change()  # 确保最后一次回车被应用（如果用户没按回车，这里也不会保存未确认的修改）
        self.save_yaml(self.yaml_path)
        self.EndModal(wx.ID_OK)

    def on_save_as(self, event):
        self.apply_value_change()  # 同样，只保存已回车确认的修改
        # 获取当前yaml文件所在目录作为默认目录
        default_dir = os.path.dirname(self.yaml_path) if self.yaml_path and os.path.exists(self.yaml_path) else self.parent_win.workingdir
        with wx.FileDialog(
            self, "Save YAML file", defaultDir=default_dir, wildcard="YAML files (*.yaml;*.yml)|*.yaml;*.yml",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT
        ) as fileDialog:
            if fileDialog.ShowModal() == wx.ID_CANCEL:
                return
            pathname = fileDialog.GetPath()
            self.save_yaml(pathname)

    def save_yaml(self, path):
        try:
            # VNT1 遗留字段列表（保存前需要清除）
            vnt1_legacy_keys = {'token', 'server_address', 'name', 'cipher_model', 'server_encrypt', 'compressor'}
            
            # 创建数据副本并清除VNT1遗留字段
            clean_data = {k: v for k, v in self.original_data.items() if k not in vnt1_legacy_keys}
            
            with open(path, 'w', encoding='utf-8') as f:
                yaml.dump(clean_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            win32api.MessageBox(None, _("Saved successfully!"), _("Info"), win32con.MB_OK | win32con.MB_ICONINFORMATION | win32con.MB_SYSTEMMODAL)
        except Exception as e:
            win32api.MessageBox(None, _("Failed to save:\n{e}").format(e=e), _("Error"), win32con.MB_OK | win32con.MB_ICONERROR | win32con.MB_SYSTEMMODAL)

    def _get_expanded_paths(self, item=None, parent_path=()):
        """递归获取所有已展开的非叶子节点的 key 路径"""
        if item is None:
            item = self.root

        expanded_paths = []
        child, cookie = self.tree.GetFirstChild(item)
        while child.IsOk():
            label = self.tree.GetItemText(child)
            key_part = label.split(': ', 1)[0] if ': ' in label else label

            # 解析 key_part 为原始类型（str 或 int）
            if key_part.startswith('[') and key_part.endswith(']'):
                try:
                    idx = int(key_part[1:-1])
                    current_key = idx
                except ValueError:
                    current_key = key_part
            else:
                current_key = key_part

            current_path = parent_path + (current_key,)

            # 如果该子项是容器（dict/list），且当前处于展开状态
            has_children = self.tree.ItemHasChildren(child)
            is_expanded = self.tree.IsExpanded(child)

            if has_children and is_expanded:
                expanded_paths.append(current_path)
                # 递归子级
                expanded_paths.extend(self._get_expanded_paths(child, current_path))

            child, cookie = self.tree.GetNextChild(item, cookie)

        return expanded_paths

    def on_close(self, event):
        self.EndModal(wx.ID_CANCEL)


class VNT_ManageProfile_Frame(wx.Dialog):

    def __init__(self, parent, vnt_app=None):
        # Step 1: 使用逻辑尺寸初始化父类
        super().__init__(
            parent,
            id=wx.ID_ANY,
            title=_(u"Manage VNT Profiles"),
            pos=wx.DefaultPosition,
            size=wx.Size(600, 300),  # 逻辑尺�����（100% DPI）
            style=wx.STAY_ON_TOP | wx.CAPTION | wx.STATIC_BORDER | wx.TAB_TRAVERSAL
        )

        # Step 2: 应用 DPI 缩放到窗口大小
        logical_size = (600, 300)
        scaled_size = self.FromDIP(logical_size)
        self.SetSize(scaled_size)

        self.SetSizeHints(wx.DefaultSize, wx.DefaultSize)

        # === 主布局：垂直 ===
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # 上半部分：水平布局（左：列表，右：按钮）
        top_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # 左侧 ListBox —— ��度自适应剩余空间
        self.m_listBox1 = wx.ListBox(
            self,
            choices=[],
            style=wx.LB_SINGLE | wx.LB_NEEDED_SB
        )
        top_sizer.Add(self.m_listBox1, 1, wx.ALL | wx.EXPAND, 5)

        # 右侧按钮区域 —— 固定宽度，约比默认宽50%
        BUTTON_AREA_WIDTH_LOGICAL = 120  # 默认按钮宽度约80，120 ≈ +50%
        BUTTON_AREA_WIDTH_SCALED = self.FromDIP(BUTTON_AREA_WIDTH_LOGICAL)  # DPI 缩放

        button_sizer = wx.BoxSizer(wx.VERTICAL)
        self.btn_new = wx.Button(self, label=_(u"New..."))
        self.btn_apply = wx.Button(self, label=_(u"Apply..."))
        self.btn_open = wx.Button(self, label=_(u"Edit"))
        self.btn_import = wx.Button(self, label=_(u"Import"))
        self.btn_delete = wx.Button(self, label=_(u"Delete"))
        self.btn_close = wx.Button(self, label=_(u"Close"))

        self.btn_open.Enable(False)
        self.btn_apply.Enable(False)
        self.btn_delete.Enable(False)

        # === 为 New �� Apply 添加深色背景 ===
        self.btn_new.SetBackgroundColour(wx.Colour(200, 200, 200))
        self.btn_apply.SetBackgroundColour(wx.Colour(200, 200, 200))
        # 文字颜色保持为黑色（深灰背景+黑色文字可读性好）
        self.btn_new.SetForegroundColour(wx.BLACK)
        self.btn_apply.SetForegroundColour(wx.BLACK)

        # 设置每个按钮的最小宽度
        for btn in [self.btn_new, self.btn_apply, self.btn_open, self.btn_import, self.btn_delete, self.btn_close]:
            btn.SetMinSize(wx.Size(BUTTON_AREA_WIDTH_SCALED - self.FromDIP(10), -1))

        # === 添加分��线 ===
        # 在Apply和Open之间
        line1 = wx.StaticLine(self, wx.ID_ANY, size=(-1, self.FromDIP(5)), style=wx.LI_HORIZONTAL)
        line1.SetMinSize((BUTTON_AREA_WIDTH_SCALED, self.FromDIP(1)))  # 确保分隔线宽度与按钮一致

        # === 按钮添加顺序 ===
        button_sizer.Add(self.btn_new, 1, wx.ALL | wx.EXPAND, self.FromDIP(2))
        button_sizer.Add(self.btn_apply, 1, wx.ALL | wx.EXPAND, self.FromDIP(2))
        button_sizer.Add(line1, 0, wx.ALL | wx.EXPAND, self.FromDIP(2))  # 分隔线
        button_sizer.Add(self.btn_open, 1, wx.ALL | wx.EXPAND, self.FromDIP(2))
        button_sizer.Add(self.btn_import, 1, wx.ALL | wx.EXPAND, self.FromDIP(2))
        button_sizer.Add(self.btn_delete, 1, wx.ALL | wx.EXPAND, self.FromDIP(2))
        button_sizer.Add(self.btn_close, 1, wx.ALL | wx.EXPAND, self.FromDIP(2))

        # 添加按钮区域到 top_sizer���指定固定宽度
        top_sizer.Add(button_sizer, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, self.FromDIP(5))

        main_sizer.Add(top_sizer, 1, wx.EXPAND)

        # 底部 StaticText：横贯窗口，居中
        self.m_staticText_info = wx.StaticText(
            self,
            label=_(u"Current profile:"),
            style=wx.ALIGN_LEFT
        )
        main_sizer.Add(self.m_staticText_info, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, self.FromDIP(10))

        self.SetSizer(main_sizer)
        self.Layout()
        self.Centre(wx.BOTH)

        # ===== 以下逻辑完全保持不变 =====
        self.m_listBox1.Bind(wx.EVT_LISTBOX, self.on_selection_changed)
        self.m_listBox1.Bind(wx.EVT_LISTBOX_DCLICK, self.on_listbox_dclick)
        self.btn_new.Bind(wx.EVT_BUTTON, self.on_new)
        self.btn_open.Bind(wx.EVT_BUTTON, self.on_open)
        self.btn_import.Bind(wx.EVT_BUTTON, self.on_import)
        self.btn_apply.Bind(wx.EVT_BUTTON, self.on_apply)
        self.btn_delete.Bind(wx.EVT_BUTTON, self.on_delete)
        self.btn_close.Bind(wx.EVT_BUTTON, self.on_close)

        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

        self.vnt_app = vnt_app
        if vnt_app:
            self.config_fn = vnt_app.config_fn
            self.workingdir = vnt_app.workingdir
            self.logger = vnt_app.logger
        else:
            self.config_fn = None
            self.logger = None

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        if vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML) is None or not os.path.exists(vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)):
            p = "Not Available"
        else:
            p = os.path.splitext(os.path.basename(vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)))[0]
        self.m_staticText_info.SetLabel(f"{_(u'Current profile:')} {p}")

        self._load_profiles()

    def __del__(self):
        pass

    def on_char_hook(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.Hide()
        else:
            event.Skip()  # 允许其他按键正常处理

    def on_new(self, event):
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        previous_conf = vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)
        vnt_conf.set_value(VNT_Config.KEY_VNT_PREV_PROFILE, previous_conf)
        vnt_conf.set_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML, '')
        self.EndModal(wx.ID_CANCEL)
        time.sleep(0.5)
        self.vnt_app.main_window.Show(True)
        return

    def on_import(self, event):
        # VNT2 配置验证字段（基于 vnt2_conf_toml_example.toml）
        important_keys = {'network_code', 'server', 'device_id', 'device_name', 'password', 'ip', 'compress'}


        def extract_all_keys(data: Union[Dict, List]) -> Set[str]:
            """递归提取数据中所有字典的键名（去重）"""
            keys = set()
            if isinstance(data, dict):
                keys.update(data.keys())
                for value in data.values():
                    keys.update(extract_all_keys(value))
            elif isinstance(data, list):
                for item in data:
                    keys.update(extract_all_keys(item))
            return keys

        def is_valid_setting_profile(yaml_path: str, important_keys: Set[str]) -> bool:

            try:
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                    if data is None:
                        return False  # 空文件
            except Exception:
                return False  # 文件不存在或格式错误

            all_keys = extract_all_keys(data)
            return important_keys.issubset(all_keys)

        with wx.FileDialog(
            self, message="Import setting YAML file", wildcard="YAML files (*.yaml;*.yml)|*.yaml;*.yml", defaultDir=self.workingdir,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST
        ) as fileDialog:
            if fileDialog.ShowModal() == wx.ID_CANCEL:
                return
            pathname = fileDialog.GetPath()
            if is_valid_setting_profile(pathname, important_keys):
                if pathname is not None and os.path.exists(pathname):
                    target_path = os.path.join(self.workingdir, os.path.basename(pathname))

                    if os.path.normpath(os.path.dirname(pathname)) != os.path.normpath(self.workingdir):  # 如果源路径不在当前工作目录中
                        if os.path.exists(target_path):
                            hwnd = self.GetHandle()  # 👈 关键：提供父窗口句柄
                            msg = _("The file '{}' already exists in the current directory.\nDo you want to overwrite it?").format(os.path.basename(target_path))
                            # Ask user whether to overwrite
                            result = win32api.MessageBox(hwnd, msg, _("Confirm Overwrite"), win32con.MB_YESNO | win32con.MB_ICONQUESTION)
                            if result == win32con.IDYES:
                                shutil.copy2(pathname, target_path)
                            # else: user chose No – do nothing
                        else:
                            # No conflict, just copy
                            shutil.copy2(pathname, target_path)

                    if self.update_profile_list(os.path.join(self.workingdir, self.config_fn), VNT_Config.KEY_VNT_PROFILE_LIST, os.path.splitext(os.path.basename(pathname))[0], 'add'):
                        self.logger.write(f"Profile list updated with {os.path.splitext(os.path.basename(pathname))[0]}")
                        self._load_profiles()
                    else:
                        self.logger.write(f"Error updating profile list with {os.path.splitext(os.path.basename(pathname))[0]}, probably already exists", "debug")
            else:
                win32api.MessageBox(None, _("The selected file does not appear to be a valid VNT setting profile:\n{pathname}").format(pathname=pathname), _("Error"), win32con.MB_OK | win32con.MB_ICONERROR | win32con.MB_SYSTEMMODAL)

    def on_delete(self, event):

        def remove_from_semicolon_string(s, target):
            """
            从分号分隔的字符串中移除指定项（精确匹配），并返回清理后的字符串。

            参数:
                s (str): 原始分号分隔字符串，如 "russ_1;russ_0;russ2;russ"
                target (str): 要移除的子串，如 "russ_0"
            返回:
                str: 移除后的字符串，无首尾分号，无空段，如 "russ_1;russ2;russ"
            """
            if not s:
                return ""

            # 分割、去空格、过滤空字符串
            parts = [part.strip() for part in s.split(';') if part.strip()]

            # 精确匹配移除（注意：不是子串匹配！）
            filtered = [part for part in parts if part != target]

            # 重新用分号连接
            return ';'.join(filtered)

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        list_of_profiles = vnt_conf.get_value(VNT_Config.KEY_VNT_PROFILE_LIST)

        selected_indices = self.m_listBox1.GetSelections()
        selected_item = self.get_selected_items()[0]
        if not selected_indices:
            return  # 不应发生，因按钮已禁用

        fn = os.path.join(self.workingdir, f"{selected_item}.yaml")

        if win32api.MessageBox(None, _("Are you sure to delete the selected profile?\n\nThis will also delete the file:\n{fn}").format(fn=fn), _("Confirmation"), win32con.MB_OKCANCEL | win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL) != win32con.IDOK:
            return

        for idx in sorted(selected_indices, reverse=True):
            self.m_listBox1.Delete(idx)

        try:
            os.remove(fn)
        except OSError as e:
            if self.logger:
                self.logger.write(f"Delete profile {fn}: {e}", "critical")
            win32api.MessageBox(None, _("Failed to delete profile file:\n{e}").format(e=e), _("Error"), win32con.MB_OK | win32con.MB_ICONERROR | win32con.MB_SYSTEMMODAL)

        new_list = remove_from_semicolon_string(list_of_profiles, selected_item)
        if not vnt_conf.set_value(VNT_Config.KEY_VNT_PROFILE_LIST, new_list):
            win32api.MessageBox(None, _("Failed to update profile list in:\n{fn}\nYou may need to manually update it").format(fn=fn), _("Error"), win32con.MB_OK | win32con.MB_ICONERROR | win32con.MB_SYSTEMMODAL)
        # 👇 删除后选中清空，需更新按钮状态
        self.on_selection_changed(event)

    def on_open(self, event):
        fn = os.path.join(self.workingdir, f"{self.get_selected_items()[0]}.yaml")
        editor = VNT_YamlConfigEditor_Window(self, fn)
        editor.ShowModal()
        VNT_Main_Window.set_window_topmost(editor)
        editor.Destroy()
        return

    def on_apply(self, event):
        fn = os.path.join(self.workingdir, f"{self.get_selected_items()[0]}.yaml")
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        previous_conf = vnt_conf.get_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML)
        vnt_conf.set_value(VNT_Config.KEY_VNT_PREV_PROFILE, previous_conf)
        vnt_conf.set_value(VNT_Config.KEY_VNT_CONNECTION_CONFIG_YAML, fn)
        self.EndModal(wx.ID_CANCEL)
        self.vnt_app.main_window.Show(True)
        return

    def on_selection_changed(self, event):
        """当 ListBox 选中项变化时调用"""
        selected_count = len(self.m_listBox1.GetSelections())
        enabled = selected_count > 0
        self.btn_open.Enable(enabled)
        self.btn_delete.Enable(enabled)
        self.btn_apply.Enable(enabled)
        event.Skip()

    def on_listbox_dclick(self, event):
        if self.btn_open.IsEnabled():
            self.on_open(event)

    def on_close(self, event):
        """关闭对话框"""
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        vnt_conf.set_value(VNT_Config.KEY_VNT_PREV_PROFILE, '')
        self.EndModal(wx.ID_CANCEL)  # 或 wx.ID_OK，根据需求

    def get_selected_items(self):
        """获取当前被选中（高亮）的项文本列表"""
        selected_indices = self.m_listBox1.GetSelections()
        return [self.m_listBox1.GetString(i) for i in selected_indices]

    def _get_all_items(self):
        """获取 ListBox 中所有项的列表"""
        return [self.m_listBox1.GetString(i) for i in range(self.m_listBox1.GetCount())]

    def _load_profiles(self):

        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
        raw_value = vnt_conf.get_value(VNT_Config.KEY_VNT_PROFILE_LIST)

        if raw_value is None or raw_value == '':
            return

        profiles = []
        if isinstance(raw_value, str):
            profiles = [s.strip() for s in raw_value.split(';') if s.strip()]

        self.m_listBox1.Clear()
        for profile in profiles:
            self.m_listBox1.Append(profile)

    @staticmethod
    def update_profile_list(yaml_path, key, target_str, action):
        """
        在 YAML 文件中对指定 key 的分号分隔字符串值进行添加或删除操作。

        参数:
            yaml_path (str or Path): YAML 文件路径
            key (str): 要操作的键名
            target_str (str): 要添加或删除的子字符串（会自动 strip）
            action (str): 操作类型，必须是 'add' 或 'remove'

        返回:
            bool:
                - action='add': True 表示成功添加（之前不存在），False 表示已存在未变
                - action='remove': True 表示成功删除（之前存在），False 表示不存在未变

        异常:
            ValueError: 当 action 不是 'add'/'remove'，或 target_str 为空
            OSError: 文件读写错误
        """
        if action not in ('add', 'remove'):
            raise ValueError("action must be 'add' or 'remove'")

        target_str = target_str.strip()
        if not target_str:
            raise ValueError("target_str cannot be empty or whitespace-only")

        yaml_path = Path(yaml_path)

        # 1. 读取现有数据（若文件不存在，则 data = {}）
        if yaml_path.exists():
            try:
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
            except (yaml.YAMLError, UnicodeDecodeError):
                data = {}
        else:
            data = {}

        # 确保 data 是 dict
        if not isinstance(data, dict):
            data = {}

        # 2. 解析当前值为干净的字符串列表
        current_value = data.get(key)
        print(f"current_value: {current_value}")

        if current_value is None:
            items = []
        elif isinstance(current_value, str):
            # 分割、去空格、过滤空段
            items = [s.strip() for s in current_value.split(';') if s.strip()]
        else:
            # 非字符串值（如数字、bool、list）视为无效，重置为空
            items = []

        print(f"items: {items}")

        # 3. 执行操作
        changed = False
        if action == 'add':
            if target_str not in items:
                items.append(target_str)
                changed = True
        elif action == 'remove':
            if target_str in items:
                items.remove(target_str)
                changed = True

        print(f"items updated: {items}")

        # 4. 写回值：空列表 → 空字符串，否则用 ; 连接
        data[key] = ';'.join(items) if items else ''
        # 5. 写入文件
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(
                data,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False  # 保持 key 顺序（Python 3.7+ dict 有序）
            )
            f.flush()
            os.fsync(f.fileno())

        return changed


class AboutDialog(wx.Dialog):

    def __init__(self, parent,
                 title="About",
                 icon_path="",
                 app_name="VNT Helper",
                 version="v1.0.0",
                 description="A powerful tool for managing VNT profiles.",
                 url="https://example.com",
                 quote="Simplicity is the ultimate sophistication. — Leonardo da Vinci"):
        class ClickableURL(wx.StaticText):
            def __init__(self, parent, url):
                super().__init__(parent, label=url, style=wx.ST_ELLIPSIZE_END)
                self.url = url
                self.SetForegroundColour(wx.BLUE)
                font = self.GetFont()
                font.SetUnderlined(True)
                self.SetFont(font)
                self.SetCursor(wx.Cursor(wx.CURSOR_HAND))

                self.Bind(wx.EVT_LEFT_UP, self._on_click)
                self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
                self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

            def _on_enter(self, event):
                self.SetCursor(wx.Cursor(wx.CURSOR_HAND))

            def _on_leave(self, event):
                self.SetCursor(wx.NullCursor)

            def _on_click(self, event):
                try:
                    webbrowser.open(self.url)
                except Exception as e:
                    win32api.MessageBox(None, _("Cannot open URL:\n{e}").format(e=e), _("Error"), win32con.MB_OK | win32con.MB_ICONERROR | win32con.MB_SYSTEMMODAL)

        style = wx.CAPTION | wx.STATIC_BORDER | wx.TAB_TRAVERSAL | wx.STAY_ON_TOP
        super(AboutDialog, self).__init__(parent, title=title, style=style)

        # === 主布局 ===
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.AddSpacer(15)

        # --- 1. 图标 + 文字区域（水平布局）---
        top_sizer = wx.BoxSizer(wx.HORIZONTAL)
        top_sizer.AddSpacer(20)

        # 图标
        if icon_path and os.path.exists(icon_path):
            try:
                icon_img = wx.Image(icon_path, wx.BITMAP_TYPE_ANY).Scale(64, 64, wx.IMAGE_QUALITY_HIGH)
                icon_bmp = icon_img.ConvertToBitmap()
                icon_static = wx.StaticBitmap(self, bitmap=icon_bmp)
                top_sizer.Add(icon_static, 0, wx.TOP)
                top_sizer.AddSpacer(15)
            except Exception as e:
                print(f"Failed to load icon: {e}")

        # 文字信息
        text_sizer = wx.BoxSizer(wx.VERTICAL)
        bold_font = self.GetFont().Bold()

        name_label = wx.StaticText(self, label=app_name)
        name_label.SetFont(bold_font)
        text_sizer.Add(name_label, 0, wx.BOTTOM, 4)

        version_label = wx.StaticText(self, label=_("Version INFO: {version}").format(version=version))
        text_sizer.Add(version_label, 0, wx.BOTTOM, 8)

        desc_label = wx.StaticText(self, label=description)
        text_sizer.Add(desc_label, 0, wx.BOTTOM, 8)

        self.url_label = ClickableURL(self, url=url)
        text_sizer.Add(self.url_label, 0, wx.BOTTOM, 10)

        top_sizer.Add(text_sizer, 1, wx.ALIGN_CENTER_VERTICAL)
        main_sizer.Add(top_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 20)

        # --- 2. 格言区域 ---
        main_sizer.AddSpacer(10)
        quote_label = wx.StaticText(self, label=quote, style=wx.ALIGN_CENTER)
        bold_font = self.GetFont().Bold()
        quote_label.SetFont(bold_font)
        quote_label.SetForegroundColour(wx.Colour(60, 60, 60))  # 灰色文字
        main_sizer.Add(quote_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 40)
        main_sizer.AddSpacer(15)

        # --- 3. OK 按钮（右下角）---
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn = wx.Button(self, wx.ID_OK, _("OK"))
        ok_btn.SetDefault()
        btn_sizer.Add(ok_btn, 0, wx.ALL, 5)
        main_sizer.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 15)

        self.SetSizerAndFit(main_sizer)
        self.CentreOnParent()
        self.Layout()

        # 绑定回车关闭（可选）
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key_down)

    def on_key_down(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE or event.GetKeyCode() == wx.WXK_RETURN:
            self.EndModal(wx.ID_OK)
        else:
            event.Skip()


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


class Bubble_Message():
    BUBBLE_EXIT_BUZZ_WORD = "bubble_message_queue_exit"
    MIN_STATUS_INTERVAL = 2  # 最小状态间隔时间（秒）

    def __init__(self, vnt_app):
        self.msg_q = queue.Queue()
        self.running = False
        self.thread = None
        self.workingdir = vnt_app.workingdir
        self.ico_path = os.path.join(self.workingdir, VNT_HELPER_ICON)
        self.logger = vnt_app.logger
        self.config_fn = vnt_app.config_fn
        self.vnt_app = vnt_app

    def __del__(self):
        pass

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._bubble_message_daemon)
            self.thread.daemon = True
            self.thread.start()

    def stop(self):
        self.msg_q.put(self.BUBBLE_EXIT_BUZZ_WORD)
        time.sleep(0.5)
        if self.thread:
            self.thread.join(timeout=1)
        self.running = False

    def is_alive(self):
        return self.running

    def msg(self, msgtxt):
        try:
            self.msg_q.put(msgtxt)
        except Exception as e:
            self.logger.write(f"Message queue {e}", 'critical')

    def _bubble_message_daemon(self):
        LAST_STATUS_MSG = None
        LAST_STATUS_TIME = 0  # Track the time of the last displayed message
        vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)

        while self.running:
            m = self.msg_q.get()

            if m == self.BUBBLE_EXIT_BUZZ_WORD:
                return

            if vnt_conf.get_value(VNT_Config.KEY_VNT_NOTIFICATION_ENABLED) and (not self.vnt_app.args.no_gui):
                try:
                    current_time = time.time()

                    # If message is the same as the last one and less than 10 seconds have passed,
                    # don't show it even if VIRTUAL_IP_TEXT is in the message
                    if m == LAST_STATUS_MSG and (current_time - LAST_STATUS_TIME) < self.MIN_STATUS_INTERVAL:
                        # Skip showing the message
                        pass
                    elif m != LAST_STATUS_MSG or VNT_Connection.VIRTUAL_IP_TEXT in m:
                        toast = Notification(app_id="VNT Network Helper", title=m.split('#')[0], msg=m.split('#')[1].rstrip(), icon=self.ico_path)
                        toast.show()
                        # Update the last message and time when actually showing a message
                        LAST_STATUS_MSG = m
                        LAST_STATUS_TIME = current_time

                except Exception as e:
                    self.logger.write(f"Bubble message {e}", 'debug')

            else:
                self.logger.write(f"Message [{m.replace('#', ' : ')}] received in slience mode")

            time.sleep(0.5)


class Registry_Taskschedule_for_AutoRun():
    TASK_BUZZ_WORD_SESSION_1 = "VNT_SESSION_1"
    DEFAULT_REG_HIVE = win32con.HKEY_LOCAL_MACHINE
    DEFAULT_WINREG_KEY = r"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run"
    DEFAULT_REG_CONTENT = "schtasks /run /tn " + TASK_BUZZ_WORD_SESSION_1

    def __init__(self, vntapp, workingdir, logger, no_gui_flag, debug_mode, kill_flag, task_name1=TASK_BUZZ_WORD_SESSION_1):
        self.task_name_1 = task_name1

        self.workingdir = workingdir
        self.vnt_app = vntapp
        self.config_fn = vntapp.config_fn

        self.task_arg = ''
        s1 = "-b" if no_gui_flag else ''
        s2 = '-d' if debug_mode else ''
        s3 = '-k' if kill_flag else ''
        arg_txt = [s for s in [s1, s2, s3] if s]
        self.task_arg = ' '.join(arg_txt)

        self.task_description = "Run VNT with admin from Registry"
        self.task_current_user = self._get_current_user()
        self.logger = logger
        self.TaskScheduler_Succss = True
        self.Regedit_Success = True

    def _get_current_user(self):
        username = ctypes.create_unicode_buffer(1024)
        user_len = ctypes.c_ulong(1024)
        ctypes.windll.advapi32.GetUserNameW(username, ctypes.byref(user_len))
        return username.value

    def _check_task_existence(self, task_name):
        scheduler = win32com.client.Dispatch('Schedule.Service')
        scheduler.Connect()
        root_folder = scheduler.GetFolder('\\')
        tasks = root_folder.GetTasks(1)  # 1 including hidden task

        for task in tasks:
            if task.Name == task_name:
                return True

        return False

    def _create_taskschedule_item(self, usr, action_txt, action_args='', description='', task_name=TASK_BUZZ_WORD_SESSION_1):

        if self._check_task_existence(task_name):
            return True

        scheduler = win32com.client.Dispatch('Schedule.Service')
        scheduler.Connect()
        root_folder = scheduler.GetFolder('\\')
        task_def = scheduler.NewTask(0)

        # 设置任务描述
        scheduler.Connect()
        task_def.RegistrationInfo.Description = description
        task_def.RegistrationInfo.Author = usr
        task_def.Principal.RunLevel = 1
        task_def.Settings.Compatibility = 4  # WIN10

        '''
        trigger = task_def.Triggers.Create(2)  # 2 表示每天触发
        trigger.DaysInterval = 1
        trigger.StartBoundary = (datetime.datetime.now() + datetime.timedelta(minutes=10)).isoformat()
        '''

        # 设置任务动作
        action = task_def.Actions.Create(0)  # 0 表示执行程序
        action.Path = action_txt  # 替换为你的程序路径
        # 确保参数中包含 -k 标志
        if action_args == "":
            action_args = "-k"
        elif "-k" not in action_args:
            action_args = action_args + " -k"

        action.Arguments = action_args  # 程序参数

        task_def.Settings.DisallowStartIfOnBatteries = False
        task_def.Settings.StopIfGoingOnBatteries = False
        task_def.Settings.WakeToRun = False
        try:
            root_folder.RegisterTaskDefinition(
                task_name,
                task_def,
                6,           # TASK_CREATE_OR_UPDATE
                None,        # USER
                None,        # PASSWD
                0)
            self.logger.write(f'Task "{task_name}" added successfully.')
            return True
        except Exception as e:
            self.logger.write(f"Task Writing Error {e}", "debug")
            return False

    def _remove_task(self, task_name):
        scheduler = win32com.client.Dispatch('Schedule.Service')
        scheduler.Connect()
        root_folder = scheduler.GetFolder('\\')
        try:
            root_folder.DeleteTask(task_name, 0)
            self.logger.write(f'Task "{task_name}" deleted successfully.', 'info')
            return True
        except Exception as e:
            self.logger.write(f'Deleting task: {e}', 'critical')
            return False

    def _handle_autostart_registry(self, mode, reg_hive=DEFAULT_REG_HIVE, reg_name=TASK_BUZZ_WORD_SESSION_1, reg_content=DEFAULT_REG_CONTENT, KeyName=DEFAULT_WINREG_KEY):

        key = win32api.RegOpenKey(reg_hive, KeyName, 0, win32con.KEY_ALL_ACCESS)

        if mode.lower() == "add":
            try:
                win32api.RegSetValueEx(key, reg_name, 0, win32con.REG_SZ, reg_content)
                win32api.RegCloseKey(key)
                self.logger.write(f"Registry \\\\LOCALMACHINE\\\\{KeyName}")
                self.logger.write(f"RegName [{reg_name}], Value [{reg_content}]")
                return True
            except Exception as e:
                self.logger.write(f"Adding registry {e}", "critical")
                return False

        elif mode.lower() == "remove":
            try:
                win32api.RegDeleteValue(key, reg_name)
                win32api.RegCloseKey(key)
                self.logger.write(f"Registry {reg_name} Removed")
                return True
            except Exception as e:
                self.logger.write(f"Removing registry {e}", 'critical')
                return False

        elif mode.lower() == "status":
            try:
                location, type = win32api.RegQueryValueEx(key, reg_name)
                win32api.RegCloseKey(key)
                return True
            except Exception as e:
                self.logger.write(f"Registry Query Info: {e}", 'debug')
                return False

        else:
            self.logger.write("Unsupported action in handling regeistry")
            return False

    def _set_vnt_services_autorun_vnt_cli(self, state):
        try:
            if os.path.exists(os.path.join(self.workingdir, self.config_fn)):  # in case removed by reset
                vnt_conf = VNT_Config(self.workingdir, self.config_fn, self.logger)
                vnt_conf.set_value(VNT_Config.KEY_AUTORUN_CLI_ON_STARTUP, state)
            return True
        except Exception as e:
            self.logger.write(f"Failed to set autorun CLI state to {state}: {e}", 'critical')
            return False

    def is_autorun_on(self):
        try:
            if self._handle_autostart_registry("status") and self._check_task_existence(self.task_name_1):
                return True
            else:
                return False
        except Exception as e:
            self.logger.write(f"Setting up autostart  {e}", 'critical')
            return False

    def add_autorun(self):
        self.TaskScheduler_Succss = True
        self.Regedit_Success = True
        self.Service_Success = True

        try:
            if self._check_task_existence(self.task_name_1):
                self.TaskScheduler_Succss = self._remove_task(self.task_name_1)

            self.TaskScheduler_Succss = self.TaskScheduler_Succss and \
                self._create_taskschedule_item(self.task_current_user, os.path.join(self.workingdir, os.path.basename(sys.argv[0])), self.task_arg, self.task_description, self.task_name_1)

            if not self.TaskScheduler_Succss:
                self.logger.write("Error Writing TaskScheduler!", "critical")
                return False

            if self._handle_autostart_registry("status") is True:
                self.Regedit_Success = self._handle_autostart_registry("remove")

            self.Regedit_Success = self.Regedit_Success and self._handle_autostart_registry("add")

            if not self.Regedit_Success:
                self.logger.write("Error Writing Registry!", "critical")
                return False

            self.Service_Success = self._set_vnt_services_autorun_vnt_cli(True)

            if self.logger is not None:
                self.logger.write(f"task {self.TaskScheduler_Succss}, registry {self.Regedit_Success}, service {self.Service_Success}", 'debug')
            return self.TaskScheduler_Succss and self.Regedit_Success and self.Service_Success

        except Exception as e:
            self.logger.write(f"Add autorun error {e}", 'critical')
            return False

    def remove_autorun(self):
        self.TaskScheduler_Succss = True
        self.Regedit_Success = True
        self.Service_Success = True

        try:
            if self._check_task_existence(self.task_name_1):
                self.TaskScheduler_Succss = self._remove_task(self.task_name_1)
            if self._handle_autostart_registry("status") is True:
                self.Regedit_Success = self._handle_autostart_registry("remove")
                if not self.Regedit_Success:
                    self.logger.write("Remove Registry Itme Error", 'critical')

            self.Service_Success = self._set_vnt_services_autorun_vnt_cli(False)

            if self.logger is not None:
                self.logger.write(f"task {self.TaskScheduler_Succss}, registry {self.Regedit_Success}, service {self.Service_Success}", 'debug')

            return self.TaskScheduler_Succss and self.Regedit_Success and self.Service_Success
        except Exception as e:
            self.logger.write(f"Remove autorun error {e}", 'critical')
            return False


if __name__ == '__main__':
    vnt_helper = VNT_Helper_App()
    vnt_helper.start()
    print("\nVNT_Helper is about to exit...")
    sys.exit(0)
