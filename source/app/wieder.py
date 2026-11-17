"""Wieder eingestellte Angebote erkennen (Issue #23).

Verkäufer stellen Angebote, die nicht verkauft wurden, oft neu ein. Das neue Angebot bekommt bei eBay eine
neue Nummer. Was du ausgeblendet hast, bleibt trotzdem ausgeblendet: DAYS Tage lang erkennt Heuhaufen die
Neueinstellung im selben Gesuch und blendet sie von selbst aus. Sie steht dann unter „Ausgeblendet“ mit dem
Hinweis „Wieder eingestellt“ und lässt sich dort zurückholen.

Erkannt wird nur an Angaben über die Sache, nie über Verkäufer oder Käufer: gleicher Titel und dazu
dasselbe Hauptfoto (die Bildnummer bei eBay) oder derselbe grobe Standort (etwa „22***“) mit denselben
Versandkosten. Gespeichert werden davon nur Prüfwerte (gekürzte SHA-256-Werte), nicht die Angaben selbst.
Holst du ein Angebot zurück, vergisst Heuhaufen auch dessen Prüfwerte.
"""
from __future__ import annotations

import hashlib
import re
import time

from . import db

DAYS = 60
SECONDS = DAYS * 86400


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def title_key(title: str | None) -> str | None:
    """Titel ohne Groß-/Kleinschreibung, Satzzeichen und doppelte Leerzeichen."""
    words = re.findall(r"\w+", (title or "").casefold())
    return _key("titel:" + " ".join(words)) if words else None


def image_key(url: str | None) -> str | None:
    """Nummer des Hauptfotos bei eBay (…/images/g/<Nummer>/s-l500.jpg), unabhängig von Größe und Format."""
    m = re.search(r"/images/g/([^/]+)/", url or "")
    return _key("bild:" + m.group(1)) if m else None


def place_key(item: dict) -> str | None:
    """Grober Standort (so, wie eBay ihn zeigt) mit Land, Versandkosten und „nur Abholung“."""
    place = (item.get("location_text") or "").strip()
    if not place or not item.get("shipping_known"):
        return None
    shipping = float(item.get("shipping") or 0)
    return _key(f"ort:{place}|{item.get('country') or ''}|{shipping:.2f}|{int(bool(item.get('pickup_only')))}")


def fingerprint(item: dict) -> tuple[str | None, str | None, str | None]:
    return title_key(item.get("title")), image_key(item.get("image")), place_key(item)


def remember(search_id: int, item: dict, when: float | None = None) -> None:
    """Ausgeblendetes Angebot vormerken, damit eine Neueinstellung erkannt wird."""
    title, image, place = fingerprint(item)
    if not title or not (image or place):
        return   # nur mit Titel allein wäre die Verwechslungsgefahr zu groß
    db.ex("DELETE FROM relist_marks WHERE search_id = ? AND item_id = ?", (search_id, item["item_id"]))
    db.ex("INSERT INTO relist_marks(search_id, item_id, title_key, image_key, place_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
          (search_id, item["item_id"], title, image, place, when or time.time()))


def matches(search_id: int, item: dict, now: float | None = None) -> bool:
    """Ist das Angebot die Neueinstellung eines ausgeblendeten (in diesem Gesuch, in den letzten DAYS Tagen)?"""
    title, image, place = fingerprint(item)
    if not title:
        return False
    rows = db.q("SELECT image_key, place_key FROM relist_marks WHERE search_id = ? AND title_key = ? AND item_id != ? "
                "AND created_at > ?", (search_id, title, item["item_id"], (now or time.time()) - SECONDS))
    return any((image and r["image_key"] == image) or (place and r["place_key"] == place) for r in rows)


def forget(search_id: int, item: dict) -> None:
    """Zurückgeholt: dieses Angebot und alles, woran es als Neueinstellung erkannt wurde, nicht mehr ausblenden."""
    title, image, place = fingerprint(item)
    db.ex("DELETE FROM relist_marks WHERE search_id = ? AND (item_id = ? OR (title_key = ? AND "
          "((image_key IS NOT NULL AND image_key = ?) OR (place_key IS NOT NULL AND place_key = ?))))",
          (search_id, item["item_id"], title, image, place))


def adopt_existing() -> None:
    """Beim Start: Angebote, die schon vor diesem Update ausgeblendet waren, ebenfalls vormerken."""
    now = time.time()
    rows = db.q("SELECT h.search_id, h.item_id, h.title, h.image, h.hidden_at, i.location_text, i.country, i.shipping, "
                "i.shipping_known, i.pickup_only, i.title AS item_title, i.image AS item_image FROM hidden_items h "
                "LEFT JOIN items i ON i.search_id = h.search_id AND i.item_id = h.item_id "
                "WHERE h.auto = 0 AND h.hidden_at > ? AND NOT EXISTS (SELECT 1 FROM relist_marks m "
                "WHERE m.search_id = h.search_id AND m.item_id = h.item_id)", (now - SECONDS,))
    for r in rows:
        item = {**r, "title": r["item_title"] or r["title"], "image": r["item_image"] or r["image"]}
        remember(r["search_id"], item, r["hidden_at"])


def prune(now: float | None = None) -> None:
    db.ex("DELETE FROM relist_marks WHERE created_at < ?", ((now or time.time()) - SECONDS,))
