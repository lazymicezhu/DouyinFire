"""Maintenance entrypoints. Normal users launch DouyinFire.app."""
from __future__ import annotations

from pathlib import Path
import typer

from .config import DEFAULT_CONFIG_PATH, load_config

app = typer.Typer(help='DouyinFire maintenance tools; use DouyinFire.app for normal operation.')


@app.command()
def gui(config: Path = typer.Option(DEFAULT_CONFIG_PATH, '--config', '-c'), port: int = 8765):
    from .desktop import ensure_started
    import webbrowser
    webbrowser.open(ensure_started(config, port))


@app.command()
def serve(config: Path = typer.Option(DEFAULT_CONFIG_PATH, '--config', '-c'), port: int = 8765):
    from .desktop import serve as start_server
    start_server(config, port)


@app.command()
def background(config: Path = typer.Option(DEFAULT_CONFIG_PATH, '--config', '-c'), port: int = 8765):
    from .desktop import ensure_started
    ensure_started(config, port)


@app.command()
def doctor(config: Path = typer.Option(DEFAULT_CONFIG_PATH, '--config', '-c')):
    """Actually launch the bundled browser on an isolated blank page."""
    import json, tempfile
    from playwright.sync_api import sync_playwright
    cfg = load_config(config)
    checks = {'config': True, 'accounts': len(cfg.users), 'browser': False, 'authenticated': 'not checked'}
    try:
        with tempfile.TemporaryDirectory(prefix='douyinfire-doctor-') as directory:
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(directory, headless=True, channel='chromium')
                try:
                    page = context.pages[0]
                    page.goto('about:blank')
                    checks['browser'] = page.evaluate('1 + 1') == 2
                finally:
                    context.close()
    except Exception as exc:
        checks['error'] = str(exc)
    typer.echo(json.dumps(checks, ensure_ascii=False, indent=2))
    raise typer.Exit(0 if checks['browser'] else 1)


if __name__ == '__main__':
    app()
