"""pytest plugin: line coverage with one context per test while the whole suite runs in one process.

Some lines are reached only then, through state that earlier tests leave behind (a warm cache, a registry that a
test has changed). Writes {"order": [test ids], "lines": {file: {line: [test ids]}}} to $SUITE_OUT; mkmap.py
unions it into the per-test map as build/suite.json.

    cd sqlglot-src && SKIP_INTEGRATION=0 SQLGLOT_ROOT=$PWD SUITE_OUT=../build/suite.json PYTHONPATH=$PWD:../build \
        ../.venv/bin/python -m pytest -p suitecov -q -p no:cacheprovider
"""
import json, os, coverage, pytest

ROOT = os.path.realpath(os.environ["SQLGLOT_ROOT"])
_cov = coverage.Coverage(data_file=None, include=[ROOT + "/sqlglot/*", ROOT + "/tests/*"], config_file=False)
_order = []
_cov.start()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _order.append(item.nodeid)
    _cov.switch_context(item.nodeid)
    try:
        return (yield)
    finally:
        _cov.switch_context("")


def pytest_sessionfinish(session):
    _cov.stop()
    data = _cov.get_data(); lines = {}
    for f in data.measured_files():
        rel = os.path.realpath(f)[len(ROOT) + 1:]
        lines[rel] = {str(l): sorted(c) for l, c in data.contexts_by_lineno(f).items()}
    with open(os.environ["SUITE_OUT"], "w") as fh:
        json.dump({"order": _order, "lines": lines}, fh)
