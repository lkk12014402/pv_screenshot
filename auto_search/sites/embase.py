"""Embase 检索流程：关键词检索 -> 日期过滤 -> 设置每页条数 -> 逐页打印 PDF / 导出 CSV。

网页结构可能随 Embase 改版变化，所有选择器集中在类 S 中，优先改这里；
运行失败时输出目录的 _debug/ 下会保存截图和 HTML 用于对照。
"""
from __future__ import annotations

import asyncio
import csv
import logging
import re
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeout

from ..config import TaskConfig
from ..utils import (
    CHECKBOX_MAP_JS,
    dump_debug,
    ensure_checkbox_by_text,
    fill_input_smart,
    first_visible,
    toggle_checkbox,
    wait_ready,
)
from .base import Flow

# 页眉页脚模板（等价于浏览器打印对话框“更多设置-页眉和页脚”的默认样式）
HEADER_TPL = (
    '<div style="font-size:9px;width:100%;padding:4px 24px 0;'
    'display:flex;justify-content:space-between;color:#555;">'
    '<span class="date"></span><span class="title"></span></div>'
)
FOOTER_TPL = (
    '<div style="font-size:9px;width:100%;padding:0 24px;'
    'display:flex;justify-content:space-between;color:#555;">'
    '<span class="url"></span>'
    '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
)

PAGE_NUM_RE = re.compile(r"Page\s+(\d+)\s+of\s+([\d,]+)")


class S:  # noqa: D101 - 选择器集中处
    # 快速检索页：关键词输入框（placeholder 如 "e.g. 'heart attack' AND stress"）
    SEARCH_INPUT = [
        '[data-testid="input-fragments[0].value"]',
        'input[placeholder*="heart attack"]',
        'textarea[placeholder*="heart attack"]',
        '#searchForm input[type="text"]',
        'main input[type="text"]',
    ]
    SHOW_RESULTS = [
        '[data-testid="show-results-button"]',
        'button:has-text("Show results")',
    ]
    COOKIE_ACCEPT = [
        '#onetrust-accept-btn-handler',          # OneTrust(中文: 接受Cookies)
        'button:has-text("接受Cookies")',
        'button:has-text("接受 Cookies")',
        'button:has-text("Accept all cookies")',
        'button:has-text("Accept cookies")',
    ]
    # Pendo 新手引导弹窗的关闭按钮
    OVERLAY_CLOSE = [
        'button._pendo-close-guide',
        'button[id^="pendo-close-guide"]',
        '#pendo-base button[aria-label="Close"]',
    ]
    # 结果页顶部查询框（textarea#search-query, aria-placeholder="e.g. ..."）
    RESULTS_QUERY_INPUT = [
        'form[data-testid="results-search-form"] textarea',
        '#search-query',
        'textarea[aria-placeholder*="cancer gene"]',
    ]
    # 结果页查询框右侧的放大镜搜索按钮
    SEARCH_BUTTON = [
        'button[title="Search query"]',
        'form[data-testid="results-search-form"] button[title*="Search" i]',
    ]
    # “Date”折叠面板开关（role=tab 的 Mapping/Date/Fields/Quick limits 之一）
    DATE_TOGGLE = [
        'button[role="tab"]:has-text("Date")',
        'button:has-text("Date")',
    ]
    # 分页容器与翻页按钮
    PAGINATION = '[data-testid="pagination"]'
    NEXT_BUTTON = [
        'button[title="Next page"]',
        'button[aria-label="Next page"]',
    ]
    PREV_BUTTON = [
        'button[title="Previous page"]',
        'button[aria-label="Previous page"]',
    ]
    # 每页条数: "Display: N results per page" 自定义下拉
    PAGE_SIZE_WRAP = '[data-testid="page-size"]'
    # 结果工具条: 勾选本页 / Export 按钮
    SELECT_PAGE_CB = 'input[aria-label="Select page results"]'
    EXPORT_BUTTON = [
        'button[data-testid="export"]',
        'button:has-text("Export")',
    ]
    EXPORT_DIALOG = [
        '[role="dialog"]:has-text("Export to")',
        '[aria-modal="true"]:has-text("Export to")',
        'div[role="dialog"]',
    ]


class EmbaseFlow(Flow):
    name = "embase"

    async def run(self, page: Page, task: TaskConfig, outdir: Path,
                  logger: logging.Logger) -> None:
        try:
            await self._check_cloudflare_block(page, logger)
            await self._accept_cookies(page, logger)
            await self._dismiss_overlays(page, logger)
            await self._search(page, task.query, logger)
            await self._dismiss_overlays(page, logger)
            if task.date_filter.enabled:
                await self._set_date_filter(page, task, logger)
                await self._dismiss_overlays(page, logger)
            await self._set_per_page(page, task.per_page, logger)

            _cur, total_pages = await self._pagination_info(page)
            if task.max_pages > 0:
                total_pages = min(total_pages, task.max_pages)
            logger.info("检索结果共 %d 页", total_pages)

            if task.print_pdf.enabled:
                await self._goto_first_page(page, logger)
                await self._iterate_pages(
                    page, total_pages,
                    lambda p, i: self._print_page(p, task, outdir / "pdf", i, logger),
                    logger, "打印PDF",
                )
            if task.export_csv.enabled:
                await self._goto_first_page(page, logger)
                csv_dir = outdir / "csv"
                await self._iterate_pages(
                    page, total_pages,
                    lambda p, i: self._export_page(p, task, csv_dir, i, logger),
                    logger, "导出CSV",
                )
                self._merge_csv(csv_dir, logger)
        except Exception:
            await dump_debug(page, outdir, "error", logger)
            raise

    # ---------------- 检索与过滤 ----------------

    @staticmethod
    async def _is_cloudflare_blocked(page: Page) -> bool:
        try:
            title = await page.title()
            if "Attention Required" in title or "Cloudflare" in title:
                body = await page.inner_text("body")
                if "you have been blocked" in body.lower():
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _check_cloudflare_block(self, page: Page, logger: logging.Logger) -> None:
        """检测 Cloudflare 封锁页；等 8 秒刷新重试一次，仍被拦则给出明确报错。"""
        if not await self._is_cloudflare_blocked(page):
            return
        logger.warning("检测到 Cloudflare 封锁页，8 秒后刷新重试一次...")
        await asyncio.sleep(8)
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass
        await wait_ready(page, 5000)
        if await self._is_cloudflare_blocked(page):
            raise RuntimeError(
                "embase.com 的 Cloudflare 风控拦截了本次访问（常见于无头浏览器指纹或"
                "出口 IP 被临时风控）。建议: 1) 稍等片刻重试; 2) config.yaml 中 "
                "browser.channel 改用 msedge/chrome(真实浏览器指纹); "
                "3) 改用 --headed 显示窗口运行(此时 print_pdf 需关闭)。")
        logger.info("Cloudflare 封锁页已消失，继续")

    async def _accept_cookies(self, page: Page, logger: logging.Logger) -> None:
        """关闭 cookie 同意弹窗(OneTrust 可能延迟几秒才出现，且遮罩会拦截点击)。"""
        for sel in S.COOKIE_ACCEPT:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=3000)
                await btn.click()
                # 等遮罩层消失，避免挡住后续点击
                try:
                    await page.locator(".onetrust-pc-dark-filter").first.wait_for(
                        state="hidden", timeout=5000)
                except Exception:  # noqa: BLE001
                    pass
                logger.info("已接受 cookies 弹窗")
                return
            except Exception:  # noqa: BLE001
                continue

    async def _dismiss_overlays(self, page: Page, logger: logging.Logger) -> None:
        """关闭 Pendo 新手引导等遮挡弹窗（可能连续出现多个）。"""
        for _ in range(3):
            found = False
            for sel in S.OVERLAY_CLOSE:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible():
                        await btn.click(timeout=2000)
                        logger.info("已关闭引导弹窗(Pendo)")
                        found = True
                        await asyncio.sleep(0.8)
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not found:
                return

    async def _search(self, page: Page, query: str, logger: logging.Logger) -> None:
        await wait_ready(page, 8000)
        box = await first_visible(page, S.SEARCH_INPUT, timeout=30000)
        await fill_input_smart(box, query)
        btn = await first_visible(page, S.SHOW_RESULTS)
        await btn.click()
        try:
            await page.wait_for_url(re.compile(r"/results"), timeout=60000)
        except PWTimeout:
            pass
        await wait_ready(page, 8000)
        # 等分页栏或结果计数出现，确认结果页加载完成
        for sel in (S.PAGINATION, '[data-testid="search-results-count"]'):
            try:
                await page.locator(sel).first.wait_for(state="visible", timeout=15000)
                break
            except PWTimeout:
                continue
        logger.info("检索完成: %s", query)

    async def _set_date_filter(self, page: Page, task: TaskConfig, logger: logging.Logger) -> None:
        df = task.date_filter
        toggle = await first_visible(page, S.DATE_TOGGLE, timeout=15000)
        await toggle.click()
        await asyncio.sleep(0.5)

        if df.type == "records_added":
            # 勾选 “Records added to Embase”，填 Start date / End date (yyyy-mm-dd)
            await ensure_checkbox_by_text(
                page, re.compile(r"Records added to Embase"), True, logger, "Records added to Embase")
            await self._fill_labeled_input(page, "Start date", df.start)
            await self._fill_labeled_input(page, "End date", df.end)
        else:
            # 勾选 “Publication years”，在 From/To 下拉中选择年份
            await ensure_checkbox_by_text(
                page, re.compile(r"Publication years"), True, logger, "Publication years")
            await self._pick_year(page, "From", df.start, logger)
            await self._pick_year(page, "To", df.end, logger)

        # 点击查询框右侧的搜索按钮（放大镜）
        btn = await first_visible(page, S.SEARCH_BUTTON, timeout=10000)
        await btn.click()
        await wait_ready(page, 8000)
        try:
            await page.locator(S.PAGINATION).first.wait_for(state="visible", timeout=30000)
        except PWTimeout:
            pass
        await asyncio.sleep(1.5)
        logger.info("已应用日期过滤: %s ~ %s (%s)", df.start, df.end, df.type)

    async def _fill_labeled_input(self, page: Page, label_text: str, value: str) -> None:
        candidates = [
            page.get_by_label(re.compile(f"^{re.escape(label_text)}", re.I)),
            page.locator(f'input[aria-label*="{label_text}" i]'),
            page.locator(f'input[placeholder*="{label_text}" i]'),
            page.locator(f'xpath=//*[contains(normalize-space(text()),"{label_text}")]/following::input[1]'),
        ]
        for loc in candidates:
            try:
                target = loc.first
                await target.wait_for(state="visible", timeout=2500)
                await fill_input_smart(target, value)
                return
            except Exception:  # noqa: BLE001
                continue
        raise LookupError(f"找不到输入框: {label_text}")

    async def _pick_year(self, page: Page, which: str, year: str, logger: logging.Logger) -> None:
        """Publication years 的 From/To 年份下拉（原生 select 或自定义下拉都尝试一下）。"""
        try:
            sel = page.locator(
                f'xpath=//*[contains(normalize-space(text()),"{which}")]/following::select[1]').first
            await sel.wait_for(state="attached", timeout=2500)
            await sel.select_option(label=year)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            dd = page.locator(
                f'xpath=//*[contains(normalize-space(text()),"{which}")]/following::*[self::button or @role="combobox"][1]').first
            await dd.click(timeout=2500)
            await page.get_by_role("option", name=re.compile(f"^{year}$")).first.click(timeout=3000)
            return
        except Exception:  # noqa: BLE001
            pass
        logger.warning("未能设置 %s 年份为 %s，请人工核对", which, year)

    # ---------------- 每页条数与翻页 ----------------

    async def _set_per_page(self, page: Page, per_page: int, logger: logging.Logger) -> None:
        if not per_page:
            return
        # 结果页底部 “Display: N results per page” 自定义下拉([data-testid="page-size"])
        try:
            wrap = page.locator(S.PAGE_SIZE_WRAP)
            wrap_text = (await wrap.inner_text(timeout=8000)).strip()
            if re.search(rf"Display:\s*{per_page}\b", wrap_text):
                logger.info("每页条数已是 %d", per_page)
                return
            dd = wrap.locator('button[role="combobox"]').first
            await dd.scroll_into_view_if_needed()
            await dd.click()
            opt = page.locator('[role="listbox"] [role="option"]').get_by_text(
                re.compile(rf"^{per_page}$")).first
            await opt.wait_for(state="visible", timeout=5000)
            await opt.click()
            await wait_ready(page, 8000)
            await asyncio.sleep(2)  # 等结果列表刷新
            logger.info("已设置每页显示 %d 条", per_page)
        except Exception as e:  # noqa: BLE001
            logger.warning("设置每页 %d 条失败(%s)，将按当前默认条数继续", per_page, e)

    async def _pagination_info(self, page: Page) -> tuple[int, int]:
        """读取分页栏 "Page X of Y"，找不到时视为只有 1 页。"""
        try:
            text = await page.locator(S.PAGINATION).first.inner_text(timeout=5000)
            m = PAGE_NUM_RE.search(text)
            if m:
                return int(m.group(1)), int(m.group(2).replace(",", ""))
        except Exception:  # noqa: BLE001
            pass
        return 1, 1

    async def _next_page(self, page: Page, current: int, logger: logging.Logger) -> None:
        btn = await first_visible(page, S.NEXT_BUTTON, timeout=10000)
        await btn.scroll_into_view_if_needed()
        await btn.click()
        for _ in range(80):  # 最多等 40 秒页码更新
            await asyncio.sleep(0.5)
            cur, _total = await self._pagination_info(page)
            if cur >= current + 1:
                return
        raise TimeoutError(f"点击 Next 后页码未从第 {current} 页更新")

    async def _goto_first_page(self, page: Page, logger: logging.Logger) -> None:
        for _ in range(200):
            cur, _total = await self._pagination_info(page)
            if cur <= 1:
                return
            try:
                btn = await first_visible(page, S.PREV_BUTTON, timeout=5000)
                await btn.scroll_into_view_if_needed()
                await btn.click()
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"返回第 1 页失败: {e}")
            for _ in range(80):
                await asyncio.sleep(0.5)
                new_cur, _t = await self._pagination_info(page)
                if new_cur < cur:
                    break
        logger.info("已回到第 1 页")

    async def _iterate_pages(self, page: Page, total_pages: int, action,
                             logger: logging.Logger, what: str) -> None:
        for i in range(1, total_pages + 1):
            logger.info("%s: 第 %d/%d 页", what, i, total_pages)
            await action(page, i)
            if i < total_pages:
                await self._next_page(page, i, logger)

    # ---------------- 打印 PDF ----------------

    async def _print_page(self, page: Page, task: TaskConfig, pdf_dir: Path,
                          idx: int, logger: logging.Logger) -> None:
        pdf_dir.mkdir(parents=True, exist_ok=True)
        path = pdf_dir / f"page_{idx:03d}.pdf"
        await page.evaluate("window.scrollTo(0, 0)")
        kwargs: dict = {
            "path": str(path),
            "print_background": True,
            "format": task.print_pdf.paper_format,
            "scale": task.print_pdf.scale,
        }
        if task.print_pdf.header_footer:
            kwargs.update(
                display_header_footer=True,
                header_template=HEADER_TPL,
                footer_template=FOOTER_TPL,
                margin={"top": "1.5cm", "bottom": "1.5cm", "left": "1.2cm", "right": "1.2cm"},
            )
        try:
            await page.pdf(**kwargs)
        except Exception as e:  # noqa: BLE001
            if "headless" in str(e).lower():
                raise RuntimeError(
                    "导出 PDF 需要无头模式(headless: true)。请在配置中开启 headless，"
                    "或将 print_pdf.enabled 设为 false。"
                ) from e
            raise
        logger.info("已保存 %s", path)

    # ---------------- 导出 CSV ----------------

    async def _export_page(self, page: Page, task: TaskConfig, csv_dir: Path,
                           idx: int, logger: logging.Logger) -> None:
        csv_dir.mkdir(parents=True, exist_ok=True)
        await self._select_current_page(page, logger)

        exp = await first_visible(page, S.EXPORT_BUTTON, timeout=10000)
        await exp.scroll_into_view_if_needed()
        await exp.click()

        dlg = await first_visible(page, S.EXPORT_DIALOG, timeout=15000)
        await self._ensure_export_format_csv(dlg, logger)
        await self._set_fields_by(dlg, task.export_csv.fields_by, logger)
        await self._set_export_fields(page, dlg, task.export_csv.fields, logger)

        btn = await first_visible(dlg, [
            'button:has-text("Export")',
            'button[type="submit"]',
        ], timeout=10000)
        async with page.expect_download(timeout=180000) as dl_info:
            await btn.click()
        download = await dl_info.value
        path = csv_dir / f"page_{idx:03d}.csv"
        await download.save_as(str(path))
        logger.info("已保存 %s", path)
        try:
            await page.keyboard.press("Escape")  # 若对话框未自动关闭
        except Exception:  # noqa: BLE001
            pass

    async def _select_current_page(self, page: Page, logger: logging.Logger) -> None:
        """勾选结果列表的“Select page results”复选框（选中当前页全部结果）。

        该 checkbox 是自定义样式(input 视觉隐藏)，用 JS click 触发框架事件最可靠。
        """
        try:
            cb = page.locator(S.SELECT_PAGE_CB).first
            await cb.wait_for(state="attached", timeout=8000)
            if not await cb.is_checked():
                await cb.evaluate("(el) => el.click()")
                await asyncio.sleep(0.5)
            if not await cb.is_checked():
                await page.locator('label[title="Select page results"]').first.click(timeout=3000)
                await asyncio.sleep(0.5)
        except Exception:  # noqa: BLE001
            # 兜底：点结果列表上方的“Select all”文本对应的复选框
            logger.warning("未找到 Select page results 复选框，尝试勾选 Select all")
            await ensure_checkbox_by_text(
                page, re.compile(r"^Select all$"), True, logger, "Select all")
        # 确认出现了 “N selected” 或 Export 按钮变为可用
        try:
            txt = await page.get_by_text(re.compile(r"[\d,]+\s+selected")).first.inner_text(timeout=5000)
            logger.info("已勾选: %s", txt.strip())
        except Exception:  # noqa: BLE001
            try:
                checked = await page.locator(S.SELECT_PAGE_CB).first.is_checked()
                if not checked:
                    logger.warning("本页结果勾选状态未确认，仍继续导出，请人工核对")
            except Exception:  # noqa: BLE001
                logger.warning("本页结果勾选状态未确认，仍继续导出，请人工核对")

    async def _ensure_export_format_csv(self, dlg, logger: logging.Logger) -> None:
        """导出对话框 “Export to” 选择 CSV（默认通常已是 CSV）。"""
        try:
            cur = dlg.locator(
                '[role="combobox"], button[aria-haspopup], select').first
            text = (await cur.inner_text(timeout=3000)).strip()
            if "csv" in text.lower():
                return
            await cur.click()
            opt = await first_visible(dlg, [
                '[role="option"]:text-matches("^CSV$", "i")',
                'text=/^CSV$/',
            ], timeout=3000)
            await opt.click()
            logger.info("导出格式已选择 CSV")
        except Exception as e:  # noqa: BLE001
            logger.warning("未能确认导出格式为 CSV(%s)，按当前默认继续", e)

    async def _set_fields_by(self, dlg, fields_by: str, logger: logging.Logger) -> None:
        """Fields by: Row / Column 单选。"""
        name = "Column" if fields_by == "column" else "Row"
        try:
            radio = dlg.get_by_label(re.compile(f"^{name}$", re.I)).first
            await radio.check(timeout=3000)
        except Exception:  # noqa: BLE001
            try:
                await dlg.get_by_text(re.compile(f"^{name}$")).first.click(timeout=3000)
            except Exception as e:  # noqa: BLE001
                logger.warning("设置 Fields by=%s 失败(%s)，按当前默认继续", name, e)

    async def _set_export_fields(self, page: Page, dlg, wanted_fields: list[str],
                                 logger: logging.Logger) -> None:
        """按字段文字勾选/取消对话框内所有 checkbox，使其恰好等于 wanted_fields。"""
        wanted = {f.strip() for f in wanted_fields}
        handle = await dlg.element_handle()
        boxes = await page.evaluate(CHECKBOX_MAP_JS, handle)
        available = {b["text"] for b in boxes}
        missing = wanted - available
        if missing:
            logger.warning("以下字段在导出对话框中未找到(文字需与网页完全一致): %s；可用字段: %s",
                           sorted(missing), sorted(available))
        for b in boxes:
            if b["disabled"]:
                continue
            want = b["text"] in wanted
            if b["checked"] != want:
                await toggle_checkbox(dlg, b["index"])
        # 复核一遍状态
        boxes = await page.evaluate(CHECKBOX_MAP_JS, handle)
        bad = [b["text"] for b in boxes if not b["disabled"] and b["checked"] != (b["text"] in wanted)]
        if bad:
            logger.warning("部分字段勾选状态未生效: %s，请人工核对", bad)
        else:
            logger.info("导出字段已勾选: %s", sorted(wanted & available))

    # ---------------- CSV 合并 ----------------

    def _merge_csv(self, csv_dir: Path, logger: logging.Logger) -> None:
        """把逐页导出的 page_*.csv 合并为 export_all.csv（首个文件的表头只写一次）。"""
        files = sorted(csv_dir.glob("page_*.csv"))
        if not files:
            return
        out = csv_dir / "export_all.csv"
        total = 0
        header_written = False
        with open(out, "w", newline="", encoding="utf-8-sig") as w:
            writer = csv.writer(w)
            for f in files:
                with open(f, newline="", encoding="utf-8-sig") as r:
                    reader = csv.reader(r)
                    header = next(reader, None)
                    if header is None:
                        continue
                    if not header_written:
                        writer.writerow(header)
                        header_written = True
                    for row in reader:
                        writer.writerow(row)
                        total += 1
        logger.info("已合并 %d 个 CSV -> %s (共 %d 条记录)", len(files), out, total)
