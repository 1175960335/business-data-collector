# 随包分发的第三方组件：许可全文

这个目录放的是**随本仓库一起分发**的第三方代码和数据各自的许可原文。
`THIRD_PARTY_NOTICES.md` 里写的是"哪个文件来自哪里、改了什么"，这里放的是许可证本身。

为什么要单独放：`admin/static/` 下的两个 xterm 压缩文件在上游打包时就把许可横幅剥掉了，
文件里一个版权字都没有；只写一句"MIT"不构成完整许可材料，所以把原文放在这里。

| 组件 | 随包位置 | 版本 | 许可 | 版权 | 许可全文 |
|---|---|---|---|---|---|
| xterm.js | `google-maps-scraper/admin/static/xterm.min.js`、`xterm.css` | 未确认（压缩文件内无版本串） | MIT | Copyright (c) 2017-2019 The xterm.js authors；2014-2016 SourceLair Private Company；2012-2013 Christopher Jeffrey | `xterm.js.LICENSE.txt` |
| xterm-addon-fit | `google-maps-scraper/admin/static/xterm-addon-fit.min.js` | 未确认 | MIT | 同上（同一仓库 `addons/addon-fit`，共用仓库 LICENSE） | `xterm-addon-fit.LICENSE.txt` |
| Pico CSS | `google-maps-scraper/admin/static/pico.min.css` | **2.1.1**（文件横幅写明） | MIT | Copyright 2019-2025 Pico（文件横幅）/ Copyright (c) 2019-2024 Pico（上游 v2.1.1 的 LICENSE.md） | `pico-css.LICENSE.md` |
| Administrative-divisions-of-China（数据） | `collector/collector/data/cn_divisions.json` | 数据集 2023-06-30 快照 | WTFPL v2 | Copyright (C) 2004 Sam Hocevar（WTFPL 文本）；数据集由 modood 整理，上游为国家统计局区划代码 | `administrative-divisions-of-china.WTFPL.txt` |

几点说明：

- **xterm 的版本号确实核不出来**：两个 `.min.js` 里没有版本串、没有横幅，也没有 source map。
  许可全文与版本无关（MIT 各版本条款一致），所以按"许可已补齐、版本未确认"处理。
  如果之后决定收紧，办法是重新从上游某个确定 tag 拉这两个文件替换，而不是继续猜。
- **xterm-addon-fit 的许可文件与 xterm.js 内容相同**：它是同一个仓库里的 addon，
  共用仓库根目录的 LICENSE。这里单独放一份，是为了让读者不用跳仓库就能看到。
- **Pico 的两处年份不一致**是上游自己的：文件横幅写 2019-2025，v2.1.1 的 LICENSE.md 写
  2019-2024。两处都原样保留，没有替上游改写。
- **区划数据只有省级与地级**（31 省 / 333 市），另有杭州市下辖 13 个县级单位写在
  `regions.py` 里，已与上游 `dist/areas.json` 的 3301 项逐条核对一致（2026-11-04）。

上游许可全文来源：

- https://github.com/xtermjs/xterm.js/blob/master/LICENSE
- https://github.com/picocss/pico/blob/v2.1.1/LICENSE.md
- https://github.com/modood/Administrative-divisions-of-China/blob/master/LICENSE
