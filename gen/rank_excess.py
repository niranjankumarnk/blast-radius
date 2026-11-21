"""Rank tests by the seconds they add across labelled patches where they are scheduled but do not fail.
usage: rank_excess.py SETDIR... (env BR_TMP)"""
import collections, json, os, shutil, subprocess, sys, tempfile
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tests = json.load(open(os.path.join(here, "sample/tests.json")))["tests"]
sec = collections.Counter(); cnt = collections.Counter(); fail = collections.Counter(); total = 0
for sd in sys.argv[1:]:
    sd = os.path.abspath(sd); lab = {}
    for f in sorted(os.listdir(os.path.join(sd, "labels"))):
        d = json.load(open(os.path.join(sd, "labels", f)))
        if d.get("applied"): lab[f[:-5]] = set(d["failing"])
    tmp = tempfile.mkdtemp(prefix="brrank-", dir=os.environ.get("BR_TMP")); os.mkdir(os.path.join(tmp, "commits"))
    for c in lab: shutil.copy(os.path.join(sd, c + ".patch"), os.path.join(tmp, "commits"))
    shutil.copy(os.path.join(here, "sample/tests.json"), tmp)
    env = dict(os.environ, BR_DATA=tmp, BR_SRC=os.path.join(here, "sqlglot-src"), BR_RUN="0", PATH=os.path.join(here, ".venv/bin") + ":" + os.environ["PATH"])
    p = subprocess.run([os.path.join(here, ".venv/bin/python"), os.path.join(here, "selector.py")], env=env, capture_output=True); shutil.rmtree(tmp)
    for line in p.stdout.decode().splitlines():
        d = json.loads(line); total += 1
        for w in d["workers"]:
            for n in w:
                if n in lab[d["commit"]]: fail[n] += 1
                else: sec[n] += tests[n]["seconds"]; cnt[n] += 1
print(total, "patches; excess seconds by file:")
byf = collections.Counter()
for n, s in sec.items(): byf[tests[n]["file"]] += s
for f, s in byf.most_common(8): print(f"  {s:8.1f}  {f}")
print("top tests (excess seconds, times scheduled without failing, times failing, recorded seconds):")
for n, s in sec.most_common(30): print(f"  {s:8.1f} {cnt[n]:4d} {fail[n]:4d} {tests[n]['seconds']:6.2f}  {tests[n]['id']}")
