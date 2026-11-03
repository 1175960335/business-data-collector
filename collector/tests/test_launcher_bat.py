"""双击入口 ``打开采集控制台.bat`` 的形态与契约测试。

这些断言全部来自 029 轮实测，不是「看起来更整齐」：

* 中文注释会让 cmd.exe 的读取指针错位——每次都冒一句
  ``not recognized as an internal or external command``，**并且吞掉后面的 echo 行**；
  ``chcp 65001`` 提前到第一行也挡不住（见 ``collector.launcher`` 与
  本地探针脚本）。
* bat 里打印的地址必须和控制台真正监听的端口一致，否则操作者照着地址打不开。
* ``cd /d "%~dp0collector"``：双击时的工作目录由资源管理器决定，不能依赖。
* CRLF、无 BOM：cmd.exe 的老习惯。
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import unittest

from collector import launcher, local_ui

BAT = launcher.launcher_path(pathlib.Path(__file__).resolve().parents[2])


class ShippedBatchShapeTest(unittest.TestCase):
    """线上那个文件本身的形态。"""

    def setUp(self):
        if not BAT.exists():
            self.skipTest(f"没有交付的批处理：{BAT}")
        self.data = BAT.read_bytes()
        self.text = self.data.decode("utf-8")

    def test_it_is_shipped_and_named_for_a_double_click(self):
        self.assertEqual(BAT.name, launcher.LAUNCHER_NAME)
        self.assertTrue(self.data)

    def test_there_is_no_bom(self):
        self.assertFalse(self.data.startswith(b"\xef\xbb\xbf"))

    def test_it_is_valid_utf8(self):
        self.data.decode("utf-8")          # 解不开就是失败，不需要额外断言

    def test_every_newline_is_crlf(self):
        bare = [i for i, byte in enumerate(self.data)
                if byte == 0x0A and (i == 0 or self.data[i - 1] != 0x0D)]
        self.assertEqual(bare, [], "批处理里出现了裸 LF")

    def test_the_shape_checker_finds_nothing_wrong(self):
        self.assertEqual(launcher.problems(self.data), [])

    def test_chinese_only_ever_appears_inside_an_echo_argument(self):
        offenders = [line for line in self.text.split("\r\n")
                     if any(ord(ch) > 127 for ch in line)
                     and not line.lstrip().lower().startswith("echo")]
        self.assertEqual(offenders, [])

    def test_the_codepage_switch_comes_before_any_chinese(self):
        chcp = self.data.index(b"chcp.com")
        first_chinese = next(i for i, b in enumerate(self.data) if b > 127)
        self.assertLess(chcp, first_chinese)

    def test_the_printed_address_matches_the_port_we_listen_on(self):
        self.assertEqual(launcher.printed_port(self.text),
                         inspect.signature(local_ui.serve).parameters["port"].default)

    def test_it_locates_the_project_by_its_own_path_not_the_working_directory(self):
        self.assertIn('cd /d "%~dp0collector"', self.text)
        self.assertIn("--base-dir ..", self.text)

    def test_the_operator_is_not_left_staring_at_a_vanished_window(self):
        self.assertIn("pause", self.text)
        self.assertIn("exit /b 1", self.text)

    def test_a_missing_virtual_environment_is_explained_in_chinese(self):
        self.assertIn("找不到虚拟环境", self.text)
        self.assertIn("pip install -r requirements.txt", self.text)


class LauncherProblemsTest(unittest.TestCase):
    """检查器本身：好文件不报，坏文件要指名道姓。"""

    GOOD = (
        "@echo off\r\n"
        f"{launcher.CHCP_LINE}\r\n"
        "rem ascii comment\r\n"
        'cd /d "%~dp0collector"\r\n'
        "echo 正在启动： http://127.0.0.1:8757/\r\n"
        ".venv\\Scripts\\python.exe -m collector ui --base-dir ..\r\n"
    )

    def test_a_good_file_reports_nothing(self):
        self.assertEqual(launcher.problems(self.GOOD.encode("utf-8")), [])

    def test_a_chinese_comment_is_caught(self):
        bad = self.GOOD.replace("rem ascii comment", "rem 中文注释")
        problems = launcher.problems(bad.encode("utf-8"))
        self.assertTrue(problems)
        self.assertIn("不是 echo 行", " ".join(problems))

    def test_a_bom_is_caught(self):
        data = b"\xef\xbb\xbf" + self.GOOD.encode("utf-8")
        self.assertIn("BOM", " ".join(launcher.problems(data)))

    def test_a_bare_lf_is_caught(self):
        data = self.GOOD.replace("\r\n", "\n", 1).encode("utf-8")
        self.assertIn("裸 LF", " ".join(launcher.problems(data)))

    def test_a_late_codepage_switch_is_caught(self):
        late = (
            "@echo off\r\n"
            "rem 中文注释跑到了最前面\r\n"
            f"{launcher.CHCP_LINE}\r\n"
            'cd /d "%~dp0collector"\r\n'
            "echo 正在启动\r\n"
            ".venv\\Scripts\\python.exe -m collector ui --base-dir ..\r\n"
        )
        joined = " ".join(launcher.problems(late.encode("utf-8")))
        self.assertIn("chcp 必须排在所有中文之前", joined)

    def test_a_missing_codepage_switch_is_caught(self):
        bad = self.GOOD.replace(f"{launcher.CHCP_LINE}\r\n", "")
        self.assertIn("找不到切换代码页", " ".join(launcher.problems(bad.encode("utf-8"))))

    def test_a_missing_absolute_cd_is_caught(self):
        bad = self.GOOD.replace('cd /d "%~dp0collector"', "cd collector")
        self.assertIn("cd /d", " ".join(launcher.problems(bad.encode("utf-8"))))

    def test_a_missing_base_dir_is_caught(self):
        bad = self.GOOD.replace("--base-dir ..", "")
        self.assertIn("--base-dir", " ".join(launcher.problems(bad.encode("utf-8"))))

    def test_the_port_is_read_back_from_the_printed_address(self):
        self.assertEqual(launcher.printed_port(self.GOOD), 8757)
        self.assertIsNone(launcher.printed_port("echo no address here"))


class LauncherGeneratorTest(unittest.TestCase):
    """生成器与线上文件不能漂移。"""

    def test_the_shipped_file_is_exactly_what_the_generator_writes(self):
        script = (pathlib.Path(__file__).resolve().parents[2]
                  / "verification" / "cn" / "ui" / "write-launcher-bat.py")
        if not script.exists():
            self.skipTest("没有生成脚本")
        spec = importlib.util.spec_from_file_location("launcher_generator", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        wanted = module.encoded()
        if not BAT.exists():
            self.skipTest(f"没有交付的批处理：{BAT}")
        self.assertEqual(BAT.read_bytes(), wanted,
                         "线上批处理和生成器不一致：改一处要改两处")


if __name__ == "__main__":
    unittest.main()
