#!/usr/bin/env python3
"""Build a relocatable local .app with Python, Playwright, and full Chromium.

Run from the project's build virtual environment after installing runtime
requirements and scripts/requirements-build.txt. This never downloads a browser,
starts DouyinFire, registers launchd, accesses accounts, or uploads artifacts.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def chromium_source(cache: Path) -> tuple[Path, str]:
    import playwright
    metadata = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    chromium = next(item for item in json.loads(metadata.read_text())["browsers"] if item["name"] == "chromium")
    revision = chromium.get("revisionOverrides", {}).get("mac11-arm64" if platform.machine() == "arm64" else "mac11", chromium["revision"])
    source = cache / f"chromium-{revision}"
    arch_dir = "chrome-mac-arm64" if platform.machine() == "arm64" else "chrome-mac-x64"
    executable = source / arch_dir / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"
    if not executable.is_file():
        raise SystemExit(f"Matching full Chromium was not found: {executable}. Install Chromium with this environment's Playwright first.")
    return source, revision


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-cache", type=Path, default=Path.home() / "Library" / "Caches" / "ms-playwright")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if platform.system() != "Darwin":
        raise SystemExit("This builder runs on macOS and builds for the current Mac's architecture.")
    try:
        import PyInstaller
    except ImportError:
        raise SystemExit("Install scripts/requirements-build.txt in the build environment first.") from None
    project = Path(__file__).resolve().parent.parent
    # Running a script adds scripts/, not the checkout, to sys.path. Resolve
    # this checkout explicitly, including paths containing a literal colon.
    sys.path.insert(0, str(project))
    if not (project / "douyinfire" / "desktop.py").is_file():
        raise SystemExit("douyinfire/desktop.py is required before building.")
    output = (args.output or project / "dist").resolve()
    output.mkdir(parents=True, exist_ok=True)
    source, revision = chromium_source(args.browser_cache.expanduser().resolve())
    # Signing inside an iCloud checkout fails because FileProvider attaches
    # FinderInfo to bundles. Assemble and sign on a local temporary filesystem.
    with tempfile.TemporaryDirectory(prefix="douyinfire-build-") as temp:
        work = Path(temp)
        env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / "pyinstaller-cache"))
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
                   "--name", "DouyinFire", "--osx-bundle-identifier", "com.lazymice.douyinfire",
                   "--target-architecture", platform.machine(), "--distpath", str(work / "dist"),
                   "--workpath", str(work / "work"), "--specpath", str(work),
                   "--collect-all", "playwright", "--collect-submodules", "douyinfire",
                   "--copy-metadata", "playwright", "--copy-metadata", "typer", "--copy-metadata", "rich",
                   "--exclude-module", "cloakbrowser", "--exclude-module", "pytest",
                   "--exclude-module", "tkinter", str(project / "scripts" / "macos_entry.py")]
        subprocess.run(command, cwd=project, env=env, check=True)
        built = work / "dist" / "DouyinFire.app"
        resources = built / "Contents" / "Resources"
        from douyinfire.desktop import APP_VERSION
        info_path = built / "Contents" / "Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        info.update(CFBundleDisplayName="DouyinFire", CFBundleShortVersionString=APP_VERSION,
                    CFBundleVersion=APP_VERSION, LSUIElement=True, NSHighResolutionCapable=True)
        info_path.write_bytes(plistlib.dumps(info))
        target = resources / "browsers" / source.name
        shutil.copytree(source, target, symlinks=True, copy_function=shutil.copy)
        manifest = {"architecture": platform.machine(), "python": platform.python_version(), "pyinstaller": PyInstaller.__version__,
                    "playwright": importlib.metadata.version("playwright"), "chromium_revision": revision,
                    "signature": "local ad-hoc; not notarized", "browser_path": f"Contents/Resources/browsers/{source.name}"}
        (resources / "build-info.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        # Re-seal after adding the browser and preserve Chromium JIT entitlements.
        subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-",
                        "--preserve-metadata=entitlements,requirements,flags", str(built)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(built)], check=True)
        app = output / "DouyinFire.app"
        if app.exists():
            shutil.rmtree(app)
        shutil.copytree(built, app, symlinks=True, copy_function=shutil.copy)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    # Remove only the redundant companion from older builds of this script.
    companion = output / "DouyinFire"
    if companion.is_dir():
        shutil.rmtree(companion)
    print(f"Built {app}")
    print("Local ad-hoc signature only; no upload or notarization was performed.")


if __name__ == "__main__":
    main()
