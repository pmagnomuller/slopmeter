# Waste audit (slopcop-style gate)

Offline, deterministic rules aligned with the [slopcop](https://github.com/JGalego/slopcop)
catalog (`DEAD001`–`DEAD018`, `TRAIL001`–`TRAIL002`, `VIBE001`–`VIBE026`).
No models, no network at scan time.

## Try it locally

```bash
cd /path/to/slopmeter
pip install -e .          # or: uv pip install -e .
slopmeter rules           # list every rule ID
slopmeter audit .         # whole tree
slopmeter audit --diff    # unstaged + untracked vs HEAD
slopmeter audit --staged
slopmeter audit --base origin/main --history --format github
slopmeter audit --fail-level error   # only fail on errors
```

Exit codes: `0` clean (or below fail-level), `1` findings at/above fail-level, `2` usage error.

## GitHub Action

In any repo:

```yaml
name: waste
on: [pull_request]
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: pmagnomuller/slopmeter@main   # pin a tag/SHA in production
        with:
          base: ${{ github.base_ref }}
          fail-level: warning
          history: true
```

### Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `base` | _(empty)_ | Diff `BASE...HEAD`. Empty scans `paths` (default `.`) |
| `fail-level` | `warning` | `info` / `warning` / `error` |
| `history` | `true` | Also TRAIL-check commit subjects in the range |
| `paths` | `.` | Used when `base` is empty |
| `version` | Action SHA | Override pip install ref if not using the Action checkout |
| `python-version` | `3.12` | setup-python version |

PR annotations use `--format github`.

## Company rollout tips

1. Start non-blocking or with `fail-level: error` (deadweight hard fails; vibe stays warning).
2. Pin the Action to a tag/SHA; or vendor `waste.py` if supply-chain policy requires it.
3. Pair with the observatory: `slopmeter --org …` for trends, Action for new waste on the diff.
