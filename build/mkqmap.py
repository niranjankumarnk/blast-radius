"""Build qmap.bin: the same kind of map as map.bin, but its "tests" are the 99 per-query replays of
test_execute_tpcds (tpcds_replay.py, build/q/) plus one pseudo-test, "harness".

"harness" stands for everything the real test executes that no replay does (its own body, the class set-up, the
process pool): every line, file read, function entry and class instantiation that map.bin records for the real
test and no replay shows. A patch that selects "harness" cannot be judged query by query.

usage: mkqmap.py MAP_BIN TESTS_JSON OUT      (after iso.py / isofn.py have run on build/q/tests.json)

build/q/tests.json lists one pytest id per query, {"tests": [{"number": i, "id": "<absolute path of
build/q/test_tpcds_replay.py>::test_q[i]", ...}]}; it holds local paths, so it is generated, not committed.
"""
import json, os, pickle, subprocess, sys, zlib
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
main_map, tests_json, out = sys.argv[1:4]
REAL = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
tmp = out + ".raw"
env = dict(os.environ, MAP_ISO=os.path.join(HERE, "q", "iso"), MAP_FN=os.path.join(HERE, "q", "fn"))
subprocess.run([sys.executable, os.path.join(HERE, "mkmap.py"), os.path.join(HERE, "q", "tests.json"), tmp], env=env, check=True)
q = pickle.loads(zlib.decompress(open(tmp, "rb").read())); os.remove(tmp)
m = pickle.loads(zlib.decompress(open(main_map, "rb").read()))
real = m["ids"].index(REAL); n = len(q["ids"]); hbit = 1 << n
mb = lambda i: int.from_bytes(m["sets"][i], "little"); qb = lambda i: int.from_bytes(q["sets"][i], "little")
width = (n + 1 + 7) // 8
sets, idx = [], {}
def sid(b):
    if b not in idx:
        idx[b] = len(sets); sets.append(b.to_bytes(width, "little"))
    return idx[b]
files = {}; added = 0
for f in set(m["files"]) | set(q["files"]):
    lines = {}
    if f in q["files"]:
        for l, i in zip(*q["files"][f]): lines[l] = qb(i)
    if f in m["files"]:
        for l, i in zip(*m["files"][f]):
            if mb(i) >> real & 1 and not lines.get(l): lines[l] = hbit; added += 1
    if lines:
        ks = sorted(lines); files[f] = (ks, [sid(lines[k]) for k in ks])
reads = {k: qb(i) for k, i in q["reads"].items()}
for k, i in m["reads"].items():
    if mb(i) >> real & 1 and not reads.get(k): reads[k] = hbit
fn = {f: {l: qb(i) for l, i in d.items()} for f, d in q["fn"].items()}
for f, d in m["fn"].items():
    for l, i in d.items():
        if mb(i) >> real & 1 and not fn.get(f, {}).get(l): fn.setdefault(f, {})[l] = hbit
inst = {c: qb(i) for c, i in q["inst"].items()}
for c, i in m["inst"].items():
    if mb(i) >> real & 1 and not inst.get(c): inst[c] = hbit
cost = []
for i in range(n):
    p = os.path.join(HERE, "q", "out", f"{i}.txt")
    cost.append(float(open(p).read().split()[1]) if os.path.exists(p) else 1.0)
blob = zlib.compress(pickle.dumps({"ids": [f"tpcds::q{i}" for i in range(n)] + ["harness"], "sets": sets, "files": files, "reads": {k: sid(v) for k, v in reads.items()},
                                   "subproc": 0, "fn": {f: {l: sid(b) for l, b in d.items()} for f, d in fn.items()},
                                   "inst": {c: sid(b) for c, b in inst.items()}, "fn_missing": q.get("fn_missing", 0), "cost": cost, "real": REAL}, protocol=5), 9)
open(out, "wb").write(blob)
print("replays", n, "lines only the real test reaches", added, "bytes", len(blob), "native cpu of all replays", round(sum(cost), 1), file=sys.stderr)
