#!/usr/bin/env python3
"""
Le Grinder Watchdog — keeps the bot and Edge browser alive 24/7.
- Restarts the bot if it crashes
- Re-launches Edge if port 9222 goes down
- Auto-logins if Wealthsimple session expires
"""
import subprocess
import platform
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT      = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PYTHON    = sys.executable
BOT       = ROOT / "scripts" / "run_grinder.py"
AUTOLOGIN = ROOT / "scripts" / "_autologin.py"
LOG_FILE  = ROOT / "data" / "grinder.log"
PROFILE   = ROOT / "data" / "browser_profile"
CDP_URL   = "http://localhost:9222"
WS_HOME   = "https://my.wealthsimple.com/app/home"
TZ        = ZoneInfo("America/Toronto")


def log(msg: str) -> None:
    line = f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S} ET] [watchdog] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def edge_alive() -> bool:
    try:
        urllib.request.urlopen(f"{CDP_URL}/json", timeout=3)
        return True
    except Exception:
        return False


def launch_edge() -> None:
    from scripts.wealthsimple_auto import find_browser_executable
    browser_exe = find_browser_executable()
    log("Launching browser with remote debugging on port 9222...")
    subprocess.Popen([
        browser_exe,
        "--remote-debugging-port=9222",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        WS_HOME,
    ])
    # Wait for browser to be ready
    for _ in range(20):
        time.sleep(2)
        if edge_alive():
            log("Edge is up.")
            return
    log("WARNING: Edge did not come up on port 9222 after 40s.")


def ensure_edge_and_session() -> None:
    if not edge_alive():
        launch_edge()
        time.sleep(3)

    # Always attempt auto-login in case session expired
    try:
        result = subprocess.run(
            [PYTHON, str(AUTOLOGIN)],
            cwd=ROOT, capture_output=True, text=True, timeout=90,
        )
        output = result.stdout.strip()
        if output:
            log(f"Session check: {output.splitlines()[-1]}")
    except Exception as exc:
        log(f"Auto-login error: {exc}")


def prevent_sleep() -> None:
    """Keep the computer awake while this watchdog is running."""
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["caffeinate", "-dims", "-w", str(os.getpid())])
            log("Sleep prevention active (caffeinate).")
            return
        import ctypes
        ES_CONTINUOUS       = 0x80000000
        ES_SYSTEM_REQUIRED  = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log("Sleep prevention active (SetThreadExecutionState).")
    except Exception as exc:
        log(f"Could not enable sleep prevention: {exc}")


def main() -> None:
    log("=" * 50)
    log("Le Grinder Watchdog — STARTING")
    log("=" * 50)
    prevent_sleep()

    restart_count = 0

    while True:
        ensure_edge_and_session()

        log(f"Starting bot (run #{restart_count + 1})...")
        proc = subprocess.Popen(
            [PYTHON, str(BOT)],
            cwd=ROOT,
        )

        # Monitor while bot runs — check Edge every 60s
        while proc.poll() is None:
            time.sleep(60)
            if not edge_alive():
                log("Edge went down while bot is running — relaunching...")
                launch_edge()
                time.sleep(3)
                ensure_edge_and_session()

        exit_code = proc.returncode
        restart_count += 1
        log(f"Bot exited (code {exit_code}). Restart #{restart_count} in 20s...")
        time.sleep(20)


if __name__ == "__main__":
    main()
