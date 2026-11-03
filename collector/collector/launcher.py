"""Windows 一键入口（``打开采集控制台.bat``）的契约与静态检查。

为什么这个模块存在
------------------
开发期实测：一个 UTF-8 的 .bat 里只要
有一行**非 ASCII 的 ``rem``**，cmd.exe 读这个文件的指针就会错位。后果有两个，第二个
比第一个严重：

1. 每次双击都先冒一句
   ``'……集控制台：' is not recognized as an internal or external command``；
2. 错位之后 cmd 会**跳过/吞掉后面的若干行 ``echo``**，也就是操作者看不到本该看到的提示。

把 ``chcp 65001`` 提到第一行**不能**避免它——错位发生在 chcp 之后的第一个非 ASCII
注释行。探针的对照结果：非 ASCII 只出现在 ``echo`` 的参数里（变体 B/C/E）是干净的，
文件长度跨过 1KB 也不是原因（变体 D 是对照组）。

所以规则是：**这个 .bat 里除 ``echo`` 行的参数之外，一个非 ASCII 字节都不许有。**
注释一律写英文，中文只出现在 ``echo`` / ``pause`` 之类的用户可见输出里。

这条规则没法写在 .bat 里解释（写进去就违反了规则本身），于是写在这里，并用
``collector/tests/test_launcher_bat.py`` 把它钉住：谁哪天顺手在注释里加一句中文，
测试会直接失败并指向本文件。
"""

from __future__ import annotations

import pathlib
import re

#: 交付给操作者的双击入口文件名（相对项目根目录）。
LAUNCHER_NAME = "打开采集控制台.bat"

#: 控制台默认监听端口。批处理里打印的地址必须和它一致，
#: ``collector.local_ui.serve`` 的默认值由测试交叉核对。
DEFAULT_PORT = 8757

#: 切换控制台代码页的那一行（纯 ASCII，必须是文件里第一条命令）。
CHCP_LINE = '"%SystemRoot%\\System32\\chcp.com" 65001 >nul'

_PORT_RE = re.compile(r"127\.0\.0\.1:(\d+)")
#: ``echo`` 行（允许前导空白，允许 ``echo.`` 这种只输出空行的写法）。
_ECHO_RE = re.compile(r"^\s*echo(\.|\s|$)", re.IGNORECASE)


def launcher_path(project_root: pathlib.Path) -> pathlib.Path:
    """返回项目根目录下的一键入口路径。"""
    return pathlib.Path(project_root) / LAUNCHER_NAME


def printed_port(text: str) -> int | None:
    """从批处理里打印给用户看的地址里取出端口，取不到返回 None。"""
    found = _PORT_RE.search(text)
    return int(found.group(1)) if found else None


def problems(data: bytes) -> list[str]:
    """检查一键入口的**字节形态**，返回人话写的问题清单；空列表表示合格。

    只看静态形态，不运行任何东西。真跑一遍由开发期的端到端脚本负责（未随包分发）。
    """
    found: list[str] = []

    if data.startswith(b"\xef\xbb\xbf"):
        found.append("文件带了 UTF-8 BOM；cmd.exe 会把 BOM 当成第一条命令的一部分")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return found + [f"文件不是合法 UTF-8：{exc}"]

    bare_lf = sum(1 for i, byte in enumerate(data)
                  if byte == 0x0A and (i == 0 or data[i - 1] != 0x0D))
    if bare_lf:
        found.append(f"有 {bare_lf} 个裸 LF 换行；批处理必须整篇 CRLF")

    lines = text.split("\r\n")
    chcp_at: int | None = None
    first_non_ascii: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == CHCP_LINE:
            chcp_at = index
        if any(ord(ch) > 127 for ch in line):
            first_non_ascii = index if first_non_ascii is None else first_non_ascii
            if not _ECHO_RE.match(line):
                found.append(
                    f"第 {index + 1} 行有非 ASCII 字符，但它不是 echo 行：{line.strip()!r}"
                    " —— 中文只允许出现在 echo 的参数里，见本模块开头的实测说明"
                )

    if chcp_at is None:
        found.append(f"找不到切换代码页的那一行：{CHCP_LINE}")
    elif first_non_ascii is not None and chcp_at > first_non_ascii:
        found.append(
            f"切换代码页在第 {chcp_at + 1} 行，晚于第 {first_non_ascii + 1} 行的中文；"
            "chcp 必须排在所有中文之前"
        )

    if 'cd /d "%~dp0collector"' not in text:
        found.append('找不到 cd /d "%~dp0collector"：批处理不靠当前目录定位项目，'
                     "双击时的工作目录由资源管理器决定")
    if "--base-dir" not in text:
        found.append("找不到 --base-dir：控制台会以错误的工作目录找结果库")

    return found
