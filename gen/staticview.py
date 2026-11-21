"""Static selection size per generated patch, grouped by kind (no labels needed)."""
import json, os, subprocess, sys, tempfile, shutil, collections
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sd = os.path.abspath(sys.argv[1])
kinds = json.load(open(os.path.join(sd, "kinds.json")))
tmp = tempfile.mkdtemp(); os.symlink(sd, os.path.join(tmp, "commits")); shutil.copy(os.path.join(here, "sample/tests.json"), tmp)
p = subprocess.run([os.path.join(here, ".venv/bin/python"), os.path.join(here, "selector.py")], env=dict(os.environ, BR_DATA=tmp, BR_SRC=os.path.join(here, "sqlglot-src"), BR_RUN="0"), capture_output=True, text=True)
shutil.rmtree(tmp)
if p.returncode: print(p.stderr[-2000:]); sys.exit(1)
g = collections.defaultdict(list)
for line in p.stdout.splitlines():
    d = json.loads(line); n = sum(len(w) for w in d["workers"]); k = kinds[d["commit"]]
    key = k.split(":")[0] + ":" + (k.split(":", 1)[1].split("+")[0] if k.startswith(("py", "test_py")) else "")
    g[key].append((n, d["commit"], k))
for key in sorted(g):
    v = sorted(g[key]); print(f"{key:28} n={len(v):3} zero={sum(1 for x in v if x[0]==0):3} all={sum(1 for x in v if x[0]>=1620):3} median={v[len(v)//2][0]:5}  zeros: {' '.join(x[1] for x in v if x[0]==0)[:80]}")
