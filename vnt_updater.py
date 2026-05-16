# -*- coding: utf-8 -*-
import os
import sys
import time
import zipfile
import shutil
import subprocess
import logging
import logging.handlers
import argparse
import ctypes
import signal
from typing import List, Optional

import psutil
import win32api
import win32con
from win32comext.shell.shell import ShellExecuteEx


class VNT_Updater:
    DEFAULT_WORKING_DIR = '.'
    DEFAULT_UPDATE_ZIP = 'vnt_helper.zip'
    DEFAULT_EXE_NAME = 'vnt_helper.exe'
    DEFAULT_CLI_NAME = 'vnt-cli.exe'
    DEFAULT_FILES_TO_UPDATE = ['vnt_helper.exe', 'vnt-cli.exe', 'vnt_service.exe', 'wintun.dll']
    DEFAULT_LOG_FILE = 'vnt_cli.log'

    def __init__(self):
        self.working_dir: str = self.DEFAULT_WORKING_DIR
        self.resource_dir: Optional[str] = None
        self.files_to_update: List[str] = self.DEFAULT_FILES_TO_UPDATE.copy()
        self.update_zip: str = self.DEFAULT_UPDATE_ZIP
        self.main_exe: str = self.DEFAULT_EXE_NAME
        self.cli_exe: str = self.DEFAULT_CLI_NAME
        self.service_exe: str = 'vnt_service.exe'
        self.log_file: str = self.DEFAULT_LOG_FILE
        self.run_in_background: bool = False
        self.logger: Optional[logging.Logger] = None

    def parse_args(self) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("-d", "--dir", dest="working_dir", default=self.DEFAULT_WORKING_DIR,
                            help="Set working directory")
        parser.add_argument("-r", "--res", dest="resource_dir", default=None,
                            help="Set resource/temp directory")
        parser.add_argument("-n", "--names", dest="update_file_names",
                            default=','.join(self.DEFAULT_FILES_TO_UPDATE),
                            help="Comma-separated file names to update")
        parser.add_argument("-f", "--file", dest="main_file_name", default=self.DEFAULT_UPDATE_ZIP,
                            help="Main update ZIP package")
        parser.add_argument("-e", "--exe", dest="exe_file_name", default=self.DEFAULT_EXE_NAME,
                            help="Executable to run after update")
        parser.add_argument("-l", "--log", dest="log_file_name", default=self.DEFAULT_LOG_FILE,
                            help="Log file name")
        parser.add_argument("-b", "--background", action="store_true", dest="back", default=False,
                            help="Run in background (no GUI alerts)")

        args = parser.parse_args()

        self.working_dir = args.working_dir
        self.resource_dir = args.resource_dir
        self.files_to_update = [f.strip() for f in args.update_file_names.split(',')] if args.update_file_names else []
        self.update_zip = args.main_file_name
        self.main_exe = args.exe_file_name
        self.log_file = args.log_file_name
        self.run_in_background = args.back

    def setup_logger(self) -> None:
        log_path = os.path.join(self.working_dir, self.log_file)
        self.logger = logging.getLogger("VNT_Updater")
        self.logger.setLevel(logging.DEBUG)

        # Avoid adding multiple handlers if called repeatedly
        if not self.logger.handlers:
            handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=1024 * 1024, backupCount=3
            )
            formatter = logging.Formatter('%(asctime)s - %(levelname)-8s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def log(self, level: str, msg: str) -> None:
        if self.logger:
            getattr(self.logger, level.lower())(f"PID {os.getppid():<6} : {msg}")
            print(f"PID {os.getppid():<6} : {msg}")
        if not self.run_in_background and level.upper() == "CRITICAL":
            win32api.MessageBox(0, msg, "Updater Error", win32con.MB_OK | win32con.MB_ICONERROR)

    def run_as_admin(self) -> None:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return

        script = ''
        if not getattr(sys, 'frozen', False) and "__compiled__" not in globals() and os.environ.get("NUITKA_ONEFILE_PARENT") is None:
            script = os.path.abspath(sys.argv[0])

        args = ' '.join(sys.argv[1:]) if len(sys.argv) > 1 else ''
        try:
            ShellExecuteEx(
                lpFile=sys.executable,
                lpParameters=f'"{script}" {args}' if script else args,
                nShow=1,
                lpVerb='runas'
            )
            sys.exit(0)
        except Exception as e:
            print(f"\nFailed to run as admin: {e}\n")
            sys.exit(1)

    def get_running_pids(self, process_name: str) -> List[int]:
        pids = []
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if proc.info['name'] == process_name:
                    pids.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return pids

    def kill_processes(self, process_name: str) -> None:
        pids = self.get_running_pids(process_name)
        if not pids:
            return

        self.log("INFO", f"Found {len(pids)} instance(s) of {process_name}, attempting to terminate...")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGINT)
            except Exception as e:
                self.log("ERROR", f"Failed to kill PID {pid}: {e}")
                if not self.run_in_background:
                    win32api.MessageBox(0, "Failed to manage process. Run as admin?", "Error",
                                        win32con.MB_OK | win32con.MB_ICONERROR)
            finally:
                self.log("INFO", f"Killed PID {pid} : {process_name}")

    def wait_for_process_exit(self, process_name: str, max_wait_sec: int = 60, force_after_sec: int = 30) -> None:
        for i in range(max_wait_sec):
            if not self.get_running_pids(process_name):
                return
            if i == force_after_sec:
                self.kill_processes(process_name)
                if self.resource_dir:
                    self.clean_temp_folder()
            time.sleep(1)
            self.log("DEBUG", f"{process_name} still running (round #{i})")

    def clean_temp_folder(self) -> None:
        if not self.resource_dir or not os.path.isdir(self.resource_dir):
            return
        try:
            for item in os.listdir(self.resource_dir):
                path = os.path.join(self.resource_dir, item)
                if os.path.isfile(path):
                    os.remove(path)
                    self.log("INFO", f"Deleted temp file: {path}")
                elif os.path.isdir(path):
                    shutil.rmtree(path)
                    self.log("INFO", f"Deleted temp folder: {path}")
        except OSError as e:
            self.log("WARNING", f"Error cleaning temp folder: {e}")

    def delete_old_files(self) -> None:
        all_files = set(self.files_to_update + [self.main_exe])
        for filename in all_files:
            filepath = os.path.join(self.working_dir, filename)
            if not os.path.exists(filepath):
                continue
            try:
                # Special handling for service executable - stop service first
                if filename == self.service_exe and self.is_service_installed():
                    if self.get_service_status() == "RUNNING":
                        self.log("INFO", "Stopping VNT daemon service before file removal...")
                        self.stop_service()
                    self.log("INFO", "Uninstalling old VNT daemon service...")
                    self.uninstall_service()

                os.remove(filepath)
                self.log("INFO", f"Deleted old file: {filepath}")
            except OSError as e:
                self.log("CRITICAL", f"Failed to delete {filepath}: {e}")

        # Retry main exe deletion up to 10 times
        main_path = os.path.join(self.working_dir, self.main_exe)
        for attempt in range(11):
            if not os.path.exists(main_path):
                break
            try:
                os.remove(main_path)
                break
            except OSError as e:
                self.log("CRITICAL", f"Retry {attempt+1}/10 - Failed to remove {self.main_exe}: {e}")
                time.sleep(1)
        else:
            self.log("CRITICAL", f"Could not remove {self.main_exe} after 10 retries")
            sys.exit(1)

    def deploy_update(self) -> None:
        zip_path = os.path.join(self.working_dir, self.update_zip)
        if not os.path.isfile(zip_path):
            self.log("CRITICAL", f"Update package not found: {zip_path}")
            sys.exit(1)

        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(self.working_dir)
            self.log("INFO", "Successfully deployed update.")
        except Exception as e:
            self.log("CRITICAL", f"Failed to deploy update from {zip_path}: {e}")
            if not self.run_in_background:
                win32api.MessageBox(0, f"Update failed: {e}", "Status", win32con.MB_OK | win32con.MB_ICONERROR)
            sys.exit(1)

    def is_service_installed(self):
        """Check if the VNT daemon service is installed"""
        try:
            # Hide console window on Windows
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            result = subprocess.run(['sc', 'query', 'VNTDaemonService'], capture_output=True, text=True, cwd=self.working_dir, startupinfo=startupinfo)
            # If the command succeeds, the service exists
            return result.returncode == 0
        except Exception:
            return False

    def install_service(self):
        """Install the VNT daemon service"""
        try:
            # Stop the service if it's running
            if self.is_service_installed() and self.get_service_status() == "RUNNING":
                self.stop_service()

            # Uninstall the old service if it exists
            if self.is_service_installed():
                self.uninstall_service()

            # Install the new service using the command line interface
            service_path = os.path.join(self.working_dir, "vnt_service.exe")

            # Check if the service executable exists
            if not os.path.exists(service_path):
                self.log("CRITICAL", f"Service executable not found: {service_path}")
                return False

            # Hide console window on Windows
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            cmd = [service_path, 'install']
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.working_dir, startupinfo=startupinfo)

            if result.returncode == 0:
                self.log("INFO", "VNT daemon service installed successfully")
                return True
            else:
                self.log("CRITICAL", f"Failed to install VNT daemon service: {result.stderr}")
                return False
        except Exception as e:
            self.log("CRITICAL", f"Error installing VNT daemon service: {e}")
            return False

    def uninstall_service(self):
        """Uninstall the VNT daemon service"""
        try:
            # Stop the service if it's running
            if self.is_service_installed() and self.get_service_status() == "RUNNING":
                self.stop_service()

            # Remove the service using the command line interface
            service_path = os.path.join(self.working_dir, "vnt_service.exe")

            # Check if the service executable exists
            if not os.path.exists(service_path):
                self.log("CRITICAL", f"Service executable not found: {service_path}")
                return False

            # Hide console window on Windows
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            cmd = [service_path, 'remove']
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.working_dir, startupinfo=startupinfo)

            if result.returncode == 0:
                self.log("INFO", "VNT daemon service uninstalled successfully")
                return True
            else:
                self.log("CRITICAL", f"Failed to uninstall VNT daemon service: {result.stderr}")
                return False
        except Exception as e:
            self.log("CRITICAL", f"Error uninstalling VNT daemon service: {e}")
            return False

    def start_service(self):
        """Start the VNT daemon service"""
        try:
            if not self.is_service_installed():
                self.log("CRITICAL", "VNT daemon service is not installed")
                return False

            # Hide console window on Windows
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            result = subprocess.run(['net', 'start', 'VNTDaemonService'], capture_output=True, text=True, cwd=self.working_dir, startupinfo=startupinfo)
            if result.returncode == 0 or "服务已经启动" in result.stdout or "service was started" in result.stdout:
                self.log("INFO", "VNT daemon service started")
                return True
            else:
                self.log("CRITICAL", f"Failed to start VNT daemon service: {result.stderr}")
                return False
        except Exception as e:
            self.log("CRITICAL", f"Error starting VNT daemon service: {e}")
            return False

    def stop_service(self):
        """Stop the VNT daemon service"""
        try:
            if not self.is_service_installed():
                self.log("CRITICAL", "VNT daemon service is not installed")
                return False

            # Hide console window on Windows
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            result = subprocess.run(['net', 'stop', 'VNTDaemonService'], capture_output=True, text=True, cwd=self.working_dir, startupinfo=startupinfo)
            if result.returncode == 0 or "服务已经停止" in result.stdout or "service was stopped" in result.stdout:
                self.log("INFO", "VNT daemon service stopped")
                return True
            else:
                self.log("CRITICAL", f"Failed to stop VNT daemon service: {result.stderr}")
                return False
        except Exception as e:
            self.log("CRITICAL", f"Error stopping VNT daemon service: {e}")
            return False

    def get_service_status(self):
        """Get the current status of the VNT daemon service"""
        try:
            # Hide console window on Windows
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            result = subprocess.run(['sc', 'query', 'VNTDaemonService'], capture_output=True, text=True, cwd=self.working_dir, startupinfo=startupinfo)
            if result.returncode != 0:
                return "NOT_INSTALLED"

            output = result.stdout
            if "RUNNING" in output:
                return "RUNNING"
            elif "STOPPED" in output:
                return "STOPPED"
            elif "PAUSED" in output:
                return "PAUSED"
            elif "START_PENDING" in output:
                return "STARTING"
            elif "STOP_PENDING" in output:
                return "STOPPING"
            else:
                return "UNKNOWN"
        except Exception as e:
            self.log("CRITICAL", f"Error getting service status: {e}")
            return "ERROR"

    def launch_updated_program(self) -> None:
        exe_path = os.path.join(self.working_dir, self.main_exe)
        if not os.path.isfile(exe_path):
            self.log("CRITICAL", f"Updated executable not found: {exe_path}")
            sys.exit(1)

        os.chdir(self.working_dir)
        cmd = [exe_path, "-u"]
        if self.run_in_background:
            cmd.append("-b")

        try:
            proc = subprocess.Popen(cmd)
            self.log("INFO", f"Launched updated {self.DEFAULT_EXE_NAME} (PID: {proc.pid})")
            print(f"Restart VNT Helper PID: {proc.pid}")
            sys.exit(0)
        except OSError as e:
            self.log("CRITICAL", f"Failed to start updated {self.DEFAULT_EXE_NAME} : {e}")
            if not self.run_in_background:
                win32api.MessageBox(0, f"Failed to start {self.DEFAULT_EXE_NAME} : {e}", "Status",
                                    win32con.MB_OK | win32con.MB_ICONERROR)
            sys.exit(1)

    def run(self) -> None:
        self.parse_args()
        self.setup_logger()
        self.run_as_admin()

        # Wait and kill main helper
        self.wait_for_process_exit(self.main_exe, max_wait_sec=60, force_after_sec=30)

        # Kill daemon, service, and CLI (shorter timeout)
        # self.wait_for_process_exit(self.daemon_exe, max_wait_sec=11, force_after_sec=10)
        self.wait_for_process_exit(self.service_exe, max_wait_sec=11, force_after_sec=10)
        self.wait_for_process_exit(self.cli_exe, max_wait_sec=11, force_after_sec=10)

        # Remove old files
        self.delete_old_files()

        # Deploy new version
        self.deploy_update()

        # Launch updated program
        self.launch_updated_program()


if __name__ == '__main__':
    updater = VNT_Updater()
    updater.run()
