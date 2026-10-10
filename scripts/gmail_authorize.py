"""Run ONLY on your local computer; never put credentials.json in GitHub."""
from __future__ import annotations

import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CLIENT_FILE = Path("credentials.json")
OUTPUT = Path(".secrets/gmail_token.json")

if not CLIENT_FILE.exists():
    raise SystemExit("Missing credentials.json: download a Google Cloud Desktop OAuth client JSON first.")

flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
if not credentials.refresh_token:
    raise SystemExit("No refresh token obtained. Revoke old app access, re-run with prompt=consent, and verify configuration.")
OUTPUT.parent.mkdir(exist_ok=True)
OUTPUT.write_text(credentials.to_json(), encoding="utf-8")
os.chmod(OUTPUT, 0o600)
print(f"Credentials saved locally to {OUTPUT} (private file, gitignored).")
print("Open the file locally and paste the JSON into GitHub Actions secret GMAIL_OAUTH_TOKEN_JSON.")
print("NEVER paste your token into Codex, chat, commits, README or issue comments.")
