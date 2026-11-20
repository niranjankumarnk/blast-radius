"""Blast Radius selector: map changed lines to the tests that execute them (per-test coverage of the pinned commit)."""
import ast, bisect, json, os, pickle, re, shutil, signal, sys, time, zlib
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", Path(__file__).with_name("map.bin")))
WORKER_MS, MAX_WORKERS = 2000, 8
ALL_TRIGGERS = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "setup.py")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """-> list of (old_path|None, new_path|None, hunks); hunk = (old_start, [(tag, line), ...])."""
    files, cur, lines, i = [], None, text.split("\n"), 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", l)
            cur = [m.group(1), m.group(2), []] if m else [None, None, []]
            files.append(cur)
        elif cur is not None and l.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            a, b = l[4:].split("\t")[0], lines[i + 1][4:].split("\t")[0]
            cur[0] = None if a == "/dev/null" else a[2:] if a[:2] in ("a/", "b/") else a
            cur[1] = None if b == "/dev/null" else b[2:] if b[:2] in ("a/", "b/") else b
            i += 1
        elif cur is not None and l.startswith(("deleted file mode", "rename from", "rename to", "new file mode", "old mode", "new mode", "GIT binary", "Binary files")):
            cur.append(l.split(" ")[0])
        elif cur is not None and (m := HUNK.match(l)):
            n_old = int(m.group(2) or 1); n_new = int(m.group(4) or 1)
            body = []; i += 1
            while i < len(lines) and (n_old > 0 or n_new > 0):
                h = lines[i]; tag = h[:1]
                if tag == "\\":
                    i += 1; continue
                if tag == "-": n_old -= 1
                elif tag == "+": n_new -= 1
                else: n_old -= 1; n_new -= 1; tag = " "
                body.append((tag, h[1:])); i += 1
            cur[2].append((int(m.group(1)), body))
            continue
        i += 1
    return files


def apply_hunks(old_lines, hunks):
    """-> (new_lines, removed old line numbers (1-based), insertion anchors as old line numbers)."""
    out, pos, removed, anchors = [], 0, set(), set()
    for start, body in hunks:
        s = max(start - 1, 0) if any(t != "+" for t, _ in body) else start
        out.extend(old_lines[pos:s]); pos = s
        for tag, text in body:
            if tag == " ":
                out.append(old_lines[pos] if pos < len(old_lines) else text); pos += 1
            elif tag == "-":
                removed.add(pos + 1); pos += 1
            else:
                out.append(text); anchors.add(pos)        # between old line pos and pos+1
    out.extend(old_lines[pos:])
    return out, removed, anchors


class Index:
    """Statement spans of the pinned source file."""
    def __init__(self, tree):
        self.stmts = []     # (lo, hi, header_hi, node, func, cls)   func = innermost enclosing def or None; cls = "A.B" or None
        self._walk(tree.body, None, None)
        self.stmts.sort(key=lambda s: (s[0], -s[1]))

    def _walk(self, body, func, cls):
        for n in body:
            lo = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
            hi = n.end_lineno
            inner = [b for f in ("body", "orelse", "finalbody", "handlers", "cases") for b in getattr(n, f, []) or []]
            hdr = min(b.lineno for b in inner) - 1 if inner else hi
            self.stmts.append((lo, hi, max(hdr, lo), n, func, cls))
            f2 = n if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            c2 = ((cls + "." if cls else "") + n.name) if isinstance(n, ast.ClassDef) else None if f2 is not func else cls
            for f in ("body", "orelse", "finalbody"):
                self._walk(getattr(n, f, []) or [], f2, c2)
            for h in getattr(n, "handlers", []) or []:
                self.stmts.append((h.lineno, h.end_lineno, h.body[0].lineno - 1 if h.body else h.end_lineno, h, func, cls))
                self._walk(h.body, f2, c2)
            for c in getattr(n, "cases", []) or []:
                self._walk(c.body, f2, c2)

    def innermost(self, line):
        best = None
        for s in self.stmts:
            if s[0] > line: break
            if s[1] >= line: best = s
        return best


FUNC = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_STUB = ast.Constant(value="<body>")


def func_nodes(tree):
    """Function-like nodes in a deterministic order (parents before children)."""
    out, stack = [], [tree]
    while stack:
        n = stack.pop()
        if isinstance(n, FUNC): out.append(n)
        stack.extend(reversed(list(ast.iter_child_nodes(n))))
    return out


def strip_bodies(tree):
    """Dump of the tree with every function-like body replaced, and the own-body dump of each function-like."""
    fns = func_nodes(tree); saved = [f.body for f in fns]
    for f in fns: f.body = _STUB if isinstance(f, ast.Lambda) else [ast.Expr(value=_STUB)]
    skeleton = ast.dump(tree)
    own = []
    for f, b in zip(reversed(fns), reversed(saved)):      # children first: restore, dump with nested bodies stubbed
        f.body = b
    for f in fns:
        inner = [g for g in func_nodes(f) if g is not f]; keep = [g.body for g in inner]
        for g in inner: g.body = _STUB if isinstance(g, ast.Lambda) else [ast.Expr(value=_STUB)]
        own.append(ast.dump(f.body) if isinstance(f, ast.Lambda) else "".join(ast.dump(x) for x in f.body))
        for g, b in zip(inner, keep): g.body = b
    return skeleton, fns, own


CORE_STEMS = {"dialect", "__init__"}
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


class Selector:
    def __init__(self, tests):
        m = pickle.loads(zlib.decompress(MAP.read_bytes()))
        self.sets, self.files, self.reads, self.subproc = m["sets"], m["files"], m["reads"], m["subproc"]
        self.fn, self.inst, self.fn_missing = m.get("fn", {}), m.get("inst", {}), m.get("fn_missing", 0)
        self._src = None
        num = {t["id"]: t["number"] for t in tests}
        self.remap = None if [num.get(i) for i in m["ids"]] == list(range(len(m["ids"]))) and len(num) == len(m["ids"]) else [num.get(i) for i in m["ids"]]
        self.unknown = [t["number"] for t in tests if t["id"] not in set(m["ids"])] if self.remap else []
        self.all = (1 << len(m["ids"])) - 1
        self.by_file = {}
        for i, tid in enumerate(m["ids"]):
            self.by_file[tid.split("::")[0]] = self.by_file.get(tid.split("::")[0], 0) | (1 << i)

    def bits(self, idx): return int.from_bytes(self.sets[idx], "little")

    def note(self, x):
        """Remember each changed spot's own test set (used to decide whether a heavy test has cheap witnesses)."""
        if x: self.parts.append(x)
        return x

    def cover(self, path, lo, hi):
        f = self.files.get(path)
        if not f: return 0
        ks, ix = f; r = 0
        for j in range(bisect.bisect_left(ks, lo), bisect.bisect_right(ks, hi)):
            r |= self.bits(ix[j])
        return r

    def fn_tests(self, path, line):
        i = self.fn.get(path, {}).get(line)
        return (self.bits(i) if i is not None else 0) | self.fn_missing

    def name_users(self, name, skip):
        """Tests that execute any line mentioning `name` (outside `skip` = {(path, line)}); None if nothing mentions it."""
        if self._src is None:
            self._src = {str(p.relative_to(SRC)): p.read_text(encoding="utf-8") for p in sorted((SRC / "sqlglot").rglob("*.py"))}
        pat = re.compile(r"\b" + re.escape(name) + r"\b"); r = 0; found = False
        define = re.compile(r"\s*" + re.escape(name) + r"\s*(:|=(?!=))")      # a (re)definition in some class body reads nothing
        for path, text in self._src.items():
            if name not in text: continue
            for i, line in enumerate(text.split("\n"), 1):
                if (path, i) in skip or not pat.search(line) or line.lstrip().startswith("#"): continue
                if define.match(line) and len(pat.findall(line)) == 1: continue
                found = True; r |= self.cover(path, i, i)
        return r if found else None

    def class_attr(self, path, node, cls, whole):
        """A changed assignment in a class body: only tests that build an instance of the class's family can see it."""
        parts = path[:-3].split("/"); mod = ".".join(parts); r = None
        if len(parts) == 3 and parts[1] == "expressions":
            i = self.inst.get(mod + ":" + cls)
            r = self.bits(i) if i is not None else None
        elif len(parts) == 3 and parts[1] in ("dialects", "parsers", "generators") and parts[2] not in CORE_STEMS:
            mods = {f"sqlglot.{d}.{parts[2]}" for d in ("dialects", "parsers", "generators")}; g = 0
            for k, i in self.inst.items():
                if k.split(":")[0] in mods: g |= self.bits(i)
            r = g or None
            if r is not None:
                names = [t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(t, ast.Name)]
                if names and all(NAME_RE.match(n) for n in names):
                    skip = {(path, l) for l in range(node.lineno, node.end_lineno + 1)}; u = 0
                    for n in names:
                        x = self.name_users(n, skip)
                        if x is None: u = None; break
                        u |= x
                    if u is not None: r &= u
        if r is None: return whole
        return (r | self.fn_missing) & self.all

    def file_tests(self, path):
        f = self.files.get(path); r = 0
        if f:
            for j in set(f[1]): r |= self.bits(j)
        return r

    def readers(self, path, parents=False):
        r = self.bits(self.reads[path]) if path in self.reads else 0
        d = os.path.dirname(path) if parents else ""
        while d:                       # tests that list a parent directory see new/removed/renamed files
            if d in self.reads: r |= self.bits(self.reads[d])
            d = os.path.dirname(d)
        return r

    def select(self, patch_text):
        self.parts = []
        r = self._select(patch_text)
        if r == self.all: self.parts = [r]
        return r

    def _select(self, patch_text):
        sel = 0
        for f in parse_patch(patch_text):
            old, new, hunks, flags = f[0], f[1], f[2], f[3:]
            for p in {old, new} - {None}:
                if os.path.basename(p) in ALL_TRIGGERS: return self.all
            if old is None or new is None or old != new or flags and not hunks:      # add / delete / rename / mode / binary
                for p in {old, new} - {None}:
                    sel |= self.note(self.readers(p, True) | self.file_tests(p))
                    if p.endswith(".py") and p.startswith("sqlglot/"): sel |= self.note(self.subproc)
                    if p.endswith("__init__.py") or (p.endswith(".py") and old != new and old and new): return self.all
                if old is None or new is None: continue
            path = old
            if not path.endswith(".py"):
                sel |= self.note(self.readers(path)); continue
            sel |= self.py_change(path, hunks) | self.note(self.readers(path))
        return sel

    def py_change(self, path, hunks):
        try:
            old_text = (SRC / path).read_text(encoding="utf-8")
        except OSError:
            return self.all
        old_lines = old_text.split("\n")
        new_lines, removed, anchors = apply_hunks(old_lines, hunks)
        try:
            old_tree = ast.parse(old_text)
        except SyntaxError:
            return self.all
        whole = self.file_tests(path) | (self.subproc if path.startswith("sqlglot/") else 0)
        if path.startswith("tests/") and path in self.by_file: whole |= self.by_file[path]
        try:
            new_tree = ast.parse("\n".join(new_lines))
        except (SyntaxError, ValueError):
            return self.note(whole)
        if ast.dump(old_tree) == ast.dump(new_tree):
            return 0
        idx = Index(old_tree); r = 0
        n = len(old_lines)
        sk_old, fns_old, own_old = strip_bodies(old_tree)
        sk_new, fns_new, own_new = strip_bodies(new_tree)
        bodies_only = sk_old == sk_new and len(fns_old) == len(fns_new)      # every change is inside a function-like body
        if bodies_only:
            for f, a, b in zip(fns_old, own_old, own_new):
                if a != b and isinstance(f, ast.Lambda):
                    r |= self.note(self.fn_tests(path, f.lineno))
        def code(i): return 1 <= i <= n and old_lines[i - 1].strip() and not old_lines[i - 1].strip().startswith("#")
        targets = set(removed)
        for a in anchors:                      # inserted lines: nearest code line on each side
            lo = a
            while lo >= 1 and not code(lo): lo -= 1
            hi = a + 1
            while hi <= n and not code(hi): hi += 1
            if lo >= 1: targets.add(lo)
            if hi <= n: targets.add(hi)
        for line in targets:
            s = idx.innermost(line)
            if s is None:
                r |= self.note(whole); continue          # module level outside any statement
            lo, hi, hdr, node, func, cls = s
            if bodies_only and func is None and not isinstance(node, FUNC):
                continue                       # import-time statement whose only changes are lambda bodies (handled above)
            if not bodies_only and func is None and cls and isinstance(node, (ast.Assign, ast.AnnAssign)):
                r |= self.note(self.class_attr(path, node, cls, whole)); continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and line <= hdr and not node.decorator_list:
                c = self.cover(path, hdr + 1, hi)          # signature change: whoever runs the body
            elif isinstance(node, ast.ClassDef) or func is None:
                c = self.cover(path, lo, hdr) or whole     # import-time code
            else:
                c = self.cover(path, lo, hdr)
            r |= self.note(c)
        if path.startswith("sqlglot/") and r: r |= self.note(self.subproc)
        return r


# ---- running candidate tests against the patched source -------------------------------------------------------
# The statically selected tests are a safe superset. When that superset is cheap to execute, run it on the patched
# tree inside the container and schedule only the tests that fail. CPU time is charged, so this is done only when
# the superset costs clearly less to run than to schedule. Any doubt (timeout, crash, odd patch) keeps the superset.

HEAVY_MS = int(os.environ.get("BR_HEAVY_MS", "5000"))
RUN = os.environ.get("BR_RUN", "1") == "1"
FORCE = os.environ.get("BR_FORCE", "0") == "1"
RUN_RATIO = float(os.environ.get("BR_RATIO", "0.6"))
MAX_EST = float(os.environ.get("BR_MAX_EST", "6"))      # a run that goes wrong is killed, and its CPU is still charged
HEAVY_RATIO = float(os.environ.get("BR_HEAVY_RATIO", "0.6"))
MIN_WITNESS = int(os.environ.get("BR_WITNESS", "3"))
HEAVY_DROP = os.environ.get("BR_HEAVY_DROP", "1") == "1"
WALL_BUDGET = float(os.environ.get("BR_WALL", "110"))
TMP = os.environ.get("BR_TMP", "/tmp")
POOL_FILES = ("tests/test_optimizer.py", "tests/test_executor.py")


def hunks_fit(old_lines, hunks):
    for start, body in hunks:
        pos = max(start - 1, 0) if any(t != "+" for t, _ in body) else start
        for tag, text in body:
            if tag == "+": continue
            if pos >= len(old_lines) or old_lines[pos] != text: return False
            pos += 1
    return True


def patch_edits(files):
    """{path: new text, or None when deleted} for a patch we can reproduce exactly; None when we cannot."""
    edits = {}
    for f in files:
        old, new, hunks, flags = f[0], f[1], f[2], f[3:]
        if "GIT" in flags or "Binary" in flags or (old is None and new is None): return None
        for p in (old, new):
            if p and (p.startswith("/") or ".." in p.split("/")): return None
        if old is not None:
            try:
                text = edits[old] if old in edits and edits[old] is not None else (SRC / old).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
            old_lines = text.split("\n")
        else:
            old_lines = []
        if not hunks_fit(old_lines, hunks): return None
        if new is None:
            edits[old] = None; continue
        new_lines = apply_hunks(old_lines, hunks)[0] if hunks else old_lines
        text = "\n".join(new_lines)
        if old is None and hunks and not text.endswith("\n"): text += "\n"
        if old is not None and old != new: edits[old] = None
        edits[new] = text
    return edits


def build_overlay(root, edits):
    """A copy of the pinned checkout made of symlinks, with the patched files written for real."""
    changed = set(edits)
    def walk(rel):
        src = SRC / rel if rel else SRC; dst = os.path.join(root, rel)
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            r = rel + "/" + name if rel else name
            if r in changed: continue
            if any(c.startswith(r + "/") for c in changed) and os.path.isdir(src / name): walk(r)
            else: os.symlink(src / name, os.path.join(dst, name))
    walk("")
    for p, text in edits.items():
        if text is None: continue
        os.makedirs(os.path.dirname(os.path.join(root, p)), exist_ok=True)
        with open(os.path.join(root, p), "w", encoding="utf-8", newline="") as fh: fh.write(text)


class _Recorder:
    def __init__(self): self.failed, self.seen, self.collect = set(), set(), []
    def pytest_runtest_logreport(self, report):
        self.seen.add(report.nodeid)
        if report.outcome == "failed": self.failed.add(report.nodeid)
    def pytest_collectreport(self, report):
        if report.outcome == "failed": self.collect.append(report.nodeid)


def _child(root, ids, out, per_test_timeout):
    try:
        os.setsid()
        nul = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2): os.dup2(nul, fd)
        os.chdir(root)
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path[:] = [root] + [p for p in sys.path if p not in ("", here, str(SRC))]
        # one pool worker: the tests' process pools would otherwise start a worker per host CPU, each paying an import
        os.environ.update(PYTHONPATH=root, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT="1")
        os.cpu_count = lambda: 1
        if hasattr(os, "process_cpu_count"): os.process_cpu_count = lambda: 1
        sys.dont_write_bytecode = True
        import pytest
        rec = _Recorder()
        code = pytest.main(["-q", "-p", "no:cacheprovider", "--timeout", str(per_test_timeout), *ids], plugins=[rec])
        with open(out + ".tmp", "w") as fh:
            json.dump({"code": int(code), "failed": sorted(rec.failed), "seen": sorted(rec.seen), "collect": rec.collect}, fh)
        os.rename(out + ".tmp", out)
    finally:
        os._exit(0)


def run_jobs(jobs, deadline):
    """jobs: [(key, edits, ids, est_seconds)] -> {key: set of failing ids}; a key is absent when the run gave no answer."""
    results, running, queue, seq = {}, {}, list(jobs), 0
    try:
        import pytest  # noqa: F401  (imported once here, inherited by every forked run)
    except Exception:
        return results
    def reap(pid, key, root, out, ok):
        try:
            if ok and os.path.exists(out):
                d = json.load(open(out)); ids = dict.fromkeys(running[pid][4])
                if d["code"] in (0, 1):
                    bad = set(d["failed"]) | (set(ids) - set(d["seen"]))
                    for c in d["collect"]: bad |= {i for i in ids if i.split("::")[0] == c or not c.endswith(".py")}
                    results[key] = (bad & set(ids), list(ids))
        except Exception:
            pass
        shutil.rmtree(root, ignore_errors=True)
        try: os.remove(out)
        except OSError: pass
    while queue or running:
        while queue and len(running) < 2 and time.time() < deadline:
            key, edits, ids, est = queue.pop(0)
            seq += 1
            root = os.path.join(TMP, f"br-{os.getpid()}-{seq}"); out = root + ".json"
            try:
                shutil.rmtree(root, ignore_errors=True); build_overlay(root, edits)
                sys.stdout.flush(); sys.stderr.flush()
                pid = os.fork()
            except Exception:
                shutil.rmtree(root, ignore_errors=True); continue
            if pid == 0: _child(root, ids, out, int(max(15, est * 4)))
            running[pid] = (key, root, out, time.time() + max(10, est * 3 + 5), ids)
        if not running:
            if time.time() >= deadline: break
            continue
        try:
            pid, status, ru = os.wait4(-1, os.WNOHANG)
            if pid in running: print(f"{running[pid][0]}: run cpu {ru.ru_utime + ru.ru_stime:.2f}s", file=sys.stderr)
        except ChildProcessError:
            pid = 0
            for p in list(running): reap(p, *running[p][:3], False); del running[p]
        if pid and pid in running:
            reap(pid, *running[pid][:3], os.WIFEXITED(status)); del running[pid]
            try: os.killpg(pid, signal.SIGKILL)       # stray pool workers
            except OSError: pass
        elif not pid:
            now = time.time()
            for p in list(running):
                if now > running[p][3] or now > deadline + 30:
                    try: os.killpg(p, signal.SIGKILL)
                    except OSError: pass
                    try: os.waitpid(p, 0)
                    except OSError: pass
                    reap(p, *running[p][:3], False); del running[p]
            time.sleep(0.01)
    return results


def pack_cost(numbers, ms):
    if not numbers: return 0
    ws = pack(numbers, ms)
    return max(sum(ms[n] for n in w) for w in ws) + WORKER_MS * len(ws)


def pack(numbers, ms):
    if not numbers: return []
    order = sorted(numbers, key=lambda n: -ms[n]); best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads, ws = [0] * k, [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads)); ws[j].append(n); loads[j] += ms[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]: best = (cost, ws)
    return best[1]


def main():
    t0 = time.time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    tid = {t["number"]: t["id"] for t in tests}; tnum = {t["id"]: t["number"] for t in tests}
    s = Selector(tests); chosen, jobs, parts = {}, [], {}
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="surrogateescape")
        try:
            b = s.select(text)
        except Exception as e:                                   # never miss because of a bug: run everything
            print(f"{patch.stem}: {type(e).__name__}: {e}", file=sys.stderr); b = s.all
        nums = [i for i in range(b.bit_length()) if b >> i & 1]
        if s.remap: nums = [s.remap[i] for i in nums if s.remap[i] is not None] + (s.unknown if nums else [])
        chosen[patch.stem] = nums
        parts[patch.stem] = list(getattr(s, "parts", []))
        if not RUN or not nums or b == s.all: continue
        try:
            files = parse_patch(text)
            if not any((p or "").endswith(".py") for f in files for p in f[:2]): continue      # data files: schedule as selected
            # light = every selected test short enough to run here; heavy ones are decided from the light results (see below)
            light = [n for n in nums if ms[n] < HEAVY_MS]
            if not light: continue
            lset = set(light); rest = [n for n in nums if n not in lset]
            heavy = [n for n in rest if ms[n] >= HEAVY_MS]
            after = [n for n in rest if ms[n] < HEAVY_MS] if HEAVY_DROP else rest
            pool = sum(ms[n] for n in light if tid[n].split("::")[0] in POOL_FILES) / 1000
            est = 0.5 + 1.0 * (sum(ms[n] for n in light) / 1000 - pool) + (1.0 + 2.0 * pool if pool else 0)
            saving = (pack_cost(nums, ms) - pack_cost(after, ms)) / 1000
            print(f"{patch.stem}: plan light={len(light)} rest={len(rest)} heavy={len(heavy)} est={est:.1f} saving={saving:.1f}", file=sys.stderr)
            if not FORCE and (est > MAX_EST or est > (HEAVY_RATIO if heavy and HEAVY_DROP else RUN_RATIO) * saving): continue
            edits = patch_edits(files)
            if edits is None: continue
            jobs.append((patch.stem, edits, [tid[n] for n in light], est, saving - est))
        except Exception as e:
            print(f"{patch.stem}: plan {type(e).__name__}: {e}", file=sys.stderr)
    inv = {n: i for i, n in enumerate(s.remap)} if s.remap else {t["number"]: t["number"] for t in tests}
    jobs.sort(key=lambda j: -j[4])
    try:
        results = run_jobs([j[:4] for j in jobs], t0 + WALL_BUDGET) if jobs else {}
    except Exception as e:
        print(f"runner {type(e).__name__}: {e}", file=sys.stderr); results = {}
    out = []
    for cid in sorted(chosen):
        nums = chosen[cid]
        if cid in results:
            ran = set(results[cid][1]); bad = sorted(tnum[i] for i in results[cid][0])
            rest = [n for n in nums if tid[n] not in ran]
            # A heavy test (tens of seconds) is kept unless every light test that shares the changed code passed.
            def witnessed(n):          # every changed spot that selected n was also exercised by enough of the tests we ran
                rb = sum(1 << (inv[tnum[i]]) for i in ran)
                return all(bin(p & rb).count("1") >= MIN_WITNESS for p in parts[cid] if p >> inv[n] & 1)
            keep = bad + [n for n in rest if ms[n] < HEAVY_MS or bad or not HEAVY_DROP or not witnessed(n)]
            print(f"{cid}: ran {len(ran)} of {len(nums)} selected, {len(bad)} failed, scheduling {len(keep)}", file=sys.stderr)
            nums = keep
        else:
            print(f"{cid}: {len(nums)} tests", file=sys.stderr)
        out.append(json.dumps({"commit": cid, "workers": pack(nums, ms)}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
