"""How many TPC-DS queries does each patch that selects test_execute_tpcds affect, and what would replaying them cost?"""
import json, os, sys, pickle, zlib, collections
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.environ.update(BR_SRC=ROOT + '/sqlglot-src', BR_MAP=ROOT + '/map.bin'); sys.path.insert(0, ROOT)
import selector as S
tests = json.load(open(ROOT + '/sample/tests.json'))['tests']
sel = S.Selector(tests)
qm = pickle.loads(zlib.decompress(open(ROOT + '/build/q/qmap.bin', 'rb').read()))
qsel = S.Selector([{"id": i, "number": k} for k, i in enumerate(qm['ids'])], ROOT + '/build/q/qmap.bin')
exp = json.load(open(sys.argv[1])); cost = exp['cost']; H = len(qm['ids']) - 1
REAL = [t['number'] for t in tests if t['id'] == qm['real']][0]
def affected(text):
    b = qsel.select(text)
    if b == qsel.all or b >> H & 1: return None
    return [i for i in range(H) if b >> i & 1]
ans = json.load(open(ROOT + '/sample/answers.json'))['commits']
print('--- samples that select test_execute_tpcds', flush=True)
for c, a in sorted(ans.items()):
    text = open(f'{ROOT}/sample/commits/{c}.patch').read(); b = sel.select(text)
    if not b >> REAL & 1 or b == sel.all: continue
    Q = affected(text)
    print(c, 'truth: tpcds fails' if REAL in a['failing'] else 'truth: tpcds passes', '| cannot slice' if Q is None else f'| affected queries {len(Q)} of 99 (executed ones {sum(1 for q in Q if exp["tables"][q])}), replay cpu {sum(cost[q] for q in Q):.2f} s', flush=True)
for sd in sys.argv[2:]:
    hist = collections.Counter(); n = 0; cpu = []
    for f in sorted(os.listdir(f'{ROOT}/gen/{sd}')):
        if not f.endswith('.patch'): continue
        text = open(f'{ROOT}/gen/{sd}/{f}', encoding='utf-8').read(); b = sel.select(text)
        if not b >> REAL & 1 or b == sel.all: continue
        n += 1; Q = affected(text)
        if Q is None: hist['cannot slice'] += 1; continue
        cpu.append(sum(cost[q] for q in Q))
        hist['0 queries' if not Q else '1-5' if len(Q) <= 5 else '6-20' if len(Q) <= 20 else '21-50' if len(Q) <= 50 else '51-98' if len(Q) < 99 else 'all 99'] += 1
    cpu.sort()
    print(f'--- {sd}: {n} patches select test_execute_tpcds:', dict(hist))
    if cpu: print('    replay cpu of the sliceable ones, deciles 10/25/50/75/90:', [round(cpu[int(len(cpu) * p)], 2) for p in (0.1, 0.25, 0.5, 0.75, 0.9)], '| share of all with replay cpu <= 1/3 of the total:', round(sum(1 for x in cpu if x <= sum(cost) / 3) / max(1, n), 2), '| total replay cpu', round(sum(cost), 1))
