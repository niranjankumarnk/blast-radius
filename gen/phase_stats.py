"""Would a compile-only check decide test_execute_tpcds? Lines that run inside PythonExecutor.execute (the data
phase) versus lines that only run before it (parse, optimize, plan, transpile), measured on the pinned commit, and
how many tpcds-selecting patches touch only the latter.

usage: phase_stats.py SETDIR...
"""
import json, os, pickle, sys, zlib
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); SRC = os.path.join(ROOT, "sqlglot-src")
cache = os.path.join(HERE, "phase_lines.json")
if not os.path.exists(cache):
    import subprocess
    code = r'''
import json, os, sys, coverage
root = sys.argv[1]; sys.path.insert(0, root); sys.path.insert(1, sys.argv[2])
cov = coverage.Coverage(data_file=None, include=[root + "/sqlglot/*"], config_file=False); cov.start(); cov.switch_context("C")
from tpcds_replay import Replay
from sqlglot.executor.python import PythonExecutor
orig = PythonExecutor.execute; depth = [0]
def execute(self, plan, outer_scope=None):
    depth[0] += 1; cov.switch_context("E")
    try: return orig(self, plan, outer_scope)
    finally:
        depth[0] -= 1
        if not depth[0]: cov.switch_context("C")
PythonExecutor.execute = execute
r = Replay()
for i in range(len(r.sqls)): r.run(i)
cov.stop(); d = cov.get_data(); out = {"E": {}, "C": {}}
for f in d.measured_files():
    rel = os.path.realpath(f)[len(os.path.realpath(root)) + 1:]
    for l, ctxs in d.contexts_by_lineno(f).items():
        for c in ctxs:
            if c in out: out[c].setdefault(rel, []).append(l)
print(json.dumps(out))
'''
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1"); env.pop("PYTHONPATH", None)
    p = subprocess.run([os.path.join(ROOT, ".venv/bin/python"), "-c", code, SRC, ROOT], capture_output=True, text=True, env=env)
    open(cache, "w").write(p.stdout.strip().splitlines()[-1])
ph = json.load(open(cache)); E = {f: set(ls) for f, ls in ph["E"].items()}; C = {f: set(ls) for f, ls in ph["C"].items()}
print("lines run in the data phase:", sum(len(v) for v in E.values()), "in", len(E), "files; lines run only before it:", sum(len(C[f] - E.get(f, set())) for f in C))
print("data-phase lines by file:", sorted(((len(v), f.replace("sqlglot/", "")) for f, v in E.items()), reverse=True)[:14])
os.environ.update(BR_SRC=SRC, BR_MAP=os.path.join(ROOT, "map.bin")); sys.path.insert(0, ROOT)
import selector as S
tests = json.load(open(os.path.join(ROOT, "sample/tests.json")))["tests"]; sel = S.Selector(tests)
REAL = next(t["number"] for t in tests if t["id"].endswith("test_execute_tpcds"))
def touched(text):
    """Old line numbers a patch removes or inserts next to, per file (None if it is not a plain edit of sqlglot/*.py)."""
    out = {}
    for f in S.parse_patch(text):
        old, new, hunks = f[0], f[1], f[2]
        if old != new or old is None or not old.endswith(".py") or not old.startswith("sqlglot/"): return None
        ol = open(os.path.join(SRC, old), encoding="utf-8").read().split("\n")
        if not S.hunks_fit(ol, hunks): return None
        _, removed, anchors = S.apply_hunks(ol, hunks)
        out[old] = set(removed) | set(anchors) | {a + 1 for a in anchors}
    return out
def classify(text):
    t = touched(text)
    if t is None: return "other"
    import ast
    for f, lines in t.items():
        tree = ast.parse(open(os.path.join(SRC, f), encoding="utf-8").read())
        # be conservative: a touched function counts as data-phase code if any of its lines runs in the data phase
        spans = [(n.lineno, n.end_lineno) for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for l in lines:
            fn = [s for s in spans if s[0] <= l <= s[1]]
            rng = range(min(s[0] for s in fn), max(s[1] for s in fn) + 1) if fn else [l]
            if any(x in E.get(f, ()) for x in rng): return "touches the data phase"
    return "compile phase only"
import collections
ans = json.load(open(os.path.join(ROOT, "sample/answers.json")))["commits"]
for c in ("2haz", "2jrq", "3utz", "66ks"): print(c, classify(open(os.path.join(ROOT, "sample/commits", c + ".patch")).read()), "| tpcds fails" if REAL in ans[c]["failing"] else "| tpcds passes")
for sd in sys.argv[1:]:
    tab = collections.Counter()
    for f in sorted(os.listdir(os.path.join(ROOT, sd))):
        if not f.endswith(".patch"): continue
        text = open(os.path.join(ROOT, sd, f), encoding="utf-8").read(); b = sel.select(text)
        if b == sel.all or not b >> REAL & 1: continue
        tab[classify(text)] += 1
    print(sd, dict(tab))
