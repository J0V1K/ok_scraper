"""VPN-rotating wrapper around scraper.py for sustained backfills.

Runs the scraper as a subprocess. When the scraper exits non-zero AND its
output contains an IP_RESTRICTED marker, the wrapper cycles the macOS
NetworkExtension VPN tunnel (Tier 1: `scutil --nc stop/start`), verifies
the public IP changed, and relaunches the scraper. The scraper's
auto-resume logic skips days already marked complete, so subsequent
launches pick up from where the IP-block hit.

Designed for Hotspot Shield's "Optimal" server selection (where toggling
the tunnel often yields a new IP), but the rotation primitive works for
any NetworkExtension-registered VPN visible to `scutil --nc list`.

Usage:
    python ok_scraper/rotate.py \\
      --start-date 2025-01-01 --end-date 2025-12-31 \\
      --county tulsa --type CJ,CV,CF,CM \\
      --no-filter --no-cap --refresh-on-gate \\
      --data-root /Volumes/Seagate/Oklahoma/2025

All flags after --start-date are forwarded verbatim to scraper.py.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRAPER = Path(__file__).resolve().parent / "scraper.py"
TOGGLE_SCRIPT = Path(__file__).resolve().parent / "_hotspot_toggle.applescript"
DEFAULT_VPN_NAME = "Hotspot Shield VPN (Hydra)"
IP_BLOCK_PATTERN = re.compile(r"IP_RESTRICTED|YOUR IP ADDRESS IS RESTRICTED", re.IGNORECASE)


def public_ip() -> str:
    """Return the current public IPv4 as a string, or empty on failure.

    Uses ipv4.icanhazip.com (forces an A-record endpoint) since OSCN and
    CF track at the IPv4 level; an IPv6 lookup would mask a real v4
    change when the user is dual-stacked.
    """
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "8", "https://ipv4.icanhazip.com"],
            capture_output=True, text=True, check=False,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def cycle_vpn(vpn_name: str, settle_s: float = 6.0) -> None:
    """Toggle Hotspot Shield off/on via GUI (AppleScript).

    Empirically `scutil --nc stop/start` reuses the same exit IP — the
    NEVPNManager toggle doesn't reinvoke Hotspot Shield's
    server-selection logic. Driving the app UI directly DOES yield a
    new exit, so we use osascript to click the in-app Disconnect/Connect
    toggle.

    `vpn_name` is unused on this path but kept for API stability; if a
    user later wires up Tier 3 (IKEv2 profiles) we'll add a branch here.
    """
    if not TOGGLE_SCRIPT.exists():
        raise RuntimeError(f"Toggle script not found at {TOGGLE_SCRIPT}")
    print("[rotate] Toggling Hotspot Shield via AppleScript GUI driver")
    result = subprocess.run(
        ["osascript", str(TOGGLE_SCRIPT)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(f"Toggle script failed (rc={result.returncode}): {err or out}")
    print(f"[rotate] Toggle result: {out}")
    time.sleep(settle_s)


def wait_for_ip_change(prev_ip: str, *, timeout_s: float = 30.0, poll_s: float = 2.0) -> str:
    """Poll ifconfig.me until the public IP differs from prev_ip, or timeout."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = public_ip()
        if last and last != prev_ip:
            return last
        time.sleep(poll_s)
    return last


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_scraper_once(forwarded_args: list[str]) -> tuple[int, bool]:
    """Run scraper.py with the forwarded args; tee stdout to terminal.

    Returns (returncode, ip_block_detected). The scraper exits non-zero
    on IP_RESTRICTED already; we additionally inspect the stream so that
    transient failures unrelated to IP can be distinguished.
    """
    cmd = [sys.executable, "-u", str(SCRAPER), *forwarded_args]
    print(f"[rotate] Launching scraper: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    ip_block = False
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if IP_BLOCK_PATTERN.search(line):
            ip_block = True
    rc = proc.wait()
    return rc, ip_block


def main() -> int:
    parser = argparse.ArgumentParser(
        description="VPN-rotating wrapper around ok_scraper/scraper.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Any extra args are forwarded verbatim to scraper.py.",
    )
    parser.add_argument("--vpn-name", default=DEFAULT_VPN_NAME,
                        help=f"NetworkExtension service name (see `scutil --nc list`). Default: {DEFAULT_VPN_NAME!r}")
    parser.add_argument("--max-rotations", type=int, default=50,
                        help="Stop after this many VPN rotations (safety cap). Default: 50")
    parser.add_argument("--require-ip-change", action="store_true",
                        help="Abort if cycling the tunnel doesn't actually change the public IP. "
                             "Useful for verifying Hotspot Shield's Optimal mode is rotating.")
    parser.add_argument("--cooldown-s", type=float, default=10.0,
                        help="Sleep this long between rotations after the IP is verified changed. Default: 10s.")
    args, forwarded = parser.parse_known_args()

    if not SCRAPER.exists():
        print(f"[rotate] scraper not found at {SCRAPER}", file=sys.stderr)
        return 1

    rotation_count = 0
    sessions: list[dict] = []  # per-session: ip, started_at, returncode, ip_block

    while True:
        ip = public_ip()
        started_at = now_iso()
        print(f"\n[rotate] === Session {len(sessions)+1} (IP: {ip or '<unknown>'}) at {started_at} ===")
        rc, ip_block = run_scraper_once(forwarded)
        sessions.append({"ip": ip, "started_at": started_at, "returncode": rc, "ip_block": ip_block})
        print(f"[rotate] Session ended: returncode={rc} ip_block={ip_block}")

        # Healthy exit (rc=0, no IP block) → backfill complete, stop.
        if rc == 0 and not ip_block:
            print("[rotate] Scraper exited cleanly; backfill complete.")
            break

        # Hard error not from IP block → stop and let the operator look.
        if rc != 0 and not ip_block:
            print(f"[rotate] Scraper exited {rc} without IP_RESTRICTED — not an IP issue, stopping.")
            return rc

        # IP block path: rotate.
        rotation_count += 1
        if rotation_count > args.max_rotations:
            print(f"[rotate] Max rotations ({args.max_rotations}) reached; stopping.")
            return 2

        cycle_vpn(args.vpn_name)
        new_ip = wait_for_ip_change(ip, timeout_s=30.0)
        if new_ip == ip or not new_ip:
            print(f"[rotate] IP did not change after rotation (still {new_ip or '<unknown>'}).")
            if args.require_ip_change:
                print("[rotate] --require-ip-change set; aborting.")
                return 3
            print("[rotate] Continuing anyway — VPN's Optimal mode may have re-handed the same exit.")
        else:
            print(f"[rotate] New IP: {new_ip} (was {ip})")
        time.sleep(args.cooldown_s)

    # Summary
    print(f"\n[rotate] Sessions: {len(sessions)}, rotations: {rotation_count}")
    for i, s in enumerate(sessions, 1):
        print(f"  [{i}] {s['started_at']}  ip={s['ip'] or '?'}  rc={s['returncode']}  ip_block={s['ip_block']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
