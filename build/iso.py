"""Run every test in its own process with coverage; one JSON per test in build/iso/."""
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.realpath(sys.argv[1]); TESTS = sys.argv[2]
OUT = os.path.join(HERE, "iso"); os.makedirs(OUT, exist_ok=True)
tests = json.load(open(TESTS))["tests"]
def run(t):
    out = os.path.join(OUT, f"{t['number']}.json")
    if os.path.exists(out): return 0
    env = dict(os.environ, SKIP_INTEGRATION="0", SQLGLOT_ROOT=ROOT, MAP_OUT=out + ".tmp", PYTHONPATH=ROOT + ":" + HERE,
               PATH=os.path.dirname(sys.executable) + ":" + os.environ["PATH"], PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "covplugin", "-q", "-p", "no:cacheprovider", "--timeout", "600", t["id"]],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out + ".tmp"):
        sys.stderr.write(f"FAIL {t['id']} rc={r.returncode}\n{r.stdout[-400:]}\n"); return 1
    os.rename(out + ".tmp", out); return 0
with ThreadPoolExecutor(int(sys.argv[3]) if len(sys.argv) > 3 else 8) as ex:
    print("failures", sum(ex.map(run, tests)), "of", len(tests))
