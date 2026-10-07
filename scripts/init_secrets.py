#!/usr/bin/env python3
"""Generate .env and Diode OAuth2 client credentials (idempotent, stdlib only).

Existing files are never overwritten, so re-running is safe and secrets stay
stable across restarts. Delete .env and secrets/ to rotate everything.
"""

from __future__ import annotations

import json
import secrets
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
CREDS_FILE = ROOT / "secrets" / "oauth2" / "client-credentials.json"
PLUGIN_SECRET_FILE = ROOT / "secrets" / "netbox" / "netbox_to_diode"

# OAuth2 clients registered in Hydra by diode-auth-bootstrap.
CLIENTS = {
    "diode-ingest": "diode:ingest",  # producers (SDK / Orb agent)
    "diode-to-netbox": "netbox:read netbox:write",  # reconciler -> NetBox plugin
    "netbox-to-diode": "diode:read diode:write",  # NetBox plugin -> Diode
}


def _secret(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def _alnum(n: int) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


def ensure_credentials() -> dict[str, str]:
    if not CREDS_FILE.exists():
        CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        creds = [
            {
                "client_id": cid,
                "client_secret": _secret(),
                "grant_types": ["client_credentials"],
                "scope": scope,
            }
            for cid, scope in CLIENTS.items()
        ]
        CREDS_FILE.write_text(json.dumps(creds, indent=2) + "\n")
        print(f"created {CREDS_FILE.relative_to(ROOT)}")
    creds = {c["client_id"]: c["client_secret"] for c in json.loads(CREDS_FILE.read_text())}
    if not PLUGIN_SECRET_FILE.exists():
        PLUGIN_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        PLUGIN_SECRET_FILE.write_text(creds["netbox-to-diode"])
        print(f"created {PLUGIN_SECRET_FILE.relative_to(ROOT)}")
    # Containers run as non-root users; secrets are mounted read-only.
    for p in (CREDS_FILE, PLUGIN_SECRET_FILE):
        p.chmod(0o644)
    return creds


def ensure_env(creds: dict[str, str]) -> None:
    if ENV_FILE.exists():
        print(".env exists, leaving it untouched")
        return
    lines = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            if value == "CHANGE_ME":
                value = _secret(18)
            elif value == "CHANGE_ME_LONG":
                value = _secret(48)
            elif value.startswith("CHANGE_ME_ALNUM"):
                value = _alnum(int(value.removeprefix("CHANGE_ME_ALNUM")))
            elif key == "DIODE_TO_NETBOX_CLIENT_SECRET":
                value = creds["diode-to-netbox"]
            elif key == "DIODE_INGEST_CLIENT_SECRET":
                value = creds["diode-ingest"]
            line = f"{key}={value}"
        lines.append(line)
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)
    print("created .env")


if __name__ == "__main__":
    ensure_env(ensure_credentials())
