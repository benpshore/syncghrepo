"""Run commands with a timeout. Every git and gh call goes through a Runner."""

import os
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# Never wait on a prompt: no terminal, no credential or update prompts.
QUIET = {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}


@dataclass(frozen=True)
class Result:
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    def last_line(self) -> str:
        """The most useful single line of output, for short reports."""
        for text in (self.err, self.out):
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines:
                return lines[-1][:200]
        return f"exit {self.code}"


# runner(cmd, cwd) -> Result. Tests pass a fake one.
Runner = Callable[[Sequence[str], str | None], Result]


def run(cmd: Sequence[str], cwd: str | None = None, timeout: float = 60.0) -> Result:
    """Run cmd; on timeout kill its whole process group."""
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            env={**os.environ, **QUIET},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as err:
        return Result(127, "", f"{cmd[0]}: {err.strerror}")
    with proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return Result(124, "", f"{cmd[0]} timed out after {timeout:g}s")
    return Result(proc.returncode, out, err)


def make_runner(timeout: float) -> Runner:
    def runner(cmd: Sequence[str], cwd: str | None = None) -> Result:
        return run(cmd, cwd, timeout)

    return runner
