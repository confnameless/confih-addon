#!/usr/bin/env python3
"""Log in to Qobuz and store a fresh user_auth_token.

Qobuz browser tokens expire. This script sends the same MD5(password) login the
Qobuz web player uses, writes the returned token to the addon's token file, and
optionally updates .env for you.

    python3 scripts/qobuz_login.py --email you@example.com
    python3 scripts/qobuz_login.py --update-env

The password is read with getpass when not supplied on the command line. On a
server, prefer setting QOBUZ_EMAIL and QOBUZ_PASSWORD in .env and running the
script without arguments, so the password never enters shell history.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.qobuz import QobuzClient, QobuzError  # noqa: E402


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="Qobuz account e-mail (default: QOBUZ_EMAIL)")
    parser.add_argument("--password", help="Qobuz password (prefer getpass or .env)")
    parser.add_argument(
        "--password-md5",
        help="MD5 hex digest of the password instead of the password itself",
    )
    parser.add_argument(
        "--update-env",
        action="store_true",
        help="write the new token and user id into .env",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print the user object Qobuz returns (subscription details)",
    )
    return parser.parse_args()


def _update_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    for index, line in enumerate(lines):
        match = re.match(r"^([A-Za-z0-9_]+)=", line)
        if match and match.group(1) in pending:
            key = match.group(1)
            lines[index] = f"{key}={pending.pop(key)}"
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{key}={value}" for key, value in pending.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _run(args: argparse.Namespace) -> int:
    overrides: dict[str, object] = {
        "qobuz_self_check": False,
        "qobuz_token_check_interval_seconds": 0,
    }
    if args.email:
        overrides["qobuz_email"] = args.email
    if args.password:
        overrides["qobuz_password"] = args.password
    if args.password_md5:
        overrides["qobuz_password_md5"] = args.password_md5

    settings = Settings(_env_file=ROOT / ".env", **overrides)
    if not settings.qobuz_email:
        print("error: no Qobuz e-mail. Pass --email or set QOBUZ_EMAIL.", file=sys.stderr)
        return 2
    if not settings.qobuz_password and not settings.qobuz_password_md5:
        password = getpass.getpass("Qobuz password: ")
        settings = settings.model_copy(update={"qobuz_password": password})
    if not settings.qobuz_app_secret:
        print(
            "error: QOBUZ_APP_SECRET is empty. Set the supplied app secret, or run "
            "scripts/qobuz_app_credentials.py.",
            file=sys.stderr,
        )
        return 2

    client = QobuzClient(settings)
    await client.start()
    try:
        data = await client.login_with_password()
    except QobuzError as exc:
        print(f"login failed: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await client.stop()

    token = str(data.get("user_auth_token") or "").strip()
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    user_id = str(user.get("id") or settings.qobuz_user_id or "").strip()
    if not token:
        print("login failed: Qobuz returned no token", file=sys.stderr)
        return 1

    print("Login succeeded.")
    print()
    print(f"QOBUZ_USER_AUTH_TOKEN={token}")
    if user_id:
        print(f"QOBUZ_USER_ID={user_id}")
    print()

    if args.verbose and user:
        print("user object:")
        import json

        print(json.dumps(user, indent=2, ensure_ascii=False))
        print()

    print(f"Token file: {settings.token_path}")
    if args.update_env:
        values = {"QOBUZ_USER_AUTH_TOKEN": token}
        if user_id:
            values["QOBUZ_USER_ID"] = user_id
        env_path = ROOT / ".env"
        _update_env(env_path, values)
        print(f"Updated:   {env_path}")
    else:
        print("Add the lines above to .env, or re-run with --update-env.")
    return 0


def main() -> int:
    return asyncio.run(_run(_args()))


if __name__ == "__main__":
    raise SystemExit(main())
