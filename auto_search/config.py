"""配置加载：config.yaml + 环境变量覆盖 + 缺省时交互输入。"""
from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    """配置文件内容不合法。"""


# 首次运行时自动生成的配置模板（也用于打包进 exe，避免额外数据文件）
CONFIG_TEMPLATE = """\
# ================= 账号 =================
# 也可以不写在这里，改用环境变量 AUTOSEARCH_USERNAME / AUTOSEARCH_PASSWORD，
# 或运行时加 --ask 参数手动输入
account:
  username: ""      # chaoslib 账号
  password: ""      # chaoslib 密码

# ================= 浏览器 =================
browser:
  headless: true        # true=无头后台运行(可导出PDF); false=显示浏览器窗口
  channel: msedge       # 使用系统浏览器: msedge / chrome; 填 chromium 则用内置浏览器
  slow_mo: 0            # 每个操作(点击/输入)之间的间隔毫秒数, 有头观察时可设 300~800
  # user_agent: ""      # 高级选项: 自定义 UA，一般留空即可
  # locale: zh-CN
  # timezone: Asia/Shanghai

# 输出根目录（每个任务会在下面创建一个带时间戳的子目录）
output_dir: "./output"

# 任务失败后的整体重试次数（仅对网关超时等疑似临时网络问题生效，每次间隔 60 秒）
task_retries: 1

# ================= 任务列表（可配置多个，按顺序执行） =================
tasks:
  # ---- Embase 示例 ----
  - name: embase_demo             # 任务名，用于输出子目录命名
    site: chaoslib                # 站点: 目前支持 chaoslib
    database: embase              # 数据库: embase / pubmed
    query: "'adverse event'/exp OR 'adverse event'"   # Embase 检索式

    # 检索日期范围（对应结果页 Date 面板，设置后再点搜索按钮）
    date_filter:
      enabled: true
      type: records_added         # records_added=入库日期(Start/End date); publication_years=发表年份
      start: "2026-08-23"         # records_added 填 yyyy-mm-dd
      end: "2026-08-28"           # publication_years 时填年份如 "2020"

    per_page: 200                 # 每页显示条数 (Display: N results per page)
    max_pages: 0                  # 最多处理多少页, 0=全部

    # 检索结果页整页截图（应用日期过滤后截取; 仅 Embase）
    screenshot:
      enabled: true

    # 逐页打印 PDF（等价于浏览器“打印-另存为PDF”，含页眉页脚设置）
    print_pdf:
      enabled: true
      header_footer: true         # 页眉(左日期/右标题) 页脚(左网址/右页码)，与浏览器默认样式一致
      paper_format: A4            # A4 / Letter
      scale: 1.0                  # 缩放, 0.1 ~ 2.0
      # 高级: 自定义页眉/页脚 HTML 模板(留空=默认)。可用占位符 class:
      #   date / title / url / pageNumber / totalPages
      # header_template: ""
      # footer_template: ""

    # 逐页导出 CSV（对应 Select -> Export）
    export_csv:
      enabled: true
      fields_by: column           # column / row
      fields:                     # 需要勾选的字段，名字必须与网页上的文字完全一致
        - "Title"
        - "Source"
        - "Author names"
        - "Digital Object Identifier (DOI)"
        - "Medline PMID"

  # ---- Pubmed 示例（取消注释即可用; 流程: 检索+Custom Range日期筛选+逐页打印+全量CSV） ----
  # - name: pubmed_demo
  #   site: chaoslib
  #   database: pubmed
  #   query: "adverse events"
  #   date_filter:               # 对应 PUBLICATION DATE -> Custom Range -> Apply
  #     enabled: true
  #     type: records_added      # pubmed 下 type 不区分, 起止都填 yyyy-mm-dd
  #     start: "2026-08-23"
  #     end: "2026-08-28"
  #   per_page: 200              # 对应 URL 参数 size=200
  #   max_pages: 0
  #   print_pdf: {enabled: true}
  #   export_csv: {enabled: true}   # Save -> All results + CSV -> Create file 一次导出
  #   # pubmed 不支持: screenshot / export_csv.fields 自定义字段(PubMed CSV 字段固定)
"""

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")


@dataclass
class AccountConfig:
    username: str = ""
    password: str = ""


@dataclass
class BrowserConfig:
    headless: bool = True
    channel: str = "msedge"  # msedge / chrome / chromium
    slow_mo: int = 0
    user_agent: str = ""     # 留空=自动; 高级选项，一般不用改
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"


@dataclass
class DateFilterConfig:
    enabled: bool = False
    type: str = "records_added"  # records_added / publication_years
    start: str = ""
    end: str = ""


@dataclass
class PrintPdfConfig:
    enabled: bool = True
    header_footer: bool = True
    paper_format: str = "A4"
    scale: float = 1.0
    header_template: str = ""  # 留空=内置默认(贴近浏览器打印样式); 占位符 class: date/title/url/pageNumber/totalPages
    footer_template: str = ""


@dataclass
class ScreenshotConfig:
    enabled: bool = True  # 检索结果页整页截图


@dataclass
class ExportCsvConfig:
    enabled: bool = True
    fields_by: str = "column"  # column / row
    fields: list[str] = field(default_factory=list)


@dataclass
class TaskConfig:
    name: str = ""
    site: str = "chaoslib"
    database: str = "embase"
    category: str = "医学数据库"  # chaoslib 左侧分类名
    query: str = ""
    date_filter: DateFilterConfig = field(default_factory=DateFilterConfig)
    per_page: int = 200
    max_pages: int = 0  # 0 = 全部页
    screenshot: ScreenshotConfig = field(default_factory=ScreenshotConfig)
    print_pdf: PrintPdfConfig = field(default_factory=PrintPdfConfig)
    export_csv: ExportCsvConfig = field(default_factory=ExportCsvConfig)


@dataclass
class AppConfig:
    account: AccountConfig
    browser: BrowserConfig
    output_dir: Path
    tasks: list[TaskConfig]
    task_retries: int = 1  # 任务失败后整体重试次数（仅对疑似临时网络问题生效）


def _expect_mapping(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} 必须是一个键值段(mapping)")
    return value


def _parse_task(raw, idx: int) -> TaskConfig:
    where = f"tasks[{idx}]"
    raw = _expect_mapping(raw, where)

    df_raw = _expect_mapping(raw.get("date_filter") or {}, f"{where}.date_filter")
    df = DateFilterConfig(
        enabled=bool(df_raw.get("enabled", False)),
        type=str(df_raw.get("type", "records_added")).strip(),
        start=str(df_raw.get("start", "")).strip(),
        end=str(df_raw.get("end", "")).strip(),
    )
    if df.enabled:
        if df.type not in ("records_added", "publication_years"):
            raise ConfigError(f"{where}.date_filter.type 只能是 records_added 或 publication_years")
        pat = _DATE_RE if df.type == "records_added" else _YEAR_RE
        hint = "yyyy-mm-dd" if df.type == "records_added" else "yyyy"
        for label, val in (("start", df.start), ("end", df.end)):
            if not pat.match(val):
                raise ConfigError(f"{where}.date_filter.{label} 格式应为 {hint}，当前为: {val!r}")

    pp_raw = _expect_mapping(raw.get("print_pdf") or {}, f"{where}.print_pdf")
    pp = PrintPdfConfig(
        enabled=bool(pp_raw.get("enabled", True)),
        header_footer=bool(pp_raw.get("header_footer", True)),
        paper_format=str(pp_raw.get("paper_format", "A4")),
        scale=float(pp_raw.get("scale", 1.0)),
        header_template=str(pp_raw.get("header_template", "") or ""),
        footer_template=str(pp_raw.get("footer_template", "") or ""),
    )
    if not 0.1 <= pp.scale <= 2.0:
        raise ConfigError(f"{where}.print_pdf.scale 必须在 0.1 ~ 2.0 之间")

    ex_raw = _expect_mapping(raw.get("export_csv") or {}, f"{where}.export_csv")
    ex = ExportCsvConfig(
        enabled=bool(ex_raw.get("enabled", True)),
        fields_by=str(ex_raw.get("fields_by", "column")).strip().lower(),
        fields=[str(f).strip() for f in (ex_raw.get("fields") or []) if str(f).strip()],
    )
    if ex.fields_by not in ("column", "row"):
        raise ConfigError(f"{where}.export_csv.fields_by 只能是 column 或 row")
    if ex.enabled and not ex.fields:
        raise ConfigError(f"{where}.export_csv.fields 不能为空（导出时需要勾选字段）")

    task = TaskConfig(
        name=str(raw.get("name", "")).strip(),
        site=str(raw.get("site", "chaoslib")).strip(),
        database=str(raw.get("database", "embase")).strip(),
        category=str(raw.get("category", "医学数据库")).strip(),
        query=str(raw.get("query", "")).strip(),
        date_filter=df,
        per_page=int(raw.get("per_page", 200)),
        max_pages=int(raw.get("max_pages", 0)),
        screenshot=ScreenshotConfig(
            enabled=bool(_expect_mapping(raw.get("screenshot") or {}, f"{where}.screenshot").get("enabled", True)),
        ),
        print_pdf=pp,
        export_csv=ex,
    )
    if not task.query:
        raise ConfigError(f"{where}.query 不能为空（检索内容/关键词）")
    if task.per_page <= 0:
        raise ConfigError(f"{where}.per_page 必须为正整数")
    if task.max_pages < 0:
        raise ConfigError(f"{where}.max_pages 不能为负数，0 表示全部")
    return task


def parse_raw_config(raw: dict, base_path: Path, prompt: bool = True) -> AppConfig:
    """把已解析的 YAML dict 变成 AppConfig（含环境变量覆盖与校验）。

    prompt=True 时，缺账号密码会在命令行询问；GUI 场景传 False，直接报错提示填写。
    环境变量: AUTOSEARCH_USERNAME / AUTOSEARCH_PASSWORD / AUTOSEARCH_OUTPUT_DIR / AUTOSEARCH_HEADLESS
    """
    raw = _expect_mapping(raw or {}, "配置文件根节点")

    acc_raw = _expect_mapping(raw.get("account") or {}, "account")
    account = AccountConfig(
        username=str(acc_raw.get("username") or "").strip(),
        password=str(acc_raw.get("password") or "").strip(),
    )
    account.username = os.environ.get("AUTOSEARCH_USERNAME", account.username).strip()
    account.password = os.environ.get("AUTOSEARCH_PASSWORD", account.password).strip()
    if prompt:
        if not account.username:
            account.username = input("请输入 chaoslib 账号: ").strip()
        if not account.password:
            account.password = getpass.getpass("请输入 chaoslib 密码: ").strip()
    elif not account.username or not account.password:
        raise ConfigError("账号或密码为空，请填写")

    br_raw = _expect_mapping(raw.get("browser") or {}, "browser")
    browser = BrowserConfig(
        headless=bool(br_raw.get("headless", True)),
        channel=str(br_raw.get("channel", "msedge")).strip().lower(),
        slow_mo=int(br_raw.get("slow_mo", 0)),
        user_agent=str(br_raw.get("user_agent", "")).strip(),
        locale=str(br_raw.get("locale", "zh-CN")).strip(),
        timezone=str(br_raw.get("timezone", "Asia/Shanghai")).strip(),
    )
    if browser.channel not in ("msedge", "chrome", "chromium"):
        raise ConfigError("browser.channel 只能是 msedge / chrome / chromium")
    env_headless = os.environ.get("AUTOSEARCH_HEADLESS")
    if env_headless is not None:
        browser.headless = env_headless.strip().lower() in ("1", "true", "yes", "on")

    out = os.environ.get("AUTOSEARCH_OUTPUT_DIR") or str(raw.get("output_dir") or "./output")
    out_path = Path(out).expanduser()
    if not out_path.is_absolute():
        out_path = (base_path / out_path).resolve()

    tasks_raw = raw.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise ConfigError("tasks 必须是非空列表")
    tasks = [_parse_task(t, i) for i, t in enumerate(tasks_raw)]

    task_retries = max(0, int(raw.get("task_retries", 1)))

    return AppConfig(account=account, browser=browser, output_dir=out_path,
                     tasks=tasks, task_retries=task_retries)


def load_config(path: Path, output_dir: str | None = None,
                force_headed: bool = False, ask: bool = False) -> AppConfig:
    """读取 config.yaml，应用环境变量/命令行覆盖，并做合法性校验。

    优先级（高到低）: 命令行参数 > 环境变量 > 配置文件。
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 解析失败: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError("配置文件根节点必须是键值段(mapping)")
    if ask:
        acc = raw.get("account") or {}
        acc["username"] = ""
        acc["password"] = ""
        raw["account"] = acc
    cfg = parse_raw_config(raw, path.parent, prompt=True)
    if output_dir:
        out_path = Path(output_dir).expanduser()
        cfg.output_dir = out_path if out_path.is_absolute() else (path.parent / out_path).resolve()
    if force_headed:
        cfg.browser.headless = False
    return cfg
