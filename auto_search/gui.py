"""简单图形界面(tkinter)：exe 双击后默认打开。

- 字段预填 config.yaml 的值（config.yaml 视为默认配置）；
- 界面上修改只对本次运行生效，点“保存为默认配置”才写回 config.yaml；
- 界面只编辑单个任务（config.yaml 中 tasks[0] 的各字段）；多任务批量运行请用 --cli。
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import yaml

from .config import CONFIG_TEMPLATE, ConfigError, parse_raw_config
from .presets import DATABASE_LABELS, DRUG_PRESETS, preset_query
from .runner import run
from .sites import SITES
from .utils import register_log_handler

_DATE_TYPE_LABELS = {"records_added": "入库日期 (Records added)", "publication_years": "发表年份 (Publication years)"}
_DATE_TYPE_VALUES = {v: k for k, v in _DATE_TYPE_LABELS.items()}

# 各数据库流程能力提示（显示在数据库下拉框旁）
_DB_FLOW_HINTS = {
    "embase": "流程: 检索+日期过滤+截图+逐页打印PDF+逐页导出CSV",
    "pubmed": "流程: 检索+日期筛选+打印PDF+全量CSV导出(无截图/字段勾选)",
    "wanfang": "流程: 登录+专业检索+每页50条+打印第1页PDF+分批导出XLS(500条/批)",
    "cnki": "暂未实现自动流程，仅预填检索式",
}

# 数据库 -> 站点(决定登录方式): embase/pubmed 走 chaoslib WebVPN，万方直连
_DB_SITE = {"embase": "chaoslib", "pubmed": "chaoslib", "wanfang": "wanfang", "cnki": "cnki"}


def build_raw_config(base_raw: dict, v: dict) -> dict:
    """把界面值(v)合并进 config.yaml 解析出的 dict(base_raw)，只改界面覆盖的部分。"""
    raw = dict(base_raw or {})
    acc = dict(raw.get("account") or {})
    acc.update(username=v["username"], password=v["password"])
    raw["account"] = acc
    accounts = dict(raw.get("accounts") or {})
    wf_acc = dict(accounts.get("wanfang") or {})
    wf_acc.update(username=v["wf_username"], password=v["wf_password"],
                  cookies_file=v["wf_cookies_file"])
    accounts["wanfang"] = wf_acc
    raw["accounts"] = accounts
    br = dict(raw.get("browser") or {})
    br.update(headless=v["headless"], channel=v["channel"], slow_mo=v["slow_mo"])
    raw["browser"] = br
    raw["output_dir"] = v["output_dir"]

    tasks = list(raw.get("tasks") or [{}])
    t = dict(tasks[0] if tasks else {})
    # 各子段做合并而非替换，保留界面上没有的高级配置项(如 print_pdf.header_template)
    df = dict(t.get("date_filter") or {})
    df.update(enabled=v["df_enabled"], type=v["df_type"], start=v["df_start"], end=v["df_end"])
    pp = dict(t.get("print_pdf") or {})
    pp.update(enabled=v["pdf_enabled"], header_footer=v["pdf_header_footer"],
              paper_format=v["pdf_format"], scale=v["pdf_scale"])
    ss = dict(t.get("screenshot") or {})
    ss.update(enabled=v["screenshot_enabled"])
    ex = dict(t.get("export_csv") or {})
    ex.update(enabled=v["csv_enabled"], fields_by=v["csv_fields_by"],
              fields=[f for f in v["csv_fields"] if f])
    t.update(
        name=t.get("name") or "embase_task",
        site=_DB_SITE.get(v["database"], t.get("site") or "chaoslib"),
        database=v["database"],
        compound=v["compound"],
        query=v["query"],
        date_filter=df,
        per_page=v["per_page"],
        max_pages=v["max_pages"],
        screenshot=ss,
        print_pdf=pp,
        export_csv=ex,
    )
    tasks[0] = t
    raw["tasks"] = tasks[:1]  # GUI 只运行单个任务
    return raw


class _QueueHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(self.format(record))


class App:
    def __init__(self, root: tk.Tk, config_path: Path):
        self.root = root
        self.config_path = config_path
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        register_log_handler(_QueueHandler(self.log_q))

        root.title("auto_search - 医学数据库自动检索")
        root.geometry("860x720")

        # 读默认配置
        self.base_raw: dict = {}
        if config_path.exists():
            try:
                self.base_raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            except Exception as e:  # noqa: BLE001
                messagebox.showwarning("配置解析失败", f"{config_path} 解析失败，将使用内置默认值。\n{e}")
        else:
            config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
            self.base_raw = yaml.safe_load(CONFIG_TEMPLATE) or {}

        task0 = (self.base_raw.get("tasks") or [{}])[0] or {}
        df0 = task0.get("date_filter") or {}
        pp0 = task0.get("print_pdf") or {}
        ex0 = task0.get("export_csv") or {}
        ss0 = task0.get("screenshot") or {}
        acc0 = self.base_raw.get("account") or {}
        br0 = self.base_raw.get("browser") or {}

        main = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)

        row = 0

        def label(text, r, c=0):
            ttk.Label(main, text=text).grid(row=r, column=c, sticky="w", pady=2)

        # ---- 账号 ----
        label("chaoslib 账号", row); self.v_user = ttk.Entry(main)
        self.v_user.grid(row=row, column=1, sticky="ew", pady=2); row += 1
        label("chaoslib 密码", row); self.v_pass = ttk.Entry(main, show="*")
        self.v_pass.grid(row=row, column=1, sticky="ew", pady=2); row += 1
        label("万方账号", row); self.v_wf_user = ttk.Entry(main)
        self.v_wf_user.grid(row=row, column=1, sticky="ew", pady=2); row += 1
        label("万方密码", row); self.v_wf_pass = ttk.Entry(main, show="*")
        self.v_wf_pass.grid(row=row, column=1, sticky="ew", pady=2); row += 1

        # ---- 万方 cookies(可选, 优先于账号密码) ----
        label("万方cookies文件", row)
        wf_ck_frame = ttk.Frame(main)
        self.v_wf_cookies = ttk.Entry(wf_ck_frame)
        self.v_wf_cookies.pack(side="left", fill="x", expand=True)
        ttk.Button(wf_ck_frame, text="浏览…", command=self._pick_wf_cookies).pack(side="left", padx=4)
        ttk.Label(wf_ck_frame, text="可选: Cookie-Editor 导出的 JSON, 填了就免账号密码(绕开滑块)",
                  foreground="#888").pack(side="left", padx=6)
        wf_ck_frame.grid(row=row, column=1, sticky="ew", pady=2); row += 1

        # ---- 药物 ----
        label("药物", row)
        drug_frame = ttk.Frame(main)
        self.v_drug = ttk.Combobox(drug_frame, values=list(DRUG_PRESETS),
                                   width=28, state="readonly")
        self.v_drug.pack(side="left")
        self.v_drug.bind("<<ComboboxSelected>>", lambda _e: self._fill_preset_query())
        ttk.Label(drug_frame, text="选好药物和数据库后自动填入对应检索式(可再手动修改)",
                  foreground="#888").pack(side="left", padx=8)
        drug_frame.grid(row=row, column=1, sticky="w", pady=2); row += 1

        # ---- 数据库 ----
        label("目标数据库", row)
        db_frame = ttk.Frame(main)
        self.v_database = ttk.Combobox(db_frame, values=list(DATABASE_LABELS.values()),
                                       width=10, state="readonly")
        self.v_database.pack(side="left")
        self.v_database.set(DATABASE_LABELS.get(str(task0.get("database") or "embase"),
                                                DATABASE_LABELS["embase"]))
        self.v_database.bind("<<ComboboxSelected>>", lambda _e: self._on_database_changed())
        self.v_db_hint = ttk.Label(db_frame, text="", foreground="#888")
        self.v_db_hint.pack(side="left", padx=8)
        db_frame.grid(row=row, column=1, sticky="w", pady=2); row += 1

        # ---- 检索式 ----
        label("检索式", row)
        self.v_query = tk.Text(main, height=3, wrap="word")
        self.v_query.grid(row=row, column=1, sticky="ew", pady=2); row += 1

        # ---- 日期过滤 ----
        df_frame = ttk.Frame(main)
        self.v_df_enabled = tk.BooleanVar(value=bool(df0.get("enabled", True)))
        ttk.Checkbutton(df_frame, text="启用日期过滤", variable=self.v_df_enabled).pack(side="left")
        ttk.Label(df_frame, text="类型").pack(side="left", padx=(12, 2))
        self.v_df_type = ttk.Combobox(df_frame, values=list(_DATE_TYPE_LABELS.values()),
                                      state="readonly", width=26)
        self.v_df_type.pack(side="left")
        ttk.Label(df_frame, text="起").pack(side="left", padx=(12, 2))
        self.v_df_start = ttk.Entry(df_frame, width=12)
        self.v_df_start.pack(side="left")
        ttk.Label(df_frame, text="止").pack(side="left", padx=(6, 2))
        self.v_df_end = ttk.Entry(df_frame, width=12)
        self.v_df_end.pack(side="left")
        label("日期范围", row); df_frame.grid(row=row, column=1, sticky="w", pady=2); row += 1
        self.v_df_type.set(_DATE_TYPE_LABELS.get(df0.get("type", "records_added"),
                                                 _DATE_TYPE_LABELS["records_added"]))
        self.v_df_start.insert(0, str(df0.get("start", "")))
        self.v_df_end.insert(0, str(df0.get("end", "")))

        # ---- 页数 ----
        pg_frame = ttk.Frame(main)
        ttk.Label(pg_frame, text="每页条数").pack(side="left")
        self.v_per_page = ttk.Combobox(pg_frame, values=["25", "50", "100", "200"],
                                       width=6, state="readonly")
        self.v_per_page.pack(side="left")
        ttk.Label(pg_frame, text="最多处理页数(0=全部)").pack(side="left", padx=(12, 2))
        self.v_max_pages = ttk.Entry(pg_frame, width=6)
        self.v_max_pages.pack(side="left")
        label("分页", row); pg_frame.grid(row=row, column=1, sticky="w", pady=2); row += 1
        self.v_per_page.set(str(task0.get("per_page", 200)))
        self.v_max_pages.insert(0, str(task0.get("max_pages", 0)))

        # ---- 输出目录 ----
        out_frame = ttk.Frame(main)
        self.v_outdir = ttk.Entry(out_frame)
        self.v_outdir.pack(side="left", fill="x", expand=True)
        ttk.Button(out_frame, text="浏览…", command=self._pick_outdir).pack(side="left", padx=4)
        label("输出目录", row); out_frame.grid(row=row, column=1, sticky="ew", pady=2); row += 1
        self.v_outdir.insert(0, str(self.base_raw.get("output_dir") or "./output"))

        # ---- 选项 ----
        opt_frame = ttk.Frame(main)
        self.v_headless = tk.BooleanVar(value=bool(br0.get("headless", True)))
        ttk.Checkbutton(opt_frame, text="无头运行(不弹出浏览器窗口)", variable=self.v_headless).pack(side="left")
        ttk.Label(opt_frame, text="浏览器").pack(side="left", padx=(12, 2))
        self.v_channel = ttk.Combobox(opt_frame, values=["msedge", "chrome", "chromium"],
                                      width=9, state="readonly")
        self.v_channel.pack(side="left")
        self.v_channel.set(str(br0.get("channel", "msedge")))
        ttk.Label(opt_frame, text="动作间隔ms").pack(side="left", padx=(12, 2))
        self.v_slowmo = ttk.Entry(opt_frame, width=6)
        self.v_slowmo.pack(side="left")
        self.v_slowmo.insert(0, str(br0.get("slow_mo", 0)))
        label("运行方式", row); opt_frame.grid(row=row, column=1, sticky="w", pady=2); row += 1

        act_frame = ttk.Frame(main)
        self.v_shot = tk.BooleanVar(value=bool(ss0.get("enabled", True)))
        ttk.Checkbutton(act_frame, text="结果页整页截图", variable=self.v_shot).pack(side="left")
        self.v_pdf = tk.BooleanVar(value=bool(pp0.get("enabled", True)))
        ttk.Checkbutton(act_frame, text="逐页打印 PDF", variable=self.v_pdf).pack(side="left", padx=(10, 0))
        self.v_pdf_hf = tk.BooleanVar(value=bool(pp0.get("header_footer", True)))
        ttk.Checkbutton(act_frame, text="PDF 页眉页脚", variable=self.v_pdf_hf).pack(side="left", padx=(10, 0))
        self.v_csv = tk.BooleanVar(value=bool(ex0.get("enabled", True)))
        self.cb_csv = ttk.Checkbutton(act_frame, text="逐页导出 CSV", variable=self.v_csv)
        self.cb_csv.pack(side="left", padx=(10, 0))
        ttk.Label(act_frame, text="Fields by").pack(side="left", padx=(12, 2))
        self.v_fields_by = ttk.Combobox(act_frame, values=["column", "row"], width=7, state="readonly")
        self.v_fields_by.pack(side="left")
        self.v_fields_by.set(str(ex0.get("fields_by", "column")))
        label("输出动作", row); act_frame.grid(row=row, column=1, sticky="w", pady=2); row += 1

        # ---- 导出字段 ----
        label("CSV 导出字段\n(每行一个)", row)
        self.v_fields = tk.Text(main, height=5, wrap="none")
        self.v_fields.grid(row=row, column=1, sticky="ew", pady=2); row += 1

        # ---- 按钮 ----
        btn_frame = ttk.Frame(main)
        self.btn_run = ttk.Button(btn_frame, text="开始运行", command=self._start)
        self.btn_run.pack(side="left")
        ttk.Button(btn_frame, text="保存为默认配置", command=self._save_defaults).pack(side="left", padx=8)
        ttk.Button(btn_frame, text="去重…", command=self._open_dedup).pack(side="left", padx=4)
        self.v_status = ttk.Label(btn_frame, text="就绪")
        self.v_status.pack(side="left", padx=12)
        btn_frame.grid(row=row, column=0, columnspan=2, sticky="w", pady=6); row += 1

        # ---- 日志 ----
        self.log_box = scrolledtext.ScrolledText(main, height=14, state="disabled", wrap="word")
        self.log_box.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=4)
        main.rowconfigure(row, weight=1)

        # 预填账号密码和检索式、字段
        wf_acc0 = (self.base_raw.get("accounts") or {}).get("wanfang") or {}
        self.v_user.insert(0, str(acc0.get("username") or ""))
        self.v_pass.insert(0, str(acc0.get("password") or ""))
        self.v_wf_user.insert(0, str(wf_acc0.get("username") or ""))
        self.v_wf_pass.insert(0, str(wf_acc0.get("password") or ""))
        self.v_wf_cookies.insert(0, str(wf_acc0.get("cookies_file") or ""))
        self.v_query.insert("1.0", str(task0.get("query") or ""))
        self.v_fields.insert("1.0", "\n".join(str(f) for f in (ex0.get("fields") or [])))

        self._update_db_hint()
        self.root.after(150, self._drain_logs)

    # ---------------- 界面动作 ----------------

    def _pick_outdir(self):
        d = filedialog.askdirectory()
        if d:
            self.v_outdir.delete(0, "end")
            self.v_outdir.insert(0, d)

    def _pick_wf_cookies(self):
        f = filedialog.askopenfilename(
            title="选择万方 cookies 文件", filetypes=[("JSON 文件", "*.json")])
        if f:
            self.v_wf_cookies.delete(0, "end")
            self.v_wf_cookies.insert(0, f)

    def _open_dedup(self):
        DedupDialog(self)

    def _db_key(self) -> str:
        """数据库下拉框当前显示名 -> 内部键(embase/pubmed/wanfang/cnki)。"""
        label = self.v_database.get().strip()
        for k, v in DATABASE_LABELS.items():
            if v == label:
                return k
        return label or "embase"

    def _update_db_hint(self):
        key = self._db_key()
        self.v_db_hint.config(text=_DB_FLOW_HINTS.get(key, ""))
        # 「逐页导出CSV」复选框文案随数据库切换(万方是批量引用导出XLS)
        self.cb_csv.config(
            text="分批导出 XLS（批量引用）" if key == "wanfang" else "逐页导出 CSV")

    def _on_database_changed(self):
        self._update_db_hint()
        self._fill_preset_query()

    def _fill_preset_query(self):
        """选了药物和数据库后，把对应预设检索式填入检索式框(用户可再改)。"""
        drug = self.v_drug.get().strip()
        if not drug:
            return
        query = preset_query(drug, self._db_key())
        if not query:
            return
        self.v_query.delete("1.0", "end")
        self.v_query.insert("1.0", query)

    def _collect(self) -> dict:
        return {
            "username": self.v_user.get().strip(),
            "password": self.v_pass.get().strip(),
            "wf_username": self.v_wf_user.get().strip(),
            "wf_password": self.v_wf_pass.get().strip(),
            "wf_cookies_file": self.v_wf_cookies.get().strip(),
            "database": self._db_key(),
            "compound": self.v_drug.get().strip(),
            "query": self.v_query.get("1.0", "end").strip(),
            "df_enabled": self.v_df_enabled.get(),
            "df_type": _DATE_TYPE_VALUES.get(self.v_df_type.get(), "records_added"),
            "df_start": self.v_df_start.get().strip(),
            "df_end": self.v_df_end.get().strip(),
            "per_page": int(self.v_per_page.get() or 200),
            "max_pages": int(self.v_max_pages.get() or 0),
            "output_dir": self.v_outdir.get().strip() or "./output",
            "headless": self.v_headless.get(),
            "channel": self.v_channel.get(),
            "slow_mo": int(self.v_slowmo.get() or 0),
            "pdf_enabled": self.v_pdf.get(),
            "pdf_header_footer": self.v_pdf_hf.get(),
            "pdf_format": str((self.base_raw.get("tasks") or [{}])[0].get("print_pdf", {}).get("paper_format", "A4")),
            "pdf_scale": float((self.base_raw.get("tasks") or [{}])[0].get("print_pdf", {}).get("scale", 1.0)),
            "screenshot_enabled": self.v_shot.get(),
            "csv_enabled": self.v_csv.get(),
            "csv_fields_by": self.v_fields_by.get(),
            "csv_fields": [l.strip() for l in self.v_fields.get("1.0", "end").splitlines() if l.strip()],
        }

    def _build_cfg(self):
        raw = build_raw_config(self.base_raw, self._collect())
        return parse_raw_config(raw, self.config_path.parent, prompt=False)

    def _start(self):
        try:
            cfg = self._build_cfg()
        except (ConfigError, ValueError) as e:
            messagebox.showerror("配置有误", str(e))
            return
        site = SITES.get(cfg.tasks[0].site)
        if site is None or cfg.tasks[0].database not in site.flows:
            supported = ", ".join(site.flows) if site else "-"
            messagebox.showerror(
                "暂不支持",
                f"数据库 {cfg.tasks[0].database} 的自动流程暂未实现（当前支持: {supported}）。\n"
                "检索式已预填到检索式框，可手动复制到对应网站检索。")
            return
        self.btn_run.state(["disabled"])
        self.v_status.config(text="运行中…")
        self._log("===== 开始运行 =====")

        def work():
            try:
                code = asyncio.run(run(cfg))
                self.log_q.put(f"__DONE__:{code}")
            except Exception as e:  # noqa: BLE001
                self.log_q.put(f"__DONE__:1:{e}")

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _save_defaults(self):
        try:
            raw = build_raw_config(self.base_raw, self._collect())
            header = ("# auto_search 默认配置（由界面“保存为默认配置”生成）\n"
                      "# 详细字段说明见 config.example.yaml / README.md\n")
            self.config_path.write_text(
                header + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
            self.base_raw = raw
            messagebox.showinfo("已保存", f"已写入 {self.config_path}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存失败", str(e))

    # ---------------- 日志显示 ----------------

    def _log(self, text: str):
        self.log_box.config(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _drain_logs(self):
        try:
            while True:
                line = self.log_q.get_nowait()
                if line.startswith("__DONE__:"):
                    parts = line.split(":", 2)
                    code = parts[1]
                    detail = f"：{parts[2]}" if len(parts) > 2 else ""
                    self._log(f"===== 运行结束 (退出码 {code}){detail} =====")
                    self.v_status.config(text="完成" if code == "0" else "失败")
                    self.btn_run.state(["!disabled"])
                    if code == "0":
                        messagebox.showinfo("完成", "任务已完成，请到输出目录查看结果。")
                    else:
                        messagebox.showerror("失败", f"任务失败{detail}，详见日志。")
                else:
                    self._log(line)
        except queue.Empty:
            pass
        self.root.after(150, self._drain_logs)


def run_gui(config_path: Path) -> int:
    root = tk.Tk()
    App(root, config_path)
    root.mainloop()
    return 0


class DedupDialog(tk.Toplevel):
    """去重窗口: 新检索结果 vs 历史检索文件。

    流程: 选历史文件(默认 histories/ 下最新) + 新检索文件(默认 output/ 下最新导出)
    + 化合物 -> 开始匹配(RapidFuzz, 结果缓存到 新文件同目录 dedup_match_cache.json)
    -> 列表显示每条判定(重复/待确认/新增), 选中行看 top-5 匹配, 可手动改判
    -> 调阈值后「重新分类」不重新计算 -> 「写入新历史文件」另存到 histories/。
    """

    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.title("检索去重(万方)")
        self.geometry("980x640")
        self.results: list = []          # list[dedup.MatchResult]
        self.hist_path: Path | None = None
        self.sheet2_rows: list = []

        from . import dedup  # 延迟导入, 避免主界面启动时就加载 rapidfuzz
        self.dedup = dedup

        base = app.config_path.parent
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        # 历史检索文件
        ttk.Label(top, text="历史检索文件").grid(row=0, column=0, sticky="w")
        self.v_hist = ttk.Entry(top, width=70)
        self.v_hist.grid(row=0, column=1, columnspan=4, sticky="ew", padx=4)
        ttk.Button(top, text="浏览…", command=self._pick_hist).grid(row=0, column=5)
        hist0 = dedup.find_latest_history(base / "histories")
        if hist0:
            self.v_hist.insert(0, str(hist0))

        # 新检索文件
        ttk.Label(top, text="新检索文件").grid(row=1, column=0, sticky="w")
        self.v_new = ttk.Entry(top, width=70)
        self.v_new.grid(row=1, column=1, columnspan=4, sticky="ew", padx=4)
        ttk.Button(top, text="浏览…", command=self._pick_new).grid(row=1, column=5)
        new0 = self._find_latest_export(base)
        if new0:
            self.v_new.insert(0, str(new0))

        # 化合物 + 人员 + 阈值
        ttk.Label(top, text="化合物(成分)").grid(row=2, column=0, sticky="w")
        self.v_compound = ttk.Combobox(top, values=list(DRUG_PRESETS), state="readonly", width=28)
        self.v_compound.grid(row=2, column=1, sticky="w", padx=4)
        if app.v_drug.get().strip():
            self.v_compound.set(app.v_drug.get().strip())
        ttk.Label(top, text="人员").grid(row=2, column=2, sticky="e")
        self.v_operator = ttk.Entry(top, width=10)
        self.v_operator.grid(row=2, column=3, sticky="w")
        ttk.Label(top, text="阈值 重复≥").grid(row=2, column=4, sticky="e")
        th = ttk.Frame(top)
        self.v_hi = ttk.Entry(th, width=5)
        self.v_hi.insert(0, "90")
        self.v_hi.pack(side="left")
        ttk.Label(th, text=" 待确认≥").pack(side="left")
        self.v_lo = ttk.Entry(th, width=5)
        self.v_lo.insert(0, "75")
        self.v_lo.pack(side="left")
        th.grid(row=2, column=5, sticky="w", padx=4)

        act = ttk.Frame(self, padding=(8, 0))
        act.pack(fill="x")
        self.btn_match = ttk.Button(act, text="开始匹配", command=self._start_match)
        self.btn_match.pack(side="left")
        ttk.Button(act, text="重新分类(不重算)", command=self._reclassify).pack(side="left", padx=8)
        self.btn_write = ttk.Button(act, text="写入新历史文件", command=self._write, state="disabled")
        self.btn_write.pack(side="left", padx=8)
        self.v_summary = ttk.Label(act, text="")
        self.v_summary.pack(side="left", padx=12)

        # 结果列表 + top5 详情
        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        cols = ("idx", "title", "score", "verdict")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", height=14)
        self.tree.heading("idx", text="#")
        self.tree.heading("title", text="新检索文章名")
        self.tree.heading("score", text="最佳分")
        self.tree.heading("verdict", text="判定")
        self.tree.column("idx", width=50)
        self.tree.column("title", width=520)
        self.tree.column("score", width=70)
        self.tree.column("verdict", width=80)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self._show_top5)
        self.tree.tag_configure("重复", foreground="#999")
        self.tree.tag_configure("新增", foreground="#0a0")
        self.tree.tag_configure("待确认", foreground="#e60")

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", padx=(8, 0))
        ttk.Label(right, text="Top-5 历史匹配(选中行)").pack(anchor="w")
        self.top5_box = tk.Text(right, width=46, height=12, wrap="word", state="disabled")
        self.top5_box.pack(fill="both", expand=True)
        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="判为重复", command=lambda: self._set_verdict("重复")).pack(side="left")
        ttk.Button(btns, text="判为新增", command=lambda: self._set_verdict("新增")).pack(side="left", padx=4)
        ttk.Button(btns, text="判为待确认", command=lambda: self._set_verdict("待确认")).pack(side="left")

    # ---------------- 文件选择 ----------------

    def _pick_hist(self):
        f = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")])
        if f:
            self.v_hist.delete(0, "end")
            self.v_hist.insert(0, f)

    def _pick_new(self):
        f = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")])
        if f:
            self.v_new.delete(0, "end")
            self.v_new.insert(0, f)

    @staticmethod
    def _find_latest_export(base: Path) -> Path | None:
        out = base / "output"
        if not out.exists():
            return None
        files = list(out.rglob("export_all*.xlsx"))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    # ---------------- 匹配 ----------------

    def _log(self, text: str):
        self.app.log_q.put(text)

    def _start_match(self):
        hist = Path(self.v_hist.get().strip())
        new = Path(self.v_new.get().strip())
        compound = self.v_compound.get().strip()
        if not hist.exists() or not new.exists():
            messagebox.showerror("文件缺失", "请选好历史检索文件和新检索文件", parent=self)
            return
        if not compound:
            messagebox.showerror("缺化合物", "请选择化合物(成分)", parent=self)
            return
        self.btn_match.state(["disabled"])
        self._log(f"===== 去重匹配: {new.name} vs {hist.name} (化合物: {compound}) =====")
        self._done = False
        self._results = []
        self._hist_path = hist
        log = _DialogLogger(self._log)

        def work():
            try:
                cache = new.parent / "dedup_match_cache.json"
                results = self.dedup.load_match_cache(cache, new, hist, compound)
                if results is not None:
                    self._log(f"使用缓存的匹配结果({len(results)} 条, 未重复计算)")
                else:
                    _s1, s2 = self.dedup.load_history(hist)
                    items = self.dedup.load_wanfang_export(new)
                    self._log(f"新检索 {len(items)} 条, 历史 Sheet2 {len(s2) - 1} 条, 开始匹配…")
                    results = self.dedup.match_items(
                        items, s2, compound,
                        hi=float(self.v_hi.get() or 90), lo=float(self.v_lo.get() or 75),
                        logger=log)
                    self.dedup.save_match_cache(cache, new, hist, compound, results)
                    self._log(f"匹配完成, 结果已缓存到 {cache}")
                self._results = results
            except Exception as e:  # noqa: BLE001
                self._error = str(e)
            finally:
                self._done = True

        self._error = None
        threading.Thread(target=work, daemon=True).start()
        self.after(200, self._poll_match)

    def _poll_match(self):
        if not self._done:
            self.after(200, self._poll_match)
            return
        self.btn_match.state(["!disabled"])
        if self._error:
            self._log(f"匹配失败: {self._error}")
            messagebox.showerror("匹配失败", self._error, parent=self)
            return
        self._fill_tree()
        self.btn_write.state(["!disabled"])
        self._log("匹配结果显示在下方列表；选中任意行看 Top-5 匹配，可改判或调阈值后「重新分类」")

    # 线程安全日志走主窗口; 匹配完成信号轮询
    def _fill_tree(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for i, r in enumerate(self._results, 1):
            self.tree.insert("", "end", iid=str(i - 1), values=(
                i, r.item["题名"][:60], f"{r.best:.1f}", r.verdict), tags=(r.verdict,))
        self._update_summary()

    def _update_summary(self):
        from collections import Counter
        c = Counter(r.verdict for r in self._results)
        self.v_summary.config(
            text=f"共 {len(self._results)} 条 | 重复 {c.get('重复', 0)} | "
                 f"待确认 {c.get('待确认', 0)} | 新增 {c.get('新增', 0)}")

    def _show_top5(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        r = self._results[int(sel[0])]
        lines = [f"新: {r.item['题名']}", f"作者: {r.item.get('作者','')} | {r.item.get('刊名','')} {r.item.get('年','')}", ""]
        for j, (t, s, d) in enumerate(r.top5, 1):
            lines.append(f"{j}. [{s:.1f}分] ({d}) {t}")
        if not r.top5:
            lines.append("(历史范围内无匹配)")
        self.top5_box.config(state="normal")
        self.top5_box.delete("1.0", "end")
        self.top5_box.insert("1.0", "\n".join(lines))
        self.top5_box.config(state="disabled")

    def _set_verdict(self, v: str):
        for iid in self.tree.selection():
            r = self._results[int(iid)]
            r.verdict = v
            r.manual = True
            self.tree.item(iid, values=(int(iid) + 1, r.item["题名"][:60], f"{r.best:.1f}", v),
                           tags=(v,))
        self._update_summary()

    def _reclassify(self):
        if not self._results:
            return
        hi = float(self.v_hi.get() or 90)
        lo = float(self.v_lo.get() or 75)
        self.dedup.classify(self._results, hi, lo)
        self._fill_tree()
        self._log(f"已按阈值 重复≥{hi} / 待确认≥{lo} 重新分类(手动判定的行保持不变, 未重新计算匹配)")

    # ---------------- 写入 ----------------

    def _write(self):
        if not self._results or self._hist_path is None:
            return
        hist_dir = self.app.config_path.parent / "histories"
        operator = self.v_operator.get().strip() or "张瀛月"
        try:
            out = self.dedup.write_new_history(
                self._hist_path, hist_dir, self.v_compound.get().strip(),
                self._results, total_new=len(self._results),
                operator=operator, logger=_DialogLogger(self._log))
            messagebox.showinfo("已写入", f"新历史检索文件:\n{out}", parent=self)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("写入失败", str(e), parent=self)


class _DialogLogger:
    """把 dedup 模块的 logger 调用转到主窗口日志。"""

    def __init__(self, log_fn):
        self._fn = log_fn

    def info(self, msg, *a):
        self._fn(str(msg) % a if a else str(msg))

    def warning(self, msg, *a):
        self._fn("WARNING: " + (str(msg) % a if a else str(msg)))

    def error(self, msg, *a):
        self._fn("ERROR: " + (str(msg) % a if a else str(msg)))