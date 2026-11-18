"""Read-only process & host telemetry plus live SVG status badge rendering.

Everything here uses the Python standard library only (no subprocesses, no shell
commands, and no network calls) and exposes strictly aggregate/runtime metrics
safe for public status and bench views.
"""

from __future__ import annotations

import html
import os
import platform
import resource
import sys
from pathlib import Path
from typing import Any


def _read_rss_mb() -> float:
    """Return current process resident set size (RSS) in MiB."""
    proc_status = Path("/proc/self/status")
    try:
        if proc_status.is_file():
            for line in proc_status.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return round(int(parts[1]) / 1024.0, 1)
    except OSError:
        pass

    try:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports ru_maxrss in KiB; macOS/BSD in bytes.
        if sys.platform == "darwin":
            return round(usage / (1024.0 * 1024.0), 1)
        return round(usage / 1024.0, 1)
    except Exception:
        return 0.0


def collect_host_telemetry() -> dict[str, Any]:
    """Safe, non-identifying runtime and host load snapshot."""
    load_1m = 0.0
    load_5m = 0.0
    load_15m = 0.0
    try:
        l1, l5, l15 = os.getloadavg()
        load_1m = round(float(l1), 2)
        load_5m = round(float(l5), 2)
        load_15m = round(float(l15), 2)
    except (AttributeError, OSError):
        pass

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return {
        "rss_mb": _read_rss_mb(),
        "cpu_cores": os.cpu_count() or 1,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "load_15m": load_15m,
        "arch": platform.machine() or "unknown",
        "python": py_ver,
        "python_version": py_ver,
    }


_BADGE_THEMES: dict[str, tuple[str, str, str]] = {
    "operational": ("#4fd69c", "#102820", "OPERATIONAL"),
    "failover": ("#ffb23d", "#2a1d08", "FAILOVER"),
    "degraded": ("#ffc861", "#2a210c", "DEGRADED"),
    "outage": ("#ff6f52", "#2b120d", "OFFLINE"),
}


def render_status_badge_svg(
    status: str,
    *,
    failover_active: bool = False,
    detail: str = "",
) -> str:
    """Render a crisp, hardware-styled SVG status badge for READMEs and dashboards."""
    key = "failover" if (failover_active and status == "operational") else status.lower()
    dot_color, right_bg, word = _BADGE_THEMES.get(key, ("#8d929c", "#1b1f26", "UNKNOWN"))

    right_text = f"{word} · {detail}".strip(" ·") if detail else word
    left_text = "CONFIH"

    # Approximate monospace character width at 10px font-size + letter-spacing
    left_w = 72
    right_w = max(96, int(len(right_text) * 6.8 + 28))
    total_w = left_w + right_w

    esc_left = html.escape(left_text)
    esc_right = html.escape(right_text)
    aria = html.escape(f"Confih Gateway: {right_text}")

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="24" '
        f'viewBox="0 0 {total_w} 24" role="img" aria-label="{aria}">'
        f"<title>{aria}</title>"
        f'<rect width="{total_w}" height="24" rx="4" fill="#121418"/>'
        f'<path d="M{left_w} 0h{right_w - 4}a4 4 0 0 1 4 4v16a4 4 0 0 1-4 4H{left_w}z" fill="{right_bg}"/>'
        f'<rect x="0.5" y="0.5" width="{total_w - 1}" height="23" rx="3.5" fill="none" stroke="rgba(255,255,255,0.14)"/>'
        f'<line x1="{left_w}" y1="1" x2="{left_w}" y2="23" stroke="rgba(255,255,255,0.1)"/>'
        f'<text x="{left_w // 2}" y="15.5" fill="#efede9" text-anchor="middle" '
        f'font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" font-size="10" font-weight="600" letter-spacing="1.2">{esc_left}</text>'
        f'<circle cx="{left_w + 13}" cy="12" r="3.5" fill="{dot_color}"/>'
        f'<text x="{left_w + 23}" y="15.5" fill="{dot_color}" '
        f'font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" font-size="10" font-weight="600" letter-spacing="0.8">{esc_right}</text>'
        f"</svg>"
    )
