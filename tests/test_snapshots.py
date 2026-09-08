"""End-to-end on a throwaway git repo: exact counts, envs, movement, tags."""
import subprocess
from pathlib import Path

import pytest

from slopmeter.gitlog import daily_heads, detect_envs, load_commits, tag_heads
from slopmeter.scoring import compute_movement, compute_movement_between, compute_slop, compute_snapshots


def git(repo, *a, env=None):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t.t")
    git(r, "config", "user.name", "Tester")

    def commit(files, msg, date):
        for p, content in files.items():
            f = r / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        git(r, "add", "-A")
        import os
        env = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        git(r, "commit", "-q", "-m", msg, env=env)

    commit({"app/main.go": "package main\n\nfunc main() {}\n", "README.md": "# hi\n"}, "init", "2026-01-01T10:00:00Z")
    commit({"app/main_test.go": "package main\n\nfunc TestX(t *testing.T) {}\n"}, "tests", "2026-01-02T10:00:00Z")
    git(r, "tag", "stg-v0.1.0")
    commit({"app/util.go": "package main\n\n\nfunc u() {}\n"}, "util", "2026-01-04T10:00:00Z")
    git(r, "tag", "stg-v0.1.1")
    git(r, "tag", "prd-v0.1.0")
    git(r, "tag", "prd-v0.0.1", "HEAD~2")  # env needs >= 2 tags to count
    return r


def test_daily_snapshots_exact_nonblank(repo):
    heads = daily_heads(repo, "main")
    assert [d for d, _ in heads] == ["2026-01-01", "2026-01-02", "2026-01-04"]
    snaps = compute_snapshots(repo, heads)
    assert (snaps[0].prod, snaps[0].test) == (2, 0)        # README ignored, blank line dropped
    assert (snaps[1].prod, snaps[1].test) == (2, 2)
    assert (snaps[2].prod, snaps[2].test) == (4, 2)
    assert snaps[2].prod_all == 7                            # blanks counted in "all lines"
    assert snaps[2].groups["app"] == [4, 2, 2, 1]
    assert snaps[2].langs["go"] == [4, 2]


def test_tag_envs_detected_and_deploy_movement(repo):
    envs = {e.id: e for e in detect_envs(repo)}
    assert envs["dev"].ref == "main" and envs["stg"].kind == "tags" and envs["prd"].ref == "prd-"
    th = tag_heads(repo, "stg-")
    assert [t for _, _, t in th] == ["stg-v0.1.0", "stg-v0.1.1"]
    moves = compute_movement_between(repo, th)
    assert len(moves) == 1 and moves[0].prod_add == 4 and moves[0].commits == 1


def test_movement_and_slop(repo):
    commits = load_commits(repo, "main", first_parent=True)
    mv = compute_movement(commits)
    assert [m.date for m in mv] == ["2026-01-01", "2026-01-02", "2026-01-04"]
    assert mv[1].test_add == 3 and mv[1].authors["Tester"][2] == 1
    report = compute_slop("r", commits)
    assert 0.0 <= report.overall_score <= 1.0
    assert report.confident is False  # far too little history
