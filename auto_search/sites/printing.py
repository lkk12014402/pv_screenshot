"""共享的 PDF 打印实现（Embase / Pubmed 等流程复用）。

页眉页脚模板按 Edge/Chrome 打印预览"页眉和页脚"的实际输出调校（A4、1cm 页边距、
10.25px 字体、页眉左日期+标题居中、页脚左网址+右页码）。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from playwright.async_api import Page

# 可用占位符 class: date / title / url / pageNumber / totalPages
HEADER_TPL = (
    '<div style="display:flex;width:100%;font-size:10.25px;font-family:sans-serif;'
    'padding-left:32px;box-sizing:border-box;margin-top:0.3px;">'
    '<span class="date"></span>'
    '<span class="title" style="flex:1;text-align:center;"></span></div>'
)
FOOTER_TPL = (
    '<div style="display:flex;width:100%;font-size:10.25px;font-family:sans-serif;'
    'justify-content:space-between;padding:0 32px 1.3px;box-sizing:border-box;">'
    '<span class="url"></span>'
    '<span><span class="pageNumber"></span>/<span class="totalPages"></span></span></div>'
)
# 页边距 1cm（与浏览器打印预览在公制区域的"默认"页边距一致）；CDP 参数用英寸
PRINT_MARGIN_CM = 1.0
PRINT_MARGIN_IN = PRINT_MARGIN_CM / 2.54


def pdf_templates(task) -> tuple[str, str]:
    """优先用配置里的自定义模板，否则用内置默认。"""
    header = task.print_pdf.header_template.strip() or HEADER_TPL
    footer = task.print_pdf.footer_template.strip() or FOOTER_TPL
    return header, footer


async def print_page(page: Page, task, pdf_dir: Path, idx: int,
                     logger: logging.Logger, headless: bool = True) -> None:
    """打印当前页面为 pdf/page_NNN.pdf；有头模式用 CDP 直打或克隆兜底。"""
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
        header, footer = pdf_templates(task)
        kwargs.update(
            display_header_footer=True,
            header_template=header,
            footer_template=footer,
            margin={"top": f"{PRINT_MARGIN_IN}in", "bottom": f"{PRINT_MARGIN_IN}in",
                    "left": f"{PRINT_MARGIN_IN}in", "right": f"{PRINT_MARGIN_IN}in"},
        )
    if headless:
        await page.pdf(**kwargs)
    else:
        await _print_headed(page, path, task, logger)
    logger.info("已保存 %s", path)


async def _print_headed(page: Page, path: Path, task, logger: logging.Logger) -> None:
    """有头模式下打印 PDF（page.pdf 仅支持无头 Chromium）。

    方式1: 直接发 CDP Page.printToPDF(新版 Chromium 有头模式部分支持)；
    方式2(兜底): 把渲染好的整页 DOM 克隆到临时无头浏览器里再打印。
    """
    import base64

    # ---- 方式1: CDP 直接打印 ----
    try:
        session = await page.context.new_cdp_session(page)
        params: dict = {
            "printBackground": True,
            "scale": task.print_pdf.scale,
            "preferCSSPageSize": False,
        }
        # 纸张尺寸(英寸): Letter=8.5x11, A4=8.27x11.69
        if task.print_pdf.paper_format.upper() == "A4":
            params.update(paperWidth=8.27, paperHeight=11.69)
        else:
            params.update(paperWidth=8.5, paperHeight=11.0)
        if task.print_pdf.header_footer:
            header, footer = pdf_templates(task)
            params.update(
                displayHeaderFooter=True,
                headerTemplate=header,
                footerTemplate=footer,
                marginTop=PRINT_MARGIN_IN, marginBottom=PRINT_MARGIN_IN,
                marginLeft=PRINT_MARGIN_IN, marginRight=PRINT_MARGIN_IN,
            )
        resp = await asyncio.wait_for(session.send("Page.printToPDF", params), timeout=60)
        path.write_bytes(base64.b64decode(resp["data"]))
        return
    except Exception as e:  # noqa: BLE001
        logger.info("有头模式 CDP 直接打印不可用(%s)，改用无头克隆打印", e)

    # ---- 方式2: 克隆当前页面到临时无头浏览器打印 ----
    html = await page.content()
    if "<base" not in html.lower():  # 让相对/绝对路径资源按原站(VPN域名)加载
        html = html.replace("<head>", f'<head><base href="{page.url}">', 1)
    src_browser = page.context.browser
    clone_browser = await src_browser.browser_type.launch(headless=True)
    try:
        clone_ctx = await clone_browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1600, "height": 900},
            user_agent=await page.evaluate("navigator.userAgent"),
        )
        try:
            await clone_ctx.add_cookies(await page.context.cookies())
        except Exception:  # noqa: BLE001
            pass
        cp = await clone_ctx.new_page()
        await cp.set_content(html, wait_until="load", timeout=60000)
        try:
            await cp.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        kwargs: dict = {
            "path": str(path),
            "print_background": True,
            "format": task.print_pdf.paper_format,
            "scale": task.print_pdf.scale,
        }
        if task.print_pdf.header_footer:
            header, footer = pdf_templates(task)
            kwargs.update(
                display_header_footer=True,
                header_template=header,
                footer_template=footer,
                margin={"top": f"{PRINT_MARGIN_IN}in", "bottom": f"{PRINT_MARGIN_IN}in",
                        "left": f"{PRINT_MARGIN_IN}in", "right": f"{PRINT_MARGIN_IN}in"},
            )
        await cp.pdf(**kwargs)
    finally:
        await clone_browser.close()
