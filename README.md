# Neovis

**A JARVIS-style operator for your work computer — command it from Slack on your phone, or by voice at your desk. Enterprise-safe by design.**

Most "AI assistants" answer questions. Neovis *does the work*: it inspects your
screen, wrangles files, runs jobs, and watches long tasks — then reports back to
your phone when they finish. Every dangerous action is gated by human approval
and written to a tamper-evident audit log, so it's something a regulated shop
(a hedge fund, say) can actually turn on.

> Status: early. The text pipeline + security core + CLI run today. Slack and
> voice land next (see the roadmap).

## Why it's different

| | Typical chat bot | OpenClaw-style agents | **Neovis** |
|---|---|---|---|
| Actually operates the machine | ✗ | ✓ | ✓ |
| Human approval on dangerous actions | ✗ | rare | ✓ (per-tier gate) |
| Full audit trail | ✗ | ✗ | ✓ (SQLite, mirrorable to Slack) |
| Model-agnostic (Claude / GPT / your proxy) | — | varies | ✓ (one YAML line) |
| Proactively reports finished jobs to your phone | ✗ | ✗ | ✓ (roadmap: watchers) |

## Architecture

```
phone Slack ──Socket Mode──┐
                           ├─→ channels ─→ core/agent (model-agnostic loop)
desktop hotkey/voice ──────┘                 │
                                  ┌──────────┼───────────┐
                              tools/*   registry gate   audit (SQLite)
                            (risk-tiered)  approval →  every call logged
```

The **registry is the one chokepoint**: tool authors write a plain typed
function and declare a risk tier; approval, denylists, sandboxing and audit are
enforced there, so no code path reaches a dangerous action ungated.

- **`safe`** (read-only: screenshot, list files/processes) → runs immediately
- **`moderate`** (write a file, zip a folder) → runs, audited, sandbox-checked
- **`dangerous`** (shell, delete, GUI) → **requires approval** first

## Quickstart

```bash
uv sync --extra dev            # install
uv run neovis --self-test      # prove the pipeline end-to-end, no API key needed
```

Then, with a key:

```bash
cp .env.example .env           # set NEOVIS_API_KEY
uv run neovis                  # interactive REPL
uv run neovis --auto-approve   # skip approval prompts (demos only)
```

Try: `what files are in my Downloads folder?` · `take a screenshot` ·
`show me CPU and memory` · `run "git status" in ~/project`.

## Configure for your company

Two files, no code:

- **`neovis/config/models.yaml`** — set `provider` (`anthropic`/`openai`),
  `model`, and `base_url` (point it at your internal LLM proxy). Keys come from
  the environment, never the file. Switching Claude ↔ GPT is one line.
- **`neovis/config/policy.yaml`** — shell denylist, filesystem sandbox roots,
  per-tool risk overrides.

## Add a tool

Drop a module in `neovis/tools/` — that's the whole integration:

```python
from neovis.core.registry import tool

@tool(risk="safe", description="Get today's P&L for a book.")
def book_pnl(book: str) -> str:
    ...  # call your internal service
    return f"{book}: +1.2%"
```

## Roadmap

- **Stage 1 (done):** model-agnostic agent core, risk gate, audit, base tools, CLI
- **Stage 1.5:** Slack channel (Socket Mode) — DM from your phone, approval buttons
- **Stage 2:** async voice (Slack voice clip → transcribe → act → voice reply)
- **Stage 2.5:** proactive watchers (job finishes → push chart to your phone)
- **Stage 3:** desktop hotkey + realtime voice (Siri-style)

## License

MIT.
