#!/usr/bin/env python3
"""Slack self-check — is Neovis's app wired up for the native thinking status?

    uv run python scripts/check_slack.py

Reads the token from ~/.neovis/settings.yaml (what the GUI saved), or from
SLACK_BOT_TOKEN. Reports the granted scopes and whether the native
`assistant.threads.setStatus` indicator will work; tells you exactly what's
missing if it won't.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request

# Scopes Neovis actually uses, and why.
NEEDED = {
    "chat:write": "post and edit replies",
    "im:history": "read your DMs to the bot",
    "reactions:write": "the 👀 / ✅ acknowledgements",
    "files:read": "download voice notes you send",
    "files:write": "upload screenshots back to you",
    "assistant:write": "the NATIVE 'Neovis is thinking…' status",
}


def _token() -> str:
    tok = os.environ.get("SLACK_BOT_TOKEN", "")
    if tok:
        return tok
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from neovis.core.settings import load

        return str(load().get("slack_bot_token") or "")
    except Exception:
        return ""


def _call(method: str, token: str, **params) -> tuple[dict, dict]:
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=urllib.parse.urlencode(params).encode(),
        headers={"Authorization": f"Bearer {token}"},
    )
    import json

    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read()), dict(r.headers)


def main() -> int:
    token = _token()
    if not token:
        print("No bot token. Put it in the app's Slack fields, or set SLACK_BOT_TOKEN.")
        return 1

    body, headers = _call("auth.test", token)
    if not body.get("ok"):
        print(f"❌ Token rejected: {body.get('error')}")
        return 1
    print(f"✅ Connected as '{body.get('user')}' in workspace '{body.get('team')}'")

    granted = {s.strip() for s in (headers.get("x-oauth-scopes") or "").split(",") if s.strip()}
    print("\nScopes:")
    missing = []
    for scope, why in NEEDED.items():
        if scope in granted:
            print(f"  ✅ {scope:18s} {why}")
        else:
            missing.append(scope)
            print(f"  ❌ {scope:18s} {why}   ← MISSING")

    if "assistant:write" in missing:
        print(
            "\nThe native thinking indicator is OFF (Neovis will fall back to an\n"
            "animated placeholder message — everything still works).\n"
            "To turn it on: api.slack.com/apps → your app → Agents & AI Apps →\n"
            "enable it (that adds assistant:write automatically) → Install App →\n"
            "reinstall → paste the bot token back into Neovis if it changed."
        )
    elif not missing:
        print("\n🎉 Everything Neovis needs is granted, including the native status.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
