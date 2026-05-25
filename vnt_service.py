# vnt_service.py
# Windows Service wrapper for vnt_daemon
import os
import sys
import time
import threading
import win32serviceutil
import win32service
import win32event
import servicemanager
import traceback
import winreg
from vnt_daemon import VNTDaemon


class VNTService(win32serviceutil.ServiceFramework):
    _svc_name_ = "VNTDaemonService"
    _svc_display_name_ = "VNT Daemon Service"
    _svc_description_ = "VNT Daemon Service for managing VPN connections"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.running = True
        self._daemon_run_returned = False  # Track when daemon.run() completes
        # Do not initialize daemon here to avoid import issues during service registration
        self.daemon = None

    def logger_write(self, message):
        """Simple logging helper for the service"""
        try:
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STOPPED if "stop" in message.lower() else servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, message)
            )
        except Exception:
            pass  # Ignore logging errors

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        
        # Signal the daemon to stop gracefully
        if self.daemon:
            try:
                # Set daemon's running flag to False - this will cause its main loop to exit
                self.daemon.running = False
                self.logger_write("SvcStop: Signaled daemon to shutdown")
            except Exception as e:
                self.logger_write(f"SvcStop: Error signaling daemon: {e}")
        
        # Set service stop event so main() loop can detect it
        self.running = False
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        # Import and initialize the original daemon functionality only when service actually starts
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            if current_dir not in sys.path:
                sys.path.insert(0, current_dir)

            # Only import when the service actually starts
            # from vnt_daemon import VNTDaemon
            self.daemon = VNTDaemon()
        except Exception as e:
            # Log error and exit
            print(f"Error initializing daemon: {e}")
            print(traceback.format_exc())
            return

        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ''))
        self.main()

    def main(self):
        # Start the daemon's main functionality in a separate thread
        # This allows the service stop signal to be processed while daemon is running
        daemon_thread = None
        if self.daemon:
            daemon_thread = threading.Thread(target=self._run_daemon, daemon=True)
            daemon_thread.start()

        # Keep the service running until stop is requested
        while self.running:
            if win32event.WaitForSingleObject(self.hWaitStop, 1000) == win32event.WAIT_OBJECT_0:
                break
        
        # Wait for daemon thread to finish (with timeout)
        if daemon_thread and daemon_thread.is_alive():
            self.logger_write("SvcDoRun: Waiting for daemon thread to exit...")
            daemon_thread.join(timeout=20)  # Wait up to 20 seconds
            if daemon_thread.is_alive():
                self.logger_write("SvcDoRun: Daemon thread did not exit within timeout")
            else:
                self.logger_write("SvcDoRun: Daemon thread exited successfully")

    def _run_daemon(self):
        """Wrapper to run daemon and track when it completes"""
        try:
            if self.daemon:
                self.daemon.run()
        except Exception as e:
            self.logger_write(f"Daemon run error: {e}")
        finally:
            # Mark that daemon has returned
            self._daemon_run_returned = True

    @staticmethod
    def update_registry_description():
        try:
            key_path = f"SYSTEM\\CurrentControlSet\\Services\\{VNTService._svc_name_}"
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_WRITE)
            winreg.SetValueEx(key, "Description", 0, winreg.REG_SZ, VNTService._svc_description_)
            winreg.CloseKey(key)
            print("Service description updated successfully in registry.")
        except FileNotFoundError:
            print(f"Registry key not found: {key_path}. Service may not be installed yet.")
        except PermissionError:
            print("Permission denied when trying to update registry. Please run as administrator.")
        except Exception as e:
            print(f"Failed to update service description in registry: {e}")


if __name__ == '__main__':
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(VNTService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        # 检查是否是安装命令
        if len(sys.argv) > 1 and sys.argv[1] in ('install', 'update'):
            # 先执行标准命令行处理
            win32serviceutil.HandleCommandLine(VNTService)
            time.sleep(2)
            VNTService.update_registry_description()
        else:
            win32serviceutil.HandleCommandLine(VNTService)
