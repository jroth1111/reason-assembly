import os

from reason_assembly.workers import SubprocessBackend


def test_subprocess_backend(tmp_path):
    result = SubprocessBackend(["/bin/echo"]).execute(
        "hello", tmp_path, os.environ, 5
    )
    assert result.returncode == 0
    assert result.output.strip() == "hello"
    assert not result.timed_out
