"""Cross-session suggestions (docs/intents-spec.md section 9): an index of past requests, matching, skill hints.

Index: every main transcript under a root (default ~/.claude/projects, or $TOKENSCOPE_INDEX_ROOT) is replayed
through the live state (tokenscope/live/replay.py), its steps segmented and labelled deterministically (rule /
todo / subagent / declared labels; never a model call), and cut into user requests (a prompt up to the next one).
Each request, and each intent group of >= 2 steps, becomes a record with a WORKFLOW SIGNATURE:

    {"sid", "project", "ts0", "level": "request"|"group", "label",
     "tokens": normalised words (lowercase, stopwords, generic category words, identifiers, numbers dropped): of a
       request, mainly its user prompt's (code, paths and numbers stripped) plus its todo / declared step labels; of
       a group, its label and its steps' labels (skill-only and rule labels have none),
     "pwords": a request's prompt words as matched (cleaned, never shown; only the hint's short workflow name is
       built from them),
     "cats": category sequence (consecutive repeats folded), "tools": {display name: calls} (Read, Edit,
     "Bash unittest", "Bash git status" ...), "exts": file types, "files": basenames, "skills": skills used,
     "cw", "usd", "dur_s", "cw_cat": {category: CW},
     "steps": [{"cat", "label", "files", "cmds", "cw"}]}

Records are cached per transcript under <cache dir>/xindex/ (keyed by path + size + mtime of the transcript and its
subagent transcripts) and rebuilt incrementally: only new or changed transcripts are replayed.

Matching (`suggest`): the current request (and its groups) against records of OTHER sessions only: similarity =
W_TOK × word similarity + W_SEQ × category-sequence similarity + W_TOOL × tool / file overlap (WP_* weights and
prompt-word Dice for two requests with prompts, else label-token Jaccard), at least SIM_MIN, with an anchor (shared
words or shared files). A hint needs >= MIN_OCC prior matches in >= MIN_SESSIONS
sessions, each >= PRIOR_MIN_CW, the current one >= CUR_MIN_CW, and an estimated saving >= MIN_SAVING. The saving is
an estimate, not the whole cost: the median CW the matching prior runs spent in investigate steps on files common to
the runs (re-discovering what a skill's file map and steps would give up front).

Existing skills (`load_skills`): SKILL.md files (frontmatter name / description + the first lines) from
~/.claude/skills, <project>/.claude/skills and extra dirs. A repeated workflow matching a skill the session did not
use by word overlap (conservative, no model) becomes "Use skill /<name>" instead of "Make ... a skill".

Evidence leads with the numbers ("≈34k/run saving · done 4× in 4 sessions · ≈95k CW each · ..."), so a narrow pane
cuts the words, never the saving.

Drafts: a "make it a skill" hint writes a SKILL.md stub to <cache dir>/skill-drafts/<slug>/SKILL.md (never into a
skills directory; slug: whole words, up to 40 characters) and names it in `stub_path`, shown as "~/..." under home,
else "<cache>/skill-drafts/<slug>/SKILL.md" (never an absolute machine path). Its "## Steps" are the ordered steps the
matched runs share (common_steps).
"""
import difflib
import glob
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter

from . import intents

CACHE_VERSION = 5
# similarity
W_TOK, W_SEQ, W_TOOL = 0.4, 0.3, 0.3
# two requests with prompt words on both sides: the prompts say what the task is, the step shape how it was done
# (a /tdd run alternates edits and tests where a plain run edits once): prompt words (Dice) weigh more
WP_TOK, WP_SEQ, WP_TOOL = 0.5, 0.15, 0.35
ANCHOR_PTOK = 0.5             # prompt-word Dice for the anchor (about Jaccard 0.33)
SIM_MIN = 0.55                # a match: weighted similarity at least this ...
ANCHOR_TOK = 0.34             # ... and label-token Jaccard >= this, or
ANCHOR_FILES = 0.5            # ... an idf-weighted file overlap >= this
ANCHOR_N_FILES = 2            # ... over at least this many shared files
IDF_MAX = 5.0                 # weight of a file the index has never seen
# gates
MIN_OCC = 3                   # prior matching occurrences
MIN_SESSIONS = 2              # ... in at least this many distinct sessions
PRIOR_MIN_CW = 50_000         # each prior occurrence
CUR_MIN_CW = 50_000           # the current request / group
MIN_SAVING = 15_000           # estimated saving per run
COMMON_SHARE = 0.5            # a file is part of the workflow's file map when >= this share of the runs touch it
# existing skills
SK_MIN_OVERLAP = 3            # shared words between a skill (name + description) and the workflow
SK_MIN_COVER = 0.3            # ... and at least this share of the smaller word set
MAX_STEPS = 200

STOP = set("""a an the and or of to in on for with by from at as is are be been was were it its this that these those
into onto via per out up down over under then than so not no yes all any each both few more most other some such own
same can will just should now new old get got make made do does did done use used using add added fix fixed check
please also very only again via vs etc let lets need needs want wants way ways like one two three first last next
your you our we us me my i he she they them their what which who how why when where don doesn isn won ok okay
until already plus whose optionally sure go ask tell keep stop""".split())
# words of the rule labels and category names: the category sequence already says these
GENERIC = set("""step task work working read reading edit editing file files code search searching run running test
tests testing doc docs build command shell script checking git state commit committing pr setting setup delegat
subagent subagents research web driving browser query querying data writing external service gathering context
looking tool tools skill planning plan wait waiting asking report reporting coordinating investigate change verify
deleg overhead other respond load deferred""".split())
GENERIC |= {w for v in intents.LABELS.values() for w in re.split(r"[^a-z]+", v) if w}
# shell utilities: say nothing about what a workflow is for (skill matching ignores them)
UTILS = set("""cat grep egrep rg sed awk echo printf mkdir rm cp mv ls find head tail cut sort uniq tr wc xargs zsh bash
sh kill pkill sleep print man diff curl wget node npm npx pnpm yarn python python3 pip env jq less touch chmod which
true date tee test exec timeout open""".split())
JUNK_FILES = {"null", "stdout", "stderr", "tty", "zero", "random", "urandom"}   # /dev/... targets, not project files
BARE_FILES = {"makefile", "dockerfile", "license", "licence", "readme", "changelog", "gemfile", "procfile", "rakefile",
              "jenkinsfile", "vagrantfile", "justfile", "brewfile", "pipfile", "containerfile", "caddyfile", "notice",
              "authors", "copying", "codeowners"}   # files without an extension


def project_file(f):
    """A file worth comparing: looks like a file (an extension, a dotfile or a known bare name such as Makefile; a
    directory from `ls hist-1` does not), not a device (/dev/null) nor a background task's output capture."""
    if not f or f in JUNK_FILES or f.endswith(".output"):
        return False
    return bool(os.path.splitext(f)[1]) or (f.startswith(".") and len(f) > 1) or f.lower() in BARE_FILES


def cache_dir():
    return os.environ.get("TOKENSCOPE_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "tokenscope")


def index_root(arg=None):
    """--index-root, else $TOKENSCOPE_INDEX_ROOT, else ~/.claude/projects."""
    return os.path.abspath(os.path.expanduser(arg or os.environ.get("TOKENSCOPE_INDEX_ROOT")
                                              or os.path.join("~", ".claude", "projects")))


# ---------------------------------------------------------------- signatures

def _stem(w):
    if re.search(r"(?:ss|us|is)$", w):
        return w
    if len(w) >= 5 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) >= 6 and w.endswith("ing"):
        return w[:-3]
    if len(w) >= 5 and w.endswith("ed"):
        return w[:-2]
    if len(w) >= 5 and re.search(r"(?:ch|sh|x|z|ss)es$", w):
        return w[:-2]
    return w[:-1] if len(w) >= 4 and w.endswith("s") else w


GENERIC |= {_stem(w) for w in GENERIC}


def tokens(text):
    """Normalised words of a label: lowercase, stemmed; stopwords, generic category words, file names, paths,
    identifiers (digits, snake_case, camelCase) and words < 3 letters dropped."""
    out = []
    for raw in re.split(r"[\s,;:()\[\]{}\"'`·→/|<>=+*!?]+", str(text or "")):
        w = raw.strip(".-_#@$%&^~")
        if not w or re.search(r"[\d_]", w) or "." in w or re.search(r"[a-z][A-Z]", w):
            continue
        for part in w.lower().split("-"):
            if len(part) < 3 or part in STOP or part in GENERIC:
                continue
            part = _stem(part)
            if part in STOP or part in GENERIC or len(part) < 3:
                continue
            out.append(part)
    return out


SKILL_LABEL = re.compile(r"Use skills? \S")   # steps.skill_label: a skill-only step


def skill_only(step):
    return step.get("source") == "rule" and bool(SKILL_LABEL.match(str(step.get("label") or "")))


def label_tokens(step):
    """A step's label words; a rule label ("editing files · store.py, cli.py") has none of its own: its words are
    the category's and its targets are the files / programs, compared separately. A skill-only step ("Use skill
    tdd") has none either: the skills are compared on their own."""
    lab = str(step.get("label") or "")
    if step.get("cat") == "overhead" or skill_only(step):
        return []   # "Load notion-update-page tool": a tool name, compared with the tools
    return tokens(lab.split(" · ")[0] if step.get("source") == "rule" else lab)


def prompt_words(text, max_words=80):
    """The words of a user prompt kept for matching: fenced code, `backticked` identifiers, tags, urls, paths, file
    names and numbers dropped; lowercase, in order, at most `max_words` (stopwords are dropped later by tokens);
    "|" marks the end of a clause (. : ; ! ? or a line break)."""
    t = re.sub(r"```.*?(?:```|$)", " ", str(text or ""), flags=re.S)
    t = re.sub(r"`[^`\n]*`?", " ", t)
    t = re.sub(r"<[^<>\n]{1,80}>", " ", t)
    t = re.sub(r"\b\w+://\S+", " ", t)
    t = re.sub(r"\S*[/\\]\S*", " ", t)                    # paths, "a/b"
    t = re.sub(r"\S*\w\.\w\S*", " ", t)                   # file names, module.attr
    out = []
    for w in re.findall(r"[A-Za-z0-9_'-]+|[.:;!?\n]", t):
        w = w.strip("'-")
        if w and w in ".:;!?\n":
            if out and out[-1] != "|":
                out.append("|")
        elif w and not re.search(r"[\d_]", w) and not re.search(r"[a-z][A-Z]", w):
            out.append(w.lower().split("'")[0])
    return [w for w in out if w][:max_words]


def _ext(f):
    e = os.path.splitext(f)[1].lower()
    return e if 1 < len(e) <= 8 else (f.lower() if f.lower() in ("makefile", "dockerfile") else "")


def _record(sid, project, level, label, sub, pwords=None):
    """The signature record of a run of steps (a request or a group); `pwords`: the request's prompt words."""
    cats, tools, files, skills, cw_cat = [], Counter(), [], [], Counter()
    for x in sub:
        if (not cats or cats[-1] != x["cat"]) and x["cat"] != "overhead" and not skill_only(x):
            cats.append(x["cat"])   # loading tools / a skill says nothing about the workflow's shape
        tools.update({k: v for k, v in (x.get("tools") or {}).items() if not k.startswith("✦")})
        files += [f for f in x.get("files") or () if f not in files]
        skills += [n for n in x.get("skills") or () if n not in skills]
        cw_cat[x["cat"]] += x.get("cw") or 0
    ts = [t for x in sub for t in (x.get("ts0"), x.get("ts1")) if t]
    first = next((x for x in sub if not skill_only(x) and x["cat"] != "overhead"), sub[0])   # nor "Use skill tdd"
    rec = {"sid": sid, "project": project, "level": level, "label": label or (first.get("label") or ""),
           "ts0": min(ts) if ts else None, "dur_s": round(max(ts) - min(ts), 1) if ts else None,
           "tokens": [], "pwords": list(pwords or [])[:80], "cats": cats, "tools": dict(tools),
           "exts": sorted({_ext(f) for f in files} - {""}), "files": files[:40], "skills": skills,
           "cw": round(sum(x.get("cw") or 0 for x in sub)), "usd": round(sum(x.get("usd") or 0 for x in sub), 4),
           "cw_cat": {k: round(v) for k, v in cw_cat.items()},
           "steps": [{"cat": x["cat"], "label": str(x.get("label") or "")[:80], "source": x.get("source"),
                      "tested": x.get("tested"), "runners": [r.replace("Bash ", "", 1) for r in x.get("runners") or ()][:2],
                      "files": (x.get("files") or [])[:6], "cmds": (x.get("cmds") or [])[:4], "cw": round(x.get("cw") or 0)} for x in sub[:MAX_STEPS]]}
    return retoken(rec)


def retoken(rec):
    """Fill a record's words from its prompt (a request), group label and steps, and drop non-project files:
    recomputed when the index loads, so a change of the normalisation needs no replay. A request with a prompt
    takes its words from the prompt only: step labels depend on the labeller (model labels in the live pane, rule
    labels in the index), so mixing them in would make the same request match with one and not the other."""
    toks = set(tokens(rec["label"])) if rec["level"] == "group" else set(tokens(" ".join(rec.get("pwords") or ())))
    by_prompt_only = rec["level"] == "request" and bool(rec.get("pwords"))
    rec["files"] = [f for f in rec["files"] if project_file(f)]
    rec["exts"] = sorted({_ext(f) for f in rec["files"]} - {""})
    for x in rec["steps"]:
        x["files"] = [f for f in x["files"] if project_file(f)]
        if not by_prompt_only:
            toks |= set(label_tokens(x))
    rec["tokens"] = sorted(toks)
    return rec


def _prompt_of(sub, prompts, lo):
    """The words of the prompt that started a request: the last prompt that arrived after the previous request
    started (turn `lo`) and before this one's first turn (an earlier one there got no work: "thanks")."""
    hi = sub[0].get("turn_first", sub[0]["i"])
    return next((w for at, w in reversed(prompts or ()) if lo < at <= hi), None)


def requests_of(steps, groups, sid="", project="", prompts=()):
    """Records of the requests (a user prompt up to the next one) and of the groups of >= 2 steps in them.
    `prompts`: [(turn count when the prompt arrived, prompt_words(text))] (LiveState.prompt_at / prompt_words)."""
    out, cur = [], []
    reqs = []
    for x in steps:
        if cur and x.get("prompt_before"):
            reqs.append(cur)
            cur = []
        cur.append(x)
    if cur:
        reqs.append(cur)
    lo = -1
    for sub in reqs:
        pw = _prompt_of(sub, prompts, lo)
        lo = sub[0].get("turn_first", sub[0]["i"])
        if not any(x.get("cw") for x in sub):
            continue
        out.append(_record(sid, project, "request", "", sub, pw))
        a, b = sub[0]["i"], sub[-1]["i"]
        for g in groups or ():
            if g["first"] >= a and g["last"] <= b and g["last"] > g["first"]:
                out.append(_record(sid, project, "group", g.get("label"), steps[g["first"]:g["last"] + 1]))
    return out


def current_records(steps, groups, sid="", project="", prompts=()):
    """The last request of a session and its groups (what the idle recap covers)."""
    recs = requests_of(steps, groups, sid, project, prompts)
    last = max((k for k, r in enumerate(recs) if r["level"] == "request"), default=None)
    return recs[last:] if last is not None else []


def index_transcript(path):
    """{"sid", "project", "cwd", "ts0", "skills", "records"} of one main transcript (deterministic labels only)."""
    from .live.replay import replay
    state, s = replay(path, redact=False, model_labels=False)
    if s is None or not s.main.turn_steps:
        return None
    steps, gs = state.steps(s, final=True)
    project = s.project_raw
    return {"sid": s.id, "project": project, "cwd": getattr(s, "cwd", None), "ts0": s.started,
            "skills": sorted(s.skills),
            "records": requests_of(steps, gs, s.id, project, list(zip(s.prompt_at, s.prompt_words)))}


# ---------------------------------------------------------------- the index (cached per transcript)

def _key(path):
    st = os.stat(path)
    sub = os.path.join(os.path.dirname(path), os.path.basename(path)[:-len(".jsonl")], "subagents")
    n = size = 0
    if os.path.isdir(sub):
        for p in glob.glob(os.path.join(sub, "**", "agent-*.jsonl"), recursive=True):
            try:
                size += os.path.getsize(p)
                n += 1
            except OSError:
                pass
    return [st.st_size, st.st_mtime_ns, n, size]


def _safe_index(path):
    try:
        return path, index_transcript(path)
    except Exception as e:  # noqa: BLE001 - one bad transcript must not stop the index
        print("tokenscope xindex: %s: %s" % (os.path.basename(path), type(e).__name__), file=sys.stderr)
        return path, None


class XIndex:
    """Signature records of every main session under `root`, cached in <cache dir>/xindex/<hash of root>.json."""

    def __init__(self, root=None, cache=None):
        self.root = index_root(root)
        self.cache = os.path.join(cache or cache_dir(), "xindex",
                                  hashlib.sha1(self.root.encode("utf-8")).hexdigest()[:12] + ".json")
        self.sessions = {}        # sid -> entry
        self.stats = {}
        self.ready = False

    def _load(self):
        try:
            with open(self.cache, encoding="utf-8") as f:
                d = json.load(f)
            return d.get("files", {}) if d.get("version") == CACHE_VERSION else {}
        except (OSError, ValueError):
            return {}

    def build(self, jobs=None):
        """Load the cache, replay new / changed transcripts (in `jobs` processes), save. Returns stats."""
        t0 = time.time()
        old = self._load()
        files, todo = {}, []
        for path in sorted(glob.glob(os.path.join(self.root, "*", "*.jsonl"))):
            try:
                k = _key(path)
            except OSError:
                continue
            hit = old.get(path)
            if hit and hit.get("key") == k:
                files[path] = hit
            else:
                todo.append((path, k))
        if todo:
            keys = dict(todo)
            paths = [p for p, _ in todo]
            jobs = jobs or min(8, os.cpu_count() or 1)
            if jobs > 1 and len(paths) > 1:
                from multiprocessing import get_context
                with get_context("fork" if sys.platform != "win32" else "spawn").Pool(jobs) as pool:
                    res = pool.map(_safe_index, paths, chunksize=1)
            else:
                res = [_safe_index(p) for p in paths]
            for p, entry in res:
                files[p] = {"key": keys[p], "entry": entry}
        if todo or set(old) != set(files):
            os.makedirs(os.path.dirname(self.cache), exist_ok=True)
            tmp = self.cache + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": CACHE_VERSION, "root": self.root, "files": files}, f, separators=(",", ":"))
            os.replace(tmp, self.cache)
        self.sessions = {v["entry"]["sid"]: v["entry"] for v in files.values() if v.get("entry")}
        for e in self.sessions.values():
            for r in e["records"]:
                retoken(r)
        self.stats = {"transcripts": len(files), "replayed": len(todo), "sessions": len(self.sessions),
                      "records": sum(len(e["records"]) for e in self.sessions.values()),
                      "seconds": round(time.time() - t0, 2)}
        self.ready = True
        return self.stats

    def records(self, exclude_sid=None, before=None):
        """Records of the other sessions (optionally only sessions that started before `before`)."""
        out = []
        for sid, e in self.sessions.items():
            if sid == exclude_sid or (before is not None and (e.get("ts0") is None or e["ts0"] >= before)):
                continue
            out += e["records"]
        return out


# ---------------------------------------------------------------- similarity

def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def weighted_jaccard(a, b):
    keys = set(a) | set(b)
    den = sum(max(a.get(k, 0), b.get(k, 0)) for k in keys)
    return sum(min(a.get(k, 0), b.get(k, 0)) for k in keys) / den if den else 0.0


def seq_sim(a, b):
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0


def file_overlap(a, b, idf=None):
    """(idf-weighted overlap coefficient of two file sets, shared files): shared weight / the smaller set's weight.
    A file most runs touch (README.md) weighs little, a rare one shared by both a lot."""
    a, b = set(a), set(b)
    w = (lambda f: idf.get(f, IDF_MAX)) if idf else (lambda f: 1.0)
    shared = a & b
    den = min(sum(w(f) for f in a), sum(w(f) for f in b))
    return (sum(w(f) for f in shared) / den if den else 0.0), shared


def dice(a, b):
    a, b = set(a), set(b)
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def by_prompt(a, b):
    return bool(a.get("pwords")) and bool(b.get("pwords"))


def token_sim(a, b):
    """(similarity, anchor threshold) of two records' words: Dice of two requests' prompt words (a prompt carries
    task-specific words too: "list the items with the highest stock value"), else label-token Jaccard."""
    return (dice(a["tokens"], b["tokens"]), ANCHOR_PTOK) if by_prompt(a, b) else \
        (jaccard(a["tokens"], b["tokens"]), ANCHOR_TOK)


def similarity(a, b, idf=None):
    """(score, parts): W_TOK × word similarity (token_sim) + W_SEQ × category-sequence similarity + W_TOOL × tool /
    file overlap (tool calls 0.4, file types 0.1, files 0.5); WP_* weights for two requests with prompts. Without
    words on either side the score is the other two parts, renormalised."""
    jt, _ = token_sim(a, b)
    fo, shared = file_overlap(a["files"], b["files"], idf)
    tf = 0.4 * weighted_jaccard(a["tools"], b["tools"]) + 0.1 * jaccard(a["exts"], b["exts"]) + 0.5 * fo
    sq = seq_sim(a["cats"], b["cats"])
    if by_prompt(a, b):
        sc = WP_TOK * jt + WP_SEQ * sq + WP_TOOL * tf
    elif a["tokens"] or b["tokens"]:
        sc = W_TOK * jt + W_SEQ * sq + W_TOOL * tf
    else:
        sc = (W_SEQ * sq + W_TOOL * tf) / (W_SEQ + W_TOOL)
    return sc, {"tok": jt, "seq": sq, "tool": tf, "files": fo, "shared": len(shared)}


def is_match(a, b, idf=None):
    """(matches?, score): similarity >= SIM_MIN with an anchor: shared words (token_sim >= ANCHOR_TOK, or
    ANCHOR_PTOK for prompts), or >= ANCHOR_N_FILES shared files with an overlap >= ANCHOR_FILES."""
    jt, anchor = token_sim(a, b)
    fo, shared = file_overlap(a["files"], b["files"], idf)
    if jt < anchor and (fo < ANCHOR_FILES or len(shared) < ANCHOR_N_FILES):
        return False, 0.0
    sc, _ = similarity(a, b, idf)
    return sc >= SIM_MIN, sc


def file_idf(records):
    """{file: log(N / runs touching it)} over request records (a rare shared file is stronger evidence)."""
    reqs = [r for r in records if r["level"] == "request"]
    df = Counter(f for r in reqs for f in set(r["files"]))
    n = max(len(reqs), 1)
    return {f: max(0.1, math.log(n / k)) for f, k in df.items()}


# ---------------------------------------------------------------- skills on disk

def _frontmatter(text):
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n?(.*)", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"([A-Za-z_-]+)\s*:\s*(.*)", line)
        if mm:
            meta[mm.group(1).lower()] = mm.group(2).strip().strip("\"'")
    return meta, m.group(2)


def skill_dirs(cwd=None, extra=()):
    out = [os.path.join(os.path.expanduser("~"), ".claude", "skills")]
    if cwd:
        out.append(os.path.join(cwd, ".claude", "skills"))
    return out + [os.path.abspath(os.path.expanduser(d)) for d in extra or ()]


def load_skills(dirs):
    """[{"name", "description", "path", "tokens"}] of the SKILL.md files under `dirs` (first one per name wins)."""
    out, seen = [], set()
    for d in dirs:
        for p in sorted(glob.glob(os.path.join(d, "*", "SKILL.md")) + glob.glob(os.path.join(d, "SKILL.md"))):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read(8000)
            except OSError:
                continue
            meta, body = _frontmatter(text)
            name = meta.get("name") or os.path.basename(os.path.dirname(p))
            if name in seen:
                continue
            seen.add(name)
            desc = " ".join((meta.get("description") or "").split())
            head = " ".join(ln for ln in body.splitlines()[:8] if ln.strip() and not ln.startswith("```"))
            out.append({"name": name, "description": desc, "path": p,
                        "name_tokens": sorted(set(tokens(name.replace("-", " ").replace("_", " ")))),
                        "tokens": sorted(set(tokens(name.replace("-", " ").replace("_", " ") + " " + desc + " "
                                                    + head)))})
    return out


def workflow_words(recs):
    """Words describing a workflow for skill matching: label words, file stems, programs."""
    w = set()
    for r in recs:
        w |= set(r["tokens"])
        for f in r["files"]:
            w |= set(tokens(os.path.splitext(f)[0].replace("_", " ").replace("-", " ")))
        for x in r["steps"]:
            for c in x["cmds"]:
                w |= set(tokens(c))
    return w - UTILS


def match_skill(words, skills, used=(), min_overlap=SK_MIN_OVERLAP):
    """The best skill (not in `used`) whose words overlap the workflow's: >= min_overlap shared words, >=
    SK_MIN_COVER of the smaller set, and at least one word of the skill's name; else None."""
    best = None
    for sk in skills:
        if sk["name"] in used:
            continue
        st = set(sk["tokens"])
        common = st & words
        if len(common) < min_overlap or not (set(sk["name_tokens"]) & words) \
                or len(common) < SK_MIN_COVER * min(len(st), len(words)):
            continue
        if best is None or len(common) > best[1]:
            best = (sk, len(common), sorted(common))
    return best


# ---------------------------------------------------------------- suggestions

VERBS = {"investigate": "read", "change": "edit", "verify": "run", "plan": "plan", "delegate": "delegate",
         "ship": "commit", "research": "research"}


def common_files(runs):
    n = len(runs)
    c = Counter(f for r in runs for f in dict.fromkeys(r["files"]))   # first-seen order breaks ties
    return [f for f, k in c.most_common() if k >= max(2, COMMON_SHARE * n)]


def step_sequence(rec, files, max_parts=5):
    """"read cli.py → edit cli.py, README.md → run unittest": the steps on the workflow's file map (reads, edits)
    and its test runs, in order, each (verb, target) once."""
    parts, seen = [], set()
    for x in rec["steps"]:
        verb = VERBS.get(x["cat"])
        if skill_only(x):
            continue
        if verb in ("read", "edit"):
            tg = [f for f in x["files"] if f in files]
        elif x.get("tested") or x.get("runners"):
            verb, tg = "run", list(x.get("runners") or ["tests"])
        elif verb in ("plan", "delegate", "commit"):
            tg = []
        else:
            continue
        tg = [t for t in tg if (verb, t) not in seen]
        if verb in ("read", "edit", "run") and not tg or (verb, None) in seen and not tg:
            continue
        seen.update((verb, t) for t in tg or [None])
        if parts and parts[-1][0] == verb:
            parts[-1][1].extend(tg)
        else:
            parts.append((verb, tg))
    out = []
    for verb, tg in parts[:max_parts]:
        out.append(verb + (" " + ", ".join(tg[:3]) + (" +%d" % (len(tg) - 3) if len(tg) > 3 else "") if tg else ""))
    return " → ".join(out) + (" → …" if len(parts) > max_parts else "")


def rediscovery(rec, files):
    """CW of a run's investigate steps on files of the workflow's file map."""
    fs = set(files)
    return sum(x["cw"] for x in rec["steps"] if x["cat"] == "investigate" and fs & set(x["files"]))


TASK_VERBS = set("""add implement create write fix update refactor remove delete rename migrate port bump upgrade document
debug review optimise optimize extend support replace convert move split merge clean improve generate build""".split())
PREPS = {"to", "of", "in", "for", "on", "into", "from"}
DOC_WORDS = {"readme", "changelog", "unit", "document", "documentation", "docs", "doc", "test", "tests", "testing"}
ACRONYMS = {"cli", "api", "csv", "json", "ui", "url", "sql", "http", "html", "css", "yaml", "toml", "sdk", "db", "ci"}


def _is_test_file(f):
    b = f.lower()
    return b.startswith("test_") or re.search(r"(?:_test|\.test|\.spec|_spec)\.\w+$", b) is not None


def _prompt_name(runs, files):
    """"Add CLI subcommand with tests, README and CHANGELOG": the task verb and the words of the first clause of
    the current prompt (the last run) that most runs' prompts share (project-name words dropped; "X to the Y" read
    as "Y X"), plus the tests / README / CHANGELOG most runs edited. None without such words."""
    cur = runs[-1]
    pw = cur.get("pwords") or []
    if not pw:
        return None
    share = max(2, COMMON_SHARE * len(runs))
    df = Counter(t for r in runs for t in set(tokens(" ".join(r.get("pwords") or ()))))
    proj = set(tokens(re.sub(r"[^A-Za-z]+", " ", str(cur.get("project") or ""))))
    clause = []
    for w in pw[:30]:   # the first clause (up to "|" or a doc / test word) states the task
        if clause and (w == "|" or w in DOC_WORDS):
            break
        if w != "|":
            clause.append(w)
    verb = next((w for w in clause if w in TASK_VERBS
                 and sum(1 for r in runs if w in (r.get("pwords") or ())) >= share), None)
    words, after = [], []
    if verb:
        clause = clause[clause.index(verb) + 1:]
    prep = False
    for w in clause:
        t = (tokens(w) or [None])[0]
        if t and df[t] >= share and t not in proj and t not in DOC_WORDS and t != verb:
            (after if prep and words else words).append(w)
        elif w in PREPS and words:
            prep = True
    words = (after + words)[:4]
    if not words:
        return None
    n = len(runs)
    edits = Counter(f for r in runs for f in edits_of(r))
    tested = sum(1 for r in runs if any(x.get("tested") or x.get("runners") for x in r["steps"])
                 or any(_is_test_file(f) for f in edits_of(r)))
    extras = (["tests"] if tested >= COMMON_SHARE * n else []) + \
             [lab for lab, stem in (("README", "readme"), ("CHANGELOG", "changelog"))
              if sum(k for f, k in edits.items() if os.path.splitext(f)[0].lower() == stem) >= COMMON_SHARE * n]
    name = " ".join(w.upper() if w in ACRONYMS else w for w in words)
    name = ((verb.capitalize() + " " + name) if verb else name[:1].upper() + name[1:])
    if extras:
        name += " with " + (", ".join(extras[:-1]) + " and " + extras[-1] if len(extras) > 1 else extras[0])
    return name


def edits_of(rec):
    return {f for x in rec["steps"] if x["cat"] == "change" for f in x["files"]}


def _name(runs, files):
    """A short name for the workflow: from the prompts (_prompt_name), else the label words most runs share, else
    "<verb> <top files>" (+ "and test")."""
    pn = _prompt_name(runs, files)
    if pn:
        return pn
    c = Counter(t for r in runs for t in r["tokens"])
    top = [t for t, k in c.most_common(6) if k >= max(2, COMMON_SHARE * len(runs))][:4]
    if top:
        return " ".join(top)
    edits = Counter(f for r in runs for x in r["steps"] if x["cat"] == "change" for f in x["files"] if f in files)
    tests = sum(1 for r in runs if any(x.get("tested") or x.get("runners") for x in r["steps"])) >= COMMON_SHARE * len(runs)
    verb = "change" if edits else "work through"
    fs = [f for f, _ in edits.most_common(2)] or files[:2]
    return "%s %s%s" % (verb, " + ".join(fs) or "these files", " and test" if tests else "")


def slug(text, max_len=40):
    """"add-cli-subcommand-with-tests-readme-and": whole words, as many as fit in `max_len` characters (a single
    longer word is cut)."""
    out = []
    for w in re.findall(r"[a-z0-9]+", str(text).lower()):
        if out and len("-".join(out + [w])) > max_len:
            break
        out.append(w[:max_len])
    return "-".join(out) or "workflow"


def _fk(n):
    n = float(n or 0)
    return "%.1fM" % (n / 1e6) if n >= 1e6 else "%dk" % round(n / 1e3) if n >= 1e3 else "%d" % n


def find_repeat(cur, history, idf=None):
    """Prior matches of `cur` from `history` (records of other sessions) passing the gates, else None:
    {"cur", "matches": [(score, rec)], "sessions", "files", "saving", "seq"}."""
    if cur["cw"] < CUR_MIN_CW:
        return None
    m = []
    for r in history:
        if r["level"] != cur["level"] or r["sid"] == cur["sid"] or r["cw"] < PRIOR_MIN_CW:
            continue
        ok, sc = is_match(cur, r, idf)
        if ok:
            m.append((sc, r))
    sessions = {r["sid"] for _, r in m}
    if len(m) < MIN_OCC or len(sessions) < MIN_SESSIONS:
        return None
    m.sort(key=lambda x: -x[0])
    runs = [r for _, r in m]
    files = common_files(runs + [cur])
    saving = statistics.median(rediscovery(r, files) for r in runs) if files else 0
    return {"cur": cur, "matches": m, "sessions": sessions, "files": files, "saving": saving,
            "seq": step_sequence(cur, files)}


DOC_EXTS = {".md", ".rst", ".txt", ".adoc"}


def _phases(rec):
    """A run's steps as ordered phases [(verb, [targets])], consecutive same verbs merged: "read" files, "edit"
    code files ("tests" for test files), "update" docs, "run" its test runners / commands, "plan", "delegate",
    "commit"; skill-only and overhead steps skipped."""
    out = []
    for x in rec["steps"]:
        if skill_only(x) or x["cat"] == "overhead":
            continue
        verb = VERBS.get(x["cat"])
        parts = []
        if x["cat"] == "investigate":
            parts = [("read", list(x["files"]))]
        elif x["cat"] == "change":
            docs = [f for f in x["files"] if _ext(f) in DOC_EXTS]
            code = [f for f in x["files"] if f not in docs]
            parts = [("edit", ["tests" if _is_test_file(f) else f for f in code])] + ([("update", docs)] if docs else [])
        elif x.get("tested") or x.get("runners"):
            parts = [("run", list(x.get("runners") or ["tests"]))]
        elif x["cat"] == "verify":
            parts = [("run", [c for c in x["cmds"] if c.split()[0] not in UTILS][:2])]
        elif verb in ("plan", "delegate", "commit"):
            parts = [(verb, [])]
        for v, tg in parts:
            if v in ("read", "edit", "update", "run") and not tg:
                continue
            if out and out[-1][0] == v:
                out[-1][1].extend(t for t in tg if t not in out[-1][1])
            else:
                out.append((v, list(dict.fromkeys(tg))))
    return out


def common_steps(runs, max_steps=8):
    """The ordered steps the runs share: the phase sequence of the run that agrees most with the others (verb
    sequences, difflib), each phase kept when >= COMMON_SHARE of the runs have it at an aligned place, with the
    targets >= COMMON_SHARE of those runs had there. ["read cli.py, models.py", "edit cli.py + tests",
    "update README.md, CHANGELOG.md", "run unittest"]; [] when nothing is shared."""
    ph = [_phases(r) for r in runs]
    ph = [p for p in ph if p]
    if not ph:
        return []
    verbs = [[v for v, _ in p] for p in ph]
    score = lambda a: sum(difflib.SequenceMatcher(None, verbs[a], verbs[b], autojunk=False).ratio()
                          for b in range(len(ph)) if b != a)
    base = max(range(len(ph)), key=lambda a: (score(a), -abs(len(ph[a]) - statistics.median(map(len, ph)))))
    share = max(1, COMMON_SHARE * len(ph))
    hits = [[ph[base][k][1]] for k in range(len(ph[base]))]   # per base phase: the aligned phases' targets
    for b in range(len(ph)):
        if b == base:
            continue
        sm = difflib.SequenceMatcher(None, verbs[base], verbs[b], autojunk=False)
        for blk in sm.get_matching_blocks():
            for d in range(blk.size):
                hits[blk.a + d].append(ph[b][blk.b + d][1])
    out = []
    for (verb, tg), seen in zip(ph[base], hits):
        if len(seen) < share:
            continue
        c = Counter(t for h in seen for t in dict.fromkeys(h))
        keep = [t for t in dict.fromkeys(t for h in seen for t in h) if c[t] >= share] or tg
        code = [t for t in keep if t != "tests"]
        text = verb + (" " + ", ".join(code[:4]) + (" +%d" % (len(code) - 4) if len(code) > 4 else "") if code else "")
        if "tests" in keep:
            text += " + tests" if code else " tests"
        out.append(text)
    return out[:max_steps]


def write_stub(rep, name, cache=None):
    """Write a SKILL.md draft for a repeated workflow under <cache dir>/skill-drafts/<slug>/; returns its path."""
    runs = [r for _, r in rep["matches"]]
    sl = slug(name)
    d = os.path.join(cache or cache_dir(), "skill-drafts", sl)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "SKILL.md")
    n = len(runs)
    fc = Counter(f for r in runs for f in dict.fromkeys(r["files"]))
    cmds = Counter(c for r in runs for x in r["steps"] for c in dict.fromkeys(x["cmds"])
                   if len(c) > 1 and c.split()[0] not in UTILS)
    lines = ["---", "name: %s" % sl,
             "description: %s. Drafted by tokenscope from %d similar past requests in %d sessions; review before use."
             % (name[:1].upper() + name[1:], n, len(rep["sessions"])), "---", "",
             "# %s" % name, "",
             "Draft only: tokenscope saw this workflow %d times (median %s CW each) and wrote this stub from the step "
             "labels, files and commands of those runs. Edit it, then move it into a skills directory yourself."
             % (n, _fk(statistics.median(r["cw"] for r in runs))), "",
             "## Steps", ""]
    seq = common_steps(runs) or [p for p in rep["seq"].split(" → ") if p != "…"]
    lines += ["%d. %s" % (k + 1, p) for k, p in enumerate(seq)]
    lines += ["", "## Files (start here instead of searching)", ""]
    lines += ["- %s (in %d of %d runs)" % (f, fc[f], n) for f in rep["files"][:15]] or ["- (no common files)"]
    lines += ["", "## Commands seen", ""]
    lines += ["- %s (%d runs)" % (c, k) for c, k in cmds.most_common(8)] or ["- (none)"]
    lines += ["", "## Step labels of one past run", ""]
    lines += ["- [%s] %s" % (x["cat"].upper(), x["label"]) for x in runs[0]["steps"][:12]]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def shown_path(p, cache=None, home=None):
    """A stub path as shown, never an absolute machine path: under home "~/...", else relative to the cache dir
    "<cache>/skill-drafts/<slug>/SKILL.md"."""
    if not p:
        return p
    p = os.path.abspath(p)
    h = os.path.abspath(home or os.path.expanduser("~"))
    if h != os.sep and p.startswith(h + os.sep):
        return "~" + p[len(h):]
    c = os.path.abspath(cache or cache_dir())
    rel = os.path.relpath(p, c) if p.startswith(c + os.sep) else os.path.join(
        "skill-drafts", os.path.basename(os.path.dirname(p)), os.path.basename(p))
    return "<cache>/" + rel.replace(os.sep, "/")


def suggest(cur_recs, history, skills=(), used_skills=(), write=True, cache=None, idf=None):
    """At most one cross-session hint for the current request (`cur_recs` = current_records(...)), ranked by
    estimated saving; None when nothing passes the gates. `history`: records of OTHER sessions."""
    used = set(used_skills)
    for r in cur_recs:
        used |= set(r["skills"])
    best = None
    for cur in cur_recs:
        rep = find_repeat(cur, history, idf if idf is not None else file_idf(history))
        if rep and rep["saving"] >= MIN_SAVING and (best is None or rep["saving"] > best["saving"]):
            best = rep
    if best is None:
        return None
    runs = [r for _, r in best["matches"]]
    cur = best["cur"]
    n, ns = len(runs), len(best["sessions"])
    typ = statistics.median(r["cw"] for r in runs)
    est = (("median CW the %d matching runs spent in investigate steps on the %d files they share (%s), what a "
            "skill with the file map and steps would skip; not the whole run" % (n, len(best["files"]),
                                                                                 ", ".join(best["files"][:3]))))
    matches = [{"sid": r["sid"][:8], "level": r["level"], "label": r["label"] or "; ".join(
        x["label"] for x in r["steps"][:3]), "cw": r["cw"], "score": round(sc, 2)} for sc, r in best["matches"][:8]]
    without = sum(1 for r in runs if not used & set(r["skills"]))
    # a skill this request used that most matching runs used too: the skill is there, the file map is not
    sc_used = Counter(n for r in runs for n in set(r["skills"]))
    drive = [k for k in cur["skills"] if sc_used[k] >= COMMON_SHARE * n]
    sk = None if drive else match_skill(workflow_words(runs + [cur]), skills, used)
    hint = {"rule": "repeat", "kind": "cross_session", "cw_at_stake": int(round(best["saving"])),
            "est_saving_cw": int(round(best["saving"])), "estimate": "estimate: " + est, "skill": None,
            "stub_path": None, "matches": matches, "seq": best["seq"]}
    if drive:
        k = sc_used[drive[0]]
        hint.update(rule="skill_filemap", skill=drive[0],
                    action="Add a file map to skill /%s (%s)" % (drive[0], ", ".join(best["files"][:3])),
                    evidence="≈%s/run saving · /%s in %d of %d runs, %d sessions · ≈%s CW each · they re-read the "
                             "same files; listing them in the skill saves that (estimate)"
                             % (_fk(best["saving"]), drive[0], k, n, ns, _fk(typ)))
    elif sk:
        s, _, common = sk
        desc = s["description"][:90] + ("…" if len(s["description"]) > 90 else "")
        hint.update(rule="existing_skill", skill=s["name"],
                    action="Use skill /%s for this" % s["name"],
                    evidence='≈%s/run saving · done %d× without it in %d sessions · ≈%s CW each · you have skill %s '
                             '("%s") that covers this (estimate)' % (_fk(best["saving"]), without, ns, _fk(typ),
                                                                     s["name"], desc),
                    skill_words=common)
    else:
        name = _name(runs + [cur], best["files"])
        hint.update(action='Make "%s" a skill' % name,
                    evidence="≈%s/run saving · done %d× in %d sessions · ≈%s CW each · %s (estimate)"
                             % (_fk(best["saving"]), n, ns, _fk(typ), best["seq"]))
        if write:
            try:
                hint["stub_path"] = shown_path(write_stub(best, name, cache), cache)
            except OSError:
                pass
    hint["text"] = hint["action"]
    return hint


class Suggester:
    """The live server's cross-session hints: an XIndex built in the background, skills per project dir."""

    def __init__(self, root=None, skills_dirs=(), cache=None, refresh_s=600):
        self.index = XIndex(root, cache)
        self.extra = list(skills_dirs or ())
        self.cache = cache
        self.refresh_s = refresh_s
        self.idf = {}
        self._skills = {}
        self.version = 0          # bumped on every (re)build: the live recap cache keys on it

    def refresh(self):
        self.index.build()
        self.idf = file_idf(self.index.records())
        self._skills = {}
        self.version += 1

    def run(self, stop):
        while not stop.is_set():
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001 - the pane works without cross-session hints
                print("tokenscope live: cross-session index: %r" % (e,), file=sys.stderr)
            stop.wait(self.refresh_s)

    def skills(self, cwd):
        if cwd not in self._skills:
            self._skills[cwd] = load_skills(skill_dirs(cwd, self.extra))
        return self._skills[cwd]

    def __call__(self, sid, project, cwd, steps, groups, used_skills=(), prompts=()):
        if not self.index.ready:
            return None
        cur = current_records(steps, groups, sid, project, prompts)
        return suggest(cur, self.index.records(exclude_sid=sid), self.skills(cwd), used_skills, cache=self.cache,
                       idf=self.idf)
