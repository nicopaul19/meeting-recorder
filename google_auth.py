from __future__ import annotations
"""
Google OAuth helpers shared by the calendar watcher and the Google Docs writer.

Design rules (these are what make the calendar connection reliable):
- Never open a browser on its own. Background code only loads and refreshes
  tokens; the interactive consent flow runs only when the user clicks
  "Connect" in the UI.
- Tell a network failure (offline, DNS after wake) apart from a revoked token.
  Network failures are retried; only a revoked or missing token asks the user
  to reconnect.
- Calendar and Docs use separate token files, so adding Docs access never
  invalidates the working calendar token.
"""
import threading
from pathlib import Path

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from config import GOOGLE_CREDENTIALS_PATH, CALENDAR_TOKEN_PATH, DOCS_TOKEN_PATH

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
DOCS_SCOPES = ["https://www.googleapis.com/auth/documents"]

HTTP_TIMEOUT_SECONDS = 20
_lock = threading.Lock()


class AuthRequired(Exception):
    """No usable token: the user has to (re)connect via the browser once."""


class NetworkUnavailable(Exception):
    """Google could not be reached. Retry later; the token is still fine."""


def _load(token_path: Path, scopes: list[str]) -> Credentials:
    if not token_path.exists():
        raise AuthRequired(f"{token_path.name} missing")

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    except Exception as e:  # corrupt / wrong-scope file
        raise AuthRequired(f"{token_path.name} unreadable: {e}")

    if creds.valid:
        return creds

    if not creds.refresh_token:
        raise AuthRequired("token has no refresh_token")

    try:
        creds.refresh(Request())
    except TransportError as e:
        raise NetworkUnavailable(str(e))
    except RefreshError as e:
        # invalid_grant = revoked/expired refresh token -> user must reconnect.
        # Anything else here is usually transient on Google's side.
        if "invalid_grant" in str(e) or "invalid_scope" in str(e) or "unauthorized_client" in str(e):
            raise AuthRequired(str(e))
        raise NetworkUnavailable(str(e))

    token_path.write_text(creds.to_json())
    return creds


def _interactive(token_path: Path, scopes: list[str]) -> Credentials:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not GOOGLE_CREDENTIALS_PATH.exists():
        raise AuthRequired(
            "credentials.json is missing. Download OAuth Desktop credentials from "
            "Google Cloud Console into the meeting-recorder folder."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CREDENTIALS_PATH), scopes)
    try:
        creds = flow.run_local_server(
            port=0, timeout_seconds=300, open_browser=True,
            success_message="Meeting Recorder is connected. You can close this tab.")
    except Exception as e:  # timeout / closed tab / denied consent
        raise AuthRequired(f"Google sign-in wasn't completed ({type(e).__name__}). Try again and click Allow.")
    if not creds:
        raise AuthRequired("Google sign-in was not completed.")
    token_path.write_text(creds.to_json())
    return creds


def _authorized_http(creds: Credentials) -> AuthorizedHttp:
    return AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS))


# ─── Calendar ─────────────────────────────────────────────────

def calendar_service():
    """Build a Calendar client from the stored token. Never opens a browser."""
    with _lock:
        creds = _load(CALENDAR_TOKEN_PATH, CALENDAR_SCOPES)
    return build("calendar", "v3", http=_authorized_http(creds), cache_discovery=False)


def connect_calendar_interactive():
    """User-initiated: open the browser consent screen for Calendar."""
    with _lock:
        _interactive(CALENDAR_TOKEN_PATH, CALENDAR_SCOPES)


# ─── Docs ─────────────────────────────────────────────────────

def docs_service(allow_interactive: bool = False):
    """
    Build a Docs client. With allow_interactive=True (only from a user click),
    a missing token opens the browser once to grant Docs access.
    """
    with _lock:
        try:
            creds = _load(DOCS_TOKEN_PATH, DOCS_SCOPES)
        except AuthRequired:
            if not allow_interactive:
                raise
            creds = _interactive(DOCS_TOKEN_PATH, DOCS_SCOPES)
    return build("docs", "v1", http=_authorized_http(creds), cache_discovery=False)
