#!/usr/bin/env python3
"""Register the development-only Discord /ask command.

Required environment variables:
  DISCORD_APPLICATION_ID
  DISCORD_BOT_TOKEN

Optional:
  DISCORD_GUILD_ID      Register instantly in one test server.
  DISCORD_COMMAND_NAME  Defaults to ``ask``.
"""

import json
import os
import sys
import urllib.error
import urllib.request


API_BASE = "https://discord.com/api/v10"


def main() -> int:
    application_id = os.environ.get("DISCORD_APPLICATION_ID", "").strip()
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    command_name = os.environ.get("DISCORD_COMMAND_NAME", "ask").strip()
    if not application_id or not bot_token:
        print("DISCORD_APPLICATION_ID and DISCORD_BOT_TOKEN are required", file=sys.stderr)
        return 2

    if guild_id:
        endpoint = f"{API_BASE}/applications/{application_id}/guilds/{guild_id}/commands"
    else:
        endpoint = f"{API_BASE}/applications/{application_id}/commands"

    body = json.dumps(
        {
            "name": command_name,
            "type": 1,
            "description": "Ask the offline edge knowledge assistant",
            "options": [
                {
                    "name": "question",
                    "description": "Question to send through the edge RAG and model pipeline",
                    "type": 3,
                    "required": True,
                    "max_length": 1000,
                }
            ],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "edge-inference-at-scale/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        detail = exc.read().decode("utf-8") if isinstance(exc, urllib.error.HTTPError) else str(exc)
        print(f"Discord command registration failed: {detail}", file=sys.stderr)
        return 1

    scope = f"guild {guild_id}" if guild_id else "global"
    print(f"Registered /{result.get('name', command_name)} ({scope})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
