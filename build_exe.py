"""Windows 打包脚本（build_exe.bat 的 Python 版）。

用法（在项目目录、已安装 Python 3.10+ 的 Windows 机器上）:
    python build_exe.py

产出: dist\\auto_search.exe
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY = VENV / "Scripts" / "python.exe"  # Windows 虚拟环境里的 python


def run(cmd: list[str]) -> None:
    print(">>", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], cwd=ROOT, check=True)


def main() -> int:
    try:
        if not PY.exists():
            print("创建虚拟环境 .venv ...")
            run([sys.executable, "-m", "venv", str(VENV)])
        run([PY, "-m", "pip", "install", "--upgrade", "pip"])
        run([PY, "-m", "pip", "install", "-r", "requirements.txt", "pyinstaller"])
        run([PY, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
             "--name", "auto_search", "--collect-all", "playwright", "main.py"])
    except subprocess.CalledProcessError as e:
        print(f"\n打包失败(退出码 {e.returncode})，请把上面的错误信息反馈给开发者。")
        return 1
    print()
    print("=" * 56)
    print(" 打包完成: dist\\auto_search.exe")
    print(" 双击打开图形界面; 纯命令行模式: auto_search.exe --cli")
    print(" 默认使用系统 Edge 浏览器, 无需额外下载。")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
