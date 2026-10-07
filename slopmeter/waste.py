"""Deterministic waste (deadweight + vibe) detection.

Complements the attention-gap slop score: that model asks whether code arrived
faster than humans could own it. These rules ask whether the surface area that
*did* arrive looks unused, unfinished, or formulaically padded.

No network, no models — pattern density with quiet boundaries. Designed for
two consumers:

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
        Rule("DEAD001", "deadweight", "error", "Exception swallowed without handling"),
        Rule("DEAD003", "deadweight", "error", "Unimplemented stub still in the tree"),
        Rule("DEAD005", "deadweight", "warning", "Empty concrete function body"),
        Rule("DEAD006", "deadweight", "info", "Comment restates the next line"),
        Rule("DEAD010", "deadweight", "error", "Assertion that can only pass"),
        Rule("DEAD012", "deadweight", "warning", "Empty or heading-only documentation section"),
        Rule("VIBE001", "vibecheck", "warning", "Stock transition phrases are unusually dense"),
        Rule("VIBE002", "vibecheck", "warning", "Assistant-response framing in prose"),
        Rule("VIBE003", "vibecheck", "info", "Generic evaluative modifiers are dense"),
        Rule("TRAIL001", "papertrail", "warning", "Placeholder commit subject"),
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
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".md": "markdown",
    ".mdx": "markdown",
    ".rst": "rst",
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


# --- deadweight ------------------------------------------------------------

_PY_EMPTY_EXCEPT = re.compile(
    r"(?m)^([ \t]*)except\b[^\n]*:\n(?:\1[ \t]+(?:pass|\.\.\.|ellipsis)\s*(?:#.*)?\n)+"
)
_BRACE_EMPTY_CATCH = re.compile(
    r"(?m)catch\s*\([^)]*\)\s*\{\s*\}|catch\s*\{\s*\}"
)
# Statement forms only — bare `todo!` inside string/regex literals must not match.
_UNIMPLEMENTED = re.compile(
    r"(?m)^\s*(?:raise\s+NotImplementedError\b|raise\s+NotImplemented\b|"
    r"throw\s+new\s+Error\(\s*['\"]TODO|"
    r"throw\s+new\s+(?:Error|Exception)\(\s*['\"]not\s+implemented|"
    r"panic\(\s*['\"]TODO|"
    r"(?:todo|unimplemented)!\()",
    re.IGNORECASE,
)
_EMPTY_DEF_PY = re.compile(
    r"(?m)^([ \t]*)(?:async\s+)?def\s+\w+\s*\([^)]*\)\s*(?:->[^:]+)?:\n"
    r"(?:\1[ \t]+(?:pass|\.\.\.|ellipsis)\s*(?:#.*)?\n)+"
)
_EMPTY_FN_JS = re.compile(
    r"(?m)(?:function\s+\w+\s*\([^)]*\)\s*\{\s*\}|"
    r"(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?\([^)]*\)\s*=>\s*\{\s*\})"
)
_TRIVIAL_ASSERT = re.compile(
    r"(?m)^\s*(?:assert\s+True\b|assert\s+1\b|self\.assertTrue\(\s*True\s*\)|"
    r"expect\(\s*true\s*\)\.toBe\(\s*true\s*\)|assert\.\s*(?:ok|isTrue)\(\s*true\s*\))",
    re.IGNORECASE,
)
_RESTATE_COMMENT = re.compile(
    r"(?m)^([ \t]*)#\s*([A-Za-z][\w\s]{2,40})\s*\n\1([A-Za-z_]\w*)\s*="
)


def _check_deadweight(text: str, path: str, lang: Optional[str], out: List[Finding]) -> None:
    if lang == "python":
        for m in _PY_EMPTY_EXCEPT.finditer(text):
            line, ev = _line_at(text, m.start())
            out.append(Finding(path, line, "DEAD001", RULES["DEAD001"].message, ev))
        for m in _EMPTY_DEF_PY.finditer(text):
            # skip abstract-looking names
            chunk = m.group(0)
            if "abstractmethod" in text[max(0, m.start() - 80) : m.start()]:
                continue
            line, ev = _line_at(text, m.start())
            out.append(Finding(path, line, "DEAD005", RULES["DEAD005"].message, ev.split("\n")[0]))
            _ = chunk
    if lang in {"javascript", "typescript", "java", "kotlin", "csharp", "php"}:
        for m in _BRACE_EMPTY_CATCH.finditer(text):
            line, ev = _line_at(text, m.start())
            out.append(Finding(path, line, "DEAD001", RULES["DEAD001"].message, ev))
    if lang in {"javascript", "typescript"}:
        for m in _EMPTY_FN_JS.finditer(text):
            line, ev = _line_at(text, m.start())
            out.append(Finding(path, line, "DEAD005", RULES["DEAD005"].message, ev))
    if lang in {"python", "javascript", "typescript", "go", "rust", "java"}:
        for m in _UNIMPLEMENTED.finditer(text):
            line, ev = _line_at(text, m.start())
            out.append(Finding(path, line, "DEAD003", RULES["DEAD003"].message, ev))
    if lang in {"python", "javascript", "typescript"}:
        for m in _TRIVIAL_ASSERT.finditer(text):
            line, ev = _line_at(text, m.start())
            out.append(Finding(path, line, "DEAD010", RULES["DEAD010"].message, ev))
    if lang == "python":
        for m in _RESTATE_COMMENT.finditer(text):
            comment = m.group(2).strip().lower().replace(" ", "")
            name = m.group(3).lower()
            if comment == name or comment.replace("_", "") == name.replace("_", ""):
                line, ev = _line_at(text, m.start())
                out.append(Finding(path, line, "DEAD006", RULES["DEAD006"].message, ev))


# Heading followed only by blank lines then another heading / EOF
_MD_HEADING_GAP = re.compile(
    r"(?m)^(#{1,6})\s+(\S[^\n]*)\n(?:[ \t]*\n)+(?=#{1,6}\s|\Z)"
)


def _check_docs(text: str, path: str, lang: Optional[str], out: List[Finding]) -> None:
    if lang not in {"markdown", "rst"}:
        return
    for m in _MD_HEADING_GAP.finditer(text):
        title = m.group(2).strip()
        if title.lower() in {"table of contents", "toc", "license", "changelog"}:
            continue
        line, ev = _line_at(text, m.start())
        out.append(
            Finding(
                path,
                line,
                "DEAD012",
                RULES["DEAD012"].message,
                ev,
                observation=f"section '{title}' has no body",
            )
        )


# --- vibecheck -------------------------------------------------------------

_TRANSITIONS = (
    r"that said",
    r"put differently",
    r"the real question is",
    r"at the end of the day",
    r"when it comes to",
    r"it'?s worth noting",
    r"it is worth noting",
    r"in today'?s (?:world|landscape)",
    r"at its core",
    r"make no mistake",
    r"needless to say",
    r"to be clear",
    r"importantly,",
    r"notably,",
    r"crucially,",
)
_ASSISTANT = (
    r"absolutely[!.,]",
    r"great question",
    r"of course[!.,]",
    r"happy to help",
    r"let me (?:explain|break|walk)",
    r"here'?s (?:the thing|what|why)",
    r"you(?:'re| are) (?:absolutely )?right",
    r"i hope this helps",
    r"as an ai",
)
_MODIFIERS = (
    r"\brobust\b",
    r"\bseamless(?:ly)?\b",
    r"\bcomprehensive(?:ly)?\b",
    r"\bcutting[- ]edge\b",
    r"\brevolutionary\b",
    r"\bgame[- ]changing\b",
    r"\bcrucial(?:ly)?\b",
    r"\bessential(?:ly)?\b",
    r"\bdelve\b",
    r"\blandscape\b",
    r"\bleverage\b",
    r"\bempower(?:s|ing)?\b",
    r"\bstreamline[ds]?\b",
)

_TRANS_RE = re.compile("|".join(_TRANSITIONS), re.I)
_ASSIST_RE = re.compile("|".join(_ASSISTANT), re.I)
_MOD_RE = re.compile("|".join(_MODIFIERS), re.I)


def _prose_words(text: str) -> int:
    return max(1, len(re.findall(r"[A-Za-z]{2,}", text)))


def _check_vibe(text: str, path: str, lang: Optional[str], out: List[Finding]) -> None:
    # Prefer docs and comments-heavy files; still scan .md fully.
    if lang not in {"markdown", "rst", None} and Path(path).suffix.lower() not in {
        ".md", ".mdx", ".rst", ".txt",
    }:
        # Light check on code comments only would need a parser; skip code for vibe.
        if lang in {"python", "javascript", "typescript", "go", "rust", "java"}:
            return

    words = _prose_words(text)
    transitions = list(_TRANS_RE.finditer(text))
    if len(transitions) >= 4 and len(transitions) * 120 >= words:
        line, ev = _line_at(text, transitions[0].start())
        out.append(
            Finding(
                path, line, "VIBE001", RULES["VIBE001"].message, ev,
                observation=f"{len(transitions)} stock transitions across {words} words",
            )
        )

    assists = list(_ASSIST_RE.finditer(text))
    forms = {m.group(0).lower()[:12] for m in assists}
    if len(assists) >= 2 and len(forms) >= 2:
        line, ev = _line_at(text, assists[0].start())
        out.append(
            Finding(
                path, line, "VIBE002", RULES["VIBE002"].message, ev,
                observation=f"{len(assists)} assistant phrases ({len(forms)} forms)",
            )
        )

    mods = list(_MOD_RE.finditer(text))
    if len(mods) >= 6 and len(mods) * 80 >= words:
        line, ev = _line_at(text, mods[0].start())
        out.append(
            Finding(
                path, line, "VIBE003", RULES["VIBE003"].message, ev,
                observation=f"{len(mods)} generic modifiers across {words} words",
            )
        )


# --- papertrail ------------------------------------------------------------

_PLACEHOLDER_SUBJECT = re.compile(
    r"^(?:wip|fix|tmp|temp|update|updates|misc|stuff|changes|checkpoint|asdf|test)\s*$",
    re.I,
)


def check_commit_subject(subject: str, path: str = "COMMIT_EDITMSG") -> List[Finding]:
    s = subject.strip()
    if not s or _PLACEHOLDER_SUBJECT.match(s):
        return [
            Finding(path, 1, "TRAIL001", RULES["TRAIL001"].message, s or "(empty)",
                    observation="subject is a placeholder")
        ]
    return []


# --- public scan API -------------------------------------------------------

def scan_text(text: str, path: str) -> List[Finding]:
    """Run all applicable rules on one UTF-8 text artifact."""
    if not text or "\0" in text[:8000]:
        return []
    # Cap huge files — still scan head+tail enough for density rules.
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
            rel = str(p.relative_to(root)) if root in p.resolve().parents or p.resolve() == root.resolve() else p.name
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
    """Scan unique blobs. *entries* is ``[(blob_sha, path), ...]``.

    Returns ``{blob_sha: WasteBlob}`` using the first path seen for language.
    """
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
        if not text:
            result[sha] = WasteBlob()
            continue
        findings = scan_text(text, path)
        # Re-home paths are left as the representative path; aggregation counts
        # once per blob occurrence in the tree caller.
        result[sha] = WasteBlob(findings=findings)
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
            sev = f.severity
            if sev == "error":
                self.errors += occurrences
            elif sev == "warning":
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
    if fancy:
        tail += " Nice try, robot." if any(f.severity != "info" for f in findings) else ""
    lines.append(tail)
    return "\n".join(lines)


def filter_by_level(findings: Sequence[Finding], fail_level: str) -> List[Finding]:
    threshold = SEVERITY_RANK.get(fail_level, 1)
    return [f for f in findings if SEVERITY_RANK.get(f.severity, 0) >= threshold]
