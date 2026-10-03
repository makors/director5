import os
import signal
import string
import subprocess
import time
from pathlib import Path

import pytest

from orchestrator.api.docker.services import TEMPLATE_DIR


@pytest.mark.parametrize("exit_code", (0, 23))
def test_run_wrapper_preserves_process_exit_code(tmp_path: Path, exit_code: int):
    runfile = tmp_path / "run.sh"
    runfile.write_text(f"#!/bin/sh\necho 'site process started'\nexit {exit_code}\n")
    runfile.chmod(0o755)
    script = string.Template((TEMPLATE_DIR / "run-site.sh").read_text()).safe_substitute(
        SEARCH_PATH=str(runfile)
    )

    result = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=5, check=False
    )
    assert result.returncode == exit_code
    assert "site process started" in result.stdout
    assert "DIRECTOR: Stopped server" in result.stdout


def test_run_wrapper_waits_for_child_to_stop(tmp_path: Path):
    ready_file = tmp_path / "ready"
    runfile = tmp_path / "run.sh"
    runfile.write_text(
        "#!/bin/sh\n"
        "trap 'echo \"site process stopped\"; exit 0' TERM\n"
        f"touch '{ready_file}'\n"
        "while :; do sleep 0.05; done\n"
    )
    runfile.chmod(0o755)
    script = string.Template((TEMPLATE_DIR / "run-site.sh").read_text()).safe_substitute(
        SEARCH_PATH=str(runfile)
    )
    process = subprocess.Popen(
        ["sh", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_file.exists(), "Child process did not start"
        process.terminate()
        stdout, _stderr = process.communicate(timeout=3)
        assert process.returncode == 0
        assert "site process stopped" in stdout
        assert "DIRECTOR: Stopped server" in stdout
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
