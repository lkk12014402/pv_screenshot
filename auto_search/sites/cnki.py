"""知网 (c61.oversea.cnki.net -> kns.cnki.net)：登录 + 专业检索流程。

流程: 个人登录 -> 高级检索 -> 专业检索 -> 填检索式+发表时间 -> 检索 ->
显示条数 -> 逐页[全选 -> 打印PDF -> 导出与分析->导出文献->GB/T 7714-2025->导出txt]。

注意: 结果为空(暂无数据)时也打印结果页 PDF(留证)，只跳过导出。
选择器集中在类 S 中；失败时输出目录 _debug/ 有截图和 HTML 可对照。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PWTimeout

from ..config import AccountConfig, TaskConfig
from ..utils import dump_debug, fill_input_smart, first_visible, wait_ready
from .base import Flow, SitePlugin
from .printing import print_page as _shared_print_page

RESULT_COUNT_RE = re.compile(r"共找到\s*([\d,]+)\s*条结果")


class S:  # noqa: D101 - 选择器集中处
    # ---- 登录(c61.oversea.cnki.net 首页右侧“个人登录”面板) ----
    LOGIN_ENTRY = [
        'a:has-text("个人登录")',
        'button:has-text("个人登录")',
    ]
    LOGIN_USER = [
        'input[placeholder*="账号"]',
        'input[placeholder*="手机"]',
        'input[name*="user" i]',
        'input[type="text"]:visible',
    ]
    LOGIN_PWD = ['input[type="password"]:visible']
    # “登录即同意中国知网使用协议、隐私政策” 勾选框
    AGREEMENT = ['text=登录即同意']
    LOGIN_SUBMIT = [
        'button:has-text("登 录")',
        'button:has-text("登录")',
        'a:has-text("登 录")',
    ]

    # ---- 高级检索入口(首页搜索框右侧) ----
    ADV_SEARCH = [
        'a:has-text("高级检索")',
        'text=高级检索',
    ]

    # ---- 专业检索页(kns.cnki.net/kns8s/AdvSearch) ----
    PRO_TAB = ['li[name="majorSearch"]', 'li:has-text("专业检索")',
               'a:has-text("专业检索")', 'text=专业检索']
    # 专业检索大输入框
    QUERY_INPUT = [
        'textarea#text_searchpro',
        'textarea.textarea-major',
        'div#ModuleSearchPro textarea',
        'textarea:visible',
    ]
    # 时间范围: 发表时间 [start] -- [end] (格式 yyyy-mm-dd)
    DATE_START = [
        'input[placeholder*="起始"]',
        'input[placeholder*="开始"]',
        'div:has-text("时间范围") >> .. >> input:visible >> nth=0',
    ]
    DATE_END = [
        'input[placeholder*="结束"]',
        'input[placeholder*="截止"]',
        'div:has-text("时间范围") >> .. >> input:visible >> nth=1',
    ]
    SEARCH_BTN = [
        'input.search-btn',           # kns8s 检索按钮(input type=button value=检索)
        'input[type="button"][value*="检"]',
        'button:has-text("检 索")',
        'button:has-text("检索")',
    ]
    RESULT_COUNT_TEXT = re.compile(r"共找到\s*[\d,]+\s*条结果")
    NO_RESULT_TEXT = re.compile(r"暂无数据")

    # ---- 结果工具条 ----
    SELECT_ALL = [
        'input[type="checkbox"][id*="selectall" i]',
        'label:has-text("全选")',
    ]
    # “显示 20▾”下拉(自定义) -> 选项 10/20/50
    PAGE_SIZE_WRAP_TEXT = re.compile(r"显示")
    # 导出与分析 -> 导出文献 -> GB/T 7714-2025 格式引文
    # 真实结构: ul.dropdown-analysis-btns > li > a(导出与分析); div:has-text 会误命中祖先容器
    EXPORT_MENU = ['ul.dropdown-analysis-btns a:has-text("导出与分析")',
                   'a:has-text("导出与分析")',
                   'li:has-text("导出与分析")',
                   'text=导出与分析']
    EXPORT_LIT = ['a:has-text("导出文献")', 'text=导出文献']
    GBT_LINK = ['text=GB/T 7714-2025 格式引文', 'a:has-text("GB/T 7714-2025")']
    # 文献管理中心(dm8/manage/export.html)的导出按钮
    EXPORT_BTN = [
        'a:has-text("导 出")',
        'a:has-text("导出")',
        'button:has-text("导出")',
    ]


class CnkiSite(SitePlugin):
    name = "cnki"
    base_url = "https://c61.oversea.cnki.net/"

    flows: dict = {}  # 在文件末尾注册，避免循环引用

    async def login(self, context: BrowserContext, account: AccountConfig,
                    outdir: Path, logger: logging.Logger, task=None) -> Page:
        page = await context.new_page()
        base_url = (getattr(task, "base_url", "") or "").strip() or self.base_url
        logger.info("打开 %s", base_url)
        await page.goto(base_url, wait_until="domcontentloaded")
        await wait_ready(page, 8000)

        # 打开“个人登录”面板(面板可能默认已展开; 此时再点入口会把它关掉!)
        if await self._pwd_visible(page, timeout_s=5):
            logger.info("登录面板已展开，直接填账号密码")
        else:
            logger.info("点击「个人登录」展开登录面板")
            try:
                entry = await first_visible(page, S.LOGIN_ENTRY, timeout=15000)
                await entry.click()
            except LookupError:
                await dump_debug(page, outdir, "login_entry_missing", logger)
                raise RuntimeError("未找到「个人登录」入口(页面结构可能已变，见 _debug 快照)")
            if not await self._pwd_visible(page, timeout_s=15):
                await dump_debug(page, outdir, "login_panel_missing", logger)
                raise RuntimeError("点击「个人登录」后密码框仍未出现(见 _debug 快照)")

        logger.info("填入账号")
        user = await first_visible(page, S.LOGIN_USER, timeout=20000)
        await fill_input_smart(user, account.username)
        logger.info("填入密码")
        pwd = await first_visible(page, S.LOGIN_PWD, timeout=10000)
        await fill_input_smart(pwd, account.password)

        # 勾选“登录即同意...”协议(不勾选可能无法登录)
        try:
            await page.get_by_text(re.compile("登录即同意")).first.click(timeout=3000)
        except Exception:  # noqa: BLE001
            logger.warning("未找到协议勾选框，继续尝试登录")

        btn = await first_visible(page, S.LOGIN_SUBMIT, timeout=10000)
        await btn.click()

        # 登录成功: “个人登录”入口消失、右上角出现脱敏手机号
        try:
            await page.wait_for_function(
                "() => !document.body.innerText.includes('个人登录') || "
                r"/\d{3}\*{2,}\d{4}/.test(document.body.innerText)",
                timeout=20000)
            await wait_ready(page)
            logger.info("登录成功: %s", page.url)
            return page
        except PWTimeout:
            await dump_debug(page, outdir, "login_failed", logger)
            raise RuntimeError(
                "登录可能失败。如出现滑块/验证码，请改用有头模式(--headed 或界面取消无头)人工通过后再运行。")

    @staticmethod
    async def _pwd_visible(page: Page, timeout_s: float = 5) -> bool:
        """登录面板的密码框是否已可见(判断面板是否已展开)。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if await page.locator('input[type="password"]:visible').count() > 0:
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        return False

    async def open_database(self, page: Page, db_name: str, logger: logging.Logger,
                            category: str = "") -> Page:
        """点击首页“高级检索”，跳到高级检索页(可能新标签页)。

        目标地址按主机不同: 官网 kns.cnki.net/kns8s/AdvSearch; 镜像 oversea.cnki.net/kns8s/advsearch。
        统一按 url 里含 kns8s/advsearch(不区分大小写)判断。
        """
        adv = await first_visible(page, S.ADV_SEARCH, timeout=20000)
        await adv.click(no_wait_after=True)
        deadline = time.monotonic() + 30
        db_page = None

        def _is_adv(url: str) -> bool:
            u = url.lower()
            return "kns8s" in u and "advsearch" in u

        while time.monotonic() < deadline:
            for p in page.context.pages:
                if not p.is_closed() and _is_adv(p.url):
                    db_page = p
                    break
            if not db_page and not page.is_closed() and _is_adv(page.url):
                db_page = page
            if db_page:
                break
            await asyncio.sleep(0.5)
        if db_page is None:
            raise RuntimeError("点击高级检索后未进入高级检索页(kns8s/advsearch)")
        await db_page.bring_to_front()
        await wait_ready(db_page, 10000)
        logger.info("已进入高级检索页: %s", db_page.url)
        return db_page


class CnkiFlow(Flow):
    name = "cnki"

    async def run(self, page: Page, task: TaskConfig, outdir: Path,
                  logger: logging.Logger, headless: bool = True) -> None:
        try:
            await self._pro_search(page, task, logger)

            count = await self._results_count(page, logger)
            total_pages = self._total_pages(count, task)
            if task.max_pages > 0:
                total_pages = min(total_pages, task.max_pages)
            logger.info("检索结果 %s 条, 共 %d 页", count, total_pages)

            await self._set_page_size(page, task.per_page, logger)

            for i in range(1, total_pages + 1):
                logger.info("处理第 %d/%d 页", i, total_pages)
                await self._select_all(page, logger)
                if task.print_pdf.enabled:
                    await _shared_print_page(page, task, outdir / "pdf", i, logger, headless)
                if task.export_csv.enabled and count > 0:
                    await self._export_page(page, task, outdir / "txt", i, logger)
                if i < total_pages:
                    await self._next_page(page, logger)

            if task.export_csv.enabled and count > 0:
                self._merge_txt(outdir / "txt", logger)
        except Exception:
            await dump_debug(page, outdir, "error", logger)
            raise

    # ---------------- 专业检索 ----------------

    async def _pro_search(self, page: Page, task: TaskConfig, logger: logging.Logger) -> None:
        await wait_ready(page, 10000)
        # 切到“专业检索”tab
        tab = await first_visible(page, S.PRO_TAB, timeout=20000)
        await tab.click()
        await asyncio.sleep(1)

        # 填检索式
        box = await first_visible(page, S.QUERY_INPUT, timeout=20000)
        await fill_input_smart(box, task.query)

        # 填发表时间范围
        if task.date_filter.enabled:
            start = await first_visible(page, S.DATE_START, timeout=10000)
            await fill_input_smart(start, task.date_filter.start)
            end = await first_visible(page, S.DATE_END, timeout=10000)
            await fill_input_smart(end, task.date_filter.end)

        # 点“检索”
        btn = await first_visible(page, S.SEARCH_BTN, timeout=10000)
        await btn.click()
        # 等结果区出现: “共找到 N 条结果” 或 “暂无数据”
        try:
            await page.wait_for_function(
                """() => /共找到\\s*[\\d,]+\\s*条结果/.test(document.body.innerText)
                    || document.body.innerText.includes('暂无数据')""",
                timeout=120000)
        except PWTimeout as e:
            raise RuntimeError("检索后未等到结果区(共找到/暂无数据)") from e
        await wait_ready(page, 8000)
        logger.info("检索完成: %s", task.query[:60])

    async def _results_count(self, page: Page, logger: logging.Logger) -> int:
        """读取“共找到 N 条结果”；暂无数据返回 0。"""
        try:
            text = await page.locator("body").inner_text(timeout=8000)
            m = RESULT_COUNT_RE.search(text)
            if m:
                n = int(m.group(1).replace(",", ""))
                logger.info("结果数: %d 条", n)
                return n
            if S.NO_RESULT_TEXT.search(text):
                logger.info("结果数: 0 条(暂无数据)")
                return 0
        except Exception:  # noqa: BLE001
            pass
        logger.warning("未能读取结果数，按 0 处理")
        return 0

    @staticmethod
    def _total_pages(count: int, task: TaskConfig) -> int:
        """总页数 = ceil(结果数 / 每页条数)。"""
        if count <= 0:
            return 1  # 空结果也打印 1 页留证
        per = task.per_page or 50
        return max(1, -(-count // per))  # ceil

    # ---------------- 每页条数 ----------------

    async def _set_page_size(self, page: Page, per_page: int, logger: logging.Logger) -> None:
        """结果工具条右侧 “显示 N▾” 自定义下拉，选项 10/20/50。"""
        if not per_page:
            return
        try:
            # 点“显示”区域的下拉
            dd = await first_visible(page, [
                'div:has-text("显示") select',
                'select:has(option:text-is("50"))',
                'text=/^显示$/ >> .. >> [role="combobox"]',
                'text=/^显示$/ >> .. >> div >> nth=-1',
            ], timeout=8000)
            tag = await dd.evaluate("(el) => el.tagName.toLowerCase()")
            if tag == "select":
                await dd.select_option(label=str(per_page))
            else:
                await dd.click()
                opt = await first_visible(page, [
                    f'[role="option"]:text-is("{per_page}")',
                    f'li:text-is("{per_page}")',
                    f'text=/^{per_page}$/',
                ], timeout=4000)
                await opt.click()
            await wait_ready(page, 8000)
            logger.info("已设置每页显示 %d 条", per_page)
        except Exception as e:  # noqa: BLE001
            logger.warning("设置每页 %d 条失败(%s)，按当前默认条数继续", per_page, e)

    # ---------------- 全选 / 翻页 ----------------

    async def _select_all(self, page: Page, logger: logging.Logger) -> None:
        """勾选工具条“全选”(选中本页全部)。打印和导出前必须调用。

        知网翻页后已选会累计保留，先点“清除”再全选，避免跨页重复导出。
        """
        try:
            clear = page.get_by_text(re.compile(r"^清除$")).first
            if await clear.is_visible():
                await clear.click(timeout=2000)
                await asyncio.sleep(0.5)
        except Exception:  # noqa: BLE001
            pass
        for sel in S.SELECT_ALL:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=8000)
                if await loc.get_attribute("type") == "checkbox" or await loc.get_attribute("role") == "checkbox":
                    if not await loc.is_checked():
                        await loc.evaluate("(el) => el.click()")
                else:
                    await loc.click()
                await asyncio.sleep(0.5)
                return
            except Exception:  # noqa: BLE001
                continue
        raise LookupError("找不到“全选”勾选框")

    async def _next_page(self, page: Page, logger: logging.Logger) -> None:
        """翻到下一页。CNKI 分页形如 “2/120” 带前后箭头。"""
        btn = await first_visible(page, [
            'a.next-page', 'button.next-page',
            '[class*="page"] [class*="next"]',
            'a:has-text("下一页")',
            'li:has-text(">")',
        ], timeout=10000)
        await btn.scroll_into_view_if_needed()
        await btn.click()
        await wait_ready(page, 10000)

    # ---------------- 导出 ----------------

    async def _export_page(self, page: Page, task: TaskConfig, txt_dir: Path,
                           idx: int, logger: logging.Logger) -> None:
        """导出与分析 -> 导出文献 -> GB/T 7714-2025 -> 新标签页(文献管理中心) -> 导出txt。"""
        txt_dir.mkdir(parents=True, exist_ok=True)
        menu = await first_visible(page, S.EXPORT_MENU, timeout=10000)
        await menu.click()
        # 等“导出文献”子菜单出现; 点击没展开则改用悬停展开
        lit = None
        for attempt in range(3):
            try:
                lit = await first_visible(page, S.EXPORT_LIT, timeout=4000)
                break
            except LookupError:
                if attempt == 0:
                    logger.info("点击未展开导出菜单，改用悬停")
                    await menu.hover()
                else:
                    await menu.hover()
                    await asyncio.sleep(1)
        if lit is None:
            await dump_debug(page, txt_dir.parent, "export_menu", logger)
            raise LookupError("「导出与分析」菜单点击/悬停后都没出现「导出文献」(见 _debug 快照)")
        await lit.hover()
        await asyncio.sleep(0.8)
        gbt = await first_visible(page, S.GBT_LINK, timeout=5000)
        await gbt.click()

        # 文献管理中心可能新开标签页
        before = list(page.context.pages)
        deadline = time.monotonic() + 30
        exp_page = None
        while time.monotonic() < deadline:
            for p in page.context.pages:
                if not p.is_closed() and p not in before and "manage/export" in p.url:
                    exp_page = p
                    break
            if not page.is_closed() and "manage/export" in page.url:
                exp_page = page
                break
            if exp_page:
                break
            await asyncio.sleep(0.5)
        if exp_page is None:
            raise RuntimeError("未进入文献管理中心(manage/export)")
        await exp_page.bring_to_front()
        await wait_ready(exp_page, 10000)

        btn = await first_visible(exp_page, S.EXPORT_BTN, timeout=15000)
        async with exp_page.expect_download(timeout=180000) as dl_info:
            await btn.click()
        download = await dl_info.value
        path = txt_dir / f"page_{idx:03d}.txt"
        await download.save_as(str(path))
        logger.info("已保存 %s", path)

        # 关掉导出标签页，回到结果页继续
        if exp_page is not page:
            try:
                await exp_page.close()
            except Exception:  # noqa: BLE001
                pass
            await page.bring_to_front()

    # ---------------- txt 合并 ----------------

    def _merge_txt(self, txt_dir: Path, logger: logging.Logger) -> None:
        """把逐页导出的 page_*.txt 合并为 export_all.txt。"""
        files = sorted(txt_dir.glob("page_*.txt"))
        if not files:
            return
        out = txt_dir / "export_all.txt"
        with open(out, "w", encoding="utf-8") as w:
            for f in files:
                w.write(f.read_text(encoding="utf-8", errors="replace"))
                w.write("\n")
        logger.info("已合并 %d 个 TXT -> %s", len(files), out)


# 注册流程（放末尾避免与类定义的相互引用）
CnkiSite.flows = {"cnki": CnkiFlow}
