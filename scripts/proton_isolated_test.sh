#!/usr/bin/env bash
#
# Test a Proton VPN WireGuard config without routing the whole machine
# through it.
#
# wireproxy runs entirely in user space: it opens the WireGuard tunnel and
# exposes a local SOCKS5 proxy. Only processes explicitly pointed at that
# proxy use the VPN. No interface, route, DNS or firewall setting on the host
# is touched, and the tunnel is torn down when this script exits.
#
# Usage:
#   scripts/proton_isolated_test.sh /path/to/proton.conf
#   scripts/proton_isolated_test.sh --geo /path/to/proton.conf
#   scripts/proton_isolated_test.sh --qobuz /path/to/proton.conf "daft punk"
#
# --geo checks Qobuz's catalogue anonymously through the tunnel; it does not
# need a subscription. --qobuz runs the full signed-stream check and does.
#
# The config is read, never printed. Keep it outside the repository, for
# example /tmp/opencode/proton.conf, with mode 600.
#
# Requirements: bash, curl, python3, and either wireproxy or Go (for the
# one-time `go install` of wireproxy). The Qobuz checks use .venv/bin/python
# when the project venv exists.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PROTON_TEST_PORT:-25344}"
RUN_GEO=0
RUN_QOBUZ=0
QUERY="daft punk"

usage() {
    cat <<'EOF'
Test a Proton VPN WireGuard config without routing the whole machine through it.

wireproxy runs in user space, opens the WireGuard tunnel and exposes a local
SOCKS5 proxy. Only processes pointed at that proxy use the VPN. No host
interface, route, DNS or firewall setting is changed.

Usage:
  scripts/proton_isolated_test.sh /path/to/proton.conf
  scripts/proton_isolated_test.sh --geo /path/to/proton.conf
  scripts/proton_isolated_test.sh --qobuz /path/to/proton.conf "daft punk"

--geo checks Qobuz's catalogue anonymously through the tunnel; it does not
need a subscription. --qobuz runs the full signed-stream check and does.

The config is read, never printed. Keep it outside the repository, for example
/tmp/opencode/proton.conf, with mode 600.

Requirements: bash, curl, python3, and either wireproxy or Go (for the
one-time `go install` of wireproxy). The Qobuz checks use .venv/bin/python
when the project venv exists.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --geo)
            RUN_GEO=1
            shift
            ;;
        --qobuz)
            RUN_QOBUZ=1
            shift
            ;;
        --)
            shift
            break
            ;;
        -*)
            die "unknown option: $1 (use --help)"
            ;;
        *)
            break
            ;;
    esac
done

CONFIG="${1:-}"
if [[ -z "$CONFIG" ]]; then
    usage >&2
    exit 2
fi
[[ -f "$CONFIG" ]] || die "no such config: $CONFIG"
[[ -r "$CONFIG" ]] || die "config is not readable: $CONFIG"
shift || true
if (($#)); then
    QUERY="$1"
fi

for tool in curl python3; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
PYTHON="${QOBUZ_PYTHON:-$ROOT/.venv/bin/python}"
[[ -x "$PYTHON" ]] || PYTHON=python3

# WireGuard hook directives are not part of a plain Proton client config and
# would be surprising in a tool that promises not to touch host networking.
if grep -Eq '^[[:space:]]*(PreUp|PostUp|PreDown|PostDown|SaveConfig)[[:space:]]*=' "$CONFIG"; then
    die "$CONFIG contains a wg-quick hook; refusing to run it"
fi
grep -q '^\[Interface\]' "$CONFIG" || die "$CONFIG has no [Interface] section"
grep -q '^\[Peer\]' "$CONFIG" || die "$CONFIG has no [Peer] section"
grep -Eq '^[[:space:]]*PrivateKey[[:space:]]*=' "$CONFIG" || die "$CONFIG has no PrivateKey"
grep -Eq '^[[:space:]]*PublicKey[[:space:]]*=' "$CONFIG" || die "$CONFIG has no PublicKey"
grep -Eq '^[[:space:]]*Endpoint[[:space:]]*=' "$CONFIG" || die "$CONFIG has no Endpoint"
if ! grep -Eq '^[[:space:]]*DNS[[:space:]]*=' "$CONFIG"; then
    printf 'warning: %s has no DNS line; hostname lookups through the tunnel may fail\n' "$CONFIG" >&2
fi

BIN="${WIREPROXY_BIN:-}"
if [[ -z "$BIN" ]]; then
    if command -v wireproxy >/dev/null 2>&1; then
        BIN="$(command -v wireproxy)"
    elif [[ -x /tmp/opencode/wireproxy ]]; then
        BIN=/tmp/opencode/wireproxy
    else
        command -v go >/dev/null 2>&1 || die \
            "wireproxy is not installed. Install it from https://github.com/windtf/wireproxy or install Go."
        printf 'installing wireproxy v1.1.3 into /tmp/opencode (one time)...\n' >&2
        mkdir -p /tmp/opencode
        GOBIN=/tmp/opencode go install github.com/windtf/wireproxy/cmd/wireproxy@v1.1.3 >&2
        BIN=/tmp/opencode/wireproxy
    fi
fi
[[ -x "$BIN" ]] || die "wireproxy is not executable: $BIN"

TMP_BASE="${TMPDIR:-/tmp}/opencode"
mkdir -p "$TMP_BASE"
TMP="$(mktemp -d "$TMP_BASE/proton-test.XXXXXX")"
chmod 700 "$TMP"
WP_PID=""
cleanup() {
    if [[ -n "$WP_PID" ]] && kill -0 "$WP_PID" 2>/dev/null; then
        kill "$WP_PID" 2>/dev/null || true
        wait "$WP_PID" 2>/dev/null || true
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

# Copy the config so the generated wireproxy file never has to quote a path
# that may contain spaces, and so the original is never modified.
cp "$CONFIG" "$TMP/proton.conf"
chmod 600 "$TMP/proton.conf"
cat > "$TMP/wireproxy.conf" <<EOF
WGConfig = $TMP/proton.conf

[Socks5]
BindAddress = 127.0.0.1:$PORT
EOF
chmod 600 "$TMP/wireproxy.conf"

printf 'starting userspace tunnel on 127.0.0.1:%s...\n' "$PORT" >&2
"$BIN" -c "$TMP/wireproxy.conf" -s &
WP_PID=$!

READY=0
for _ in $(seq 1 60); do
    if ! kill -0 "$WP_PID" 2>/dev/null; then
        wait "$WP_PID" 2>/dev/null || true
        die "wireproxy exited before the tunnel was ready (check the config and the endpoint)"
    fi
    if curl -4 -fsS --max-time 2 --socks5-hostname "127.0.0.1:$PORT" \
        https://ipinfo.io/ip >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 0.5
done
((READY)) || die "the tunnel did not answer within 30s (Proton may be down, the key may be wrong, or another device may be using the free slot)"

DIRECT_JSON="$(curl -4 -fsS --max-time 15 https://ipinfo.io/json || true)"
PROXIED_JSON="$(curl -4 -fsS --max-time 30 --socks5-hostname "127.0.0.1:$PORT" https://ipinfo.io/json || true)"
[[ -n "$PROXIED_JSON" ]] || die "no JSON came back through the tunnel"

summarize() {
    python3 -c 'import json,sys; d=json.load(sys.stdin); print("{}  {}, {}  {}".format(d.get("ip","?"), d.get("city","?"), d.get("country","?"), d.get("org","?")))'
}

printf '\ndirect:  '
if [[ -n "$DIRECT_JSON" ]]; then
    printf '%s\n' "$DIRECT_JSON" | summarize
else
    printf 'unavailable\n'
fi
printf 'proxied: '
printf '%s\n' "$PROXIED_JSON" | summarize

PROXIED_IP="$(printf '%s\n' "$PROXIED_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ip",""))')"
COUNTRY="$(printf '%s\n' "$PROXIED_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("country",""))')"
DIRECT_IP=""
if [[ -n "$DIRECT_JSON" ]]; then
    DIRECT_IP="$(printf '%s\n' "$DIRECT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ip",""))')"
fi

if [[ -n "$DIRECT_IP" && "$DIRECT_IP" == "$PROXIED_IP" ]]; then
    die "the proxied IP is the same as the direct IP; traffic is not going through the VPN"
fi
case "$COUNTRY" in
    US|NL|JP|PL|RO) ;;
    *) printf 'warning: exit country is %s, not one of the Proton Free countries (US/NL/JP/PL/RO)\n' "${COUNTRY:-unknown}" >&2 ;;
esac

printf '\nOK: the tunnel works and only this test process used it.\n'
printf 'No host interface, route or DNS setting was changed.\n'

if ((RUN_GEO)); then
    printf '\nQobuz catalogue geo check through the tunnel:\n'
    if ! "$PYTHON" -c 'import httpx, pydantic_settings' >/dev/null 2>&1; then
        printf 'skipped: %s has no dependencies. Create the project venv first:\n' "$PYTHON" >&2
        printf '  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n' >&2
    else
        (
            cd "$ROOT"
            QOBUZ_PROXY_URL="socks5h://127.0.0.1:$PORT" "$PYTHON" - <<'PY'
import asyncio
import os

from app.config import Settings
from app.qobuz import QobuzClient


async def main() -> None:
    settings = Settings(
        _env_file=".env",
        qobuz_self_check=False,
        qobuz_token_check_interval_seconds=0,
        qobuz_proxy_url=os.environ["QOBUZ_PROXY_URL"],
    )
    client = QobuzClient(settings)
    await client.start()
    try:
        geo = await client.check_geo(force=True)
        print("qobuz catalogue:", "ok" if geo else "blocked")
        if geo is not True:
            raise SystemExit(1)
    finally:
        await client.stop()


asyncio.run(main())
PY
        )
    fi
fi

if ((RUN_QOBUZ)); then
    printf '\nQobuz check through the tunnel (API and the 64-byte CDN probe):\n'
    if ! "$PYTHON" -c 'import httpx, fastapi, pydantic_settings' >/dev/null 2>&1; then
        printf 'skipped: %s has no dependencies. Use the project venv, for example:\n' "$PYTHON" >&2
        printf '  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n' >&2
        printf '  .venv/bin/python scripts/qobuz_check.py --query %q\n' "$QUERY" >&2
    else
        QOBUZ_PROXY_URL="socks5h://127.0.0.1:$PORT" \
            "$PYTHON" "$ROOT/scripts/qobuz_check.py" --query "$QUERY" --quality max || true
        printf '\n(If only the CDN probe failed, the addon is still fine: in production the phone fetches audio directly from Qobuz, not through Proton.)\n'
    fi
fi
