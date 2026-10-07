"""Miro side: drop each campaign as a card into your Campaign Bank.

If the board has a box with GREAT, GOOD and BAD headings inside it (your Campaign Bank),
cards go under those headings, newest at the top. When a column runs out of room, its
oldest cards move to Archive frames off to the side, so nothing gets deleted.

If the board has no such box, the bot makes its own Great / Good / Bad frames instead.
"""

import html
import logging
import re
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("campaign-scout")

API = "https://api.miro.com/v2"

ORDER = ["Great", "Good", "Bad"]
TIER_STYLE = {
    "Great": {"emoji": "🟢", "fill": "#E6F7EE", "card": "#12B76A"},
    "Good":  {"emoji": "🟡", "fill": "#FEF6E6", "card": "#F79009"},
    "Bad":   {"emoji": "🔴", "fill": "#FDEDEC", "card": "#F04438"},
}

# Frames (used for the Archive, or as the main columns when there's no Campaign Bank)
CARD_W = 460          # card width inside a frame
COLS = 2              # cards per row inside a frame
GAP = 40              # space between cards / frame edge
ROW_H = 380           # vertical space per card row
TOP_PAD = 80          # space under the frame title
FRAME_W = COLS * CARD_W + (COLS + 1) * GAP
FRAME_H = 4000        # starting height; frames grow when they fill up
GROW_BY = 4000
FRAME_GAP = 200       # space between frames
BOARD_MARGIN = 800    # space between your existing board content and new frames

# Campaign Bank columns
BANK_PAD_X = 150          # space between a column's edge and its cards
BANK_PAD_TOP = 150        # space under the GREAT / GOOD / BAD headings
BANK_PAD_BOTTOM = 150     # space above the bottom of the box
BANK_CARD_TARGET_W = 1000
BANK_GAP = 100
EST_CARD_H = 220          # used only until Miro tells us a card's real height

MSG_ID_RE = re.compile(r"Discord msg (\d+)")
TITLE_SEP = " | "         # card title = "<campaign> | <pay>"


class MiroError(RuntimeError):
    pass


def frame_title(tier: str, archive: bool) -> str:
    s = TIER_STYLE[tier]
    return f"{s['emoji']} {tier.upper()} — Archive" if archive else f"{s['emoji']} {tier.upper()} Campaigns"


# --------------------------------------------------------------------------- #
# Geometry helpers (Miro positions are item centers)
# --------------------------------------------------------------------------- #

@dataclass
class Rect:
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom


def _center(it: dict) -> tuple[float, float]:
    p = it.get("position") or {}
    return float(p.get("x", 0)), float(p.get("y", 0))


def _has_geo(it: dict) -> bool:
    return "x" in (it.get("position") or {}) and "width" in (it.get("geometry") or {})


def _height(it: dict, default: float = EST_CARD_H) -> float:
    return float((it.get("geometry") or {}).get("height") or default)


def _rect(it: dict) -> Rect:
    x, y = _center(it)
    g = it.get("geometry") or {}
    w = float(g.get("width", 0))
    h = float(g.get("height") or w * 0.43)
    return Rect(x - w / 2, y - h / 2, x + w / 2, y + h / 2)


def _plain(content: str) -> str:
    text = re.sub(r"<[^>]+>", " ", content or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _key(title: str) -> str:
    """Normalized campaign name (pay part dropped) used to skip duplicates."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").split(TITLE_SEP)[0].lower())


def find_bank(items: list[dict]) -> dict | None:
    """Find the smallest box that contains GREAT, GOOD and BAD headings; split it into 3 columns."""
    heads: dict[str, list[dict]] = {t: [] for t in ORDER}
    for it in items:
        if it.get("parent") or it.get("type") not in ("text", "shape", "sticky_note") or not _has_geo(it):
            continue
        word = _plain((it.get("data") or {}).get("content") or "").title()
        if word in heads:
            heads[word].append(it)
    if not all(heads.values()):
        return None

    best = None
    for box in items:
        if box.get("parent") or box.get("type") != "shape" or not _has_geo(box):
            continue
        r = _rect(box)
        inside = {t: [h for h in hs if h["id"] != box["id"] and r.contains(*_center(h))] for t, hs in heads.items()}
        if all(inside.values()):
            area = r.width * r.height
            if best is None or area < best[0]:
                best = (area, r, {t: v[0] for t, v in inside.items()})
    if not best:
        return None

    _, box, chosen = best
    xs = {t: _center(h)[0] for t, h in chosen.items()}
    third = box.width / 3
    slot = {t: min(2, max(0, int((xs[t] - box.left) // third))) for t in ORDER}
    if len(set(slot.values())) == 3:   # one heading per third: equal-width columns
        bounds = {t: (box.left + slot[t] * third, box.left + (slot[t] + 1) * third) for t in ORDER}
    else:                              # otherwise split halfway between headings
        o = sorted(ORDER, key=lambda t: xs[t])
        edges = [box.left, (xs[o[0]] + xs[o[1]]) / 2, (xs[o[1]] + xs[o[2]]) / 2, box.right]
        bounds = {t: (edges[i], edges[i + 1]) for i, t in enumerate(o)}

    top = max(_rect(h).bottom for h in chosen.values()) + BANK_PAD_TOP
    bottom = box.bottom - BANK_PAD_BOTTOM
    if bottom - top < 300:
        return None
    return {"box": box, "cols": {t: Rect(l, top, r, bottom) for t, (l, r) in bounds.items()}}


def bank_grid(col: Rect) -> tuple[int, float]:
    """Cards per row and card width for a Campaign Bank column."""
    usable = col.width - 2 * BANK_PAD_X
    n = max(1, int((usable + BANK_GAP) // (BANK_CARD_TARGET_W + BANK_GAP)))
    return n, (usable - (n - 1) * BANK_GAP) / n


# --------------------------------------------------------------------------- #
# Board
# --------------------------------------------------------------------------- #

class MiroBoard:
    def __init__(self, token: str, board_id: str, session: requests.Session | None = None):
        self.board_id = board_id.strip()
        # Forgive common paste mistakes: quotes around it, or "Bearer " in front.
        token = token.strip().strip("'\"").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        self.s = session or requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.bank: dict | None = None
        self.frames: dict[str, dict] = {}     # tier -> {"id", "x", "y", "w", "h"}
        self.counts: dict[str, int] = {t: 0 for t in ORDER}   # cards in each frame
        self.title_keys: set[str] = set()
        self.message_ids: set[int] = set()

    @property
    def archive(self) -> bool:
        """True when frames are the overflow Archive (Campaign Bank found)."""
        return self.bank is not None

    # ---------------------------------------------------------------- HTTP --
    def _req(self, method: str, path: str, **kw) -> dict:
        url = f"{API}/boards/{self.board_id}{path}"
        for attempt in range(6):
            r = self.s.request(method, url, timeout=30, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                log.warning("Miro %s on %s %s, retrying in %.0fs", r.status_code, method, path, wait)
                time.sleep(wait)
                continue
            if r.status_code == 401:
                raise MiroError(
                    "Miro rejected MIRO_TOKEN. Use the access token from the popup after "
                    "'Install app and get OAuth token' (not the Client ID or Client secret).")
            if r.status_code in (403, 404) and path == "/items":
                raise MiroError(
                    f"Miro can't open board {self.board_id} ({r.status_code}). Check MIRO_BOARD_ID, and that "
                    "the Miro app is installed on the team that owns this board.")
            if r.status_code >= 400:
                raise MiroError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
            return r.json() if r.content else {}
        raise MiroError(f"{method} {path} kept failing")

    def _items(self, item_type: str | None = None):
        params = {"limit": 50}
        if item_type:
            params["type"] = item_type
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            page = self._req("GET", "/items", params=params)
            yield from page.get("data", [])
            cursor = page.get("cursor")
            if not cursor or not page.get("data"):
                break

    # --------------------------------------------------------------- setup --
    def setup(self) -> None:
        """Find the Campaign Bank (or make frames), and load what's already on the board."""
        items = list(self._items())
        self.bank = find_bank(items)
        self._load_frames(items)
        if not self.bank and len(self.frames) < 3:
            self._create_frames(items)

        frame_tier = {v["id"]: t for t, v in self.frames.items()}
        for card in items:
            if card.get("type") != "card":
                continue
            data = card.get("data") or {}
            self.title_keys.add(_key(data.get("title") or ""))
            m = MSG_ID_RE.search(data.get("description") or "")
            if m:
                self.message_ids.add(int(m.group(1)))
            parent = (card.get("parent") or {}).get("id")
            if parent and str(parent) in frame_tier:
                self.counts[frame_tier[str(parent)]] += 1

        if self.bank:
            in_bank = {t: sum(1 for c in items if c.get("type") == "card" and not c.get("parent")
                              and col.contains(*_center(c))) for t, col in self.bank["cols"].items()}
            log.info("Found your Campaign Bank: %s cards in it (%s), %s archived",
                     sum(in_bank.values()), ", ".join(f"{t} {in_bank[t]}" for t in ORDER),
                     sum(self.counts.values()))
        else:
            log.info("No Campaign Bank (box with GREAT / GOOD / BAD headings) found; using frames. "
                     "%s cards already sorted (%s)",
                     sum(self.counts.values()), ", ".join(f"{t} {self.counts[t]}" for t in ORDER))

    def _load_frames(self, items: list[dict]) -> None:
        titles = {frame_title(t, self.archive): t for t in ORDER}
        for f in items:
            if f.get("type") != "frame":
                continue
            tier = titles.get(((f.get("data") or {}).get("title") or "").strip())
            if tier:
                pos, geo = f.get("position") or {}, f.get("geometry") or {}
                self.frames[tier] = {
                    "id": str(f["id"]),
                    "x": float(pos.get("x", 0)), "y": float(pos.get("y", 0)),
                    "w": float(geo.get("width", FRAME_W)), "h": float(geo.get("height", FRAME_H)),
                }

    def _create_frames(self, items: list[dict]) -> None:
        left, top = self._free_spot(items)
        for i, tier in enumerate(ORDER):
            if tier in self.frames:
                continue
            cx = left + i * (FRAME_W + FRAME_GAP) + FRAME_W / 2
            cy = top + FRAME_H / 2
            f = self._create_frame(tier, cx, cy)
            self.frames[tier] = {"id": str(f["id"]), "x": cx, "y": cy, "w": FRAME_W, "h": FRAME_H}
            log.info("Created frame %s", frame_title(tier, self.archive))

    def _free_spot(self, items: list[dict]) -> tuple[float, float]:
        """Top-left corner to the right of everything already on the board."""
        right, top = None, None
        for it in items:
            if it.get("parent") or it.get("type") == "connector" or not _has_geo(it):
                continue
            r = _rect(it)
            right = r.right if right is None else max(right, r.right)
            top = r.top if top is None else min(top, r.top)
        if right is None:
            return 0.0, 0.0
        if self.bank:  # line the Archive up with the top of the Campaign Bank
            top = self.bank["box"].top
        return right + BOARD_MARGIN, top

    def _create_frame(self, tier: str, cx: float, cy: float) -> dict:
        s = TIER_STYLE[tier]
        body = {
            "data": {"title": frame_title(tier, self.archive), "format": "custom", "type": "freeform"},
            "style": {"fillColor": s["fill"]},
            "position": {"x": cx, "y": cy},
            "geometry": {"width": FRAME_W, "height": FRAME_H},
        }
        try:
            return self._req("POST", "/frames", json=body)
        except MiroError as e:
            log.warning("Frame with color failed (%s); retrying plain", e)
            body.pop("style")
            return self._req("POST", "/frames", json=body)

    # ---------------------------------------------------------- de-dupe ----
    def already_has(self, title: str = "", message_id: int | None = None) -> bool:
        if message_id is not None and message_id in self.message_ids:
            return True
        return bool(title) and _key(title) in self.title_keys

    # ------------------------------------------------------------ cards ----
    def add_card(self, tier: str, title: str, description: str, message_id: int) -> dict:
        data = {"title": title[:250], "description": description}
        style = {"cardTheme": TIER_STYLE[tier]["card"]}
        if self.bank:
            card = self._bank_add(tier, data, style)
        else:
            card = self._frame_add(tier, {"data": data, "style": style})
        self.title_keys.add(_key(title))
        self.message_ids.add(message_id)
        return card

    # Campaign Bank ---------------------------------------------------------
    def _bank_add(self, tier: str, data: dict, style: dict) -> dict:
        col = self.bank["cols"][tier]
        _, card_w = bank_grid(col)
        new = self._req("POST", "/cards", json={
            "data": data, "style": style,
            "position": {"x": col.left + BANK_PAD_X + card_w / 2, "y": col.top + EST_CARD_H / 2},
            "geometry": {"width": card_w},
        })
        self._reflow(tier, new)
        return new

    def _reflow(self, tier: str, new: dict) -> None:
        """Lay out a column newest-first; whatever doesn't fit moves to the Archive."""
        col = self.bank["cols"][tier]
        n_cols, card_w = bank_grid(col)
        new_id = str(new.get("id"))
        cards = [c for c in self._items("card") if not c.get("parent") and col.contains(*_center(c))]
        if not any(str(c["id"]) == new_id for c in cards):
            cards.append(new)
        # Newest first. Miro times are to the second, so break ties with the (increasing) item id.
        cards.sort(key=lambda c: (c.get("createdAt") or "", int(c["id"]) if str(c["id"]).isdigit() else 0),
                   reverse=True)
        cards.sort(key=lambda c: str(c["id"]) != new_id)  # the new card always goes first

        placed, overflow = [], []
        row_top, i = col.top, 0
        while i < len(cards):
            row = cards[i:i + n_cols]
            row_h = max(_height(c) for c in row)
            if i > 0 and row_top + row_h > col.bottom:
                overflow = cards[i:]
                break
            for j, c in enumerate(row):
                x = col.left + BANK_PAD_X + j * (card_w + BANK_GAP) + card_w / 2
                placed.append((c, x, row_top + _height(c) / 2))
            row_top += row_h + BANK_GAP
            i += n_cols

        for c, x, y in placed:
            cx, cy = _center(c)
            if abs(cx - x) > 1 or abs(cy - y) > 1:
                self._req("PATCH", f"/cards/{c['id']}", json={"position": {"x": x, "y": y}})
        for c in overflow:
            self._to_archive(tier, c)

    def _to_archive(self, tier: str, card: dict) -> None:
        x_rel, y_rel = self._next_frame_slot(tier)
        f = self.frames[tier]
        try:
            self._req("PATCH", f"/cards/{card['id']}", json={
                "parent": {"id": f["id"]},
                "position": {"x": x_rel, "y": y_rel},
                "geometry": {"width": CARD_W},
            })
        except MiroError as e:  # can't re-parent: copy it into the Archive, then remove the original
            log.warning("Couldn't move card into the Archive (%s); copying it instead", e)
            d = card.get("data") or {}
            self._post_in_frame(tier, {
                "data": {"title": d.get("title") or "", "description": d.get("description") or ""},
                "style": {"cardTheme": (card.get("style") or {}).get("cardTheme") or TIER_STYLE[tier]["card"]},
            }, x_rel, y_rel)
            self._req("DELETE", f"/cards/{card['id']}")
        self.counts[tier] += 1
        log.info("%s column full: moved '%s' to the Archive", tier, ((card.get("data") or {}).get("title") or "")[:60])

    # Frames ----------------------------------------------------------------
    def _next_frame_slot(self, tier: str) -> tuple[float, float]:
        if tier not in self.frames:  # Archive frames are made the first time they're needed
            self._create_frames(list(self._items()))
        f = self.frames[tier]
        n = self.counts[tier]
        col, row = n % COLS, n // COLS
        x_rel = GAP + col * (CARD_W + GAP) + CARD_W / 2
        y_rel = TOP_PAD + row * ROW_H + ROW_H / 2
        if y_rel + ROW_H / 2 > f["h"] - GAP:
            self._grow(tier)
        return x_rel, y_rel

    def _post_in_frame(self, tier: str, body: dict, x_rel: float, y_rel: float) -> dict:
        f = self.frames[tier]
        body = {**body, "position": {"x": x_rel, "y": y_rel},
                "geometry": {"width": CARD_W}, "parent": {"id": f["id"]}}
        try:
            return self._req("POST", "/cards", json=body)
        except MiroError as e:
            # Some API versions want board coordinates for children; try that before giving up.
            log.warning("Card placement relative to frame failed (%s); retrying with board coordinates", e)
            body["position"] = {"x": f["x"] - f["w"] / 2 + x_rel, "y": f["y"] - f["h"] / 2 + y_rel}
            return self._req("POST", "/cards", json=body)

    def _frame_add(self, tier: str, body: dict) -> dict:
        x_rel, y_rel = self._next_frame_slot(tier)
        card = self._post_in_frame(tier, body, x_rel, y_rel)
        self.counts[tier] += 1
        return card

    def _grow(self, tier: str) -> None:
        """Make a full frame taller without moving its top edge (cards stay put)."""
        f = self.frames[tier]
        top = f["y"] - f["h"] / 2
        new_h = f["h"] + GROW_BY
        new_y = top + new_h / 2
        self._req("PATCH", f"/frames/{f['id']}", json={
            "position": {"x": f["x"], "y": new_y},
            "geometry": {"width": f["w"], "height": new_h},
        })
        f["h"], f["y"] = new_h, new_y
        log.info("Grew %s frame to %.0fpx tall", tier, new_h)
