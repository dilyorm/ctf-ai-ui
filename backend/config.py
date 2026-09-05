"""Pydantic Settings — credentials from .env file + environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # CTF platform: "ctfd" (default) or "rctf"
    platform: str = "ctfd"

    # CTFd (the field name is retained for backwards compatibility — it holds
    # the URL/token for whichever platform is selected)
    ctfd_url: str = "http://localhost:8000"
    ctfd_user: str = "admin"
    ctfd_pass: str = "admin"
    ctfd_token: str = ""
    # API mount point for CTFd (standard "/api/v1"; SAS CTF uses "/public-api")
    ctfd_api_base: str = "/api/v1"
    # Generic-platform adapter spec (JSON string) — used when platform == "generic".
    platform_adapter_json: str = ""

    # Subscription/API tokens for OpenAI-compatible providers (leased from the
    # shared pool at run time; may also be set directly for single-user runs).
    grok_api_key: str = ""
    kimi_api_key: str = ""
    antigravity_api_key: str = ""

    # API Keys
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # Claude subscription (Claude Code CLI)
    # When using claude-agent-sdk (provider: claude-sdk/*), auth comes from the
    # local Claude Code CLI session, not ANTHROPIC_API_KEY.
    claude_cli_path: str = ""  # optional override for the `claude` binary (env: CLAUDE_CLI_PATH)
    claude_config_dir: str = ""  # optional override for Claude config home (env: CLAUDE_CONFIG_DIR)

    # Codex subscription (OpenAI Codex CLI with ChatGPT account auth)
    # When set, the codex solver uses HOME={codex_config_dir} so credentials
    # stored by `codex auth login` are isolated per user.
    codex_cli_path: str = ""  # optional override for the `codex` binary (env: CODEX_CLI_PATH)
    codex_config_dir: str = ""  # per-user home for codex credentials (env: CODEX_CONFIG_DIR)

    # Provider-specific (optional, for Bedrock/Azure/Zen fallback)
    aws_region: str = "us-east-1"
    aws_bearer_token: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    opencode_zen_api_key: str = ""

    # GitHub Copilot — long-lived GitHub OAuth/PAT, exchanged at runtime for
    # short-lived Copilot session tokens by ``backend.copilot_auth``.
    github_copilot_oauth_token: str = ""

    # Infra
    sandbox_image: str = "ctf-sandbox"
    max_concurrent_challenges: int = 10
    # For the manual platform: which CTF's hand-entered Tasks to solve.
    ctf_id: int = 0
    # Parallel solver sessions allowed on a subscription that is NOT in the
    # account pool (the server's own CLI login). Pooled accounts use their own
    # per-account `max_concurrent` instead.
    ambient_solver_concurrency: int = 1
    max_attempts_per_challenge: int = 3
    container_memory_limit: str = "16g"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
