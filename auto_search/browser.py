"""浏览器启动与上下文创建。"""
from __future__ import annotations

import logging
import os

from playwright.async_api import Browser, BrowserContext

from .config import AppConfig


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
        return await pw.chromium.launch(**kwargs)


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
