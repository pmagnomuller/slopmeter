"""Unit tests for the deterministic waste scanner + audit gate."""
from slopmeter.waste import (
    RULES,
    check_commit_subject,
    filter_by_level,
    list_rules,
    scan_text,
)


def test_rule_catalog_covers_slopcop_ids():
    ids = set(RULES)
    for n in range(1, 19):
        assert f"DEAD{n:03d}" in ids
    assert "TRAIL001" in ids and "TRAIL002" in ids
    for n in range(1, 27):
        assert f"VIBE{n:03d}" in ids
    assert len(list_rules()) == len(RULES)


def test_empty_except_python():
    src = "def f():\n    try:\n        x()\n    except Exception:\n        pass\n"
    assert "DEAD001" in {f.rule_id for f in scan_text(src, "app.py")}


def test_empty_except_with_body_is_quiet():
    src = "def f():\n    try:\n        x()\n    except Exception:\n        log.exception('x')\n"
    assert "DEAD001" not in {f.rule_id for f in scan_text(src, "app.py")}


def test_todo_marker():
    src = "def f():\n    # TODO: finish this\n    return 1\n"
    assert "DEAD002" in {f.rule_id for f in scan_text(src, "app.py")}


def test_todo_in_tests_is_quiet():
    src = "def test_x():\n    # TODO: finish this\n    assert 1\n"
    assert "DEAD002" not in {f.rule_id for f in scan_text(src, "tests/test_app.py")}


def test_unimplemented_todo_throw():
    src = "def todo():\n    raise NotImplementedError('TODO: wire this')\n"
    assert "DEAD003" in {f.rule_id for f in scan_text(src, "mod.py")}


def test_bare_not_implemented_is_quiet():
    src = "def abstractish():\n    raise NotImplementedError\n"
    assert "DEAD003" not in {f.rule_id for f in scan_text(src, "mod.py")}


def test_except_return_none():
    src = "def f():\n    try:\n        x()\n    except Exception:\n        return None\n"
    assert "DEAD004" in {f.rule_id for f in scan_text(src, "app.py")}


def test_empty_function():
    src = "def hook():\n    pass\n"
    assert "DEAD005" in {f.rule_id for f in scan_text(src, "hooks.py")}


def test_bool_branch():
    src = "def f(x):\n    if x:\n        return True\n    else:\n        return False\n"
    assert "DEAD009" in {f.rule_id for f in scan_text(src, "app.py")}


def test_trivial_assert():
    src = "def test_x():\n    assert True\n"
    assert "DEAD010" in {f.rule_id for f in scan_text(src, "mod.py")}


def test_js_empty_catch_and_jsx():
    src = "try { doThing() } catch (e) {}\nconst C = () => <button onClick={() => {}} />;\n"
    ids = {f.rule_id for f in scan_text(src, "a.tsx")}
    assert "DEAD001" in ids
    assert "DEAD016" in ids


def test_action_ok_stub():
    src = "async function handleSubmit() { return { ok: true } }\n"
    assert "DEAD017" in {f.rule_id for f in scan_text(src, "a.ts")}


def test_step_banners():
    src = "# Step 1: load\nx()\n# Step 2: save\ny()\n"
    assert "DEAD018" in {f.rule_id for f in scan_text(src, "app.py")}


def test_empty_markdown_section():
    src = "# Title\n\n## Setup\n\n## Usage\n\nRun it.\n"
    assert "DEAD012" in {f.rule_id for f in scan_text(src, "README.md")}


def test_vibe_transitions_need_density():
    sparse = "Notably, caches help. The rest of this guide is concrete setup.\n" * 3
    assert "VIBE001" not in {f.rule_id for f in scan_text(sparse, "guide.md")}
    dense = (
        "Notably, caches help. Put differently, latency drops. "
        "The real question is ownership. At the end of the day, ship less. "
        "When it comes to reviews, slow down. Importantly, measure waste.\n"
    )
    assert "VIBE001" in {f.rule_id for f in scan_text(dense, "guide.md")}


def test_assistant_and_chatbot():
    src = "Absolutely! Great question. Let me explain the pipeline.\n"
    assert "VIBE002" in {f.rule_id for f in scan_text(src, "notes.md")}
    bot = "As an AI language model, I don't have personal experiences.\n"
    assert "VIBE019" in {f.rule_id for f in scan_text(bot, "notes.md")}


def test_commit_placeholder_and_autosquash():
    assert check_commit_subject("WIP")
    assert check_commit_subject("fix")
    assert not check_commit_subject("fix waste density chart on dashboard")
    fix = check_commit_subject("fixup! typo")
    assert any(f.rule_id == "TRAIL002" for f in fix)
    allowed = check_commit_subject("fixup! typo", allow_autosquash=True)
    assert not any(f.rule_id == "TRAIL002" for f in allowed)


def test_fail_level_filter():
    findings = scan_text(
        "def f():\n    try:\n        x()\n    except Exception:\n        pass\n",
        "a.py",
    )
    hard = filter_by_level(findings, "error")
    assert hard and all(f.severity == "error" for f in hard)


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
