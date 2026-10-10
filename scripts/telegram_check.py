"""Explicit manual connection test; never disclose transport exceptions."""
from jobwatch.notification import send_message

try:
    send_message("NZ Food Job Monitor: Telegram connection test.")
except Exception:
    raise SystemExit("Telegram connection test failed; check repository Secrets and connectivity.") from None
print("Telegram test request sent.")
