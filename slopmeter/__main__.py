"""CLI entry point: `python -m slopmeter [REPO]`.

Analyzes the git history of REPO (default: current directory), computes exact
daily LOC snapshots plus the slop score, and writes a self-contained HTML
dashboard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .gitlog import EnvSpec, ahead_behind, daily_heads, detect_envs, load_commits, tag_heads, trunk_branch
from .render import render_html
from .scoring import (RepoConfig, compute_movement, compute_movement_between, compute_slop,
                      compute_snapshots, set_repo_config)


def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc


def _is_git_repo(path: Path) -> bool:
    return path.is_dir() and _git(path, "rev-parse", "--git-dir").returncode == 0


_GH_URL_RE = re.compile(r"^(?:https?://github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?/?$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def default_cache() -> Path:
    return Path(os.environ.get("SLOPMETER_CACHE") or Path.home() / ".cache" / "slopmeter" / "repos")


def _github_slug(spec: str) -> Optional[str]:
    """'owner/repo', a GitHub URL or ssh remote -> 'owner/repo'; None for local paths."""
    m = _GH_URL_RE.match(spec)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if _SLUG_RE.match(spec) and not Path(spec).exists():
        return spec
    return None


def _sync_remote(slug: str, cache: Path, refresh: bool) -> Optional[Path]:
    """Clone (no checkout, full history, all tags) or fetch a GitHub repo into the cache."""
    dest = cache / slug.replace("/", "__")
    url = f"git@github.com:{slug}.git"
    if _is_git_repo(dest):
        if refresh:
            print(f"  fetching {slug} …")
            if _git(dest, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
                _git(dest, "fetch", "--unshallow", "--tags", "--prune", "origin")
            else:
                _git(dest, "fetch", "--tags", "--prune", "origin")
            _git(dest, "remote", "set-head", "origin", "-a")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  cloning {slug} → {dest} …")
    proc = subprocess.run(
        ["git", "clone", "--no-checkout", "--quiet", url, str(dest)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        print(f"  clone failed: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'unknown error'}")
        shutil.rmtree(dest, ignore_errors=True)
        return None
    # `--no-checkout` leaves HEAD detached-ish; make local branches for main/master exist.
    _git(dest, "remote", "set-head", "origin", "-a")
    for br in ("main", "master", "develop", "dev", "staging"):
        if _git(dest, "rev-parse", "--verify", f"origin/{br}").returncode == 0:
            _git(dest, "branch", "--track", br, f"origin/{br}")
    return dest


def _org_repos(org: str) -> List[str]:
    if not shutil.which("gh"):
        raise SystemExit("error: --org needs the GitHub CLI (`gh`) on PATH and authenticated")
    proc = subprocess.run(
        ["gh", "repo", "list", org, "--limit", "500", "--json", "nameWithOwner,isArchived,isEmpty,isFork"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"error: gh repo list failed: {proc.stderr.strip()}")
    return sorted(
        r["nameWithOwner"] for r in json.loads(proc.stdout)
        if not r.get("isArchived") and not r.get("isEmpty") and not r.get("isFork")
    )


def _resolve_repos(args) -> List[Path]:
    """Turn positional specs (+ --org) into local repo paths, cloning as needed."""
    specs = list(args.repo) or ["."]
    if args.org:
        specs = [s for s in specs if s != "."] + _org_repos(args.org)
    cache = Path(args.cache).expanduser() if args.cache else default_cache()
    out: List[Path] = []
    for spec in specs:
        slug = _github_slug(spec)
        if slug:
            dest = _sync_remote(slug, cache, args.refresh)
            if dest:
                out.append(dest)
            continue
        repo = Path(spec).expanduser().resolve()
        if not repo.is_dir():
            raise SystemExit(f"error: not a directory: {repo}")
        if not _is_git_repo(repo):
            raise SystemExit(f"error: not a git repository: {repo}")
        out.append(repo)
    return out


def _remote_slug(repo: Path) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True, text=True,
        )
        url = proc.stdout.strip()
    except Exception:
        return None
    if not url:
        return None
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if ":" in url and "//" not in url:
        url = url.split(":", 1)[1]  # git@host:org/repo
    parts = url.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]


def _is_shallow(repo: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() == "true"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slopmeter",
        description="Quantify AI slop in a git repository and render a dashboard.",
    )
    p.add_argument("repo", nargs="*", default=[],
                   help="repositories: local paths, owner/repo, or GitHub URLs (remote ones are cloned into the cache). "
                        "Several repos go into one dashboard with a repo switcher. Default: cwd")
    p.add_argument("--org", default=None, help="add every non-archived, non-fork repo of a GitHub org (needs `gh`)")
    p.add_argument("--cache", default=None, help="where remote clones live (default: ~/.cache/slopmeter/repos or $SLOPMETER_CACHE)")
    p.add_argument("--refresh", action="store_true", help="git fetch cached remote clones before analyzing")
    p.add_argument("--only", action="append", default=None, help="regex filter on repo slugs (repeatable)")
    p.add_argument("--no-prs", action="store_true", help="skip fetching merged pull requests via `gh` (used for day tooltips)")
    p.add_argument("-o", "--output", default=None, help="output HTML path (default: slopmeter-<repo>.html)")
    p.add_argument("-w", "--open", action="store_true", help="open the dashboard in the browser after writing")
    p.add_argument("-b", "--branch", action="append", default=None,
                   help="environment source, repeatable: ENV=BRANCH, ENV=tag:PREFIX (e.g. stg=tag:stg-), or BRANCH. "
                        "Default: auto-detect main/develop/staging branches, or trunk + stg-*/prd-* deploy tags")
    p.add_argument("-d", "--days", type=int, default=30, help="default date range in days (default: 30)")
    p.add_argument("--group-depth", type=int, default=1,
                   help="directory depth for the 'where the lines live' breakdown (default: 1; container dirs like services/ go one deeper)")
    p.add_argument("--title", default=None, help="display name for a single repo (default: origin org/repo)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__import__('slopmeter').__version__}")
    return p


def _envs_from_args(repo: Path, specs):
    if not specs:
        return detect_envs(repo)
    out = []
    for i, b in enumerate(specs):
        if "=" in b:
            env, ref = b.split("=", 1)
        else:
            env, ref = (("prod", b) if i == 0 else (b, b))
        if ref.startswith("tag:"):
            out.append(EnvSpec(env, ref[4:], "tags"))
        else:
            out.append(EnvSpec(env, ref, "branch"))
    return out


def _merged_prs(slug: str) -> Dict[str, List[Dict]]:
    """{iso_date: [{n, t, a, u, d, ai}, ...]} of merged PRs, via `gh`. Empty on any failure."""
    if not shutil.which("gh") or "/" not in slug:
        return {}
    proc = subprocess.run(
        ["gh", "pr", "list", "--repo", slug, "--state", "merged", "--limit", "2000",
         "--json", "number,title,author,mergedAt,additions,deletions,url,labels"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {}
    out: Dict[str, List[Dict]] = {}
    for pr in json.loads(proc.stdout or "[]"):
        if not pr.get("mergedAt"):
            continue
        day = pr["mergedAt"][:10]
        author = (pr.get("author") or {}).get("login", "?")
        labels = " ".join(l.get("name", "") for l in pr.get("labels") or [])
        ai = bool(re.search(r"claude|codex|copilot|cursor|\bai\b|agent", f"{author} {labels} {pr['title']}", re.I))
        out.setdefault(day, []).append({
            "n": pr["number"], "t": pr["title"][:140], "a": author, "u": pr["url"],
            "d": (pr.get("additions") or 0) - (pr.get("deletions") or 0), "ai": ai,
        })
    return out


def _delta_encode(snaps: List[Dict], key: str) -> None:
    """Replace per-day dict *key* with only the entries that changed since the
    previous snapshot (removed entries become null). Decoded client-side."""
    prev: Dict = {}
    for s in snaps:
        cur = s[key]
        diff = {k: v for k, v in cur.items() if prev.get(k) != v}
        for k in prev:
            if k not in cur:
                diff[k] = None
        s[key] = diff
        prev = cur


def analyze_repo(repo: Path, args, name: Optional[str] = None) -> Optional[Dict]:
    slug = _remote_slug(repo) or repo.name
    name = name or slug
    print(f"\nanalyzing {repo} ({slug}) …")
    if _is_shallow(repo):
        print("  warning: shallow clone — history is truncated. Run `git fetch --unshallow` for the full story.")
    cfg = RepoConfig.load(repo)
    set_repo_config(cfg)
    if any((cfg.ignore, cfg.test, cfg.prod, cfg.generated)):
        print("  using .slopmeter.json overrides")
    trunk = trunk_branch(repo)

    env_data = []
    for spec in _envs_from_args(repo, args.branch):
        print(f"[{spec.id}] {spec.label}")
        if spec.kind == "tags":
            theads = tag_heads(repo, spec.ref)
            heads = [(d, sha) for d, sha, _ in theads]
            tags = {d: t for d, _, t in theads}
            drift = {d: ahead_behind(repo, sha, trunk) for d, sha in heads} if trunk else {}
        else:
            drift = {}
            if _git(repo, "rev-parse", "--verify", spec.ref).returncode != 0:
                print("  branch missing — skipped")
                continue
            heads = daily_heads(repo, spec.ref)
            tags = {}
        if not heads:
            print("  no snapshots — skipped")
            continue
        snaps = compute_snapshots(repo, heads, args.group_depth, progress=print)
        commits = load_commits(repo, branch=heads[-1][1], first_parent=True)
        if spec.kind == "tags":
            moves = compute_movement_between(repo, theads, args.group_depth)
        else:
            moves = compute_movement(commits, args.group_depth)
        report = compute_slop(name, commits)
        env_data.append({
            "id": spec.id,
            "branch": spec.label,
            "kind": spec.kind,
            "head": heads[-1][1],
            "commits": len(commits),
            "slop": {"score": round(report.overall_score, 4), "latest": round(report.latest_score, 4),
                     "ok": report.confident, "ai": report.ai_commits, "n": report.total_commits},
            "snaps": [
                {
                    "d": s.date, "sha": s.sha[:9], "tag": tags.get(s.date), "ab": drift.get(s.date),
                    "p": s.prod, "t": s.test, "pa": s.prod_all, "ta": s.test_all,
                    "pf": s.prod_files, "tf": s.test_files, "pb": s.prod_bytes, "tb": s.test_bytes,
                    "g": s.groups, "l": s.langs,
                }
                for s in snaps
            ],
            "moves": [
                {
                    "d": m.date, "pa": m.prod_add, "pd": m.prod_del, "ta": m.test_add, "td": m.test_del,
                    "c": m.commits, "bc": m.bot_commits, "ai": m.ai_commits, "al": m.ai_lines, "a": m.authors, "g": m.groups,
                }
                for m in moves
            ],
        })

    if not env_data:
        print("  no commits found — skipped.")
        return None

    marks = [
        {"env": e["id"], "d": s["d"], "tag": s["tag"]}
        for e in env_data if e["kind"] == "tags" for s in e["snaps"]
    ]
    for e in env_data:
        e["marks"] = [m for m in marks if m["env"] != e["id"]]
        for s in e["snaps"]:
            for k in ("tag", "ab"):
                if s[k] is None:
                    del s[k]
        _delta_encode(e["snaps"], "g")
        _delta_encode(e["snaps"], "l")

    prs = {} if args.no_prs else _merged_prs(slug)
    if prs:
        print(f"  {sum(len(v) for v in prs.values()):,} merged PRs linked")

    for e in env_data:
        s = e["snaps"][-1]
        ratio = s["t"] / s["p"] if s["p"] else 0
        print(
            f"  [{e['id']}] {e['branch']}  {len(e['snaps'])} snapshots  "
            f"prod={s['p']:,}  test={s['t']:,}  test/prod={ratio:.2f}x  slop={e['slop']['latest'] * 100:.0f}%"
        )
    return {"slug": slug, "name": name, "active": env_data[0]["id"], "envs": env_data, "prs": prs,
            "trunk": trunk, "url": f"https://github.com/{slug}" if "/" in slug else None}


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    repos = _resolve_repos(args)
    if args.only:
        pats = [re.compile(p, re.I) for p in args.only]
        repos = [r for r in repos if any(p.search(_remote_slug(r) or r.name) for p in pats)]
    if not repos:
        print("no repositories to analyze.")
        return 1

    # Local path + --org can name the same repo twice: local clone wins.
    seen, uniq = set(), []
    for r in repos:
        key = (_remote_slug(r) or r.name).lower()
        if key in seen:
            continue
        seen.add(key); uniq.append(r)
    repos = uniq

    results = []
    for repo in repos:
        try:
            r = analyze_repo(repo, args, args.title if len(repos) == 1 else None)
        except Exception as exc:  # keep going on one bad repo in a batch
            print(f"  failed: {exc}")
            r = None
        if r:
            results.append(r)
    if not results:
        print("nothing to measure.")
        return 1

    if args.output:
        output = Path(args.output)
    elif len(results) == 1:
        output = Path(f"slopmeter-{results[0]['slug'].split('/')[-1]}.html")
    else:
        owner = results[0]["slug"].split("/")[0] if "/" in results[0]["slug"] else "repos"
        output = Path(f"slopmeter-{args.org or owner}.html")

    html = render_html(
        results,
        default_days=args.days,
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    output.write_text(html, encoding="utf-8")
    print(f"\n  {len(results)} repo{'s' if len(results) != 1 else ''} → {output}  ({output.stat().st_size / 1024:.0f} KB)")
    print(f"  open with: open {output}\n")
    if args.open:
        webbrowser.open(output.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
