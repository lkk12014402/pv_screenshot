# auto_search — 医学数据库自动检索工具

自动化完成「登录 chaoslib（混沌书苑 WebVPN）→ 进入 Embase → 检索 → 设置日期范围 →
逐页打印 PDF → 逐页导出 CSV」的完整流程。支持**无头（后台）运行**，可打包为
Windows 单文件 exe。

> 技术栈：Python + Playwright（浏览器自动化）+ PyInstaller（打包 exe）。
> 架构按「站点插件 + 数据库流程」设计，后续新增网址（万方、CNKI）或数据库
> （Pubmed）时，只需在 `auto_search/sites/` 下增加对应模块，无需改动主程序。

## 功能对应关系（对照截图步骤）

| 步骤 | 截图 | 程序实现 |
| --- | --- | --- |
| 1. 登录 | 0.png | 自动填账号/密码、勾选免责声明、点登录（账号密码在 config.yaml 或环境变量中配置） |
| 2. 医学数据库 → Embase | 1.png | 点击左侧分类和数据库卡片，自动跟踪新标签页 |
| 3. 输入检索内容 | 2.png | `tasks[].query` 配置检索式 |
| 4. 设置日期范围再搜索 | 3.png | `date_filter` 配置（入库日期 records_added 或发表年份 publication_years） |
| 5. 每页显示条数 | 4.png | `per_page: 200` |
| 6. 打印（含页眉页脚）、逐页 | 5.png | `print_pdf`：等价于浏览器"打印→另存为PDF"，自动逐页保存到输出目录 |
| 7. Select → Export 导出 CSV、逐页 | 6.png / 7.png | `export_csv`：自动勾选指定字段，逐页导出并合并为 `export_all.csv` |

## 一、直接运行 exe（Windows 用户）

1. 把 `auto_search.exe` 放到任意目录，**双击运行会打开图形界面**（首次运行会在同目录生成
   `config.yaml` 作为默认配置）。
2. 界面上填写/修改：账号密码、检索式、日期范围、每页条数、输出目录、导出字段等。
   - 界面里的值**只对本次运行生效**；`config.yaml` 始终保留为默认配置。
   - 点「保存为默认配置」可把当前界面值写回 `config.yaml`（会丢失原文件中的注释）。
3. 点「开始运行」，日志会显示在界面下方；完成后到输出目录查看结果。

命令行模式（完全按 config.yaml 运行，支持多任务）：

```
auto_search.exe --cli              # 使用同目录 config.yaml
auto_search.exe --cli -c my.yaml   # 指定配置文件
auto_search.exe --cli -o D:\结果   # 覆盖输出目录
auto_search.exe --cli --headed     # 显示浏览器窗口
auto_search.exe --cli --ask        # 手动输入账号密码
```

说明：

- 默认调用 Windows 自带的 **Edge** 浏览器（`channel: msedge`），无需额外下载浏览器。
- **无头运行**（默认）：不弹出浏览器窗口，全程后台执行。
- **取消勾选「无头运行」**（或命令行加 `--headed`）：会弹出真实浏览器窗口，可以**全程观察
  自动操作的过程**（登录、输入检索式、设置日期、翻页、导出等）。注意：此模式下无法导出
  PDF（Chromium 限制），运行时若勾选了 PDF 会提示并自动跳过；CSV 导出不受影响。
- 账号密码不想写进文件：设置环境变量 `AUTOSEARCH_USERNAME` / `AUTOSEARCH_PASSWORD`，
  或命令行加 `--ask` 手动输入。

## 二、配置文件说明（config.yaml）

```yaml
account:
  username: ""        # chaoslib 账号
  password: ""        # chaoslib 密码

browser:
  headless: true      # 无头后台运行；false 则弹出浏览器窗口
  channel: msedge     # msedge / chrome / chromium(内置浏览器)

output_dir: "./output"   # 输出根目录，可用绝对路径如 "D:\\检索结果"

tasks:                     # 可配多个任务，按顺序执行
  - name: embase_demo
    site: chaoslib
    database: embase
    query: "'adverse event'/exp OR 'adverse event'"   # Embase 检索式，可任意自定义

    date_filter:           # 对应结果页 Date 面板
      enabled: true
      type: records_added  # records_added=入库日期(yyyy-mm-dd)；publication_years=发表年份(yyyy)
      start: "2026-08-23"
      end: "2026-08-28"

    per_page: 200          # 每页显示条数
    max_pages: 0           # 最多处理页数，0=全部

    print_pdf:             # 逐页打印为 PDF（含页眉页脚）
      enabled: true
      header_footer: true
      paper_format: Letter # 或 A4
      scale: 1.0

    export_csv:            # 逐页导出 CSV
      enabled: true
      fields_by: column    # column / row
      fields:              # 字段名必须与网页上的文字完全一致
        - "Title"
        - "Source"
        - "Author names"
        - "Digital Object Identifier (DOI)"
        - "Medline PMID"
```

每次运行的输出结构：

```
output/
└── embase_demo_20260829_123000/     # 任务名_时间戳
    ├── pdf/page_001.pdf ...         # 逐页打印结果
    ├── csv/page_001.csv ...         # 逐页导出结果
    ├── csv/export_all.csv           # 合并后的完整 CSV
    └── run.log                      # 运行日志（出错时另有 _debug/ 截图和网页快照）
```

## 三、从源码运行（开发者）

```bash
pip install -r requirements.txt
playwright install chromium        # 如果不用系统 Edge/Chrome
cp config.example.yaml config.yaml # 编辑后运行
python main.py                     # 图形界面
python main.py --cli               # 纯命令行，按 config.yaml 运行
```

## 四、打包 exe（在 Windows 上执行）

```
build_exe.bat
```

生成的 `dist\auto_search.exe` 为单文件（约几十 MB，含 Python 运行时和 playwright
驱动；浏览器使用系统 Edge，不打包进 exe）。

## 五、常见问题

- **登录失败**：程序会在输出目录 `_debug/` 保存截图和 HTML，核对账号密码是否正确、
  网站是否改版。
- **提示“账号已在其他设备登录”**：这是 WebVPN 的单点登录限制（同一账号同时只允许
  一处在线，上一次运行或你浏览器里登录的会话可能还没过期）。程序会自动点击
  “强制登录/强制下线”踢掉旧会话继续运行；注意这会把你正在用的浏览器登录态踢下线。
- **进入数据库时 504 Gateway Timeout**：WebVPN 中转网关偶发的上游超时（服务端问题），
  程序会自动重试 5 次（间隔递增），仍失败则整个任务 60 秒后再重试一次
  （`task_retries` 配置）。频繁出现就说明平台在维护，换个时间段再跑。
- **Cloudflare 拦截页(Sorry, you have been blocked)**：embase.com 对无头浏览器/异常
  流量的风控。程序已内置反检测（完整版浏览器、真实 UA、屏蔽 webdriver 特征）并会自动
  刷新重试一次；仍被拦就稍等重跑，或改用真实浏览器 channel(msedge/chrome)。
- **网页改版导致找不到按钮**：选择器集中在 `auto_search/sites/embase.py` 顶部的
  `class S` 和 `chaoslib.py` 中，对照 `_debug/` 里的快照调整即可。
- **WebVPN 会话超时**：检索结果特别多（几百页）时，长时间运行可能掉线，建议用
  `max_pages` 分批执行。
- **导出字段没勾上**：`fields` 里的名字必须和网页对话框中的文字完全一致；日志会
  列出对话框里所有可用字段名，照着改即可。
- **导出格式**：Embase 导出对话框默认是 RIS 格式，程序会自动切换为 CSV 再勾选字段，
  无需手动干预。
