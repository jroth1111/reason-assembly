from reason_assembly.demo import run_demo


def test_offline_demo_is_conservative():
    result = run_demo("decide", "Which option is safer?")
    assert result.manifest.calls_used == 0
    assert result.manifest.integrity_sha256
    assert result.verdict.finality == "verdict_commit"
    assert not result.verdict.calibrated
