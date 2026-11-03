# Business Data Collector

**状态：实验性版本 v0.2.0**。能跑真实采集，结果需要自己筛；已知问题列在文末，欢迎使用和改进。

**维护者 / Maintainer：Loop** ｜ 开发过程使用 AI 辅助 ｜ 本项目是集成与扩展，**不是**原创采集引擎

---

## 来源与归属 / Attribution

**请先读这一节，再读代码。**

本仓库的 Go 采集核心不是本项目写的。可执行文件里跑的搜索、翻页、DOM 解析、浏览器采集，
来自 **gosom/google-maps-scraper**；它依赖的并发抓取框架来自 **gosom/scrapemate**，
本项目使用的是**本地修改副本**（通过 `go.mod` 的 `replace` 指向 `forks/scrapemate`）。
两个上游都是 MIT 许可，原作者版权声明原样保留在各自目录：

| 上游 | 链接 | 许可 | 版权 | 在本仓库的位置 |
|---|---|---|---|---|
| google-maps-scraper | https://github.com/gosom/google-maps-scraper | MIT | Copyright (c) 2023 Georgios Komninos | `google-maps-scraper/`（含本地修改） |
| scrapemate | https://github.com/gosom/scrapemate | MIT | Copyright (c) 2023 Georgios Komninos | `forks/scrapemate/`（基于 v1.4.0，含本地修改） |

- 上游基准：`google-maps-scraper` 取自其 `main` 分支提交
  `d0b51bcf3cd56d9a3f71e049cb6e226554e24162`（"Updates sponsor's link"，2026-09-24），
  `scrapemate` 取自上游发布 **v1.4.0**。两者都在此之上有本地修改，清单见
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- 各上游目录内的 `LICENSE` **原样保留，未被根目录许可覆盖或替换**。根 `LICENSE` 只覆盖
  本项目新增的贡献，不覆盖也不取代任何第三方许可。
- 修改内容与文件清单、捆绑的第三方前端资源（xterm.js、Pico CSS）、捆绑的行政区划数据
  （WTFPL）以及未能完全核实的部分，全部记在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

English: this project is an integration around **gosom/google-maps-scraper** (MIT) with a
locally modified copy of **gosom/scrapemate** (MIT). Upstream copyright notices are retained
in each upstream directory and are not replaced by the root `LICENSE`, which covers only the
contributions made here. See `THIRD_PARTY_NOTICES.md` for the exact file-level changes.

## 谁写了什么 / What comes from where

| 层 | 来源 | 说明 |
|---|---|---|
| Go 采集核心（搜索、翻页、DOM 解析、浏览器模式、导出 JSONL） | **上游 gosom/google-maps-scraper** | 本项目在其上加了两类改动：13 个新增文件（含 `records/` 记录格式包、`web/apiview.go`、若干测试），14 个文件的内容修改（`runner/`、`web/`、`grid/`、`go.mod`，共 +1325/−101 行）。`records/` 是本项目写的，不属于上游。 |
| 并发抓取与浏览器会话框架 | **上游 gosom/scrapemate v1.4.0** | 本地修改 6 个文件（`adapters/fetchers/jshttp/{jshttp,local_proxy,pool,session_slot}.go`、`scrapemate.go`、`scrapemateapp/scrapemateapp.go`）并新增 3 个测试。 |
| Python 编排层（`collector/`） | **本项目新增** | 运行编排、来源装配、配置校验、断点恢复。 |
| 证据记录（SQLite 观测 / 字段事实 / 合并理由 / 来源与获取方式） | **本项目新增** | 每条字段带来源页与原文片段；同源与跨源合并分开记录。 |
| 中文界面（`local_ui.py`） | **本项目新增** | 本地 HTML 控制台，标准库自带，不含第三方前端框架。 |
| 多语言导出（中文 / 英文 / 双语 CSV、XLSX、JSONL） | **本项目新增** | XLSX 用 openpyxl；其余为标准库。 |
| 中国行政区划数据（`collector/collector/data/cn_divisions.json`） | **第三方数据** | 来自 modood/Administrative-divisions-of-China（WTFPL），上游为国家统计局区划代码；详见 notices。 |

一句话：**Go 采集核心是上游的（有本地修改），Python 编排、证据记录、中文界面、多语言导出是本项目新增的。**
不宣称整体原创。

## 能做什么

- Go 地图采集核心与 Python 多来源编排。
- SQLite 运行记录、字段证据及可追溯的合并记录。
- 本地 CSV / 已保存网页导入、官网信息补全。
- 中文、英文、双语阅读表及 CSV / JSONL 技术导出。

## 边界

搜索结果是候选，不保证每条符合行业或城市；目标条数是停止阈值，不是严格上限。国内目录覆盖有限，城市别名表有限。翻译选项主要控制表头与说明，不翻译原始商家名称和地址。本工具仅默认监听本机，不适合未经鉴权直接部署公网。

数据来自第三方站点。使用前请自行确认目标站点的服务条款与所在地法律；本项目不对采集行为与采集结果的合规性作保证，也不提供绕过访问限制的功能。

## 安装（Windows / PowerShell）

```powershell
cd collector
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

在线地图采集还需构建 Go 核心（Go 版本见其 go.mod）：

```powershell
cd ../google-maps-scraper
go build -o bin/google-maps-scraper.exe .
```

保留同级 forks/scrapemate，Go replace 指令依赖它。浏览器采集的额外安装步骤见 Go 核心 README。

启动本地界面：

```powershell
cd ../collector
# 如需代理，设置你自己的地址；不需要则不设置
# $env:COLLECTOR_PROXY = "http://127.0.0.1:PORT"
.venv\Scripts\python.exe -m collector ui --base-dir ..
```

## 验证状态（2026-11-04）

发布候选源码：本机 Python 测试 1173 passed、2 skipped、102 subtests passed；Go 主程序构建成功并能显示帮助。测试使用原项目已有 Python 环境，并非全新机器验收。未在本轮重新验证外部网站采集，不保证实时可达。

未包含真实运行库、采集数据、聊天记录、截图、虚拟环境和旧 Git 历史。部分依赖本机辅助脚本或真实网页样本的检查不适用于发布副本。根目录 LICENSE 用于新增贡献，上游各目录保留自身许可。

运行测试：在 collector 目录安装 requirements-dev.txt 后执行 `python -m pytest -q`。Go 测试素材未全部收录，本轮只验证主程序构建，不宣称 Go 全量测试通过。

## 已知问题（v0.2.0）

这是一个**实验性版本**，下面这些是真的、已知的，不打算藏：

- **结果要自己筛。** 搜索结果是候选，行业与城市不保证准确；国内企业目录这一路尤其脏，
  同一页里会混进不相关的企业，得自己剔除。
- **国内在线目录覆盖有限**，城市别名表也有限。不建议当成"全国可用"。
- **目标条数是停止阈值，不是上限。** 填 10 可能拿回 19 条，也可能只拿回 6 条就停。
- **翻译只翻表头与说明**，不翻商家名称和地址。
- **评论数不总是可用。** 快速模式经常不给，表格会如实显示"未获取"而不是 0。
- **判重有边界。** 同名同电话但地址冲突的，保留为两行，标"疑似不同网点或重复登记（未确认）"，
  不自动合并——宁可让你自己看，不替你做判断。
- Go 部分只验证了主程序构建；上游测试素材未全部随包，因此**不宣称 Go 全量测试通过**。
  Python 侧 1173 passed / 2 skipped，跳过的 2 条需要未分发的素材。
- 没有在全新电脑上做过验收，用的是本机已有环境。
- 源码注释里保留了开发期的内部编号（"task book 004 / round 026"之类），读者对不上号，
  属于历史遗留措辞，不影响功能。
- 没有自动翻译、没有严格的条数上限、没有逐个手工审完所有安装依赖（见
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 的检查范围）。

## 上游与许可

- 归属、逐文件改动、安装依赖清单、明确未包含的内容：见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- 随包分发的第三方组件（xterm.js、xterm-addon-fit、Pico CSS、区划数据）的**许可全文**：
  见 [third_party_licenses/](third_party_licenses/README.md)。
- 根 `LICENSE` 是 MIT，覆盖本项目新增的贡献；两个上游目录保留各自的 LICENSE 与版权，
  互不覆盖。本项目新增贡献署名 Loop。
