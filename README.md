# agystats

Local telemetry, P95 latency roofline, and offline Paygo / AI Credits dashboard for **Antigravity CLI**. Ported from [`agy-metrics`](https://github.com/davidgao/agy-metrics).

![Project Status - Feature Complete](https://img.shields.io/badge/Project_Status-Feature_Complete-2ea44f) 
![100% Written by AI](https://img.shields.io/badge/Written_by_AI-100%25-blue) ![Works - On My Machine](https://img.shields.io/badge/Works-On_My_Machine-2ea44f)

## Usage

```bash
# Realtime interactive TUI (default when attached to a TTY)
uv run agystats
uv run agystats tui
uv run agystats tui --all --window 24h

# Report mode (text summary)
uv run agystats report
uv run agystats report --all --window 7d
uv run agystats report --project agystats --window 24h
uv run agystats report --conversation <id-or-title> --model argon
uv run agystats report --json
uv run agystats report --no-color

# List conversations
uv run agystats list

# Run standalone script entrypoint
uv run agystats.py report
```

## TUI Keybindings

- `q` / `Ctrl+C` / `Ctrl+D`: Quit
- `Tab` / `a`: Toggle `Conversation` vs `All` mode
- `p`: Switch to `Project` mode and cycle projects
- `[` / `]`: Switch to previous / next conversation
- `u` / `Backspace`: Jump to parent conversation when inspecting a subagent
- `f`: Toggle `Follow active`
- `w`: Cycle time window (`5h`, `24h`, `7d`, `30d`, `all`)
- `m`: Cycle per-model filter & P95 latency roofline scatterplot
- `c`: Toggle cold-prefill points on roofline scatter
- `g`: Cycle quota pool (`All`, `Google`, `3rd-Party`)
- `s`: Toggle recent 5 steps vs expanded step list
- `j` / `k` (or `↓` / `↑`): Scroll steps
- `r`: Force refresh

## Tests

```bash
uv run python -m unittest discover -s tests -v
```

