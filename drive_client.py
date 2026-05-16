"""Google Drive client: upload captured PDFs and return public shareable links."""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

log = logging.getLogger(__name__)

# Drive scope only — kept separate from Sheets scope so the principle of least
# privilege is preserved per client.
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class DriveClient:
    """Uploads a PDF to a Drive folder, sets anyone-with-link viewer access,
    and returns the shareable webViewLink URL.

    The folder must already exist and be shared with the service account as Editor.
    """

    def __init__(
        self,
        *,
        folder_id: str,
        service_account_file: Optional[str] = None,
        service_account_json: Optional[str] = None,
        service_account_json_b64: Optional[str] = None,
    ):
        if not folder_id:
            raise RuntimeError("DriveClient requires a folder_id.")

        if service_account_json_b64:
            raw = base64.b64decode(service_account_json_b64.strip()).decode("utf-8")
            info = json.loads(raw)
            creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        elif service_account_json:
            info = json.loads(service_account_json)
            creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        elif service_account_file:
            creds = Credentials.from_service_account_file(service_account_file, scopes=_SCOPES)
        else:
            raise RuntimeError(
                "DriveClient needs service_account_json_b64, service_account_json, or service_account_file."
            )

        # cache_discovery=False avoids a noisy warning on Railway's read-only-ish FS.
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._folder_id = folder_id

    # ----------------------------------------------------------------------------------

    def upload_pdf(self, *, file_bytes: bytes, file_name: str) -> str:
        """Upload PDF bytes to the configured Drive folder and return a shareable link.

        Behavior:
        - Sets file permission to anyone-with-link 'reader' so the URL works without login.
        - Returns the file's webViewLink (the human-friendly preview page on drive.google.com).

        If anything goes wrong, raises — caller decides whether to fall back.
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
        link = created.get("webViewLink")

        # Make it viewable by anyone with the link — no Google login required.
        try:
            self._svc.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                supportsAllDrives=True,
            ).execute()
        except Exception as e:
            # Permission set can fail in some Workspace configurations (e.g. external
            # sharing disabled). Log loudly but still return the link — it'll work for
            # anyone inside the domain at minimum.
            log.warning("Could not set anyone-with-link permission on %s: %s", file_name, e)

        if not link:
            # Construct a fallback link from the file ID.
            link = f"https://drive.google.com/file/d/{file_id}/view"

        return link
