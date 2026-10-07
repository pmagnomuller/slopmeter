"""Deterministic waste (deadweight + vibe + papertrail) detection.

Complements the attention-gap slop score: that model asks whether code arrived
faster than humans could own it. These rules ask whether the surface area that
*did* arrive looks unused, unfinished, or formulaically padded.

Rule IDs align with the slopcop catalog (DEAD / VIBE / TRAIL) where practical —
same smells, stdlib Python, density quiet-boundaries. No network, no models.

Consumers:

* longitudinal snapshots (findings per unique blob, aggregated per day)
* ``slopmeter audit`` (working-tree / diff CI gate)
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --- severities ------------------------------------------------------------

SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


@dataclass(frozen=True)
class Rule:
    id: str
    module: str  # deadweight | vibecheck | papertrail
    severity: str
    message: str


RULES: Dict[str, Rule] = {
    r.id: r
    for r in (
        # deadweight
        Rule("DEAD001", "deadweight", "error", "Exception swallowed without handling"),
        Rule("DEAD002", "deadweight", "warning", "TODO/FIXME/HACK/XXX marker in code prose"),
        Rule("DEAD003", "deadweight", "error", "Unimplemented stub still in the tree"),
        Rule("DEAD004", "deadweight", "warning", "Exception converted to a silent empty value"),
        Rule("DEAD005", "deadweight", "warning", "Empty concrete function body"),
        Rule("DEAD006", "deadweight", "info", "Comment restates the next line"),
        Rule("DEAD007", "deadweight", "warning", "Identical substantial comment repeated"),
        Rule("DEAD008", "deadweight", "info", "Pass-through wrapper that only forwards"),
        Rule("DEAD009", "deadweight", "warning", "Opposite Boolean literals in both branches"),
        Rule("DEAD010", "deadweight", "error", "Assertion that can only pass"),
        Rule("DEAD011", "deadweight", "warning", "Large identical block duplicated in one file"),
        Rule("DEAD012", "deadweight", "warning", "Empty or heading-only documentation section"),
        Rule("DEAD013", "deadweight", "info", "Hard-coded count of a mutable inventory"),
        Rule("DEAD014", "deadweight", "warning", "Handler only rethrows the same exception"),
        Rule("DEAD015", "deadweight", "warning", "Handler only prints / console.logs"),
        Rule("DEAD016", "deadweight", "error", "Empty JSX event callback"),
        Rule("DEAD017", "deadweight", "warning", "Action stub that only returns success"),
        Rule("DEAD018", "deadweight", "warning", "Numbered Step/Phase comment banners"),
        # papertrail
        Rule("TRAIL001", "papertrail", "warning", "Placeholder commit subject"),
        Rule("TRAIL002", "papertrail", "error", "Autosquash marker in commit subject"),
        # vibecheck
        Rule("VIBE001", "vibecheck", "warning", "Stock transition phrases are unusually dense"),
        Rule("VIBE002", "vibecheck", "warning", "Assistant-response framing in prose"),
        Rule("VIBE003", "vibecheck", "info", "Generic evaluative modifiers are dense"),
        Rule("VIBE004", "vibecheck", "warning", "Hedging / padding phrases are dense"),
        Rule("VIBE005", "vibecheck", "warning", "Artificial both-sides balance templates"),
        Rule("VIBE006", "vibecheck", "warning", "Explicit structure announcements are dense"),
        Rule("VIBE007", "vibecheck", "info", "Em dashes are unusually dense"),
        Rule("VIBE008", "vibecheck", "info", "Staged-reveal / colon-sentence rhythm"),
        Rule("VIBE009", "vibecheck", "warning", "Many sentences share the same opening"),
        Rule("VIBE010", "vibecheck", "info", "Unusually uniform sentence lengths"),
        Rule("VIBE011", "vibecheck", "info", "Abstract triad lists are dense"),
        Rule("VIBE012", "vibecheck", "warning", "Vague demonstrative abstractions are dense"),
        Rule("VIBE013", "vibecheck", "warning", "Stock metaphor / idiom markers are dense"),
        Rule("VIBE014", "vibecheck", "warning", "Promotional positivity markers are dense"),
        Rule("VIBE015", "vibecheck", "warning", "Generic disclaimer phrases cluster"),
        Rule("VIBE016", "vibecheck", "warning", "Stock conclusion markers cluster"),
        Rule("VIBE017", "vibecheck", "warning", "Adjacent sentences largely restate each other"),
        Rule("VIBE018", "vibecheck", "info", "Paragraph lengths are unusually uniform"),
        Rule("VIBE019", "vibecheck", "warning", "Chatbot identity / cutoff disclaimer artifact"),
        Rule("VIBE020", "vibecheck", "warning", "Rhetorical contrast templates are dense"),
        Rule("VIBE021", "vibecheck", "warning", "Not-only/but-also correlatives are dense"),
        Rule("VIBE022", "vibecheck", "warning", "Workplace jargon markers are dense"),
        Rule("VIBE023", "vibecheck", "warning", "Generic AI-favored vocabulary is dense"),
        Rule("VIBE024", "vibecheck", "warning", "Generalized-lesson phrases are dense"),
        Rule("VIBE025", "vibecheck", "warning", "Dramatic characterization phrases are dense"),
        Rule("VIBE026", "vibecheck", "warning", "Paraphrase of the reader's question"),
    )
}


@dataclass
class Finding:
    path: str
    line: int
    rule_id: str
    message: str
    evidence: str = ""
    observation: str = ""

    @property
    def rule(self) -> Rule:
        return RULES[self.rule_id]

    @property
    def severity(self) -> str:
        return self.rule.severity


@dataclass
class WasteBlob:
    """Findings for one blob content under a language key."""

    findings: List[Finding] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.findings)

    def by_severity(self) -> Dict[str, int]:
        out = {"error": 0, "warning": 0, "info": 0}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out


# --- language helpers ------------------------------------------------------

_CODE_EXT = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".md": "markdown", ".mdx": "markdown", ".rst": "rst", ".txt": "text",
}


def language_of(path: str) -> Optional[str]:
    name = Path(path).name
    if name in {"Dockerfile", "Makefile", "Justfile"}:
        return "shell"
    return _CODE_EXT.get(Path(path).suffix.lower())


def _line_at(text: str, offset: int) -> Tuple[int, str]:
    line = text.count("\n", 0, offset) + 1
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end < 0:
        end = len(text)
    return line, text[start:end].rstrip()


def _add(out: List[Finding], text: str, path: str, offset: int, rule_id: str, observation: str = "") -> None:
    line, ev = _line_at(text, offset)
    out.append(Finding(path, line, rule_id, RULES[rule_id].message, ev, observation))


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    name = Path(p).name
    if any(seg in p.split("/") for seg in ("test", "tests", "__tests__", "spec", "specs", "e2e", "fixtures")):
        return True
    stem = Path(name).stem
    return (
        stem.startswith("test_") or stem.endswith("_test") or stem.endswith(".test")
        or stem.endswith(".spec") or ".test." in name or ".spec." in name
    )


# --- deadweight ------------------------------------------------------------

_PY_EMPTY_EXCEPT = re.compile(
    r"(?m)^([ \t]*)except\b[^\n]*:\n(?:\1[ \t]+(?:pass|\.\.\.|ellipsis)\s*(?:#.*)?\n)+"
)
_BRACE_EMPTY_CATCH = re.compile(r"(?m)catch\s*\([^)]*\)\s*\{\s*\}|catch\s*\{\s*\}")
_TODO_MARKER = re.compile(
    r"(?m)^[ \t]*(?://|#|--|/\*)[ \t]*(TODO|FIXME|HACK|XXX)\b(?![-\w])"
)
# Executable stubs with TODO markers — bare `raise NotImplementedError` alone is quiet.
_UNIMPLEMENTED = re.compile(
    r"(?m)^\s*(?:"
    r"raise\s+NotImplementedError\s*\([^)\n]*TODO|"
    r"throw\s+new\s+Error\([^)\n]*TODO|"
    r"throw\s+new\s+(?:Error|Exception)\([^)\n]*not\s+implemented|"
    r"panic\([^)\n]*TODO|"
    r"(?:todo|unimplemented)!\("
    r")",
    re.IGNORECASE,
)
_PY_EXCEPT_NONE = re.compile(
    r"(?m)^([ \t]*)except\b(?![^\n]*\b(?:ImportError|ModuleNotFoundError|OSError|FileNotFoundError|StopIteration)\b)[^\n]*:\n"
    r"\1[ \t]+return\s+(?:None|False|\[\]|\{\}|set\(\)|0|\"\"|'')\s*(?:#.*)?$"
)
_JS_CATCH_NULL = re.compile(
    r"(?m)catch\s*\((\w+)\)\s*\{\s*return\s+(?:null|undefined|false|\[\]|\{\}|0|\"\"|'')\s*;?\s*\}"
)
_EMPTY_DEF_PY = re.compile(
    r"(?m)^([ \t]*)(?:async\s+)?def\s+\w+\s*\([^)]*\)\s*(?:->[^:]+)?:\n"
    r"(?:\1[ \t]+(?:pass|\.\.\.|ellipsis)\s*(?:#.*)?\n)+"
)
_EMPTY_FN_JS = re.compile(
    r"(?m)(?:function\s+\w+\s*\([^)]*\)\s*\{\s*\}|"
    r"(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?\([^)]*\)\s*=>\s*\{\s*\})"
)
_RESTATE_COMMENT = re.compile(
    r"(?m)^([ \t]*)#\s*([A-Za-z][\w\s]{2,40})\s*\n\1([A-Za-z_]\w*)\s*="
)
_DUP_COMMENT = re.compile(r"(?m)^([ \t]*)#\s*(.{12,120})\n\1#\s*\2\s*$")
_BOOL_BRANCH_PY = re.compile(
    r"(?m)^([ \t]*)if\b[^\n]+:\n\1[ \t]+return\s+(True|False)\s*\n\1else:\n\1[ \t]+return\s+(True|False)\s*$"
)
_BOOL_BRANCH_JS = re.compile(
    r"(?m)if\s*\([^)]+\)\s*\{\s*return\s+(true|false)\s*;?\s*\}\s*else\s*\{\s*return\s+(true|false)\s*;?\s*\}",
    re.I,
)
_TRIVIAL_ASSERT = re.compile(
    r"(?m)^\s*(?:assert\s+True\b|assert\s+1\b|self\.assertTrue\(\s*True\s*\)|"
    r"expect\(\s*true\s*\)\.toBe\(\s*true\s*\)|assert\.\s*(?:ok|isTrue)\(\s*true\s*\))",
    re.I,
)
_FORWARD_PY = re.compile(
    r"(?m)^([ \t]*)def\s+(\w+)\s*\(([^)]*)\):\n\1[ \t]+return\s+self\.(\w+)\(\2\)\s*$"
)
_PY_BARE_RAISE = re.compile(
    r"(?m)^([ \t]*)except\b[^\n]*:\n\1[ \t]+raise\s*$"
)
_JS_RETHROW = re.compile(
    r"(?m)catch\s*\((\w+)\)\s*\{\s*throw\s+\1\s*;?\s*\}"
)
_PY_PRINT_HANDLER = re.compile(
    r"(?m)^([ \t]*)except\b[^\n]*:\n\1[ \t]+print\([^)]*\)\s*$"
)
_JS_CONSOLE_HANDLER = re.compile(
    r"(?m)catch\s*\([^)]*\)\s*\{\s*console\.(?:log|warn|error)\([^)]*\)\s*;?\s*\}"
)
_JSX_EMPTY_HANDLER = re.compile(
    r"(?m)\bon[A-Z]\w*\s*=\s*\{\s*(?:\(\s*\)|[A-Za-z_]\w*)\s*=>\s*\{\s*\}\s*\}"
)
_ACTION_OK_STUB = re.compile(
    r"(?m)(?:async\s+)?function\s+((?:handle|do|run|submit|save|create|update|delete|send)\w*)\s*\([^)]*\)\s*\{"
    r"\s*return\s*\{\s*(?:ok|success)\s*:\s*true\s*\}\s*;?\s*\}",
    re.I,
)
_ACTION_OK_ARROW = re.compile(
    r"(?m)(?:const|let|var)\s+((?:handle|do|run|submit|save|create|update|delete|send)\w*)\s*=\s*"
    r"(?:async\s+)?\([^)]*\)\s*=>\s*(?:\{\s*return\s*)?\{\s*(?:ok|success)\s*:\s*true\s*\}",
    re.I,
)
_STEP_BANNER = re.compile(
    r"(?m)^[ \t]*(?://|#)\s*(?:[=*-]{3,}\s*)?(?:Step|Phase)\s+\d+\b",
    re.I,
)
_INVENTORY_COUNT = re.compile(
    r"(?mi)\b(?:there are|we have|includes?|supports?|covers?)\s+(\d{2,}|[2-9])\s+"
    r"(?:rules?|commands?|integrations?|endpoints?|checks?|findings?)\b"
)
_MD_HEADING_GAP = re.compile(
    r"(?m)^(#{1,6})\s+(\S[^\n]*)\n(?:[ \t]*\n)+(?=#{1,6}\s|\Z)"
)


def _check_deadweight(text: str, path: str, lang: Optional[str], out: List[Finding]) -> None:
    if lang in {
        "python", "javascript", "typescript", "java", "kotlin", "csharp",
        "php", "go", "rust", "swift", "cpp", "ruby",
    } and not _is_test_path(path):
        for m in _TODO_MARKER.finditer(text):
            _add(out, text, path, m.start(), "DEAD002")

    if lang == "python":
        for m in _PY_EMPTY_EXCEPT.finditer(text):
            _add(out, text, path, m.start(), "DEAD001")
        for m in _PY_EXCEPT_NONE.finditer(text):
            _add(out, text, path, m.start(), "DEAD004")
        for m in _EMPTY_DEF_PY.finditer(text):
            if "abstractmethod" in text[max(0, m.start() - 80) : m.start()]:
                continue
            _add(out, text, path, m.start(), "DEAD005")
        for m in _RESTATE_COMMENT.finditer(text):
            comment = m.group(2).strip().lower().replace(" ", "")
            name = m.group(3).lower()
            if comment == name or comment.replace("_", "") == name.replace("_", ""):
                _add(out, text, path, m.start(), "DEAD006")
        for m in _DUP_COMMENT.finditer(text):
            _add(out, text, path, m.start(), "DEAD007")
        for m in _FORWARD_PY.finditer(text):
            if m.group(2) == m.group(4):
                _add(out, text, path, m.start(), "DEAD008")
        for m in _BOOL_BRANCH_PY.finditer(text):
            if m.group(2) != m.group(3):
                _add(out, text, path, m.start(), "DEAD009")
        for m in _PY_BARE_RAISE.finditer(text):
            _add(out, text, path, m.start(), "DEAD014")
        for m in _PY_PRINT_HANDLER.finditer(text):
            _add(out, text, path, m.start(), "DEAD015")

    if lang in {"javascript", "typescript", "java", "kotlin", "csharp", "php", "cpp"}:
        for m in _BRACE_EMPTY_CATCH.finditer(text):
            _add(out, text, path, m.start(), "DEAD001")

    if lang in {"javascript", "typescript"}:
        for m in _JS_CATCH_NULL.finditer(text):
            _add(out, text, path, m.start(), "DEAD004")
        for m in _EMPTY_FN_JS.finditer(text):
            _add(out, text, path, m.start(), "DEAD005")
        for m in _BOOL_BRANCH_JS.finditer(text):
            if m.group(1).lower() != m.group(2).lower():
                _add(out, text, path, m.start(), "DEAD009")
        for m in _JS_RETHROW.finditer(text):
            _add(out, text, path, m.start(), "DEAD014")
        for m in _JS_CONSOLE_HANDLER.finditer(text):
            _add(out, text, path, m.start(), "DEAD015")
        if not _is_test_path(path):
            for m in _JSX_EMPTY_HANDLER.finditer(text):
                _add(out, text, path, m.start(), "DEAD016")
            for m in _ACTION_OK_STUB.finditer(text):
                _add(out, text, path, m.start(), "DEAD017", observation=m.group(1))
            for m in _ACTION_OK_ARROW.finditer(text):
                _add(out, text, path, m.start(), "DEAD017", observation=m.group(1))

    if lang in {"python", "javascript", "typescript", "go", "rust", "java"}:
        for m in _UNIMPLEMENTED.finditer(text):
            _add(out, text, path, m.start(), "DEAD003")

    if lang in {"python", "javascript", "typescript"}:
        for m in _TRIVIAL_ASSERT.finditer(text):
            _add(out, text, path, m.start(), "DEAD010")

    # DEAD011 — ten identical substantial lines
    if lang and lang not in {"markdown", "rst", "text"} and not _is_test_path(path):
        lines = text.splitlines()
        substantial = [(i, ln) for i, ln in enumerate(lines) if len(ln.strip()) >= 20 and not ln.strip().startswith(("#", "//", "*"))]
        if len(substantial) >= 20:
            window = 10
            seen = set()
            for i in range(len(substantial) - window + 1):
                block = tuple(ln for _, ln in substantial[i : i + window])
                key = "\n".join(block)
                if key in seen:
                    continue
                for j in range(i + window, len(substantial) - window + 1):
                    other = tuple(ln for _, ln in substantial[j : j + window])
                    if other == block:
                        seen.add(key)
                        line_no = substantial[i][0]
                        offset = sum(len(lines[k]) + 1 for k in range(line_no))
                        _add(
                            out, text, path, min(offset, max(0, len(text) - 1)), "DEAD011",
                            observation=f"10 identical lines starting at line {line_no + 1}",
                        )
                        break

    # DEAD018 — Step/Phase banners
    steps = list(_STEP_BANNER.finditer(text))
    if len(steps) >= 2:
        _add(out, text, path, steps[0].start(), "DEAD018",
             observation=f"{len(steps)} Step/Phase banners")

    # DEAD013 — inventory counts in prose/docs/comments
    if lang in {"markdown", "rst", "text", "python"} or Path(path).suffix.lower() in {".md", ".rst", ".txt"}:
        for m in _INVENTORY_COUNT.finditer(text):
            _add(out, text, path, m.start(), "DEAD013", observation=m.group(0)[:80])


def _check_docs(text: str, path: str, lang: Optional[str], out: List[Finding]) -> None:
    if lang not in {"markdown", "rst"}:
        return
    for m in _MD_HEADING_GAP.finditer(text):
        title = m.group(2).strip()
        if title.lower() in {"table of contents", "toc", "license", "changelog"}:
            continue
        _add(out, text, path, m.start(), "DEAD012", observation=f"section '{title}' has no body")


# --- vibecheck -------------------------------------------------------------

def _pat(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.I)


_TRANS_RE = _pat(
    r"that said", r"put differently", r"the real question is", r"at the end of the day",
    r"when it comes to", r"it'?s worth noting", r"it is worth noting",
    r"in today'?s (?:world|landscape)", r"at its core", r"make no mistake",
    r"needless to say", r"to be clear", r"importantly,", r"notably,", r"crucially,",
)
_ASSIST_RE = _pat(
    r"absolutely[!.,]", r"great question", r"of course[!.,]", r"happy to help",
    r"let me (?:explain|break|walk)", r"here'?s (?:the thing|what|why)",
    r"you(?:'re| are) (?:absolutely )?right", r"i hope this helps", r"as an ai",
)
_MOD_RE = _pat(
    r"\brobust\b", r"\bseamless(?:ly)?\b", r"\bcomprehensive(?:ly)?\b",
    r"\bcutting[- ]edge\b", r"\brevolutionary\b", r"\bgame[- ]changing\b",
    r"\bcrucial(?:ly)?\b", r"\bessential(?:ly)?\b", r"\bdelve\b", r"\blandscape\b",
    r"\bleverage\b", r"\bempower(?:s|ing)?\b", r"\bstreamline[ds]?\b",
)
_HEDGE_RE = _pat(
    r"it can be argued", r"to some extent", r"in many ways", r"it depends",
    r"more or less", r"generally speaking", r"for the most part",
    r"it is important to note", r"one might say", r"arguably,",
)
_BALANCE_RE = _pat(
    r"on the one hand", r"on the other hand",
    r"neither approach is inherently better", r"both have their (?:pros|merits)",
    r"there is no one[- ]size[- ]fits[- ]all",
)
_STRUCTURE_RE = _pat(
    r"first and foremost", r"there are three key points", r"let'?s break this down",
    r"in this (?:section|article|post)(?: we)?(?: will)?", r"without further ado",
    r"as follows:", r"the following sections",
)
_REVEAL_RE = _pat(
    r"(?:the|here'?s the) (?:result|answer|kicker|twist)\s*:",
    r"here'?s (?:the thing|why|how)\s*:",
)
_DISCLAIMER_RE = _pat(
    r"results may vary", r"there is no one[- ]size[- ]fits[- ]all answer",
    r"this is not (?:financial|legal|medical) advice", r"your mileage may vary",
    r"not intended as (?:a )?substitute",
)
_CONCLUSION_RE = _pat(
    r"\bin conclusion\b", r"the key takeaway", r"this serves as a reminder",
    r"to summarize", r"wrapping up", r"in summary",
)
_DEMONSTRATIVE_RE = _pat(
    r"\bthis approach\b", r"\bthis dynamic\b", r"\bthat perspective\b",
    r"\bthis paradigm\b", r"\bthis landscape\b", r"\bthis narrative\b",
    r"\bthis reality\b", r"\bthat dynamic\b",
)
_METAPHOR_RE = _pat(
    r"\blandscape\b", r"double[- ]edged sword", r"tip of the iceberg",
    r"move the needle", r"low[- ]hanging fruit", r"paradigm shift",
    r"silver bullet", r"perfect storm",
)
_PROMO_RE = _pat(
    r"exciting opportunity", r"strong foundation", r"game[- ]changer",
    r"unlock(?:s|ing)? (?:the )?potential", r"empower(?:s|ing)? (?:teams|users|developers)",
    r"seamless experience",
)
_CHATBOT_RE = _pat(
    r"as an ai(?: language model)?", r"i don'?t have (?:personal )?experiences?",
    r"my (?:knowledge|training) cutoff", r"i (?:cannot|can't) browse the (?:internet|web)",
    r"i'?m (?:just )?an? (?:ai|language model|assistant)",
)
_CONTRAST_RE = _pat(
    r"it'?s not (?:just )?about .{5,40},\s*it'?s about",
    r"the (?:goal|point|aim) isn'?t .{5,40}[;,]?\s*it'?s",
    r"not merely .{5,40} but",
)
_CORRELATIVE_RE = _pat(
    r"not only .{5,60} but also", r"everything from .{5,40} to ",
    r"\bboth\b.{5,40}\band\b",
)
_JARGON_RE = _pat(
    r"\bleverage\b", r"key stakeholders?", r"drive alignment", r"synerg(?:y|ies|ize)",
    r"circle back", r"move the needle", r"value[- ]add", r"bandwidth",
    r"deep dive", r"actionable insights?",
)
_AI_WORDS_RE = _pat(
    r"\bnuanced\b", r"\bholistic\b", r"\bdelve\b", r"\btestament\b",
    r"\brealm\b", r"\btapestry\b", r"\bunderscore[sd]?\b", r"\bpivotal\b",
)
_LESSON_RE = _pat(
    r"this (?:serves as|is) a reminder that", r"is only as good as the",
    r"the lesson here is", r"what this (?:really )?shows is",
)
_DRAMA_RE = _pat(
    r"pivotal moment", r"fundamental shift", r"inflection point",
    r"watershed moment", r"sea change", r"quantum leap",
)
_PARAPHRASE_Q_RE = _pat(
    r"you(?:'re| are) (?:essentially |basically )?asking (?:whether|if|about)",
    r"what you(?:'re| are) really asking",
)
_TRIAD_RE = re.compile(
    r"\b([A-Za-z]{3,}),\s+([A-Za-z]{3,}),?\s+and\s+([A-Za-z]{3,})\b"
)


def _prose_words(text: str) -> int:
    return max(1, len(re.findall(r"[A-Za-z]{2,}", text)))


def _is_prose_path(path: str, lang: Optional[str]) -> bool:
    if lang in {"markdown", "rst", "text"}:
        return True
    return Path(path).suffix.lower() in {".md", ".mdx", ".rst", ".txt"}


def _density_hit(
    out: List[Finding], text: str, path: str, matches: List[re.Match],
    rule_id: str, min_count: int, words_per: int, words: int,
    min_forms: int = 1,
) -> None:
    if len(matches) < min_count:
        return
    forms = {m.group(0).lower()[:24] for m in matches}
    if len(forms) < min_forms:
        return
    if len(matches) * words_per < words:
        return
    _add(out, text, path, matches[0].start(), rule_id,
         observation=f"{len(matches)} matches across {words} words")


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if len(p.split()) >= 5]


def _check_vibe(text: str, path: str, lang: Optional[str], out: List[Finding]) -> None:
    if not _is_prose_path(path, lang):
        return
    words = _prose_words(text)

    _density_hit(out, text, path, list(_TRANS_RE.finditer(text)), "VIBE001", 4, 120, words)
    assists = list(_ASSIST_RE.finditer(text))
    if len(assists) >= 2 and len({m.group(0).lower()[:12] for m in assists}) >= 2:
        _add(out, text, path, assists[0].start(), "VIBE002",
             observation=f"{len(assists)} assistant phrases")
    _density_hit(out, text, path, list(_MOD_RE.finditer(text)), "VIBE003", 6, 80, words)
    _density_hit(out, text, path, list(_HEDGE_RE.finditer(text)), "VIBE004", 7, 70, words)
    bals = list(_BALANCE_RE.finditer(text))
    if len(bals) >= 2:
        _add(out, text, path, bals[0].start(), "VIBE005", observation=f"{len(bals)} balance templates")
    _density_hit(out, text, path, list(_STRUCTURE_RE.finditer(text)), "VIBE006", 5, 100, words)

    em = list(re.finditer(r"—", text))
    if len(em) >= 5 and len(em) * 100 >= words:
        _add(out, text, path, em[0].start(), "VIBE007", observation=f"{len(em)} em dashes")

    reveals = list(_REVEAL_RE.finditer(text))
    if len(reveals) >= 3 and len(reveals) * 200 >= words:
        _add(out, text, path, reveals[0].start(), "VIBE008", observation=f"{len(reveals)} staged reveals")

    sents = _sentences(text)
    if len(sents) >= 4:
        openings: Dict[str, List[int]] = {}
        for i, s in enumerate(sents):
            key = " ".join(s.split()[:2]).lower()
            openings.setdefault(key, []).append(i)
        for key, idxs in openings.items():
            if len(idxs) >= 4 and len(idxs) * 2 >= len(sents):
                # approximate offset of first such sentence
                pos = text.lower().find(sents[idxs[0]][:40].lower())
                _add(out, text, path, max(0, pos), "VIBE009",
                     observation=f"{len(idxs)} sentences open with '{key}'")
                break

        # VIBE010 uniform lengths
        if len(sents) >= 8:
            for i in range(len(sents) - 7):
                counts = [len(s.split()) for s in sents[i : i + 8]]
                if max(counts) - min(counts) <= 3:
                    pos = text.lower().find(sents[i][:40].lower())
                    _add(out, text, path, max(0, pos), "VIBE010",
                         observation="8 consecutive sentences within ±3 words")
                    break

        # VIBE017 restatement
        for i in range(len(sents) - 1):
            a = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", sents[i])}
            b = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", sents[i + 1])}
            if len(a) >= 7 and len(b) >= 7:
                shared = a & b
                if len(shared) >= 7 and len(shared) / max(1, len(a | b)) >= 0.75:
                    pos = text.lower().find(sents[i][:40].lower())
                    _add(out, text, path, max(0, pos), "VIBE017",
                         observation=f"{len(shared)} shared content words")
                    break

    triads = list(_TRIAD_RE.finditer(text))
    abstract = [m for m in triads if all(g.isalpha() and g[0].islower() for g in m.groups())]
    if len(abstract) >= 3 and len(abstract) * 150 >= words:
        _add(out, text, path, abstract[0].start(), "VIBE011",
             observation=f"{len(abstract)} abstract triads")

    _density_hit(out, text, path, list(_DEMONSTRATIVE_RE.finditer(text)), "VIBE012", 8, 80, words)
    _density_hit(out, text, path, list(_METAPHOR_RE.finditer(text)), "VIBE013", 5, 120, words, min_forms=4)
    _density_hit(out, text, path, list(_PROMO_RE.finditer(text)), "VIBE014", 4, 150, words, min_forms=3)

    discs = list(_DISCLAIMER_RE.finditer(text))
    if len(discs) >= 4:
        _add(out, text, path, discs[0].start(), "VIBE015", observation=f"{len(discs)} disclaimers")

    conc = list(_CONCLUSION_RE.finditer(text))
    if len(conc) >= 3:
        latter = sum(1 for m in conc if m.start() * 2 >= len(text))
        if latter >= 2:
            _add(out, text, path, conc[0].start(), "VIBE016", observation=f"{len(conc)} conclusion markers")

    # VIBE018 paragraph uniformity
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.split()) >= 40]
    if len(paras) >= 5:
        for i in range(len(paras) - 4):
            counts = [len(p.split()) for p in paras[i : i + 5]]
            avg = sum(counts) / len(counts)
            if avg and all(abs(c - avg) / avg <= 0.20 for c in counts):
                pos = text.find(paras[i][:40])
                _add(out, text, path, max(0, pos), "VIBE018")
                break

    bots = list(_CHATBOT_RE.finditer(text))
    if bots:
        _add(out, text, path, bots[0].start(), "VIBE019")

    _density_hit(out, text, path, list(_CONTRAST_RE.finditer(text)), "VIBE020", 3, 250, words, min_forms=2)
    cors = list(_CORRELATIVE_RE.finditer(text))
    strong = [m for m in cors if re.search(r"not only|everything from", m.group(0), re.I)]
    if len(strong) >= 2 or (len(cors) >= 4 and len(cors) * 150 >= words and strong):
        _add(out, text, path, cors[0].start(), "VIBE021", observation=f"{len(cors)} correlatives")

    _density_hit(out, text, path, list(_JARGON_RE.finditer(text)), "VIBE022", 6, 100, words, min_forms=4)
    _density_hit(out, text, path, list(_AI_WORDS_RE.finditer(text)), "VIBE023", 5, 120, words, min_forms=3)
    _density_hit(out, text, path, list(_LESSON_RE.finditer(text)), "VIBE024", 2, 400, words, min_forms=2)
    _density_hit(out, text, path, list(_DRAMA_RE.finditer(text)), "VIBE025", 2, 300, words, min_forms=2)

    pq = list(_PARAPHRASE_Q_RE.finditer(text))
    if pq:
        _add(out, text, path, pq[0].start(), "VIBE026")


# --- papertrail ------------------------------------------------------------

_PLACEHOLDER_SUBJECT = re.compile(
    r"^(?:wip|fix|tmp|temp|update|updates|misc|stuff|changes|checkpoint|asdf|test)\s*$",
    re.I,
)
_AUTOSQUASH_SUBJECT = re.compile(r"^(?:fixup|squash|amend)!", re.I)


def check_commit_subject(
    subject: str,
    path: str = "COMMIT_EDITMSG",
    *,
    allow_autosquash: bool = False,
) -> List[Finding]:
    s = subject.strip()
    out: List[Finding] = []
    if not s or _PLACEHOLDER_SUBJECT.match(s):
        out.append(Finding(path, 1, "TRAIL001", RULES["TRAIL001"].message, s or "(empty)",
                           observation="subject is a placeholder"))
    if s and _AUTOSQUASH_SUBJECT.match(s) and not allow_autosquash:
        out.append(Finding(path, 1, "TRAIL002", RULES["TRAIL002"].message, s,
                           observation="autosquash marker should not reach the integration branch"))
    return out


def check_commit_history(subjects: Sequence[Tuple[str, str]]) -> List[Finding]:
    """*subjects*: [(sha, subject), ...] — TRAIL001/002 over a commit range."""
    out: List[Finding] = []
    for sha, subject in subjects:
        for f in check_commit_subject(subject, path=sha[:9], allow_autosquash=False):
            out.append(f)
    return out


# --- public scan API -------------------------------------------------------

def list_rules() -> List[Rule]:
    return [RULES[k] for k in sorted(RULES)]


def scan_text(text: str, path: str) -> List[Finding]:
    """Run all applicable rules on one UTF-8 text artifact."""
    if not text or "\0" in text[:8000]:
        return []
    if len(text) > 400_000:
        text = text[:200_000] + "\n" + text[-200_000:]
    lang = language_of(path)
    out: List[Finding] = []
    _check_deadweight(text, path, lang, out)
    _check_docs(text, path, lang, out)
    _check_vibe(text, path, lang, out)
    return out


def scan_file(path: Path, repo_relative: Optional[str] = None) -> List[Finding]:
    rel = repo_relative or str(path)
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\0" in raw[:8000]:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
    return scan_text(text, rel)


def scan_paths(root: Path, paths: Sequence[Path]) -> List[Finding]:
    findings: List[Finding] = []
    for p in paths:
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not _skip_path(f, root):
                    findings.extend(scan_file(f, str(f.relative_to(root))))
        elif p.is_file():
            try:
                rel = str(p.resolve().relative_to(root.resolve()))
            except ValueError:
                rel = p.name
            if not _skip_path(p, root):
                findings.extend(scan_file(p, rel))
    return findings


def _skip_path(path: Path, root: Path) -> bool:
    try:
        parts = set(path.resolve().relative_to(root.resolve()).parts)
    except ValueError:
        parts = set(path.parts)
    skip_dirs = {
        ".git", "node_modules", "vendor", "dist", "build", ".venv", "venv",
        "__pycache__", ".next", ".cache", "target", "coverage",
    }
    if parts & skip_dirs:
        return True
    if path.suffix.lower() in {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
        ".woff", ".woff2",
    }:
        return True
    if language_of(str(path)) is None and path.suffix.lower() not in {".txt"}:
        return True
    return False


def blob_texts(repo: Path, shas: Sequence[str]) -> Dict[str, str]:
    """Read blob contents via one ``git cat-file --batch`` stream."""
    out: Dict[str, str] = {}
    if not shas:
        return out
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout
    import threading

    def _feed() -> None:
        for s in shas:
            proc.stdin.write((s + "\n").encode())
        proc.stdin.close()

    threading.Thread(target=_feed, daemon=True).start()
    rd = proc.stdout
    for _ in shas:
        header = rd.readline()
        if not header:
            break
        parts = header.split()
        if len(parts) < 3:
            continue
        sha, size = parts[0].decode(), int(parts[2])
        body = rd.read(size)
        rd.read(1)
        if b"\0" in body[:8000]:
            out[sha] = ""
            continue
        out[sha] = body.decode("utf-8", "replace")
    proc.wait()
    return out


def scan_blobs(
    repo: Path,
    entries: Iterable[Tuple[str, str]],
    progress=None,
) -> Dict[str, WasteBlob]:
    """Scan unique blobs. *entries* is ``[(blob_sha, path), ...]``."""
    first_path: Dict[str, str] = {}
    for blob, path in entries:
        if blob not in first_path and language_of(path) is not None:
            first_path[blob] = path
    shas = list(first_path)
    if progress:
        progress(f"  scanning waste in {len(shas):,} blobs …")
    texts = blob_texts(repo, shas)
    result: Dict[str, WasteBlob] = {}
    for sha, path in first_path.items():
        text = texts.get(sha, "")
        result[sha] = WasteBlob(findings=scan_text(text, path) if text else [])
    return result


@dataclass
class WasteStats:
    total: int = 0
    errors: int = 0
    warnings: int = 0
    info: int = 0
    deadweight: int = 0
    vibecheck: int = 0

    def add(self, blob: WasteBlob, occurrences: int = 1) -> None:
        if not blob.findings or occurrences <= 0:
            return
        for f in blob.findings:
            self.total += occurrences
            if f.severity == "error":
                self.errors += occurrences
            elif f.severity == "warning":
                self.warnings += occurrences
            else:
                self.info += occurrences
            if f.rule.module == "deadweight":
                self.deadweight += occurrences
            elif f.rule.module == "vibecheck":
                self.vibecheck += occurrences

    def as_tuple(self) -> Tuple[int, int, int, int, int, int]:
        return (self.total, self.errors, self.warnings, self.info, self.deadweight, self.vibecheck)


def format_findings(findings: Sequence[Finding], *, fancy: bool = True) -> str:
    if not findings:
        return "0 finding(s). Clean." if fancy else ""
    lines = []
    for f in findings:
        lines.append(f"{f.path}:{f.line}:1  {f.rule_id}  {f.severity}")
        lines.append(f.rule.message if not f.observation else f"{f.rule.message} ({f.observation})")
        if f.evidence:
            lines.append(f"  | {f.evidence[:120]}")
        lines.append("")
    tail = f"{len(findings)} finding(s)."
    if fancy and any(f.severity != "info" for f in findings):
        tail += " Nice try, robot."
    lines.append(tail)
    return "\n".join(lines)


def format_github_annotations(findings: Sequence[Finding]) -> str:
    """GitHub Actions workflow-command annotations."""
    lines = []
    for f in findings:
        lvl = "error" if f.severity == "error" else "warning"
        msg = f"{f.rule_id}: {f.message}"
        if f.observation:
            msg += f" ({f.observation})"
        # Escape control chars for workflow commands
        msg = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        lines.append(f"::{lvl} file={f.path},line={f.line}::{msg}")
    return "\n".join(lines)


def filter_by_level(findings: Sequence[Finding], fail_level: str) -> List[Finding]:
    threshold = SEVERITY_RANK.get(fail_level, 1)
    return [f for f in findings if SEVERITY_RANK.get(f.severity, 0) >= threshold]
