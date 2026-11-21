"""Build fmap.bin from the per-fixture runs (build/iso.py and build/isofn.py on build/f/tests.json, with
ISO_OUT=build/f/iso and build/f/fn): the same kind of map as map.bin, its "tests" being the units of fx_meta.json.
usage: mkfmap.py OUT"""
import json, os, pickle, subprocess, sys, zlib
HERE = os.path.dirname(os.path.abspath(__file__)); out = sys.argv[1]
env = dict(os.environ, MAP_ISO=os.path.join(HERE, "iso"), MAP_FN=os.path.join(HERE, "fn"))
subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "mkmap.py"), os.path.join(HERE, "tests.json"), out + ".raw"], env=env, check=True)
m = pickle.loads(zlib.decompress(open(out + ".raw", "rb").read())); os.remove(out + ".raw")
n = len(json.load(open(os.path.join(HERE, "fx_meta.json")))["units"]); assert len(m["ids"]) == n, (len(m["ids"]), n)
m["ids"] = [f"fx::{k}" for k in range(n)]                # (the generated ids hold local paths)
blob = zlib.compress(pickle.dumps(m, protocol=5), 9); assert os.path.expanduser("~").encode() not in pickle.dumps(m)
open(out, "wb").write(blob); print("units", n, "bytes", len(blob), file=sys.stderr)
