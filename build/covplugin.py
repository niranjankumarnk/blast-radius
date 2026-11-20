"""pytest plugin: per-test line coverage + per-test non-.py file reads. Writes raw map JSON."""
import builtins, io, json, os, coverage, pytest

ROOT = os.path.realpath(os.environ["SQLGLOT_ROOT"])
OUT = os.environ["MAP_OUT"]
_cov = coverage.Coverage(data_file=None, include=[ROOT + "/sqlglot/*", ROOT + "/tests/*"], config_file=False)
_cur = [""]
_reads = {}
_order = []
_real_open = builtins.open

def _note(path):
    try:
        if isinstance(path, int):
            return
        p = os.path.realpath(os.fspath(path))
    except Exception:
        return
    if p.startswith(ROOT + "/") and not p.endswith(".py") and "__pycache__" not in p and "/.git" not in p:
        _reads.setdefault(p[len(ROOT) + 1:], set()).add(_cur[0])

def _open(file, *a, **k):
    _note(file)
    return _real_open(file, *a, **k)

builtins.open = _open
io.open = _open
_real_listdir = os.listdir
def _listdir(p="."):
    _note(p)
    return _real_listdir(p)
os.listdir = _listdir
_real_scandir = os.scandir
def _scandir(p="."):
    _note(p)
    return _real_scandir(p)
os.scandir = _scandir

_cov.start()
import tempfile, glob
_child_dir = tempfile.mkdtemp(prefix="isochild")
os.environ["ISO_CHILD_DIR"] = _child_dir
os.environ["ISO_IS_CHILD"] = "1"
_inst = {}   # "module:Class" -> set(test)
_subproc = set()
import subprocess
_real_popen_init = subprocess.Popen.__init__
def _popen_init(self, *a, **k):
    _subproc.add(_cur[0])
    return _real_popen_init(self, *a, **k)
subprocess.Popen.__init__ = _popen_init

def _wrap(cls):
    orig = cls.__init__
    def __init__(self, *a, **k):
        t = type(self)
        _inst.setdefault(t.__module__ + ":" + t.__qualname__, set()).add(_cur[0])
        return orig(self, *a, **k)
    cls.__init__ = __init__

def pytest_configure(config):
    import sqlglot
    from sqlglot import parser, generator, tokens
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    for c in (Dialect, parser.Parser, generator.Generator, tokens.Tokenizer, Expr):
        _wrap(c)

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _cur[0] = item.nodeid
    _order.append(item.nodeid)
    _cov.switch_context(item.nodeid)
    try:
        return (yield)
    finally:
        _cur[0] = ""
        _cov.switch_context("")

def pytest_sessionfinish(session):
    _cov.stop()
    data = _cov.get_data()
    pre, run = {}, {}
    for f in data.measured_files():
        rel = os.path.realpath(f)[len(ROOT) + 1:]
        a, b = [], []
        for l, c in data.contexts_by_lineno(f).items():
            (b if any(x for x in c) else a).append(l)   # b: hit during a test; a: import/collection only
        if a: pre[rel] = sorted(a)
        if b: run[rel] = sorted(b)
    nchild = 0
    for cf in glob.glob(os.path.join(_child_dir, "*.json")):
        nchild += 1
        for rel, ls in json.load(_real_open(cf)).items():
            run[rel] = sorted(set(run.get(rel, [])) | set(ls))
    for rel in list(pre):
        pre[rel] = [l for l in pre[rel] if l not in set(run.get(rel, []))]
    json.dump({"order": _order, "pre": pre, "run": run, "children": nchild,
               "reads": sorted(_reads), "inst": sorted(_inst), "subproc": bool(_subproc)}, _real_open(OUT, "w"))
