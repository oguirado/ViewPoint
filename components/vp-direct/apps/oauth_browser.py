#!/usr/bin/python3 -I
"""Electron's per-process BROWSER command for an S1-provisioned browser profile."""

import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session.launch import LOOPBACK, LaunchError, _profile_path


OAUTH_BROWSERS = {
    "google-chrome": "/opt/google/chrome/chrome",
    "brave-browser": "/opt/brave.com/brave/brave",
    "opera": "/usr/lib/aarch64-linux-gnu/opera-stable/opera",
}


def browser_argv(arguments, environment):
    if len(arguments) != 1 or any(character.isspace() or ord(character) < 32 for character in arguments[0]):
        raise ValueError("A single OAuth URL is required")
    url = arguments[0]
    parsed = urlsplit(url)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or "\\" in url or (parsed.scheme != "https"
                               and (parsed.scheme, parsed.hostname) != ("http", LOOPBACK))):
        raise ValueError("Unsupported OAuth URL")
    browser = OAUTH_BROWSERS[environment["VP_DIRECT_OAUTH_BROWSER"]]
    profile = Path(environment["VP_DIRECT_OAUTH_PROFILE"])
    profile = _profile_path(profile.parent, profile.name)
    return browser, f"--user-data-dir={profile}", "--", url


def main():
    try:
        command = browser_argv(sys.argv[1:], os.environ)
        os.execv(command[0], command)
    except (KeyError, ValueError, LaunchError, OSError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
