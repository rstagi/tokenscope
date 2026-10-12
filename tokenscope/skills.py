"""Skill invocations in Claude Code transcripts: pure functions, no I/O.

Two kinds:
- `tool`: the agent called the Skill tool (tool_use name "Skill", input `skill` / `name` / `command`). The skill's
  body then arrives as a meta user message "Base directory for this skill: ..." carrying `sourceToolUseID`; that one
  is not counted again.
- `slash`: a slash command that loads a skill. The user message carries `<command-name>/foo</command-name>`; it is a
  skill (not a built-in like /model or /compact) when the message says `<skill-format>true</skill-format>`, or when
  the next meta user message starts with "Base directory for this skill:" and has no `sourceToolUseID`.

`SkillScanner.feed(record)` sees a thread's records in order and returns the skill invocations they complete:
[("tool" | "slash", name)].
"""
import re

COMMAND_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
SKILL_FORMAT = "<skill-format>true</skill-format>"
BASE_DIR = "Base directory for this skill:"


def tool_skill(name, inp):
    """Skill name of a Skill tool_use, else None."""
    if name != "Skill":
        return None
    inp = inp if isinstance(inp, dict) else {}
    return str(inp.get("skill") or inp.get("name") or inp.get("command") or "").strip().lstrip("/") or None


def _texts(r):
    msg = r.get("message") if isinstance(r.get("message"), dict) else {}
    c = msg.get("content")
    if isinstance(c, str):
        return [c]
    return [str(b.get("text") or "") for b in c or () if isinstance(b, dict) and b.get("type") == "text"]


def base_dir_skill(r):
    """The skill a meta "Base directory for this skill: <dir>" message loads (its directory name), else None."""
    for t in _texts(r):
        t = t.lstrip()
        if t.startswith(BASE_DIR):
            d = t[len(BASE_DIR):].split("\n", 1)[0].strip().rstrip("/\\")
            return re.split(r"[/\\]", d)[-1] or "skill"
    return None


class SkillScanner:
    """Skill invocations of one thread, fed record by record (see module doc)."""

    def __init__(self):
        self.pending = None   # (command name, already counted) of the last slash command
        self.typed = False    # the last slash skill's command message was typed (not a meta message)

    def feed(self, r):
        if not isinstance(r, dict):
            return []
        typ = r.get("type")
        if typ == "assistant":
            msg = r.get("message") if isinstance(r.get("message"), dict) else {}
            out = []
            for b in msg.get("content") or ():
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    n = tool_skill(b.get("name"), b.get("input"))
                    if n:
                        out.append(("tool", n))
            if out or self.pending:
                self.pending = None
            return out
        if typ != "user":
            return []
        text = "\n".join(_texts(r))
        m = COMMAND_RE.search(text) if "<command-name>" in text else None
        if m:
            name = m.group(1)
            self.typed = not r.get("isMeta")
            if SKILL_FORMAT in text:
                self.pending = (name, True)
                return [("slash", name)]
            self.pending = (name, False)
            return []
        pend, self.pending = self.pending, None   # a command's skill body is the very next user message
        base = base_dir_skill(r) if r.get("isMeta") and not r.get("sourceToolUseID") else None
        if not base or (pend and pend[1]):
            return []   # not a skill body, or the body of a <skill-format> command already counted
        if not pend:
            self.typed = False
        return [("slash", pend[0] if pend else base)]


def scan(records):
    """[(kind, name)] over one thread's records."""
    sc = SkillScanner()
    return [x for r in records for x in sc.feed(r)]
