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

    async def run_sweep(self) -> int:
        """Walk every channel in the watched category and ingest any new PDFs."""
        channels = self._category_channels()
        log.info("Sweeping %d channel(s) in category", len(channels))
        count = 0
        for ch in channels:
            if self._is_ignored_channel(ch):
                continue
            try:
                async for msg in ch.history(limit=None, oldest_first=True):
                    if msg.author.bot or not msg.attachments:
                        continue
                    captured = await self._process_message(msg)
                    count += captured
            except discord.Forbidden:
                log.warning("No read permission in #%s", ch.name)
        self.state.set_last_sweep(dt.datetime.now(dt.timezone.utc).isoformat())
        return count

    async def _process_message(self, message: discord.Message) -> int:
        """Returns the number of NEW PDF attachments captured from this message."""
        captured = 0
        channel = message.channel
        channel_name = getattr(channel, "name", "")
        hint = parse_channel_name(channel_name)
        existing_links = None  # lazy-fetch once if needed

        for att in message.attachments:
            if not att.filename.lower().endswith(".pdf"):
                continue
            if self.state.seen(att.id):
                continue

            # Cross-check against the sheet (covers reinstalls where state.json was lost).
            # Check both the CDN URL (old rows) and jump URL (new rows) to avoid duplicates.
            if existing_links is None:
                existing_links = self.sheets.existing_links()
            if att.url in existing_links or message.jump_url in existing_links:
                self.state.mark(att.id)
                continue

            try:
                pdf_bytes = await self.extractor.download(att.url)
                extracted = await asyncio.to_thread(
                    self.extractor.extract,
                    pdf_bytes,
                    hint=f"channel '{channel_name}', file '{att.filename}'",
                )
                merged = merge_with_channel(extracted, hint)
                result = TestResult(
                    fields=merged,
                    source_channel=channel_name,
                    source_link=message.jump_url,  # permanent link, never expires
                    file_name=att.filename,
                )
                row = _build_row(result, message.jump_url)
                await asyncio.to_thread(self.sheets.append_row, row)
                self.state.mark(att.id)
                captured += 1
                log.info("Captured %s from #%s", att.filename, channel_name)
            except Exception:
                log.exception("Failed to process %s in #%s", att.filename, channel_name)
        return captured

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


def _build_row(result: TestResult, jump_url: str) -> list[str]:
    captured_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    base = {
        "source_channel": result.source_channel,
        "source_link": result.source_link or jump_url,
        "file_name": result.file_name,
        "captured_at": captured_at,
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
