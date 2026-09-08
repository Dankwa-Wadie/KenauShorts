"""
youtube_auth.py — One-time YouTube OAuth setup for KenauShorts.

Authorizes upload access to the user's YouTube Channel using Google's Desktop OAuth client.
Writes token.json securely.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

LOG = logging.getLogger("kenaushorts.youtube_auth")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def authenticate_youtube(
    client_secret_path: Path,
    token_path: Path,
    port: int = 8080,
    force: bool = False,
    open_browser: bool = True,
) -> bool:
    """Run local server OAuth flow to produce or refresh token.json."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        LOG.error("google-auth-oauthlib not installed. Run: pip install google-auth-oauthlib google-api-python-client")
        return False

    creds = None
    if token_path.exists() and not force:
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if creds and creds.valid:
                LOG.info("Existing YouTube token is valid.")
                return True
            if creds and creds.expired and creds.refresh_token:
                LOG.info("Refreshing expired YouTube token...")
                creds.refresh(Request())
                token_path.write_text(creds.to_json(), encoding="utf-8")
                token_path.chmod(0o600)
                return True
        except Exception as e:
            LOG.warning("Could not refresh existing token: %s", e)

    if not client_secret_path.exists():
        LOG.error("client_secret.json not found at %s", client_secret_path)
        return False

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
        creds = flow.run_local_server(
            port=port,
            open_browser=open_browser,
            prompt="consent",
            authorization_prompt_message="Visit this URL to authorize KenauShorts for YouTube uploads: {url}",
        )
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        token_path.chmod(0o600)
        LOG.info("YouTube authorization successful! Token saved to %s", token_path)
        return True
    except Exception as e:
        LOG.error("YouTube authorization failed: %s", e)
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Authorise YouTube uploads.")
    parser.add_argument("--client-secret", default="client_secret.json")
    parser.add_argument("--token", default="token.json")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    success = authenticate_youtube(
        client_secret_path=Path(args.client_secret).resolve(),
        token_path=Path(args.token).resolve(),
        port=args.port,
        force=args.force,
    )
    sys.exit(0 if success else 1)
