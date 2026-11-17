"""Load dependency locations from private runtime configuration."""
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote


def parse_sources(raw, expected):
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != set(expected) or not data:
        raise ValueError("Unexpected dependency configuration")
    for folder, repository in data.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", folder):
            raise ValueError("Invalid dependency directory")
        if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repository):
            raise ValueError("Invalid repository identifier")
    return data


def checkout_sources(raw, expected, token):
    sources = parse_sources(raw, expected)
    if not token:
        raise ValueError("Missing dependency authorization")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    for folder, repository in sources.items():
        url = f"https://x-access-token:{quote(token, safe='')}@github.com/{repository}.git"
        result = subprocess.run(["git", "clone", "--quiet", url, folder], env=env, capture_output=True, timeout=180)
        if result.returncode:
            # Git's stderr can contain private paths or authenticated URLs.
            raise RuntimeError("Dependency checkout failed")
    return len(sources)


def main():
    try:
        count = checkout_sources(os.environ.get("RUNTIME_SOURCES", ""), sys.argv[1:], os.environ.get("PAT_TOKEN", ""))
    except Exception as exc:
        print(f"::error::Dependency preparation failed ({type(exc).__name__})", flush=True)
        return 1
    print(f"Prepared {count} runtime dependencies", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
