"""站点插件基类。新增站点/数据库时按此接口扩展。"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import BrowserContext, Page

if TYPE_CHECKING:
    from ..config import AccountConfig, TaskConfig


class SitePlugin(ABC):
    """一个站点（如 chaoslib）: 负责登录，以及打开某个数据库入口页面。"""

    name: str = ""
    # 数据库名 -> Flow 子类（如 {"embase": EmbaseFlow}）
    flows: dict[str, type["Flow"]] = {}

    @abstractmethod
    async def login(self, context: BrowserContext, account: "AccountConfig",
                    outdir: Path, logger: logging.Logger) -> Page:
        """登录站点，返回登录后的首页 Page。"""

    @abstractmethod
    async def open_database(self, page: Page, db_name: str,
                            logger: logging.Logger) -> Page:
        """从站点首页打开指定数据库（可能弹出新标签页），返回数据库页面。"""


class Flow(ABC):
    """一个数据库上的完整作业流程（检索 -> 过滤 -> 打印/导出）。"""

    name: str = ""

    @abstractmethod
    async def run(self, page: Page, task: "TaskConfig", outdir: Path,
                  logger: logging.Logger, headless: bool = True) -> None:
        """在数据库页面上执行完整任务。headless 指示当前浏览器是否无头(影响 PDF 打印方式)。"""
