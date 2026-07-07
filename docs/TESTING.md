# Testing Neovis — every feature, end to end

Work top to bottom. Each card: **what to run → what to do → what you should see.**

## 0. Prerequisites (once)

```bash
cd ~/Desktop/Neovis
uv sync --extra dev --extra slack --extra voice --extra desktop --extra app
```

- **Model auth:** uses your logged-in Claude Code (Pro/Max subscription → no extra API spend). Nothing to set.
- **Voice models** (for the voice tests) into `~/.neovis/models/`:
  - Kokoro TTS: `kokoro-en-v0_19` · ASR: `sherpa-onnx-zipformer-en-2023-04-01` (has `bpe.model` for hotwords) · VAD: `ten-vad.onnx` (falls back to `silero_vad.onnx`).
- **macOS permissions** (for push-to-talk / hands-free): System Settings → Privacy & Security → **Accessibility** (your terminal) and **Microphone**.

---

## The app — start here (the one thing you run)

```bash
uv run neovis-app        # or: ./scripts/make_app.sh && open Neovis.app
```

Click **Start**. One process brings up **both** channels: the Slack bot (phone) and
the push-to-talk voice loop (desk). The dots tell the story — amber breathing =
warming up, green glow = live, red = failed with the reason next to it (e.g.
`Voice · no microphone found`); one channel failing never blocks the other.
Settings (hotkey, voice, hands-free, tokens) live in the window and lock while
running — **Stop** to change them. Untick **Enable voice** to run Slack-only.

Everything below can be exercised through the app; the per-feature terminal
commands are for isolating one piece at a time.

---

## 1. Unit tests — the safety logic, no model needed

```bash
uv run pytest -q
```
**Expect:** `59 passed`. Covers the consequence gate (denylist beats auto-approve, sandbox, auto-mode, fail-safe browser clicks, Send→OUTWARD), the intent router tiers, voice commands, and watchers.

---

## 2. Text REPL — the agent + gate

```bash
uv run neovis
```
- `what files are in my Downloads folder?` → lists them (READ, runs free).
- `create a file demo.txt with the text hello` → prompts **`[y/a/N]`** (LOCAL_WRITE). Say `N` → nothing written; `y` → written.
- `run "git status"` → READ, runs; `run "rm -rf /"` → **DENIED by denylist** (can't be approved).

**Expect:** reads run silently; writes/outward actions pause for your approval; every step in `neovis_audit.db`.

---

## 3. Browser control + real Gmail (the hero flow)

```bash
uv run neovis --browser
```
First run opens the dedicated **Neovis Chrome** (`~/.neovis/chrome-profile`) — **log into Gmail once** in that window. Then:
- `open gmail and start a compose to <someone>, subject Test, body hi` → watch the live trace (`· navigate_page`, `· take_snapshot`, `· fill_form`) and the Chrome window.
- When it reaches **Send**, it pauses: `[y/a/N]`. **This is the key safety moment** — the Send is classified OUTWARD and held until you approve.

**Expect:** you see each browser step in the terminal + the Chrome window; the Send never fires without your yes.

---

## 4. Desktop voice — speak to Neovis

```bash
python -m neovis.channels.desktop.voice            # push-to-talk (hold Right-Cmd)
python -m neovis.channels.desktop.voice --hands-free   # no key; barge-in (use headphones)
python -m neovis.channels.desktop.voice --type     # no mic — type to test the logic
```
Try saying (or typing):
- `take a screenshot` → agent acts, then **speaks** the result (Kokoro).
- **Voice easter egg:** `make it sound posh` → switches to a British voice and confirms *in that voice*; `talk like a guy` → male; `switch to emma`; `change your voice` → it **asks** which one. (Handled by the Haiku router — natural phrasing works, not just exact keywords.)
- **Barge-in (`--hands-free`):** start talking while Neovis is speaking → it stops immediately.
- **Hotwords:** say a name/ticker (e.g. a colleague's name) — biased by `~/.neovis/settings.yaml`.

**Expect:** ASR ~instant; voice switches on natural language; hands-free segments your speech automatically; barge-in interrupts playback.

**Change the hotkey:** edit `~/.neovis/settings.yaml` (`hotkey: alt_r` / `f5` / …) or `--key`.

---

## 5. Slack — command from your phone (server is already running)

DM the **neovis** bot in Slack:
- `what files are in my Downloads` → 👀 reaction, a live "🔧 working…" message showing each step (the thinking), then the answer in Slack formatting, then ✅.
- `create a file test.txt with hello` → an **Approve / Deny** card appears in the DM (approve from your phone).
- **For the 👀/✅ reactions:** add the `reactions:write` scope (OAuth & Permissions → Bot Token Scopes) and reinstall the app. Without it, everything else still works.

**Expect:** phone → agent → phone reply, with consequential actions gated by buttons on your phone.

The app's **Start** runs this for you (tokens from the window). Standalone, for
isolation only — never at the same time as the app (two Socket Mode connections
split events):
```bash
SLACK_BOT_TOKEN=xoxb-… SLACK_APP_TOKEN=xapp-… uv run python -m neovis.channels.slack.app
```

---

## 5.5 Memory, recall, and steer (the learning loop)

- **Memory:** tell it a durable fact in passing — `btw our CTO is Alice Zhang,
  alice.zhang@fund.com` — no need to say "remember". Later (even after a
  restart, any channel): `email the CTO a hello` → it knows who and where.
  Files are human-readable: `~/.neovis/memory/MEMORY.md` / `USER.md`.
- **Recall:** `what did I ask you yesterday?` / `what was that file we made
  last time?` → it searches `~/.neovis/transcripts.db` (FTS5, every channel's
  turns) and answers from your own history.
- **Steer (Slack):** while a task is visibly working, just send another
  message — `actually, put them in ~/archive instead`. You'll see
  `🔀 Redirecting the current task…` and it pivots with full context.
  (`stop` still fully stops.)

---

## 6. Proactive watcher — "kick off a job, ping me when it's done"

In Slack **or** desktop voice:
- `run "sleep 15 && echo REPORT READY" in the background and ping me when it's done`

**Expect:** Neovis registers the watch and replies immediately ("I'll notify you…"); ~15s later a **🔔 Watch finished** push arrives (Slack DM) or is **spoken aloud** (desktop). Also works with `kind=process` (a PID exits) and `kind=file` (a path appears).

---

## 7. Non-Claude model (optional) via a gateway

```bash
NVIDIA_API_KEY=… uv run litellm --config dev/litellm.config.yaml --port 4000
uv run neovis --gateway-url http://localhost:4000
```
**Expect:** the same agent, driven by a non-Claude model through the LiteLLM gateway.
