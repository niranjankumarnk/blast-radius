"""pytest plugin (also used by child processes through sitecustomize): which function-like code objects
each test's process tree enters, and which sqlglot classes it instantiates (with all their base classes).
Class and module bodies are excluded, so a lambda inside a class-level dict counts only when called."""
import atexit, glob, json, os, sys, tempfile
ROOT = os.path.realpath(os.environ["SQLGLOT_ROOT"]) + "/"
_seen, _inst = set(), set()
mon = sys.monitoring; TOOL = 3

def start_tracking():
    mon.use_tool_id(TOOL, "fnmap")
    def _start(code, offset):
        if code.co_flags & 0x2 and code.co_filename.startswith(ROOT):      # CO_NEWLOCALS: function-like
            _seen.add((code.co_filename, code.co_firstlineno))
        return mon.DISABLE
    mon.register_callback(TOOL, mon.events.PY_START, _start)
    mon.set_events(TOOL, mon.events.PY_START)

def wrap_classes():
    from sqlglot import parser, generator, tokens
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    def _wrap(cls):
        orig = cls.__init__
        def __init__(self, *a, **k):
            _inst.add(type(self))
            return orig(self, *a, **k)
        cls.__init__ = __init__
    for c in (Dialect, parser.Parser, generator.Generator, tokens.Tokenizer, Expr):
        _wrap(c)

def snapshot():
    fn = {}
    for f, l in _seen:
        fn.setdefault(os.path.realpath(f)[len(ROOT):], set()).add(l)
    classes = set()
    for t in _inst:
        for b in t.__mro__:
            if b.__module__.startswith("sqlglot"):
                classes.add(b.__module__ + ":" + b.__qualname__)
    return {"fn": {k: sorted(v) for k, v in fn.items() if k.startswith(("sqlglot/", "tests/"))}, "inst": sorted(classes)}

if os.environ.get("ISO_FN_CHILD"):          # imported from sitecustomize in a child process
    start_tracking(); wrap_classes()
    _done = []
    def _dump():
        if _done: return
        _done.append(1)
        p = os.path.join(os.environ["ISO_CHILD_DIR"], f"{os.getpid()}.json")
        with open(p + ".tmp", "w") as fh: json.dump(snapshot(), fh)
        os.rename(p + ".tmp", p)
    atexit.register(_dump)
    from multiprocessing import util as _u
    _u.Finalize(None, _dump, exitpriority=0)
else:
    OUT = os.environ["MAP_OUT"]
    _child_dir = tempfile.mkdtemp(prefix="isofn")
    os.environ["ISO_CHILD_DIR"] = _child_dir
    os.environ["ISO_FN_CHILD"] = "1"
    start_tracking()

    def pytest_configure(config):
        wrap_classes()

    def pytest_sessionfinish(session):
        mon.set_events(TOOL, 0)
        d = snapshot(); fn = {k: set(v) for k, v in d["fn"].items()}; inst = set(d["inst"]); n = 0
        for cf in glob.glob(os.path.join(_child_dir, "*.json")):
            c = json.load(open(cf)); n += 1
            for k, v in c["fn"].items(): fn.setdefault(k, set()).update(v)
            inst.update(c["inst"])
        json.dump({"fn": {k: sorted(v) for k, v in fn.items()}, "inst": sorted(inst), "children": n}, open(OUT, "w"))
