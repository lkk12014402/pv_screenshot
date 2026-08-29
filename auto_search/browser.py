"""浏览器启动与上下文创建。"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from playwright.async_api import Browser, BrowserContext

from .config import AppConfig


def ensure_browsers_path(logger: logging.Logger | None = None) -> None:
    """打包成 exe 后，playwright 默认会在解包临时目录里找浏览器(必然找不到)。

    冻结模式下把 PLAYWRIGHT_BROWSERS_PATH 固定到 exe 同目录的 ms-playwright，
    用户只需把浏览器文件放该目录(或预先设置同名环境变量)。源码运行则保持默认。
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    if getattr(sys, "frozen", False):
        p = Path(sys.executable).resolve().parent / "ms-playwright"
        p.mkdir(exist_ok=True)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(p)
        if logger:
            logger.info("exe 模式: 内置浏览器目录为 %s", p)


def _proxy_from_env() -> dict[str, str] | None:
    """若环境变量里配置了代理(http_proxy/https_proxy)，则让浏览器走该代理。"""
    proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if proxy:
        return {"server": proxy}
    return None


async def launch_browser(pw, cfg: AppConfig, logger: logging.Logger) -> Browser:
    """按配置启动浏览器；指定的系统浏览器不可用时回退到内置 Chromium。"""
    kwargs = {
        "headless": cfg.browser.headless,
        "slow_mo": cfg.browser.slow_mo,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    proxy = _proxy_from_env()
    if proxy:
        kwargs["proxy"] = proxy
        logger.info("检测到代理环境变量，浏览器将使用代理: %s", proxy["server"])
    channel = cfg.browser.channel
    try:
        if channel in ("msedge", "chrome"):
            logger.info("使用系统浏览器 channel=%s (headless=%s)", channel, cfg.browser.headless)
            return await pw.chromium.launch(channel=channel, **kwargs)
        # channel="chromium": 完整版 Chromium 新无头模式，指纹更接近真实浏览器，
        # 比默认的 headless shell 更不容易被 Cloudflare 等风控识别
        logger.info("使用内置完整版 Chromium (headless=%s)", cfg.browser.headless)
        return await pw.chromium.launch(channel="chromium", **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning("channel=%s 启动失败(%s)，回退到内置 Chromium headless shell", channel, e)
        try:
            return await pw.chromium.launch(**kwargs)
        except Exception as e2:  # noqa: BLE001
            if "Executable doesn't exist" in str(e2):
                raise RuntimeError(
                    "未找到可用的浏览器。请二选一：\n"
                    "1) config.yaml 的 browser.channel 设为 msedge 或 chrome（用系统已装浏览器，推荐）；\n"
                    "2) 使用内置浏览器：源码运行执行 `playwright install chromium`；"
                    "exe 运行则把浏览器文件放到 exe 同目录的 ms-playwright 文件夹"
                    "（可在已安装过的机器上复制该文件夹过来）。"
                ) from e2
            raise


async def new_context(browser: Browser, cfg: AppConfig) -> BrowserContext:
    """新建浏览器上下文：允许下载、忽略证书错误(WebVPN 常见)、统一超时。

    为降低被 Cloudflare 等风控识别的概率：
    - 屏蔽 navigator.webdriver 等自动化特征
    - 内置 Chromium 时把 UA 伪装成同版本 Windows Chrome(平台差异无法完全消除)
    """
    ua = cfg.browser.user_agent
    if not ua and cfg.browser.channel == "chromium":
        ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/{browser.version} Safari/537.36")
    ctx = await browser.new_context(
        accept_downloads=True,
        ignore_https_errors=True,
        viewport={"width": 1600, "height": 900},
        locale=cfg.browser.locale,
        timezone_id=cfg.browser.timezone,
        user_agent=ua or None,
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    ctx.set_default_timeout(30000)
    ctx.set_default_navigation_timeout(60000)
    return ctx
