"""Goal name from a session's first real prompt.

Keyword heuristic (default): verb class ("Fix", "Build", ...) + up to 4 salient keywords, e.g.
"Fix: checkout total tax" (<= 48 chars). In redact mode paths, emails, URLs, long numbers, hex ids and capitalised
proper-noun-looking words are masked first. Optional Haiku naming (`--haiku`) runs `claude -p` in a background
thread with only the masked first 240 chars; failures keep the heuristic name.

A prompt that reads oddly as a label (reads_oddly: long, multi-line, opens with filler, carries paths, URLs, code or
markup) is named from its cleaned text (goal_label: deterministic, no network); the live server also hands only those
prompts to Haiku (HaikuNamer(only_odd=True)), its names cached on disk by a hash of the masked model input
($TOKENSCOPE_CACHE_DIR/goal-names.json, default ~/.cache/tokenscope/goal-names.json).
"""
import hashlib
import json
import os
import tempfile
import re
import subprocess
import threading

from ..redact import _FILE_RE

MAX_LEN = 48
VERBS = [  # (class, trigger words) checked in order of the first matching word in the prompt
    ("Fix", {"fix", "fixes", "fixing", "debug", "repair", "broken", "bug", "crash", "failing", "error"}),
    ("Build", {"add", "build", "implement", "create", "make", "new", "support", "introduce", "generate", "grow", "extend", "expand"}),
    ("Refactor", {"refactor", "clean", "cleanup", "simplify", "restructure", "rename", "tidy"}),
    ("Test", {"test", "tests", "testing", "coverage"}),
    ("Review", {"review", "check", "audit", "inspect", "verify"}),
    ("Explain", {"explain", "what", "how", "why", "understand", "describe"}),
    ("Write", {"write", "draft", "document", "docs"}),
    ("Ship", {"deploy", "release", "ship", "publish"}),
    ("Research", {"research", "find", "compare", "investigate", "look", "search", "evaluate"}),
]
VERB_OF = {w: cls for cls, ws in VERBS for w in ws}
FILLER = re.compile(r"\b(please|pls|can you|could you|would you|will you|i want( you)? to|i'd like( you)? to|"
                    r"i need( you)? to|let's|lets|we need to|help me( to)?|go ahead and|try to|just)\b", re.I)
STOPWORDS = frozenset("""a an the and or but if then else of to in on at by for with from into onto about over under
as is are was were be been being it its this that these those there here we you i me my our your they them their
he she his her do does did done have has had not no yes so too very can could would should will shall may might
must also all any some each every more most less few such only own same than now up out off again further once
use using used get got want need like make sure some thing things stuff way see let also what which who whom how why
when where able one two via per etc just""".split())
WORD_RE = re.compile(r"[A-Za-z_][\w.-]*[\w]|[A-Za-z]")
CODEISH_RE = re.compile(r"[a-z][A-Z]|_|\.\w+$")
MASKS = [
    re.compile(r"https?://\S+"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    re.compile(r"(?:~|\.{1,2})?/(?:[\w.@+-]+/)*[\w.@+-]+|[\w.-]+/(?:[\w.@+-]+/)+[\w.@+-]*"),
    _FILE_RE,
    re.compile(r"\b[0-9a-f]{7,}\b", re.I),
    re.compile(r"\b\d{4,}\b"),
]
PROPER_RE = re.compile(r"(?<=[^.!?\s]\s)[A-Z][a-z]+\b")   # capitalised word not at a sentence start


def mask(text):
    """Mask private-looking spans (paths, emails, URLs, ids, long numbers, proper nouns) with "…"."""
    s = str(text or "")
    for rx in MASKS:
        s = rx.sub("…", s)
    return PROPER_RE.sub("…", s)


SENT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
CONTEXT_RE = re.compile(r"^(you are|you're|i am|i'm|we are|we're|this is|there is|there are|it is|it's|"
                        r"context|background|note)\b|^(the|this|our|my)\b.*\b(is|are|was|were|has|have)\b", re.I)


def main_clause(text):
    """The imperative sentence of a prompt: the first one starting with a verb (after filler), else the first one
    with a verb that is not a context sentence ("The test suite is red.", "You are ..."), else the whole text."""
    sents = [x.strip() for x in SENT_RE.split(str(text or "")) if x.strip()]
    for x in sents:
        w = WORD_RE.findall(FILLER.sub(" ", x))
        if w and w[0].lower() in VERB_OF:
            return x
    for x in sents:
        if not CONTEXT_RE.search(x) and any(w.lower() in VERB_OF for w in WORD_RE.findall(x)):
            return x
    return str(text or "")


NOUNISH = frozenset({"bug", "broken", "crash", "error", "failing", "new", "test", "tests", "testing", "coverage", "docs",
                     "what", "how", "why", "look", "support", "make"})   # verb-class words that are rarely the ask


def _keywords(words, skip):
    cands = []
    for i, w in enumerate(words):
        lw = w.lower()
        if i == skip or lw in STOPWORDS or len(w) < 3 or w.isdigit():
            continue
        if lw in (c[2].lower() for c in cands):
            continue
        cands.append((0 if CODEISH_RE.search(w) else 1, i, w))
    best = sorted(cands)[:4]
    return [w if CODEISH_RE.search(w) else w.lower() for _, _, w in sorted(best, key=lambda c: c[1])]


def keyword_name(text, redact=False):
    """Heuristic goal name, e.g. "Fix: checkout total tax" (imperative clause first, code identifiers kept)."""
    full = FILLER.sub(" ", (mask(text) if redact else str(text or "")).replace("\n", " "))
    words = WORD_RE.findall(FILLER.sub(" ", main_clause(mask(text) if redact else str(text or ""))))
    verbs = [i for i, w in enumerate(words) if w.lower() in VERB_OF]
    pick = next((i for i in verbs if words[i].lower() not in NOUNISH), verbs[0] if verbs else None)
    if pick is None:   # no verb in the clause: look at the whole prompt
        words = WORD_RE.findall(full)
        verbs = [i for i, w in enumerate(words) if w.lower() in VERB_OF]
        pick = next((i for i in verbs if words[i].lower() not in NOUNISH), verbs[0] if verbs else None)
    verb = VERB_OF[words[pick].lower()] if pick is not None else "Work on"
    kws = _keywords(words, pick)
    if len(kws) < 2:   # e.g. "Can you debug why?" after a context sentence
        extra = [k for k in _keywords(WORD_RE.findall(full), None) if k.lower() not in VERB_OF and k not in kws]
        kws = (kws + extra)[:4]
    name = ("%s: %s" % (verb, " ".join(kws))) if kws else verb
    if len(name) > MAX_LEN:
        name = name[:MAX_LEN - 1].rsplit(" ", 1)[0] + "…"
    return name


# ---------------------------------------------------------------- labels that read oddly
LONG_CHARS = 80   # longer than a top-spenders turn label (top.TURN_CHARS): too long to read as a goal
LEAD_FILLER = re.compile(r"^\W*(?:(?:ok(?:ay)?|so|hey|hi|hello|hmm+|um+|uh+|well|alright|right|now|also|and|but|great|"
                         r"thanks|thank you|cool|yes|yeah|yep|sure|please|pls|can you|could you|would you|will you|"
                         r"i want(?: you)? to|i'd like(?: you)? to|i need(?: you)? to|let's|lets|we need to|"
                         r"help me(?: to)?|go ahead and|just)\b[\s,.!:;-]*)+", re.I)
FENCE_RE = re.compile(r"```.*?(?:```|$)", re.S)
TAG_RE = re.compile(r"</?[A-Za-z][\w-]*(?:\s[^<>]*)?/?>")
URL_RE = MASKS[0]
PATH_RE = MASKS[2]
CODE_RE = re.compile(r"`|[{};]|=>|::|==|\b(?:def|class|function|import|return|const|var|let)\s+\w|\w\(.*\)|"
                     r"^\s*(?:\$|>>>|#!)|Traceback \(most recent|\b\w+(?:Error|Exception):")


def reads_oddly(text):
    """Why a prompt's raw text reads oddly as a goal label ("multi-line", "long", "filler", "path", "code",
    "markup"), else None."""
    t = str(text or "").strip()
    if not t:
        return None
    if "\n" in t:
        return "multi-line"
    if TAG_RE.search(t):
        return "markup"
    if CODE_RE.search(t):
        return "code"
    if URL_RE.search(t) or PATH_RE.search(t):
        return "path"
    if LEAD_FILLER.match(t):
        return "filler"
    if len(t) > LONG_CHARS:
        return "long"
    return None


def clean_prompt(text):
    """The prose of a prompt: code fences and markup dropped, inline code unquoted, URLs dropped, paths cut to their
    last part, leading filler stripped, whitespace collapsed (deterministic, no network)."""
    s = FENCE_RE.sub(" ", str(text or ""))
    s = TAG_RE.sub(" ", s)
    s = s.replace("`", "")
    s = URL_RE.sub(" ", s)
    s = PATH_RE.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1], s)
    lines = [x.strip() for x in s.splitlines() if x.strip()]
    s = " ".join(x if x[-1] in ".!?:" else x + "." for x in lines)   # one sentence per line (main_clause splits)
    return " ".join(LEAD_FILLER.sub("", s).split())


def goal_label(text, redact=False):
    """The goal name of a prompt: keyword_name of its text, of its cleaned text (clean_prompt) when the raw text
    reads oddly; a cleaned prompt without keywords is named by its first words."""
    if not reads_oddly(text):
        return keyword_name(text, redact=redact)
    clean = clean_prompt(text)
    name = keyword_name(clean, redact=redact)
    if ":" not in name:   # no keywords ("Work on"): its first few words, if any are left
        words = []
        for w in WORD_RE.findall(mask(clean) if redact else clean):
            if w.lower() not in (x.lower() for x in words):
                words.append(w)
        words = words[:6]
        if words:
            name = _clip(" ".join(words), MAX_LEN)
            name = name[0].upper() + name[1:]
    return name


# ---------------------------------------------------------------- the Haiku name cache
_CACHE_LOCK = threading.Lock()


def cache_path():
    d = os.environ.get("TOKENSCOPE_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "tokenscope")
    return os.path.join(d, "goal-names.json")


def cache_key(text):
    """Hash of exactly what Haiku sees (the masked first 240 chars): the same prompt gets the same cached name."""
    return hashlib.sha1(mask(text)[:240].encode("utf-8")).hexdigest()[:20]


def _load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def cached_name(text, path=None):
    """The cached Haiku name of a prompt, else None."""
    name = _load_cache(path or cache_path()).get(cache_key(text))
    return name if isinstance(name, str) and name else None


def store_name(text, name, path=None):
    path = path or cache_path()
    with _CACHE_LOCK:
        c = _load_cache(path)
        c[cache_key(text)] = name
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(c, f)
            os.replace(tmp, path)
        except OSError:
            pass


HAIKU_MAX_LEN = 60


def _clip(name, n):
    """Cut at a word boundary with an ellipsis (never mid-word)."""
    name = " ".join(str(name).split())
    return name if len(name) <= n else name[:n - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"


HAIKU_PROMPT = ("Name the goal of this coding-assistant request in 3-7 words, using only what the request says "
                "(no guessed project names). Reply with the name only.\n\n"
                "Request: %s")


def _neutral_dir():
    d = os.path.join(tempfile.gettempdir(), "goal-naming")   # the dir name is visible to the model: keep it neutral
    os.makedirs(d, exist_ok=True)
    return d


def haiku_name(text, timeout=30, runner=subprocess.run):
    """Ask Haiku for a 3-7 word goal name from the masked first 240 chars; None on any failure."""
    try:
        p = runner(["claude", "-p", "--model", "haiku", "--tools", "", "--output-format", "json",
                    HAIKU_PROMPT % mask(text)[:240]], capture_output=True, text=True, timeout=timeout,
                   cwd=_neutral_dir())   # not the server's cwd: claude -p would read that project's CLAUDE.md
        if p.returncode != 0:
            return None
        out = json.loads(p.stdout)
        name = str(out.get("result") if isinstance(out, dict) else out).strip().strip('"').splitlines()[0]
        return _clip(name, HAIKU_MAX_LEN) if name else None
    except Exception:  # noqa: BLE001 - naming must never break the tailer
        return None


class HaikuNamer:
    """Background Haiku naming: cached per key (session id, or (session id, prompt n)); calls `done(key, name)`.
    `only_odd`: only prompts that read oddly as labels (reads_oddly); `cache_path`: names also cached on disk by
    prompt (cache_key), a cached name needs no model call."""

    def __init__(self, done, namer=haiku_name, only_odd=False, cache_path=None):
        self.done, self.namer, self.only_odd, self.cache_path = done, namer, only_odd, cache_path
        self.cache, self.lock = {}, threading.Lock()

    def request(self, key, text):
        if self.only_odd and not reads_oddly(text):
            return
        with self.lock:
            if key in self.cache:
                return
            self.cache[key] = None
        threading.Thread(target=self._run, args=(key, text), daemon=True).start()

    def _run(self, session_id, text):
        name = cached_name(text, self.cache_path) if self.cache_path else None
        if not name:
            name = self.namer(text)
            if name and self.cache_path:
                store_name(text, name, self.cache_path)
        if name:
            with self.lock:
                self.cache[session_id] = name
            self.done(session_id, name)
