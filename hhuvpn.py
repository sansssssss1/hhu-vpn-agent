#!/usr/bin/env python3
"""hhuvpn 启动器：python hhuvpn.py <子命令> [--json]

Windows 上也可以直接用 hhuvpn.cmd（把项目目录加进 PATH 后即可全局调用 hhuvpn）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hhuvpn.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
