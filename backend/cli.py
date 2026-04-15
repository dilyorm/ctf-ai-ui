"""Click CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

import click
from rich.console import Console

from backend.config import Settings
from backend.models import DEFAULT_MODELS

console = Console()


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiodocker").setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%X")
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)


@click.command()
@click.option("--ctfd-url", default=None, help="CTFd URL (overrides .env)")
@click.option("--ctfd-token", default=None, help="CTFd API token (overrides .env)")
@click.option("--image", default="ctf-sandbox", help="Docker sandbox image name")
@click.option("--models", multiple=True, help="Model specs (default: all configured)")
@click.option("--challenge", default=None, help="Solve a single challenge directory")
@click.option("--challenges-dir", default="challenges", help="Directory for challenge files")
@click.option(
    "--exclude-challenge",
    "exclude_challenges",
    multiple=True,
    help="Challenge name to exclude (repeatable).",
)
@click.option(
    "--exclude-challenge-regex",
    default=None,
    help="Regex to exclude challenges by name (applies to auto-spawn only).",
)
@click.option("--no-submit", is_flag=True, help="Dry run — don't submit flags")
@click.option(
    "--claude-cli-path",
    default=None,
    help="Path to Claude Code CLI binary (subscription mode). Overrides CLAUDE_CLI_PATH.",
)
@click.option(
    "--claude-config-dir",
    default=None,
    help="Claude config dir (subscription mode). Overrides CLAUDE_CONFIG_DIR.",
)
@click.option(
    "--coordinator-model", default=None, help="Model for coordinator (default: claude-opus-4-6)"
)
@click.option(
    "--coordinator",
    default="claude",
    type=click.Choice(["claude", "codex"]),
    help="Coordinator backend",
)
@click.option("--max-challenges", default=10, type=int, help="Max challenges solved concurrently")
@click.option("--msg-port", default=0, type=int, help="Operator message port (0 = auto)")
@click.option("--ui/--no-ui", default=True, help="Launch the web UI dashboard (default: enabled)")
@click.option("--ui-port", default=8080, type=int, help="Web UI port (default: 8080)")
@click.option("--ui-host", default="0.0.0.0", help="Web UI host (default: 0.0.0.0)")
@click.option(
    "--auto-run/--no-auto-run",
    default=True,
    help="Start solving immediately (use --no-auto-run for UI-driven starts)",
)
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging")
def main(
    ctfd_url: str | None,
    ctfd_token: str | None,
    image: str,
    models: tuple[str, ...],
    challenge: str | None,
    challenges_dir: str,
    exclude_challenges: tuple[str, ...],
    exclude_challenge_regex: str | None,
    no_submit: bool,
    claude_cli_path: str | None,
    claude_config_dir: str | None,
    coordinator_model: str | None,
    coordinator: str,
    max_challenges: int,
    msg_port: int,
    ui: bool,
    ui_port: int,
    ui_host: str,
    auto_run: bool,
    verbose: bool,
) -> None:
    """CTF Agent — multi-model solver swarm.

    Run without --challenge to start the full coordinator (Ctrl+C to stop).
    The web dashboard is available at http://localhost:8080 by default.
    """
    _setup_logging(verbose)

    settings = Settings(sandbox_image=image)
    # UI-only mode should not talk to any default CTFd instance.
    # Clear these before any coordinator logic runs.
    if not auto_run:
        settings.ctfd_url = ""
        settings.ctfd_token = ""
    else:
        if ctfd_url:
            settings.ctfd_url = ctfd_url
        if ctfd_token:
            settings.ctfd_token = ctfd_token
    settings.max_concurrent_challenges = max_challenges

    # Subscription-backed Claude SDK uses the local Claude Code CLI session.
    # Allow callers to override the CLI path / config dir (useful in containers).
    if claude_cli_path:
        settings.claude_cli_path = claude_cli_path
    if claude_config_dir:
        settings.claude_config_dir = claude_config_dir

    model_specs = list(models) if models else list(DEFAULT_MODELS)

    # Normalize exclusions (passed into coordinator loop for auto-spawn)
    exclude_list: list[str] = list(exclude_challenges)

    # UI / env compatibility: allow exclusions to be configured via env vars.
    # CLI flags take precedence when provided.
    if not exclude_list:
        raw = (os.environ.get("EXCLUDE_CHALLENGES") or "").strip()
        if raw:
            # Support newline or comma-separated entries.
            parts: list[str] = []
            for line in raw.splitlines():
                parts.extend(line.split(","))
            exclude_list = [p.strip() for p in parts if p.strip()]

    if exclude_challenge_regex is None:
        exclude_challenge_regex = (os.environ.get("EXCLUDE_CHALLENGE_REGEX") or "").strip() or None

    if exclude_challenge_regex:
        import re

        # Validate early so we fail fast if misconfigured.
        re.compile(exclude_challenge_regex)

    # If the host doesn't have the `codex` CLI installed, codex-backed solvers
    # will crash immediately. Filter them out so the swarm can still run.
    if shutil.which("codex") is None:
        before = list(model_specs)
        model_specs = [m for m in model_specs if not m.startswith("codex/")]
        removed = [m for m in before if m.startswith("codex/")]
        if removed:
            console.print(
                "[yellow]Warning:[/yellow] `codex` CLI not found; disabling codex models: "
                + ", ".join(removed)
            )

    # Claude subscription mode requires the `claude` CLI. If it's not installed and
    # the user didn't provide --claude-cli-path, claude-agent-sdk solvers will fail.
    if not settings.claude_cli_path and shutil.which("claude") is None:
        before = list(model_specs)
        model_specs = [m for m in model_specs if not m.startswith("claude-sdk/")]
        removed = [m for m in before if m.startswith("claude-sdk/")]
        if removed:
            console.print(
                "[yellow]Warning:[/yellow] `claude` CLI not found; disabling claude-sdk models: "
                + ", ".join(removed)
            )

    if not model_specs:
        raise click.ClickException(
            "No models configured after filtering. Install `codex` or pass `--models` explicitly."
        )

    console.print("[bold]CTF Agent v2[/bold]")
    console.print(f"  CTFd: {settings.ctfd_url}")
    console.print(f"  Models: {', '.join(model_specs)}")
    console.print(f"  Image: {settings.sandbox_image}")
    console.print(f"  Max challenges: {max_challenges}")
    if ui:
        console.print(
            f"  Dashboard: http://{ui_host if ui_host != '0.0.0.0' else 'localhost'}:{ui_port}"
        )
    console.print()

    if challenge:
        asyncio.run(
            _run_single(
                settings, challenge, model_specs, no_submit, max_challenges, ui, ui_host, ui_port
            )
        )
    else:
        if not auto_run:
            if not ui:
                raise click.ClickException("--no-auto-run requires --ui")
            # UI-only mode: runs are started via the web UI (/api/run/start).
            asyncio.run(_start_ui_server(ui_host, ui_port))
        else:
            asyncio.run(
                _run_coordinator(
                    settings,
                    model_specs,
                    challenges_dir,
                    exclude_list,
                    exclude_challenge_regex,
                    no_submit,
                    coordinator_model,
                    coordinator,
                    max_challenges,
                    msg_port,
                    ui,
                    ui_host,
                    ui_port,
                )
            )


async def _run_single(
    settings: Settings,
    challenge_dir: str,
    model_specs: list[str],
    no_submit: bool,
    max_challenges: int,
    start_ui: bool = True,
    ui_host: str = "0.0.0.0",
    ui_port: int = 8080,
) -> None:
    """Run a single challenge with a swarm."""
    from backend.agents.swarm import ChallengeSwarm
    from backend.cost_tracker import CostTracker
    from backend.ctfd import CTFdClient
    from backend.prompts import ChallengeMeta
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()

    challenge_path = Path(challenge_dir)
    meta_path = challenge_path / "metadata.yml"
    if not meta_path.exists():
        console.print(f"[red]No metadata.yml found in {challenge_dir}[/red]")
        sys.exit(1)

    meta = ChallengeMeta.from_yaml(meta_path)
    console.print(f"[bold]Challenge:[/bold] {meta.name} ({meta.category}, {meta.value} pts)")

    ctfd = CTFdClient(
        base_url=settings.ctfd_url,
        token=settings.ctfd_token,
        username=settings.ctfd_user,
        password=settings.ctfd_pass,
    )
    cost_tracker = CostTracker()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_path),
        meta=meta,
        ctfd=ctfd,
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=model_specs,
        no_submit=no_submit,
    )

    # Start web UI in background if requested
    ui_task = None
    if start_ui:
        ui_task = asyncio.create_task(
            _start_ui_server(ui_host, ui_port),
            name="ctf-ui-server",
        )
        # Emit the challenge to the UI bus
        try:
            from ui.coordinator_bridge import SwarmObserver
            from ui.event_bus import get_bus

            SwarmObserver.observe(swarm, get_bus())
            get_bus().emit_sync("ctfd_status", {"connected": True, "url": settings.ctfd_url})
        except ImportError:
            pass

    try:
        result = await swarm.run()
        from backend.solver_base import FLAG_FOUND

        if result and result.status == FLAG_FOUND:
            console.print(f"\n[bold green]FLAG FOUND:[/bold green] {result.flag}")
        else:
            console.print("\n[bold red]No flag found.[/bold red]")

        console.print("\n[bold]Cost Summary:[/bold]")
        for agent_name in cost_tracker.by_agent:
            console.print(f"  {agent_name}: {cost_tracker.format_usage(agent_name)}")
        console.print(f"  [bold]Total: ${cost_tracker.total_cost_usd:.2f}[/bold]")
    finally:
        await ctfd.close()
        if ui_task:
            ui_task.cancel()
            try:
                await ui_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass


async def _run_coordinator(
    settings: Settings,
    model_specs: list[str],
    challenges_dir: str,
    exclude_challenges: list[str],
    exclude_challenge_regex: str | None,
    no_submit: bool,
    coordinator_model: str | None,
    coordinator_backend: str,
    max_challenges: int,
    msg_port: int = 0,
    start_ui: bool = True,
    ui_host: str = "0.0.0.0",
    ui_port: int = 8080,
) -> None:
    """Run the full coordinator (continuous until Ctrl+C)."""
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()
    console.print(f"[bold]Starting coordinator ({coordinator_backend}, Ctrl+C to stop)...[/bold]\n")

    # Start web UI dashboard in the background
    ui_task = None
    if start_ui:
        ui_task = asyncio.create_task(
            _start_ui_server(ui_host, ui_port),
            name="ctf-ui-server",
        )

    try:
        if coordinator_backend == "codex":
            from backend.agents.codex_coordinator import run_codex_coordinator

            results = await run_codex_coordinator(
                settings=settings,
                model_specs=model_specs,
                challenges_root=challenges_dir,
                exclude_challenges=exclude_challenges,
                exclude_challenge_regex=exclude_challenge_regex,
                no_submit=no_submit,
                coordinator_model=coordinator_model,
                msg_port=msg_port,
            )
        else:
            from backend.agents.claude_coordinator import run_claude_coordinator

            results = await run_claude_coordinator(
                settings=settings,
                model_specs=model_specs,
                challenges_root=challenges_dir,
                exclude_challenges=exclude_challenges,
                exclude_challenge_regex=exclude_challenge_regex,
                no_submit=no_submit,
                coordinator_model=coordinator_model,
                msg_port=msg_port,
            )
    finally:
        if ui_task:
            ui_task.cancel()
            try:
                await ui_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    console.print("\n[bold]Final Results:[/bold]")
    for challenge, data in results.get("results", {}).items():
        console.print(f"  {challenge}: {data.get('flag', 'no flag')}")
    console.print(f"\n[bold]Total cost: ${results.get('total_cost_usd', 0):.2f}[/bold]")


async def _start_ui_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start the FastAPI UI server as an asyncio task."""
    import uvicorn

    # UI-only server: don't ever spin up a coordinator from .env defaults.
    # Runs must be started via /api/run/start with an explicit CTF selection.

    config = uvicorn.Config(
        "ui.server:app",
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True


@click.command()
@click.argument("message")
@click.option("--port", default=9400, type=int, help="Coordinator message port")
@click.option("--host", default="127.0.0.1", help="Coordinator host")
def msg(message: str, port: int, host: str) -> None:
    """Send a message to the running coordinator."""
    import json
    import urllib.request

    body = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/msg",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            console.print(f"[green]Sent:[/green] {data.get('queued', message[:200])}")
    except Exception as e:
        console.print(f"[red]Failed:[/red] {e}")
        console.print("Is the coordinator running?")
        sys.exit(1)


if __name__ == "__main__":
    main()
