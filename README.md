<div align="center">

![PRANA](assets/prana-banner.svg)

[![Typing SVG](https://readme-typing-svg.demolab.com?font=Fira+Code&size=20&pause=1000&color=00FF41&center=true&vCenter=true&width=600&lines=Give+any+chatbot+a+life;ReAct+loop+%2B+tools+%2B+memory;Zero+dependencies.+Pure+Python)](https://github.com/Thegreatprabh/Prana)

[![Tests](https://img.shields.io/badge/tests-291_passing-00ff41.svg)](#-verify-it-yourself)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-00e5ff.svg)](pyproject.toml)
[![Lines](https://img.shields.io/badge/code-%7E10k_lines-b366ff.svg)](#-architecture)

**🫁 PRANA** — an open-source **brain runtime** that turns any plain chatbot
into an autonomous AI agent.

ReAct loop · real tools · persistent memory · proactive daemon · personality ·
your own API key. **Stdlib-only Python. Zero dependencies.**

</div>

## 📦 Installation

```bash
git clone https://github.com/Thegreatprabh/Prana
cd Prana
pip install -e .          # stdlib only — nothing else to install
prana-setup               # instant installer: API key + AI agents + channels
prana --save-key          # sk-ant-… → Anthropic · AIza… → Gemini · sk-… → OpenAI
prana --chat              # talk to your agent
```

Bas. 4 commands me live agent. Details ke liye [install PDFs](#-install-guides--download-the-pdfs) dekho.

## 🫁 What is PRANA?

A chatbot is stateless: prompt in, text out.
An **agent** is a *runtime* — it generates its own prompts (schedules, hooks,
workers), uses **tools**, keeps **memory**, and has **initiative**.

**PRANA is that runtime.** Give it any LLM API key and it becomes an
autonomous agent that lives on your machine, remembers you, pings you on
Telegram/WhatsApp/Discord, and does work while you're away.

Architecture: [`SPEC.md`](SPEC.md) · Practical guide: [`docs/USAGE.md`](docs/USAGE.md)

## ✅ What's inside

**7 commands** — `prana` (chat CLI) · `prana-setup` (instant installer) ·
`prana-mcp` (MCP server) · `prana-telegram` · `prana-whatsapp` ·
`prana-discord` · `prana-daemon` (proactive background runner)

**4 channels, two-way bots** — Telegram (+ voice notes: Whisper STT + TTS) ·
WhatsApp (Meta Cloud API + webhook) · Discord (custom-built gateway, no
discord.py needed)

**MCP server (5 tools)** — `prana_chat` · `prana_remember` · `prana_recall` ·
`prana_schedule` · `prana_jobs` → plugs into Claude Code, Claude Desktop,
Cursor, ChatGPT Desktop, Gemini CLI via `prana-setup --agents`

**Memory** — `MEMORY.md` + per-person notes + daily log + TF-IDF recall
(credential-like writes **refused**) · **4 personality modes**
(casual/teacher/engineer/strategist) · **BYOK** (OpenAI/Anthropic/Gemini,
auto-detected from key)

**Proactivity** — cron jobs · file-watch hooks · background worker pool ·
delivery rules (`[NOTIFY]`/`[URGENT]` markers, change-detection, silent on
no-change)

**Safety** — channel pairing (first sender = owner, strangers ignored) ·
`schedule` always asks approval (no auto-approve flag) · SSRF blocklist ·
webhook HMAC verification · keys in `0600` config, never logged

**Quality** — 291 tests passing · 8/8 scripted demo · zero dependencies ·
~10k lines of readable Python · MIT license

### 🎬 Demo — how it works

```console
$ prana "remind me every morning at 9 to check the markets"

🤔 I'll create a scheduled job for that.
🔧 schedule(name="morning-markets", schedule="0 9 * * *", ...)
⚠️  IRREVERSIBLE tool 'schedule' — Approve? [y/N] y
✅ Job created. The daemon will run it every morning at 9.

$ prana-daemon &
# ... next morning, 9:00 AM ...
📩 Telegram: "Morning! Nifty is at 25,410 (+0.4%). Your watchlist: ..."
```

![divider](assets/divider.svg)

## 🏗️ Architecture

```
                    ┌─────────────────────────────┐
                    │        🫁 PRANA BRAIN        │
                    │   ReAct loop (25 iters,     │
                    │   wall-clock cap, interrupt) │
                    └──────────────┬──────────────┘
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
     ┌────────────────┐  ┌─────────────────┐  ┌──────────────────┐
     │     TOOLS      │  │     MEMORY      │  │   PROACTIVITY    │
     │ exec · read    │  │ MEMORY.md       │  │ cron jobs        │
     │ write · edit   │  │ people notes    │  │ file hooks       │
     │ web · remember │  │ daily log       │  │ worker pool      │
     │ recall · sched │  │ TF-IDF recall   │  │ daemon + delivery│
     └────────────────┘  └─────────────────┘  └──────────────────┘
              └────────────────────┼────────────────────┘
                                   ▼
              ┌────────────────────────────────────────┐
              │               CHANNELS                 │
              │ Telegram (+voice) · WhatsApp · Discord │
              │              MCP server                │
              └────────────────────────────────────────┘
```

~10k lines of Python. No frameworks. No dependencies. A human can read the
whole thing end-to-end — that's the point.

## ⚡ The six systems

| # | System | What it does |
|---|--------|--------------|
| 1 | **Core loop** | ReAct: reason → tool call → observe → repeat. 25-iteration budget, wall-clock cap, graceful interrupt, history compaction. |
| 2 | **Tools** | `exec` (sandboxed shell), `read`/`write`/`edit`/`list` (sandboxed files), `web_search`/`fetch_url` (SSRF-guarded), `remember`, `recall`, `schedule`. |
| 3 | **Memory** | Long-term `MEMORY.md`, per-person notes, daily log, TF-IDF recall injected into every turn. Credential-like writes are **refused**, never stored. |
| 4 | **Proactivity** | Cron jobs, file-watch hooks, background workers. The daemon wakes on its own, works tasks, and pings you **only** when something is new. |
| 5 | **Personality** | SOUL.md-style identity file + 4 modes: `casual` / `teacher` / `engineer` / `strategist`. System prompt assembled fresh each turn. |
| 6 | **Channels** | Telegram (+ voice notes), WhatsApp, Discord — two-way bots. **Pairing**: the first message makes you the owner; strangers are silently ignored. |

## 💬 Channels — your agent, in your pocket

| Channel | Command | Notes |
|---|---|---|
| 📩 Telegram | `prana-telegram` | Needs `TELEGRAM_BOT_TOKEN` from [@BotFather](https://t.me/BotFather) |
| 🟢 WhatsApp | `prana-whatsapp` | Meta Cloud API · `WHATSAPP_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` |
| 🟣 Discord | `prana-discord` | Bot token · `DISCORD_BOT_TOKEN` · Message Content intent on |
| 🎙️ Voice notes | built into `prana-telegram` | Whisper STT + TTS on your OpenAI key — talk to your agent |

**Pairing-first safety:** whoever messages the bot *first* becomes the owner.
Everyone else is ignored — no reply, nothing to probe. A guessed bot username
gets an attacker exactly nowhere.

## 📕 Install guides — download the PDFs

Step-by-step setup, one PDF per app. Each guide marks what `prana-setup`
does automatically (🤖) vs your 2-minute manual steps (👆). Tap any link to
read it right in your browser, or download it.

| Guide | Covers |
|---|---|
| [📩 Telegram Setup](PRANA-Telegram-Setup.pdf) | @BotFather bot, `prana-telegram`, pairing, voice notes |
| [🟢 WhatsApp Setup](PRANA-WhatsApp-Setup.pdf) | Meta Cloud API, webhook, `prana-whatsapp` |
| [🟣 Discord Setup](PRANA-Discord-Setup.pdf) | Bot portal, intents (37377), `prana-discord` |
| [🎙️ Voice Notes Setup](PRANA-Voice-Setup.pdf) | Telegram voice notes (Whisper STT + TTS) |
| [🔌 AI Agents MCP Setup](PRANA-AI-Agents-MCP-Setup.pdf) | MCP server → Claude Code/Desktop, Cursor, ChatGPT Desktop |

## 🔌 MCP server — PRANA inside your AI tools

```bash
prana-setup --agents     # Claude Code + Claude Desktop + Cursor + Codex + Gemini CLI, one shot
prana-mcp                # …or run the stdio server directly
```

5 tools, zero config beyond your key:

| Tool | Does what |
|---|---|
| `prana_chat` | Run the full agent loop on a task |
| `prana_remember` / `prana_recall` | Write / search long-term memory |
| `prana_schedule` / `prana_jobs` | Create / list autonomous jobs |

Then just tell your agent: *"use PRANA to remember that I like dark mode."*

## 🧠 Memory that actually persists

- **`MEMORY.md`** — durable facts, curated over time
- **People notes** — per-person context (`memory/people/`)
- **Daily log** — append-only journal
- **TF-IDF recall** — relevant memories injected into every turn, 1500-char cap

Write path is guarded: anything that looks like a credential is **refused** —
your keys can never end up in memory files.

## ⏰ Proactivity — the daemon

```bash
prana-daemon                  # runs forever; Ctrl+C stops gracefully
prana-daemon --interval 10    # tick every 10s (default 30s)
```

The agent creates schedules from inside a run:

> *"Remind me every morning at 9 to check the markets"*
> → the agent calls `schedule` → **you approve once** → the daemon picks it up.

Delivery rules (in code, not vibes): first run delivers · unchanged results
stay silent · empty results never deliver · `[NOTIFY]`/`[URGENT]` forces
delivery. File hooks fire on new files. Workers run tasks in parallel.

## 🛡️ Safety — honest version

| Claim | Reality |
|---|---|
| Pairing | First sender on any channel = owner. Strangers silently ignored. |
| Approval gate | `schedule` (autonomous background jobs) **always asks first**. No auto-approve flag exists. The gate fails closed. |
| Shell / files | These run with **your** full machine access (OpenClaw-style). PRANA is *your* agent on *your* machine — only ever talk to your own instance. |
| Keys | `0600` config, `0700` dir, env-var override. Never logged, never in tracebacks, never in memory. |
| Web fetch | Private/loopback/link-local IPs blocked, redirects re-validated. |
| Webhooks | HMAC signature verification (WhatsApp), 1 MB request cap. |

## ⚔️ PRANA vs the world — honest table

| | **PRANA** | OpenClaw |
|---|---|---|
| Maturity | v0.1.0, fresh | ✅ years ahead — 20+ channels, huge community, skills marketplace |
| Dependencies | ✅ **zero** (stdlib only) | Node.js + npm tree |
| Codebase you can read fully | ✅ ~10k lines | — 100k+ lines |
| Approval gate | ✅ `schedule` always asks, fails closed | ✅ approvals |
| Channel pairing | ✅ first-sender owner | ✅ pairing codes |
| Voice | ✅ Telegram voice notes (Whisper+TTS) | ✅ voice calls |
| MCP | ✅ 5 tools | ✅ |
| License | MIT | MIT |

OpenClaw is the more mature project today — no false claims here. PRANA's bet:
**radical simplicity**. Zero dependencies, one readable codebase, pairing-first
safety, and an architecture you can understand in an afternoon and hack on by
evening.

## 🔍 Verify it yourself

```bash
.venv/bin/pytest tests/ -q        # 291 tests — should all pass
python demo/scripted_demo.py      # 8/8 — full system, no key, no network
```

No fake screenshots, no inflated numbers. Every badge above is checkable in
this repo.

## 🤝 Contributing

PRANA is early and hungry for contributors. Good first targets: new channels,
new tools, new personality modes, better recall.

1. Fork → branch → change → **add tests**
2. `pytest tests/ -q` must stay green
3. Open a PR with what you changed and why

Read [`SPEC.md`](SPEC.md) first — it's the architecture contract.

## 📄 License

MIT © 2026 Prabhjot Singh. Build something alive. 🫁
