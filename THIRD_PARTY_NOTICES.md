# Third-party notices / 第三方代码与许可说明

维护者：Loop。本文件按三件事分开写，因为它们的义务不同：

- **A. 实际包含、复制或修改的代码** —— 随本仓库分发，必须保留许可与署名。
- **B. 安装依赖** —— 构建时下载，本仓库不分发其源码，义务是列出而非提供许可全文。
- **C. 仅参考思路** —— 没有复制代码，不构成许可义务。

判定依据是**仓库内的文件本身**（许可证文件、版权头、构建声明），不是聊天记录或口头说明。
每一条都标了它是"实测"还是"待核实"。

---

## A. 实际包含或修改的代码

### A1. google-maps-scraper — MIT

| 项 | 内容 |
|---|---|
| 位置 | `google-maps-scraper/` |
| 上游 | https://github.com/gosom/google-maps-scraper |
| 许可 | MIT |
| 版权 | Copyright (c) 2023 Georgios Komninos |
| 许可文件 | `google-maps-scraper/LICENSE`（原样保留，未改动） |
| 基准提交 | `d0b51bcf3cd56d9a3f71e049cb6e226554e24162`（上游 `main`，"Updates sponsor's link"，2026-09-24） |

**本项目在这棵树上的改动**（实测：以本地克隆的上游 git 记录 + 上游 v1.4.0 压缩包逐文件比对得出）：

- **新增 13 个文件**（上游没有，本项目写的）：
  `records/{record.go,jsonl.go,writer.go,record_test.go}`、
  `web/{apiview.go,apiview_test.go,spec_contract_test.go}`、
  `web/sqlite/migration_test.go`、
  `runner/{concurrency_test.go}`、`runner/filerunner/exit_status_internal_test.go`、
  `runner/webrunner/webrunner_status_test.go`、
  `gmaps/{emailjob_internal_test.go,entry_export_test.go}`。
  其中 `records/` 是本项目新写的记录格式包，**不属于上游**。
- **内容修改 14 个文件**（+1325 / −101 行）：
  `go.mod`、`grid/grid.go`、`grid/grid_test.go`、
  `runner/runner.go`、`runner/jobs_test.go`、`runner/filerunner/filerunner.go`、
  `runner/filerunner/filerunner_test.go`、`runner/resume/writer_test.go`、
  `runner/webrunner/webrunner.go`、`web/web.go`、`web/job.go`、`web/service.go`、
  `web/sqlite/sqlite.go`、`web/static/spec/spec.yaml`。
- 另有 **36 个文件相对上游只有换行符差异（LF→CRLF），内容未改**。`git status` 会把它们显示为
  modified，容易误读成"改了 50 个文件"，实际内容差异只有上面 14 个。
- **未删除任何版权声明**：对全部改动执行 `git diff | grep '^-.*[Cc]opyright'` 为空（实测）。
- **相对上游省略了 41 个跟踪文件**：`img/` 22 个赞助商图、`testdata/` 与 `gmaps/testdata/` 共 9 个
  抓取样本、`skills/` 4 个上游辅助脚本、`local/test_ssh_key(.pub)` 2 个上游自带的**测试用 SSH 密钥**
  （有意排除，非本机凭据）、`Dockerfile.saas`、`PROVISION`、`banner.png`、`examples/examples-api/typescript/scrape.ts`。
  后果：上游 Go 部分的测试依赖这些样本，因此本仓库**不宣称 Go 全量测试通过**；上游 README 中的
  赞助商图片会显示不出来。

### A2. scrapemate — MIT（本地修改副本，v1.4.0）

| 项 | 内容 |
|---|---|
| 位置 | `forks/scrapemate/` |
| 上游 | https://github.com/gosom/scrapemate |
| 许可 | MIT |
| 版权 | Copyright (c) 2023 Georgios Komninos |
| 许可文件 | `forks/scrapemate/LICENSE`（与上游 v1.4.0 的 LICENSE 逐字节相同，实测） |
| 基准版本 | v1.4.0（上游模块压缩包 `proxy.golang.org/github.com/gosom/scrapemate/@v/v1.4.0.zip`） |
| 接线方式 | `google-maps-scraper/go.mod` 中 `replace github.com/gosom/scrapemate => ../forks/scrapemate` |

**修改内容**（实测：与上游 v1.4.0 压缩包逐文件比对，共 9 处差异）：

- 修改 6 个文件：`adapters/fetchers/jshttp/jshttp.go`（86 行差异）、
  `adapters/fetchers/jshttp/local_proxy.go`（46）、`adapters/fetchers/jshttp/pool.go`（2）、
  `adapters/fetchers/jshttp/session_slot.go`（44）、`scrapemate.go`（9）、
  `scrapemateapp/scrapemateapp.go`（18）。改动集中在浏览器会话生命周期与本地代理。
- 新增 3 个测试文件：`jshttp_launch_args_internal_test.go`、`local_proxy_unauth_internal_test.go`、
  `session_slot_regression_internal_test.go`。
- 未删除任何版权声明（同样执行了 `diff | grep -i copyright` 检查，为空）。
- 相对上游省略 2 个文件：`cmd/tools/`（开发工具）与 `docs/.nojekyll`（GitHub Pages 标记），
  构建与运行均不引用它们。

### A3. 上游捆绑的前端库（本项目原样分发）

这三个文件在上游仓库里就已经是压缩后的第三方库，不是本项目引入的，但**随本仓库一起分发**，
因此需要在这里补记署名：

| 文件 | 项目 | 许可 | 版权声明 | 许可全文 |
|---|---|---|---|---|
| `google-maps-scraper/admin/static/xterm.min.js` | xterm.js | MIT | `Copyright (c) 2017-2019 The xterm.js authors`；`2014-2016 SourceLair Private Company`；`2012-2013 Christopher Jeffrey` | `third_party_licenses/xterm.js.LICENSE.txt` |
| `google-maps-scraper/admin/static/xterm-addon-fit.min.js` | xterm.js `addons/addon-fit` | MIT | 同上（同一仓库，共用仓库 LICENSE） | `third_party_licenses/xterm-addon-fit.LICENSE.txt` |
| `google-maps-scraper/admin/static/pico.min.css` | Pico CSS v2.1.1 | MIT | `Copyright 2019-2025`（文件横幅） | `third_party_licenses/pico-css.LICENSE.md` |

**许可全文已随仓库分发**，放在 `third_party_licenses/`，因此 MIT 要求的
"随副本附上版权声明与许可声明"已经满足，不再只写一句"MIT"。

同目录 `xterm.css` 里还留着上游自己的版权头（实测）：

```
Copyright (c) 2014 The xterm.js authors. All rights reserved.
Copyright (c) 2012-2013, Christopher Jeffrey (MIT License)
```

**仍然未确认的只有版本号**：两个 `*.min.js` 里没有版本串、没有横幅、也没有 source map，
只能确认它们属于 xterm.js 项目。MIT 各版本条款一致，所以许可材料完整，版本以"未确认"
记录（见 E1）。要收紧就得从上游某个确定 tag 重新取这两个文件，而不是继续猜。


### A4. 中国行政区划数据

| 项 | 内容 |
|---|---|
| 位置 | `collector/collector/data/cn_divisions.json`（20 KB） |
| 来源 | https://github.com/modood/Administrative-divisions-of-China |
| 许可 | **WTFPL v2**（实测：已取上游 `LICENSE` 原文核对，非依据注释；全文见 `third_party_licenses/administrative-divisions-of-china.WTFPL.txt`） |
| 上游数据出处 | 国家统计局《统计用区划代码和城乡划分代码》 |
| 数据截止 | 2023-06-30（上游发布 2023-09-11） |
| 覆盖 | 仅中国大陆省级与地级（31 省 / 333 市）；不含中国香港、中国澳门、中国台湾 |
| 附带说明 | 上游已注明：国家统计局自 2024-10 起不再公开具体区划代码，该数据集停止更新 |

WTFPL 允许无限制使用、复制、修改与再分发，不要求署名；这里仍然署名是因为上游整理方值得
记一笔。数据本身是**事实性的代码与名称**，本项目不主张任何额外权利。文件内自带 `_source` /
`_source_url` / `_upstream` / `_data_as_of` / `_coverage_note` 字段，来源与版本随数据一起分发。

**逐条核对结果（2026-11-04，与上游 `dist/` 同名文件比对）**：

| 部分 | 本项目 | 上游 | 结果 |
|---|---|---|---|
| `provinces` | 31 条 | `dist/provinces.json` 31 条 | **完全一致**（代码 + 名称逐条相同，双向差集为空） |
| `cities` | 333 条 | `dist/cities.json` 342 条 | 本项目的 333 条是上游的**子集**，无本项目独有的条目；少的 9 条是 `市辖区`、`省直辖县级行政区划` 这类占位项，被有意剔除 |
| 县级表（`regions.py`） | 杭州市 13 条 | `dist/areas.json` 中 `cityCode=3301` 的 13 条 | **完全一致**（代码 + 名称逐条相同） |

**县级表**在 `regions.py` 里写成 Python 常量（`CN_COUNTIES`），来源是民政部 2023 年县以上
行政区划代码公开列表，只覆盖本项目已配预设的杭州市。上面的核对说明：这 13 条与随包分发的
区划数据集逐条吻合，不是凭记忆写的。表里的"等级"字段由代码按 GB/T 2260 推导，并由测试校验
推导是否与登记值一致。该表是**代码与名称的对照事实**，不是复制某个软件库的实现。


**城市中心坐标** `CITY_CENTRES`：本项目自行整理的城市中心近似值（用于半径搜索的圆心），
不是测绘成果、不是第三方数据。原注释已写明"非测绘点"。

---

## B. 安装依赖（不随本仓库分发）

本仓库不含 `vendor/`、不打包任何依赖源码。下列依赖在构建/安装时从各自发布渠道获取，
许可与版权由各自项目持有。

### B1. Python 运行时依赖

| 包 | 版本 | 许可 | 证据 |
|---|---|---|---|
| PyYAML | 6.0.2 | MIT | 已安装包的 `dist-info` 元数据（实测） |
| phonenumbers | 9.0.41 | **Apache-2.0** | 已安装包内 `licenses/LICENSE` 全文（实测）；该包**未附带 NOTICE 文件** |
| dnspython | 2.9.0 | **ISC** | `METADATA` 的 `License-Expression: ISC`（实测）；作者 Bob Halley |
| openpyxl | 3.1.5 | MIT | `dist-info` 元数据（实测） |

`phonenumbers` 是 Google libphonenumber 的 Python 移植，用于电话号码解析；本项目只调用它，
没有复制其代码。

### B2. Python 开发依赖

`pytest >= 8.0` — MIT（已安装 9.1.1 的 `licenses/LICENSE`，Copyright (c) 2004 Holger Krekel and others）。
测试套件本身基于标准库 `unittest`，不装 pytest 也能跑。

### B3. Go 模块依赖

`google-maps-scraper/go.mod` 声明 37 个直接依赖（不含 indirect），完整闭包见 `go.sum`。
列举几个有代表性的：`PuerkitoBio/goquery`、`mxschmitt/playwright-go`、`jackc/pgx/v5`、
`modernc.org/sqlite`、`urfave/cli/v3`、`gosom/go-leadsdb`、`gosom/kit`、AWS SDK v2 系列。

- **本轮只就地核对了两项的许可**：`gosom/go-leadsdb` = MIT（Copyright (c) 2025 ZENDROM LTD）、
  `gosom/kit`（模块缓存内未附 LICENSE 文件）。其余 35 个模块的许可**未逐一核对**，见 E4。
- 这些模块不随本仓库分发，所以这里列名而非附许可全文；若将来改为 vendoring，必须补齐全文。

---

## C. 仅参考思路（未复制代码）

本轮**未发现**被复制进本仓库的"仅参考"类项目。判定方式：检查了全部 Python 与 Go 源码的
版权头、模块 docstring 与注释中的出处声明，除上文 A1–A4 外没有指向第三方的代码来源。

明确说明一个容易误判的点：`collector/collector/selector.py`（自写的 CSS 选择器引擎）与
`collector/collector/htmlextract.py`（自写的 HTML 抽取）都是本项目代码，**不是**对
BeautifulSoup / soupsieve / goquery 的移植或裁剪——它们不导入这些库，也不复制其代码结构。
这一点是"阅读源码后的判断"，不是可自动验证的结论，故标为**推断**。

---

## D. 明确未包含

真实采集数据库、抓取结果、导出表格、运行日志、截图、聊天与交接记录、账号凭据、
旧 Git 历史。发布副本中不存在 `*.sqlite`、`*.jsonl`、`*.xlsx`、`*.csv`、`runs/` 等运行产物。

---

## E. 待核实 / 未确认

| # | 事项 | 现状 |
|---|---|---|
| E1 | `xterm.min.js` 与 `xterm-addon-fit.min.js` 的精确版本号 | 压缩后无版本串、无 source map，只能确认属于 xterm.js 项目。**许可全文已随仓库分发**（`third_party_licenses/`），所以许可材料是完整的，只是版本号未确认 |
| E2 | 35 个 Go 模块的许可 | 未逐一核对；均为上游 `go.mod` 既有依赖，非本项目新增。只核对了本项目真正用到的那一个（`gosom/go-leadsdb`，MIT） |
| E3 | 上游抓取行为与 Google 服务条款的关系 | **不属于许可证问题**，但属公开发布后可能被问到的事。本项目不做法律判断，只提示：分发代码与遵守目标站点条款是两件事 |

（原 E2「xterm 许可全文未分发」与原 E3「区划数据再分发条件」已在 2026-11-04 关闭：
前者把 MIT 全文放进了 `third_party_licenses/`，后者通过逐条比对确认数据来自 WTFPL 数据集。）


---

## 许可证关系声明

根目录 `LICENSE`（MIT，Copyright (c) 2026 Loop）**只覆盖本项目新增的贡献**，
不覆盖、不替换、也不削减 `google-maps-scraper/LICENSE` 与 `forks/scrapemate/LICENSE`
中的原作者版权与许可条件。三者可以并存，因为它们同为 MIT；若将来引入不同许可的组件，
必须在此单列并说明其与 MIT 的兼容性，而不是修改上游文件。
