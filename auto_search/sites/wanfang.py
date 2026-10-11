"""万方 (wanfangdata.com.cn)：账号密码登录 + 专业检索 + 首页打印PDF + 分批导出XLS。

流程（对应用户需求）：
1. 首页右上角「登录/注册」→ 弹层 iframe(my.wanfangdata.com.cn) 填账号密码
   - 填完可能出现阿里云滑块：自动拖拽尝试 2 次，不过则有头模式提示手动拖、无头报错
2. 高级检索页 → 专业检索 → 文献类型「全部」→ 填检索式 → 检索
3. 结果页：读总条数(span.mark-number)，URL 参数 s=50 设每页条数
4. 只打印第 1 页 PDF（复用 sites/printing.py）
5. 逐页勾选「全选本页」→ 批量引用 → 导出页「查新格式」→ 导出XLS
   - 单次最多选 500 条，满 500 先导出一批，清除已选后继续，直到导完
   - 分批文件保存在 xls/batch_NNN.xlsx，最后合并为 xls/export_all.xlsx（保留分批文件）

注意：
- 万方任何时候可能跳安全验证页(/verify)，本模块导航后统一检测处理
- 结果页翻页/每页条数都是 URL 参数(p=页码, s=每页条数)，直接改 URL 最稳
- 检索/导出在小流量时不强制登录也能用，但批量导出建议先登录(登录态下同源存储同步更可靠)

选择器集中在类 S；网页改版时对照输出目录 _debug/ 快照调整。
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Page

from ..config import AccountConfig, TaskConfig
from ..utils import dump_debug, wait_ready
from .base import Flow, SitePlugin
from .printing import print_page as _shared_print_page


class S:  # noqa: D101 - 选择器集中处
    HOME_URL = "https://www.wanfangdata.com.cn/"
    ADV_URL = "https://s.wanfangdata.com.cn/advanced-search/paper"
    HOME_SEARCH_INPUT = "#search-input"
    LOGIN_ENTRY = "a.anxs-8qwe-login-all"          # 右上角 登录/注册
    LOGIN_FRAME_MARK = "my.wanfangdata.com.cn"     # 登录弹层 iframe 域名
    LOGIN_USER = "#txt_username"
    LOGIN_PWD = "#txt_password"
    LOGIN_SUBMIT = "#new_sub_tj"
    SLIDER = "#aliyunCaptcha-sliding-slider"       # 阿里云滑块把手
    SLIDER_TRACK = "#aliyunCaptcha-sliding-body"   # 滑块轨道
    # 专业检索页
    PRO_TAB_TEXT = "专业检索"
    TYPE_ALL = ".resource-tab .resource-item.checkAll"   # 文献类型-全部
    QUERY_INPUT = "#queryInput"                    # 专业检索大文本框
    SUBMIT = "span.submit-btn"                     # 检索按钮
    # 结果页
    TOTAL_NUMBER = "span.total-number span.mark-number"  # 找到 X 条结果
    LIST_MARK = ".normal-list"                     # 结果列表容器
    CHECK_ALL = ".bottom-check-bar .ivu-checkbox"  # 全选本页
    CHECK_ALL2 = ".common-area .ivu-checkbox"      # 顶部批量选择(备选)
    SELECTED_TIP = ".bottom-check-bar"             # 含"已选择 X 条"
    CLEAR_BTN = ".bottom-check-bar .clear-btn"     # 清除已选
    EXPORT_BTN = ".bottom-check-bar .export-btn"   # 批量引用
    EXPORT_PAGE_MARK = "/export"                   # 导出页 url 特征
    EXPORT_TAB_CX = "查新格式"                       # 导出页 tab(有XLS按钮)
    EXPORT_XLS_TEXT = "导出XLS"


# 阿里云滑块定位(统一处理主页/iframe两种情形, 返回页面坐标)
_FIND_SLIDER_JS = """() => {
    function rectOf(doc, sel) { const el = doc.querySelector(sel); return el ? el.getBoundingClientRect() : null; }
    let sl = rectOf(document, '#aliyunCaptcha-sliding-slider');
    let tr = rectOf(document, '#aliyunCaptcha-sliding-body');
    if (sl && tr && sl.width > 0) {
        return {x: sl.x + sl.width / 2, y: sl.y + sl.height / 2, delta: tr.width - sl.width};
    }
    for (const f of document.querySelectorAll('iframe')) {
        try {
            const doc = f.contentDocument;
            if (!doc) continue;
            sl = rectOf(doc, '#aliyunCaptcha-sliding-slider');
            tr = rectOf(doc, '#aliyunCaptcha-sliding-body');
            if (sl && tr && sl.width > 0) {
                const ir = f.getBoundingClientRect();
                // iframe 内容可能被弹层缩放(layui): 显示宽 != 布局宽
                const scale = ir.width / (f.clientWidth || ir.width);
                return {x: ir.x + (sl.x + sl.width / 2) * scale,
                        y: ir.y + (sl.y + sl.height / 2) * scale,
                        delta: (tr.width - sl.width) * scale};
            }
        } catch (e) { /* 跨域 */ }
    }
    return null;
}"""

_SLIDER_STATE_JS = """() => {
    function stateIn(doc) {
        const sl = doc.querySelector('#aliyunCaptcha-sliding-slider');
        const cap = doc.querySelector('#captchaVerifyParam') || doc.querySelector('#captcha');
        const capVal = cap ? (cap.value || '') : '';
        if (!sl) return capVal ? {cls: '<gone>', left: '', cap: capVal} : null;
        const left = doc.querySelector('#aliyunCaptcha-sliding-left');
        return {cls: sl.className || '', left: left ? (left.style.width || '') : '', cap: capVal};
    }
    let s = stateIn(document);
    if (s) return s;
    for (const f of document.querySelectorAll('iframe')) {
        try { s = stateIn(f.contentDocument); if (s) return s; } catch (e) {}
    }
    return null;
}"""


class WanfangSite(SitePlugin):
    name = "wanfang"
    base_url = S.HOME_URL

    flows: dict = {}  # 在文件末尾注册 WanfangFlow

    # ---------------- 登录 ----------------

    async def login(self, context: BrowserContext, account: AccountConfig,
                    outdir: Path, logger: logging.Logger) -> Page:
        page = await context.new_page()
        logger.info("打开 %s", self.base_url)
        await page.goto(self.base_url, wait_until="commit", timeout=60000)
        if not await self._wait_home(page, logger):
            raise RuntimeError("万方首页长时间未渲染，请检查网络")
        headless = getattr(context.browser, "_auto_search_headless", True)

        # 点 登录/注册
        entry = await self._pick_visible(page.locator(S.LOGIN_ENTRY))
        if entry is None:
            await dump_debug(page, outdir, "login_entry_missing", logger)
            raise LookupError("未找到首页「登录/注册」入口")
        await entry.click()
        logger.info("已点击「登录/注册」，等待登录弹层")

        # 等登录 iframe
        frame = None
        for _ in range(60):
            for fr in page.frames:
                if S.LOGIN_FRAME_MARK in fr.url and \
                        await self._pick_visible(fr.locator(S.LOGIN_PWD)) is not None:
                    frame = fr
                    break
            if frame:
                break
            await asyncio.sleep(1)
        if frame is None:
            await dump_debug(page, outdir, "login_frame_missing", logger)
            raise RuntimeError("登录弹层未出现")

        await (await self._pick_visible(frame.locator(S.LOGIN_USER))).fill(account.username)
        await (await self._pick_visible(frame.locator(S.LOGIN_PWD))).fill(account.password)
        logger.info("已填入账号密码")

        # 滑块 + 提交循环: 以登录结果为判定依据(验证码 DOM 状态不可靠)
        last_outcome = ""
        for attempt in range(1, 4):
            pos = await self._wait_slider(page, 20)
            if pos:
                logger.warning("[登录] 检测到滑块验证，自动拖拽(%d/3)...", attempt)
                await self._drag_slider(page, pos)
                await asyncio.sleep(1)
            await frame.evaluate("() => document.querySelector('#new_sub_tj').click()")
            logger.info("已提交登录，等待结果...")
            outcome = await self._wait_login_outcome(page, frame, outdir, logger)
            if outcome == "ok":
                logger.info("登录成功")
                await wait_ready(page, 5000)
                return page
            last_outcome = outcome
            if outcome == "captcha_fail":
                logger.warning("[登录] 滑块校验被服务端拒绝(%d/3)，重试", attempt)
                continue
            break  # 密码错误/超时不再重试

        # 自动多次不过: 有头模式提示手动, 无头报错
        headless = getattr(context.browser, "_auto_search_headless", True)
        if last_outcome in ("captcha_fail", "timeout") and not headless:
            logger.warning("请在浏览器窗口中手动拖动滑块并点击「登录」(最多等 3 分钟)...")
            outcome = await self._wait_login_outcome(page, frame, outdir, logger, timeout=180)
            if outcome == "ok":
                logger.info("登录成功(手动验证)")
                await wait_ready(page, 5000)
                return page
        if last_outcome == "bad_cred":
            raise RuntimeError("登录失败：账号或密码错误，请检查 config.yaml 的 accounts.wanfang 配置")
        if last_outcome == "captcha_fail":
            raise RuntimeError(
                "滑块验证多次被服务端拒绝（无头浏览器指纹易被风控）。"
                "请改用有头模式：界面取消勾选「无头运行」或 config.yaml 设 headless: false，"
                "届时按日志提示手动拖动滑块即可")
        raise RuntimeError("登录超时：长时间未确认登录成功，请检查网络或改有头模式重试")

    async def _wait_login_outcome(self, page: Page, frame, outdir: Path,
                                  logger: logging.Logger, timeout: int = 25) -> str:
        """等登录结果: ok=成功 bad_cred=账号密码错 captcha_fail=滑块被拒 timeout=超时。"""
        for _ in range(int(timeout * 2)):
            try:
                body = await page.inner_text("body")
                if "登录 / 注册" not in body and "登录/注册" not in body:
                    return "ok"
            except Exception:  # noqa: BLE001
                pass
            try:
                modal = (await frame.evaluate("() => document.body.innerText"))[:300]
            except Exception:  # noqa: BLE001 - 弹层可能已随登录成功销毁
                modal = ""
            if re.search(r"校验失败|验证码.{0,4}(失败|错误)", modal):
                return "captcha_fail"
            if re.search(r"(密码|账号|账户|用户名).{0,4}(错误|不正确|不存在)", modal):
                await dump_debug(page, outdir, "login_bad_cred", logger)
                return "bad_cred"
            await asyncio.sleep(0.5)
        return "timeout"

    async def open_database(self, page: Page, db_name: str, logger: logging.Logger,
                            category: str = "") -> Page:
        """万方不需要从首页点卡片, 直接打开高级检索页。"""
        logger.info("打开高级检索页: %s", S.ADV_URL)
        await self._goto_and_wait(page, S.ADV_URL, "专业检索", logger)
        return page

    # ---------------- 通用小工具 ----------------

    @staticmethod
    async def _pick_visible(loc, timeout_s: float = 10):
        """轮询返回第一个可见匹配(页面常有同名隐藏元素)。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                n = await loc.count()
                for i in range(n):
                    el = loc.nth(i)
                    box = await el.bounding_box()
                    if box and box["width"] > 0 and box["height"] > 0:
                        return el
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        return None

    @staticmethod
    async def _js_click_text(page: Page, text: str, scope_sel: str | None = None) -> bool:
        """点击文本精确匹配的可见元素(JS点击, 避免遮挡/稳定性问题)。"""
        return await page.evaluate(
            """([text, scopeSel]) => {
                const scope = scopeSel ? document.querySelector(scopeSel) : document;
                if (!scope) return false;
                const els = Array.from(scope.querySelectorAll('*')).filter(el =>
                    (el.innerText || '').trim() === text && el.childElementCount <= 2);
                const vis = els.find(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
                if (vis) { vis.click(); return true; }
                return false;
            }""",
            [text, scope_sel])

    async def _wait_home(self, page: Page, logger: logging.Logger, timeout_s: int = 180) -> bool:
        """等首页渲染出搜索框; 代理截断(fault filter abort)时刷新重试。"""
        for attempt in range(6):
            for _ in range(int(timeout_s * 2)):
                try:
                    if await self._pick_visible(page.locator(S.HOME_SEARCH_INPUT), 1) is not None:
                        return True
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.5)
            try:
                body = await page.inner_text("body")
            except Exception:  # noqa: BLE001
                body = ""
            logger.warning("首页未渲染(%s)，刷新重试(%d/6)",
                           "代理截断" if "fault filter abort" in body else "超时", attempt + 1)
            try:
                await page.reload(wait_until="commit", timeout=60000)
            except Exception:  # noqa: BLE001
                pass
        return False

    async def _goto_and_wait(self, page: Page, url: str, marker: str, logger: logging.Logger,
                             tries: int = 5) -> None:
        """导航并等 marker 文本出现; 处理代理截断与安全验证页。"""
        for t in range(1, tries + 1):
            try:
                await page.goto(url, wait_until="commit", timeout=60000)
            except Exception as e:  # noqa: BLE001
                logger.warning("导航异常(%s)，重试(%d/%d)", str(e)[:60], t, tries)
            for _ in range(120):
                if await self._check_verify_page(page, logger):
                    break
                try:
                    body = await page.inner_text("body")
                except Exception:  # noqa: BLE001
                    body = ""
                if "fault filter abort" in body:
                    break
                if marker in body:
                    await wait_ready(page, 3000)
                    return
                await asyncio.sleep(0.5)
            logger.warning("页面未渲染出 %r，重试(%d/%d)", marker, t, tries)
            await asyncio.sleep(2)
        await dump_debug(page, Path.cwd(), "goto_fail", logger)
        raise RuntimeError(f"多次尝试后页面仍未渲染出 {marker!r}: {url[:100]}")

    async def _check_verify_page(self, page: Page, logger: logging.Logger) -> bool:
        """检测并处理安全验证页(/verify)。处理了返回 True。"""
        if "/verify" not in page.url:
            return False
        logger.warning("命中安全验证页，尝试通过滑块: %s", page.url[:90])
        await self.solve_slider(page, logger, headless=None, outdir=None,
                                wait_appear_s=20, context="安全验证")
        # 等跳转回目标页
        for _ in range(60):
            if "/verify" not in page.url:
                return True
            await asyncio.sleep(0.5)
        raise RuntimeError("安全验证页滑块未通过，建议改用有头模式手动拖动")

    # ---------------- 滑块 ----------------

    async def solve_slider(self, page: Page, logger: logging.Logger,
                           headless: bool | None, outdir: Path | None,
                           wait_appear_s: int = 25, context: str = "") -> bool:
        """处理阿里云滑块: 自动拖 2 次; 不过则有头模式等手动, 无头报错。

        headless=None 表示从浏览器对象读取(verify页场景)。
        注意: 拖拽失败后滑块会短暂消失再重置, 「找不到」不等于「已通过」, 需连续确认。
        """
        # 等滑块出现(可能根本不出现)
        pos = await self._wait_slider(page, wait_appear_s)
        if not pos:
            logger.info("[%s] 未出现滑块，继续", context)
            return True

        passed = False
        for attempt in range(1, 3):
            logger.warning("[%s] 检测到滑块验证，自动拖拽尝试(%d/2)...", context, attempt)
            if await self._drag_slider(page, pos):
                passed = True
                break
            logger.warning("[%s] 第 %d 次拖拽未通过", context, attempt)
            if attempt < 2:
                # 等滑块重置后重新出现; 15 秒内不再出现视为已通过
                pos = await self._wait_slider(page, 15)
                if not pos:
                    passed = True
                    break
        if passed:
            logger.info("[%s] 滑块验证通过", context)
            return True

        # 自动不过: 有头等手动, 无头报错
        if headless is None:
            headless = getattr(page.context.browser, "_auto_search_headless", True)
        if headless:
            if outdir:
                await dump_debug(page, outdir, "slider_failed", logger)
            raise RuntimeError(
                f"[{context}] 滑块自动验证未通过（阿里云风控对无头浏览器较严格）。"
                "请改用有头模式运行（界面取消勾选「无头运行」或 config.yaml 设 headless: false），"
                "届时按日志提示手动拖动一次即可")
        logger.warning("[%s] 自动拖拽未通过，请在浏览器窗口中手动拖动滑块（最多等 3 分钟）...", context)
        for _ in range(90):
            state = await page.evaluate(_SLIDER_STATE_JS)
            if not state or "ok" in state.get("cls", "") or state.get("cap"):
                logger.info("[%s] 手动验证完成", context)
                return True
            await asyncio.sleep(2)
        raise RuntimeError(f"[{context}] 等待手动滑块超时(3分钟)")

    async def _wait_slider(self, page: Page, timeout_s: float) -> dict | None:
        """等滑块出现，返回其页面坐标；超时未出现返回 None。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            pos = await page.evaluate(_FIND_SLIDER_JS)
            if pos:
                return pos
            await asyncio.sleep(0.5)
        return None

    async def _drag_slider(self, page: Page, pos: dict) -> bool:
        """按给定坐标执行一次拟人拖拽，返回是否通过。"""
        hx, hy, delta = pos["x"], pos["y"], pos["delta"]
        # 先移动到把手附近再弧线靠近(直接瞬移到把手上是典型机器特征)
        random.seed()
        pre_x = hx - random.randint(250, 500)
        pre_y = max(10, hy + random.randint(-160, 160))
        await page.mouse.move(pre_x, pre_y)
        steps_approach = random.randint(20, 35)
        for i in range(1, steps_approach + 1):
            t = i / steps_approach
            ease = t * t * (3 - 2 * t)
            arc = math.sin(t * math.pi) * random.uniform(4, 10)  # 弧线偏移
            await page.mouse.move(pre_x + (hx - pre_x) * ease + random.uniform(-1, 1),
                                  pre_y + (hy - pre_y) * ease + arc)
            await page.wait_for_timeout(random.randint(8, 25))
        await page.wait_for_timeout(random.randint(250, 450))
        await page.mouse.down()
        await page.wait_for_timeout(random.randint(120, 220))
        steps = 50
        overshoot = 6
        for i in range(1, steps + 1):
            t = i / steps
            ease = 1 - (1 - t) ** 2.4  # 先快后慢
            target = (delta + overshoot) * ease
            if t > 0.9:  # 末端回拉过冲
                target = (delta + overshoot) - overshoot * (t - 0.9) / 0.1
            await page.mouse.move(hx + target, hy + random.uniform(-1.3, 1.3))
            await page.wait_for_timeout(random.randint(15, 55))
        await page.wait_for_timeout(random.randint(200, 400))
        await page.mouse.up()
        # 通过判定(观测 8 秒): 验证页跳走 / class 含 ok(服务端认可)。
        # 注意 captchaVerifyParam 隐藏域在本地拖完就会写入，不能作为通过依据。
        was_verify = "/verify" in page.url
        for _ in range(16):
            if was_verify and "/verify" not in page.url:
                return True
            state = await page.evaluate(_SLIDER_STATE_JS)
            if state and "ok" in state.get("cls", ""):
                return True
            await page.wait_for_timeout(500)
        return False


class WanfangFlow(Flow):
    name = "wanfang"

    VALID_PAGE_SIZES = (20, 30, 50)
    BATCH_LIMIT = 500  # 单次最多选 500 条导出

    async def run(self, page: Page, task: TaskConfig, outdir: Path,
                  logger: logging.Logger, headless: bool = True) -> None:
        try:
            site = WanfangSite()
            if task.date_filter.enabled:
                logger.warning("万方流程暂不支持日期过滤，已忽略 date_filter 配置")
            await self._search(page, task, site, logger)

            total = await self._read_total(page, logger)
            if total <= 0:
                logger.warning("检索结果为空或未能读取总条数")
                await dump_debug(page, outdir, "no_results", logger)
                if task.print_pdf.enabled:
                    await self._print_first_page(page, task, outdir, logger, headless)
                return
            logger.info("检索结果总数: %d 条", total)

            per_page = self._clamp_page_size(task.per_page, logger)
            await self._set_page_size(page, per_page, logger)
            total_pages = math.ceil(total / per_page)
            if task.max_pages > 0:
                total_pages = min(total_pages, task.max_pages)
            logger.info("每页 %d 条，共 %d 页%s", per_page, total_pages,
                        f"（受 max_pages={task.max_pages} 限制）" if task.max_pages > 0 else "")

            # 结果页整页截图(可选)
            if task.screenshot.enabled:
                await self._screenshot(page, outdir, logger)

            # 只打印第 1 页 PDF
            if task.print_pdf.enabled:
                await self._goto_page(page, 1, per_page, logger)
                await self._print_first_page(page, task, outdir, logger, headless)

            # 分批导出 XLS
            if task.export_csv.enabled:  # 万方下此开关表示「批量引用导出XLS」
                xls_dir = outdir / "xls"
                files = await self._export_all(page, task, total_pages, per_page, xls_dir, logger)
                if files:
                    self._merge_xlsx(xls_dir, logger)
            logger.info("万方任务完成，输出目录: %s", outdir)
        except Exception:
            await dump_debug(page, outdir, "error", logger)
            raise

    # ---------------- 检索 ----------------

    async def _search(self, page: Page, task: TaskConfig, site: WanfangSite,
                      logger: logging.Logger) -> None:
        # 专业检索 tab
        if not await site._js_click_text(page, S.PRO_TAB_TEXT):
            raise LookupError("未找到「专业检索」选项卡")
        logger.info("已切换到专业检索")
        await asyncio.sleep(2)

        # 文献类型-全部
        ok = await page.evaluate(
            "() => { const el = document.querySelector('%s'); if (el) { el.click(); return true; } return false; }"
            % S.TYPE_ALL)
        if not ok:
            logger.warning("未找到文献类型「全部」按钮，按默认类型继续")
        else:
            await asyncio.sleep(1.5)
            active = await page.evaluate(
                """() => Array.from(document.querySelectorAll('.resource-tab .resource-item'))
                    .filter(e => e.className.includes('active')).map(e => (e.innerText || '').trim()).join(' ')""")
            logger.info("文献类型已选: %s", active or "(未读到)")

        # 填检索式(优先 #queryInput，兜底最大的可见 textarea)
        filled = await page.evaluate(
            """(q) => {
                let ta = document.querySelector('#queryInput');
                if (!ta) {
                    const cands = Array.from(document.querySelectorAll('textarea')).filter(el => {
                        const r = el.getBoundingClientRect(); return r.width > 400 && r.height > 50;
                    });
                    ta = cands[0];
                }
                if (!ta) return false;
                const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                setter.call(ta, q);
                ta.dispatchEvent(new Event('input', {bubbles: true}));
                ta.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }""",
            task.query)
        if not filled:
            raise LookupError("未找到专业检索输入框")
        logger.info("已填入检索式（%d 字符）", len(task.query))

        # 点检索
        ok = await page.evaluate(
            "() => { const el = document.querySelector('%s'); if (el) { el.click(); return true; } return false; }"
            % S.SUBMIT)
        if not ok:
            raise LookupError("未找到检索按钮")
        logger.info("已点击检索，等待结果页")

        # 等跳转到结果页(可能先跳 /verify)
        left = False
        for _ in range(180):
            if "/verify" in page.url:
                await site._check_verify_page(page, logger)
                continue
            if "advanced-search" not in page.url:
                left = True
                break
            await asyncio.sleep(0.5)
        if not left:
            await dump_debug(page, Path.cwd(), "search_no_results_page", logger)
            raise RuntimeError("点击检索后长时间未跳转到结果页，请检查 _debug 快照")
        for _ in range(120):
            if await page.locator(S.TOTAL_NUMBER).count() > 0 or \
                    await page.locator(S.LIST_MARK).count() > 0:
                break
            await asyncio.sleep(0.5)
        await wait_ready(page, 5000)
        logger.info("已进入结果页: %s", page.url[:120])

    async def _read_total(self, page: Page, logger: logging.Logger) -> int:
        for _ in range(60):
            try:
                txt = await page.locator(S.TOTAL_NUMBER).first.inner_text(timeout=3000)
                m = re.search(r"([\d,]+)", txt)
                if m:
                    return int(m.group(1).replace(",", ""))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        # 兜底: body 里找 "找到 X 条结果"
        try:
            body = await page.inner_text("body")
            m = re.search(r"找到\s*([\d,]+)\s*条", body)
            if m:
                return int(m.group(1).replace(",", ""))
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _clamp_page_size(self, per_page: int, logger: logging.Logger) -> int:
        if per_page in self.VALID_PAGE_SIZES:
            return per_page
        logger.warning("万方每页条数只支持 20/30/50，配置的 %d 已按 50 处理", per_page)
        return 50

    @staticmethod
    def _set_url_params(url: str, **params) -> str:
        parts = urlsplit(url)
        q = dict(parse_qsl(parts.query))
        q.update({k: str(v) for k, v in params.items()})
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))

    async def _set_page_size(self, page: Page, per_page: int, logger: logging.Logger) -> None:
        """URL 参数 s= 控制每页条数；当前已是则跳过。"""
        cur = dict(parse_qsl(urlsplit(page.url).query)).get("s")
        if cur == str(per_page):
            logger.info("每页条数已是 %d", per_page)
            return
        await page.goto(self._set_url_params(page.url, s=per_page, p=1),
                        wait_until="commit", timeout=60000)
        await self._wait_results(page, logger)
        logger.info("已设置每页 %d 条", per_page)

    async def _wait_results(self, page: Page, logger: logging.Logger) -> None:
        for _ in range(120):
            try:
                if await page.locator(S.TOTAL_NUMBER).count() > 0 or \
                        await page.locator(S.LIST_MARK).count() > 0:
                    await wait_ready(page, 3000)
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        logger.warning("结果区等待超时，继续尝试后续步骤")

    async def _goto_page(self, page: Page, n: int, per_page: int, logger: logging.Logger) -> None:
        cur = dict(parse_qsl(urlsplit(page.url).query)).get("p", "1")
        if cur == str(n):
            return
        await page.goto(self._set_url_params(page.url, p=n, s=per_page),
                        wait_until="commit", timeout=60000)
        await self._wait_results(page, logger)

    async def _screenshot(self, page: Page, outdir: Path, logger: logging.Logger) -> None:
        path = outdir / "screenshot_results.png"
        try:
            await page.evaluate("window.scrollTo(0, 0)")
            await page.screenshot(path=str(path), full_page=True, timeout=90000)
            logger.info("已保存结果页整页截图: %s", path)
        except Exception as e:  # noqa: BLE001
            logger.warning("结果页截图失败(%s)，继续", str(e)[:80])

    async def _print_first_page(self, page: Page, task: TaskConfig, outdir: Path,
                                logger: logging.Logger, headless: bool) -> None:
        logger.info("打印结果第 1 页 PDF")
        await _shared_print_page(page, task, outdir / "pdf", 1, logger, headless)

    # ---------------- 勾选与导出 ----------------

    async def _selected_count(self, page: Page) -> int:
        try:
            txt = await page.locator(S.SELECTED_TIP).first.inner_text(timeout=3000)
            m = re.search(r"已选择\s*([\d,]+)\s*条", txt)
            if m:
                return int(m.group(1).replace(",", ""))
        except Exception:  # noqa: BLE001
            pass
        return 0

    async def _select_current_page(self, page: Page, logger: logging.Logger) -> None:
        """勾选本页全部条目（bottom-check-bar 的 wf-checkbox）。"""
        before = await self._selected_count(page)
        clicked = await page.evaluate(
            """() => {
                for (const scope of ['.bottom-check-bar', '.common-area']) {
                    const bar = document.querySelector(scope);
                    if (!bar) continue;
                    const cb = bar.querySelector('.ivu-checkbox');
                    if (cb) { cb.click(); return true; }
                }
                return false;
            }""")
        if not clicked:
            raise LookupError("未找到「全选本页」勾选框")
        # 等已选数变化
        for _ in range(20):
            await asyncio.sleep(0.5)
            if await self._selected_count(page) > before:
                return
        logger.warning("勾选后「已选择」数量未增加，可能本页已全部选过")

    async def _clear_selection(self, page: Page, logger: logging.Logger) -> None:
        clicked = await page.evaluate(
            """() => {
                const bar = document.querySelector('.bottom-check-bar');
                if (!bar) return false;
                const els = Array.from(bar.querySelectorAll('*')).filter(el =>
                    (el.innerText || '').trim() === '清除' && el.childElementCount <= 1);
                const vis = els.find(el => { const r = el.getBoundingClientRect(); return r.width > 0; });
                if (vis) { vis.click(); return true; }
                return false;
            }""")
        if clicked:
            for _ in range(20):
                await asyncio.sleep(0.5)
                if await self._selected_count(page) == 0:
                    break
            logger.info("已清除选择")
        else:
            logger.warning("未找到「清除」按钮")

    async def _export_all(self, page: Page, task: TaskConfig, total_pages: int,
                          per_page: int, xls_dir: Path, logger: logging.Logger) -> list[Path]:
        """逐页勾选 -> 满 500 或到末页就点批量引用导出 XLS；返回分批文件列表。"""
        xls_dir.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        batch_no = 0
        for p in range(1, total_pages + 1):
            logger.info("勾选第 %d/%d 页", p, total_pages)
            await self._goto_page(page, p, per_page, logger)
            await self._select_current_page(page, logger)
            selected = await self._selected_count(page)
            logger.info("当前已选: %d 条", selected)
            # 满 500 或最后一页 -> 导出一批
            if selected >= self.BATCH_LIMIT or p == total_pages:
                if selected <= 0:
                    continue
                batch_no += 1
                f = await self._export_batch(page, xls_dir, batch_no, selected, logger)
                if f is None:
                    raise RuntimeError(f"第 {batch_no} 批导出失败（已选 {selected} 条），"
                                       "详见上方日志；可重新运行任务")
                files.append(f)
                await self._clear_selection(page, logger)
        logger.info("导出完成: 共 %d 批", batch_no)
        return files

    async def _export_batch(self, page: Page, xls_dir: Path, batch_no: int,
                            selected: int, logger: logging.Logger) -> Path | None:
        """点「批量引用」-> 导出页新标签 -> 查新格式 -> 导出XLS -> 保存。"""
        btn = page.locator(S.EXPORT_BTN).first
        if not await btn.count():
            # 备选: 文本匹配
            ok = await WanfangSite._js_click_text(page, "批量引用")
            if not ok:
                logger.error("未找到「批量引用」按钮")
                return None
        else:
            await btn.click()
        logger.info("第 %d 批: 已点击「批量引用」(%d 条)，等待导出页", batch_no, selected)

        # 等导出页新标签
        export_page = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for p in page.context.pages:
                if p is not page and not p.is_closed() and S.EXPORT_PAGE_MARK in p.url:
                    export_page = p
                    break
            if export_page:
                break
            await asyncio.sleep(0.5)
        if export_page is None:
            logger.error("导出页未打开")
            return None
        logger.info("导出页已打开: %s", export_page.url[:100])

        try:
            # 等导出页渲染(找 导出XLS 或 tab)
            for _ in range(120):
                try:
                    body = await export_page.inner_text("body")
                except Exception:  # noqa: BLE001
                    body = ""
                if S.EXPORT_XLS_TEXT in body or S.EXPORT_TAB_CX in body:
                    break
                await asyncio.sleep(0.5)

            # 若当前 tab 没有 导出XLS 按钮，切到「查新格式」tab
            has_xls = await export_page.evaluate(
                """() => Array.from(document.querySelectorAll('.export-button-item, .option-button, div, span'))
                    .some(el => (el.innerText || '').trim() === '导出XLS'
                        && el.getBoundingClientRect().width > 0)""")
            if not has_xls:
                clicked = await export_page.evaluate(
                    """(tabText) => {
                        const els = Array.from(document.querySelectorAll('*')).filter(el =>
                            (el.innerText || '').trim() === tabText && el.childElementCount <= 2);
                        const vis = els.find(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
                        if (vis) { vis.click(); return true; }
                        return false;
                    }""", S.EXPORT_TAB_CX)
                logger.info("切换到「%s」tab: %s", S.EXPORT_TAB_CX, clicked)
                await asyncio.sleep(3)

            # 点 导出XLS，捕获下载(ExcelJS 客户端生成 .xlsx, Blob 下载)
            path = xls_dir / f"batch_{batch_no:03d}.xlsx"
            async with export_page.expect_download(timeout=180000) as dl_info:
                clicked = await export_page.evaluate(
                    """() => {
                        const els = Array.from(document.querySelectorAll('.export-button-item, .option-button, div, span'))
                            .filter(el => (el.innerText || '').trim() === '导出XLS');
                        const vis = els.find(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
                        if (vis) { vis.click(); return true; }
                        return false;
                    }""")
                if not clicked:
                    raise LookupError("导出页未找到「导出XLS」按钮")
            download = await dl_info.value
            await download.save_as(str(path))
            logger.info("第 %d 批已保存: %s（建议文件名: %s）",
                        batch_no, path, download.suggested_filename)
            return path
        except Exception as e:  # noqa: BLE001
            logger.error("第 %d 批导出失败: %s", batch_no, e)
            return None
        finally:
            try:
                await export_page.close()
            except Exception:  # noqa: BLE001
                pass
            await page.bring_to_front()

    # ---------------- 合并 ----------------

    def _merge_xlsx(self, xls_dir: Path, logger: logging.Logger) -> None:
        """把 batch_*.xlsx 合并为 export_all.xlsx（按 sheet 名归类，去掉重复表头）。"""
        files = sorted(xls_dir.glob("batch_*.xlsx"))
        if not files:
            return
        try:
            from openpyxl import Workbook, load_workbook
        except ImportError:
            logger.error("缺少 openpyxl，无法合并（pip install openpyxl）；分批文件已保留在 %s", xls_dir)
            return

        sheets: dict[str, list[tuple]] = {}
        headers: dict[str, set] = {}
        for f in files:
            wb = load_workbook(f, read_only=True)
            try:
                for ws in wb.worksheets:
                    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
                    if ws.title not in sheets:
                        sheets[ws.title] = list(rows)
                        headers[ws.title] = set(rows[:2])  # 类型标题行 + 表头行
                    else:
                        hdr = headers[ws.title]
                        sheets[ws.title].extend(r for r in rows if r not in hdr)
            finally:
                wb.close()

        out = xls_dir / "export_all.xlsx"
        wb = Workbook(write_only=True)
        total = 0
        for name, rows in sheets.items():
            ws = wb.create_sheet(name)
            for r in rows:
                ws.append(list(r))
                total += 1
        wb.save(out)
        logger.info("已合并 %d 个分批文件 -> %s（共 %d 行，含表头；分批文件保留在同目录）",
                    len(files), out, total)


WanfangSite.flows = {"wanfang": WanfangFlow}
