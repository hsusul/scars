import re

from cli_helpers import make_ctx, run


def test_bench_reports_sequential_and_concurrent_percentiles(tmp_path):
    code, out, err = run(make_ctx(tmp_path), "bench", "--runs", "3", "--lessons", "5", "--concurrency", "2")
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[1] == "lessons: 5  runs per case: 3"
    labels = [re.sub(r"\s+p50.*", "", line) for line in lines[2:]]
    assert labels == [
        "sequential, no match",
        "sequential, match",
        "2 concurrent, no match",
        "2 concurrent, match",
    ]
    for line in lines[2:]:
        assert re.search(r"p50 +\d+\.\d ms  p95 +\d+\.\d ms  p99 +\d+\.\d ms$", line)


def test_bench_rejects_a_zero_count(tmp_path):
    assert run(make_ctx(tmp_path), "bench", "--runs", "0") == (
        1,
        "",
        "scars bench: --runs, --lessons and --concurrency must be at least 1\n",
    )
