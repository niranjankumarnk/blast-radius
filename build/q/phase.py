"""For every TPC-DS query replay, the sqlglot lines that run inside PythonExecutor.execute (the data phase), each
query in its own process. Writes build/q/phase/<i>.json = {file: [lines]}.   usage: phase.py SRC [JOBS]"""
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(HERE)); SRC = os.path.realpath(sys.argv[1])
OUT = os.path.join(HERE, "phase"); os.makedirs(OUT, exist_ok=True)
CODE = r'''
import json, os, sys, coverage
root, i, out = sys.argv[1], int(sys.argv[3]), sys.argv[4]; sys.path.insert(0, root); sys.path.insert(1, sys.argv[2])
cov = coverage.Coverage(data_file=None, include=[root + "/sqlglot/*", root + "/tests/*"], config_file=False); cov.start(); cov.switch_context("C")
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
Replay().run(i)
cov.stop(); d = cov.get_data(); res = {}
for f in d.measured_files():
    rel = os.path.realpath(f)[len(os.path.realpath(root)) + 1:]
    ls = sorted(l for l, ctxs in d.contexts_by_lineno(f).items() if "E" in ctxs)
    if ls: res[rel] = ls
json.dump(res, open(out + ".tmp", "w")); os.rename(out + ".tmp", out)
'''
def run(i):
    out = os.path.join(OUT, f"{i}.json")
    if os.path.exists(out): return 0
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1"); env.pop("PYTHONPATH", None)
    r = subprocess.run([sys.executable, "-c", CODE, SRC, ROOT, str(i), out], capture_output=True, text=True, env=env)
    return 0 if os.path.exists(out) else 1
n = len(json.load(open(os.path.join(HERE, "tests.json")))["tests"])
with ThreadPoolExecutor(int(sys.argv[2]) if len(sys.argv) > 2 else 2) as ex: print("failures", sum(ex.map(run, range(n))), "of", n)
