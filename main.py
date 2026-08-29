"""auto_search 命令行入口（也是打包成 exe 后的入口）。"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from auto_search.config import CONFIG_TEMPLATE, ConfigError, load_config
from auto_search.runner import run


def default_config_path() -> Path:
    """打包成 exe 后取 exe 同目录；源码运行时取当前目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.yaml"
    return Path.cwd() / "config.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="auto_search",
        description="医学数据库自动检索工具 (chaoslib -> Embase: 检索/打印PDF/导出CSV)",
    )
    parser.add_argument("-c", "--config", default=None,
                        help="配置文件路径 (默认: exe 或命令行所在目录的 config.yaml)")
    parser.add_argument("-o", "--output-dir", default=None, help="输出目录（覆盖配置文件中的 output_dir）")
    parser.add_argument("--headed", action="store_true",
                        help="显示浏览器窗口运行（默认无头；注意: 导出PDF需要无头模式）")
    parser.add_argument("--ask", action="store_true", help="忽略配置中的账号密码，运行时手动输入")
    parser.add_argument("--cli", action="store_true",
                        help="纯命令行模式：完全按 config.yaml 运行，不打开图形界面")
    args = parser.parse_args()

    cfg_path = Path(args.config) if args.config else default_config_path()

    # 默认打开图形界面；--cli 或无显示环境(如服务器)时走纯命令行
    if not args.cli:
        try:
            from auto_search.gui import run_gui
            return run_gui(cfg_path)
        except Exception as e:  # noqa: BLE001 - 无显示环境等
            print(f"图形界面不可用({e})，改用命令行模式。")

    if not cfg_path.exists():
        cfg_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"未找到配置文件，已生成模板: {cfg_path}")
        print("请填写账号、检索式等信息后重新运行。")
        return 2

    try:
        cfg = load_config(cfg_path, output_dir=args.output_dir,
                          force_headed=args.headed, ask=args.ask)
    except ConfigError as e:
        print(f"配置错误: {e}")
        return 2

    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("\n已被用户取消。")
        return 130
    except Exception as e:  # noqa: BLE001 - exe 场景给用户友好报错而非堆栈
        print(f"运行出错: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
