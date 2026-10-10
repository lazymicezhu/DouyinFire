"""Frozen macOS entrypoint. Keep multiprocessing dispatch before GUI imports."""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import tempfile
import traceback
from importlib.metadata import version
from pathlib import Path


def resource_browsers() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.parent / "Resources" / "browsers"
    return Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / "Library" / "Caches" / "ms-playwright"))


def _spawn_probe(queue) -> None:
    queue.put({"pid": os.getpid(), "frozen": bool(getattr(sys, "frozen", False))})


def self_check(output: Path) -> int:
    """No website, account, stored profile, or notifications are touched."""
    report = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)), "executable": sys.executable,
              "browser_resources": str(resource_browsers())}
    try:
        import douyinfire.desktop  # Verify lazy desktop dependencies are packaged.
        import douyinfire.manager
        from playwright.sync_api import sync_playwright
        report["playwright"] = version("playwright")
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        worker = ctx.Process(target=_spawn_probe, args=(queue,))
        worker.start()
        worker.join(timeout=20)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
            raise RuntimeError("Packaged multiprocessing worker did not finish")
        if worker.exitcode != 0:
            raise RuntimeError(f"Packaged multiprocessing worker exited {worker.exitcode}")
        report["worker"] = queue.get(timeout=5)
        queue.close()
        queue.join_thread()
        with tempfile.TemporaryDirectory(prefix="douyinfire-package-check-") as temp:
            with sync_playwright() as playwright:
                report["chromium_executable"] = playwright.chromium.executable_path
                if not Path(playwright.chromium.executable_path).is_relative_to(resource_browsers()):
                    raise RuntimeError("Browser executable escaped bundled resources")
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=temp, headless=True, channel="chromium",
                    viewport={"width": 800, "height": 600})
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("about:blank")
                page.set_content("<title>DouyinFire package check</title><p>offline</p>")
                report["page_title"] = page.title()
                report["browser_version"] = context.browser.version if context.browser else "persistent context"
                context.close()
        report["ok"] = report["page_title"] == "DouyinFire package check"
    except Exception:
        report["error"] = traceback.format_exc()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


def main() -> None:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(resource_browsers())
    multiprocessing.freeze_support()
    if "--self-check" in sys.argv:
        index = sys.argv.index("--self-check")
        if index + 1 >= len(sys.argv):
            raise SystemExit("--self-check requires an output report path")
        raise SystemExit(self_check(Path(sys.argv[index + 1])))
    if sys.stdout is None or sys.stderr is None:
        from douyinfire.runtime import default_app_dir
        log_root = default_app_dir() / "logs"
        if "--config" in sys.argv:
            position = sys.argv.index("--config")
            if position + 1 < len(sys.argv):
                log_root = Path(sys.argv[position + 1]).resolve().parent.parent / "logs"
        log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stream = (log_root / "startup.log").open("a", encoding="utf-8", buffering=1)
        os.chmod(log_root / "startup.log", 0o600)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    from douyinfire.desktop import main as desktop_main
    raise SystemExit(desktop_main())


if __name__ == "__main__":
    main()
