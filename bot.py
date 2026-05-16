"""Discord bot that captures test-result PDFs from a category into Google Sheets."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Iterable, List, Optional

import discord
from discord import app_commands
from discord.ext import tasks

from config import Config
from drive_client import DriveClient
from pdf_extractor import (
    PDFExtractor,
    TestResult,
    merge_with_channel,
    parse_channel_name,
)
from sheets_client import MASTER_HEADERS, SheetsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("test-results-bot")


# --------------------------------------------------------------------------------------
# State (dedupe set of processed Discord attachment IDs)
# --------------------------------------------------------------------------------------


class State:
    def __init__(self, path: Path):
        self.path = path
        self.processed: set[int] = set()
        self.last_sweep: Optional[str] = None
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self.processed = set(data.get("processed", []))
                self.last_sweep = data.get("last_sweep")
            except Exception as e:
                log.warning("Could not read state %s: %s", path, e)

    def mark(self, attachment_id: int) -> None:
        self.processed.add(attachment_id)
        self._save()

    def seen(self, attachment_id: int) -> bool:
        return attachment_id in self.processed

    def set_last_sweep(self, iso: str) -> None:
        self.last_sweep = iso
        self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps({
            "processed": sorted(self.processed),
            "last_sweep": self.last_sweep,
        }, indent=2))


# --------------------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------------------


def make_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    return intents


class TestResultsBot(discord.Client):
    def __init__(self, config: Config):
        super().__init__(intents=make_intents())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.extractor = PDFExtractor(api_key=config.anthropic_api_key, model=config.claude_model)
        self.sheets = SheetsClient(
            service_account_file=config.google_service_account_file or None,
            service_account_json=config.google_service_account_json or None,
            service_account_json_b64=config.google_service_account_json_b64 or None,
            spreadsheet_id=config.spreadsheet_id,
            master_tab=config.master_tab,
            dashboard_tab=config.dashboard_tab,
        )
        # Optional Drive uploader — only if a folder ID is configured.
        self.drive: Optional[DriveClient] = None
        if config.google_drive_folder_id:
            try:
                self.drive = DriveClient(
                    folder_id=config.google_drive_folder_id,
                    oauth_client_id=config.google_oauth_client_id or None,
                    oauth_client_secret=config.google_oauth_client_secret or None,
                    oauth_refresh_token=config.google_oauth_refresh_token or None,
                    service_account_file=config.google_service_account_file or None,
                    service_account_json=config.google_service_account_json or None,
                    service_account_json_b64=config.google_service_account_json_b64 or None,
                )
                log.info("Drive uploads enabled — folder %s", config.google_drive_folder_id)
            except Exception as e:
                log.warning("Drive client failed to initialise (%s) — falling back to Discord jump URLs", e)
                self.drive = None
        else:
            log.info("Drive uploads disabled (no GOOGLE_DRIVE_FOLDER_ID set) — using Discord jump URLs")
        # googleapiclient's Drive service isn't thread-safe — serialize uploads
        # with a lock so the ~1-2s upload calls don't crash the interpreter
        # when multiple sweep workers fire concurrently. Claude extraction
        # (the actual bottleneck) stays fully parallel.
        self._drive_lock = asyncio.Lock()
        self.state = State(Path(config.state_path))
        self._register_commands()

    # -- lifecycle ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        await self.tree.sync()
        self.weekly_sweep.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s — watching category %s",
                 self.user, self.config.test_results_category_id)

    # -- live capture ---------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.attachments:
            return
        if not self._is_in_watched_category(message.channel):
            return
        if self._is_ignored_channel(message.channel):
            return
        await self._process_message(message)

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if not self._is_in_watched_category(channel):
            return
        log.info("New channel detected in Test Results category: #%s", getattr(channel, "name", channel.id))
        # Nothing to extract yet — uploads will arrive via on_message.

    # -- slash commands -------------------------------------------------------------

    def _register_commands(self) -> None:
        @self.tree.command(name="sweep_tests", description="Scan all channels in the Test Results category right now.")
        @app_commands.default_permissions(manage_guild=True)
        async def sweep_tests(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                n = await self.run_sweep()
                await interaction.followup.send(
                    f"Sweep complete — {n} new PDF(s) captured into the spreadsheet.",
                    ephemeral=True,
                )
            except Exception as e:
                log.exception("Manual sweep failed")
                await interaction.followup.send(f"Failed: {e}", ephemeral=True)

        @self.tree.command(name="test_status", description="When was the last test-results sweep?")
        async def test_status(interaction: discord.Interaction):
            last = self.state.last_sweep or "never"
            await interaction.response.send_message(
                f"Last sweep: **{last}** UTC. Weekly sweep: day {self.config.sweep_day} at "
                f"{self.config.sweep_hour:02d}:00 UTC.",
                ephemeral=True,
            )

        @self.tree.command(
            name="diagnose",
            description="Show what the bot can see — channels in the category, PDF counts, permissions.",
        )
        @app_commands.default_permissions(manage_guild=True)
        async def diagnose(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)
            lines: list[str] = []
            lines.append(f"**Configured category ID:** `{self.config.test_results_category_id}`")
            lines.append(f"**Ignore patterns:** `{self.config.ignore_channel_patterns or '(none)'}`")
            lines.append("")

            # Walk every guild the bot is in and look up the category.
            found_any_category = False
            for guild in self.guilds:
                lines.append(f"**Guild:** {guild.name} ({guild.id})")
                cat = discord.utils.get(guild.categories, id=self.config.test_results_category_id)
                if not cat:
                    # Maybe the ID is a channel, not a category?
                    maybe_channel = guild.get_channel(self.config.test_results_category_id)
                    if maybe_channel is not None:
                        lines.append(
                            f"  ⚠️ ID `{self.config.test_results_category_id}` matches a **channel**, not a category: "
                            f"#{getattr(maybe_channel, 'name', '?')} (type={type(maybe_channel).__name__}). "
                            f"You need the category's ID, not the channel's."
                        )
                    else:
                        lines.append(
                            f"  ❌ No category or channel with ID `{self.config.test_results_category_id}` in this guild."
                        )
                    # List the category IDs we DO see, to help them find the right one.
                    if guild.categories:
                        lines.append("  Categories in this guild:")
                        for c in guild.categories[:15]:
                            lines.append(f"    • `{c.id}` — {c.name}")
                    continue

                found_any_category = True
                lines.append(f"  ✅ Found category: **{cat.name}** with {len(cat.channels)} channel(s)")
                total_pdfs = 0
                for ch in cat.channels:
                    if not isinstance(ch, discord.TextChannel):
                        lines.append(f"  • #{ch.name} — skipped (not a text channel)")
                        continue
                    ignored = any(p.lower() in ch.name.lower() for p in self.config.ignore_channel_patterns)
                    perms = ch.permissions_for(guild.me)
                    can_read = perms.view_channel and perms.read_message_history
                    pdf_count = 0
                    if can_read and not ignored:
                        try:
                            async for msg in ch.history(limit=200):
                                if not msg.attachments:
                                    continue
                                pdf_count += sum(1 for a in msg.attachments if a.filename.lower().endswith(".pdf"))
                        except discord.Forbidden:
                            can_read = False
                    total_pdfs += pdf_count
                    flags = []
                    if ignored: flags.append("IGNORED")
                    if not can_read: flags.append("NO READ PERM")
                    flag_str = f" — {', '.join(flags)}" if flags else ""
                    lines.append(f"  • #{ch.name} → {pdf_count} PDF(s){flag_str}")
                lines.append(f"  **Total PDFs visible in category: {total_pdfs}**")

            if not self.guilds:
                lines.append("⚠️ The bot isn't in any guild. Re-invite using the OAuth URL.")

            msg = "\n".join(lines)
            # Discord caps interaction responses at 2000 chars — chunk if needed.
            if len(msg) > 1900:
                msg = msg[:1900] + "\n…(truncated)"
            await interaction.followup.send(msg, ephemeral=True)

        @self.tree.command(
            name="test_query",
            description="Summarise all stored test results for a compound (or channel).",
        )
        @app_commands.describe(compound="Compound name or channel substring, e.g. 'tirz' or 'bpc157'")
        async def test_query(interaction: discord.Interaction, compound: str):
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                rows = await asyncio.to_thread(self.sheets.query_by_compound, compound)
                embed = _build_summary_embed(compound, rows)
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                log.exception("test_query failed")
                await interaction.followup.send(f"Failed: {e}", ephemeral=True)

    # -- weekly safety-net sweep ----------------------------------------------------

    @tasks.loop(hours=1)
    async def weekly_sweep(self):
        now = dt.datetime.now(dt.timezone.utc)
        if now.weekday() != self.config.sweep_day or now.hour != self.config.sweep_hour:
            return
        if self.state.last_sweep:
            try:
                last = dt.datetime.fromisoformat(self.state.last_sweep)
                if (now - last).total_seconds() < 6 * 3600:
                    return
            except ValueError:
                pass
        log.info("Running weekly category sweep")
        try:
            await self.run_sweep()
        except Exception:
            log.exception("Scheduled sweep failed")

    @weekly_sweep.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()

    # -- core pipeline --------------------------------------------------------------
    #
    # Two entry points:
    #   run_sweep()         — batch backfill, processes PDFs concurrently and
    #                         appends all rows to the sheet in a single API call
    #   _process_message()  — live capture path (on_message); one message at a time
    # Both funnel through _capture_attachment() which is the single source of truth.

    async def run_sweep(self) -> int:
        """Walk every channel in the watched category and ingest any new PDFs.

        Strategy:
          1. Pre-fetch the (channel, filename) dedup set from the sheet ONCE.
          2. Walk every channel concurrently to collect work items
             (each work item = a PDF attachment we haven't seen before).
          3. Process work items in parallel with a semaphore — the Claude API
             call is the bottleneck (10-15s each), so this is a big speedup.
          4. Batch-append all resulting rows to the sheet in one API call.
        """
        channels = [ch for ch in self._category_channels() if not self._is_ignored_channel(ch)]
        log.info("Sweep: scanning %d channel(s) in category", len(channels))

        existing_files = await asyncio.to_thread(self.sheets.existing_files)

        # Phase 1 — collect work items by walking history in every channel.
        work: list[tuple[discord.Message, discord.Attachment]] = []
        for ch in channels:
            try:
                async for msg in ch.history(limit=None, oldest_first=True):
                    if msg.author.bot or not msg.attachments:
                        continue
                    ch_name = getattr(msg.channel, "name", "")
                    for att in msg.attachments:
                        if not att.filename.lower().endswith(".pdf"):
                            continue
                        if self.state.seen(att.id):
                            continue
                        if (ch_name, att.filename) in existing_files:
                            self.state.mark(att.id)
                            continue
                        work.append((msg, att))
            except discord.Forbidden:
                log.warning("No read permission in #%s", ch.name)

        log.info("Sweep: %d new PDF(s) to process", len(work))
        if not work:
            self.state.set_last_sweep(dt.datetime.now(dt.timezone.utc).isoformat())
            return 0

        # Phase 2 — process concurrently.
        sem = asyncio.Semaphore(max(1, self.config.sweep_concurrency))
        progress = {"done": 0, "total": len(work)}

        async def _bound(msg, att):
            async with sem:
                row = await self._capture_attachment(msg, att)
                progress["done"] += 1
                if progress["done"] % 5 == 0 or progress["done"] == progress["total"]:
                    log.info("Sweep progress: %d/%d", progress["done"], progress["total"])
                return row

        results = await asyncio.gather(
            *[_bound(msg, att) for msg, att in work], return_exceptions=False
        )
        rows = [r for r in results if r is not None]

        # Phase 3 — single batched write.
        if rows:
            await asyncio.to_thread(self.sheets.append_rows, rows)
            log.info("Sweep: appended %d row(s) to sheet", len(rows))

        self.state.set_last_sweep(dt.datetime.now(dt.timezone.utc).isoformat())
        return len(rows)

    async def _process_message(self, message: discord.Message) -> int:
        """Live capture path (called from on_message). Processes one message,
        appends one row at a time. Cheap enough to do inline.
        """
        captured = 0
        channel_name = getattr(message.channel, "name", "")
        existing_files: Optional[set] = None  # lazy

        for att in message.attachments:
            if not att.filename.lower().endswith(".pdf"):
                continue
            if self.state.seen(att.id):
                continue
            if existing_files is None:
                existing_files = await asyncio.to_thread(self.sheets.existing_files)
            if (channel_name, att.filename) in existing_files:
                self.state.mark(att.id)
                continue

            row = await self._capture_attachment(message, att)
            if row is not None:
                await asyncio.to_thread(self.sheets.append_row, row)
                captured += 1
        return captured

    async def _capture_attachment(
        self, message: discord.Message, att: discord.Attachment
    ) -> Optional[list[str]]:
        """Download → Claude extract → Drive upload → build sheet row.

        Returns the row to append, or None if processing failed.
        Marks the attachment in state.json on success so future sweeps skip it.
        """
        channel_name = getattr(message.channel, "name", "")
        hint = parse_channel_name(channel_name)
        try:
            pdf_bytes = await self.extractor.download(att.url)
            extracted = await asyncio.to_thread(
                self.extractor.extract,
                pdf_bytes,
                hint=f"channel '{channel_name}', file '{att.filename}'",
            )
            merged = merge_with_channel(extracted, hint)

            # Upload to Drive if enabled; fall back to Discord jump URL on any failure.
            stored_link = message.jump_url
            preview_formula = ""
            if self.drive is not None:
                try:
                    drive_name = _drive_filename(channel_name, att.filename, merged)
                    # Serialize Drive uploads — googleapiclient is NOT thread-safe.
                    async with self._drive_lock:
                        upload = await asyncio.to_thread(
                            self.drive.upload_pdf,
                            file_bytes=pdf_bytes,
                            file_name=drive_name,
                        )
                    stored_link = upload.web_view_link
                    # IMAGE formula renders the PDF's first page in the cell.
                    preview_formula = f'=IMAGE("{upload.thumbnail_url}")'
                except Exception as e:
                    log.warning("Drive upload failed for %s — using jump URL: %s", att.filename, e)

            result = TestResult(
                fields=merged,
                source_channel=channel_name,
                source_link=stored_link,
                file_name=att.filename,
            )
            row = _build_row(result, message.jump_url, preview=preview_formula)
            self.state.mark(att.id)
            log.info("Captured %s from #%s", att.filename, channel_name)
            return row
        except Exception:
            log.exception("Failed to process %s in #%s", att.filename, channel_name)
            return None

    # -- helpers --------------------------------------------------------------------

    def _category_channels(self) -> List[discord.TextChannel]:
        out: List[discord.TextChannel] = []
        for guild in self.guilds:
            cat = discord.utils.get(guild.categories, id=self.config.test_results_category_id)
            if cat:
                out.extend(c for c in cat.channels if isinstance(c, discord.TextChannel))
        return out

    def _is_in_watched_category(self, channel: discord.abc.GuildChannel | discord.abc.Messageable) -> bool:
        cat_id = getattr(channel, "category_id", None)
        return cat_id == self.config.test_results_category_id

    def _is_ignored_channel(self, channel: discord.abc.GuildChannel) -> bool:
        name = getattr(channel, "name", "") or ""
        return any(pat.lower() in name.lower() for pat in self.config.ignore_channel_patterns)


# --------------------------------------------------------------------------------------
# Row assembly
# --------------------------------------------------------------------------------------


def _build_summary_embed(query: str, rows: list[dict]) -> discord.Embed:
    if not rows:
        return discord.Embed(
            title=f"No results for '{query}'",
            description="Nothing in the sheet matches that compound or channel substring yet.",
            color=discord.Color.greyple(),
        )

    # Sort by test_date desc (fall back to captured_at) so "latest" is row 0.
    def _sort_key(r: dict) -> str:
        return r.get("test_date") or r.get("captured_at") or ""

    rows = sorted(rows, key=_sort_key, reverse=True)
    latest = rows[0]

    purities = []
    for r in rows:
        try:
            p = float(r.get("purity_pct", "") or "")
            purities.append(p)
        except ValueError:
            continue
    avg_purity = (sum(purities) / len(purities)) if purities else None

    passes = sum(1 for r in rows if (r.get("result") or "").lower() == "pass")
    fails = sum(1 for r in rows if (r.get("result") or "").lower() == "fail")
    inconc = sum(1 for r in rows if (r.get("result") or "").lower() == "inconclusive")
    unknown = len(rows) - passes - fails - inconc

    dates = [r.get("test_date") for r in rows if r.get("test_date")]
    earliest = min(dates) if dates else "—"
    latest_date = max(dates) if dates else "—"

    embed = discord.Embed(
        title=f"📊 Test summary — '{query}'",
        color=discord.Color.green() if fails == 0 and passes > 0 else discord.Color.blurple(),
    )
    embed.add_field(name="Total tests", value=str(len(rows)), inline=True)
    embed.add_field(name="Avg purity", value=f"{avg_purity:.2f}%" if avg_purity is not None else "—", inline=True)
    embed.add_field(name="Date range", value=f"{earliest} → {latest_date}", inline=True)

    embed.add_field(name="Pass", value=str(passes), inline=True)
    embed.add_field(name="Fail", value=str(fails), inline=True)
    embed.add_field(
        name="Inconclusive / unknown",
        value=f"{inconc} / {unknown}",
        inline=True,
    )

    # Latest test detail block.
    latest_lines = []
    if latest.get("test_date"):   latest_lines.append(f"**Date:** {latest['test_date']}")
    if latest.get("batch_lot"):   latest_lines.append(f"**Lot:** {latest['batch_lot']}")
    if latest.get("lab"):         latest_lines.append(f"**Lab:** {latest['lab']}")
    if latest.get("purity_pct"):  latest_lines.append(f"**Purity:** {latest['purity_pct']}%")
    if latest.get("result"):      latest_lines.append(f"**Result:** {latest['result']}")
    if latest.get("source_link"):
        latest_lines.append(f"[Open PDF]({latest['source_link']})")
    if latest_lines:
        embed.add_field(name="Latest test", value="\n".join(latest_lines), inline=False)

    # Compact list of the 5 most recent rows (lot · purity · result).
    recent = rows[:5]
    recent_lines = []
    for r in recent:
        bits = []
        if r.get("test_date"): bits.append(r["test_date"])
        if r.get("batch_lot"): bits.append(f"lot {r['batch_lot']}")
        if r.get("purity_pct"): bits.append(f"{r['purity_pct']}%")
        if r.get("result"): bits.append(r["result"])
        link = r.get("source_link") or ""
        label = " · ".join(bits) or r.get("file_name", "(test)")
        recent_lines.append(f"• [{label}]({link})" if link else f"• {label}")
    if recent_lines:
        embed.add_field(name=f"Recent tests ({len(recent)} of {len(rows)})", value="\n".join(recent_lines), inline=False)

    return embed


def _drive_filename(channel_name: str, file_name: str, fields: dict) -> str:
    """Build a human-friendly filename for the Drive copy.

    Format: '<channel> — <lot or original> .pdf' if a lot exists, else just the original.
    Keeps the channel as a prefix so Drive's filename sort groups by compound.
    """
    base = file_name.rsplit(".", 1)[0]
    lot = (fields.get("batch_lot") or "").strip()
    parts = [p for p in [channel_name, lot, base] if p]
    # Drive filename length cap is generous (~32k) but keep it readable.
    return " — ".join(parts)[:200] + ".pdf"


def _build_row(result: TestResult, jump_url: str, preview: str = "") -> list[str]:
    captured_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    base = {
        "source_channel": result.source_channel,
        "source_link": result.source_link or jump_url,
        "file_name": result.file_name,
        "captured_at": captured_at,
        "preview": preview,   # =IMAGE(...) formula or empty
    }
    base.update(result.fields)
    return [base.get(h, "") for h in MASTER_HEADERS]


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def main() -> None:
    config = Config.from_env()
    bot = TestResultsBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
