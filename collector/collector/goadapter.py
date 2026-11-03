"""Adapter that drives the Go scraper as a child process.

Task book 004, stage C: "优先 CLI/JSONL 调用 Go，REST 作可选适配；参数以数组
传给子进程，不拼 shell 命令。"

Every invocation is built as an argument list and executed with ``shell=False``,
so a query containing spaces, quotes or ``&`` is passed through untouched and can
never be interpreted by a shell.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .errors import GoInvocationError

#: How much of a child's stderr is kept for an error message.
STDERR_TAIL_BYTES = 2000


@dataclass
class RunOutcome:
    """Result of one child-process invocation."""

    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class GoAdapter:
    """Builds and runs scraper command lines.

    ``prefix`` is inserted between the executable and the scraper arguments. It
    exists so the test suite can point ``binary`` at an interpreter and run a
    stub script instead of the real scraper, without going through a shell.
    """

    binary: pathlib.Path
    cwd: pathlib.Path | None = None
    proxy: str = ""
    prefix: tuple[str, ...] = ()
    extra_env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.binary = pathlib.Path(self.binary)
        if self.cwd is not None:
            self.cwd = pathlib.Path(self.cwd)

    def _base(self) -> list[str]:
        return [str(self.binary), *self.prefix]

    # -- capability ----------------------------------------------------------

    def exists(self) -> bool:
        return self.binary.is_file()

    def require(self) -> None:
        if not self.exists():
            raise GoInvocationError(
                f"scraper binary not found: {self.binary}. Build it with "
                "`bash tools/gorun.sh build -o bin/google-maps-scraper.exe .` or set "
                "runtime.go_binary in the config."
            )

    def version(self) -> str | None:
        """The scraper's own version string, or None if it cannot be read."""
        if not self.exists():
            return None
        try:
            proc = subprocess.run(
                [*self._base(), "-version"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(self.cwd) if self.cwd else None,
                env=self._env(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        text = (proc.stdout or "").strip()
        return text.splitlines()[0] if text else None

    # -- command line --------------------------------------------------------

    def build_argv(
        self,
        cfg,
        *,
        queries_file: pathlib.Path,
        results_path: pathlib.Path,
    ) -> list[str]:
        """The argument list for one discover task.

        Field gating lives in ``Config.fetch_flags``: a flag that costs an extra
        fetch is only added when the matching field was requested.
        """
        argv: list[str] = [
            *self._base(),
            "-input",
            str(queries_file),
            "-results",
            str(results_path),
            "-json",
            "-json-records",
            "-c",
            str(cfg.concurrency),
            "-lang",
            cfg.data["collect"]["language"],
            "-zoom",
            str(cfg.data["collect"]["zoom"]),
            "-exit-on-inactivity",
            f"{cfg.inactivity_seconds}s",
        ]

        if cfg.mode == "fast":
            # Fast mode refuses to start without a search centre, so the centre
            # is a validated part of the configuration rather than a guess here.
            lat, lon = cfg.geo
            argv += [
                "-fast-mode",
                "-geo",
                f"{lat},{lon}",
                "-radius",
                str(cfg.data["collect"]["radius_meters"]),
            ]
        else:
            argv += ["-depth", str(cfg.data["collect"]["max_depth"])]

            if cfg.bbox is not None:
                min_lat, min_lon, max_lat, max_lon = cfg.bbox
                argv += [
                    "-grid-bbox",
                    f"{min_lat},{min_lon},{max_lat},{max_lon}",
                    "-grid-cell",
                    str(cfg.data["collect"].get("grid_cell_km", 1.0)),
                ]

        argv += cfg.fetch_flags()

        if self.proxy:
            argv += ["-proxies", self.proxy]

        return argv

    # -- execution -----------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.extra_env)
        return env

    def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: int,
        stdout_path: pathlib.Path | None = None,
        stderr_path: pathlib.Path | None = None,
    ) -> RunOutcome:
        """Run a command line, capturing stdout/stderr to files.

        A timeout terminates the whole process tree: on Windows the scraper
        spawns Chromium children, and killing only the parent leaves them
        running (measured in stage B).
        """
        started = time.monotonic()

        for path in (stdout_path, stderr_path):
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)

        stdout_handle = open(stdout_path, "w", encoding="utf-8", errors="replace") if stdout_path else subprocess.DEVNULL
        stderr_handle = open(stderr_path, "w", encoding="utf-8", errors="replace") if stderr_path else subprocess.PIPE

        try:
            proc = subprocess.Popen(
                argv,
                stdout=stdout_handle,
                stderr=stderr_handle,
                cwd=str(self.cwd) if self.cwd else None,
                env=self._env(),
                shell=False,
            )
        except OSError as exc:
            if stdout_path:
                stdout_handle.close()
            if stderr_path:
                stderr_handle.close()
            raise GoInvocationError(f"could not start {argv[0]}: {exc}") from exc

        timed_out = False
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_tree(proc)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()
                proc.wait()

        duration = time.monotonic() - started

        if stdout_path:
            stdout_handle.close()

        stderr_tail = ""
        if stderr_path:
            stderr_handle.close()
            stderr_tail = _tail(stderr_path)
        elif stderr_handle is not subprocess.PIPE and hasattr(stderr_handle, "close"):
            stderr_handle.close()

        return RunOutcome(
            exit_code=proc.returncode,
            timed_out=timed_out,
            duration_seconds=duration,
            stderr_tail=stderr_tail,
        )

    @staticmethod
    def _terminate_tree(proc: subprocess.Popen) -> None:
        if sys.platform == "win32":
            taskkill = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            try:
                subprocess.run(
                    [str(taskkill), "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                return
            except (OSError, subprocess.SubprocessError):
                pass
        proc.terminate()


def _tail(path: pathlib.Path, limit: int = STDERR_TAIL_BYTES) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > limit:
        data = data[-limit:]
    return data.decode("utf-8", errors="replace").strip()
