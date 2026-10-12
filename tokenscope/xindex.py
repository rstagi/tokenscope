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
     "cw", "usd", "dur_s", "cw_cat": {category: CW}, "turns": main-thread turns,
     "disc": discovery CW, "disc_by": {search|read|tools|subagent: CW}, "disc_files": files its discovery read,
     "disc_items": [[kind, cw, step, read basename | names a search's output showed]],
     "steps": [{"cat", "label", "files", "cmds", "cw"}]}

Records are cached per transcript under <cache dir>/xindex/ (keyed by path + size + mtime of the transcript and its
subagent transcripts) and rebuilt incrementally: only new or changed transcripts are replayed.

Matching (`suggest`): the current request (and its groups) against records of OTHER sessions only: similarity =
W_TOK × word similarity + W_SEQ × category-sequence similarity + W_TOOL × tool / file overlap (WP_* weights and
prompt-word Dice for two requests with prompts, each word weighted by its idf over the index so boilerplate an app
injects into every prompt weighs ~0 (word_idf), else label-token Jaccard), at least SIM_MIN, with an anchor (shared
words or shared files). A hint needs >= MIN_OCC prior matches in >= MIN_SESSIONS sessions, each >= PRIOR_MIN_CW, and
the current one >= CUR_MIN_CW.

Saving (an estimate): net = median removable discovery − median context cost of the remedy, per run. Discovery
(indexed per request: `disc`, its calls in `disc_items`): listing / search calls (Glob, Grep, LS, Bash ls / find /
tree / grep / rg ...), ToolSearch, exploration subagents and full reads of files the run does not edit (a file it
edits must be read anyway); a call's CW is its turn's CW split per call, as steps attribute it. Removable
(`removable`): only what the remedy's content would have short-circuited: full reads of the files it lists (a file
map's files, a drafted skill's "## Files", the files an existing skill's SKILL.md names) and the searches /
listings that led to them (their output names one, or they come before the first read of one in the same step);
other searches, ToolSearch and subagents stay out. The cost: what the remedy puts in context (characters /
CHARS_PER_TOKEN), cost-weighted at one cache write plus a cache read on each later turn: a skill's body for the
run's turns, a CLAUDE.md file map for every turn of every session of the project (spread over the runs). A
token-saving hint needs net >= MIN_NET and >= MIN_NET_SHARE of the removable discovery, and a substantive remedy: a
file map of >= MIN_MAP_FILES stably found files, a drafted skill with >= 2 shared steps and >= MIN_SKILL_FILES files.
A repeated workflow with no net saving (<= 0) whose steps repeat in >= CONSISTENCY_MIN runs gets "make it a skill
for consistency".

Existing skills (`load_skills`): SKILL.md files (frontmatter name / description, plain or a YAML block scalar
`>-` / `|`, + the first lines; the files the body names) from
~/.claude/skills, <project>/.claude/skills and extra dirs. A repeated workflow matching a skill the session did not
use by word overlap (conservative, no model) becomes "Use skill /<name>" instead of "Make ... a skill".

Evidence leads with both terms ("≈41k discovery/run − ≈9k skill body → ≈32k/run net · done 4× in 4 sessions · ..."),
so a narrow pane cuts the words, never the numbers.

Drafts: a CLAUDE.md file-map hint writes its snippet to <cache dir>/claude-md-drafts/<slug>.md; a "make it a skill"
hint writes a SKILL.md stub to <cache dir>/skill-drafts/<slug>/SKILL.md (never into a
skills directory; slug: whole words, up to 40 characters) and names it in `stub_path`, shown as "~/..." under home,
else "<cache>/skill-drafts/<slug>/SKILL.md" (never an absolute machine path). Its "## Steps" are the ordered steps the
matched runs share (common_steps).
"""
import bisect
import difflib
import glob
import hashlib
import json
import math
import os
import re
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict

from . import intents
from . import pricing

CACHE_VERSION = 9
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
STRONG_PTOK = 0.7             # two prompts this alike (idf-weighted Dice) are the same task: a match whatever the
STRONG_PTOK_W = 6.0           # ... tools / files, given at least this much shared word weight (~6 content words)
STRONG_TASK_SHARE = 0.6       # ... and, on both sides, at least this share of the prompt's words weigh >=
STRONG_TASK_W = 0.9           # ... this globally (the prompt is mostly task words, not a shared preamble)
IDF_MAX = 5.0                 # weight of a file the index has never seen
# gates
MIN_OCC = 3                   # prior matching occurrences
MIN_SESSIONS = 2              # ... in at least this many distinct sessions
PRIOR_MIN_CW = 50_000         # each prior occurrence
CUR_MIN_CW = 50_000           # the current request / group
MIN_NET = 20_000              # net saving per run (median discovery - median context cost of the remedy) ...
MIN_NET_SHARE = 0.3           # ... and at least this share of the median discovery
CONSISTENCY_MIN = 3           # prior runs sharing the step sequence for a "skill for consistency" hint (no saving)
COMMON_SHARE = 0.5            # a file is part of the workflow's file map when >= this share of the runs touch it
CHARS_PER_TOKEN = 4           # token estimate of a draft / skill body / file map
FILEMAP_MAX = 12              # files in a CLAUDE.md file map
STUB_FILES = 15               # files in a drafted SKILL.md
# discovery (what a skill / file map given up front would spare): listing / search calls, full reads of files the
# run does not edit later, ToolSearch, exploration subagents
LIST_PROGS = {"ls", "find", "tree", "fd", "fdfind", "grep", "egrep", "rg", "ag", "locate", "exa", "eza"}
SEARCH_TOOLS = {"Glob", "Grep", "LS"}
EXPLORE_INTENTS = {"locate", "read_code", "read_docs", "discover_tools", "gather_context"}
# existing skills
SK_MIN_OVERLAP = 3            # shared words between a skill (name + description) and the workflow
SK_MIN_COVER = 0.3            # ... and at least this share of the smaller word set
MAX_STEPS = 200
MAX_ITEMS = 400               # discovery calls kept per record
NAMES_MAX = 40                # file names kept per search call
MIN_MAP_FILES = 3             # a file map (CLAUDE.md, in a skill) lists at least this many stable files ...
MIN_SKILL_FILES = 3           # ... a drafted skill at least this many files
# prompt words: idf over the indexed requests with prompts; a word in >= WORD_DF_ZERO of them (boilerplate an app
# puts in every prompt) weighs 0, one in <= WORD_DF_FULL of them 1, log-linear between; with fewer than WORD_IDF_MIN
# requests every word weighs 1 (too few to tell boilerplate from the task). The same cutoffs over one project's
# requests (with >= WORD_IDF_PROJ_MIN of them) give per-project weights; a word weighs the lower of the two
WORD_DF_ZERO = 0.2
WORD_DF_FULL = 0.04
WORD_IDF_MIN = 30
WORD_IDF_PROJ_MIN = 8

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


NAME_RE = re.compile(r"[^\s:\"'`()<>\[\]{},;=|*]+")


def named_files(text, max_names=300):
    """Basenames of the files a text names (a listing / search output: paths, "path:line:" matches, bare `ls` names;
    a SKILL.md's paths), each once, in order, at most `max_names`; urls dropped."""
    out = {}
    for tok in NAME_RE.findall(re.sub(r"\b\w+://\S+", " ", str(text or ""))):
        base = tok.rstrip(".").rsplit("/", 1)[-1]
        ext = os.path.splitext(base)[1]
        if 2 < len(ext) <= 9 and re.match(r"\.[A-Za-z]", ext) and base not in out:
            out[base] = None
            if len(out) >= max_names:
                break
    return list(out)


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
           "cw_cat": {k: round(v) for k, v in cw_cat.items()}, "turns": _turns(sub),
           "steps": [{"cat": x["cat"], "label": str(x.get("label") or "")[:80], "source": x.get("source"),
                      "tested": x.get("tested"), "runners": [r.replace("Bash ", "", 1) for r in x.get("runners") or ()][:2],
                      "files": (x.get("files") or [])[:6], "cmds": (x.get("cmds") or [])[:4], "cw": round(x.get("cw") or 0)} for x in sub[:MAX_STEPS]]}
    rec.update(discovery(sub))
    return retoken(rec)


def _turns(sub):
    """Main-thread turns (API calls) of a run of steps: the span of its turns, else the steps' turn counts."""
    if sub and sub[0].get("turn_first") is not None and sub[-1].get("turn_last") is not None:
        return sub[-1]["turn_last"] - sub[0]["turn_first"] + 1
    return sum(x.get("turns") or 1 for x in sub)


def discovery(sub):
    """{"disc", "disc_by", "disc_files", "disc_items"}: the CW a run of steps spent finding its way (see
    attach_discovery): listing / search calls, ToolSearch, exploration subagents, and full reads of files it does not
    edit (a file the agent then edits has to be read anyway; a re-read after the edit checks it). `disc_files`: the
    files those reads found (paths relative to the project when under it). `disc_items`: the calls in order,
    [kind, cw, step index in the run, read: basename / search: basenames its output named] (removable() weighs them
    against a remedy's files)."""
    edits = {e["path"] for x in sub for e in x.get("edits") or ()}
    by, files, items = Counter(), [], []
    for si, x in enumerate(sub):
        for d in x.get("disc") or ():
            if d["kind"] == "read" and d["path"] in edits:
                continue
            by[d["kind"]] += d["cw"]
            if d["kind"] == "read" and d.get("shown") and d["shown"] not in files:
                files.append(d["shown"])
            if len(items) < MAX_ITEMS:
                items.append([d["kind"], round(d["cw"]), si, os.path.basename(d.get("shown") or d.get("path") or "")
                              if d["kind"] == "read" else list(d.get("names") or ())[:NAMES_MAX]])
    return {"disc": round(sum(by.values())), "disc_by": {k: round(v) for k, v in by.items()},
            "disc_files": files[:40], "disc_items": items}


def removable(rec, files):
    """See removable_parts: the reads + searches."""
    p = removable_parts(rec, files)
    return p["read"] + p["search"]


def removable_parts(rec, files):
    """The discovery CW of a run that a remedy listing `files` would have short-circuited: full reads of those files
    (not edited: discovery() already left those out) and the searches / listings that led to them, i.e. whose output
    names one of them, or that come before the first read of one in the same step. Other searches, ToolSearch and
    subagents stay: knowing these files would not have spared them. Files compare by basename.
    {"read", "search": the removable CW, "kept": the rest of the run's discovery}."""
    fs = {os.path.basename(f) for f in files or ()}
    items = rec.get("disc_items") or ()
    first = {}   # step -> index of its first read of a listed file
    for k, (kind, _, si, what) in enumerate(items):
        if kind == "read" and what in fs:
            first.setdefault(si, k)
    out = {"read": 0, "search": 0, "kept": 0}
    for k, (kind, cw, si, what) in enumerate(items):
        if kind == "read" and what in fs or kind == "search" and (fs & set(what) or k < first.get(si, -1)):
            out[kind] += cw
        else:
            out["kept"] += cw
    return out


def call_facts(state, s):
    """Per main-thread call that discovers ({"kind": search|read|tools|subagent, "path", "shown", "cw", "k",
    "part"}) and per edit ({"path", "k", "part"}). A call's CW is its turn's CW split evenly over the turn's calls
    (declare_intent not counted), as steps attribute it (steps._parts); an exploration subagent (Explore type, or
    one that only located / read) adds its own CW, as the step that launched it does."""
    from .live.state import EDIT_TOOLS, _full_read
    from . import steps as stepsmod
    by_tool = {a["tool_use_id"]: a for a in s.agents.values() if a.get("tool_use_id")}
    cwd = s.cwd or ""
    disc, edits = [], []

    def norm(p):
        p = str(p or "")
        return os.path.normpath(os.path.join(cwd, p)) if p and cwd and not os.path.isabs(p) else p

    def shown(p):
        return os.path.relpath(p, cwd) if cwd and p.startswith(cwd.rstrip(os.sep) + os.sep) else os.path.basename(p)

    def explores(a):
        if "explore" in str(a.get("type") or "").lower():
            return True
        br = s.branches.get(a["id"])
        calls = [x for t in (br.turn_steps if br else ()) for x in t["tools"]]
        return bool(calls) and all(x.get("intent") in EXPLORE_INTENTS for x in calls)

    for k, t in enumerate(s.main.turn_steps):
        raw = t.get("_raw") or []
        facts = t.get("_facts") or []
        names = [x["name"] for x in raw]
        parts = stepsmod.call_parts(names)
        n = len([x for x in names if not stepsmod.is_declare(x)]) or 1
        cw = float(t.get("cw") or 0) / n
        for j, (x, part, tool) in enumerate(zip(raw, parts, t["tools"])):
            name, inp = x["name"], x.get("input") or {}
            rec = intents.record(name, inp)
            f = norm(rec.get("file"))
            kind, extra = None, 0.0
            if name in EDIT_TOOLS:
                if f:
                    edits.append({"path": f, "k": k, "part": part})
                continue
            if name in SEARCH_TOOLS or (name == "Bash" and stepsmod.program(rec.get("cmd") or "") in LIST_PROGS):
                kind = "search"
            elif name == "ToolSearch":
                kind = "tools"
            elif f and _full_read(name, inp, rec) and project_file(os.path.basename(f)):
                kind = "read"   # not a background task's output capture (polling) nor a device
            elif name in ("Agent", "Task"):
                a = by_tool.get(tool.get("id"))
                if a is not None and explores(a):
                    kind, extra = "subagent", float(a.get("cw") or 0)
            if kind:
                disc.append({"kind": kind, "path": f if kind == "read" else "", "shown": shown(f) if kind == "read"
                             else "", "cw": cw + extra, "k": k, "part": part,
                             "names": (facts[j].get("names") or [])[:NAMES_MAX] if kind == "search"
                             and j < len(facts) else []})
    return disc, edits


def attach_discovery(steps, disc, edits):
    """Hang call_facts' items on the steps holding their calls (step["disc"], step["edits"])."""
    keys = [(x.get("turn_first", x["i"]), x.get("part_first") or 0) for x in steps]
    for x in steps:
        x["disc"], x["edits"] = [], []
    for key, items in (("disc", disc), ("edits", edits)):
        for d in items:
            j = bisect.bisect_right(keys, (d["k"], d.get("part") or 0)) - 1
            if j >= 0:
                steps[j][key].append(d)


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
    """{"sid", "project", "cwd", "ts0", "skills", "records", "spend"} of one main transcript (deterministic labels
    only; "spend": per-call usage by step, for `tokenscope top`, see top.spend_facts)."""
    from .top import spend_facts
    from .live.replay import replay
    state, s = replay(path, redact=False, model_labels=False)
    if s is None or not s.main.turn_steps:
        return None
    steps, gs = state.steps(s, final=True)
    attach_discovery(steps, *call_facts(state, s))
    project = s.project_raw
    return {"sid": s.id, "project": project, "cwd": getattr(s, "cwd", None), "ts0": s.started,
            "skills": sorted(s.skills),
            "records": requests_of(steps, gs, s.id, project, list(zip(s.prompt_at, s.prompt_words))),
            "spend": spend_facts(state, s, steps, gs)}


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
            tmp = "%s.%d-%d.tmp" % (self.cache, os.getpid(), threading.get_ident())   # the live server builds in 2 threads
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


def dice(a, b, w=None, wb=None):
    """Dice of two word sets; with `w` ({word: weight}, unknown words 1) each word counts its weight (`wb`: other
    weights for b's side, e.g. b's project's)."""
    a, b = set(a), set(b)
    if not (a and b):
        return 0.0
    if w is None and wb is None:
        return 2 * len(a & b) / (len(a) + len(b))
    wa, wb = w or {}, wb if wb is not None else (w or {})
    wt = lambda w_, s_: sum(w_.get(t, 1.0) for t in s_)
    den = wt(wa, a) + wt(wb, b)
    return (wt(wa, a & b) + wt(wb, a & b)) / den if den else 0.0


class WordIdf(dict):
    """The global {word: weight} (unlisted words weigh 1) plus `proj`: {project: {word: min(global, per-project
    weight)}} for the projects with enough requests (see word_idf)."""
    proj = {}

    def of(self, project):
        return self.proj.get(project, self)


def _df_weights(reqs, n_min):
    n = len(reqs)
    if n < n_min:
        return {}
    df = Counter(t for r in reqs for t in set(r["tokens"]))
    lo, hi = math.log(1 / WORD_DF_ZERO), math.log(1 / WORD_DF_FULL)
    out = {}
    for t, k in df.items():
        x = (math.log(n / k) - lo) / (hi - lo)
        if x < 1:
            out[t] = max(0.0, x)
    return out


def word_idf(records):
    """WordIdf over the request records with prompts: 0 for a word in >= WORD_DF_ZERO of them (boilerplate: text an
    app injects into every prompt), 1 at <= WORD_DF_FULL, log-linear between; only words below 1 are listed. {} (all
    1) with fewer than WORD_IDF_MIN such requests. The same over each project's requests (>= WORD_IDF_PROJ_MIN of
    them): a word weighs min(global, per-project), so boilerplate common within one project weighs ~0 too."""
    reqs = [r for r in records if r["level"] == "request" and r.get("pwords")]
    out = WordIdf(_df_weights(reqs, WORD_IDF_MIN))
    by_proj = defaultdict(list)
    for r in reqs:
        by_proj[r.get("project")].append(r)
    out.proj = {}
    for project, rs in by_proj.items():
        pw = _df_weights(rs, WORD_IDF_PROJ_MIN)
        if pw:
            out.proj[project] = {t: min(out.get(t, 1.0), pw.get(t, 1.0)) for t in set(out) | set(pw)}
    return out


def _wfor(widf, rec):
    """The word weights for `rec`'s prompt words: its project's (WordIdf), else `widf` as is (None: all 1)."""
    return widf.of(rec.get("project")) if isinstance(widf, WordIdf) else (widf or None)


def by_prompt(a, b):
    return bool(a.get("pwords")) and bool(b.get("pwords"))


def token_sim(a, b, widf=None):
    """(similarity, anchor threshold) of two records' words: Dice of two requests' prompt words (a prompt carries
    task-specific words too: "list the items with the highest stock value"), each word weighted by `widf`
    (word_idf: boilerplate words weigh ~0; each side by its project's weights), else label-token Jaccard."""
    return (dice(a["tokens"], b["tokens"], _wfor(widf, a), _wfor(widf, b)), ANCHOR_PTOK) if by_prompt(a, b) else \
        (jaccard(a["tokens"], b["tokens"]), ANCHOR_TOK)


def shared_word_weight(a, b, widf=None):
    """The weight of the prompt words a and b share (the mean of the two sides' weights)."""
    shared = set(a["tokens"]) & set(b["tokens"])
    wa, wb = _wfor(widf, a) or {}, _wfor(widf, b) or {}
    return sum(wa.get(t, 1.0) + wb.get(t, 1.0) for t in shared) / 2


def task_share(rec, widf=None):
    """The share of `rec`'s prompt words weighing >= STRONG_TASK_W globally (a WordIdf's own weights, not its
    project's): ~1 for a prompt that is all task, low for one mostly made of a preamble other prompts share."""
    toks = set(rec["tokens"])
    w = widf or {}
    return sum(w.get(t, 1.0) >= STRONG_TASK_W for t in toks) / len(toks) if toks else 0.0


def strong_prompt_match(a, b, widf=None):
    """Two prompts alike enough to be the same task whatever the tools / files: Dice >= STRONG_PTOK over >=
    STRONG_PTOK_W shared word weight, both prompts mostly task words (task_share >= STRONG_TASK_SHARE)."""
    return (by_prompt(a, b) and token_sim(a, b, widf)[0] >= STRONG_PTOK
            and shared_word_weight(a, b, widf) >= STRONG_PTOK_W
            and min(task_share(a, widf), task_share(b, widf)) >= STRONG_TASK_SHARE)


def similarity(a, b, idf=None, widf=None):
    """(score, parts): W_TOK × word similarity (token_sim) + W_SEQ × category-sequence similarity + W_TOOL × tool /
    file overlap (tool calls 0.4, file types 0.1, files 0.5); WP_* weights for two requests with prompts. Without
    words on either side the score is the other two parts, renormalised."""
    jt, _ = token_sim(a, b, widf)
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


def is_match(a, b, idf=None, widf=None):
    """(matches?, score): similarity >= SIM_MIN with an anchor: shared words (token_sim >= ANCHOR_TOK, or
    ANCHOR_PTOK for prompts), or >= ANCHOR_N_FILES shared files with an overlap >= ANCHOR_FILES. Two prompts with
    Dice >= STRONG_PTOK over >= STRONG_PTOK_W shared word weight, each mostly task words, match regardless of the
    tools / files (the same task done another way: a git ls-files + cat instead of many Reads; strong_prompt_match)."""
    jt, anchor = token_sim(a, b, widf)
    if strong_prompt_match(a, b, widf):
        return True, similarity(a, b, idf, widf)[0]
    fo, shared = file_overlap(a["files"], b["files"], idf)
    if jt < anchor and (fo < ANCHOR_FILES or len(shared) < ANCHOR_N_FILES):
        return False, 0.0
    sc, _ = similarity(a, b, idf, widf)
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
    meta, lines = {}, m.group(1).splitlines()
    k = 0
    while k < len(lines):
        mm = re.match(r"([A-Za-z_-]+)\s*:\s*(.*)", lines[k])
        k += 1
        if not mm:
            continue
        val = mm.group(2).strip()
        if re.match(r"[>|][-+]?\d?\s*(?:#.*)?$", val):   # a YAML block scalar: the indented lines that follow
            block = []
            while k < len(lines) and (not lines[k].strip() or lines[k][:1] in " \t"):
                block.append(lines[k].strip())
                k += 1
            sep = "\n" if val[0] == "|" else " "
            val = sep.join(block).strip()
            val = re.sub(r" ?\n ?", "\n", val) if sep == "\n" else re.sub(r"\s+", " ", val)
        else:
            val = val.strip("\"'")
        meta[mm.group(1).lower()] = val
    return meta, m.group(2)


def skill_dirs(cwd=None, extra=()):
    out = [os.path.join(os.path.expanduser("~"), ".claude", "skills")]
    if cwd:
        out.append(os.path.join(cwd, ".claude", "skills"))
    return out + [os.path.abspath(os.path.expanduser(d)) for d in extra or ()]


def load_skills(dirs):
    """[{"name", "description", "path", "files", "tokens", ...}] of the SKILL.md files under `dirs` (first one per
    name wins; `files`: the files its body names, what using it spares finding)."""
    out, seen = [], set()
    for d in dirs:
        for p in sorted(glob.glob(os.path.join(d, "*", "SKILL.md")) + glob.glob(os.path.join(d, "SKILL.md"))):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    full = f.read(64_000)
            except OSError:
                continue
            text = full[:8000]
            meta, body = _frontmatter(text)
            name = meta.get("name") or os.path.basename(os.path.dirname(p))
            if name in seen:
                continue
            seen.add(name)
            desc = " ".join((meta.get("description") or "").split())
            head = " ".join(ln for ln in body.splitlines()[:8] if ln.strip() and not ln.startswith("```"))
            try:
                size = os.path.getsize(p)
            except OSError:
                size = len(text)
            out.append({"name": name, "description": desc, "path": p,
                        "files": [f for f in named_files(skill_body(full)) if project_file(f)],
                        "body_tokens": est_tokens(" " * max(size - (len(text) - len(body)), 0)),
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
    if n < 0:
        return "-" + _fk(-n)
    return "%.1fM" % (n / 1e6) if n >= 1e6 else "%dk" % round(n / 1e3) if n >= 999.5 else "%d" % round(n)


def find_repeat(cur, history, idf=None, widf=None):
    """Prior matches of `cur` from `history` (records of other sessions) passing the gates, else None:
    {"cur", "matches": [(score, rec)], "sessions", "files", "disc", "seq"} (`disc`: median discovery CW per run)."""
    if cur["cw"] < CUR_MIN_CW:
        return None
    m = []
    for r in history:
        if r["level"] != cur["level"] or r["sid"] == cur["sid"] or r["cw"] < PRIOR_MIN_CW:
            continue
        ok, sc = is_match(cur, r, idf, widf)
        if ok:
            m.append((sc, r))
    sessions = {r["sid"] for _, r in m}
    if len(m) < MIN_OCC or len(sessions) < MIN_SESSIONS:
        return None
    m.sort(key=lambda x: -x[0])
    runs = [r for _, r in m]
    files = common_files(runs + [cur])
    return {"cur": cur, "matches": m, "sessions": sessions, "files": files,
            "disc": statistics.median(r.get("disc") or 0 for r in runs), "seq": step_sequence(cur, files)}


def est_tokens(text):
    """Token estimate of a text loaded into context (a skill body, a file map): characters / CHARS_PER_TOKEN."""
    return int(math.ceil(len(text or "") / CHARS_PER_TOKEN))


def context_cost(tokens_, turns):
    """Cost-weighted tokens of `tokens_` sitting in context for `turns` turns: written to the cache once, then read
    on each later turn (pricing.W_CACHE_WRITE + pricing.W_CACHE_READ x the remaining turns)."""
    return tokens_ * (pricing.W_CACHE_WRITE + pricing.W_CACHE_READ * max(turns - 1, 0))


def net_of(runs, tokens_, files, turns_of=None):
    """(median removable discovery, median context cost, net) per run of a remedy that lists `files` (removable())
    and puts `tokens_` in context for each run's turns (`turns_of(run)`, default the run's main-thread turns)."""
    disc = statistics.median(removable(r, files) for r in runs)
    cost = statistics.median(context_cost(tokens_, (turns_of or (lambda r: r.get("turns") or 1))(r)) for r in runs)
    return disc, cost, disc - cost


def passes(disc, net):
    return net >= MIN_NET and net >= MIN_NET_SHARE * disc


def discovered_files(runs):
    """(files the runs' discovery found, stable?): files found by >= COMMON_SHARE of the runs (>= 2), most found
    first; stable when >= COMMON_SHARE of the runs found most (>= 3/4) of them."""
    n = len(runs)
    c = Counter(f for r in runs for f in dict.fromkeys(r.get("disc_files") or ()))
    fs = [f for f, k in c.most_common() if k >= max(2, COMMON_SHARE * n)][:FILEMAP_MAX]
    if not fs:
        return [], False
    found = sum(1 for r in runs if len(set(fs) & set(r.get("disc_files") or ())) >= 0.75 * len(fs))
    return fs, found >= COMMON_SHARE * n


def _role(f, runs):
    b = os.path.basename(f).lower()
    k = sum(1 for r in runs if f in (r.get("disc_files") or ()))
    role = ("tests" if _is_test_file(b) else "docs" if _ext(b) in DOC_EXTS else
            "project config" if b in ("package.json", "pyproject.toml", "setup.cfg", "cargo.toml", "go.mod",
                                      "tsconfig.json", "makefile") else "source")
    return "%s; read to get oriented in %d of %d past runs" % (role, k, len(runs))


def filemap_text(name, files, runs):
    """A short CLAUDE.md section: the stable discovered files with a one-line role each."""
    lines = ["## File map: %s" % name, "", "Start here instead of searching (drafted by tokenscope from %d past runs; "
             "fix the roles before pasting into CLAUDE.md)." % len(runs), ""]
    lines += ["- `%s`: %s" % (f, _role(f, runs)) for f in files]
    return "\n".join(lines) + "\n"


def project_turns(history, projects):
    """(main-thread turns, sessions) of the indexed requests in `projects`: what a CLAUDE.md line is paid on."""
    reqs = [r for r in history if r["level"] == "request" and r.get("project") in projects]
    return sum(r.get("turns") or 1 for r in reqs), len({r["sid"] for r in reqs})


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


def stub_text(rep, name):
    """The SKILL.md draft of a repeated workflow (frontmatter, steps, file map with counts, commands, one run's
    step labels)."""
    runs = [r for _, r in rep["matches"]]
    sl = slug(name)
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
    lines += ["- %s (in %d of %d runs)" % (f, fc[f], n) for f in rep["files"][:STUB_FILES]] or ["- (no common files)"]
    lines += ["", "## Commands seen", ""]
    lines += ["- %s (%d runs)" % (c, k) for c, k in cmds.most_common(8)] or ["- (none)"]
    lines += ["", "## Step labels of one past run", ""]
    lines += ["- [%s] %s" % (x["cat"].upper(), x["label"]) for x in runs[0]["steps"][:12]]
    return "\n".join(lines) + "\n"


def skill_body(text):
    """What loading a skill puts in context: the SKILL.md after its frontmatter."""
    return _frontmatter(text)[1]


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def write_stub(rep, name, cache=None, text=None):
    """Write a SKILL.md draft for a repeated workflow under <cache dir>/skill-drafts/<slug>/; returns its path."""
    return _write(os.path.join(cache or cache_dir(), "skill-drafts", slug(name), "SKILL.md"),
                  text or stub_text(rep, name))


def write_filemap(name, text, cache=None):
    """Write a CLAUDE.md file-map draft to <cache dir>/claude-md-drafts/<slug>.md; returns its path."""
    return _write(os.path.join(cache or cache_dir(), "claude-md-drafts", slug(name) + ".md"), text)


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
        "skill-drafts", os.path.basename(os.path.dirname(p)), os.path.basename(p)) \
        if os.path.basename(p) == "SKILL.md" else os.path.join("claude-md-drafts", os.path.basename(p))
    return "<cache>/" + rel.replace(os.sep, "/")


def _subsequence(small, big):
    it = iter(big)
    return all(any(v == w for w in it) for v in small)


def remedies(rep, history, skills=(), used=()):
    """The remedies for one repeated workflow, each with its per-run numbers {"rule", "disc", "cost", "net", "ok",
    "what"} (`disc`: median discovery CW of the matched runs; `cost`: median cost-weighted tokens the remedy puts in
    context; `ok`: passes the net gates): `skill_filemap` (a skill the runs use: a file map added to it), else
    `existing_skill` (an unused skill that covers it: its body), else `repeat` (the drafted SKILL.md's body) and
    `claude_md` (a file map of the stable discovered files, in context every turn of every session of the project)."""
    runs = [r for _, r in rep["matches"]]
    cur = rep["cur"]
    n = len(runs)
    sc_used = Counter(k for r in runs for k in set(r["skills"]))
    drive = [k for k in cur["skills"] if sc_used[k] >= COMMON_SHARE * n]
    fs, stable = discovered_files(runs)
    out = []
    if drive:
        text = filemap_text("/" + drive[0], fs, runs)
        d, c, net = net_of(runs, est_tokens(text), fs)   # the skill grows by the map: paid in each run that loads it
        out.append({"rule": "skill_filemap", "skill": drive[0], "uses": sc_used[drive[0]], "files": fs,
                    "stable": stable, "what": "file map in the skill", "disc": d, "cost": c, "net": net})
    else:
        sk = match_skill(workflow_words(runs + [cur]), skills, used)
        if sk:
            files = sk[0].get("files") or []
            d, c, net = net_of(runs, sk[0].get("body_tokens") or 0, files)
            out.append({"rule": "existing_skill", "skill": sk[0]["name"], "match": sk, "files": files,
                        "what": "skill body", "disc": d, "cost": c, "net": net})
        else:
            name = _name(runs + [cur], rep["files"])
            text = stub_text(rep, name)
            files = rep["files"][:STUB_FILES]   # what the draft's "## Files" lists
            d, c, net = net_of(runs, est_tokens(skill_body(text)), files)
            out.append({"rule": "repeat", "name": name, "text": text, "files": files, "what": "skill body",
                        "disc": d, "cost": c, "net": net})
            if fs:
                ftext = filemap_text(name, fs, runs)
                turns, sessions = project_turns(history, {r.get("project") for r in runs + [cur]})
                total = est_tokens(ftext) * (pricing.W_CACHE_WRITE * max(sessions, 1)
                                             + pricing.W_CACHE_READ * turns)
                c = total / n   # every turn of every session of the project, spread over this workflow's runs
                d = statistics.median(removable(r, fs) for r in runs)
                out.append({"rule": "claude_md", "name": name, "text": ftext, "files": fs, "stable": stable,
                            "what": "file map in CLAUDE.md", "disc": d, "cost": c, "net": d - c})
    steps_ = len(common_steps(runs)) >= 2   # one shared phase ("delegate") is no workflow to write down
    for o in out:
        o["ok"] = passes(o["disc"], o["net"]) and substantive(o, steps_)
    return out


def substantive(o, shared_steps):
    """A remedy with something to say: a file map (CLAUDE.md or in a skill) lists >= MIN_MAP_FILES files the runs
    find stably; a drafted skill has >= 2 steps the runs share and lists >= MIN_SKILL_FILES files."""
    if o["rule"] in ("claude_md", "skill_filemap"):
        return o["stable"] and len(o["files"]) >= MIN_MAP_FILES
    if o["rule"] == "repeat":
        return shared_steps and len(o["files"]) >= MIN_SKILL_FILES
    return True


def pick(opts):
    """The remedy to suggest: the file map when it passes and nets more than the skill, else the best passing one;
    None when none passes."""
    ok = [o for o in opts if o["ok"]]
    return max(ok, key=lambda o: (o["net"], o["rule"] == "claude_md")) if ok else None


def consistency(rep, opts):
    """A no-saving "make it a skill for consistency" candidate: the runs repeat the same >= 2 steps (>=
    CONSISTENCY_MIN runs have them in order) and no remedy nets a saving. Else None."""
    if any(o["net"] > 0 for o in opts) or not any(o["rule"] == "repeat" for o in opts):
        return None
    runs = [r for _, r in rep["matches"]]
    seq = common_steps(runs)
    if len(seq) < 2:
        return None
    verbs = [p.split()[0] for p in seq]
    k = sum(1 for r in runs if _subsequence(verbs, [v for v, _ in _phases(r)]))
    if k < CONSISTENCY_MIN:
        return None
    o = next(o for o in opts if o["rule"] == "repeat")
    return dict(o, rule="consistency", steps=seq, repeats=k)


def suggest(cur_recs, history, skills=(), used_skills=(), write=True, cache=None, idf=None, trace=None, widf=None):
    """At most one cross-session hint for the current request (`cur_recs` = current_records(...)): the remedy with
    the best net saving per run (see remedies; gates MIN_NET and MIN_NET_SHARE), else a "skill for consistency"
    hint, else None. `history`: records of OTHER sessions. `trace`: a list that gets every candidate's remedies."""
    used = set(used_skills)
    for r in cur_recs:
        used |= set(r["skills"])
    idf = idf if idf is not None else file_idf(history)
    widf = widf if widf is not None else word_idf(history)
    best = cons = None
    for cur in cur_recs:
        rep = find_repeat(cur, history, idf, widf)
        if not rep:
            continue
        opts = remedies(rep, history, skills, used)
        if trace is not None:
            trace.append({"level": cur["level"], "runs": len(rep["matches"]), "opts": [
                dict({k: o.get(k) for k in ("rule", "disc", "cost", "net", "ok", "files")},
                     per_run=[dict(removable_parts(r, o.get("files")), sid=r["sid"][:8], turns=r.get("turns"),
                                   score=round(sc, 2)) for sc, r in rep["matches"]]) for o in opts]})
        o = pick(opts)
        if o and (best is None or o["net"] > best[1]["net"]):
            best = (rep, o)
        c = None if o else consistency(rep, opts)
        if c and (cons is None or c["repeats"] > cons[1]["repeats"]):
            cons = (rep, c)
    best = best or cons
    if best is None:
        return None
    rep, o = best
    runs = [r for _, r in rep["matches"]]
    cur = rep["cur"]
    n, ns = len(runs), len(rep["sessions"])
    typ = statistics.median(r["cw"] for r in runs)
    net = int(round(o["net"])) if o["rule"] != "consistency" else 0
    terms = "≈%s discovery/run − ≈%s %s" % (_fk(o["disc"]), _fk(o["cost"]), o["what"])
    lead = "%s → ≈%s/run net" % (terms, _fk(o["net"]))
    est = ("discovery = median CW per matching run (%d) the %s would have spared: full reads of the %d files it "
           "lists (not edited) and the listing / search calls that led to them; minus the median cost-weighted tokens "
           "it adds to context (characters / %d per token; one cache write + a cache read per later turn of %s)"
           % (n, o["what"], len(o.get("files") or ()), CHARS_PER_TOKEN,
              "every session of the project, spread over the runs" if o["rule"] == "claude_md" else "the run"))
    matches = [{"sid": r["sid"][:8], "level": r["level"], "label": r["label"] or "; ".join(
        x["label"] for x in r["steps"][:3]), "cw": r["cw"], "score": round(sc, 2)} for sc, r in rep["matches"][:8]]
    hint = {"rule": o["rule"], "kind": "cross_session", "cw_at_stake": net, "est_saving_cw": net,
            "discovery_cw": int(round(o["disc"])), "cost_cw": int(round(o["cost"])), "net_cw": int(round(o["net"])),
            "estimate": "estimate: " + est, "skill": None, "stub_path": None, "matches": matches, "seq": rep["seq"]}
    stub = None
    if o["rule"] == "skill_filemap":
        hint.update(skill=o["skill"], action="Add a file map to skill /%s (%s)" % (o["skill"], ", ".join(
            os.path.basename(f) for f in o["files"][:3])),
            evidence="%s · /%s in %d of %d runs, %d sessions · ≈%s CW each · they re-discover the same files; "
                     "listing them in the skill saves that (estimate)" % (lead, o["skill"], o["uses"], n, ns, _fk(typ)))
    elif o["rule"] == "existing_skill":
        s, _, common = o["match"]
        desc = s["description"][:90] + ("…" if len(s["description"]) > 90 else "")
        without = sum(1 for r in runs if not used & set(r["skills"]))
        hint.update(skill=s["name"], action="Use skill /%s for this" % s["name"], skill_words=common,
                    evidence='%s · done %d× without it in %d sessions · ≈%s CW each · you have skill %s ("%s") that '
                             'covers this (estimate)' % (lead, without, ns, _fk(typ), s["name"], desc))
    elif o["rule"] == "claude_md":
        hint.update(action="Add a file map to CLAUDE.md",
                    evidence="%s · done %d× in %d sessions · ≈%s CW each · they find the same files: %s (estimate)"
                             % (lead, n, ns, _fk(typ), ", ".join(os.path.basename(f) for f in o["files"][:4])))
        stub = lambda: write_filemap(o["name"], o["text"], cache)
    elif o["rule"] == "consistency":
        hint.update(action='Make "%s" a skill for consistency' % o["name"],
                    evidence="same %d steps in %d runs; no token saving expected (%s) · done %d× in %d sessions · %s"
                             % (len(o["steps"]), o["repeats"], terms, n, ns, " → ".join(o["steps"][:5])))
        stub = lambda: write_stub(rep, o["name"], cache, o["text"])
    else:
        hint.update(action='Make "%s" a skill' % o["name"],
                    evidence="%s · done %d× in %d sessions · ≈%s CW each · %s (estimate)"
                             % (lead, n, ns, _fk(typ), rep["seq"] or " → ".join(common_steps(runs)[:5])))
        stub = lambda: write_stub(rep, o["name"], cache, o["text"])
    if write and stub:
        try:
            hint["stub_path"] = shown_path(stub(), cache)
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
        self.widf = {}
        self._skills = {}
        self.version = 0          # bumped on every (re)build: the live recap cache keys on it

    def refresh(self):
        self.index.build()
        self.idf = file_idf(self.index.records())
        self.widf = word_idf(self.index.records())
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
                       idf=self.idf, widf=self.widf)
