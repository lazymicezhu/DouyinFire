from __future__ import annotations

import random
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config, write_example_config
from .gui import run_gui
from .scheduler import delay_until, next_run_at
from .service import install_service, service_status, uninstall_service
from .tasks import login_user, run_all as execute_all, run_user as execute_user


app = typer.Typer(help="DouyinFire local automation CLI.")
service_app = typer.Typer(help="Manage the macOS launchd service.")
app.add_typer(service_app, name="service")
console = Console()


@app.command()
def init(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Config path to create."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite an existing config file."),
) -> None:
    """Create a local example config."""
    path = write_example_config(config, overwrite=overwrite)
    console.print(f"[green]Created config:[/green] {path}")


@app.command()
def login(
    user: str = typer.Option(..., "--user", "-u", help="User name from the config file."),
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Config path."),
) -> None:
    """Open a browser and save the user's Douyin login state."""
    cfg = _load(config)
    login_user(cfg, cfg.user(user))


@app.command("run")
def run_one(
    user: str = typer.Option(..., "--user", "-u", help="User name from the config file."),
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Config path."),
) -> None:
    """Run one configured user immediately."""
    cfg = _load(config)
    result = execute_user(cfg, cfg.user(user))
    _print_user_result(result)
    raise typer.Exit(code=1 if result.failure_count else 0)


@app.command("run-all")
def run_all(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Config path."),
    jitter: bool = typer.Option(False, "--jitter/--no-jitter", help="Sleep for a random schedule jitter before running."),
) -> None:
    """Run all enabled users immediately."""
    cfg = _load(config)
    if jitter and cfg.schedule.jitter_minutes:
        delay = random.randint(0, cfg.schedule.jitter_minutes * 60)
        console.print(f"Schedule jitter enabled; sleeping {delay} seconds before running.")
        time.sleep(delay)
    result = execute_all(cfg)
    table = Table(title=f"Run {result.run_id}")
    table.add_column("User")
    table.add_column("Success")
    table.add_column("Failure")
    for user_result in result.users:
        table.add_row(user_result.user, str(user_result.success_count), str(user_result.failure_count))
    console.print(table)
    raise typer.Exit(code=1 if result.failure_count else 0)


@app.command()
def doctor(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Config path."),
) -> None:
    """Check config, dependencies, and local runtime directories."""
    ok = True
    table = Table(title="DouyinFire Doctor")
    table.add_column("Check")
    table.add_column("Status")

    cfg = None
    try:
        cfg = _load(config)
        table.add_row("config", "[green]ok[/green]")
    except Exception as exc:
        ok = False
        table.add_row("config", f"[red]{exc}[/red]")

    try:
        import playwright  # noqa: F401
        table.add_row("playwright package", "[green]ok[/green]")
    except Exception as exc:
        ok = False
        table.add_row("playwright package", f"[red]{exc}[/red]")

    if cfg:
        for path in [cfg.data_dir, cfg.log_dir, cfg.screenshot_dir]:
            path.mkdir(parents=True, exist_ok=True)
            table.add_row(str(path), "[green]ok[/green]")
        for user_config in cfg.users:
            profile = cfg.data_dir / "profiles" / user_config.name
            status = "present" if profile.exists() else "missing; run login"
            color = "green" if profile.exists() else "yellow"
            table.add_row(f"profile:{user_config.name}", f"[{color}]{status}[/{color}]")

    console.print(table)
    raise typer.Exit(code=0 if ok else 1)


@app.command("next-run")
def next_run(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Config path."),
) -> None:
    """Show the next scheduled run time including jitter."""
    cfg = _load(config)
    target = next_run_at(__import__("datetime").datetime.now(), cfg.schedule)
    console.print(f"Next run: {target.isoformat(timespec='seconds')} ({delay_until(target)} seconds from now)")


@app.command()
def gui(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Config path."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind."),
    port: int = typer.Option(8765, "--port", "-p", help="Port to bind."),
) -> None:
    """Start the local web GUI."""
    run_gui(config_path=config, host=host, port=port)


@service_app.command("install")
def service_install(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Config path."),
) -> None:
    """Install or reload the macOS launchd service."""
    path = install_service(Path.cwd(), config)
    console.print(f"[green]Installed launchd service:[/green] {path}")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Unload and remove the macOS launchd service."""
    removed = uninstall_service()
    console.print("[green]Service removed[/green]" if removed else "[yellow]Service was not installed[/yellow]")


@service_app.command("status")
def service_status_command() -> None:
    """Print launchd service status."""
    console.print(service_status())


def _load(config: Path):
    try:
        return load_config(config)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc


def _print_user_result(result) -> None:
    table = Table(title=f"User {result.user}")
    table.add_column("Contact")
    table.add_column("Status")
    table.add_column("Reason")
    for item in result.results:
        table.add_row(item.contact, "ok" if item.success else "failed", item.reason)
    console.print(table)


if __name__ == "__main__":
    app()
