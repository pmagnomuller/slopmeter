"""Unit tests for the deterministic waste scanner + audit gate."""
from slopmeter.waste import (
    check_commit_subject,
    filter_by_level,
    scan_text,
)


def test_empty_except_python():
    src = "def f():\n    try:\n        x()\n    except Exception:\n        pass\n"
    ids = {f.rule_id for f in scan_text(src, "app.py")}
    assert "DEAD001" in ids


def test_empty_except_with_body_is_quiet():
    src = "def f():\n    try:\n        x()\n    except Exception:\n        log.exception('x')\n"
    ids = {f.rule_id for f in scan_text(src, "app.py")}
    assert "DEAD001" not in ids


def test_unimplemented_and_trivial_assert():
    src = (
        "def todo():\n"
        "    raise NotImplementedError\n"
        "\n"
        "def test_x():\n"
        "    assert True\n"
    )
    ids = {f.rule_id for f in scan_text(src, "mod.py")}
    assert "DEAD003" in ids
    assert "DEAD010" in ids


def test_empty_function():
    src = "def hook():\n    pass\n"
    ids = {f.rule_id for f in scan_text(src, "hooks.py")}
    assert "DEAD005" in ids


def test_js_empty_catch():
    src = "try { doThing() } catch (e) {}\n"
    ids = {f.rule_id for f in scan_text(src, "a.js")}
    assert "DEAD001" in ids


def test_empty_markdown_section():
    src = "# Title\n\n## Setup\n\n## Usage\n\nRun it.\n"
    ids = {f.rule_id for f in scan_text(src, "README.md")}
    assert "DEAD012" in ids


def test_vibe_transitions_need_density():
    sparse = "Notably, caches help. The rest of this guide is concrete setup.\n" * 3
    assert "VIBE001" not in {f.rule_id for f in scan_text(sparse, "guide.md")}

    dense = (
        "Notably, caches help. Put differently, latency drops. "
        "The real question is ownership. At the end of the day, ship less. "
        "When it comes to reviews, slow down. Importantly, measure waste.\n"
    )
    assert "VIBE001" in {f.rule_id for f in scan_text(dense, "guide.md")}


def test_assistant_framing():
    src = "Absolutely! Great question. Let me explain the pipeline.\n"
    assert "VIBE002" in {f.rule_id for f in scan_text(src, "notes.md")}


def test_commit_placeholder():
    assert check_commit_subject("WIP")
    assert check_commit_subject("fix")
    assert not check_commit_subject("fix waste density chart on dashboard")


def test_fail_level_filter():
    findings = scan_text(
        "def f():\n    try:\n        x()\n    except Exception:\n        pass\n",
        "a.py",
    )
    assert filter_by_level(findings, "error")
    assert not filter_by_level(findings, "error") or all(
        f.severity == "error" for f in filter_by_level(findings, "error")
    )


def test_snapshot_waste_counts(tmp_path):
    import os
    import subprocess

    from slopmeter.gitlog import daily_heads
    from slopmeter.scoring import compute_snapshots

    r = tmp_path / "r"
    r.mkdir()
    subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.name", "Tester"], check=True)
    (r / "app.py").write_text(
        "def f():\n    try:\n        x()\n    except Exception:\n        pass\n"
    )
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-02-01T10:00:00Z", GIT_COMMITTER_DATE="2026-02-01T10:00:00Z")
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True, env=env)

    heads = daily_heads(r, "main")
    snaps = compute_snapshots(r, heads, waste=True)
    assert snaps[-1].waste >= 1
    assert snaps[-1].waste_err >= 1
    assert snaps[-1].waste_dead >= 1

    clean = compute_snapshots(r, heads, waste=False)
    assert clean[-1].waste == 0
