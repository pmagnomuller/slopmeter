"""Parse git history into typed commit records.

Reads `git log --numstat` and returns a chronological list of :class:`Commit`.
Everything here is stdlib-only so the tool runs on a bare Python install.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Commits whose author name or email matches any of these are treated as
# machine-generated. Machine commits generate code but do not generate the
# human "attention" that the slop score is measuring.
_BOT_PATTERNS: List[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\[bot\]",  # dependabot[bot], github-actions[bot], ...
        r"\bbot\b",
        r"\bdependabot\b",
        r"\brenovate\b",
        r"\bimgbot\b",
        r"\bcodex\b",
        r"\bclaude\b",
        r"\bcopilot\b",
        r"\bopenclaw\b",
        r"\bclawdbot\b",
        r"\bgpt\b",
        r"\bchatgpt\b",
        r"\bopenai\b",
        r"\bgithub-actions\b",
        r"\bagent\b",
        r"\bauto\s?-?commit\b",
        r"\bauto\s?-?gen\b",
    )
]

# Commit-message evidence that a human commit was AI-assisted (Claude Code,
# Codex, Copilot, Cursor ...). Matched against the body + Co-Authored-By trailers.
_AI_MARKERS: List[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"co-authored-by:.*\b(claude|codex|copilot|cursor|gpt|openai|anthropic|gemini|devin|aider|windsurf|noreply@anthropic)",
        r"generated with \[?claude code",
        r"\bclaude code\b",
        r"\bcodex\b",
        r"\bgithub copilot\b",
        r"🤖",
        r"\bai[- ]generated\b",
        r"\bai[- ]assisted\b",
    )
]

# Files in these directories are vendored/generated and never demand human
# attention, regardless of extension.
_VENDORED_DIRS = (
    "node_modules",
    "vendor",
    "vendored",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".cache",
    "__pycache__",
    "Pods",
    "Carthage",
    "DerivedData",
)

_MERGE_ARROW = " => "
_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t(.*)$")


@dataclass(frozen=True)
class FileChange:
    """A single file touched in a commit."""

    added: int
    deleted: int
    path: str

    @property
    def is_rename(self) -> bool:
        return _MERGE_ARROW in self.path

    @property
    def new_path(self) -> str:
        # With --find-renames, renames show as "old => new". The lines live in
        # the new path, so that is the path we score against.
        return self.path.split(_MERGE_ARROW)[-1].strip() if self.is_rename else self.path


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    email: str
    date: datetime
    subject: str
    files: Tuple[FileChange, ...] = field(default_factory=tuple)
    body: str = ""

    @property
    def is_bot(self) -> bool:
        """Author itself is a machine (dependabot, github-actions, claude ...)."""
        haystack = f"{self.author} {self.email}"
        return any(p.search(haystack) for p in _BOT_PATTERNS)

    @property
    def is_ai(self) -> bool:
        """Bot-authored, or human-authored with AI co-author/marker evidence."""
        if self.is_bot:
            return True
        return any(p.search(self.body) for p in _AI_MARKERS)


def is_vendored(path: str) -> bool:
    parts = Path(path).parts
    return any(part in _VENDORED_DIRS for part in parts)


def parse_numstat_line(line: str) -> Optional[FileChange]:
    m = _NUMSTAT_RE.match(line)
    if not m:
        return None
    added, deleted, path = m.groups()

    def _to_int(v: str) -> int:
        return 0 if v == "-" else int(v)

    return FileChange(added=_to_int(added), deleted=_to_int(deleted), path=path)


def run_git_log(repo: Path, branch: Optional[str] = None, first_parent: bool = False) -> str:
    """Return raw `git log --numstat` output for *repo* (optionally a branch)."""
    fmt = "__SM_COMMIT__%x00%H%x00%an%x00%ae%x00%ad%x00%s%x00%b%x00__SM_END__"
    cmd = [
        "git",
        "-C",
        str(repo),
        "log",
        "--numstat",
        "--find-renames",
        "--date=unix",
    ]
    if first_parent:
        cmd.append("--first-parent")
    cmd.append(f"--format={fmt}")
    if branch:
        cmd.append(branch)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git log failed for {repo!s}:\n{proc.stderr.strip()}"
        )
    return proc.stdout


def parse_commits(raw: str) -> List[Commit]:
    """Parse `git log --numstat` output into chronological commits."""
    commits: List[Commit] = []
    blocks = raw.split("__SM_COMMIT__")
    for block in blocks:
        block = block.strip("\n\x00")
        if not block:
            continue
        head, sep, rest = block.partition("__SM_END__")
        if not sep:
            continue
        header = head.split("\x00")
        if len(header) < 6:
            continue
        sha, author, email, date_str, subject, body = header[0], header[1], header[2], header[3], header[4], header[5]
        lines = rest.splitlines()

        try:
            ts = int(date_str)
        except ValueError:
            continue
        date = datetime.fromtimestamp(ts, tz=timezone.utc)

        files: List[FileChange] = []
        for line in lines:
            change = parse_numstat_line(line)
            if change is not None:
                files.append(change)

        commits.append(
            Commit(
                sha=sha,
                author=author,
                email=email,
                date=date,
                subject=subject,
                files=tuple(files),
                body=body.strip(),
            )
        )

    # `git log` emits newest first; normalize to oldest first for time series.
    commits.reverse()
    return commits


def load_commits(repo: Path, branch: Optional[str] = None, first_parent: bool = False) -> List[Commit]:
    raw = run_git_log(repo, branch=branch, first_parent=first_parent)
    return parse_commits(raw)


def list_branches(repo: Path) -> List[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def current_branch(repo: Path) -> Optional[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    name = proc.stdout.strip()
    return name if name and name != "HEAD" else None


ENV_BRANCH_NAMES: Dict[str, Tuple[str, ...]] = {
    "prod": ("main", "master"),
    "dev": ("develop", "dev"),
    "staging": ("staging",),
}

# Tag prefixes that mark a deploy to an environment (trunk-based repos):
# stg-v0.1.33, prd-v0.1.7, staging/2026-09-01, release-1.2.0 ...
_TAG_ENV_RE = re.compile(
    r"^(?P<env>dev|development|stg|stage|staging|prd|prod|production|release|rel)[-_/.]",
    re.IGNORECASE,
)
_TAG_ENV_CANON = {
    "dev": "dev", "development": "dev",
    "stg": "stg", "stage": "stg", "staging": "stg",
    "prd": "prd", "prod": "prd", "production": "prd", "release": "prd", "rel": "prd",
}
_ENV_ORDER = {"dev": 0, "stg": 1, "prd": 2, "prod": 2, "staging": 1}


@dataclass(frozen=True)
class EnvSpec:
    """Where an environment's code comes from: a branch, or tags matching a prefix."""

    id: str
    ref: str            # branch name, or tag prefix (e.g. "stg-")
    kind: str = "branch"  # "branch" | "tags"

    @property
    def label(self) -> str:
        return self.ref if self.kind == "branch" else f"tags {self.ref}*"


def list_tags(repo: Path) -> List[Tuple[int, str, str]]:
    """[(creator_unix_ts, commit_sha, tag_name), ...] oldest first (annotated tags peeled)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--sort=creatordate",
         "--format=%(creatordate:unix)%00%(objectname)%00%(*objectname)%00%(refname:short)", "refs/tags"],
        capture_output=True, text=True,
    )
    out: List[Tuple[int, str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x00")
        if len(parts) < 4 or not parts[0]:
            continue
        ts, obj, peeled, name = parts
        out.append((int(ts), peeled or obj, name))
    return out


def tag_heads(repo: Path, prefix: str) -> List[Tuple[str, str, str]]:
    """Last deploy per UTC day for tags starting with *prefix*: [(iso_date, sha, tag), ...]."""
    by_day: Dict[str, Tuple[str, str]] = {}
    for ts, sha, name in list_tags(repo):
        if not name.lower().startswith(prefix.lower()):
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        by_day[day] = (sha, name)  # sorted by creatordate, so later wins
    return [(d, v[0], v[1]) for d, v in sorted(by_day.items())]


def detect_envs(repo: Path) -> List[EnvSpec]:
    """Auto-detect environments.

    1. Branch-based: main/master -> prod, develop/dev -> dev, staging -> staging.
    2. Tag-based (trunk-based repos): tags like stg-*, prd-*, release-* each form
       an environment; the trunk branch then becomes `dev` (every push deploys).
    """
    branches = list_branches(repo)
    trunk = next((b for b in ("main", "master") if b in branches), None)

    prefixes: Dict[str, Dict[str, int]] = {}
    for _, _, name in list_tags(repo):
        m = _TAG_ENV_RE.match(name)
        if not m:
            continue
        env = _TAG_ENV_CANON[m.group("env").lower()]
        prefix = name[: m.end()]
        prefixes.setdefault(env, {})
        prefixes[env][prefix] = prefixes[env].get(prefix, 0) + 1

    envs: List[EnvSpec] = []
    if prefixes:
        if trunk and "dev" not in prefixes:
            envs.append(EnvSpec("dev", trunk, "branch"))
        for env, counts in prefixes.items():
            if sum(counts.values()) < 2:
                continue
            prefix = max(counts, key=counts.get)
            envs.append(EnvSpec(env, prefix, "tags"))
        envs.sort(key=lambda e: _ENV_ORDER.get(e.id, 9))
        if len(envs) > 1:
            return envs
        envs = []

    for env_id, names in ENV_BRANCH_NAMES.items():
        for branch in branches:
            if branch in names:
                envs.append(EnvSpec(env_id, branch, "branch"))
                break
    if not envs:
        head = current_branch(repo) or "HEAD"
        envs.append(EnvSpec("prod", head, "branch"))
    return envs


# --- snapshot helpers ------------------------------------------------------


def daily_heads(repo: Path, branch: str) -> List[Tuple[str, str]]:
    """Last first-parent commit per UTC day on *branch*: [(iso_date, sha), ...] oldest first."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "log", "--first-parent", "--format=%H %ct", branch],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    seen: Dict[str, str] = {}
    for line in proc.stdout.splitlines():  # newest first
        sha, ts = line.split()
        day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        if day not in seen:
            seen[day] = sha
    return sorted(seen.items())


def ls_tree(repo: Path, sha: str) -> List[Tuple[str, str]]:
    """All blobs at *sha*: [(blob_sha, path), ...]."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", sha],
        capture_output=True,
    )
    out: List[Tuple[str, str]] = []
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        parts = meta.split()
        if len(parts) < 3 or parts[1] != b"blob":
            continue
        out.append((parts[2].decode(), path.decode("utf-8", "replace")))
    return out


def blob_stats(repo: Path, shas: List[str]) -> Dict[str, Tuple[int, int, int]]:
    """{blob_sha: (lines, nonblank_lines, bytes)} via one `cat-file --batch` stream."""
    stats: Dict[str, Tuple[int, int, int]] = {}
    if not shas:
        return stats
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
        rd.read(1)  # trailing newline
        if b"\0" in body[:8000]:
            stats[sha] = (0, 0, size)  # binary
            continue
        lines = body.count(b"\n") + (1 if body and not body.endswith(b"\n") else 0)
        nonblank = sum(1 for ln in body.split(b"\n") if ln.strip())
        stats[sha] = (lines, nonblank, size)
    proc.wait()
    return stats


def ahead_behind(repo: Path, sha: str, trunk: str) -> Tuple[int, int]:
    """(ahead, behind): commits *sha* has that *trunk* lacks, and vice versa."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--left-right", "--count", f"{sha}...{trunk}"],
        capture_output=True, text=True,
    )
    parts = proc.stdout.split()
    if len(parts) != 2:
        return (0, 0)
    return (int(parts[0]), int(parts[1]))


def trunk_branch(repo: Path) -> Optional[str]:
    branches = list_branches(repo)
    return next((b for b in ("main", "master") if b in branches), None)
