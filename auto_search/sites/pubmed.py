"""PubMed 检索流程：关键词检索 -> Custom Range 日期筛选 -> 逐页打印 PDF -> Save 导出 CSV。

与 Embase 的差异：
- PubMed 结果页的每页条数/页码都是 URL 参数(size=200&page=N)，翻页用 URL 导航更稳
- 导出用工具条 Save -> "Save citations to file" 面板 -> Selection: All results + Format: CSV
  -> Create file，一次下载全部结果(非逐页)
- 不做整页截图

选择器集中在类 S 中；失败时输出目录 _debug/ 有截图和 HTML 可对照。
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeout

from ..config import TaskConfig
from ..utils import dump_debug, fill_input_smart, first_visible, goto_gateway_retry, wait_ready
from .base import Flow
from .printing import print_page as _shared_print_page


class S:  # noqa: D101 - 选择器集中处
    # 首页/结果页检索框
    SEARCH_INPUT = [
        'input[name="term"]',
        '#id_term',
        'form input[type="search"]',
    ]
    SEARCH_BUTTON = [
        'button.search-btn',
        'button:has-text("Search")',
    ]
    # 结果数 "456,072 results"
    RESULTS_AMOUNT = ['.results-amount', 'div:has-text("results"):has(span.value)']
    # 分页: "Page [1] of 2,281"，页码输入框和下一页按钮
    PAGINATION_WRAP = '.pagination-wrapper, .pagination'
    PAGE_INPUT = ['input.page-number#page-number-input', 'input.page-number', '.pagination input']
    NEXT_BUTTON = [
        'button.next-page-btn',
        'button[aria-label*="next" i]',
        '.pagination button:has-text(">")',
    ]
    # 左侧 PUBLICATION DATE -> Custom Range
    CUSTOM_RANGE_RADIO = [
        'label#datepicker-trigger',          # title="Custom Range" 的触发 label
        'label[title="Custom Range"]',
        'input[name="filter"][id*="y_custom"]',
        'label:has-text("Custom Range")',
    ]
    # 自定义起止日期输入框(#datepicker 面板内 年/月/日 三个一组)
    START_INPUTS = [
        ['#datepicker .start-year', '#datepicker .start-month', '#datepicker .start-day'],
    ]
    END_INPUTS = [
        ['#datepicker .end-year', '#datepicker .end-month', '#datepicker .end-day'],
    ]
    DATE_APPLY = [
        '#datepicker button.apply-btn',      # 填齐 6 项后才可用
        'button:has-text("Apply")',
    ]
    FILTER_APPLIED_RE = re.compile(r"Filters applied|filter=dates\.")
    # 工具条 Save 按钮与保存面板
    SAVE_BUTTON = [
        'button.save-search-btn',
        'button:has-text("Save")',
    ]
    SAVE_PANEL = ['div:has(> h2:has-text("Save citations to file"))', '#save-citations-panel']
    SELECTION_SELECT = ['select#save-selection', 'select:has(option:text-is("All results"))']
    FORMAT_SELECT = ['select#save-format', 'select:has(option:text-is("CSV"))']
    CREATE_FILE = [
        'button:has-text("Create file")',
        'button.create-file-btn',
    ]
    COOKIE_ACCEPT = [
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
    ]


class PubmedFlow(Flow):
    name = "pubmed"

    async def run(self, page: Page, task: TaskConfig, outdir: Path,
                  logger: logging.Logger, headless: bool = True) -> None:
        try:
            await self._settle(page, logger)
            await self._accept_cookies(page, logger)
            await self._search(page, task.query, logger)
            await self._ensure_page_size(page, task.per_page, logger)
            if task.date_filter.enabled:
                await self._set_date_range(page, task, logger)

            if not await self._has_results(page, logger):
                # 检索结果为空：只打印结果页 PDF，跳过导出
                logger.info("检索结果为空: 仅打印结果页 PDF，跳过 CSV 导出")
                if task.print_pdf.enabled:
                    await _shared_print_page(page, task, outdir / "pdf", 1, logger, headless)
                return

            total_pages = await self._total_pages(page, task, logger)
            if task.max_pages > 0:
                total_pages = min(total_pages, task.max_pages)
            logger.info("检索结果共 %d 页", total_pages)

            if task.print_pdf.enabled:
                await self._goto_page(page, 1, logger)
                for i in range(1, total_pages + 1):
                    logger.info("打印PDF: 第 %d/%d 页", i, total_pages)
                    await _shared_print_page(page, task, outdir / "pdf", i, logger, headless)
                    if i < total_pages:
                        await self._goto_page(page, i + 1, logger)

            if task.export_csv.enabled:
                await self._export_csv(page, task, outdir / "csv", logger)
        except Exception:
            await dump_debug(page, outdir, "error", logger)
            raise

    # ---------------- 检索与筛选 ----------------

    async def _settle(self, page: Page, logger: logging.Logger,
                      networkidle_ms: int = 8000) -> None:
        """等待页面稳定；若撞上 NCBI 的 PoW 质询页则自动求解(最多 3 轮)。"""
        for round_ in range(3):
            await wait_ready(page, networkidle_ms)
            if not await self._solve_pow_challenge(page, logger):
                return
        raise RuntimeError("NCBI 风控质询(PoW)多轮求解未通过，建议稍后重试或改用有头模式运行")

    async def _solve_pow_challenge(self, page: Page, logger: logging.Logger) -> bool:
        """NCBI PoW cookie 质询页自动求解。返回 True=求解后仍需再观察一轮。

        质询页 JS 的 cookie 自检要求当前域名等于 pubmed.ncbi.nlm.nih.gov，
        WebVPN 代理域名不满足导致自检永远失败。求解结果走 POST /_pow/solve
        由服务端下发 cookie，不依赖该自检 cookie，打补丁跳过自检再执行 run() 即可。
        """
        try:
            is_challenge = await page.evaluate(
                "() => typeof challengeId !== 'undefined'"
                " && typeof submitSolve === 'function' && typeof run === 'function'")
        except Exception:  # noqa: BLE001
            return False
        if not is_challenge:
            return False
        logger.warning("检测到 NCBI PoW 质询页，自动求解中...")
        # 记录求解接口的结果，便于排查
        page.once("response", lambda r: logger.info("PoW 求解响应: %d", r.status)
                  if "/_pow/solve" in r.url else None)
        await page.evaluate(
            "() => { if (!window.__powSolving) {"
            "  window.__powSolving = true;"
            "  window.browserAcceptsCookies = () => true;"
            "  run();"
            "} }")
        try:
            # 求解成功后页面会 reload/跳转, challengeId 随之消失
            await page.wait_for_function(
                "() => typeof challengeId === 'undefined'", timeout=45000)
            logger.info("PoW 质询已通过")
            return False
        except PWTimeout:
            logger.warning("PoW 求解未完成，刷新后再试")
            try:
                await page.reload(wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
            return True  # 让 _settle 再循环一轮

    async def _accept_cookies(self, page: Page, logger: logging.Logger) -> None:
        for sel in S.COOKIE_ACCEPT:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    await btn.click(timeout=2000)
                    logger.info("已接受 cookies 弹窗")
                    return
            except Exception:  # noqa: BLE001
                continue

    async def _search(self, page: Page, query: str, logger: logging.Logger) -> None:
        await wait_ready(page, 8000)
        box = None
        for attempt in range(2):
            try:
                box = await first_visible(page, S.SEARCH_INPUT, timeout=45000)
                break
            except LookupError:
                if attempt == 1:
                    raise
                logger.warning("检索页未渲染，刷新重试一次")
                try:
                    await page.reload(wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001
                    pass
                await wait_ready(page, 10000)
        await fill_input_smart(box, query)
        # 优先回车提交(PubMed 表单回车即可提交; 检索按钮在输入有内容前是 disabled)
        await box.press("Enter")
        try:
            await page.wait_for_url(re.compile(r"term="), timeout=15000)
        except PWTimeout:
            # 回车没生效: 改逐键输入(触发完整按键事件)再回车
            logger.warning("回车提交未生效，改用逐键输入后重试")
            await self._type_value(box, query)
            await box.press("Enter")
            try:
                await page.wait_for_url(re.compile(r"term="), timeout=15000)
            except PWTimeout:
                btn = await first_visible(page, S.SEARCH_BUTTON)
                await btn.click()
                try:
                    await page.wait_for_url(re.compile(r"term="), timeout=60000)
                except PWTimeout:
                    pass
        await self._settle(page, logger)
        try:
            await page.locator(S.RESULTS_AMOUNT[0]).first.wait_for(state="visible", timeout=30000)
        except PWTimeout:
            pass
        logger.info("检索完成: %s", query)

    async def _ensure_page_size(self, page: Page, per_page: int, logger: logging.Logger) -> None:
        """每页条数由 URL 参数 size= 控制；不一致就改 URL 刷新一次。"""
        if not per_page:
            return
        cur = self._get_url_param(page.url, "size")
        if cur == str(per_page):
            logger.info("每页条数已是 %d", per_page)
            return
        new_url = self._set_url_param(page.url, "size", str(per_page))
        await goto_gateway_retry(page, new_url, logger, wait_until="domcontentloaded")
        await self._settle(page, logger)
        logger.info("已设置每页显示 %d 条", per_page)

    async def _set_date_range(self, page: Page, task: TaskConfig, logger: logging.Logger) -> None:
        """PUBLICATION DATE -> Custom Range -> 填起止(年/月/日) -> Apply。"""
        df = task.date_filter
        y1, m1, d1 = df.start.split("-")
        y2, m2, d2 = df.end.split("-")
        start_vals = [str(int(y1)), str(int(m1)), str(int(d1))]
        end_vals = [str(int(y2)), str(int(m2)), str(int(d2))]

        # 选 Custom Range（面板随之展开）
        radio = await first_visible(page, S.CUSTOM_RANGE_RADIO, timeout=15000)
        await radio.click()
        await asyncio.sleep(0.8)

        # 填起止日期
        filled = await self._fill_custom_range(page, start_vals, end_vals)
        if not filled:
            raise LookupError("未能找到 Custom Range 的日期输入框")

        # Apply 按钮在 6 项填齐前是 disabled 状态，先等它可用
        btn = await first_visible(page, S.DATE_APPLY, timeout=10000)
        try:
            await page.wait_for_function(
                """() => {
                    const b = document.querySelector('#datepicker button.apply-btn')
                        || Array.from(document.querySelectorAll('button'))
                            .find(x => x.textContent.trim() === 'Apply');
                    return b && !b.disabled;
                }""",
                timeout=10000)
        except PWTimeout:
            logger.warning("Apply 按钮一直不可用(输入可能未生效)，仍尝试点击")
        await btn.click()
        try:
            await page.wait_for_function(
                """() => location.href.includes('filter=dates.')
                    || document.body.innerText.includes('Filters applied')""",
                timeout=60000)
        except PWTimeout as e:
            raise RuntimeError("日期筛选未生效(未出现 Filters applied)") from e
        await self._settle(page, logger)
        logger.info("已应用日期筛选: %s ~ %s", df.start, df.end)

    @staticmethod
    async def _type_value(loc, value: str) -> None:
        """逐键输入(触发 keydown/keyup/input 事件)，PubMed datepicker 需要真实按键事件。"""
        await loc.click()
        await loc.press("ControlOrMeta+a")
        await loc.press_sequentially(str(value), delay=30)
        await loc.press("Tab")

    async def _fill_custom_range(self, page: Page, start_vals: list[str],
                                 end_vals: list[str]) -> bool:
        """按选择器组填 6 个输入框；不匹配则用面板内输入框顺序兜底。"""
        for group_sels, vals in ((S.START_INPUTS, start_vals), (S.END_INPUTS, end_vals)):
            filled = False
            for sel_group in group_sels:
                ok = True
                for sel, val in zip(sel_group, vals):
                    try:
                        loc = page.locator(sel).first
                        await loc.wait_for(state="visible", timeout=1500)
                        await self._type_value(loc, val)
                    except Exception:  # noqa: BLE001
                        ok = False
                        break
                if ok:
                    filled = True
                    break
            if not filled:
                # 兜底：datepicker 面板里的可见输入框按顺序即 start(3) + end(3)
                try:
                    boxes = page.locator('#datepicker input:visible')
                    n = await boxes.count()
                    if n >= 6:
                        for i, v in enumerate(start_vals + end_vals):
                            await self._type_value(boxes.nth(i), v)
                        return True
                except Exception:  # noqa: BLE001
                    pass
                return False
        return True

    # ---------------- 分页 ----------------

    async def _total_pages(self, page: Page, task: TaskConfig, logger: logging.Logger) -> int:
        """总页数 = ceil(结果数 / 每页条数)；读不到结果数再退回到分页栏文本。"""
        count = await self._results_count(page)
        if count is not None and task.per_page:
            import math
            return max(1, math.ceil(count / task.per_page))
        try:
            text = await page.locator(S.PAGINATION_WRAP).first.inner_text(timeout=8000)
            m = re.search(r"of\s+([\d,]+)", " ".join(text.split()))
            if m:
                return int(m.group(1).replace(",", ""))
        except Exception:  # noqa: BLE001
            pass
        logger.warning("未能读取总页数，按 1 页处理")
        return 1

    async def _current_page(self, page: Page) -> int:
        for sel in S.PAGE_INPUT:
            try:
                loc = page.locator(sel).first
                val = await loc.input_value(timeout=3000)
                return int(val)
            except Exception:  # noqa: BLE001
                continue
        return 1

    async def _goto_page(self, page: Page, n: int, logger: logging.Logger) -> None:
        """翻页: 优先 URL 参数 page=N 导航，失败则点"下一页"按钮逐页点。"""
        cur = await self._current_page(page)
        if cur == n:
            return
        if n < cur:  # 往回走: 直接 URL 导航最省事
            await self._goto_page_via_url(page, n, logger)
            return
        # 往前走: URL 导航也行，但优先点按钮(更像人工操作)
        try:
            await self._goto_page_via_url(page, n, logger)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("URL 翻页失败(%s)，改用按钮逐页点", e)
        while await self._current_page(page) < n:
            btn = await first_visible(page, S.NEXT_BUTTON, timeout=10000)
            await btn.click()
            await wait_ready(page, 6000)

    async def _goto_page_via_url(self, page: Page, n: int, logger: logging.Logger) -> None:
        url = self._set_url_param(page.url, "page", str(n))
        await goto_gateway_retry(page, url, logger, wait_until="domcontentloaded")
        await self._settle(page, logger)
        for _ in range(30):  # 等页码输入框更新为目标页
            if await self._current_page(page) == n:
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"翻到第 {n} 页后页码未更新")

    # ---------------- 结果与导出 ----------------

    async def _has_results(self, page: Page, logger: logging.Logger) -> bool:
        try:
            text = await page.locator(S.RESULTS_AMOUNT[0]).first.inner_text(timeout=8000)
            text = " ".join(text.split())
            logger.info("结果数: %s", text)
            m = re.search(r"([\d,]+)\s+results", text, re.I)
            return bool(m) and int(m.group(1).replace(",", "")) > 0
        except Exception:  # noqa: BLE001
            # 兜底: 看结果列表有没有条目
            try:
                return await page.locator(".docsum-content").count() > 0
            except Exception:  # noqa: BLE001
                return False

    async def _results_count(self, page: Page) -> int | None:
        try:
            text = await page.locator(S.RESULTS_AMOUNT[0]).first.inner_text(timeout=5000)
            m = re.search(r"([\d,]+)\s+results", " ".join(text.split()), re.I)
            if m:
                return int(m.group(1).replace(",", ""))
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _export_csv(self, page: Page, task: TaskConfig, csv_dir: Path,
                          logger: logging.Logger) -> None:
        """Save -> Save citations to file 面板: All results + CSV -> Create file 下载。

        注意: PubMed "All results" 导出上限约 10,000 条，超过会截断(日志告警)。
        """
        csv_dir.mkdir(parents=True, exist_ok=True)
        total = await self._results_count(page)
        if total and total > 10000:
            logger.warning("结果数 %d 超过 PubMed 单次导出上限(约10000条)，导出可能被截断；"
                           "建议缩小日期范围分批导出", total)

        btn = await first_visible(page, S.SAVE_BUTTON, timeout=10000)
        await btn.click()
        panel = await first_visible(page, S.SAVE_PANEL, timeout=15000)

        sel = await first_visible(panel, S.SELECTION_SELECT, timeout=8000)
        await sel.select_option(label="All results")
        fmt = await first_visible(panel, S.FORMAT_SELECT, timeout=8000)
        await fmt.select_option(label="CSV")

        create = await first_visible(panel, S.CREATE_FILE, timeout=8000)
        async with page.expect_download(timeout=300000) as dl_info:
            await create.click()
        download = await dl_info.value
        path = csv_dir / "export_all.csv"
        await download.save_as(str(path))
        logger.info("已保存 %s", path)

        # 导出条数与结果数核对（不一致时告警，便于发现截断）
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = sum(1 for _ in f) - 1
            logger.info("导出 CSV 共 %d 条", rows)
            if total and rows < total:
                logger.warning("导出条数(%d)少于结果数(%d)，可能达到导出上限", rows, total)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- URL 工具 ----------------

    @staticmethod
    def _get_url_param(url: str, key: str) -> str | None:
        return dict(parse_qsl(urlsplit(url).query)).get(key)

    @staticmethod
    def _set_url_param(url: str, key: str, value: str) -> str:
        parts = urlsplit(url)
        q = dict(parse_qsl(parts.query))
        q[key] = value
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(q), parts.fragment))
