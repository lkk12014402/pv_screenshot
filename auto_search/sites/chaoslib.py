"""混沌书苑 (chaoslib.com)：WebVPN 登录，并从首页打开数据库入口（Embase 等）。"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PWTimeout

from ..config import AccountConfig
from ..utils import dump_debug, fill_input_smart, first_visible, wait_ready
from .base import SitePlugin
from .embase import EmbaseFlow
from .pubmed import PubmedFlow


class ChaoslibSite(SitePlugin):
    name = "chaoslib"
    base_url = "https://www.chaoslib.com/"

    flows = {
        "embase": EmbaseFlow,
        "pubmed": PubmedFlow,
    }

    # 数据库名 -> 首页卡片上的标题文字
    CARD_TITLES = {
        "embase": "Embase",
        "pubmed": "Pubmed",
    }

    # 数据库名 -> 目标地址(webvpn 代理域名)中的关键字，用于确认跳转到了真正的入口。
    # 注意不能只用 "webvpn" 判断: 鉴权中转页(如 kns-cnki-net.webvpn.sjlib.cn/api)也含 webvpn
    HOST_KEYS = {
        "embase": "embase-com",
        "pubmed": "pubmed",
    }

    async def login(self, context: BrowserContext, account: AccountConfig,
                    outdir: Path, logger: logging.Logger) -> Page:
        page = await context.new_page()
        logger.info("打开 %s", self.base_url)
        await page.goto(self.base_url, wait_until="domcontentloaded")

        # 登录表单由页面 JS (layui) 渲染，等密码框出现再操作
        pwd = page.locator('input[type="password"]').first
        await pwd.wait_for(state="visible", timeout=30000)

        user = await first_visible(page, [
            'input[placeholder*="账"]',
            'input[name*="user" i]',
            '#login-box input[type="text"]',
            'input[type="text"]',
        ])
        await fill_input_smart(user, account.username)
        await fill_input_smart(pwd, account.password)

        # 勾选“我已阅读并同意《免责声明》”
        try:
            await page.get_by_text(re.compile("我已阅读并同意")).first.click(timeout=3000)
        except PWTimeout:
            logger.warning("未找到免责声明勾选框，继续尝试登录")

        btn = await first_visible(page, [
            '#login-box button:has-text("登")',
            'button:has-text("登 录")',
            'button:has-text("登录")',
            'a:has-text("登 录")',
            'text=/^登\\s*录$/',
        ])
        await btn.click()

        # 登录成功会跳转离开 /user/login
        if not await self._wait_login_ok(page, timeout=20000):
            # “账号已在其他设备登录”提示：点击“强制登录/强制下线”类按钮踢掉旧会话
            if await self._try_force_login(page, logger):
                if await self._wait_login_ok(page, timeout=20000):
                    await wait_ready(page)
                    logger.info("登录成功(已强制下线其他设备): %s", page.url)
                    await self._wait_home_ready(page, logger)
                    return page
            tip = ""
            for tip_sel in ("#login-enable-choose", ".include-box__tip", ".layui-layer-content"):
                try:
                    tip = (await page.locator(tip_sel).first.inner_text(timeout=1500)).strip()
                    if tip:
                        break
                except Exception:  # noqa: BLE001
                    continue
            await dump_debug(page, outdir, "login_failed", logger)
            raise RuntimeError(
                "登录可能失败，页面仍停留在登录页。" + (f"页面提示: {tip}" if tip else "请检查账号密码。")
            )
        await wait_ready(page)
        logger.info("登录成功: %s", page.url)
        await self._wait_home_ready(page, logger)
        return page

    @staticmethod
    async def _wait_home_ready(page: Page, logger: logging.Logger) -> None:
        """确认首页前端真正渲染出来；白页(资源加载失败)时刷新重试一次。"""
        markers = ['input[placeholder*="DOI"]', 'text="数字资源管理系统"', 'text="中文数据库"']
        for attempt in range(2):
            try:
                await first_visible(page, markers, timeout=30000)
                return
            except LookupError:
                if attempt == 0:
                    logger.warning("首页内容未渲染(白页?)，刷新重试一次")
                    try:
                        await page.reload(wait_until="domcontentloaded")
                    except Exception:  # noqa: BLE001
                        pass
                    await wait_ready(page)
                else:
                    raise RuntimeError("登录后首页一直未渲染出来，请检查网络后重试")

    @staticmethod
    async def _wait_login_ok(page: Page, timeout: int) -> bool:
        try:
            await page.wait_for_url(lambda url: "/user/login" not in url, timeout=timeout)
            return True
        except PWTimeout:
            return False

    @staticmethod
    async def _try_force_login(page: Page, logger: logging.Logger) -> bool:
        """页面出现“已在其他设备登录”类提示时，点击强制登录按钮。

        注意: 这会把同一账号在别处(如你自己的浏览器)的会话踢下线。
        """
        try:
            body = await page.inner_text("body")
        except Exception:  # noqa: BLE001
            return False
        if not re.search(r"其他设备|重复登录|强制(登录|下线|清除)|多处登录", body):
            return False
        for sel in [
            'text=/强制(登录|下线|清除)/',
            '.layui-layer-btn a:has-text("强制")',
            'button:has-text("强制")',
            'a:has-text("强制")',
            'button:has-text("继续登录")',
            '.layui-layer-btn a',  # 兜底: 弹窗按钮(通常是确认/强制)
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible():
                    await loc.click(timeout=2000)
                    logger.info("检测到账号在其他设备登录，已点击强制登录: %s", sel)
                    return True
            except Exception:  # noqa: BLE001
                continue
        logger.warning("检测到“其他设备登录”提示但未找到强制登录按钮")
        return False

    async def open_database(self, page: Page, db_name: str, logger: logging.Logger,
                            category: str = "医学数据库") -> Page:
        # 1. 点击左侧分类（如“医学数据库”）；首页可能加载较慢，多等一会
        cat = await first_visible(page, [
            f'text="{category}"',
        ], timeout=40000)
        await cat.click()
        await wait_ready(page, 3000)

        # 2. 点击数据库卡片（如 “Embase”），经鉴权中转后进入 webvpn 代理地址。
        #    弹窗可能因鉴权延迟较慢；中转网关偶尔 504(token 仅 30 秒有效)，需重新点击。
        title = self.CARD_TITLES.get(db_name, db_name)
        host_key = self.HOST_KEYS.get(db_name, db_name)
        last_err = ""
        for attempt in range(5):
            card = page.get_by_text(title, exact=True).first
            await card.wait_for(state="visible", timeout=15000)
            before = list(page.context.pages)
            await card.click(no_wait_after=True)
            db_page = await self._poll_db_page(page, before, host_key, timeout_ms=30000)
            if db_page is None:
                logger.warning("点击 %s 卡片后未检测到数据库页面，重试(%d/5)", title, attempt + 1)
                continue
            await db_page.bring_to_front()
            try:
                await self._wait_db_url(db_page, host_key, timeout_ms=90000)
            except PWTimeout:
                logger.warning("等待跳转到 %s 超时，当前地址: %s", host_key, db_page.url)
            await wait_ready(db_page, 15000)
            gw_err = await self._gateway_error(db_page)
            if gw_err is None and host_key in self._host_of(db_page.url):
                logger.info("已进入 %s: %s", db_name, db_page.url)
                return db_page
            last_err = gw_err or f"未跳转到 {host_key} (当前 {db_page.url})"
            wait_s = min(10 * (attempt + 1), 40)
            logger.warning("进入 %s 失败: %s，%d 秒后重试(%d/5)", db_name, last_err, wait_s, attempt + 1)
            await asyncio.sleep(wait_s)
            # 清理失败页面，回到首页状态再重新点击
            if db_page is not page:
                try:
                    await db_page.close()
                except Exception:  # noqa: BLE001
                    pass
            else:
                try:
                    await page.go_back(wait_until="domcontentloaded")
                    await wait_ready(page, 3000)
                except Exception:  # noqa: BLE001
                    pass
        raise RuntimeError(f"多次尝试后仍无法进入 {db_name}: {last_err}")

    # 中转网关错误页特征(如 "504 Gateway Timeout: remote server did not respond to the proxy")
    GATEWAY_ERR_RE = re.compile(
        r"50[24]\s*(Gateway|Bad Gateway)|Gateway Timeout|did not respond to the proxy", re.I)

    @classmethod
    async def _gateway_error(cls, page: Page) -> str | None:
        """页面是网关错误页时返回错误摘要，否则返回 None。"""
        try:
            title = await page.title()
            if cls.GATEWAY_ERR_RE.search(title):
                return title.strip()
            text = (await page.inner_text("body"))[:2000]
            m = cls.GATEWAY_ERR_RE.search(text)
            if m and len(text) < 1500:  # 错误页通常很短，避免误伤正常页面
                return text.strip().splitlines()[0][:120]
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _host_of(url: str) -> str:
        from urllib.parse import urlparse
        return urlparse(url).netloc

    @staticmethod
    async def _wait_db_url(db_page: Page, host_key: str, timeout_ms: int) -> None:
        """等待页面 URL 的主机名落到目标数据库的代理域名；出现网关错误页时快速返回。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if db_page.is_closed():
                raise RuntimeError("数据库页面被关闭了")
            if host_key in ChaoslibSite._host_of(db_page.url):
                return
            if await ChaoslibSite._gateway_error(db_page):
                return  # 网关错误页，由上层判断后重试
            await asyncio.sleep(0.5)
        raise PWTimeout(f"等待跳转到 {host_key} 超时")

    @staticmethod
    async def _poll_db_page(page: Page, before: list[Page], host_key: str,
                            timeout_ms: int) -> Page | None:
        """点击卡片后轮询: 新标签页，或本标签页已跳到目标数据库地址。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for p in page.context.pages:
                if p not in before and not p.is_closed():
                    return p
            if not page.is_closed() and host_key in ChaoslibSite._host_of(page.url):
                return page
            await asyncio.sleep(0.5)
        return None
