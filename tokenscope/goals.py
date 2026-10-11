"""Goals: what the user was trying to get done, discovered from the data instead of a fixed keyword taxonomy.

Every request (one real user prompt) gets a signature made of feature groups:

  text          prompt words (lowercased, camelCase / snake_case / paths / dots split, stopwords and generic task
                words dropped, light suffix stemming; numbers dropped unless part of an identifier like SAVE10)
  files_edited  Edit / Write / MultiEdit / NotebookEdit targets (repo-relative)
  path          tokens of the touched paths (basename without extension + directory names, e.g. "inventory";
                "docs" for documentation files); edited_path = the same for edited files only (areas only)
  files_read    Read targets, Grep / Glob paths, files named by `cat` / `sed -n` / `head` / ... in Bash
  commands      normalized Bash kinds and test runners
  chain         bigrams of the normalized tool chain ("Edit>Hook", "Bash(./run_tests.sh)>Edit")

The outcome is kept for evidence but never used for similarity, so one goal is not split into its successes and
its failures. Setup overhead is ignored: files read by more than OVERHEAD_SHARE of all requests and the agent's own
configuration (CLAUDE.md, .claude/**, docs/claude/**), plus paths outside the repo.

Each group is TF-IDF weighted (idf over requests) and L2-normalized; the groups are combined with GOAL_WEIGHTS and
compared by cosine. Goals = average-linkage agglomerative clustering at GOAL_THRESHOLD. Areas = a second pass over
the goal centroids with AREA_WEIGHTS (files and paths emphasised) at AREA_THRESHOLD. Names come from the most
distinctive terms + top file, or from one batched Claude call (`--name-with-claude`, cached).
"""
import hashlib
import json
import math
import os
import re
from collections import Counter, OrderedDict
from datetime import datetime, timezone

GOAL_WEIGHTS = OrderedDict([("text", 1.5), ("files_edited", 1.0), ("path", 0.6), ("files_read", 0.4),
                            ("commands", 0.2), ("chain", 0.2)])
AREA_WEIGHTS = OrderedDict([("text", 0.5), ("files_edited", 1.0), ("path", 1.0), ("files_read", 0.6),
                            ("edited_path", 1.0)])
GROUPS = list(GOAL_WEIGHTS) + ["edited_path"]   # edited_path (path tokens of edited files) is an area-only signal
GOAL_THRESHOLD = 0.52       # average-linkage cosine at which requests merge into one goal (tuned on shopcart v2/v3)
AREA_THRESHOLD = 0.16       # average-linkage cosine at which goal centroids merge into one area (tuned on v2/v3)
OVERHEAD_SHARE = 0.8        # files read by more than this share of requests are setup overhead...
OVERHEAD_MIN_REQUESTS = 5   # ...once there are at least this many requests
TEXT_CHARS = 2000           # prompt characters used for text features
NAME_TERMS = 3              # distinctive terms in a fallback goal name
WHY_FEATURES = 6            # shared features listed under "why grouped"
MEMBERS_SHOWN = 300         # prompt characters kept per member in the evidence
ENGINE_EDITS = ("Edit", "MultiEdit", "Write", "NotebookEdit")

GROUP_LABEL = {"text": "text", "files_edited": "file", "path": "path", "files_read": "read", "commands": "cmd",
               "chain": "chain", "edited_path": "edited-path"}

STOPWORDS = frozenset("""
a an the and or but if then else of to in on at by for with from into onto as is are was were be been being it its
this that these those there here i you we they he she me my our your their them us do does did done not no nor so
than too very can could would will shall may might must just only any all some each every both either neither such
what which who whom whose when where why how about above after again against before below between during over under
up down out off own same other more most few further once while until because through via per like etc one two
""".split())

# Generic task words: they say what kind of work it is, not which goal. Kept visible (reported in the method note).
TASK_WORDS = frozenset("""
please make add fix should test tests file files code sure ensure pass passes passing run runs need needs want use
using new update change changes implement create write work works let get go now also bug issue task thing things
way something fail failing error errors broken find look check help try
""".split())

# File extensions: noise in prompts ("README.md") and paths. Path pieces that say nothing about the goal.
EXT_NOISE = frozenset("js ts jsx tsx mjs cjs py rb rs java md json yaml yml toml txt sh".split())
PATH_NOISE = EXT_NOISE | frozenset("src lib app dist spec index main".split())

CONFIG_FILES = ("CLAUDE.md", "CLAUDE.local.md")
CONFIG_DIRS = (".claude/", "docs/claude/")
BASH_READERS = ("cat", "head", "tail", "sed", "less", "more", "nl", "bat", "wc", "grep", "rg", "awk")
BASH_PATTERN_FIRST = ("grep", "rg", "awk", "sed")  # first non-flag argument is a pattern / program, not a file

METHOD_NOTE = (
    "Goals are discovered from the data: each request's prompt words, edited and read files, path tokens, commands "
    "and tool-chain bigrams are TF-IDF weighted per group (idf over requests), L2-normalized, combined with fixed "
    "weights and compared by cosine; requests are merged by average-linkage clustering while the similarity stays "
    "above the goal threshold. Areas repeat this on goal centroids with files and paths weighted up. The outcome "
    "(success / failed / abandoned) is kept as evidence but is not a feature, so a goal is not split into its "
    "successes and its failures.")


# ---------------------------------------------------------------- text

_IDENT_RE = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def stem(w):
    """Light suffix stemming (s, es, ies, ing, ed, final e): rounding/rounded/rounds -> round."""
    if len(w) <= 3 or not w.isalpha():
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("es") and w[:-2].endswith(("s", "x", "z", "ch", "sh")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suf, min_len in (("ing", 6), ("ed", 5)):
        if w.endswith(suf) and len(w) >= min_len:
            w = w[:-len(suf)]
            if len(w) > 2 and w[-1] == w[-2] and w[-1] not in "lsz":
                w = w[:-1]
            break
    if w.endswith("e") and len(w) >= 5:
        w = w[:-1]
    return w


def words(text, noise=()):
    """[(stem, surface word)] of `text`: identifiers split, numbers and stopwords / task words / `noise` dropped."""
    out = []
    for raw in _IDENT_RE.findall(str(text or "")[:TEXT_CHARS]):
        for part in (p for p in raw.split("_") if p):
            if re.search(r"[A-Za-z]", part) and re.search(r"\d", part):
                pieces = [part]  # identifier like SAVE10 / v2: keep whole
            else:
                pieces = _CAMEL_RE.findall(part)
                if len(pieces) > 1 and not part.isupper():
                    pieces.append(part)  # camelCase identifier: its parts and the whole (toCents -> to, cents, tocents)
            for p in pieces:
                lw = p.lower()
                if lw.isdigit() or len(lw) < 2:
                    continue
                st = stem(lw)
                if lw in STOPWORDS or lw in TASK_WORDS or st in TASK_WORDS or lw in noise or st in noise:
                    continue
                out.append((st, lw))
    return out


def tokenize(text):
    return [s for s, _ in words(text)]


# ---------------------------------------------------------------- files

def _norm_path(p, cwd=None):
    p = str(p or "").strip()
    if not p:
        return ""
    if cwd and os.path.isabs(p):
        try:
            r = os.path.relpath(p, cwd)
            if not r.startswith(".."):
                p = r
        except ValueError:
            pass
    while p.startswith("./"):
        p = p[2:]
    return p


def _path_like(a):
    if a.startswith("-") or not re.fullmatch(r"[\w.@~/+-]+", a) or not re.search(r"[A-Za-z0-9]", a):
        return False
    return "/" in a or bool(re.search(r"\.[A-Za-z]\w{0,7}$", a))


_REDIRECT_RE = re.compile(r"^(?:\d|&)?>>?$")
_PY_OPEN_W_RE = re.compile(r"""open\(\s*(?:(['"])([^'"]+)\1|(\w+))\s*,\s*['"][wa]""")
_PY_ASSIGN_RE = re.compile(r"""\b(\w+)\s*=\s*(['"])([^'"\s]+)\2""")


def _segments(cmd):
    from .understand import _split_segments
    for seg in _split_segments(str(cmd or "")):
        toks = list(seg)
        while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in ("sudo", "time", "env")):
            toks.pop(0)
        if toks:
            yield toks


def _drop_redirects(toks):
    """Arguments without redirections (`> f`, `>f`, `2>&1`, `<<EOF`)."""
    out, skip = [], False
    for t in toks:
        if skip:
            skip = False
            continue
        if _REDIRECT_RE.match(t) or t in ("<", "<<", "<<<"):
            skip = True
            continue
        if t.startswith(("<", ">")) or re.match(r"^\d?>", t) or re.match(r"^&>", t):
            continue
        out.append(t)
    return out


def bash_write_targets(cmd):
    """Files a shell command writes: `> f` / `>> f` redirects, tee, sed -i / perl -pi, python open(f, 'w')."""
    out = []
    for toks in _segments(cmd):
        for i, t in enumerate(toks):
            if _REDIRECT_RE.match(t) and i + 1 < len(toks):
                out.append(toks[i + 1])
            elif re.match(r"^(?:\d)?>>?[^>&]", t):
                out.append(re.sub(r"^\d?>>?", "", t))
        prog = os.path.basename(toks[0])
        args = _drop_redirects(toks[1:])
        if prog == "tee" or (prog in ("sed", "perl") and any(re.match(r"^-\w*i", a) for a in args)):
            pats = [a for a in args if not a.startswith("-")]
            out += pats[1:] if prog != "tee" else pats
    text = str(cmd or "")
    for m in _PY_OPEN_W_RE.finditer(text):
        if m.group(2):
            out.append(m.group(2))
            continue
        assigns = [a for a in _PY_ASSIGN_RE.finditer(text[:m.start()]) if a.group(1) == m.group(3)]
        if assigns:
            out.append(assigns[-1].group(3))
    res = []
    for p in out:
        if _path_like(p) and not p.startswith("/dev/") and p not in res:
            res.append(p)
    return res


def bash_read_targets(cmd):
    """Files a shell command reads with cat / head / tail / sed -n / grep / ... (paths as written)."""
    out = []
    for toks in _segments(cmd):
        prog = os.path.basename(toks[0])
        if prog not in BASH_READERS or (prog == "sed" and any(re.match(r"^-\w*i", a) for a in toks[1:])):
            continue
        args = _drop_redirects(toks[1:])
        if prog in BASH_PATTERN_FIRST and not any(a in ("-e", "-f", "--regexp") for a in args):
            first = next((i for i, a in enumerate(args) if not a.startswith("-")), None)
            if first is not None:
                args = args[:first] + args[first + 1:]
        out += [a for a in args if _path_like(a)]
    return out


def request_files(steps, cwd=None):
    """{"edited": [...], "read": [...]} repo-relative paths a request touched (first-seen order, unique)."""
    edited, read = [], []
    for s in steps:
        if s["kind"].startswith("Hook("):
            continue
        tool = s.get("tool")
        if tool in ENGINE_EDITS:
            p = _norm_path(s.get("target"), cwd)
            if p and p not in edited:
                edited.append(p)
            continue
        cands = []
        if tool == "Read":
            cands = [s.get("target")]
        elif tool in ("Grep", "Glob"):
            cands = [s.get("path")]
        elif tool == "Bash":
            for c in bash_write_targets(s.get("target")):
                p = _norm_path(c, cwd)
                if p and p not in edited:
                    edited.append(p)
            cands = bash_read_targets(s.get("target"))
        for c in cands:
            p = _norm_path(c, cwd)
            if p and p not in read:
                read.append(p)
    return {"edited": edited, "read": read}


def is_config(path):
    base = os.path.basename(path)
    return base in CONFIG_FILES or path.startswith(CONFIG_DIRS) or any("/" + d in path for d in CONFIG_DIRS)


def overhead_files(reqs):
    """{path: reason} for files that say nothing about the goal (setup reads, agent config, outside the repo)."""
    n = len(reqs)
    reads = Counter(p for r in reqs for p in set(r["files"]["read"]))
    out = {}
    for r in reqs:
        for p in r["files"]["read"] + r["files"]["edited"]:
            if p in out:
                continue
            if is_config(p):
                out[p] = "agent config"
            elif os.path.isabs(p) or p.startswith("~"):
                out[p] = "outside the repo"
    if n >= OVERHEAD_MIN_REQUESTS:
        for p, c in reads.items():
            if p not in out and c / n > OVERHEAD_SHARE:
                out[p] = "read in %d of %d requests (> %d%%)" % (c, n, round(100 * OVERHEAD_SHARE))
    return out


DOC_EXT = (".md", ".rst", ".adoc", ".txt")


def path_tokens(path):
    """Stems of a path's basename (without extensions) and directory names, plus "docs" for documentation files:
    src/inventory/stock.test.js -> inventory, stock; CHANGELOG.md -> changelog, docs."""
    parts = path.split("/")
    base = parts[-1].split(".")[0] or parts[-1]
    out = [w for w in words(" ".join(parts[:-1] + [base]), noise=PATH_NOISE)]
    if path.lower().endswith(DOC_EXT) or "docs" in parts[:-1] or "doc" in parts[:-1]:
        out.append(("docs", "docs"))
    return out


# ---------------------------------------------------------------- signature

def _chain_kinds(steps):
    kinds = []
    for s in steps:
        k = "Hook" if s["kind"].startswith("Hook(") else s["kind"]
        if not kinds or kinds[-1] != k:
            kinds.append(k)
    return kinds


def signature(req, ignored=(), surface=None):
    """{group: Counter(feature)} for one request. `surface` collects {stem: Counter(surface word)} for display."""
    sig = {g: Counter() for g in GROUPS}
    for st, w in words(req.get("prompt"), noise=EXT_NOISE):
        sig["text"][st] += 1
        if surface is not None:
            surface.setdefault(("text", st), Counter())[w] += 1
    for st, w in words(req.get("_follow"), noise=EXT_NOISE):
        sig["text"][st] += FOLLOWUP_WEIGHT
    files = req.get("files") or {"edited": [], "read": []}
    for p in files["edited"]:
        if p not in ignored:
            sig["files_edited"][p] += 1
    for p in files["read"]:
        if p not in ignored:
            sig["files_read"][p] += 1
    for p in set(files["edited"] + files["read"]):
        if p in ignored:
            continue
        for st, w in path_tokens(p):
            sig["path"][st] = 1
            if surface is not None:
                surface.setdefault(("path", st), Counter())[w] += 1
    for p in files["edited"]:
        if p not in ignored:
            for st, _ in path_tokens(p):
                sig["edited_path"][st] = 1
    for s in req.get("steps") or []:
        if s["kind"].startswith("Bash("):
            sig["commands"][s["kind"][5:-1]] += 1
            if s.get("tests"):
                sig["commands"]["tests:" + s["kind"][5:-1]] += 1
    kinds = _chain_kinds(req.get("steps") or [])
    for a, b in zip(kinds, kinds[1:]):
        sig["chain"]["%s>%s" % (a, b)] += 1
    return {g: c for g, c in sig.items() if c}


def tfidf(sigs):
    """Per-request {group: {feature: weight}} with (1 + ln tf) * idf, each group L2-normalized."""
    n = len(sigs)
    df = Counter((g, f) for s in sigs for g, c in s.items() for f in c)
    out = []
    for s in sigs:
        v = {}
        for g, c in s.items():
            w = {f: (1 + math.log(tf)) * (math.log((1 + n) / (1 + df[(g, f)])) + 1) for f, tf in c.items()}
            norm = math.sqrt(sum(x * x for x in w.values()))
            if norm:
                v[g] = {f: x / norm for f, x in w.items()}
        out.append(v)
    return out


def combine(groups, weights):
    """One unit vector {(group, feature): value} from per-group unit vectors scaled by sqrt(weight).

    The dot product of two such vectors is sum_g w_g cos_g / sqrt(W_a W_b), W = summed weights of the groups each
    side has, so a request without edits is not penalised for the missing group."""
    v = {}
    for g, w in weights.items():
        if w <= 0 or g not in groups:
            continue
        sw = math.sqrt(w)
        for f, x in groups[g].items():
            v[(g, f)] = sw * x
    norm = math.sqrt(sum(x * x for x in v.values()))
    return {k: x / norm for k, x in v.items()} if norm else {}


def dot(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(k, 0.0) for k, x in a.items())


def sim_matrix(vecs):
    """Dense symmetric cosine matrix of unit sparse vectors (inverted index)."""
    n = len(vecs)
    S = [[0.0] * n for _ in range(n)]
    index = {}
    for i, v in enumerate(vecs):
        for k, x in v.items():
            index.setdefault(k, []).append((i, x))
    for posting in index.values():
        for a in range(len(posting)):
            i, xi = posting[a]
            row = S[i]
            for b in range(a + 1, len(posting)):
                j, xj = posting[b]
                row[j] += xi * xj
    for i in range(n):
        S[i][i] = 1.0 if vecs[i] else 0.0
        for j in range(i + 1, n):
            S[j][i] = S[i][j]
    return S


def agglomerate(S, threshold):
    """Average-linkage clustering on similarity matrix S: merge the most similar pair while its mean pairwise
    similarity >= threshold. Deterministic (ties -> lowest indices). Returns clusters as sorted index lists,
    ordered by their smallest index."""
    n = len(S)
    members = {i: [i] for i in range(n)}
    sim = {i: {j: S[i][j] for j in range(n) if j != i} for i in range(n)}

    def best(i):
        if not sim[i]:
            return (-1.0, 0)
        j = max(sim[i], key=lambda j: (round(sim[i][j], 12), -j))
        return (round(sim[i][j], 12), -j)

    nn = {i: best(i) for i in range(n)}
    while len(members) > 1:
        i = max(nn, key=lambda i: (nn[i][0], -min(i, -nn[i][1]), -i))
        s, j = nn[i][0], -nn[i][1]
        if s < threshold - 1e-12:
            break
        a, b = min(i, j), max(i, j)
        na, nb = len(members[a]), len(members[b])
        members[a] = sorted(members[a] + members.pop(b))
        del sim[b], nn[b]
        for k in sim:
            if k == a:
                continue
            new = (na * sim[k][a] + nb * sim[k].pop(b)) / (na + nb)
            sim[k][a] = new
            sim[a][k] = new
        sim[a].pop(b, None)
        nn[a] = best(a)
        for k in nn:
            if k == a:
                continue
            if -nn[k][1] in (a, b):
                nn[k] = best(k)
            elif (round(sim[k][a], 12), -a) > nn[k]:
                nn[k] = (round(sim[k][a], 12), -a)
    return [members[k] for k in sorted(members)]


# ---------------------------------------------------------------- discovery

def _sum_vec(vecs):
    s = {}
    for v in vecs:
        for k, x in v.items():
            s[k] = s.get(k, 0.0) + x
    return s


def _centroid_groups(groups_list):
    """Per group: mean of the members' unit group vectors, re-normalized."""
    out = {}
    for g in GROUPS:
        acc = {}
        for gr in groups_list:
            for f, x in (gr.get(g) or {}).items():
                acc[f] = acc.get(f, 0.0) + x
        norm = math.sqrt(sum(x * x for x in acc.values()))
        if norm:
            out[g] = {f: x / norm for f, x in acc.items()}
    return out


def _display(surface, key):
    c = surface.get(key)
    return c.most_common(1)[0][0] if c else key[1]


def _feature_label(group, feat, surface):
    if group in ("text", "path"):
        return "%s:'%s'" % (GROUP_LABEL[group], _display(surface, (group, feat)))
    return "%s:%s" % (GROUP_LABEL[group], feat)


def _counts(reqs, key):
    c = Counter(p for r in reqs for p in set(r["_files"][key]))
    return [{"path": p, "requests": n} for p, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]


def distinctive_terms(idx, groups, group="text", top=NAME_TERMS, order=None):
    """Stems with the highest mean TF-IDF mass inside the cluster minus the mean outside it (ties: `order`, the
    stem's first position in the members' prompts, then alphabetical)."""
    inside = set(idx)
    n_in, n_out = len(inside), len(groups) - len(inside)
    mass_in, mass_out = Counter(), Counter()
    for i, g in enumerate(groups):
        for f, x in (g.get(group) or {}).items():
            (mass_in if i in inside else mass_out)[f] += x
    score = {f: m / n_in - (mass_out[f] / n_out if n_out else 0.0) for f, m in mass_in.items()}
    order = order or {}
    ranked = sorted((f for f, s in score.items() if s > 0),
                    key=lambda f: (-round(score[f], 6), order.get(f, len(order)), f))
    return ranked[:top]


def goal_key(reqs):
    """Stable cache key: sha1 of the sorted unique member prompt hashes. Files are left out on purpose: reruns of
    the same prompts touch slightly different files, and the same set of requests should keep its name."""
    ph = sorted({hashlib.sha1(" ".join((r.get("prompt") or "").split()).encode("utf-8")).hexdigest() for r in reqs})
    return hashlib.sha1(json.dumps(ph).encode("utf-8")).hexdigest()


def discover(reqs, goal_threshold=None, area_threshold=None, unit="auto"):
    """Goals and areas for request dicts (need id, prompt, steps, files, chain, outcome, tokens, session_id, start).

    Returns {goals, areas, assignment: {request id: goal id}, method}. Names are fallback names (terms + top
    file); name_with_claude() can replace them."""
    gt = GOAL_THRESHOLD if goal_threshold is None else goal_threshold
    at = AREA_THRESHOLD if area_threshold is None else area_threshold
    all_reqs = sorted(reqs, key=lambda r: (r.get("start") or "", r["id"]))
    chores = detect_chores(all_reqs)
    threads = build_threads(all_reqs, chores)
    folded = sum(len(t) - 1 for t in threads)
    if unit == "auto":
        unit = "thread" if len(all_reqs) >= AUTO_MIN_REQUESTS and folded >= FOLLOWUP_SHARE_AUTO * len(all_reqs) else "request"
    if unit == "thread":
        reqs = [thread_unit(t, chores.get(t[0]["id"])) for t in threads]
    else:
        reqs = [dict(r, _members=[r], _follow="", _chore=None) for r in all_reqs]
        folded = 0
    reqs.sort(key=lambda r: (r.get("start") or "", r["id"]))
    ignored = overhead_files([dict(r, files=r.get("files") or {"edited": [], "read": []}) for r in reqs])
    surface = {}
    sigs = [signature(r, ignored, surface) for r in reqs]
    for r in reqs:
        f = r.get("files") or {"edited": [], "read": []}
        r["_files"] = {k: [p for p in f[k] if p not in ignored] for k in ("edited", "read")}
    for r in all_reqs:
        f = r.get("files") or {"edited": [], "read": []}
        r["_files"] = {k: [p for p in f[k] if p not in ignored] for k in ("edited", "read")}
    groups = tfidf(sigs)
    vecs = [combine(g, GOAL_WEIGHTS) for g in groups]
    S = sim_matrix(vecs)
    clusters = agglomerate(S, gt)
    if unit == "thread" and chores:
        cg = OrderedDict()
        for i, r in enumerate(reqs):
            if r["_chore"]:
                cg.setdefault(r["_chore"], []).append(i)
        clusters = [c for c in ([i for i in c if not reqs[i]["_chore"]] for c in clusters) if c] + list(cg.values())
    clusters.sort(key=lambda c: (-len(c), c[0]))
    goals = []
    sums = []
    for gi, idx in enumerate(clusters, 1):
        units_ = [reqs[i] for i in idx]
        members = [m for u in units_ for m in u["_members"]]
        n = len(idx)
        ssum = _sum_vec(vecs[i] for i in idx)
        sums.append(ssum)
        cohesion = None
        why = []
        if n > 1:
            sq = sum(sum(x * x for x in vecs[i].values()) for i in idx)
            cohesion = (sum(x * x for x in ssum.values()) - sq) / (n * (n - 1))
            contrib = {}
            for k, x in ssum.items():
                c = (x * x - sum(vecs[i].get(k, 0.0) ** 2 for i in idx)) / (n * (n - 1))
                if c > 1e-9:
                    contrib[k] = c
            for (g, f), c in sorted(contrib.items(), key=lambda kv: (-kv[1], kv[0]))[:WHY_FEATURES]:
                why.append({"group": g, "feature": f, "label": _feature_label(g, f, surface),
                            "contribution": round(c, 4)})
        chains = Counter(r["chain"] for r in members)
        pos = {}
        for r in members:
            for i, st in enumerate(tokenize(r.get("prompt"))):
                pos[st] = min(pos.get(st, i), i)
        terms = distinctive_terms(idx, groups, order=pos)
        if not terms:
            terms = distinctive_terms(idx, groups, "path")
        goals.append({
            "id": "g%d" % gi, "name": None, "name_source": "terms", "area": None, "area_name": None,
            "terms": [_display(surface, ("text", t)) if ("text", t) in surface else _display(surface, ("path", t))
                      for t in terms],
            "runs": len(members), "request_ids": [r["id"] for r in members],
            "kind": "chore" if units_[0]["_chore"] else "task", "threads": n, "requests": len(members),
            "follow_ups": len(members) - n, "sessions": len({r["session_id"] for r in members}),
            "projects": sorted({r.get("project") or "?" for r in members}),
            "members": [{"id": r["id"], "session": r["session_id"],
                         "prompt": _short(" ".join((r.get("prompt") or "").split()), MEMBERS_SHOWN),
                         "outcome": r["outcome"], "tokens": r["tokens"]["all_in"], "chain": r["chain"]}
                        for r in members],
            "files_edited": _counts(members, "edited")[:10], "files_read": _counts(members, "read")[:10],
            "chains": [{"chain": c, "count": k, "pct": round(100.0 * k / n, 1)}
                       for c, k in sorted(chains.items(), key=lambda kv: (-kv[1], kv[0]))[:3]],
            "outcomes": dict(sorted(Counter(r["outcome"] for r in members).items())),
            "cohesion": round(cohesion, 4) if cohesion is not None else None,
            "nearest": None, "why": why, "key": goal_key(members), "_idx": idx})
    for a, ga in enumerate(goals):
        best = None
        for b, gb in enumerate(goals):
            if a == b:
                continue
            s = dot(sums[a], sums[b]) / (len(ga["_idx"]) * len(gb["_idx"]))
            if best is None or s > best[0] + 1e-12:
                best = (s, gb)
        if best:
            ga["nearest"] = {"id": best[1]["id"], "similarity": round(best[0], 4)}
    # areas: goal centroids, files and paths emphasised
    cents = [combine(_centroid_groups([groups[i] for i in g["_idx"]]), AREA_WEIGHTS) for g in goals]
    fam_of = {}
    if unit == "thread":
        from .patterns import family
        for gi, g in enumerate(goals):
            fam = Counter(family(r.get("cwd"), r.get("project")) or "?" for i in g["_idx"] for r in reqs[i]["_members"])
            fam_of[gi] = sorted(fam.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    by_family = len(set(fam_of.values())) >= AREA_MIN_FAMILIES
    if by_family:
        fg = OrderedDict()
        for gi in range(len(goals)):
            fg.setdefault(fam_of[gi], []).append(gi)
        aclusters = list(fg.values())
    else:
        aclusters = agglomerate(sim_matrix(cents), at) if goals else []
    aclusters.sort(key=lambda c: (-sum(goals[i]["runs"] for i in c), c[0]))
    areas = []
    for ai, idx in enumerate(aclusters, 1):
        gs = [goals[i] for i in idx]
        aid = "a%d" % ai
        for g in gs:
            g["area"] = aid
        members = [m for g in gs for i in g["_idx"] for m in reqs[i]["_members"]]
        per_goal = Counter()
        for g in gs:
            for p in {p for i in g["_idx"] for p in reqs[i]["_files"]["edited"] + reqs[i]["_files"]["read"]}:
                per_goal[p] += 1
        shared = [{"path": p, "goals": c} for p, c in sorted(per_goal.items(), key=lambda kv: (-kv[1], kv[0]))
                  if c > 1 or len(gs) == 1][:8]
        areas.append({"id": aid, "name": None, "name_source": "terms", "goals": [g["id"] for g in gs],
                      "members": len(members), "shared_files": shared,
                      "key": hashlib.sha1(json.dumps(sorted(g["key"] for g in gs)).encode("utf-8")).hexdigest(),
                      "_members": members, "_idx": [i for g in gs for i in g["_idx"]]})
    fallback_names(goals, areas, groups, surface)
    if by_family:
        for a in areas:
            a["name"], a["name_source"] = fam_of[goals.index(next(g for g in goals if g["id"] == a["goals"][0]))], "family"
            a["family"] = a["name"]
    names = {g["id"]: g["name"] for g in goals}
    for g in goals:
        if g["nearest"]:
            g["nearest"]["name"] = names[g["nearest"]["id"]]
        g.pop("_idx")
    for a in areas:
        a.pop("_members")
        a.pop("_idx")
    assignment = {rid: g["id"] for g in goals for rid in g["request_ids"]}
    for r in all_reqs:
        r.pop("_files", None)
    return {"goals": goals, "areas": areas, "assignment": assignment,
            "units": {"unit": unit, "requests": len(all_reqs), "threads": len(threads) if unit == "thread" else len(all_reqs),
                      "follow_ups_folded": folded, "chores": sum(1 for g in goals if g["kind"] == "chore"),
                      "area_rollup": "family" if by_family else "clusters",
                      "rules": {"followup_max_words": FOLLOWUP_MAX_WORDS, "followup_min_terms": FOLLOWUP_MIN_TERMS,
                                "followup_starts": list(FOLLOWUP_STARTS), "followup_lead": FOLLOWUP_LEAD, "followup_weight": FOLLOWUP_WEIGHT,
                                "auto_share": FOLLOWUP_SHARE_AUTO, "auto_min_requests": AUTO_MIN_REQUESTS, "chore_jaccard": CHORE_JACCARD,
                                "chore_min_sessions": CHORE_MIN_SESSIONS, "area_min_families": AREA_MIN_FAMILIES}},
            "method": {"goal_weights": dict(GOAL_WEIGHTS), "area_weights": dict(AREA_WEIGHTS),
                       "goal_threshold": gt, "area_threshold": at, "linkage": "average",
                       "ignored_files": [{"path": p, "reason": why} for p, why in sorted(ignored.items())],
                       "task_words": sorted(TASK_WORDS), "path_noise": sorted(PATH_NOISE),
                       "text_noise": sorted(EXT_NOISE),
                       "stopwords": len(STOPWORDS), "note": METHOD_NOTE}}


# ---------------------------------------------------------------- goal units (threads, chores)

FOLLOWUP_MAX_WORDS = 25      # a follow-up is short...
FOLLOWUP_MIN_TERMS = 3       # ...and starts with a continuation word or has fewer distinctive terms than this
FOLLOWUP_LEAD = 4           # a continuation word within the first this-many words counts ("it's terrible haha ...")
FOLLOWUP_WEIGHT = 0.3        # follow-up text weight inside a thread signature
FOLLOWUP_SHARE_AUTO = 0.20   # auto mode uses threads when at least this share of requests are follow-ups (tuned on a real corpus: 25% follow-ups)
CHORE_JACCARD = 0.5          # short requests this similar (normalized token sets)...
CHORE_MIN_SESSIONS = 3       # ...recurring in this many sessions are chores: always their own goal
AUTO_MIN_REQUESTS = 30      # auto mode needs this many requests before switching to threads (small sets stay per request)
AREA_MIN_FAMILIES = 5        # thread mode rolls areas up by project family when there are this many families
FOLLOWUP_STARTS = ("yes", "yep", "ok", "okay", "sure", "go", "do it", "great", "perfect", "nice", "and", "also",
                   "then", "now", "no", "not", "still", "but", "wait", "actually", "hmm", "lol", "haha", "thanks",
                   "please", "sì", "si", "va bene", "perfetto")
_WORD_RE = re.compile(r"[^\W_]+", re.U)


def _plain(text):
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def is_follow_up(text):
    """Short AND (starts with a continuation word OR < FOLLOWUP_MIN_TERMS distinctive terms)."""
    ws = _plain(text)
    if not ws or len(ws) > FOLLOWUP_MAX_WORDS:
        return False if ws else True
    lead = ws[:FOLLOWUP_LEAD]
    pairs = {" ".join(lead[i:i + 2]) for i in range(len(lead))}
    if any(x in lead or x in pairs for x in FOLLOWUP_STARTS):
        return True
    return len({t for t in tokenize(text) if t not in TASK_WORDS}) < FOLLOWUP_MIN_TERMS


def _chore_set(text):
    return frozenset(stem(w) for w in _plain(text) if w not in STOPWORDS or w in ("pr", "push", "commit"))


def detect_chores(reqs):
    """{request id: chore group number}: short requests linked by token-set Jaccard >= CHORE_JACCARD whose
    connected group spans >= CHORE_MIN_SESSIONS sessions."""
    short = [(r, _chore_set(r.get("prompt"))) for r in reqs if 0 < len(_plain(r.get("prompt"))) <= FOLLOWUP_MAX_WORDS]
    ack = {stem(w) for x in FOLLOWUP_STARTS for w in x.split()} | {stem(w) for w in ("do", "that", "it", "this", "ahead", "them", "all", "good", "cool")}
    short = [(r, t) for r, t in short if t and (t - ack)]   # pure acknowledgements ("yes please") are follow-ups, never chores
    parent = list(range(len(short)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(short)):
        for j in range(i + 1, len(short)):
            a, b = short[i][1], short[j][1]
            if len(a & b) >= CHORE_JACCARD * len(a | b):
                parent[find(i)] = find(j)
    comps = {}
    for i in range(len(short)):
        comps.setdefault(find(i), []).append(short[i][0])
    out, n = {}, 0
    for root in sorted(comps, key=lambda k: min(r["id"] for r in comps[k])):
        rs = comps[root]
        if len({r["session_id"] for r in rs}) >= CHORE_MIN_SESSIONS:
            n += 1
            for r in rs:
                out[r["id"]] = n
    return out


def build_threads(reqs, chores):
    """[[head, follow-up, ...]] in session order. Chores always stand alone (and never absorb follow-ups)."""
    by_s = OrderedDict()
    for r in sorted(reqs, key=lambda r: (r.get("start") or "", r["id"])):
        by_s.setdefault(r["session_id"], []).append(r)
    threads = []
    for rs in by_s.values():
        cur = None
        for r in rs:
            if r["id"] in chores:
                threads.append([r])
                cur = None
            elif cur is not None and is_follow_up(r.get("prompt")):
                cur.append(r)
            else:
                cur = [r]
                threads.append(cur)
    return threads


def thread_unit(members, chore=None):
    head = members[0]
    files = {"edited": [], "read": []}
    for r in members:
        f = r.get("files") or {}
        for k in files:
            files[k] += [p for p in f.get(k, []) if p not in files[k]]
    outs = [r["outcome"] for r in members]
    return dict(head, files=files, steps=[s for r in members for s in (r.get("steps") or [])],
                outcome=outs[-1], tokens={"all_in": sum(r["tokens"]["all_in"] for r in members)},
                _members=members, _follow=" ".join(r.get("prompt") or "" for r in members[1:]), _chore=chore)


# ---------------------------------------------------------------- naming

def fallback_names(goals, areas, groups, surface):
    """goal: top distinctive terms + ' · ' + top edited (else read) file; area: its 2 most distinctive path tokens."""
    seen = Counter()
    for g in goals:
        top = (g["files_edited"] or g["files_read"] or [None])[0]
        parts = [" ".join(g["terms"][:NAME_TERMS])] if g["terms"] else []
        if top:
            parts.append(top["path"])
        name = " · ".join(parts) or "(no prompt, no files)"
        seen[name] += 1
        g["name"] = name if seen[name] == 1 else "%s (%d)" % (name, seen[name])
        g["name_source"] = "terms"
    gname = {g["id"]: g for g in goals}
    for a in areas:
        toks = [_display(surface, ("path", t)) for t in distinctive_terms(a["_idx"], groups, "path", top=2)]
        if not toks:
            toks = [_display(surface, ("text", t)) for t in distinctive_terms(a["_idx"], groups, top=2)]
        if not toks:
            toks = [gname[a["goals"][0]]["name"]]
        a["name"] = " / ".join(toks)
        a["name_source"] = "terms"
    for g in goals:
        g["area_name"] = next(a["name"] for a in areas if a["id"] == g["area"])


NAMES_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "tokenscope", "goal_names.json")
NAME_PROMPT_CHARS = 240
NAME_PROMPTS = 5
GENERIC_NAMES = {"fix a bug", "fix bug", "fix bugs", "add a feature", "add feature", "add features", "explain code",
                 "refactor", "refactor code", "update docs", "write tests", "review", "other", "bug fix",
                 "new feature", "documentation", "testing"}

NAME_INSTRUCTIONS = """You name groups of coding-agent requests. Each GOAL below is a cluster of user prompts that \
asked a coding agent for the same thing, with the files it edited/read, its most common tool chains, its most \
distinctive terms and its outcome mix. Each AREA groups related goals.
For every goal give a specific 3-7 word verb-first name of what the user wanted, e.g. "Fix rounding in checkout \
total" or "Reserve stock in Cart.add" - never generic like "Fix a bug", "Add a feature" or "Explain code". For every \
area give a 2-4 word noun phrase, e.g. "Pricing & checkout".
Reply with ONLY a JSON object, no prose and no code fences: {"goals": {"<goal id>": "<name>", ...}, \
"areas": {"<area id>": "<name>", ...}}

"""


def build_name_request(goals, areas, cached_goal_names=None):
    cached_goal_names = cached_goal_names or {}
    lines = []
    for g in goals:
        if g["id"] in cached_goal_names:
            continue
        prompts = []
        for m in g["members"]:
            p = _short(m["prompt"], NAME_PROMPT_CHARS)
            if p and p not in prompts:
                prompts.append(p)
        lines.append(json.dumps({
            "goal": g["id"], "prompts": prompts[:NAME_PROMPTS],
            "files_edited": [f["path"] for f in g["files_edited"][:5]],
            "files_read": [f["path"] for f in g["files_read"][:5]],
            "top_chains": [c["chain"] for c in g["chains"][:2]], "terms": g["terms"], "outcomes": g["outcomes"]},
            ensure_ascii=False))
    for a in areas:
        lines.append(json.dumps({"area": a["id"], "goals": [
            {"goal": gid, **({"name": cached_goal_names[gid]} if gid in cached_goal_names else {})}
            for gid in a["goals"]]}, ensure_ascii=False))
    return NAME_INSTRUCTIONS + "\n".join(lines) + "\n"


def _clean_name(x, n):
    if not isinstance(x, str):
        return None
    x = " ".join(x.split()).strip(" .\"'")[:n]
    if not x or x.lower() in GENERIC_NAMES:
        return None
    return x


def name_with_claude(d, cache_path=None, runner=None, now=None):
    """Replace the fallback names of discover() output with Claude names (one batched call, cached).

    -> status {used, source, called, cached, named, fallback_reason, cache_path}."""
    from . import labeler
    cache_path = cache_path or NAMES_CACHE
    cache = labeler.load_cache(cache_path)
    cache.setdefault("goals", {})
    cache.setdefault("areas", {})
    goals, areas = d["goals"], d["areas"]
    status = {"used": "claude", "source": None, "called": False, "cached": 0, "named": 0, "fallback_reason": None,
              "cache_path": cache_path}
    have_g = {g["id"]: cache["goals"][g["key"]] for g in goals
              if isinstance(cache["goals"].get(g["key"]), dict) and cache["goals"][g["key"]].get("name")}
    have_a = {a["id"]: cache["areas"][a["key"]] for a in areas
              if isinstance(cache["areas"].get(a["key"]), dict) and cache["areas"][a["key"]].get("name")}
    status["cached"] = len(have_g) + len(have_a)
    if len(have_g) < len(goals) or len(have_a) < len(areas):
        status["called"] = True
        req = build_name_request(goals, [a for a in areas if a["id"] not in have_a],
                                 {gid: e["name"] for gid, e in have_g.items()})
        obj, model, err = labeler.call_claude(req, runner, os.path.dirname(os.path.abspath(cache_path)))
        if err:
            status["fallback_reason"] = err
        else:
            ts = (now or datetime.now(timezone.utc)).isoformat()
            bad = []
            for kind, items, have, n in (("goals", goals, have_g, 60), ("areas", areas, have_a, 40)):
                reply = obj.get(kind) if isinstance(obj.get(kind), dict) else {}
                for it in items:
                    if it["id"] in have:
                        continue
                    nm = _clean_name(reply.get(it["id"]), n)
                    if nm is None:
                        bad.append(it["id"])
                        continue
                    entry = {"name": nm, "model": model, "ts": ts, "fallback": it["name"]}
                    cache[kind][it["key"]] = entry
                    have[it["id"]] = entry
                    status["named"] += 1
            if bad:
                status["fallback_reason"] = "%d names missing, invalid or generic in the reply (%s): named from " \
                                            "terms" % (len(bad), ", ".join(bad[:8]))
            try:
                labeler.save_cache(cache_path, cache)
            except OSError as e:
                status["cache_error"] = str(e)
    for items, have in ((goals, have_g), (areas, have_a)):
        for it in items:
            if it["id"] in have:
                it["name"] = have[it["id"]]["name"]
                it["name_source"] = labeler._source(have[it["id"]].get("model"))
    an = {a["id"]: a["name"] for a in areas}
    gn = {g["id"]: g["name"] for g in goals}
    for g in goals:
        g["area_name"] = an.get(g["area"])
        if g["nearest"]:
            g["nearest"]["name"] = gn[g["nearest"]["id"]]
    srcs = sorted({it["name_source"] for it in goals + areas})
    status["source"] = ", ".join(srcs)
    if not have_g and not have_a:
        status["used"] = "terms" if status["fallback_reason"] is None else "claude (failed)"
    return status


def name_note(namer):
    """'named by claude-haiku (3 cached, 2 new)' / 'named from terms'."""
    if not namer or namer.get("used") == "terms":
        return "named from terms"
    src = namer.get("source") or "terms"
    parts = ["named by " + src.replace(", terms", "") + (" (some from terms)" if "terms" in src and src != "terms"
                                                          else "")]
    parts.append("%d cached, %d new" % (namer.get("cached", 0), namer.get("named", 0)))
    note = "%s (%s)" % tuple(parts)
    if namer.get("fallback_reason"):
        note += "; terms fallback: %s" % namer["fallback_reason"]
    return note


def _short(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"
