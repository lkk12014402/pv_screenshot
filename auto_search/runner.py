"""按配置顺序执行任务：每个任务独立的浏览器上下文和输出目录。"""
from __future__ import annotations

import re
import time

from playwright.async_api import async_playwright

from .browser import launch_browser, new_context
from .config import AppConfig
from .sites import SITES
from .utils import get_logger


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "task"


async def run(cfg: AppConfig) -> int:
    logger = get_logger("auto_search")
    async with async_playwright() as pw:
        browser = await launch_browser(pw, cfg, logger)
        ok = True
        try:
            for task in cfg.tasks:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                task_name = _safe_name(task.name or f"{task.site}_{task.database}")
                outdir = cfg.output_dir / f"{task_name}_{stamp}"
                outdir.mkdir(parents=True, exist_ok=True)
                tlog = get_logger(f"auto_search.{task_name}", outdir / "run.log")
                tlog.info("任务输出目录: %s", outdir)

                site_cls = SITES.get(task.site)
                if site_cls is None:
                    tlog.error("未知站点: %s (可选: %s)", task.site, ", ".join(SITES))
                    ok = False
                    continue
                site = site_cls()
                if task.database not in site.flows:
                    tlog.error("站点 %s 暂不支持数据库 %s (可选: %s)",
                               task.site, task.database, ", ".join(site.flows))
                    ok = False
                    continue

                context = await new_context(browser, cfg)
                try:
                    page = await site.login(context, cfg.account, outdir, tlog)
                    flow_page = await site.open_database(
                        page, task.database, tlog, category=task.category)
                    flow = site.flows[task.database]()
                    await flow.run(flow_page, task, outdir, tlog)
                    tlog.info("任务完成: %s", task_name)
                except Exception as e:  # noqa: BLE001 - 单个任务失败不阻塞其余任务
                    ok = False
                    tlog.error("任务失败: %s", e)
                finally:
                    await context.close()
        finally:
            await browser.close()
    return 0 if ok else 1
