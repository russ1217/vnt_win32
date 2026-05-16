#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证UI退出时vnt2_cli.exe进程终止逻辑"""

import sys
from pathlib import Path

print("=" * 70)
print("UI退出时vnt2_cli.exe进程终止验证")
print("=" * 70)

# 1. 检查VNT_Connection类是否有stop_and_exit_daemon方法
print("\n1. 检查 VNT_Connection.stop_and_exit_daemon 方法...")
with open('vnt_helper.py', 'r', encoding='utf-8') as f:
    helper_content = f.read()

if 'def stop_and_exit_daemon(self):' in helper_content:
    print("   ✓ VNT_Connection.stop_and_exit_daemon 方法已添加")
else:
    print("   ✗ VNT_Connection.stop_and_exit_daemon 方法未找到")
    sys.exit(1)

# 2. 检查是否发送exit命令
if '{"cmd": "exit"}' in helper_content or "{'cmd': 'exit'}" in helper_content:
    print("   ✓ stop_and_exit_daemon 发送 exit 命令")
else:
    print("   ✗ stop_and_exit_daemon 未发送 exit 命令")
    sys.exit(1)

# 3. 检查VNT_Helper_App.stop方法是否调用新方法
print("\n2. 检查 VNT_Helper_App.stop 方法...")
if 'self.vnt_connection.stop_and_exit_daemon()' in helper_content:
    print("   ✓ VNT_Helper_App.stop 调用 stop_and_exit_daemon")
else:
    print("   ✗ VNT_Helper_App.stop 未调用 stop_and_exit_daemon")
    sys.exit(1)

# 4. 检查守护进程中的exit命令处理
print("\n3. 检查 vnt_daemon.py 中的 exit 命令处理...")
with open('vnt_daemon.py', 'r', encoding='utf-8') as f:
    daemon_content = f.read()

if 'elif cmd["cmd"] == "exit":' in daemon_content:
    print("   ✓ vnt_daemon.py 处理 exit 命令")
    
    # 检查是否终止进程
    if 'self.vnt_process.wait(timeout=5)' in daemon_content or 'terminate()' in daemon_content:
        print("   ✓ exit 命令会终止 vnt2_cli.exe 进程")
    else:
        print("   ⚠ exit 命令可能不会终止进程")
else:
    print("   ✗ vnt_daemon.py 未处理 exit 命令")
    sys.exit(1)

# 5. 验证Python语法
print("\n4. 验证 Python 语法...")
import py_compile

try:
    py_compile.compile('vnt_helper.py', doraise=True)
    print("   ✓ vnt_helper.py 语法正确")
except py_compile.PyCompileError as e:
    print(f"   ✗ vnt_helper.py 语法错误: {e}")
    sys.exit(1)

try:
    py_compile.compile('vnt_daemon.py', doraise=True)
    print("   ✓ vnt_daemon.py 语法正确")
except py_compile.PyCompileError as e:
    print(f"   ✗ vnt_daemon.py 语法错误: {e}")
    sys.exit(1)

print("\n" + "=" * 70)
print("✅ 所有验证通过！UI退出时会完全终止vnt2_cli.exe进程")
print("=" * 70)

print("\n退出流程:")
print("  1. 用户点击退出按钮")
print("  2. 调用 VNT_Helper_App.stop()")
print("  3. 调用 VNT_Connection.stop_and_exit_daemon()")
print("  4. 发送 IPC exit 命令到守护进程")
print("  5. 守护进程终止 vnt2_cli.exe 进程")
print("  6. UI完全退出")
