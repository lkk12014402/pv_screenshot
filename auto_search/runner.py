"""按配置顺序执行任务：每个任务独立的浏览器上下文和输出目录。"""
from __future__ import annotations

import asyncio
import re
import time

from playwright.async_api import async_playwright

from .browser import ensure_browsers_path, launch_browser, new_context
from .config import AppConfig
from .sites import SITES
from .utils import get_logger


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "task"


# 任务级重试: 这些错误多为网络/VPN 临时问题，值得整体重跑
TRANSIENT_RE = re.compile(
    r"Gateway Timeout|Bad Gateway|网关|多次尝试后仍无法进入|Timeout.*exceeded|白页|未渲染",
    re.I)


async def run(cfg: AppConfig) -> int:
    logger = get_logger("auto_search")
    ensure_browsers_path(logger)
    async with async_playwright() as pw:
        browser = await launch_browser(pw, cfg, logger)
        ok = True
        try:
            for task in cfg.tasks:
                if not await _run_task(browser, cfg, task, logger):
                    ok = False
        finally:
            await browser.close()
    return 0 if ok else 1


async def _run_task(browser, cfg: AppConfig, task, logger) -> bool:
    """执行单个任务；遇到临时性错误按 cfg.task_retries 整体重试。"""
    task_name = _safe_name(task.name or f"{task.site}_{task.database}")

    site_cls = SITES.get(task.site)
    if site_cls is None:
        logger.error("未知站点: %s (可选: %s)", task.site, ", ".join(SITES))
        return False
    site = site_cls()
    if task.database not in site.flows:
        logger.error("站点 %s 暂不支持数据库 %s (可选: %s)",
                     task.site, task.database, ", ".join(site.flows))
        return False

    max_attempts = 1 + cfg.task_retries
    for attempt in range(1, max_attempts + 1):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        suffix = f"_{stamp}" if attempt == 1 else f"_{stamp}_retry{attempt}"
        outdir = cfg.output_dir / f"{task_name}{suffix}"
        outdir.mkdir(parents=True, exist_ok=True)
        tlog = get_logger(f"auto_search.{task_name}.{stamp}", outdir / "run.log")
        tlog.info("任务输出目录: %s (第 %d/%d 次尝试)", outdir, attempt, max_attempts)

        context = await new_context(browser, cfg)
        try:
            page = await site.login(context, cfg.account, outdir, tlog)
            flow_page = await site.open_database(
                page, task.database, tlog, category=task.category)
            flow = site.flows[task.database]()
            await flow.run(flow_page, task, outdir, tlog,
                           headless=cfg.browser.headless)
            tlog.info("任务完成: %s", task_name)
            return True
        except Exception as e:  # noqa: BLE001 - 单个任务失败不阻塞其余任务
            tlog.error("任务失败: %s", e)
            if attempt < max_attempts and TRANSIENT_RE.search(str(e)):
                tlog.warning("错误疑似临时网络问题，60 秒后整体重试该任务")
                await asyncio.sleep(60)
                continue
            return False
        finally:
            await context.close()
    return False
