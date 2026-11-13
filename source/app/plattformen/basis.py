"""Was jede Plattform (eBay, später weitere) für Heuhaufen bereitstellt.

Der Kern (watcher.py, market.py, details.py, images.py, main.py) kennt keine Plattform im Einzelnen.
Er spricht nur mit den Methoden von Plattform und bekommt Angebote immer in derselben Form:

* Angebot (dict mit LISTING_FIELDS): Titel, Adresse, Bilder, Preis, Versand, Auktion oder Sofortkauf,
  Ende, Standort, Abholung, Zustand. Fehlt einer Plattform etwas, steht dort None bzw. 0.
* Angaben für die Galerie (dict mit DETAIL_FIELDS): Beschreibung als HTML, Zustandsbeschreibung,
  Artikelmerkmale, Bieter, Mindestgebot, Mindestpreis erreicht. details.py macht daraus reinen Text.

Neue Plattform: Datei in plattformen/ anlegen, Klasse von Plattform ableiten, in plattformen/liste.py
eintragen. Angebotsnummern: eBay-Nummern bleiben, wie sie sind (v1|…|0). Andere Plattformen setzen ihr
Kürzel davor (etsy:12345), damit sich Nummern verschiedener Plattformen nie überschneiden.

Zugangsdaten gehören zur Plattform (credentials) und stehen in den Einstellungen, geheime verschlüsselt
(geheim.py). Abfragen zählt Heuhaufen je Plattform und Zweck (PURPOSES).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# Wofür eine Abfrage war (Einstellungen zeigen die Abfragen danach aufgeschlüsselt). Gilt für alle Plattformen.
PURPOSES = {"suche": "Suche nach neuen Angeboten", "endspurt": "Endspurt vor Auktionsende",
            "erinnerung": "Erinnerung 1 Min vor Ende", "durchschnitt": "Durchschnittspreis",
            "galerie": "Galerie (Beschreibung und Gebote)", "merkmale": "Vorschläge für Artikelmerkmale"}

LISTING_FIELDS = (
    "item_id", "platform", "title", "url", "image", "images", "condition_text", "is_auction", "has_bin",
    "price", "bin_price", "currency", "shipping", "shipping_known", "bid_count", "end_time", "country",
    "location_text", "pickup", "pickup_only", "distance_km", "near", "epid", "cond",
)
# epid: Produkt im Katalog der Plattform (bei eBay die ePID), cond: new | used | parts

DETAIL_FIELDS = ("description_html", "condition_note", "aspects", "bidders", "min_bid", "reserve_met")

# Eine Kategorie aus dem Baum der Plattform (category_tree): id, parent (None = oberste Ebene), name,
# level (1 = oberste Ebene), leaf (keine Unterkategorien), position (Reihenfolge unter den Geschwistern)
CATEGORY_FIELDS = ("id", "parent", "name", "level", "leaf", "position")


RESULT_STEPS = (200, 600, 1000)   # Treffer je Suchlauf höchstens, zur Wahl im Gesuch (Issue #15)
RESULT_DEFAULT = 600              # so viel holte Heuhaufen vor Issue #15 immer (drei Seiten bei eBay)


def result_limit(s: dict, settings: dict) -> int:
    """Wie viele Treffer ein Suchlauf höchstens holt: aus dem Gesuch, sonst aus den Einstellungen."""
    for value in (s.get("max_results"), settings.get("max_results")):
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n in RESULT_STEPS:
            return n
    return RESULT_DEFAULT


def search_categories(s: dict, platform: str) -> list[str]:
    """Kategorien eines Gesuchs auf einer Plattform: searches.categories „ebay:58058,ebay:175672“ → ["58058", …]."""
    out = []
    for key in (s.get("categories") or "").split(","):
        name, _, cid = key.strip().partition(":")
        if name == platform and cid and cid not in out:
            out.append(cid)
    return out


def search_aspects(s: dict) -> list[dict]:
    """Artikelmerkmale eines Gesuchs: searches.aspects (JSON) → [{"name": "Größe", "values": ["M", "L"]}].
    Gleiches Merkmal mit mehreren Werten: eines davon reicht. Verschiedene Merkmale: alle müssen passen."""
    try:
        data = json.loads(s.get("aspects") or "[]")
    except ValueError:
        return []
    return [{"name": str(a["name"]), "values": [str(v) for v in a["values"] if str(v)]}
            for a in data if isinstance(a, dict) and a.get("name") and isinstance(a.get("values"), list) and a["values"]]


class PlatformError(Exception):
    """Fehler einer Plattform mit einem Text, den man so anzeigen kann."""


class Results(list):
    """Gefundene Angebote. total: so viele gibt es laut Plattform insgesamt."""

    total = 0

    @property
    def complete(self) -> bool:
        """Alle Treffer geholt (nicht nach der letzten Seite abgeschnitten)."""
        return len(self) >= self.total


@dataclass(frozen=True)
class Credential:
    """Ein Feld der Zugangsdaten, z. B. App ID. secret: wird verschlüsselt gespeichert und nie angezeigt."""
    key: str          # Name in den Einstellungen, z. B. ebay_client_id
    label: str
    hint: str = ""
    secret: bool = False


@dataclass
class Found:
    """Ergebnis einer Suche nach neuen Angeboten.
    items: Angebotsnummer → (Angebot, aus der Abholsuche?)
    keep_pickup: diesmal gab es keine Abholsuche; Abholangebote der letzten gelten weiter.
    capped: die Plattform hatte mehr Treffer, als die Grenze des Gesuchs zulässt (Issue #15)."""
    items: dict
    keep_pickup: bool = False
    capped: bool = False


class Plattform:
    key = ""            # Kürzel, steht in der Datenbank (searches.platforms, items.platform)
    name = ""           # für die Anzeige, z. B. „eBay“
    credentials: tuple[Credential, ...] = ()
    image_hosts: tuple[str, ...] = ()   # Bilder dürfen nur von diesen Servern geladen werden
    auctions = False    # kennt die Plattform Auktionen (Endspurt, Erinnerung)?
    details = False     # liefert die Einzelabfrage Beschreibung und Merkmale (Galerie)?
    categories = False  # lässt sich die Suche auf Kategorien beschränken (kategorien.py)?

    def configured(self, settings: dict) -> bool:
        raise NotImplementedError

    async def close(self) -> None:
        pass

    def forget_pickup(self, search_ids: list[int]) -> None:
        """Standort oder Umkreis eines Kontos geändert: für diese Gesuche beim nächsten Mal auch
        Abholangebote neu suchen."""

    async def find(self, s: dict, settings: dict, now: float) -> Found:
        """Neue Angebote zu einem Gesuch (Suchbegriff, Höchstpreis, Format, Standort, Zustand …,
        nur in seinen Kategorien dieser Plattform, falls welche gewählt sind: kategorien.of_search)."""
        raise NotImplementedError

    # ------------------------------------------------------------ Kategorien (nur mit categories = True)

    async def category_version(self, settings: dict) -> str:
        """Kennung der aktuellen Fassung des Kategorienbaums (ändert sich, wenn die Plattform umbaut)."""
        raise NotImplementedError

    async def category_tree(self, settings: dict) -> tuple[str, list[dict]]:
        """Der ganze Kategorienbaum: (Fassung, Kategorien mit CATEGORY_FIELDS, Eltern vor Kindern)."""
        raise NotImplementedError

    async def category_suggestions(self, settings: dict, query: str) -> list[str]:
        """Kategorien, die zu einem Suchbegriff passen (ids, beste zuerst)."""
        return []

    async def aspect_suggestions(self, settings: dict, query: str, category: str) -> list[tuple[str, list[tuple[str, int]]]]:
        """Artikelmerkmale in einer Kategorie mit ihren häufigsten Werten:
        [(Merkmal, [(Wert, Anzahl Angebote), …]), …], häufigste zuerst."""
        return []

    async def fetch(self, item_id: str, settings: dict, purpose: str,
                    pickup: bool = False) -> tuple[dict, dict | None] | None:
        """Aktueller Stand eines Angebots: (Angebot, Angaben für die Galerie oder None).
        None: Angebot gibt es nicht mehr."""
        raise NotImplementedError

    async def compare(self, settings: dict, s: dict | None = None, query: str | None = None,
                      catalog: str | None = None) -> tuple[list[dict], bool]:
        """Vergleichsangebote für den Durchschnittspreis (nur Sofortkauf, ohne Höchstpreis):
        zum Gesuch s, zu einem Suchbegriff (Artikelnummer) oder zu einem Katalog-Produkt.
        Zweiter Wert: Antwort vollständig (dann gelten fehlende Angebote als verschwunden)."""
        return [], False

    def image(self, url: str | None, size: int) -> str | None:
        """Adresse eines Bildes in der gewünschten Größe (Kantenlänge in Pixeln, soweit möglich)."""
        return url

    def same_image(self, a: str, b: str) -> bool:
        return self.image(a, 500) == self.image(b, 500)

    async def check(self, settings: dict) -> dict:
        """Verbindung prüfen. Ergebnis für die Einstellungen, z. B. {"limit": 5000, "remaining": 4200}."""
        return {}

    def missing_text(self) -> str:
        return f"Die Zugangsdaten für {self.name} fehlen. Trage sie unter Einstellungen → {self.name}-Verbindung ein."
