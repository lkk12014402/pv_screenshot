"""检索去重模块: 新检索结果(万方导出 xlsx) 与 历史检索文件比对去重。

历史检索文件(histories/base-data-YYYYMMDD.xlsx)结构:
- Sheet1 台账: 日期 | 产品 | pubmed检索数量 | pubmed去重后数量 | embase检索数量 |
  embase去重后数量 | 知网检索数量 | 知网去重后数量 | 万方检索数量 | 万方去重后数量 | 检索时间段
- Sheet2 明细: 日期 | 数据库 | 化合物 | 完整文献名称 | 文章名 | 结果 | 人员 | 备注

匹配方式: 文章名 RapidFuzz 模糊匹配, 范围限定在 同数据库(万方)+同化合物(成分)。
判定: score >= hi → 重复; lo <= score < hi → 待确认(界面逐条 double confirm);
score < lo → 新增。用户确认后的新增条目追加进 Sheet2, 并在 Sheet1 追加一行台账,
另存为 histories/ 下的新日期文件(不覆盖旧文件, 增量更新)。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from rapidfuzz import fuzz, process

# ---------------- 文本归一化 ----------------

_TRANS = str.maketrans({
    "‑": "-",  # U+2011 不断字连字符 -> 普通连字符(历史文件里 Lu‑177 混用)
    "–": "-", "—": "-",
    "　": " ",
    " ": " ",
})
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def norm_text(s: str) -> str:
    """归一化用于匹配: 连字符/空白统一、小写、去标点空格。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_TRANS).lower()
    return _PUNCT_RE.sub("", s)


def norm_compound(s: str) -> str:
    """化合物名归一化(比文章名宽松些, 只统一连字符和空白)。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_TRANS)
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------- 历史文件读取 ----------------

def load_history(path: Path) -> tuple[list[list], list[list]]:
    """返回 (Sheet1 行, Sheet2 行), 均含表头。"""
    wb = load_workbook(path, read_only=True)
    try:
        s1 = [list(r) for r in wb["Sheet1"].iter_rows(values_only=True)]
        s2 = [list(r) for r in wb["Sheet2"].iter_rows(values_only=True)]
    finally:
        wb.close()
    return s1, s2


def find_latest_history(hist_dir: Path) -> Path | None:
    """histories/ 下最新的 base-data-*.xlsx (按文件名日期, 其次修改时间)。"""
    files = list(hist_dir.glob("base-data-*.xlsx"))
    if not files:
        files = list(hist_dir.glob("*.xlsx"))
    if not files:
        return None

    def key(p: Path):
        m = re.search(r"(\d{8})", p.name)
        return (m.group(1) if m else "", p.stat().st_mtime)
    return max(files, key=key)


def majority_operator(sheet2_rows: list[list]) -> str:
    """历史里出现最多的「人员」作为默认值。"""
    from collections import Counter
    c = Counter(str(r[6]).strip() for r in sheet2_rows[1:]
                if len(r) > 6 and str(r[6] or "").strip())
    return c.most_common(1)[0][0] if c else ""


# ---------------- 万方导出文件读取 ----------------

def load_wanfang_export(path: Path) -> list[dict]:
    """读万方导出的 xlsx。

    结构: 按文献类型分段, 每段 = 类型标题行(如「期刊」) + 表头行(序号/作者/题名/…) +
    数据行; 表头会随类型分段重复出现, 需逐段识别跳过。
    """
    wb = load_workbook(path, read_only=True)
    items = []
    try:
        for ws in wb.worksheets:
            idx: dict = {}
            for row in ws.iter_rows(values_only=True):
                r = list(row) + [None] * 12
                cells = [str(c).strip() for c in r if c is not None and str(c).strip()]
                # 表头行标志: 同时出现文本「序号」和「题名」(各类型分段的表头列不同)
                if "序号" in cells and "题名" in cells:
                    idx = {str(c).strip(): i for i, c in enumerate(r)
                           if c is not None and str(c).strip()}
                    continue
                if not idx:
                    continue
                def cell(name):
                    i = idx.get(name)
                    v = r[i] if i is not None and i < len(r) else None
                    return str(v).strip() if v is not None else ""
                title = cell("题名")
                # 作者列各类型不同: 作者 / 申请·专利权人 / 完成人
                author = cell("作者") or cell("申请/专利权人") or cell("完成人")
                # 跳过类型标题行(只有「期刊」这类单词)
                if not title or (not author and len(cells) <= 1):
                    continue
                items.append({
                    "作者": author, "题名": title, "刊名": cell("刊名") or cell("母体文献"),
                    "年": cell("年") or cell("学位年度") or cell("年份") or cell("成果公布年份"),
                    "卷": cell("卷"), "期": cell("期"),
                    "页码": cell("页码"), "DOI": cell("DOI"),
                })
    finally:
        wb.close()
    return items


def build_citation(item: dict) -> str:
    """按历史文件的 GB/T 7714 风格拼「完整文献名称」(缺省字段自动跳过)。"""
    parts = []
    if item.get("作者"):
        parts.append(item["作者"].rstrip(",.，。") + ".")
    title = item.get("题名", "")
    parts.append(f"{title}[J]." if title else "")
    tail = ""
    if item.get("刊名"):
        tail = item["刊名"]
        if item.get("年"):
            tail += f", {item['年']}"
    elif item.get("年"):
        tail = item["年"]
    if item.get("卷"):
        tail += f", {item['卷']}"
        if item.get("期"):
            tail += f"({item['期']})"
    if item.get("页码"):
        tail += f": {item['页码']}"
    if tail:
        parts.append(tail + ".")
    if item.get("DOI"):
        parts.append(f"DOI:{item['DOI']}")
    return " ".join(p for p in parts if p)


# ---------------- 匹配 ----------------

class MatchResult:
    """一条新文献的匹配结果。"""

    def __init__(self, item: dict, top5: list[tuple[str, float, str]]):
        self.item = item
        self.top5 = top5  # [(历史文章名, 分数, 历史日期)]
        self.best = top5[0][1] if top5 else 0.0
        self.verdict = ""      # 重复 / 待确认 / 新增
        self.manual = False    # 用户在界面手动判定过则为 True, 重新分类时不覆盖

    def to_dict(self) -> dict:
        return {"item": self.item, "top5": self.top5, "best": self.best,
                "verdict": self.verdict, "manual": self.manual}

    @staticmethod
    def from_dict(d: dict) -> "MatchResult":
        mr = MatchResult(d["item"], [tuple(t) for t in d["top5"]])
        mr.verdict = d.get("verdict", "")
        mr.manual = bool(d.get("manual", False))
        return mr


def classify(results: list[MatchResult], hi: float, lo: float) -> None:
    """按阈值给未手动确认的条目重新定级(不重新计算匹配)。"""
    for r in results:
        if r.manual:
            continue
        if r.best >= hi:
            r.verdict = "重复"
        elif r.best >= lo:
            r.verdict = "待确认"
        else:
            r.verdict = "新增"


def match_items(new_items: list[dict], sheet2_rows: list[list], compound: str,
                db: str = "万方", hi: float = 90.0, lo: float = 75.0,
                logger: logging.Logger | None = None) -> list[MatchResult]:
    """把新检索条目与历史 Sheet2 比对。范围: 同数据库 + 同化合物。"""
    db_n = db.lower()
    cp_n = norm_compound(compound)
    hist = []  # (norm_title, raw_title, date_str)
    for r in sheet2_rows[1:]:
        if len(r) < 5:
            continue
        if str(r[1] or "").strip().lower() != db_n:
            continue
        if cp_n and norm_compound(str(r[2] or "")) != cp_n:
            continue
        title = str(r[4] or "").strip()
        if title:
            hist.append((norm_text(title), title, str(r[0] or "")[:10]))
    if logger:
        logger.info("历史匹配范围: 数据库=%s 化合物=%s -> %d 条", db, compound, len(hist))
    if not hist and logger:
        logger.warning("历史文件里没有该 化合物+数据库 的记录, 全部按新增处理")

    choices = [h[0] for h in hist]
    results: list[MatchResult] = []
    for item in new_items:
        q = norm_text(item["题名"])
        top5: list[tuple[str, float, str]] = []
        if q and choices:
            for _choice, score, _i in process.extract(
                    q, choices, scorer=fuzz.ratio, limit=5):
                h = hist[_i]
                top5.append((h[1], round(float(score), 1), h[2]))
        mr = MatchResult(item, top5)
        results.append(mr)
    classify(results, hi, lo)
    return results


# ---------------- 匹配结果缓存(阈值调整后免重算) ----------------

def _cache_meta(new_path: Path, hist_path: Path, compound: str) -> dict:
    return {"new": str(new_path), "new_mtime": new_path.stat().st_mtime,
            "hist": str(hist_path), "hist_mtime": hist_path.stat().st_mtime,
            "compound": compound}


def save_match_cache(cache_path: Path, new_path: Path, hist_path: Path,
                     compound: str, results: list[MatchResult]) -> None:
    import json
    data = {"meta": _cache_meta(new_path, hist_path, compound),
            "results": [r.to_dict() for r in results]}
    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_match_cache(cache_path: Path, new_path: Path, hist_path: Path,
                     compound: str) -> list[MatchResult] | None:
    """命中缓存返回结果列表, 否则 None(源文件或化合物变了就不算数)。"""
    import json
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("meta") != _cache_meta(new_path, hist_path, compound):
            return None
        return [MatchResult.from_dict(d) for d in data.get("results", [])]
    except Exception:  # noqa: BLE001
        return None


# ---------------- 写入新历史文件 ----------------

def write_new_history(src_path: Path, hist_dir: Path, compound: str,
                      results: list[MatchResult], total_new: int,
                      operator: str, logger: logging.Logger,
                      result_label: str = "新增") -> Path:
    """把判定为「新增」的条目追加进历史文件, 另存新日期文件(不覆盖原文件)。

    返回新文件路径。
    """
    today = datetime.now()
    added = [r for r in results if r.verdict == "新增"]
    dup = sum(1 for r in results if r.verdict == "重复")
    pending = sum(1 for r in results if r.verdict == "待确认")

    wb = load_workbook(src_path)  # 保留原格式
    ws2 = wb["Sheet2"]
    for r in added:
        citation = build_citation(r.item)
        ws2.append([today, "万方", compound, citation, r.item["题名"],
                    result_label, operator, ""])
    ws1 = wb["Sheet1"]
    # 列: 日期|产品|pubmed检|pubmed重|embase检|embase重|知网检|知网重|万方检|万方重|检索时间段
    ws1.append([today, compound,
                "0", "0", "0", "0", "0", "0",
                str(total_new), str(len(added)),
                f"万方: 截至 {today.strftime('%Y%m%d')}"])
    hist_dir.mkdir(parents=True, exist_ok=True)
    out = hist_dir / f"base-data-{today.strftime('%Y%m%d')}.xlsx"
    if out.resolve() == src_path.resolve():
        out = hist_dir / f"base-data-{today.strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(out)
    wb.close()
    logger.info("新历史检索文件: %s", out)
    logger.info("本次: 新检索 %d 条, 判定重复 %d, 待确认 %d, 新增写入 %d 条",
                total_new, dup, pending, len(added))
    return out
