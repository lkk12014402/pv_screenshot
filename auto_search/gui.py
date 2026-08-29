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
from .runner import run
from .utils import register_log_handler

_DATE_TYPE_LABELS = {"records_added": "入库日期 (Records added)", "publication_years": "发表年份 (Publication years)"}
_DATE_TYPE_VALUES = {v: k for k, v in _DATE_TYPE_LABELS.items()}


def build_raw_config(base_raw: dict, v: dict) -> dict:
    """把界面值(v)合并进 config.yaml 解析出的 dict(base_raw)，只改界面覆盖的部分。"""
    raw = dict(base_raw or {})
    acc = dict(raw.get("account") or {})
    acc.update(username=v["username"], password=v["password"])
    raw["account"] = acc
    br = dict(raw.get("browser") or {})
    br.update(headless=v["headless"], channel=v["channel"], slow_mo=v["slow_mo"])
    raw["browser"] = br
    raw["output_dir"] = v["output_dir"]

    tasks = list(raw.get("tasks") or [{}])
    t = dict(tasks[0] if tasks else {})
    t.update(
        name=t.get("name") or "embase_task",
        site=t.get("site") or "chaoslib",
        database=t.get("database") or "embase",
        query=v["query"],
        date_filter={
            "enabled": v["df_enabled"],
            "type": v["df_type"],
            "start": v["df_start"],
            "end": v["df_end"],
        },
        per_page=v["per_page"],
        max_pages=v["max_pages"],
        screenshot={"enabled": v["screenshot_enabled"]},
        print_pdf={
            "enabled": v["pdf_enabled"],
            "header_footer": v["pdf_header_footer"],
            "paper_format": v["pdf_format"],
            "scale": v["pdf_scale"],
        },
        export_csv={
            "enabled": v["csv_enabled"],
            "fields_by": v["csv_fields_by"],
            "fields": [f for f in v["csv_fields"] if f],
        },
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
        ttk.Checkbutton(act_frame, text="逐页导出 CSV", variable=self.v_csv).pack(side="left", padx=(10, 0))
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
        self.v_status = ttk.Label(btn_frame, text="就绪")
        self.v_status.pack(side="left", padx=12)
        btn_frame.grid(row=row, column=0, columnspan=2, sticky="w", pady=6); row += 1

        # ---- 日志 ----
        self.log_box = scrolledtext.ScrolledText(main, height=14, state="disabled", wrap="word")
        self.log_box.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=4)
        main.rowconfigure(row, weight=1)

        # 预填账号密码和检索式、字段
        self.v_user.insert(0, str(acc0.get("username") or ""))
        self.v_pass.insert(0, str(acc0.get("password") or ""))
        self.v_query.insert("1.0", str(task0.get("query") or ""))
        self.v_fields.insert("1.0", "\n".join(str(f) for f in (ex0.get("fields") or [])))

        self.root.after(150, self._drain_logs)

    # ---------------- 界面动作 ----------------

    def _pick_outdir(self):
        d = filedialog.askdirectory()
        if d:
            self.v_outdir.delete(0, "end")
            self.v_outdir.insert(0, d)

    def _collect(self) -> dict:
        return {
            "username": self.v_user.get().strip(),
            "password": self.v_pass.get().strip(),
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
            "pdf_format": str((self.base_raw.get("tasks") or [{}])[0].get("print_pdf", {}).get("paper_format", "Letter")),
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
