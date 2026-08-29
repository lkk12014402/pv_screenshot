"""通用工具：日志、等待、元素查找、勾选框处理、调试快照。"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PWTimeout


def get_logger(name: str, log_file: Path | None = None) -> logging.Logger:
    """控制台 + 可选文件 双输出的 logger。"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 避免子 logger 的消息被父 logger 重复输出
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


async def wait_ready(page: Page, networkidle_ms: int = 5000) -> None:
    """等待页面基本加载完成（networkidle 尽力而为，超时不算错误）。"""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PWTimeout:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=networkidle_ms)
    except PWTimeout:
        pass


async def first_visible(scope, selectors: list[str], timeout: float = 10000):
    """按顺序尝试一组选择器，返回第一个可见的 Locator；都找不到则抛错。

    scope 可以是 Page 或 Locator（用于限定在对话框等局部区域内查找）。
    """
    per = max(800.0, timeout / max(1, len(selectors)))
    tried = []
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            await loc.wait_for(state="visible", timeout=per)
            return loc
        except Exception:  # noqa: BLE001 - 这里只关心最终是否找到
            tried.append(sel)
    raise LookupError("找不到可见元素，已依次尝试: " + " | ".join(tried))


async def dump_debug(page: Page, outdir: Path, name: str, logger: logging.Logger) -> None:
    """出错时保存整页截图和 HTML，便于人工核对网页结构是否变化。"""
    try:
        dbg = outdir / "_debug"
        dbg.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%H%M%S")
        await page.screenshot(path=str(dbg / f"{ts}_{name}.png"), full_page=True)
        (dbg / f"{ts}_{name}.html").write_text(await page.content(), encoding="utf-8")
        logger.info("已保存调试快照到 %s", dbg)
    except Exception as e:  # noqa: BLE001
        logger.warning("保存调试快照失败: %s", e)


async def fill_input_smart(loc, value: str) -> None:
    """填写输入框；若被 JS 框架接管导致 fill 不生效，用原生 setter + 事件兜底。"""
    await loc.click()
    try:
        await loc.fill(value, timeout=3000)
        return
    except Exception:  # noqa: BLE001
        pass
    await loc.evaluate(
        """(el, v) => {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, v);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        value,
    )


async def checkbox_state_by_text(page: Page, text_pattern) -> bool | None:
    """找到包含指定文本的 label，返回其关联 checkbox 的勾选状态（找不到返回 None）。"""
    label = page.get_by_text(text_pattern).first
    handle = await label.element_handle()
    if handle is None:
        return None
    return await page.evaluate(
        """(el) => {
            const lab = el.closest('label');
            if (lab && lab.control) return lab.control.checked;
            let node = el;
            for (let i = 0; i < 5 && node; i++) {
                if (node.querySelector) {
                    const cb = node.querySelector('input[type="checkbox"]');
                    if (cb) return cb.checked;
                }
                node = node.parentElement;
            }
            return null;
        }""",
        handle,
    )


async def ensure_checkbox_by_text(page: Page, text_pattern, want: bool, logger: logging.Logger, desc: str = "") -> None:
    """确保某个文字标签对应的 checkbox 处于期望状态（点击文字标签进行切换）。"""
    label = page.get_by_text(text_pattern).first
    await label.wait_for(state="visible", timeout=10000)
    state = await checkbox_state_by_text(page, text_pattern)
    if state is None:
        logger.warning("未能确认勾选框状态(%s)，直接点击一次，请人工核对", desc or text_pattern)
        await label.click()
        return
    if state != want:
        await label.click()


# 列出某个容器内所有 checkbox 及其文字标签（用于导出对话框的字段勾选）
CHECKBOX_MAP_JS = """(root) => Array.from(root.querySelectorAll('input[type="checkbox"]')).map((cb, i) => {
    let text = '';
    if (cb.labels && cb.labels.length) text = cb.labels[0].innerText;
    else if (cb.closest('label')) text = cb.closest('label').innerText;
    else if (cb.parentElement) text = cb.parentElement.innerText;
    return {index: i, text: text.trim(), checked: cb.checked, disabled: cb.disabled};
})"""


async def toggle_checkbox(scope, index: int) -> None:
    """通过 JS 点击容器内第 index 个 checkbox（触发现有框架的事件处理）。"""
    await scope.locator('input[type="checkbox"]').nth(index).evaluate("(el) => el.click()")
    await asyncio.sleep(0.1)
