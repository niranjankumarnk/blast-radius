"""Trace child processes (multiprocessing spawn workers, subprocess python) of an isolated test run."""
import os
if os.environ.get("ISO_FN_CHILD") and os.environ.get("ISO_CHILD_DIR"):
    import fnplugin  # noqa: F401
elif os.environ.get("ISO_IS_CHILD") and os.environ.get("ISO_CHILD_DIR"):
    import atexit, json, coverage
    _root = os.path.realpath(os.environ["SQLGLOT_ROOT"])
    _cov = coverage.Coverage(data_file=None, include=[_root + "/sqlglot/*", _root + "/tests/*"], config_file=False)
    _cov.start()
    _done = []
    def _dump():
        if _done: return
        _done.append(1)
        _cov.stop()
        d = _cov.get_data()
        out = {os.path.realpath(f)[len(_root) + 1:]: sorted(d.lines(f) or []) for f in d.measured_files()}
        p = os.path.join(os.environ["ISO_CHILD_DIR"], f"{os.getpid()}.json")
        with open(p + ".tmp", "w") as fh: json.dump(out, fh)
        os.rename(p + ".tmp", p)
    atexit.register(_dump)
    try:
        from multiprocessing import util as _u
        _u.Finalize(None, _dump, exitpriority=0)
    except Exception:
        pass
