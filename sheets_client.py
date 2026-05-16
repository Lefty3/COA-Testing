"""Google Sheets client: append test-result rows, ensure dashboard KPI formulas exist."""

from __future__ import annotations

import base64
import json
import logging
from typing import List, Optional, Sequence

import gspread
from google.oauth2.service_account import Credentials

from pdf_extractor import TEST_RESULT_FIELDS

log = logging.getLogger(__name__)


# Order matters — columns A..S below correspond to these headers in this exact order.
# IMPORTANT: dashboard formulas reference letter positions of E (compound),
# I (test_date), J (lab), L (purity_pct), Q (result). Don't reorder those columns.
MASTER_HEADERS = (
    [
        "source_channel",   # A
        "source_link",      # B
        "file_name",        # C
        "captured_at",      # D  (ISO timestamp when the bot processed it)
    ]
    + TEST_RESULT_FIELDS   # E..R
    + ["preview"]          # S  — =IMAGE(...) formula showing the PDF thumbnail
)


_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsClient:
    def __init__(
        self,
        *,
        spreadsheet_id: str,
        master_tab: str,
        dashboard_tab: str,
        service_account_file: Optional[str] = None,
        service_account_json: Optional[str] = None,
        service_account_json_b64: Optional[str] = None,
    ):
        if service_account_json_b64:
            # Decode base64 first — bulletproof against env-var \n mangling.
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
                "SheetsClient needs service_account_json_b64, service_account_json, or service_account_file."
            )
        self._gc = gspread.authorize(creds)
        self._sh = self._gc.open_by_key(spreadsheet_id)
        self._master_name = master_tab
        self._dashboard_name = dashboard_tab
        self._master = self._ensure_master_tab()
        self._dashboard = self._ensure_dashboard_tab()

    # -- setup ----------------------------------------------------------------------

    def _ensure_master_tab(self) -> gspread.Worksheet:
        try:
            ws = self._sh.worksheet(self._master_name)
        except gspread.WorksheetNotFound:
            ws = self._sh.add_worksheet(title=self._master_name, rows=2000, cols=len(MASTER_HEADERS))
        # Make sure header row matches. We update if anything differs (handles
        # schema migrations like adding the 'preview' column).
        existing = ws.row_values(1)
        if existing != MASTER_HEADERS:
            ws.update("A1", [MASTER_HEADERS])
            # Bold the header row; A:S covers up to the preview column.
            ws.format("A1:S1", {"textFormat": {"bold": True}})
        return ws

    def _ensure_dashboard_tab(self) -> gspread.Worksheet:
        try:
            ws = self._sh.worksheet(self._dashboard_name)
        except gspread.WorksheetNotFound:
            ws = self._sh.add_worksheet(title=self._dashboard_name, rows=200, cols=12)
        self._install_dashboard(ws)
        return ws

    def _install_dashboard(self, ws: gspread.Worksheet) -> None:
        """Idempotent: writes KPI labels + formulas referencing the master tab."""
        m = f"'{self._master_name}'"
        # Column letters in master tab:
        #   D = captured_at, E = compound, I = test_date, J = lab,
        #   L = purity_pct, Q = result, F = dose_mg
        kpis = [
            ["KPI Dashboard"],
            [""],
            ["Total tests",           f"=COUNTA({m}!C2:C)"],
            ["Unique compounds",      f"=COUNTUNIQUE({m}!E2:E)"],
            ["Average purity (%)",    f"=IFERROR(ROUND(AVERAGE({m}!L2:L),2),0)"],
            ["Pass rate",             f"=IFERROR(COUNTIF({m}!Q2:Q,\"pass\")/COUNTIF({m}!Q2:Q,\"<>\"),0)"],
            ["Failed tests",          f"=COUNTIF({m}!Q2:Q,\"fail\")"],
            ["Inconclusive tests",    f"=COUNTIF({m}!Q2:Q,\"inconclusive\")"],
            ["Tests last 7 days",     f"=COUNTIF({m}!D2:D,\">=\"&TEXT(TODAY()-7,\"yyyy-mm-dd\"))"],
            ["Tests last 30 days",    f"=COUNTIF({m}!D2:D,\">=\"&TEXT(TODAY()-30,\"yyyy-mm-dd\"))"],
            ["Most recent test date", f"=IFERROR(MAX({m}!I2:I),\"-\")"],
        ]
        ws.update("A1", kpis, value_input_option="USER_ENTERED")
        ws.format("A1", {"textFormat": {"bold": True, "fontSize": 14}})
        ws.format("A3:A11", {"textFormat": {"bold": True}})
        ws.format("B6", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})

        # Tests-by-compound (pivot via QUERY).
        ws.update("D1",
                  [["Tests by compound"]], value_input_option="USER_ENTERED")
        ws.format("D1", {"textFormat": {"bold": True}})
        ws.update("D2",
                  [[f"=QUERY({m}!E2:E,\"select E, count(E) where E is not null group by E order by count(E) desc label E 'Compound', count(E) 'Tests'\",0)"]],
                  value_input_option="USER_ENTERED")

        # Tests-by-lab.
        ws.update("G1",
                  [["Tests by lab"]], value_input_option="USER_ENTERED")
        ws.format("G1", {"textFormat": {"bold": True}})
        ws.update("G2",
                  [[f"=QUERY({m}!J2:J,\"select J, count(J) where J is not null group by J order by count(J) desc label J 'Lab', count(J) 'Tests'\",0)"]],
                  value_input_option="USER_ENTERED")

        # Average purity by compound.
        ws.update("J1",
                  [["Avg purity by compound"]], value_input_option="USER_ENTERED")
        ws.format("J1", {"textFormat": {"bold": True}})
        ws.update("J2",
                  [[f"=QUERY({m}!E2:L,\"select E, avg(L) where E is not null group by E order by avg(L) desc label E 'Compound', avg(L) 'Avg %'\",0)"]],
                  value_input_option="USER_ENTERED")

    # -- writes ---------------------------------------------------------------------

    def existing_links(self) -> set[str]:
        """Pull the source_link column so we can skip rows we've already written."""
        values = self._master.col_values(2)  # column B
        return set(v for v in values[1:] if v)

    def existing_files(self) -> set[tuple[str, str]]:
        """Return {(source_channel, file_name)} for every row already in the sheet.

        This is link-format-agnostic — survives swapping source_link between
        Discord CDN URLs, jump URLs, and Drive URLs without producing duplicates.
        """
        values = self._master.get_all_values()
        if not values or len(values) < 2:
            return set()
        # Headers are MASTER_HEADERS — source_channel=col A (idx 0), file_name=col C (idx 2).
        return {(r[0], r[2]) for r in values[1:] if len(r) >= 3 and r[0] and r[2]}

    def append_row(self, row: Sequence[str]) -> None:
        self._master.append_row(list(row), value_input_option="USER_ENTERED")

    def append_rows(self, rows: Sequence[Sequence[str]]) -> None:
        """Batch-append multiple rows in a single API call (much faster for sweeps)."""
        if not rows:
            return
        self._master.append_rows(
            [list(r) for r in rows], value_input_option="USER_ENTERED"
        )

    # -- queries --------------------------------------------------------------------

    def all_rows(self) -> List[dict]:
        """Return every data row as a dict keyed by MASTER_HEADERS."""
        values = self._master.get_all_values()
        if not values or len(values) < 2:
            return []
        header = values[0]
        return [dict(zip(header, row)) for row in values[1:] if any(row)]

    def query_by_compound(self, query: str) -> List[dict]:
        """Case-insensitive substring match against the `compound` and `source_channel` columns."""
        needle = query.strip().lower()
        if not needle:
            return []
        out = []
        for r in self.all_rows():
            compound = (r.get("compound") or "").lower()
            channel = (r.get("source_channel") or "").lower()
            if needle in compound or needle in channel:
                out.append(r)
        return out
