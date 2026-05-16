"""Environment-driven configuration for the test-results bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _csv(raw: str) -> List[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass(frozen=True)
class Config:
    discord_token: str
    test_results_category_id: int
    ignore_channel_patterns: List[str]
    anthropic_api_key: str
    claude_model: str
    # Exactly one of these will be populated:
    google_service_account_file: str       # path on disk (local dev)
    google_service_account_json: str       # raw JSON contents (Railway / Fly / Docker secrets)
    google_service_account_json_b64: str   # base64-encoded JSON (bulletproof against env-var mangling)
    spreadsheet_id: str
    master_tab: str
    dashboard_tab: str
    google_drive_folder_id: str   # if set, PDFs are uploaded to this Drive folder & link stored in sheet
    # Optional OAuth user-delegation (preferred for personal Google accounts).
    google_oauth_client_id: str
    google_oauth_client_secret: str
    google_oauth_refresh_token: str
    sweep_day: int
    sweep_hour: int
    state_path: str

    @classmethod
    def from_env(cls) -> "Config":
        required = (
            "DISCORD_TOKEN",
            "TEST_RESULTS_CATEGORY_ID",
            "ANTHROPIC_API_KEY",
            "SPREADSHEET_ID",
        )
        missing = [k for k in required if not os.getenv(k)]

        sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
        sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        sa_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "")
        if not sa_file and not sa_json and not sa_b64:
            missing.append("GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SERVICE_ACCOUNT_JSON, or GOOGLE_SERVICE_ACCOUNT_JSON_B64")

        if missing:
            raise RuntimeError("Missing required env vars: " + ", ".join(missing))

        return cls(
            discord_token=os.environ["DISCORD_TOKEN"],
            test_results_category_id=int(os.environ["TEST_RESULTS_CATEGORY_ID"]),
            ignore_channel_patterns=_csv(os.getenv("IGNORE_CHANNEL_PATTERNS", "")),
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5"),
            google_service_account_file=sa_file,
            google_service_account_json=sa_json,
            google_service_account_json_b64=sa_b64,
            spreadsheet_id=os.environ["SPREADSHEET_ID"],
            master_tab=os.getenv("MASTER_TAB", "All Tests"),
            dashboard_tab=os.getenv("DASHBOARD_TAB", "Dashboard"),
            google_drive_folder_id=os.getenv("GOOGLE_DRIVE_FOLDER_ID", ""),
            google_oauth_client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""),
            google_oauth_client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            google_oauth_refresh_token=os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", ""),
            sweep_day=int(os.getenv("SWEEP_DAY", "0")),
            sweep_hour=int(os.getenv("SWEEP_HOUR", "13")),
            state_path=os.getenv("STATE_PATH", "state.json"),
        )
