"""Extract structured test-result fields from a PDF using Claude's document support."""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

import aiohttp
from anthropic import Anthropic

log = logging.getLogger(__name__)


# These keys define the columns in the master spreadsheet. The bot writes rows in
# this order, and the dashboard formulas reference these column letters.
TEST_RESULT_FIELDS = [
    "compound",          # Active ingredient / peptide / molecule
    "dose_mg",           # Numeric dose if stated (e.g. 10, 80)
    "vial_color",        # If indicated in the PDF or channel naming
    "batch_lot",         # Manufacturer lot / batch ID
    "test_date",         # ISO date if stated
    "lab",               # Testing lab / vendor / source
    "method",            # HPLC, LC-MS, NMR, etc.
    "purity_pct",        # Numeric purity percent (HPLC area %)
    "mass_spec_match",   # "yes" / "no" / "unknown"
    "endotoxin",         # Result string for endotoxin (or "n/a")
    "sterility",         # Sterility test result (or "n/a")
    "appearance",        # Appearance / physical description
    "result",            # "pass" / "fail" / "inconclusive" / ""
    "notes",             # Anything else worth knowing (short)
]


@dataclass
class TestResult:
    fields: Dict[str, str]
    source_channel: str
    source_link: str
    file_name: str

    def to_row(self, columns: list[str]) -> list[str]:
        return [self.fields.get(c, "") for c in columns]


_SYSTEM = """You read a single Certificate of Analysis (COA) or peptide / compound test report PDF and return STRICT JSON describing what the document says.

Rules:
- Only fill a field if the document clearly supports it. Leave it as an empty string if not stated.
- `dose_mg` and `purity_pct` must be plain numbers as strings (no units, no % sign). Examples: "10", "98.4".
- `mass_spec_match` must be exactly "yes", "no", or "unknown".
- `result` must be exactly "pass", "fail", "inconclusive", or "" if the document doesn't draw a conclusion.
- `test_date` should be ISO YYYY-MM-DD if a date is present, else empty.
- `notes` is a single short sentence (≤140 chars) summarising anything else important. No prose elsewhere.
- Do NOT guess. Do NOT fabricate lab names, batch numbers, or values.

Output schema (return ONLY this JSON object, nothing else):

{
  "compound": "",
  "dose_mg": "",
  "vial_color": "",
  "batch_lot": "",
  "test_date": "",
  "lab": "",
  "method": "",
  "purity_pct": "",
  "mass_spec_match": "unknown",
  "endotoxin": "",
  "sterility": "",
  "appearance": "",
  "result": "",
  "notes": ""
}
"""


class PDFExtractor:
    def __init__(self, api_key: str, model: str):
        self._client = Anthropic(api_key=api_key)
        self._model = model

    async def download(self, url: str) -> bytes:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as r:
                r.raise_for_status()
                return await r.read()

    def extract(self, pdf_bytes: bytes, *, hint: Optional[str] = None) -> Dict[str, str]:
        """Sync call — wrap with asyncio.to_thread from the bot."""
        b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
        user_blocks: list[dict[str, Any]] = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64,
                },
            }
        ]
        if hint:
            user_blocks.append({"type": "text", "text": f"Channel hint: {hint}"})
        user_blocks.append(
            {
                "type": "text",
                "text": "Extract the test result fields from this document and return JSON only.",
            }
        )

        resp = self._client.messages.create(
            model=self._model,
            max_tokens=2000,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_blocks}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _safe_parse_json(text) or _empty_record()


def _empty_record() -> Dict[str, str]:
    rec = {k: "" for k in TEST_RESULT_FIELDS}
    rec["mass_spec_match"] = "unknown"
    return rec


def _safe_parse_json(text: str) -> Optional[Dict[str, str]]:
    text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    return {k: str(data.get(k, "")) for k in TEST_RESULT_FIELDS}


# --------------------------------------------------------------------------------------
# Channel-name parsing — used as a hint to Claude AND as a fallback if the PDF is sparse
# --------------------------------------------------------------------------------------

# Pattern matches the screenshot's naming: e.g. "nova-tirz-10mg-black", "nova-tb500-black",
# "nova-cjc-ipa-5mg-orange". Compound may be multi-segment; dose optional.
_CHANNEL_RE = re.compile(
    r"""^
    (?P<vendor>[a-z0-9]+)
    -
    (?P<compound>[a-z0-9-]+?)
    (?:-(?P<dose>\d+)mg)?
    (?:-(?P<color>[a-z]+))?
    $""",
    re.IGNORECASE | re.VERBOSE,
)


def parse_channel_name(name: str) -> Dict[str, str]:
    """Best-effort parse of `nova-{compound}-{dose}mg-{color}` style names."""
    out = {"compound": "", "dose_mg": "", "vial_color": ""}
    m = _CHANNEL_RE.match(name)
    if not m:
        return out
    out["compound"] = (m.group("compound") or "").replace("-", " ").strip()
    out["dose_mg"] = m.group("dose") or ""
    out["color"] = m.group("color") or ""
    out["vial_color"] = m.group("color") or ""
    return {k: v for k, v in out.items() if k in ("compound", "dose_mg", "vial_color")}


def merge_with_channel(extracted: Dict[str, str], channel_hint: Dict[str, str]) -> Dict[str, str]:
    """Use channel-derived data only to fill gaps the PDF didn't cover."""
    merged = dict(extracted)
    for k, v in channel_hint.items():
        if not merged.get(k) and v:
            merged[k] = v
    return merged
