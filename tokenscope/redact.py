"""--redact: mask private data in report / patterns / goals output while keeping its shape and numbers.

  prompts & free text  -> first word + length: "fix… [42 chars]"
  file paths           -> "…/<short stable hash><ext>"
  project names        -> "project-1..N" (first-seen order; outputs are cost-ranked, so ~ranked by cost)
  session / agent ids  -> 8-char stable hash; workflow ids -> "wf-<hash>"
  MCP servers          -> kept when well known (perplexity, notion, github, posthog, playwright, browser*,
                          chrome*, claude_ai_*), else "mcp-N"
  commands             -> program / subcommand kept, arguments masked
  error classes        -> first 40 chars, paths and quoted strings masked
  numbers              -> untouched
  $TOKENSCOPE_REDACT_TERMS=<file, one term per line> masks extra terms anywhere (case-insensitive).
"""
import hashlib
import os
import re

PUBLIC_MCP = ("perplexity", "notion", "github", "posthog", "playwright")
PUBLIC_MCP_PREFIX = ("browser", "chrome", "claude_ai_")
PROMPT_KEYS = {"prompt", "prompts", "first_prompt", "text", "request", "requests_text", "correction", "corrections",
               "user_text", "description", "summary", "goal", "name_hint", "sample_prompts", "message"}
CMD_KEYS = {"cmd", "command", "commands_raw"}
SESSION_KEYS = {"session", "session_id", "sid", "sessionId"}
AGENT_KEYS = {"agent", "agent_id", "transcript"}
WORKFLOW_KEYS = {"workflow", "workflow_id"}
PROJECT_KEYS = {"project", "family", "projects_list"}
ERROR_KEYS = {"error_class"}

_PATH_RE = re.compile(r"(?<![\w<])(?:~|\.{1,2})?/(?:[\w.@+-]+/)*[\w.@+-]+|(?<![\w/<])[\w.-]+/(?:[\w.@+-]+/)*[\w.@+-]+")
_FILE_RE = re.compile(r"(?<![\w/…])[\w-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|md|json|ya?ml|toml|go|rs|rb|java|kt|swift|c|h|"
                      r"cpp|hpp|cs|php|sh|sql|html|css|scss|txt|log|lock|env|ini|cfg|ipynb)\b")
_MCP_RE = re.compile(r"(mcp__)([A-Za-z0-9_-]+?)(__)|(MCP\()([^.()]+)(\.)|(mcp:)([\w.-]+)")
_QUOTED_RE = re.compile(r"\"[^\"]*\"|'[^']*'")


KEEP_KEYS = frozenset("start end ts timestamp date first last first_ts last_ts first_date last_date day date_min date_max tmin tmax".split())


def load_terms(path=None):
    path = path or os.environ.get("TOKENSCOPE_REDACT_TERMS")
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [x.strip() for x in f if x.strip()]


GENERIC_WORDS = frozenset("""fix add test tests pr push commit review docs doc build deploy refactor bug bugs error errors
api ui the a an and or of to in on for with from by is it this that run runs read write edit update remove delete
create check make new old file files code feature features change changes open close merge branch main
bash grep glob sed cat head tail find ls echo git npm pnpm node python python3 curl gh agent task skill mcp webfetch websearch
toolsearch structuredoutput todowrite monitor perplexity notion github posthog browser chrome playwright workflow phase
verify triage implement research gather draft judge synthesize fix""".split())
_VOCAB_RE = re.compile(r"[^\W_]{3,}", re.U)
_TOKEN_RE = re.compile(r"[\w-]+", re.U)   # maximal [\w-] runs: a masked word must be a whole token


def vocab(obj, out=None):
    """Distinctive words of every prompt-like value in obj (masked anywhere they reappear, e.g. in goal names)."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in PROMPT_KEYS and isinstance(v, str):
                out.update(w.lower() for w in _VOCAB_RE.findall(v) if w.lower() not in GENERIC_WORDS)
            elif k in PROMPT_KEYS and isinstance(v, list):
                for x in v:
                    if isinstance(x, str):
                        out.update(w.lower() for w in _VOCAB_RE.findall(x) if w.lower() not in GENERIC_WORDS)
                    else:
                        vocab(x, out)
            else:
                vocab(v, out)
    elif isinstance(obj, list):
        for x in obj:
            vocab(x, out)
    return out


class Redactor:
    def __init__(self, salt="tokenscope", terms=None):
        self.salt = str(salt)
        self.projects, self.mcp = {}, {}
        terms = load_terms() if terms is None else list(terms)
        terms = sorted({t for t in terms if t}, key=len, reverse=True)
        self.terms_re = re.compile("|".join(re.escape(t) for t in terms), re.I) if terms else None
        self.words_re, self.ids = None, {}

    def learn(self, obj, words=(), session_ids=()):
        """Mask these prompt words (whole words, keeping name structure) and session ids / 8-char prefixes."""
        ws = set(w.lower() for w in words) | vocab(obj)
        if ws:
            self.wordset = ws
            self.words_re = _TOKEN_RE
        for sid in session_ids:
            if sid:
                self.ids[sid] = self.h(sid)
                self.ids[sid[:8]] = self.h(sid)
        self._ids_re = None

    def h(self, x, n=8):
        return hashlib.sha256((self.salt + "\0" + str(x)).encode("utf-8")).hexdigest()[:n]

    # --- primitives
    def terms(self, s):
        s = self.terms_re.sub("[redacted]", s) if self.terms_re else s
        if self.ids:
            if getattr(self, "_ids_re", None) is None:
                self._ids_re = re.compile("|".join(re.escape(x) for x in sorted(self.ids, key=len, reverse=True)))
            s = self._ids_re.sub(lambda m: self.ids[m.group(0)], s)
        if self.words_re is not None:
            ws = self.wordset
            def _w(m):
                t = m.group(0).lower()
                if t in ws:
                    return "…"
                if "-" in t and any(x in ws for x in t.split("-") if len(x) >= 3):
                    return "…"
                return m.group(0)
            s = self.words_re.sub(_w, s)
        return s

    def path(self, p):
        base = p.rstrip("/").rsplit("/", 1)[-1]
        ext = os.path.splitext(base)[1] if "." in base[1:] else ""
        return "…/%s%s" % (self.h(p, 3), ext)

    def server(self, name):
        low = name.lower()
        if low in PUBLIC_MCP or low.startswith(PUBLIC_MCP_PREFIX) or any(k in low for k in PUBLIC_MCP):
            return name
        if name not in self.mcp:
            self.mcp[name] = "mcp-%d" % (len(self.mcp) + 1)
        return self.mcp[name]

    def project(self, name):
        if name is None:
            return None
        if name not in self.projects:
            self.projects[name] = "project-%d" % (len(self.projects) + 1)
        return self.projects[name]

    def free(self, s):
        s = str(s)
        if not s.strip():
            return s
        first = self.terms(s.split()[0])[:12]
        return "%s… [%d chars]" % (first, len(s))

    def command(self, s):
        from .patterns import bash_segments
        segs = bash_segments(s)
        return " && ".join("%s …" % x for x in segs)

    def error(self, s):
        s = _QUOTED_RE.sub('"…"', str(s))
        return self.string(s)[:40]

    def string(self, s):
        """Structural masking for generated text (titles, labels, fine kinds)."""
        s = _MCP_RE.sub(lambda m: (m.group(1) + self.server(m.group(2)) + m.group(3)) if m.group(1) else
                        (m.group(4) + self.server(m.group(5)) + m.group(6)) if m.group(4) else
                        (m.group(7) + self.server(m.group(8))), s)
        s = _PATH_RE.sub(lambda m: self.path(m.group(0)), s)
        s = _FILE_RE.sub(lambda m: self.path(m.group(0)), s)
        if self.projects:
            if getattr(self, "_proj_n", -1) != len(self.projects):
                self._proj_n = len(self.projects)
                self._proj_re = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(n) for n in sorted(self.projects, key=len, reverse=True)))
            s = self._proj_re.sub(lambda m: self.projects[m.group(0)], s)
        s = re.sub(r'(Grep|Glob)\("[^"]*"\)', lambda m: '%s("…")' % m.group(1), s)
        return self.terms(s)

    # --- walk
    def value(self, key, v):
        if isinstance(v, dict):
            return {self.key(k): self.value(k, x) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(key, x) for x in v]
        if not isinstance(v, str):
            return v
        if isinstance(key, str) and (key.startswith("model") or key in KEEP_KEYS) and re.fullmatch(r"[\w.:+\-\[\] ]{1,64}", v):
            return v   # public model ids and timestamps are not private and are needed for pricing/dates
        if key in SESSION_KEYS:
            return self.h(v)
        if key in AGENT_KEYS:
            return v if v == "main" else self.string(v) if " " in v else self.h(v)
        if key in WORKFLOW_KEYS:
            return "wf-" + self.h(v)
        if key in PROJECT_KEYS:
            return self.project(v)
        if key in PROMPT_KEYS:
            return self.free(v)
        if key in CMD_KEYS:
            return self.command(v)
        if key in ERROR_KEYS:
            return self.error(v)
        if key == "id" and v.startswith("recerr:"):
            return "recerr:" + self.h(v)
        return self.string(v)

    def key(self, k):
        if not isinstance(k, str):
            return k
        return self.string(k) if ("/" in k or "mcp" in k.lower() or "." in k or k in self.ids
                                  or k.split(":")[0] in self.ids) else k

    def prime(self, obj):
        """Register project names first, in output (cost) order, so they mask inside free strings too."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in PROJECT_KEYS and isinstance(v, str):
                    self.project(v)
                else:
                    self.prime(v)
        elif isinstance(obj, list):
            for x in obj:
                self.prime(x)


def redact(obj, salt="tokenscope", terms=None):
    r = Redactor(salt, terms)
    r.prime(obj)
    return r.value(None, obj)


def _session_ids(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SESSION_KEYS and isinstance(v, str):
                out.add(v)
            else:
                _session_ids(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _session_ids(x, out)
    return out


def redact_many(*objs, salt=None, terms=None):
    """Redact several related structures with one shared mapping (same project-N / hashes everywhere)."""
    r = Redactor(salt or os.environ.get("TOKENSCOPE_REDACT_SALT", "tokenscope"), terms)
    for o in objs:
        r.prime(o)
    r.learn(list(objs), session_ids=_session_ids(list(objs), set()))
    return [r.value(None, o) for o in objs]


def final_pass(html, terms=None):
    """Last line of defence on rendered output: mask $TOKENSCOPE_REDACT_TERMS as whole words (also inside
    hyphen/underscore/dot compounds like acme-local) anywhere in the text, attributes and embedded JSON."""
    terms = load_terms() if terms is None else list(terms)
    if not terms:
        return html
    rx = re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.I)
    return rx.sub("[redacted]", html)
