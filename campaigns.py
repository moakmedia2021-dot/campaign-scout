"""Turn a Discord message into structured campaigns, rate them, and format Miro cards."""

import html
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("campaign-scout")

TIERS = ["Bad", "Good", "Great"]  # worst -> best


# --------------------------------------------------------------------------- #
# Reading a Discord message
# --------------------------------------------------------------------------- #

def _embed_text(embed) -> list[str]:
    parts = []
    if getattr(embed, "author", None) and getattr(embed.author, "name", None):
        parts.append(embed.author.name)
    if getattr(embed, "title", None):
        parts.append(embed.title)
    if getattr(embed, "description", None):
        parts.append(embed.description)
    for f in getattr(embed, "fields", []) or []:
        parts.append(f"{f.name}: {f.value}")
    if getattr(embed, "url", None):
        parts.append(embed.url)
    if getattr(embed, "footer", None) and getattr(embed.footer, "text", None):
        parts.append(embed.footer.text)
    return parts


def message_to_text(message) -> str:
    """All readable text in a message: body, embeds, and any forwarded message inside it."""
    parts = []
    if message.content:
        parts.append(message.content)
    for e in message.embeds or []:
        parts.extend(_embed_text(e))
    for snap in getattr(message, "message_snapshots", None) or []:
        if getattr(snap, "content", None):
            parts.append(snap.content)
        for e in getattr(snap, "embeds", None) or []:
            parts.extend(_embed_text(e))
    return "\n".join(p for p in parts if p).strip()


@dataclass
class Source:
    label: str   # e.g. "Clip Kings #campaigns"
    link: str    # jump link to the original post


def describe_source(message) -> Source:
    """Where the campaign was originally posted, with a link back to it."""
    ref = message.reference
    flags = getattr(message, "flags", None)
    if ref and ref.guild_id and ref.channel_id and ref.message_id:
        link = f"https://discord.com/channels/{ref.guild_id}/{ref.channel_id}/{ref.message_id}"
        if flags is not None and getattr(flags, "is_crossposted", False):
            # Followed announcement channel: the webhook is named "Server Name #channel"
            return Source(label=message.author.name, link=link)
        if getattr(message, "message_snapshots", None):
            return Source(label="Forwarded post", link=link)
    if getattr(message, "webhook_id", None):
        return Source(label=message.author.name, link=message.jump_url)
    return Source(label=f"#{getattr(message.channel, 'name', 'feed')}", link=message.jump_url)


# --------------------------------------------------------------------------- #
# Claude extraction
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You read Discord posts from UGC, clipping, and creator-campaign servers and pull out paid campaign opportunities for a content creator.

Record a campaign only when the post offers creators paid work: UGC videos, clips, posts, or ads for a brand, app, artist, or creator. Ignore everything else: general chat, payout screenshots, leaderboards, rule changes, giveaways, "campaign ended/paused" notices, and reminders about a campaign that adds no new pay or join info. One post can contain several campaigns; record each one.

Field rules:
- Never invent anything. If a detail isn't stated, use null (numbers) or an empty string/list.
- base_pay_usd: guaranteed flat pay per post or per video in US dollars, e.g. "$50 base + CPM", "$40 per video", "flat $30/post". If a range, use the lowest. A monthly or weekly payment is NOT base pay; that's a retainer.
- cpm_usd: US dollars paid per 1,000 views. Convert other forms: "$1 per 1K" = 1, "$0.002 per view" = 2, "$1.50/1k" = 1.5. If a range, use the lowest rate. If the currency isn't USD and you can't convert confidently, use null and explain in pay_details.
- posts_per_day: how many posts per day the campaign allows or asks for. Convert other periods ("14 per week" = 2). If a range like "4-5 daily", use the highest number.
- retainer_usd_monthly: fixed recurring pay converted to a month (weekly x 4).
- pay_details: one short line with the full pay picture in plain words (base, CPM, caps, minimums, bonuses).
- campaign_managers: names or @handles of the people running the campaign or the person to contact.
- where_to_find: concrete steps to join or apply: which channel, link, form, or who to DM.
- links: any join, application, brief, or content-guideline URLs in the post.
- Keep every text field under 200 characters."""

CAMPAIGN_TOOL = {
    "name": "record_campaigns",
    "description": "Record every paid creator campaign found in the post. Pass an empty list if there are none.",
    "input_schema": {
        "type": "object",
        "properties": {
            "campaigns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "brand": {"type": "string", "description": "Brand, app, artist, or creator paying for the content"},
                        "campaign_name": {"type": "string"},
                        "campaign_managers": {"type": "array", "items": {"type": "string"}},
                        "base_pay_usd": {"type": ["number", "null"], "description": "Flat pay per post/video"},
                        "cpm_usd": {"type": ["number", "null"]},
                        "posts_per_day": {"type": ["number", "null"]},
                        "retainer_usd_monthly": {"type": ["number", "null"]},
                        "pay_details": {"type": "string"},
                        "platforms": {"type": "string", "description": "TikTok, IG Reels, YouTube Shorts, etc."},
                        "where_to_find": {"type": "string"},
                        "links": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["brand", "campaign_name", "campaign_managers", "base_pay_usd", "cpm_usd",
                                 "posts_per_day", "retainer_usd_monthly", "pay_details", "where_to_find", "links"],
                },
            }
        },
        "required": ["campaigns"],
    },
}


@dataclass
class Campaign:
    brand: str = ""
    campaign_name: str = ""
    campaign_managers: list[str] = field(default_factory=list)
    base_pay_usd: float | None = None
    cpm_usd: float | None = None
    posts_per_day: float | None = None
    retainer_usd_monthly: float | None = None
    pay_details: str = ""
    platforms: str = ""
    where_to_find: str = ""
    links: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Campaign":
        def num(v):
            try:
                return float(v) if v is not None and str(v).strip() != "" else None
            except (TypeError, ValueError):
                return None

        def strlist(v):
            if isinstance(v, str):
                return [v] if v.strip() else []
            return [str(x) for x in (v or []) if str(x).strip()]

        return cls(
            brand=str(d.get("brand") or "").strip(),
            campaign_name=str(d.get("campaign_name") or "").strip(),
            campaign_managers=strlist(d.get("campaign_managers")),
            base_pay_usd=num(d.get("base_pay_usd")),
            cpm_usd=num(d.get("cpm_usd")),
            posts_per_day=num(d.get("posts_per_day")),
            retainer_usd_monthly=num(d.get("retainer_usd_monthly")),
            pay_details=str(d.get("pay_details") or "").strip(),
            platforms=str(d.get("platforms") or "").strip(),
            where_to_find=str(d.get("where_to_find") or "").strip(),
            links=strlist(d.get("links")),
        )

    @property
    def title(self) -> str:
        b, n = self.brand, self.campaign_name
        if b and n and b.lower() not in n.lower():
            return f"{b} — {n}"
        return n or b or "Untitled campaign"


class Extractor:
    def __init__(self, client, model: str):
        self.client = client  # anthropic.AsyncAnthropic
        self.model = model

    async def extract(self, text: str, source: Source) -> list[Campaign]:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            tools=[CAMPAIGN_TOOL],
            tool_choice={"type": "tool", "name": "record_campaigns"},
            messages=[{
                "role": "user",
                "content": f"Posted in: {source.label}\n\n<post>\n{text[:12000]}\n</post>",
            }],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                raw = (block.input or {}).get("campaigns") or []
                return [Campaign.from_dict(c) for c in raw if isinstance(c, dict)]
        return []


# --------------------------------------------------------------------------- #
# Rating
# --------------------------------------------------------------------------- #

@dataclass
class Cutoffs:
    great_base: float = 50      # $ base per post needed for Great (a CPM must be listed too)
    good_base: float = 30       # $ base per post needed for Good (CPM optional)
    good_cpm: float = 7         # a CPM this high makes a low/no-base campaign Good
    great_posts: float = 4      # posts per day for Great
    good_posts: float = 2       # posts per day for Good
    no_pay_tier: str = "Bad"    # where campaigns with no base and no CPM go


def pay_tier(c: Campaign, cut: Cutoffs) -> str | None:
    base, cpm = c.base_pay_usd, c.cpm_usd
    if base is None and cpm is None:
        return None
    base = base or 0
    if base >= cut.great_base and cpm:
        return "Great"
    if base >= cut.good_base:
        return "Good"
    if cpm is not None and cpm >= cut.good_cpm:
        return "Good"
    return "Bad"


def posting_tier(c: Campaign, cut: Cutoffs) -> str | None:
    p = c.posts_per_day
    if p is None:
        return None
    if p >= cut.great_posts:
        return "Great"
    if p >= cut.good_posts:
        return "Good"
    return "Bad"


def rate(c: Campaign, cut: Cutoffs) -> tuple[str, str]:
    """Tier = the lower of the pay tier and the posting tier. Returns (tier, short reason)."""
    pay = pay_tier(c, cut)
    posting = posting_tier(c, cut)
    pay_used = pay or cut.no_pay_tier
    tiers = [pay_used] + ([posting] if posting else [])
    tier = min(tiers, key=TIERS.index)
    reason = f"Pay: {pay or 'no $ listed'} · Posting: {posting or 'not listed'}"
    return tier, reason


# --------------------------------------------------------------------------- #
# Miro card content
# --------------------------------------------------------------------------- #

def _money(v: float) -> str:
    return f"${v:,.2f}" if v % 1 else f"${v:,.0f}"


def _num(v: float) -> str:
    return f"{v:g}"


def pay_line(c: Campaign) -> str:
    pay = []
    if c.base_pay_usd is not None:
        pay.append(f"{_money(c.base_pay_usd)} base")
    if c.cpm_usd is not None:
        pay.append(f"{_money(c.cpm_usd)} CPM")
    bits = [" + ".join(pay)] if pay else ["No $ listed"]
    if c.posts_per_day is not None:
        bits.append(f"{_num(c.posts_per_day)} post{'s' if c.posts_per_day != 1 else ''}/day")
    if c.retainer_usd_monthly is not None:
        bits.append(f"{_money(c.retainer_usd_monthly)}/mo retainer")
    return " · ".join(bits)


def _clip(s: str, n: int = 220) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def card_description(c: Campaign, tier: str, reason: str, source: Source, message_id: int, found: str) -> str:
    e = html.escape
    lines = [
        f"<p><strong>{e(tier.upper())} · {e(pay_line(c))}</strong></p>",
        f"<p><em>{e(reason)}</em></p>",
    ]
    if c.pay_details:
        lines.append(f"<p>{e(_clip(c.pay_details))}</p>")
    if c.platforms:
        lines.append(f"<p><strong>Platforms:</strong> {e(_clip(c.platforms, 120))}</p>")
    managers = ", ".join(c.campaign_managers) or "Not listed"
    lines.append(f"<p><strong>Managers:</strong> {e(_clip(managers, 160))}</p>")
    where = e(_clip(c.where_to_find)) if c.where_to_find else "See original post"
    link_html = " ".join(
        f'<a href="{e(u, quote=True)}">link{i + 1 if len(c.links) > 1 else ""}</a>'
        for i, u in enumerate(c.links[:4]) if u.startswith(("http://", "https://"))
    )
    lines.append(f"<p><strong>Where:</strong> {where}{' ' + link_html if link_html else ''}</p>")
    lines.append(f'<p><strong>Source:</strong> <a href="{e(source.link, quote=True)}">{e(_clip(source.label, 80))}</a></p>')
    lines.append(f"<p>Found {e(found)} · Discord msg {message_id}</p>")
    return "".join(lines)
