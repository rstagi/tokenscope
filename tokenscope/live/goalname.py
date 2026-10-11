"""Goal name from a session's first real prompt.

Keyword heuristic (default): verb class ("Fix", "Build", ...) + up to 4 salient keywords, e.g.
"Fix: checkout total tax" (<= 48 chars). In redact mode paths, emails, URLs, long numbers, hex ids and capitalised
proper-noun-looking words are masked first. Optional Haiku naming (`--haiku`) runs `claude -p` in a background
thread with only the masked first 240 chars; failures keep the heuristic name.
"""
import json
import re
import subprocess
import threading

from ..redact import _FILE_RE

MAX_LEN = 48
VERBS = [  # (class, trigger words) checked in order of the first matching word in the prompt
    ("Fix", {"fix", "fixes", "fixing", "debug", "repair", "broken", "bug", "crash", "failing", "error"}),
    ("Build", {"add", "build", "implement", "create", "make", "new", "support", "introduce", "generate"}),
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


HAIKU_PROMPT = ("Name the goal of this coding-assistant request in 3-7 words. Reply with the name only.\n\n"
                "Request: %s")


def haiku_name(text, timeout=30, runner=subprocess.run):
    """Ask Haiku for a 3-7 word goal name from the masked first 240 chars; None on any failure."""
    try:
        p = runner(["claude", "-p", "--model", "haiku", "--tools", "", "--output-format", "json",
                    HAIKU_PROMPT % mask(text)[:240]], capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return None
        out = json.loads(p.stdout)
        name = str(out.get("result") if isinstance(out, dict) else out).strip().strip('"').splitlines()[0]
        return name[:MAX_LEN] if name else None
    except Exception:  # noqa: BLE001 - naming must never break the tailer
        return None


class HaikuNamer:
    """Background Haiku naming: cached per key (session id, or (session id, prompt n)); calls `done(key, name)`."""

    def __init__(self, done, namer=haiku_name):
        self.done, self.namer = done, namer
        self.cache, self.lock = {}, threading.Lock()

    def request(self, key, text):
        with self.lock:
            if key in self.cache:
                return
            self.cache[key] = None
        threading.Thread(target=self._run, args=(key, text), daemon=True).start()

    def _run(self, session_id, text):
        name = self.namer(text)
        if name:
            with self.lock:
                self.cache[session_id] = name
            self.done(session_id, name)
