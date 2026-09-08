"""Slop scoring: the attention-gap algorithm.

Slop = code added faster than humans could meaningfully own, review, and verify.

Two quantities are estimated per week:

* **Attention cost** — how much human attention the code added that week
  demands. Modeled as added lines weighted by language (a line of CSS needs
  less attention than a line of Rust) and by project age (lines in a brand-new
  repo are cheaper to own than lines in a mature codebase).

* **Attention spent** — how much human attention the project actually received.
  Modeled from observable signals: human (non-bot) commits each "own" a fixed
  budget of lines, and test lines written count 1:1 as verified/owned code.

When cost exceeds spent, the excess is **slop**. The cumulative slop score is
``cumulative_slop / cumulative_cost`` (0..1). Pushing for more tests raises
"attention spent", which shrinks the gap and drives the score down — exactly
the inflection visible on the dashboard.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .gitlog import Commit, is_vendored

# --- tunable constants -----------------------------------------------------

# Lines of code a single human commit can meaningfully "own"/review.
LINES_PER_HUMAN_COMMIT = 150.0

# A human commit that carries AI evidence (Claude co-author, Codex marker ...)
# still got a human review, but the human did not write the lines. Its
# attention budget is scaled by this factor.
AI_ASSISTED_ATTENTION_FACTOR = 0.5

# Below this much weighted cost (≈ lines) or this many active weeks the slop
# score is statistically meaningless; the dashboard shows "insufficient data".
MIN_COST_FOR_SCORE = 3000.0
MIN_WEEKS_FOR_SCORE = 4

# Each test line added counts this much toward attention spent (it is code a
# human verified by writing it). This is the lever that makes the "push for
# tests" inflection show up.
TEST_ATTENTION_FACTOR = 1.0

# Language weight by file extension. Heavier = more human attention demanded.
LANGUAGE_WEIGHTS: Dict[str, float] = {
    # code
    ".py": 1.0, ".pyx": 1.0,
    ".ts": 1.0, ".tsx": 1.0, ".js": 1.0, ".jsx": 1.0, ".mjs": 1.0, ".cjs": 1.0,
    ".go": 1.0, ".rs": 1.0,
    ".c": 1.0, ".h": 1.0, ".cc": 1.0, ".cpp": 1.0, ".hpp": 1.0,
    ".java": 1.0, ".kt": 1.0, ".kts": 1.0,
    ".rb": 1.0, ".swift": 1.0, ".php": 1.0, ".cs": 1.0, ".scala": 1.0,
    ".sh": 1.0, ".bash": 1.0, ".zsh": 1.0, ".sql": 1.0,
    ".ex": 1.0, ".exs": 1.0, ".clj": 1.0, ".dart": 1.0,
    ".vue": 1.0, ".svelte": 1.0, ".sol": 1.0,
    # infra / config
    ".tf": 0.7, ".proto": 0.7, ".graphql": 0.7, ".gql": 0.7,
    ".json": 0.4, ".yaml": 0.4, ".yml": 0.4, ".toml": 0.4, ".xml": 0.4,
    ".csv": 0.3, ".ini": 0.3, ".cfg": 0.3, ".conf": 0.3, ".env": 0.2,
    # style
    ".css": 0.3, ".scss": 0.3, ".less": 0.3, ".sass": 0.3,
    # docs
    ".md": 0.3, ".rst": 0.3, ".adoc": 0.3, ".txt": 0.2,
    # generated / tooling artifacts
    ".map": 0.1, ".lock": 0.1, ".sum": 0.1, ".mod": 0.3,
    # api collections, notebooks, assets
    ".bru": 0.3, ".http": 0.3, ".ipynb": 0.3, ".svg": 0.1, ".html": 0.4,
    # bdd specs are executable tests
    ".feature": 1.0,
    # more code
    ".ps1": 1.0, ".psm1": 1.0, ".lua": 1.0, ".r": 1.0, ".jl": 1.0, ".hs": 1.0, ".erl": 1.0, ".ml": 1.0,
    ".nim": 1.0, ".zig": 1.0, ".m": 1.0, ".mm": 1.0, ".groovy": 1.0, ".gradle": 0.7, ".cmake": 0.7,
    ".mjs": 1.0, ".astro": 1.0, ".hcl": 0.7, ".bicep": 0.7, ".nix": 0.7, ".dockerfile": 1.0,
}

# Unknown extensions are ignored (assets, data, dotfiles) — only real code counts.
DEFAULT_WEIGHT = 0.3

# Extension-less files that are code.
_CODE_FILENAMES = {
    "Makefile", "GNUmakefile", "Dockerfile", "Containerfile", "Jenkinsfile", "Rakefile", "Gemfile",
    "Procfile", "Vagrantfile", "Justfile", "justfile", "Brewfile", "Fastfile", "Podfile", "BUILD", "WORKSPACE",
}

# Filenames (regardless of extension) that are generated/lock artifacts.
_LOCK_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
    "deno.lock", "Cargo.lock", "go.sum", "poetry.lock", "Pipfile.lock",
    "Podfile.lock", "composer.lock", "Gemfile.lock", "mix.lock", "requirements.lock",
}

_MINIFIED_SUFFIXES = (".min.js", ".min.css", ".min.mjs")

_TEST_DIRS = {
    "tests", "test", "__tests__", "spec", "specs", "e2e", "integration",
    "testdata", "fixtures", "mocks", "__mocks__", "bdd", "contract-tests",
}
_TEST_FILES = {"conftest.py", "pytest.ini", "jest.config.js", "vitest.config.ts"}


# Generated artifacts by filename pattern — never demand human attention.
_GENERATED_GLOBS = (
    "*.pb.go", "*.pb.gw.go", "*_pb2.py", "*_pb2_grpc.py", "*.pb.ts", "*_gen.go", "*.gen.go", "*.gen.ts",
    "*.generated.*", "zz_generated*", "*_generated.go", "*.g.dart", "*.freezed.dart", "*.designer.cs",
    "mock_*.go", "*_mock.go", "*_mocks.go", "*.min.js", "*.min.css", "*.bundle.js", "*.d.ts",
    "schema.graphql.json", "openapi.json", "swagger.json", "*.snap",
)


@dataclass
class RepoConfig:
    """Optional per-repo overrides from `.slopmeter.json` in the repo root.

    {
      "ignore":    ["docs/**", "**/*.sql"],     # never counted
      "test":      ["qa/**"],                   # force test
      "prod":      ["mocks/server/**"],         # force prod
      "generated": ["api/client/**"],           # treated as ignored
      "bots":      ["^svc-deploy$"]             # extra bot author regexes
    }
    Globs use fnmatch semantics against the repo-relative path.
    """

    ignore: List[str] = field(default_factory=list)
    test: List[str] = field(default_factory=list)
    prod: List[str] = field(default_factory=list)
    generated: List[str] = field(default_factory=list)
    bots: List[str] = field(default_factory=list)

    @classmethod
    def load(cls, repo: Path) -> "RepoConfig":
        f = repo / ".slopmeter.json"
        if not f.is_file():
            return cls()
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls(**{k: list(data.get(k, [])) for k in ("ignore", "test", "prod", "generated", "bots")})

    def matches(self, path: str, globs: List[str]) -> bool:
        return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(Path(path).name, g) for g in globs)


_ACTIVE_CONFIG = RepoConfig()


def set_repo_config(cfg: RepoConfig) -> None:
    """Install per-repo overrides for classify_file (module-level for speed)."""
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = cfg


def is_generated(path: str) -> bool:
    name = Path(path).name
    if any(fnmatch.fnmatch(name, g) for g in _GENERATED_GLOBS):
        return True
    return bool(_ACTIVE_CONFIG.generated) and _ACTIVE_CONFIG.matches(path, _ACTIVE_CONFIG.generated)


def language_weight(path: str) -> float:
    name = Path(path).name
    if name in _LOCK_FILES:
        return 0.1
    if name.endswith(_MINIFIED_SUFFIXES):
        return 0.1
    if name in _CODE_FILENAMES or name.startswith("Dockerfile."):
        return 1.0
    ext = Path(path).suffix.lower()
    if not ext or name.startswith("."):
        return 0.0  # dotfiles and extension-less config never count
    return LANGUAGE_WEIGHTS.get(ext, DEFAULT_WEIGHT)


def is_test_file(path: str) -> bool:
    parts = Path(path).parts
    dirs = set(parts[:-1])
    if dirs & _TEST_DIRS:
        return True
    name = Path(path).name
    if name in _TEST_FILES:
        return True
    stem = Path(path).stem  # "foo.test.ts" -> "foo.test"; "test_foo.py" -> "test_foo"
    if stem.startswith("test_") or stem.startswith("test-"):
        return True
    if stem.endswith("_test") or stem.endswith("-test") or stem.endswith("_spec") or stem.endswith("-spec"):
        return True
    if stem.endswith(".test") or stem.endswith(".spec"):
        return True
    return ".test." in name or ".spec." in name


def classify_file(path: str) -> str:
    """Classify a path for LOC accounting: 'test', 'prod', or 'ignored'.

    "Prod" LOC means real source code (language weight >= 0.5). Docs, config,
    styles, lockfiles and vendored/generated artifacts are ignored so the
    prod-vs-test chart reflects actual code, not JSON fixtures or Markdown.
    """
    cfg = _ACTIVE_CONFIG
    if cfg.ignore and cfg.matches(path, cfg.ignore):
        return "ignored"
    if cfg.test and cfg.matches(path, cfg.test):
        return "test"
    if cfg.prod and cfg.matches(path, cfg.prod):
        return "prod"
    if is_vendored(path) or is_generated(path):
        return "ignored"
    if language_weight(path) < 0.5:
        return "ignored"  # fixtures/docs/config never count, even under tests/
    if is_test_file(path):
        return "test"
    return "prod"


def _age_factor(project_age_weeks: int) -> float:
    """New repos demand less attention per line; ramps to 1.0 over ~6 months."""
    months = project_age_weeks / 4.345
    return min(1.0, 0.55 + 0.45 * (months / 6.0))


def _core_factor(author_commits: int) -> float:
    """Drive-by contributors' attention is less effective than core authors'."""
    return min(1.0, 0.5 + author_commits / 20.0)


@dataclass
class Week:
    start: date
    code_added: int = 0          # raw non-test, non-vendored lines added
    test_added: int = 0          # raw test lines added
    deleted: int = 0             # raw lines removed (info only)
    weighted_cost: float = 0.0   # attention cost
    attention_spent: float = 0.0  # attention spent
    slop_lines: float = 0.0      # weekly excess
    cum_slop: float = 0.0
    cum_cost: float = 0.0
    score: float = 0.0           # cumulative slop score 0..1
    human_commits: int = 0
    bot_commits: int = 0
    ai_commits: int = 0  # bot + AI-assisted human commits

    @property
    def test_ratio(self) -> float:
        total = self.code_added + self.test_added
        return self.test_added / total if total else 0.0


@dataclass
class SlopReport:
    repo_name: str
    weeks: List[Week] = field(default_factory=list)
    total_code_added: int = 0
    total_test_added: int = 0
    total_slop: float = 0.0
    overall_score: float = 0.0
    test_push_week: Optional[int] = None  # index into weeks where tests began
    total_commits: int = 0
    ai_commits: int = 0

    @property
    def confident(self) -> bool:
        """False when history is too thin for the score to mean anything."""
        active = [w for w in self.weeks if w.weighted_cost > 0]
        return len(active) >= MIN_WEEKS_FOR_SCORE and (self.weeks[-1].cum_cost if self.weeks else 0) >= MIN_COST_FOR_SCORE

    @property
    def latest_score(self) -> float:
        if not self.weeks:
            return 0.0
        # Rolling 4-week average of the cumulative score, less noisy than the
        # instantaneous value for "where are we now".
        tail = self.weeks[-4:]
        return sum(w.score for w in tail) / len(tail)


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def compute_slop(repo_name: str, commits: List[Commit]) -> SlopReport:
    if not commits:
        return SlopReport(repo_name=repo_name)

    first_date = commits[0].date.date()

    # Total commit count per author across the whole history, for core factor.
    author_commits: Dict[str, int] = {}
    for c in commits:
        author_commits[c.author] = author_commits.get(c.author, 0) + 1

    weeks: Dict[date, Week] = {}

    def _week(d: date) -> Week:
        ws = _week_start(d)
        if ws not in weeks:
            weeks[ws] = Week(start=ws)
        return weeks[ws]

    for c in commits:
        d = c.date.date()
        w = _week(d)
        age = _age_factor((d - first_date).days // 7)
        core = _core_factor(author_commits.get(c.author, 1))

        if c.is_bot:
            w.bot_commits += 1
        else:
            w.human_commits += 1
        if c.is_ai:
            w.ai_commits += 1

        for f in c.files:
            if is_vendored(f.new_path):
                continue
            if is_test_file(f.new_path):
                w.test_added += f.added
                continue
            w.code_added += f.added
            w.deleted += f.deleted
            w.weighted_cost += f.added * language_weight(f.new_path) * age

        if not c.is_bot:
            # Human attention from this commit (scaled by core factor; halved
            # when the lines were AI-written and merely reviewed).
            w.attention_spent += LINES_PER_HUMAN_COMMIT * core * (AI_ASSISTED_ATTENTION_FACTOR if c.is_ai else 1.0)

    # Flatten in chronological order and fold in cumulative state + test signal.
    ordered = [weeks[ws] for ws in sorted(weeks)]
    cum_slop = 0.0
    cum_cost = 0.0
    total_code = 0
    total_test = 0

    for w in ordered:
        # Test lines add attention: writing a test means verifying code.
        w.attention_spent += w.test_added * TEST_ATTENTION_FACTOR

        w.slop_lines = max(0.0, w.weighted_cost - w.attention_spent)
        cum_slop += w.slop_lines
        cum_cost += w.weighted_cost
        w.cum_slop = cum_slop
        w.cum_cost = cum_cost
        w.score = (cum_slop / cum_cost) if cum_cost > 0 else 0.0

        total_code += w.code_added
        total_test += w.test_added

    report = SlopReport(
        repo_name=repo_name,
        weeks=ordered,
        total_code_added=total_code,
        total_test_added=total_test,
        total_slop=cum_slop,
        overall_score=(cum_slop / cum_cost) if cum_cost > 0 else 0.0,
        total_commits=len(commits),
        ai_commits=sum(1 for c in commits if c.is_ai),
    )
    report.test_push_week = _detect_test_push(ordered)
    return report


def _detect_test_push(weeks: List[Week], window: int = 4, threshold: float = 0.15) -> Optional[int]:
    """Return the index where a sustained test push begins, else None.

    A "test push" is a week after which the rolling `window`-week test ratio
    holds at or above `threshold`. The earliest such week is reported.
    """
    if len(weeks) < window + 1:
        return None
    for i in range(window, len(weeks)):
        rolling = weeks[i - window + 1 : i + 1]
        ratio = sum(w.test_added for w in rolling) / max(
            1, sum(w.test_added + w.code_added for w in rolling)
        )
        if ratio >= threshold:
            return i
    return None


@dataclass
class LOCPoint:
    date: date
    prod: int
    test: int

    @property
    def total(self) -> int:
        return self.prod + self.test


def compute_loc_series(commits: List[Commit]) -> List[LOCPoint]:
    """Daily cumulative prod LOC and test LOC, oldest first.

    Walks commits chronologically, tracking net line changes per file class.
    One point is emitted per day that had activity, carrying the running total
    at the end of that day (clamped at zero).
    """
    if not commits:
        return []

    prod = 0
    test = 0
    points: List[LOCPoint] = []
    last_date: Optional[date] = None

    for c in commits:
        d = c.date.date()
        for f in c.files:
            kind = classify_file(f.new_path)
            delta = f.added - f.deleted
            if kind == "test":
                test = max(0, test + delta)
            elif kind == "prod":
                prod = max(0, prod + delta)

        point = LOCPoint(date=d, prod=prod, test=test)
        if d == last_date and points:
            points[-1] = point
        else:
            points.append(point)
            last_date = d

    return points


# --- exact daily snapshots --------------------------------------------------

# Top-level dirs that are containers: group one level deeper so the "where the
# lines live" panel shows services/foo instead of one giant "services" bar.
_CONTAINER_DIRS = {"services", "packages", "apps", "libs", "lib", "modules", "cmd", "internal", "src"}


def group_of(path: str, depth: int = 1) -> str:
    """Directory group used for the breakdown panel."""
    parts = Path(path).parts
    if len(parts) == 1:
        return "(root)"
    if parts[0] in _CONTAINER_DIRS and len(parts) > 2:
        depth = max(depth, 2)
    return "/".join(parts[: min(depth, len(parts) - 1)])


def language_of(path: str) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return ext or Path(path).name


@dataclass
class Snapshot:
    date: str
    sha: str
    prod: int = 0            # nonblank prod lines
    test: int = 0            # nonblank test lines
    prod_all: int = 0        # all lines incl. blank
    test_all: int = 0
    prod_files: int = 0
    test_files: int = 0
    prod_bytes: int = 0
    test_bytes: int = 0
    groups: Dict[str, List[int]] = field(default_factory=dict)  # group -> [prod, test, prod_files, test_files]
    langs: Dict[str, List[int]] = field(default_factory=dict)   # lang -> [prod, test]


def compute_snapshots(repo: Path, heads: List, group_depth: int = 1, progress=None) -> List[Snapshot]:
    """Exact per-day snapshots along *heads* ([(date, sha), ...]).

    Every unique blob is counted once via `git cat-file --batch`; trees are
    listed per day. Cost is roughly O(unique blobs + days * files).
    """
    from .gitlog import blob_stats, ls_tree

    trees: List[List] = []
    needed: Dict[str, None] = {}
    class_cache: Dict[str, str] = {}
    for i, (_, sha) in enumerate(heads):
        entries = ls_tree(repo, sha)
        kept = []
        for blob, path in entries:
            kind = class_cache.get(path)
            if kind is None:
                kind = classify_file(path)
                class_cache[path] = kind
            if kind == "ignored":
                continue
            kept.append((blob, path, kind))
            needed[blob] = None
        trees.append(kept)
        if progress and (i % 25 == 0 or i == len(heads) - 1):
            progress(f"  listing trees {i + 1}/{len(heads)}  ({len(needed):,} unique blobs)")

    if progress:
        progress(f"  counting {len(needed):,} blobs …")
    stats = blob_stats(repo, list(needed))

    group_cache: Dict[str, str] = {}
    snaps: List[Snapshot] = []
    for (date, sha), kept in zip(heads, trees):
        snap = Snapshot(date=date, sha=sha)
        for blob, path, kind in kept:
            lines, nonblank, size = stats.get(blob, (0, 0, 0))
            g = group_cache.get(path)
            if g is None:
                g = group_of(path, group_depth)
                group_cache[path] = g
            lang = language_of(path)
            gi = snap.groups.setdefault(g, [0, 0, 0, 0])
            li = snap.langs.setdefault(lang, [0, 0])
            if kind == "test":
                snap.test += nonblank; snap.test_all += lines; snap.test_files += 1; snap.test_bytes += size
                gi[1] += nonblank; gi[3] += 1; li[1] += nonblank
            else:
                snap.prod += nonblank; snap.prod_all += lines; snap.prod_files += 1; snap.prod_bytes += size
                gi[0] += nonblank; gi[2] += 1; li[0] += nonblank
        snaps.append(snap)
    return snaps


@dataclass
class DayMovement:
    date: str
    prod_add: int = 0
    prod_del: int = 0
    test_add: int = 0
    test_del: int = 0
    commits: int = 0
    bot_commits: int = 0
    ai_commits: int = 0
    ai_lines: int = 0  # lines added by bot or AI-assisted commits
    authors: Dict[str, List[int]] = field(default_factory=dict)  # author -> [prod_add, test_add, commits, is_bot, ai_commits, ai_lines]
    groups: Dict[str, List[int]] = field(default_factory=dict)   # group -> [prod_net, test_net]


def compute_movement(commits: List[Commit], group_depth: int = 1) -> List[DayMovement]:
    """Per-day additions/removals by class, plus per-author and per-group nets."""
    days: Dict[str, DayMovement] = {}
    for c in commits:
        d = c.date.date().isoformat()
        m = days.get(d)
        if m is None:
            m = days[d] = DayMovement(date=d)
        m.commits += 1
        if c.is_bot:
            m.bot_commits += 1
        ai = c.is_ai
        if ai:
            m.ai_commits += 1
        a = m.authors.setdefault(c.author, [0, 0, 0, 1 if c.is_bot else 0, 0, 0])
        a[2] += 1
        if ai:
            a[4] += 1
        for f in c.files:
            kind = classify_file(f.new_path)
            if kind == "ignored":
                continue
            g = m.groups.setdefault(group_of(f.new_path, group_depth), [0, 0])
            if ai:
                m.ai_lines += f.added; a[5] += f.added
            if kind == "test":
                m.test_add += f.added; m.test_del += f.deleted; a[1] += f.added; g[1] += f.added - f.deleted
            else:
                m.prod_add += f.added; m.prod_del += f.deleted; a[0] += f.added; g[0] += f.added - f.deleted
    return [days[k] for k in sorted(days)]


def compute_movement_between(repo: Path, heads: List, group_depth: int = 1) -> List[DayMovement]:
    """Movement per deploy: everything merged between consecutive tagged commits.

    *heads* is [(date, sha, tag), ...]; the diff A..B is attributed to B's day.
    """
    from .gitlog import load_commits

    out: List[DayMovement] = []
    for prev, cur in zip(heads, heads[1:]):
        if prev[1] == cur[1]:
            continue
        commits = load_commits(repo, branch=f"{prev[1]}..{cur[1]}", first_parent=True)
        days = compute_movement(commits, group_depth)
        m = DayMovement(date=cur[0])
        for d in days:
            m.prod_add += d.prod_add; m.prod_del += d.prod_del
            m.test_add += d.test_add; m.test_del += d.test_del
            m.commits += d.commits; m.bot_commits += d.bot_commits
            m.ai_commits += d.ai_commits; m.ai_lines += d.ai_lines
            for a, v in d.authors.items():
                acc = m.authors.setdefault(a, [0, 0, 0, v[3], 0, 0])
                acc[0] += v[0]; acc[1] += v[1]; acc[2] += v[2]; acc[4] += v[4]; acc[5] += v[5]
            for g, v in d.groups.items():
                acc = m.groups.setdefault(g, [0, 0])
                acc[0] += v[0]; acc[1] += v[1]
        out.append(m)
    return out
