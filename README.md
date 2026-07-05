# Neovis

**A JARVIS-style operator for your work computer — command it from Slack on your phone or by voice at your desk. It *operates* your machine, and every consequential action is gated by human approval and written to an audit log.**

Most "AI assistants" answer questions. Neovis *does the work*: it drives your
browser and apps, wrangles files, runs jobs — and pauses for your OK before
anything that reaches outward (sending an email, submitting a form, deleting,
pushing). That approval-and-audit spine is what makes it something a regulated
shop — a hedge fund, say — can actually turn on.

Built on the **Claude Agent SDK**, so it inherits a battle-tested agent loop,
tool ecosystem, and MCP support — Neovis adds the safety layer, the channels
(Slack / desktop), browser control, and local voice on top.

> Status: the agent core, consequence gate, audit, browser control (Gmail send
> demo), the `neovis` REPL, and local voice (Kokoro TTS + hotword ASR) all run
> and are tested today. The phone/Slack channel lands next.

## Why it's different

| | Typical chat bot | OpenClaw-style agents | **Neovis** |
|---|---|---|---|
| Actually operates the machine & browser | ✗ | ✓ | ✓ |
| Human approval on consequential actions | ✗ | rare | ✓ (per-consequence gate) |
| Fail-safe (uncertain → ask, never silently act) | ✗ | ✗ | ✓ |
| Full audit trail | ✗ | ✗ | ✓ (SQLite) |
| Model-agnostic (Claude / GPT / your proxy) | — | varies | ✓ (via gateway) |
| Local voice, English, CPU (only the brain is remote) | ✗ | ✗ | ✓ (Kokoro + sherpa-onnx) |

## The safety model: consequence-gated

Neovis classifies **every** tool call by *consequence*, not by tool, and
enforces it in one place — a Claude Agent SDK **PreToolUse hook** ([`core/gate.py`](neovis/core/gate.py))
that fires for every action regardless of mode:

- **`READ`** (screenshot, list/read files, navigate/snapshot a page) → runs
  freely, after an optional allowlist check.
- **`LOCAL_WRITE`** (write a file, run a script, type into a field) → prompts for
  approval; "approve + auto" enters an auto-mode for a batch of *file* writes.
- **`OUTWARD`** (send, submit, delete, `git push`, buy) → **always** pauses for a
  human, even in auto-mode; severe ones double-confirm.

Two invariants make it trustworthy:

- **Fail-safe, not fail-open.** Browser interactions are *never* auto-approved —
  a click can submit or send and we can't tell from an opaque element id, so it
  always reaches a human. When in doubt, Neovis asks.
- **Hard denials can't be approved around.** A shell-command denylist (`rm -rf /`,
  `mkfs`, `shutdown`, …) is refused before approval is even offered.

Every decision — tool, arguments, consequence tier, approver, outcome — is
written to a SQLite **audit log** ([`core/audit.py`](neovis/core/audit.py)),
mirrorable to a `#jarvis-audit` channel.

## Architecture

```
phone Slack ─┐
             ├─→ NeovisSession (Claude Agent SDK engine)
desktop/voice┘        │  model → subscription Claude / company proxy / gateway
                      │
        ClaudeAgentOptions:
          • PreToolUse hook  → gate.py  (READ / LOCAL_WRITE / OUTWARD)
          •                     ├─ allowlist · denylist · auto-mode
          •                     ├─ approval gateway (Slack buttons / console)
          •                     └─ audit (SQLite)
          • PostToolUse hook → capture page snapshots (uid → label; Send detection)
          • mcp_servers      → chrome-devtools MCP (drive a real Chrome)
          • built-in tools   → Read / Grep / Glob / Write / Edit / Bash
```

## Quickstart

```bash
uv sync --extra dev            # core install
uv run pytest -q               # 39 tests, all green
```

Neovis uses your machine's **Claude Code auth** by default (a Pro/Max
subscription counts against your plan — no extra API spend). Then:

```bash
uv run neovis                  # terminal REPL; reads run free, writes prompt [y/a/N]
uv run neovis --browser        # + drive a dedicated Chrome (see Browser control)
uv run neovis --auto-approve   # skip prompts (demos only)
```

Try: `what's in my Downloads folder?` · `take a screenshot` · `show CPU & memory`.

## Configure the model

The agent core is model-agnostic. Point [`neovis/config/models.yaml`](neovis/config/models.yaml)
at your endpoint; the gate/audit/channels don't change.

- **Subscription Claude (default):** nothing to set — uses your logged-in Claude Code.
- **Company Claude proxy:** `uv run neovis --gateway-url https://llm.yourfund.internal --gateway-key $KEY`.
- **Non-Claude (GPT/GLM/…):** run a LiteLLM/vLLM gateway that exposes the
  Anthropic Messages API (`/v1/messages`) in front of your model, and point
  `--gateway-url` at it. A ready example lives in [`dev/litellm.config.yaml`](dev/litellm.config.yaml)
  (translates Anthropic → an OpenAI-compatible backend).

## Browser control

`uv run neovis --browser` auto-launches a **dedicated Neovis Chrome profile**
(`~/.neovis/chrome-profile`) with remote debugging and connects to it. On first
run, log into Gmail (and anything else Neovis needs) in that window — it
persists. This is both required (Chrome 136+ blocks debugging on your *default*
logged-in profile) and safer (Neovis only sees what you log into there).

You'll see a **live step trace** in the terminal (`· navigate_page …`,
`· take_snapshot`, `· click …`) and can watch the Chrome window. Consequential
clicks pause for your approval — e.g. driving Gmail, the **Send** click is
classified `OUTWARD` and held until you confirm.

## Voice (desktop, local, English)

All on-device (CPU) — only the LLM brain is remote. Download the models once:

```bash
mkdir -p ~/.neovis/models && cd ~/.neovis/models
curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-en-v0_19.tar.bz2 && tar xjf kokoro-en-v0_19.tar.bz2
# ASR model with bpe.model (hotwords): the sherpa-onnx zipformer-en-2023-04-01 files
```

- **TTS:** [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) via sherpa-onnx
  ([`voice/tts.py`](neovis/voice/tts.py)) — Apache-2.0, faster than realtime on CPU.
- **ASR:** a sherpa-onnx transducer ([`voice/asr.py`](neovis/voice/asr.py)) with
  **hotwords** (contextual biasing), so tickers and colleagues' names transcribe
  correctly. Swap in Parakeet-TDT for higher accuracy.

```python
from neovis.voice.tts import KokoroTTS
from neovis.voice.asr import TransducerASR
KokoroTTS().synthesize("The email has been sent.", "out.wav")
TransducerASR(hotwords=["NVDA", "Tsurunaki"]).transcribe("clip.wav")
```

## Add a tool

Drop a module under [`neovis/tools/`](neovis/tools/) — the gate classifies and
audits it automatically:

```python
from neovis.core.registry import tool

@tool(risk="safe", description="Get today's P&L for a book.")
def book_pnl(book: str) -> str:
    ...  # call your internal service
```

## Roadmap

- **Done:** Agent SDK core · consequence gate + audit · browser control (Gmail
  send, live-verified) · dedicated Chrome profile · `neovis` REPL · local voice.
- **Next:** Slack channel (Socket Mode + approval buttons on your phone) —
  scaffolded, needs your workspace tokens.
- **Then:** proactive watchers (job finishes → push to phone) · realtime voice
  loop (hotkey push-to-talk) · Windows/Linux verification.

## License

MIT.
