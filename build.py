"""
ScreenToBrowser 打包脚本
使用 PyInstaller 将项目打包为单文件 exe。

用法:
    python build.py
    # 或
    pyinstaller --noconsole --onefile --name ScreenToBrowser main.py
"""

import os
import subprocess
import sys
from pathlib import Path

# Windows 控制台 UTF-8 输出
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_DIR = Path(__file__).parent
MAIN_SCRIPT = PROJECT_DIR / "main.py"
ENTRY_NAME = "ScreenToBrowser"


def build() -> None:
    print("=" * 56)
    print("  ScreenToBrowser 打包")
    print("=" * 56)

    # 确认依赖已安装
    try:
        import mss  # noqa: F401
        import aiohttp  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as e:
        print(f"\n缺少依赖: {e}")
        print("请先执行: pip install -r requirements.txt\n")
        sys.exit(1)

    # PyInstaller 参数
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconsole",           # 无控制台窗口
        "--onefile",             # 单文件
        "--name", ENTRY_NAME,
        "--clean",               # 清理临时文件
        "--noconfirm",           # 不询问确认
        str(MAIN_SCRIPT),
    ]

    # 打包 mss 截屏所需的动态库（通常 PyInstaller 会自动检测，
    # 但 mss 用 ctypes 加载平台库，需要确保包含）
    if sys.platform == "win32":
        # mss 在 Windows 上纯 Python，无需额外 DLL
        pass

    print(f"\n执行: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=str(PROJECT_DIR))

    if result.returncode == 0:
        exe_path = PROJECT_DIR / "dist" / f"{ENTRY_NAME}.exe"
        print("\n" + "=" * 56)
        print(f"  ✓ 打包成功!")
        print(f"  输出: {exe_path}")
        print("=" * 56)
    else:
        print("\n✗ 打包失败，请检查上方错误信息。")
        sys.exit(result.returncode)


if __name__ == "__main__":
    build()
