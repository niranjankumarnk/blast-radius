"""Record per-test outcome (failed if any report, subtest or phase, failed) and collection errors."""
import json, os
_bad, _seen, _coll = set(), set(), []
def pytest_runtest_logreport(report):
    _seen.add(report.nodeid)
    if report.outcome == "failed": _bad.add(report.nodeid)
def pytest_collectreport(report):
    if report.outcome == "failed": _coll.append(report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    p = os.environ["OUTCOME_OUT"]
    with open(p + ".tmp", "w") as f: json.dump({"failed": sorted(_bad), "seen": len(_seen), "collect_errors": _coll, "exit": int(exitstatus)}, f)
    os.rename(p + ".tmp", p)
