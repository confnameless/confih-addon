#!/usr/bin/env python3
"""Container health probe: GET /healthz, including the optional path secret."""

from __future__ import annotations

import os
import sys
import urllib.request

secret = os.environ.get("ADDON_PATH_SECRET", "").strip()
prefix = f"/s/{secret}" if secret else ""
url = os.environ.get("HEALTHCHECK_URL", f"http://127.0.0.1:8000{prefix}/healthz")

try:
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status == 200:
            raise SystemExit(0)
except Exception as exc:
    print(f"healthcheck failed: {exc}", file=sys.stderr)
    raise SystemExit(1) from None
raise SystemExit(1)
