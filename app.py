"""统一入口。

- 带命令行参数：走 CLI（等价于 python main.py <参数>）
- 无参数（如双击）：打开图形界面
"""
from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from main import main as cli_main

        return cli_main()

    from gui import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
