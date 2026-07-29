import io
import json

from artifacts import SecretGuard
from reason_assembly.observability import ProgressEmitter


def test_json_progress_is_parseable_and_redacted():
    stream = io.StringIO()
    emitter = ProgressEmitter(
        json_output=True,
        stream=stream,
        guard=SecretGuard({"example-secret-value"}),
    )
    emitter.stage("judge", "started", run_id="run-1", detail="example-secret-value")
    payload = json.loads(stream.getvalue())
    assert payload["stage"] == "judge"
    assert payload["run_id"] == "run-1"
    assert "example-secret-value" not in stream.getvalue()


def test_cli_exposes_structured_progress_flags():
    from reason_assembly.reason_assembly import parser

    args = parser().parse_args(["--json-progress", "--log-level", "INFO", "decide", "x"])
    assert args.json_progress
    assert args.log_level == "INFO"
