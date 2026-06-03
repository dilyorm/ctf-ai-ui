# Deploying ctf-ai-ui

The app is served at **https://ctf.dilyor.dev** → nginx → `127.0.0.1:8080`.
Postgres runs in the `ctf-agent-postgres` Docker container (`scripts/start-postgres.sh`).

## One-time: install as a systemd service

The app used to be started by hand (and would die on reboot/crash). Install the
unit so it auto-starts and restarts on failure:

```bash
# on the server (ssh ctf)
cd ~/ctf-ai-ui
git pull
which uv                       # confirm uv path; edit ExecStart in the unit if it differs
sudo cp deploy/ctf-ai-ui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ctf-ai-ui
sudo systemctl status ctf-ai-ui --no-pager
```

Logs: `journalctl -u ctf-ai-ui -f`

## Required `.env` keys

```
APP_SECRET_KEY=<long random>      # REQUIRED: encrypts stored secrets (Fernet)
UI_SECRET_KEY=<long random>       # session cookie signing
DB_URL=postgresql+asyncpg://ctf_agent:ctf_agent@127.0.0.1:5432/ctf_agent
CTFD_URL=...                      # default CTFd (optional; per-CTF overrides exist)
GITHUB_CLIENT_ID=...              # optional GitHub OAuth
GITHUB_CLIENT_SECRET=...
```

## Deploy an update

```bash
cd ~/ctf-ai-ui
git pull
uv sync
uv run alembic upgrade head       # applies new migrations (e.g. 0004 pooled_accounts)
sudo systemctl restart ctf-ai-ui
```

## Account pool (multi-account failover)

- Accounts live in the **shared pool** (`pooled_accounts` table) — every run uses
  every account, regardless of who connected it.
- Connect accounts from the **Accounts** page: each "Connect" runs the CLI web
  sign-in into its own isolated config dir under `~/.claude-ctf-agents/acct-*`
  or `~/.codex-ctf-agents/acct-*`.
- Each account has a `max_concurrent` (default 1 — subscriptions rate-limit hard
  on parallel sessions). Set higher only for plans that tolerate it.
- On a quota/limit error a solver puts the account on cooldown (parsed from the
  provider's retry-after when present, else 1h) and rotates to the next free
  subscription account. When all accounts of a provider are cooling, the
  challenge is **parked** and retried automatically once one frees up.
- The CLI binaries (`claude`, `codex`) must be installed and on PATH for the
  service user:
  - `npm install -g @anthropic-ai/claude-code`
  - `npm install -g @openai/codex`
