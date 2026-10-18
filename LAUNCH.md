# PRANA Launch Checklist — your 10 minutes

Everything below needs a human. The code is done, tested (134/134 green),
and documented. Nothing here takes skill — just clicks and pastes.

## 1. Create the GitHub repo (2 min)

I can't create repos (GitHub API returns 403 for that). You do it:

1. Go to **https://github.com/new**
2. Repository name: **`Prana`**, owner: `Thegreatprabh`
3. Public. **Don't** initialize with README (we have our own).
4. Create.

## 2. Push the code (2 min)

From `~/workspace/prana` (or wherever this folder lives on your machine):

```bash
cd ~/workspace/prana
git init
git add .
git commit -m "PRANA v0.1.0 — give any chatbot a life"
git branch -M main
git remote add origin https://github.com/Thegreatprabh/Prana.git
git push -u origin main
```

Verify: repo page shows the README with the six systems table.

## 3. Repo settings (1 min, optional but worth it)

- About → description: `Give any chatbot a life — open-source brain runtime: ReAct loop, tools, memory, proactivity, Telegram. Stdlib-only Python.`
- Topics: `ai-agent`, `autonomous-agent`, `llm`, `chatbot`, `byok`, `telegram-bot`
- License already MIT in-repo.

## 4. API key (1 min)

```bash
pip install -e .
prana --save-key     # paste your OpenAI / Anthropic / Gemini key
prana "say hello"    # first real run
```

Key goes to `~/.prana/config.json` (0600). Never leaves your machine.

## 5. Telegram bot (2 min)

1. Telegram → **@BotFather** → `/newbot` → copy the token.
2. `export TELEGRAM_BOT_TOKEN=<token>` then `prana-telegram`.
3. Message your bot. It should reply.

For proactive pings on your phone: message **@userinfobot** to get your
chat id, then tell the agent `ping me on telegram:<chat-id> when ...`.

## 6. Sanity checks (2 min)

```bash
prana --version && prana-daemon --help >/dev/null && prana-telegram --help >/dev/null && echo "entry points OK"
python demo/scripted_demo.py   # 8/8 PASS, no key needed
pytest -q                      # 134 passed
```

## After launch

- Star your own repo (day-one signal).
- Post it: one tweet/LinkedIn post — "I built an open-source brain that
  turns any chatbot into an autonomous agent."
- Issues will come. The test suite is the safety net — keep it green.

---

*PRANA v0.1.0 — P1→P5 complete. MIT. Built 2026-10-18.*
