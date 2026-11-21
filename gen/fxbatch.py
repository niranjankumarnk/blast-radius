"""Build evaluation batches of labelled patches whose static selection holds an optimizer or executor test.
usage: fxbatch.py OUTDIR SETDIR...   -> OUTDIR/b0, b1, ... (at most 60 patches each, like the hidden set)"""
import json, os, random, shutil, subprocess, sys, tempfile
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); out = sys.argv[1]
tests = json.load(open(os.path.join(here, "sample/tests.json")))["tests"]
pool = {t["number"] for t in tests if t["file"] in ("tests/test_optimizer.py", "tests/test_executor.py")}
picked = []
for sd in sys.argv[2:]:
    sd = os.path.abspath(sd); tag = os.path.basename(sd)
    lab = [f[:-5] for f in sorted(os.listdir(os.path.join(sd, "labels"))) if json.load(open(os.path.join(sd, "labels", f))).get("applied")]
    tmp = tempfile.mkdtemp(prefix="brfx-", dir=os.environ.get("BR_TMP")); os.mkdir(os.path.join(tmp, "commits"))
    for c in lab: shutil.copy(os.path.join(sd, c + ".patch"), os.path.join(tmp, "commits"))
    shutil.copy(os.path.join(here, "sample/tests.json"), tmp)
    env = dict(os.environ, BR_DATA=tmp, BR_SRC=os.path.join(here, "sqlglot-src"), BR_RUN="0", BR_EXACT="0", BR_INPROC="0")
    p = subprocess.run([os.path.join(here, ".venv/bin/python"), os.path.join(here, "selector.py")], env=env, capture_output=True); shutil.rmtree(tmp)
    kinds = json.load(open(os.path.join(sd, "kinds.json"))) if os.path.exists(os.path.join(sd, "kinds.json")) else {}
    for line in p.stdout.decode().splitlines():
        d = json.loads(line)
        if pool & {n for w in d["workers"] for n in w}: picked.append((sd, tag, d["commit"], kinds.get(d["commit"], "")))
random.Random(7).shuffle(picked)
for b in range(0, len(picked), 60):
    bd = os.path.join(out, f"b{b // 60}"); os.makedirs(os.path.join(bd, "labels"), exist_ok=True); kinds = {}
    for sd, tag, c, k in picked[b:b + 60]:
        name = f"{tag}-{c}"; kinds[name] = k
        shutil.copy(os.path.join(sd, c + ".patch"), os.path.join(bd, name + ".patch"))
        shutil.copy(os.path.join(sd, "labels", c + ".json"), os.path.join(bd, "labels", name + ".json"))
    json.dump(kinds, open(os.path.join(bd, "kinds.json"), "w"))
print(len(picked), "patches select optimizer or executor tests;", (len(picked) + 59) // 60, "batches", file=sys.stderr)
