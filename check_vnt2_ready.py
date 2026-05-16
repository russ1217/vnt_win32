#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""快速检查 vnt2_cli.exe 是否就绪"""

from pathlib import Path
import sys

working_dir = Path(__file__).parent
vnt2_cli = working_dir / "vnt2_cli.exe"

print("=" * 60)
print("VNT2 CLI 就绪检查")
print("=" * 60)

if vnt2_cli.exists():
    print(f"\n✅ vnt2_cli.exe 已就绪")
    print(f"   位置: {vnt2_cli}")
    print(f"   大小: {vnt2_cli.stat().st_size:,} bytes")
    print(f"\n现在可以重启服务:")
    print(f"   net stop VNTDaemonService")
    print(f"   net start VNTDaemonService")
    sys.exit(0)
else:
    print(f"\n❌ vnt2_cli.exe 缺失")
    print(f"   期望位置: {vnt2_cli}")
    print(f"\n请执行以下操作:")
    print(f"   1. 获取 VNT 2.0 客户端程序")
    print(f"   2. 重命名为 vnt2_cli.exe")
    print(f"   3. 放置到: {working_dir}")
    print(f"   4. 重启 VNTDaemonService 服务")
    print(f"\n详见: VNT2_CLI_MISSING_SOLUTION.md")
    sys.exit(1)
