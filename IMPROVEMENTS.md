# Slopmeter — improvement log

Status of every point raised in the September 2026 review. ✅ done · 🟡 partial · ⬜ open.

## High value

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1 | **Portfolio / fleet view** for multi-repo dashboards | ✅ | `Fleet` button (key `G`), default landing when >1 repo. Sortable table: LOC prod/test, ratio, Δ30d, Δ90d, slop, AI share (90d), last activity, 90-day sparkline; fleet KPIs on top; click row → repo. |
| 2 | **"What happened that day"** — link days to merged PRs | ✅ | `gh pr list --state merged` per repo (disable with `--no-prs`). Tooltip shows PR count; pinning a day lists PRs (title, author, ±lines, link). Tag envs list every PR merged since the previous deploy. |
| 3 | **AI / bot detection beyond author name** | ✅ | Commit bodies parsed: `Co-Authored-By: Claude/Codex/Copilot/…`, `Generated with Claude Code`, 🤖 markers. `is_bot` (machine author) vs `is_ai` (bot or AI-assisted human). AI-assisted human commits earn half the attention budget in the slop score. Surfaced as AI % of commits/lines in hero, "Who wrote it", fleet. |
| 4 | **Slop score noise on tiny repos** | ✅ | `confident` flag: needs ≥ 4 active weeks and ≥ 3 000 weighted lines; otherwise UI shows `n/a · insufficient history`. Thresholds in `scoring.py`. |

## Correctness

| # | Item | Status | Notes |
|---|------|--------|-------|
| 5 | Classification: generated code & comments | 🟡 | Generated globs (`*.pb.go`, `*_gen.go`, `mock_*.go`, `*_pb2.py`, `*.min.*`, `*.d.ts`, `*.snap`, …) now ignored. Per-repo overrides via `.slopmeter.json` (`ignore` / `test` / `prod` / `generated` globs). **Open:** language-aware comment stripping — "nonblank" still counts comment lines. |
| 6 | Author attribution on merge commits | ⬜ | First-parent numstat credits the merger, not the PR author. Correct for squash merges, wrong for merge commits. Fix would walk second parents or use PR author from #2. |
| 7 | stg > dev anomaly / env drift | ✅ | Tag snapshots carry `ahead/behind` vs trunk (`git rev-list --left-right --count`). Hero shows `prd vs dev: N LOC behind · X commits behind · Y ahead`; tooltip shows per-deploy ahead/behind. |

## Engineering

| # | Item | Status | Notes |
|---|------|--------|-------|
| 8 | Incremental cache / scheduled publish | ⬜ | Every run recomputes from git (fast: ~1 s per repo from a warm clone, ~1 min for 58 repos). A persisted per-repo JSON + GitHub Action publishing to gh-pages is the obvious next step. |
| 9 | Payload size | ✅ | Per-day `groups` / `langs` are delta-encoded (changed keys only) and expanded client-side. A 7-month, 60-directory repo: 500 KB → ~380 KB before PR data; PR titles add ~150 KB. |
| 10 | Tests | ✅ | `pytest tests/`: classification & overrides, git log parsing (bodies, renames, bot vs AI), and an end-to-end throwaway repo (exact nonblank counts, tag envs, deploy movement, slop confidence). |

## Nice to have

| # | Item | Status | Notes |
|---|------|--------|-------|
| 11 | Env / repo overlay compare (indexed growth) | ⬜ | |
| 12 | Drill into a directory → top files | ⬜ | Needs per-file sizes in payload (large); could be lazy via a second JSON. |
| 13 | Light theme; lime fails dark lightness guideline | ⬜ | Kept for fidelity to the reference design. |

## Known limitations

- Snapshot = last first-parent commit per UTC day; the newest day is partial.
- PR linkage keys on `mergedAt` date (UTC); a PR merged 23:59 shows on the next snapshot day if the merge commit landed after the day's last snapshot.
- PR `±lines` come from GitHub and include all files (docs, lockfiles), unlike the LOC series.
- `gh` rate limits: ~60 repos × 1 call is fine; `--no-prs` for offline runs.
