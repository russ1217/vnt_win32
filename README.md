# VNT Helper for Windows

A native Windows application for managing VNT (Virtual Network Tool) networks, built with **wxPython** and designed specifically for the Windows ecosystem.

## 🎯 About This Application

### Not a Web-Based App
This is **NOT** a Flutter web application wrapped in a Windows container. Instead, it's a **true native Windows application** built with:

- **wxPython**: A mature Python GUI framework that creates authentic Win32-style interfaces
- **Windows System Services**: Leverages Windows Service infrastructure for reliable background operations
- **Task Scheduler Integration**: Uses Windows Task Scheduler for automated management tasks
- **Native IPC Communication**: Direct inter-process communication using Windows mechanisms

### Why Native Windows Style?
This application embraces the Windows philosophy by:

✅ **System Services**: Runs as a proper Windows Service for automatic startup and reliability  
✅ **Task Scheduler**: Integrates with Windows Task Scheduler for scheduled operations  
✅ **Win32 UI Style**: Follows Windows UI conventions and user expectations  
✅ **Process Management**: Uses Windows-native process control and monitoring  
✅ **Event Logging**: Integrates with Windows Event Log system  
✅ **Registry Integration**: Proper Windows registry usage for configuration  

## 🚀 Features

- **VNT Network Management**: Complete control over VNT virtual network connections
- **Service-Based Architecture**: Reliable background daemon running as Windows Service
- **Real-time Monitoring**: Live network status, IP assignment, and connection quality
- **Auto-Reconnection**: Intelligent reconnection with configurable intervals
- **Update Management**: Seamless self-update with graceful shutdown procedures
- **Log Viewer**: Dual-format log parsing supporting both Python and Rust applications
- **IP Notification**: Banner notifications when IP addresses are assigned
- **Graceful Shutdown**: Coordinated shutdown following Windows service best practices

## 🏗️ Architecture

```
┌─────────────────────────────────────────┐
│         VNT Helper GUI (wxPython)       │
│         - Win32 Native Interface        │
│         - Real-time Status Display      │
│         - Configuration Management      │
└──────────────┬──────────────────────────┘
               │ IPC (Named Pipes)
┌──────────────▼──────────────────────────┐
│      VNT Daemon (Windows Service)       │
│      - Background Network Management    │
│      - Auto-reconnection Logic          │
│      - Process Monitoring               │
└──────────────┬──────────────────────────┘
               │ Command Execution
┌──────────────▼──────────────────────────┐
│         vnt2_cli (Rust Binary)          │
│         - VNT Protocol Implementation   │
│         - Network Operations            │
└─────────────────────────────────────────┘
```

## 💻 Technical Stack

- **GUI Framework**: wxPython 4.x (Phoenix)
- **Language**: Python 3.8+
- **Background Service**: pywin32 (Windows Service API)
- **IPC Mechanism**: Named Pipes / Sockets
- **Network Client**: vnt2_cli (Rust-based)
- **Packaging**: PyInstaller
- **Updater**: Custom Windows-aware update system

## 🔧 Installation

1. Download the latest release from [GitHub Releases](https://github.com/russ1217/vnt_win32/releases)
2. Extract the archive to your desired location
3. Run `vnt_helper.exe` as Administrator (required for service installation)
4. The application will automatically install the Windows Service on first run

## 📋 Requirements

- **OS**: Windows 10/11 (64-bit)
- **Privileges**: Administrator rights (for service installation)
- **Network**: Internet access for VNT connection
- **Dependencies**: All bundled in the executable (no Python installation required)

## 🎮 Usage

### First Time Setup
1. Launch `vnt_helper.exe` with administrator privileges
2. Configure your VNT server address and authentication token
3. Click "Start" to begin the VNT connection
4. The application will install the Windows Service automatically

### Daily Operation
- **Start/Stop**: Control VNT network connection from the main interface
- **Monitor**: View real-time connection status and assigned IP addresses
- **Logs**: Access detailed logs through the built-in log viewer
- **Settings**: Customize reconnection intervals and other parameters

### Service Management
The application manages a Windows Service named "VNTDaemon":
- Automatically starts on system boot (if configured)
- Runs in the background independent of the GUI
- Can be managed via Windows Services MMC snap-in (`services.msc`)

## 🔄 Update Process

The application features an intelligent update system:

1. **Check for Updates**: Periodically checks for new versions
2. **Graceful Shutdown**: 
   - Stops network connections
   - Sends shutdown signal to daemon
   - Waits for clean process termination (20-30 seconds)
   - Uninstalls Windows Service
3. **Download & Install**: Downloads and extracts new version
4. **Restart**: Launches updated application automatically

## 📝 Log Formats

The log viewer supports dual format parsing:

**Python Logging Format** (application logs):
```
2026-05-20 14:44:15,976 - INFO - PID 3720 : message text here
```

**Rust/vnt2_cli Format** (CLI output):
```
2026-05-20T14:44:15.933124700+08:00 INFO message text here
```

Both formats are automatically detected and displayed in a unified interface.

## 🔒 Security Considerations

- Runs with minimal required privileges
- Secure IPC communication between components
- Validates update checksums before installation
- No external dependencies beyond bundled libraries

## 🐛 Troubleshooting

### Service Installation Fails
- Ensure you're running as Administrator
- Check Windows Event Log for service-related errors
- Verify no antivirus is blocking service creation

### Connection Issues
- Verify firewall allows VNT traffic
- Check server address and token configuration
- Review logs for detailed error messages

### Update Problems
- Ensure stable internet connection
- Check available disk space
- Review updater logs in the application directory

## 📄 License

[Specify your license here]

## 🤝 Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## 📧 Support

For issues and questions, please visit:
- [GitHub Issues](https://github.com/russ1217/vnt_win32/issues)
- [GitHub Discussions](https://github.com/russ1217/vnt_win32/discussions)

---

**Built with ❤️ for Windows users who appreciate native applications**
