"""Pydantic Settings — credentials from .env file + environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # CTFd
    ctfd_url: str = "http://localhost:8000"
    ctfd_user: str = "admin"
    ctfd_pass: str = "admin"
    ctfd_token: str = ""

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

    # Infra
    sandbox_image: str = "ctf-sandbox"
    max_concurrent_challenges: int = 10
    max_attempts_per_challenge: int = 3
    container_memory_limit: str = "16g"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
