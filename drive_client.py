"""Google Drive client: upload captured PDFs and return public shareable links.

Two auth modes are supported:

1. OAuth user delegation (recommended for free personal Google accounts).
   Uploads files as the user, so files belong to the user's 15GB quota.
   Required env vars: GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
   GOOGLE_OAUTH_REFRESH_TOKEN.

2. Service account (only works with Workspace Shared Drives — personal
   Drive folders fail with 'storageQuotaExceeded' because service accounts
   have zero storage quota).
"""

from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import dataclass
from typing import Optional

from google.oauth2.credentials import Credentials as OAuthCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

log = logging.getLogger(__name__)

# Drive scope only — kept separate from Sheets scope so the principle of least
# privilege is preserved per client.
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_TOKEN_URI = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class DriveUpload:
    """Result of a successful Drive upload."""
    file_id: str
    web_view_link: str    # human-friendly preview page (drive.google.com/file/d/.../view)
    thumbnail_url: str    # public thumbnail (works in =IMAGE() once anyone-with-link is set)


class DriveClient:
    """Uploads a PDF to a Drive folder, sets anyone-with-link viewer access,
    and returns the shareable webViewLink URL.

    Pass *either* OAuth credentials (preferred for personal accounts) *or*
    service-account credentials (Workspace Shared Drives only).
    """

    def __init__(
        self,
        *,
        folder_id: str,
        # OAuth (user-delegated) — preferred for personal Google accounts.
        oauth_client_id: Optional[str] = None,
        oauth_client_secret: Optional[str] = None,
        oauth_refresh_token: Optional[str] = None,
        # Service account — only usable with Workspace Shared Drives.
        service_account_file: Optional[str] = None,
        service_account_json: Optional[str] = None,
        service_account_json_b64: Optional[str] = None,
    ):
        if not folder_id:
            raise RuntimeError("DriveClient requires a folder_id.")

        creds = None
        self._is_oauth = False

        # Prefer OAuth if a complete trio is supplied.
        if oauth_client_id and oauth_client_secret and oauth_refresh_token:
            creds = OAuthCredentials(
                token=None,
                refresh_token=oauth_refresh_token,
                token_uri=_TOKEN_URI,
                client_id=oauth_client_id,
                client_secret=oauth_client_secret,
                scopes=_SCOPES,
            )
            self._is_oauth = True
        elif service_account_json_b64:
            raw = base64.b64decode(service_account_json_b64.strip()).decode("utf-8")
            info = json.loads(raw)
            creds = ServiceAccountCredentials.from_service_account_info(info, scopes=_SCOPES)
        elif service_account_json:
            info = json.loads(service_account_json)
            creds = ServiceAccountCredentials.from_service_account_info(info, scopes=_SCOPES)
        elif service_account_file:
            creds = ServiceAccountCredentials.from_service_account_file(service_account_file, scopes=_SCOPES)
        else:
            raise RuntimeError(
                "DriveClient needs either OAuth (client_id/client_secret/refresh_token) "
                "or service-account credentials."
            )

        # cache_discovery=False avoids a noisy warning on Railway's read-only-ish FS.
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._folder_id = folder_id
        log.info(
            "DriveClient ready — auth=%s, folder=%s",
            "oauth" if self._is_oauth else "service-account",
            folder_id,
        )

    # ----------------------------------------------------------------------------------

    def upload_pdf(self, *, file_bytes: bytes, file_name: str) -> DriveUpload:
        """Upload PDF bytes to the configured Drive folder and return links.

        - Sets file permission to anyone-with-link 'reader' so the URL works without login.
        - Returns a DriveUpload with the file id, viewer URL, and thumbnail URL
          (the thumbnail URL is suitable for =IMAGE() in Google Sheets).

        Raises on failure — caller decides whether to fall back.
        """
        media = MediaIoBaseUpload(
            io.BytesIO(file_bytes),
            mimetype="application/pdf",
            resumable=False,
        )
        metadata = {
            "name": file_name,
            "parents": [self._folder_id],
        }
        created = (
            self._svc.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        file_id = created["id"]
        view_link = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

        # Make it viewable by anyone with the link — no login required.
        try:
            self._svc.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                supportsAllDrives=True,
            ).execute()
        except Exception as e:
            log.warning("Could not set anyone-with-link permission on %s: %s", file_name, e)

        # The /thumbnail endpoint works for anyone-with-link readable files and
        # renders an image even from a PDF (first-page preview, ~640px).
        thumbnail_url = f"https://drive.google.com/thumbnail?id={file_id}&sz=w640"

        return DriveUpload(
            file_id=file_id,
            web_view_link=view_link,
            thumbnail_url=thumbnail_url,
        )
