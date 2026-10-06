"""Campaign Scout: watches your Discord feed channel and sorts campaigns onto your Miro board.

Flow: campaign server announcement channel --(Discord "Follow")--> your feed channel
      --> this bot --> Claude pulls out the campaign --> rated by your $ cutoffs --> Miro card
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import anthropic
import discord

from campaigns import Cutoffs, Extractor, card_description, describe_source, message_to_text, rate
from miro_board import MiroBoard

try:  # lets you run it on your own computer with a .env file
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("campaign-scout")
discord.VoiceClient.warn_nacl = False  # no voice features here; skip the warnings
logging.getLogger("discord.client").addFilter(lambda r: "voice will NOT be supported" not in r.getMessage())

MIN_CHARS = 40  # skip tiny messages ("ty", "lfg") without spending a Claude call


def _need(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        sys.exit(f"Missing setting: {name}. Add it to your Railway Variables (or .env).")
    return v


def _number(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip().replace("$", "").replace(",", "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        sys.exit(f"{name} should be a number like 30, got {raw!r}")


def load_settings() -> dict:
    no_pay = os.getenv("NO_PAY_TIER", "Bad").strip().title()
    if no_pay not in ("Bad", "Good", "Great"):
        sys.exit("NO_PAY_TIER must be Bad, Good, or Great")
    d = Cutoffs()
    cut = Cutoffs(
        great_base=_number("GREAT_BASE", d.great_base),
        good_base=_number("GOOD_BASE", d.good_base),
        good_cpm=_number("GOOD_CPM", d.good_cpm),
        great_posts=_number("GREAT_POSTS_PER_DAY", d.great_posts),
        good_posts=_number("GOOD_POSTS_PER_DAY", d.good_posts),
        no_pay_tier=no_pay,
    )
    if cut.great_base < cut.good_base or cut.great_posts < cut.good_posts:
        sys.exit("GREAT cutoffs must be at least as high as GOOD cutoffs")
    ids = {int(x) for x in os.getenv("WATCH_CHANNEL_IDS", "").replace(" ", "").split(",") if x.isdigit()}
    return {
        "discord_token": _need("DISCORD_TOKEN"),
        "anthropic_key": _need("ANTHROPIC_API_KEY"),
        "miro_token": _need("MIRO_TOKEN"),
        "miro_board_id": _need("MIRO_BOARD_ID"),
        "cutoffs": cut,
        "watch_ids": ids,
        "model": os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        "backfill_hours": float(os.getenv("BACKFILL_HOURS", "24")),
    }


class Scout(discord.Client):
    def __init__(self, cfg: dict):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.cfg = cfg
        self.board = MiroBoard(cfg["miro_token"], cfg["miro_board_id"])
        self.extractor = Extractor(anthropic.AsyncAnthropic(api_key=cfg["anthropic_key"]), cfg["model"])
        self.seen: set[int] = set()
        self.board_lock = asyncio.Lock()
        self.ready_once = False

    # ------------------------------------------------------------------ #
    def watching(self, channel) -> bool:
        ids = self.cfg["watch_ids"]
        if ids:
            return channel.id in ids or getattr(channel, "parent_id", None) in ids
        return True

    def watched_channels(self):
        for ch in self.get_all_channels():
            if isinstance(ch, discord.TextChannel) and self.watching(ch):
                if ch.permissions_for(ch.guild.me).read_message_history:
                    yield ch

    # ------------------------------------------------------------------ #
    async def on_ready(self):
        if self.ready_once:  # Discord reconnects fire on_ready again
            return
        self.ready_once = True
        log.info("Logged in as %s", self.user)
        await asyncio.to_thread(self.board.setup)
        channels = list(self.watched_channels())
        log.info("Watching %d channel(s): %s", len(channels), ", ".join(f"#{c.name}" for c in channels) or "none")
        if not channels:
            log.warning("No channels to watch. Is the bot in your feed server with View Channel + Read Message History?")
        await self.backfill(channels)

    async def backfill(self, channels):
        """Catch anything posted while the bot was offline."""
        hours = self.cfg["backfill_hours"]
        if hours <= 0:
            return
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        for ch in channels:
            try:
                async for msg in ch.history(limit=200, after=since, oldest_first=True):
                    await self.handle(msg)
            except discord.HTTPException as e:
                log.warning("Couldn't read history for #%s: %s", ch.name, e)
        log.info("Caught up on the last %g hours", hours)

    async def on_message(self, message: discord.Message):
        if self.ready_once:
            await self.handle(message)

    # ------------------------------------------------------------------ #
    async def handle(self, message: discord.Message):
        if message.author == self.user or message.id in self.seen:
            return
        if not self.watching(message.channel):
            return
        self.seen.add(message.id)
        if self.board.already_has(message_id=message.id):
            return
        text = message_to_text(message)
        if len(text) < MIN_CHARS:
            return

        source = describe_source(message)
        try:
            campaigns = await self.extractor.extract(text, source)
        except anthropic.APIError as e:
            log.error("Claude couldn't read message %s: %s", message.id, e)
            self.seen.discard(message.id)  # let a later restart retry it
            return
        if not campaigns:
            return

        found = message.created_at.astimezone(timezone.utc).strftime("%b %d, %Y")
        for c in campaigns:
            tier, reason = rate(c, self.cfg["cutoffs"])
            async with self.board_lock:
                if self.board.already_has(title=c.title):
                    log.info("Skipping duplicate: %s", c.title)
                    continue
                desc = card_description(c, tier, reason, source, message.id, found)
                try:
                    await asyncio.to_thread(self.board.add_card, tier, c.title, desc, message.id)
                    log.info("Added [%s] %s (%s)", tier, c.title, source.label)
                except Exception as e:  # keep the bot alive if Miro hiccups
                    log.error("Miro wouldn't take '%s': %s", c.title, e)


def main():
    cfg = load_settings()
    Scout(cfg).run(cfg["discord_token"], log_handler=None)


if __name__ == "__main__":
    main()
