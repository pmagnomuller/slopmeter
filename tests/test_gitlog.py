from slopmeter.gitlog import Commit, parse_commits, parse_numstat_line
from datetime import datetime, timezone

RAW = (
    "__SM_COMMIT__\x00abc\x00Alice\x00a@x.de\x001700000100\x00feat: x\x00"
    "body\n\n🤖 Generated with [Claude Code]\nCo-Authored-By: Claude <noreply@anthropic.com>\x00__SM_END__\n"
    "3\t1\ta.go\n-\t-\tbin.png\n2\t0\told.go => new.go\n"
    "__SM_COMMIT__\x00def\x00dependabot[bot]\x00d@x\x001700000000\x00bump\x00\x00__SM_END__\n"
    "1\t0\tgo.sum\n"
)


def test_parse_numstat():
    fc = parse_numstat_line("3\t1\tpath/a.go")
    assert (fc.added, fc.deleted, fc.path) == (3, 1, "path/a.go")
    assert parse_numstat_line("-\t-\tbin").added == 0
    assert parse_numstat_line("garbage") is None


def test_parse_commits_chronological_with_body_and_renames():
    cs = parse_commits(RAW)
    assert [c.sha for c in cs] == ["def", "abc"]
    ai = cs[1]
    assert ai.date == datetime.fromtimestamp(1700000100, tz=timezone.utc)
    assert len(ai.files) == 3
    assert ai.files[2].is_rename and ai.files[2].new_path == "new.go"
    assert "Co-Authored-By" in ai.body


def test_bot_vs_ai_assisted():
    cs = parse_commits(RAW)
    bot, human_ai = cs[0], cs[1]
    assert bot.is_bot and bot.is_ai
    assert not human_ai.is_bot and human_ai.is_ai
    plain = Commit("x", "Alice", "alice@users.noreply.github.com", datetime.now(timezone.utc), "s")
    assert not plain.is_bot and not plain.is_ai  # noreply must not flag humans
