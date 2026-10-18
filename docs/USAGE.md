# PRANA Usage Guide

Practical guide for daily use. Install: see [README](../README.md).

## Modes

Same brain, different register. Three ways to pick:

```bash
prana --mode teacher "explain recursion"
prana --mode engineer --chat
```

Or add a `mode:` line to your identity file (`~/.prana/identity.md`):

```markdown
mode: strategist
```

Precedence: `--mode` flag > `mode:` line in identity file > default (`casual`).

| Mode | Register |
|------|----------|
| `casual` | warm, direct, to the point |
| `teacher` | patient, step-by-step, checks understanding |
| `engineer` | rigorous, detail-obsessed, tests everything |
| `strategist` | big-picture, trade-offs, opportunity-spotting |

## Memory — `remember` / `recall`

The agent writes memories itself via the `remember` tool when it learns
something durable. You can also do it explicitly in chat:

> "Remember that I prefer morning meetings"

What gets stored: `~/.prana/memory/MEMORY.md` (curated facts),
`~/.prana/memory/people/<name>.md` (people notes),
`~/.prana/memory/daily/YYYY-MM-DD.md` (append-only log).

**Recall is automatic** — every turn, relevant memories are injected into the
system prompt (token-capped). You can also ask directly:

> "What do you remember about my project?"

**Hard rule:** anything credential-like is refused at write time — it never
reaches the file. If you see `refused: that looks like a credential`, that's
the guard working.

## Scheduling — the `schedule` tool

From any chat (or one-shot task), just say it in plain language:

> "Remind me every weekday at 9am to check the Nifty"
> "Ping me every 30 minutes until I say stop — is the deploy done?"

The agent calls the `schedule` tool. **Creating a job asks for your approval
first** (it's `irreversible` — the daemon will act on it autonomously later).
Accepted formats:

- Cron: `30 9 * * 1-5` (min hour day month weekday)
- Shorthands: `@hourly`, `@daily`, `@weekly`
- Intervals: `every 30m`, `every 2h`, `every 1d`

Jobs live in `~/.prana/jobs/` as JSON (0600). The daemon runs them:

```bash
prana-daemon                 # foreground, Ctrl+C stops
prana-daemon --interval 10   # check every 10s instead of 30s
```

Run it in the background however you like (`nohup`, `tmux`, systemd user
service — your choice, PRANA doesn't care).

**Delivery rules** — when a job runs, the agent decides:

| Result | Action |
|--------|--------|
| First run | delivered |
| Changed since last run | delivered |
| Unchanged | silent |
| Empty | silent, always |
| Contains `[notify]` | delivered, forced |

To receive pings on Telegram instead of the console, set the job's delivery
target to `telegram:<chat-id>` (see below).

### Managing jobs manually

```python
from prana.proactivity import jobs
jobs.list_jobs()                    # all jobs
jobs.set_active(job_id, False)      # pause
jobs.set_active(job_id, True)       # resume
jobs.remove_job(job_id)             # delete
```

## Channel pairing (Telegram / WhatsApp / Discord)

Every channel pairs to the **first person who messages it** — that sender
becomes the owner, stored in `~/.prana/config.json` under
`channels.<name>.allowed_ids`. Everyone else is silently ignored: no reply,
no error, nothing to probe.

Telegram:

1. **@BotFather** → `/newbot` → name it → copy the token.
2. `export TELEGRAM_BOT_TOKEN=<token>` (or save it in `~/.prana/config.json`
   under `"telegram": {"bot_token": "..."}`).
3. `prana-telegram` — talk to your bot like any chat.

WhatsApp: `prana-whatsapp` (see README + `docs/guides/PRANA-WhatsApp-Setup.pdf`).
Set `WHATSAPP_APP_SECRET` so Meta's webhook signature is verified.
Discord: `prana-discord` (Developer Portal → Bot → Message Content intent ON).

To pair an additional person later, add their id to the channel's
`allowed_ids` list in `~/.prana/config.json` (Telegram: chat id from
**@userinfobot**; WhatsApp: full phone number; Discord: user id).

**Proactive pings:** tell the agent "ping me on Telegram when X", or set a
job's `delivery_target` to `telegram:<chat-id>` / `whatsapp:<number>` /
`discord:<channel-id>`.

`/start` and `/help` are answered instantly without spending LLM tokens.

## File hooks

Watch a file; when it changes, the agent wakes up:

```python
from prana.proactivity import hooks
hooks.add_hook("/path/to/file", "summarize what changed and tell me if it matters")
```

The daemon's watcher picks it up on the next tick.

## The approval gate

Tool side-effects:

| Class | Behavior |
|-------|----------|
| `read` | auto-approved |
| `write`, `external` | auto-approved, logged |
| `irreversible` (creating autonomous `schedule` jobs) | **asks you y/n, every time** |

There is no `--yes` flag, no config to disable this. That's deliberate.

Honest model: PRANA is *your* agent on *your* machine — shell/file tools run
with your full access (like OpenClaw). The safety boundary is **who can talk
to the agent**: channels pair to the first user and silently ignore everyone
else. Only ever talk to YOUR OWN bot instance.

## Troubleshooting

**"Provider error" / model not found (Gemini).** Google rotates model names;
the built-in default can go stale. Pass an explicit model:

```bash
prana --model gemini-2.0-flash "hello"
```

**"No API key" from the daemon.** The daemon reads the same
`~/.prana/config.json` as the CLI. Run `prana --save-key` once and the daemon
picks it up — no separate config.

**Telegram: "no Telegram bot token".** Set `TELEGRAM_BOT_TOKEN` in the same
shell you launch `prana-telegram` from, or put it in the config file.

**Daemon not firing jobs.** Check the tick interval (default 30s — a
`every 1m` job fires on the first tick after its minute is due) and that the
job is active: `jobs.list_jobs()`.

**Tests.** `pytest` from the repo root — 135 tests, all offline, no keys.
