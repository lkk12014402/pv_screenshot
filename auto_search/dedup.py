"""检索去重模块: 新检索结果(万方导出 xlsx) 与 历史检索文件比对去重。

历史检索文件(histories/base-data-YYYYMMDD.xlsx)结构:
- Sheet1 台账: 日期 | 产品 | pubmed检索数量 | pubmed去重后数量 | embase检索数量 |
  embase去重后数量 | 知网检索数量 | 知网去重后数量 | 万方检索数量 | 万方去重后数量 | 检索时间段
- Sheet2 明细: 日期 | 数据库 | 化合物 | 完整文献名称 | 文章名 | 结果 | 人员 | 备注

匹配方式: 文章名 RapidFuzz 模糊匹配, 范围限定在 同化合物(成分), 不限数据库
(同一文献可能同时被万方/知网/Pubmed 收录, 跨库重复也要认出来; Top-5 里带数据库来源)。
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
    "\u2010": "-",  # U+2010 连字符(NFKC 会把 U+2011 兼容分解为它, 两个都要收)
    "\u2011": "-",  # U+2011 不断字连字符(历史文件里 Lu‑177 混用)
    "\u2012": "-",  # U+2012
    "\u2013": "-",  # U+2013
    "\u2014": "-",  # U+2014
    "\u2212": "-",  # U+2212 减号
    "\u3000": " ",  # 全角空格
    "\u00a0": " ",  # NBSP
})
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def norm_text(s: str) -> str:
    """归一化用于匹配: 连字符/空白统一、小写、去标点空格。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_TRANS).lower()
    return _PUNCT_RE.sub("", s)


def norm_compound(s: str) -> str:
    """化合物名归一化: 统一连字符后, 连字符与空白一并去除( Lu‑177 == Lu 177 )。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_TRANS).lower()
    return re.sub(r"[\s\-]+", "", s)


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
            for row_i, row in enumerate(ws.iter_rows(values_only=True)):
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
                    "_sheet": ws.title, "_row": row_i,  # 源文件位置(标注写回用)
                })
    finally:
        wb.close()
    return items


def build_citation(item: dict) -> str:
    """按历史文件的 GB/T 7714 风格拼「完整文献名称」。

    知网条目直接用原始引文行(item['_raw']，本身就是 GB/T 7714 格式);
    万方条目按字段拼(缺省字段自动跳过)。
    """
    if item.get("_raw"):
        return item["_raw"]
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


# ---------------- 知网导出文件读取 ----------------

_CNKI_LINE_RE = re.compile(
    r"^(?:\[(?P<idx>\d+)\])?\s*(?P<authors>.*?)\.(?P<title>.*?)"
    r"\[(?P<dtype>[JDCMNRSP])\]\.?(?P<rest>.*)$")
_DOI_RE = re.compile(r"DOI:(?P<doi>\S+?)\.?\s*$", re.I)


def load_cnki_export(path: Path) -> list[dict]:
    """读知网导出的 GB/T 7714 引文 txt(每条一行: [N]作者.题名[J].刊名,年,卷(期):页码.DOI:x.)

    返回与万方导出相同的字段结构; 原文行存 item['_raw'](本身就是完整引文),
    item['_db'] = '知网'。
    """
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    items = []
    for line_i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line or line.startswith("<<"):
            continue
        m = _CNKI_LINE_RE.match(line)
        if not m:
            continue
        rest = m.group("rest")
        doi_m = _DOI_RE.search(rest)
        doi = doi_m.group("doi") if doi_m else ""
        # 来源与年份: [J] 后是 刊名,年,卷(期):页码; [D] 后是 学校,年
        seg = rest[:doi_m.start()] if doi_m else rest
        seg = seg.rstrip(".")
        parts = [p.strip() for p in seg.split(",") if p.strip()]
        source = parts[0] if parts else ""
        year = ""
        for p in parts[1:]:
            ym = re.match(r"^(\d{4})", p)
            if ym:
                year = ym.group(1)
                break
        title = m.group("title").strip()
        if not title:
            continue
        items.append({
            "作者": m.group("authors").strip(), "题名": title,
            "刊名": source if m.group("dtype") == "J" else "",
            "年": year, "卷": "", "期": "", "页码": "", "DOI": doi,
            "_raw": line, "_db": "知网", "_line": line_i,  # _line: 源文件行号(标注写回用)
        })
    return items


def load_new_export(path: Path) -> list[dict]:
    """按后缀分发: .txt -> 知网引文; .xlsx -> 万方导出。"""
    if path.suffix.lower() == ".txt":
        return load_cnki_export(path)
    return load_wanfang_export(path)


def infer_db_from_file(path: Path, items: list[dict] | None = None) -> str:
    """由文件后缀推断新检索的数据库名(.txt=知网, .xlsx=万方)。"""
    return "知网" if path.suffix.lower() == ".txt" else "万方"


# ---------------- 匹配 ----------------

class MatchResult:
    """一条新文献的匹配结果。top5: [(历史文章名, 分数, 历史日期, 历史数据库)]。"""

    def __init__(self, item: dict, top5: list[tuple[str, float, str, str]]):
        self.item = item
        self.top5 = top5
        self.best = top5[0][1] if top5 else 0.0
        self.verdict = ""      # 重复 / 待确认 / 新增
        self.hint = ""         # 待确认时的倾向提示: 倾向重复 / 倾向新增
        self.manual = False    # 用户在界面手动判定过则为 True, 重新分类时不覆盖

    def to_dict(self) -> dict:
        return {"item": self.item, "top5": self.top5, "best": self.best,
                "verdict": self.verdict, "manual": self.manual, "hint": self.hint}

    @staticmethod
    def from_dict(d: dict) -> "MatchResult":
        # 兼容旧缓存(3 元组无数据库列)
        top5 = [tuple(t) if len(t) >= 4 else (t[0], t[1], t[2], "") for t in d["top5"]]
        mr = MatchResult(d["item"], top5)
        mr.verdict = d.get("verdict", "")
        mr.manual = bool(d.get("manual", False))
        mr.hint = d.get("hint", "")
        return mr


def classify(results: list[MatchResult], hi: float, lo: float) -> None:
    """按阈值定级(不重新计算匹配, 不覆盖用户手动判定)。

    规则: score >= hi → 重复(不写入); 其余 → 待确认(默认不写入)。
    待确认里再按 lo 分档给提示: lo~hi 倾向重复, < lo 倾向新增。
    只有用户确认成「新增」的条目才会写入新历史文件。
    """
    for r in results:
        if r.manual:
            continue
        if r.best >= hi:
            r.verdict = "重复"
            r.hint = ""
        else:
            r.verdict = "待确认"
            r.hint = "倾向重复" if r.best >= lo else "倾向新增"


def match_items(new_items: list[dict], sheet2_rows: list[list], compound: str,
                hi: float = 90.0, lo: float = 75.0,
                logger: logging.Logger | None = None) -> list[MatchResult]:
    """把新检索条目与历史 Sheet2 比对。

    范围: 同化合物, 不限数据库(同一文献可能同时被万方/知网/Pubmed 收录,
    跨库重复也要能认出来)。top5 里带历史条目的数据库来源。
    """
    # ---- 旧逻辑备份: 只匹配同数据库(万方) ----
    # db_n = db.lower()
    # for r in sheet2_rows[1:]:
    #     if len(r) < 5: continue
    #     if str(r[1] or "").strip().lower() != db_n: continue
    #     if cp_n and norm_compound(...) != cp_n: continue
    #     hist.append((norm_title, title, date))
    # -----------------------------------------
    cp_n = norm_compound(compound)
    hist = []  # (norm_title, raw_title, date_str, db_name)
    db_counts: dict[str, int] = {}
    for r in sheet2_rows[1:]:
        if len(r) < 5:
            continue
        if cp_n and norm_compound(str(r[2] or "")) != cp_n:
            continue
        title = str(r[4] or "").strip()
        if title:
            db_name = str(r[1] or "").strip()
            hist.append((norm_text(title), title, str(r[0] or "")[:10], db_name))
            db_counts[db_name] = db_counts.get(db_name, 0) + 1
    if logger:
        logger.info("历史匹配范围: 化合物=%s(不限数据库) -> %d 条 %s",
                    compound, len(hist), db_counts)
    if not hist and logger:
        logger.warning("历史文件里没有该化合物的记录, 全部按待确认处理")

    choices = [h[0] for h in hist]
    results: list[MatchResult] = []
    for item in new_items:
        q = norm_text(item["题名"])
        top5: list[tuple[str, float, str, str]] = []
        if q and choices:
            for _choice, score, _i in process.extract(
                    q, choices, scorer=fuzz.ratio, limit=5):
                h = hist[_i]
                top5.append((h[1], round(float(score), 1), h[2], h[3]))
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
                      result_label: str = "新增", db_name: str = "万方") -> Path:
    """把判定为「新增」的条目追加进历史文件, 另存新日期文件(不覆盖原文件)。

    db_name 决定 Sheet2 的数据库列值与 Sheet1 台账行落入哪个库的列。
    返回新文件路径。
    """
    today = datetime.now()
    added = [r for r in results if r.verdict == "新增"]
    dup = sum(1 for r in results if r.verdict == "重复")
    pending = sum(1 for r in results if r.verdict == "待确认")

    # Sheet1 列: 日期|产品|pubmed检|pubmed重|embase检|embase重|知网检|知网重|万方检|万方重|检索时间段
    _DB_COLS = {"pubmed": (2, 3), "embase": (4, 5), "知网": (6, 7), "万方": (8, 9)}
    sheet1_row = [today, compound, "0", "0", "0", "0", "0", "0", "0", "0",
                  f"{db_name}: 截至 {today.strftime('%Y%m%d')}"]
    cols = _DB_COLS.get(db_name.lower(), _DB_COLS.get(db_name))
    if cols:
        sheet1_row[cols[0]] = str(total_new)
        sheet1_row[cols[1]] = str(len(added))

    wb = load_workbook(src_path)  # 保留原格式
    ws2 = wb["Sheet2"]
    for r in added:
        citation = build_citation(r.item)
        ws2.append([today, db_name, compound, citation, r.item["题名"],
                    result_label, operator, ""])
    ws1 = wb["Sheet1"]
    ws1.append(sheet1_row)
    hist_dir.mkdir(parents=True, exist_ok=True)
    out = hist_dir / f"base-data-{today.strftime('%Y%m%d')}.xlsx"
    if out.resolve() == src_path.resolve():
        out = hist_dir / f"base-data-{today.strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(out)
    wb.close()
    logger.info("新历史检索文件: %s", out)
    logger.info("本次(%s): 新检索 %d 条, 判定重复 %d, 待确认 %d, 新增写入 %d 条",
                db_name, total_new, dup, pending, len(added))
    return out


# ---------------- 标注新检索文件(生成 待处理 文件) ----------------

def _load_workbook_tolerant(path: Path):
    """万方导出的 xlsx 行高写的是字符串('22px'), openpyxl 直接读会崩;
    先剥掉 sheet XML 里的 ht 属性再加载(内容不受影响)。"""
    import zipfile
    tmp = path.with_name(path.stem + ".cleantmp" + path.suffix)
    with zipfile.ZipFile(path) as zin, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                data = re.sub(rb'\sht="[^"]*"', b"", data)
            zout.writestr(name, data)
    wb = load_workbook(tmp)
    try:
        tmp.unlink()
    except Exception:  # noqa: BLE001
        pass
    return wb


def annotate_new_file(new_path: Path, results: list[MatchResult],
                      logger: logging.Logger) -> Path:
    """把每条新文献的判定(重复/待确认/新增)写到源文件最后一列(末位),
    另存为 <原文件名>_待处理.<原格式>(万方 xlsx->xlsx, 知网 txt->txt)。

    返回标注文件路径。
    """
    out = new_path.with_name(f"{new_path.stem}_待处理{new_path.suffix}")
    if new_path.suffix.lower() == ".txt":
        _annotate_txt(new_path, out, results, logger)
    else:
        _annotate_xlsx(new_path, out, results, logger)
    logger.info("标注文件已保存: %s", out)
    return out


def _annotate_xlsx(new_path: Path, out: Path, results: list[MatchResult],
                   logger: logging.Logger) -> None:
    """xlsx: 在每条数据行的最后一列写判定(表头行写「去重判定」)。"""
    wb = _load_workbook_tolerant(new_path)
    written = 0
    # 按 (sheet, row) 定位写回
    by_sheet: dict[str, list[MatchResult]] = {}
    for r in results:
        by_sheet.setdefault(r.item.get("_sheet", ""), []).append(r)
    for ws in wb.worksheets:
        rows = by_sheet.get(ws.title)
        if not rows:
            continue
        last_col = ws.max_column + 1
        for r in rows:
            ws.cell(row=r.item["_row"] + 1, column=last_col, value=r.verdict)
            written += 1
        # 表头标注: 找该 sheet 中含「序号」「题名」的行, 在其最后一列写「去重判定」
        for row in ws.iter_rows():
            vals = [str(c.value).strip() if c.value is not None else "" for c in row]
            if "序号" in vals and "题名" in vals:
                ws.cell(row=row[0].row, column=last_col, value="去重判定")
    wb.save(out)
    wb.close()
    logger.info("xlsx 标注: %d 条写判定(列: 去重判定)", written)


def _annotate_txt(new_path: Path, out: Path, results: list[MatchResult],
                  logger: logging.Logger) -> None:
    """知网 txt: 每条引文行末追加「\\t判定」。"""
    raw = new_path.read_bytes()
    text = None
    for enc in ("utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    by_line = {r.item["_line"]: r for r in results if "_line" in r.item}
    written = 0
    for i, r in by_line.items():
        if 0 <= i < len(lines):
            lines[i] = lines[i] + "\t" + r.verdict
            written += 1
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("txt 标注: %d 条写判定(行末追加)", written)
