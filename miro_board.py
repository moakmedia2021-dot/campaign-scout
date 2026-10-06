"""Miro side: three frames (Great / Good / Bad) on the board, one card per campaign."""

import logging
import re
import time

import requests

log = logging.getLogger("campaign-scout")

API = "https://api.miro.com/v2"

# Left to right: best first, so the good stuff is the first thing you see.
FRAMES = {
    "Great": {"title": "🟢 GREAT Campaigns", "fill": "#E6F7EE", "card": "#12B76A"},
    "Good":  {"title": "🟡 GOOD Campaigns",  "fill": "#FEF6E6", "card": "#F79009"},
    "Bad":   {"title": "🔴 BAD Campaigns",   "fill": "#FDEDEC", "card": "#F04438"},
}
ORDER = ["Great", "Good", "Bad"]

CARD_W = 460          # card width
COLS = 2              # cards per row inside a frame
GAP = 40              # space between cards / frame edge
ROW_H = 380           # vertical space per card row
TOP_PAD = 80          # space under the frame title
FRAME_W = COLS * CARD_W + (COLS + 1) * GAP
FRAME_H = 4000        # starting height; frames grow when they fill up
GROW_BY = 4000
FRAME_GAP = 200       # space between the three frames
BOARD_MARGIN = 800    # space between your existing board content and the frames

MSG_ID_RE = re.compile(r"Discord msg (\d+)")


class MiroError(RuntimeError):
    pass


class MiroBoard:
    def __init__(self, token: str, board_id: str, session: requests.Session | None = None):
        self.board_id = board_id
        self.s = session or requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.frames: dict[str, dict] = {}     # tier -> {"id", "x", "y", "w", "h"}
        self.counts: dict[str, int] = {t: 0 for t in ORDER}
        self.title_keys: set[str] = set()
        self.message_ids: set[int] = set()

    # ---------------------------------------------------------------- HTTP --
    def _req(self, method: str, path: str, **kw) -> dict:
        url = f"{API}/boards/{self.board_id}{path}"
        for attempt in range(5):
            r = self.s.request(method, url, timeout=30, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                log.warning("Miro %s on %s %s, retrying in %.0fs", r.status_code, method, path, wait)
                time.sleep(wait)
                continue
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
        """Find or create the three frames, and load what's already on the board."""
        existing = {}
        for f in self._items("frame"):
            title = (f.get("data") or {}).get("title") or ""
            for tier, spec in FRAMES.items():
                if title.strip() == spec["title"]:
                    existing[tier] = f

        missing = [t for t in ORDER if t not in existing]
        if missing:
            left, top = self._free_spot()
            for i, tier in enumerate(ORDER):
                if tier in existing:
                    continue
                cx = left + i * (FRAME_W + FRAME_GAP) + FRAME_W / 2
                cy = top + FRAME_H / 2
                existing[tier] = self._create_frame(tier, cx, cy)
                log.info("Created frame %s", FRAMES[tier]["title"])

        for tier, f in existing.items():
            pos, geo = f.get("position") or {}, f.get("geometry") or {}
            self.frames[tier] = {
                "id": str(f["id"]),
                "x": float(pos.get("x", 0)), "y": float(pos.get("y", 0)),
                "w": float(geo.get("width", FRAME_W)), "h": float(geo.get("height", FRAME_H)),
            }

        frame_tier = {v["id"]: t for t, v in self.frames.items()}
        for card in self._items("card"):
            data = card.get("data") or {}
            self.title_keys.add(_key(data.get("title") or ""))
            m = MSG_ID_RE.search(data.get("description") or "")
            if m:
                self.message_ids.add(int(m.group(1)))
            parent = (card.get("parent") or {}).get("id")
            if parent and str(parent) in frame_tier:
                self.counts[frame_tier[str(parent)]] += 1
        log.info("Miro board ready: %s cards already sorted (%s)",
                 sum(self.counts.values()), ", ".join(f"{t} {self.counts[t]}" for t in ORDER))

    def _free_spot(self) -> tuple[float, float]:
        """Top-left corner to the right of everything already on the board."""
        right, top = None, None
        for it in self._items():
            if it.get("parent") or it.get("type") == "connector":
                continue
            pos, geo = it.get("position") or {}, it.get("geometry") or {}
            if "x" not in pos or "width" not in geo:
                continue
            w, h = float(geo.get("width", 0)), float(geo.get("height", geo.get("width", 0)))
            r = float(pos["x"]) + w / 2
            t = float(pos.get("y", 0)) - h / 2
            right = r if right is None else max(right, r)
            top = t if top is None else min(top, t)
        if right is None:
            return 0.0, 0.0
        return right + BOARD_MARGIN, top

    def _create_frame(self, tier: str, cx: float, cy: float) -> dict:
        spec = FRAMES[tier]
        body = {
            "data": {"title": spec["title"], "format": "custom", "type": "freeform"},
            "style": {"fillColor": spec["fill"]},
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
        frame = self.frames[tier]
        n = self.counts[tier]
        col, row = n % COLS, n // COLS
        x_rel = GAP + col * (CARD_W + GAP) + CARD_W / 2
        y_rel = TOP_PAD + row * ROW_H + ROW_H / 2

        if y_rel + ROW_H / 2 > frame["h"] - GAP:
            self._grow(tier)

        body = {
            "data": {"title": title[:250], "description": description},
            "style": {"cardTheme": FRAMES[tier]["card"]},
            "position": {"x": x_rel, "y": y_rel},
            "geometry": {"width": CARD_W},
            "parent": {"id": frame["id"]},
        }
        try:
            card = self._req("POST", "/cards", json=body)
        except MiroError as e:
            # Some API versions want canvas coordinates for children; try that before giving up.
            log.warning("Card placement relative to frame failed (%s); retrying with board coordinates", e)
            body["position"] = {
                "x": frame["x"] - frame["w"] / 2 + x_rel,
                "y": frame["y"] - frame["h"] / 2 + y_rel,
            }
            card = self._req("POST", "/cards", json=body)

        self.counts[tier] += 1
        self.title_keys.add(_key(title))
        self.message_ids.add(message_id)
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


def _key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())
