# PRANA — Architecture Spec (P0)

**One-liner:** Give any chatbot a life. PRANA is an open-source "brain" runtime that turns
a plain prompt→response chatbot into a Muse-like autonomous agent.

**Status:** P0 — spec. Name locked 🫁. Plan approved 2026-10-18.

---

## 1. Core insight (the honest version)

A chatbot is stateless: prompt in, text out. An agent like Muse is a **runtime**:
it generates its own prompts (schedules, hooks, background workers), uses **tools**,
keeps **memory**, and has **initiative**. No magic, no "feelings" — just architecture.
PRANA implements that architecture as a reusable, open-source layer over any LLM.

```
CHATBOT:   user prompt → LLM → reply
PRANA:     events (user msg / cron tick / hook / worker result)
             → memory recall → LLM + tools (loop) → reply / action / silence
             → memory write
```

---

## 2. The six systems

### 2.1 Core loop (the heart)
- **ReAct loop:** `reason → act (tool call) → observe → repeat` until done or budget hit.
- **Turn budget:** max N tool iterations per event (default 25), max wall-clock per turn.
- **Stop conditions:** model emits final answer, budget exhausted, user interrupt.
- **Event types:** `user_message`, `cron_tick`, `hook_event`, `worker_result`, `system`.
- The loop is model-agnostic: it speaks a single internal message format and
  translates to each provider's tool-call protocol.

### 2.2 Tools (the hands)
- **Tool registry:** every tool declares `name`, `description`, JSON `parameters`,
  `side_effects` class (`read` / `write` / `external` / `irreversible`).
- **Built-in v1:** `exec` (shell), `read`/`write`/`edit` (files), `web_search`,
  `fetch_url`, `remember` (memory write), `schedule` (create cron).
- **Approval policy:** `read` = auto; `write`/`external` = auto with logging;
  `irreversible` (currently: creating autonomous `schedule` jobs) = **requires
  user approval**, always. No exceptions, no config to disable silently.
  Shell/file tools run with the user's own machine access (single-user agent
  model) — the safety boundary is *who can talk to the agent*, enforced by
  channel pairing (§2.6).
- Tools are the only way the agent touches the world. The model never acts directly.

### 2.3 Memory (the past)
- **Long-term (`MEMORY.md` style):** curated facts — user profile, preferences,
  decisions, commitments. Written by the agent, read every turn.
- **People & groups:** per-person notes, relationship context.
- **Daily notes:** append-only log; background summarizer promotes durable facts up.
- **Vector recall:** embeddings over notes for "what did we discuss about X?" retrieval.
- **Write policy:** durable facts only — never credentials, never raw secrets.
  Memory is private to the user's instance, stored locally (SQLite + files).

### 2.4 Proactivity engine (the "alive" part) ⚡
This is what makes PRANA feel alive instead of reactive:
- **Cron:** scheduled jobs (reminders, checks, digests). Each has owner, schedule,
  stop condition, delivery target.
- **Hooks:** event-driven watchers (file changed, message arrived, price moved).
- **Workers:** background subagents for long tasks; results return as events.
- **Delivery rules:** notify the user only when genuinely new + worth attention.
  Routine/unchanged results stay silent. The engine decides; the user can tune
  preferences ("don't ping me about X").
- **Key rule:** a schedule is not a promise — every job verifies its own delivery.

### 2.5 Personality core (the character)
- **Identity file** (`SOUL.md` style): who the agent is — name, vibe, modes.
- **Modes:** e.g. casual / teacher / engineer / strategist — same brain, different register.
- **System prompt assembly:** identity + memory summary + tool docs + event context,
  composed fresh each turn. The user can edit identity; the agent can't silently
  rewrite its own core directives.
- **Voice:** responds in the user's language and energy; warm, direct, no corporate filler.

### 2.6 Channels (the voice)
- **v1: Telegram bot** (simplest robust two-way channel).
- **v2:** WhatsApp, Discord.
- Each channel: ingress (message → event) → loop → egress (reply / proactive ping).
- The agent is reachable where the user already lives — not another app to open.

---

## 3. Data flows

### User message
```
Telegram msg → channel adapter → user_message event
  → recall memory (profile + relevant notes)
  → assemble system prompt → ReAct loop (tools as needed)
  → reply → write memory (if something durable learned)
```

### Scheduled tick (the "own prompts" part)
```
cron fires → cron_tick event (with job's recorded request)
  → agent works the task (tools, subagents)
  → delivery decision: message user / stay silent
  → log outcome; reschedule or close the job
```

---

## 4. BYOK model layer
- User brings their own API key (OpenAI / Anthropic / Gemini). Key lives in local
  config, never leaves the machine, never logged.
- Provider adapter normalizes: chat completions + tool calls + streaming.
- Auto-detect provider from key prefix; fetch live model list; sensible default per
  provider. Bad key → clear error, never silent fallback.

## 5. Safety model (non-negotiable)
1. Creating autonomous jobs (`schedule` tool) needs explicit user approval — every time. The gate fails closed; there is no auto-approve flag. Shell/file tools run with the user's full machine access (single-user agent model); channels pair to the first user so strangers cannot drive the agent.
2. No credentials in memory, logs, or prompts. Ever.
3. Outbound communication (messages, posts, emails) is always explicit or scheduled —
   never a side effect of "being helpful."
4. The agent cannot rewrite its own identity/safety files; the user owns them.
5. Local-first: data stays on the user's machine. No telemetry without opt-in.

## 6. Phases & exit criteria

| Phase | Goal | Exit criteria |
|---|---|---|
| **P0** | Architecture spec | This document approved ✅ |
| **P1** | Core loop + tools | CLI agent: takes a task, uses shell/files/web tools, completes it, tested |
| **P2** | Memory + personality | Remembers user across restarts; editable identity; modes work |
| **P3** | Proactivity engine ⚡ | Cron + hooks + workers live; agent pings user unprompted with something useful |
| **P4** | Channels | Telegram two-way live; proactive pings arrive on phone |
| **P5** | Launch | Docs, demo video, GitHub public, announcement |

## 7. Non-goals (v1)
- Training/fine-tuning models — we orchestrate, not train.
- Multi-user server / SaaS — single-user local runtime first.
- Voice calls — text channels first.

## 8. Open questions for Prabh
1. Telegram first for channels, or CLI-only until P3? (Recommend: Telegram at P4 as planned.)
2. Repo: create `Thegreatprabh/Prana` on GitHub when ready for P1? (I can't create repos — your 2-min step.)
3. License: MIT (recommend — matches ARGUS, maximal adoption).

---

*PRANA 🫁 — "chatbot ko prana dena." Modeled on a working system: Muse.*
*P0 written 2026-10-18. Next: P1 core loop.*
