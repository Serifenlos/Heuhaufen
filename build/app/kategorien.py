"""Kategorien der Plattformen (Issue #13): einmal laden, danach nur noch lokal nachschlagen.

* Der Kategorienbaum gehört allen Konten gemeinsam (Tabelle categories, wie die anderen Caches).
* refresh() fragt die Plattform höchstens einmal pro Woche (CHECK_EVERY), welche Fassung des Baums aktuell
  ist. Das ist eine kleine Abfrage. Nur wenn sich die Fassung geändert hat, lädt es den ganzen Baum neu
  (eBay.de: einige zehntausend Kategorien) und ersetzt ihn in einem Rutsch. Angestoßen wird das vom Watcher
  (watcher.update_categories) und beim allerersten Öffnen der Auswahl, falls noch nichts da ist.
* Das Formular für Gesuche liest nur aus der Datenbank: children(), find(), lookup(). Vorschläge zu einem
  Suchbegriff (suggestions) fragt es bei der Plattform an und merkt sich die Antwort einen Tag.
* Ein Gesuch speichert seine Kategorien als „ebay:58058,ebay:175672“ (searches.categories). Eine Kategorie
  schließt ihre Unterkategorien ein; normalize() entfernt deshalb Unterkategorien einer gewählten.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from . import db
from .plattformen import liste as plattformen
from .plattformen.basis import Plattform, search_aspects

log = logging.getLogger("heuhaufen.kategorien")

CHECK_EVERY = 7 * 86400          # so oft nach einer neuen Fassung fragen
SUGGEST_FOR = 86400              # Vorschläge zu einem Suchbegriff so lange merken
MAX_PER_SEARCH = 10              # Kategorien je Gesuch (jede kostet pro Suchlauf eine eigene Abfrage)
SEP = " › "

ASPECT_CATEGORIES = 3            # Merkmal-Vorschläge aus höchstens so vielen Kategorien (je eine Abfrage)

_lock = asyncio.Lock()
_suggested: dict[tuple[str, str], tuple[float, list[str]]] = {}
_aspects: dict[tuple[str, str, str], tuple[float, list]] = {}


# ---------------------------------------------------------------- Laden und aktuell halten

def status(platform: str) -> dict | None:
    return db.q1("SELECT * FROM category_trees WHERE platform = ?", (platform,))


def loaded(platform: str) -> bool:
    row = status(platform)
    return bool(row and row["count"])


def due(platform: str, now: float | None = None) -> bool:
    row = status(platform)
    if not row or not row["count"] or not row["checked_at"]:
        return True
    return (now or time.time()) - row["checked_at"] >= CHECK_EVERY


async def refresh(p: Plattform, settings: dict, force: bool = False) -> bool:
    """Baum laden, falls fällig und die Plattform eine neue Fassung hat. True: neu geladen.
    Fehler der Plattform (PlatformError) gehen an den Aufrufer."""
    async with _lock:   # nie zweimal gleichzeitig laden (Watcher und Formular)
        if not force and not due(p.key):
            return False
        row = status(p.key)
        now = time.time()
        version = await p.category_version(settings)
        if not force and row and row["count"] and row["version"] == version:
            db.ex("UPDATE category_trees SET checked_at = ? WHERE platform = ?", (now, p.key))
            return False
        version, nodes = await p.category_tree(settings)
        if not nodes:
            log.warning("%s: Kategorienbaum ist leer, der bisherige bleibt.", p.name)
            db.ex("INSERT INTO category_trees(platform, checked_at) VALUES (?, ?) "
                  "ON CONFLICT(platform) DO UPDATE SET checked_at = excluded.checked_at", (p.key, now))
            return False
        store(p.key, version, nodes, now)
        log.info("%s: %d Kategorien geladen (Fassung %s).", p.name, len(nodes), version)
        return True


def store(platform: str, version: str, nodes: list[dict], now: float | None = None) -> None:
    """Baum ersetzen. nodes: CATEGORY_FIELDS, Eltern vor Kindern (plattformen/basis.py)."""
    now = now or time.time()
    paths: dict[str, str] = {}
    rows = []
    for n in nodes:
        parent_path = paths.get(n["parent"], "") if n["parent"] else ""
        paths[n["id"]] = (parent_path + SEP if parent_path else "") + n["name"]
        rows.append((platform, n["id"], n["parent"], n["name"], parent_path, n["name"].casefold(),
                     n["level"], int(bool(n["leaf"])), n["position"]))
    with db.transaction() as conn:
        conn.execute("DELETE FROM categories WHERE platform = ?", (platform,))
        conn.executemany("INSERT OR REPLACE INTO categories(platform, id, parent_id, name, path, search, level, leaf, "
                         "position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("INSERT INTO category_trees(platform, version, count, fetched_at, checked_at) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT(platform) DO UPDATE SET version = excluded.version, count = excluded.count, "
                     "fetched_at = excluded.fetched_at, checked_at = excluded.checked_at",
                     (platform, version, len(rows), now, now))
    _suggested.clear()


# ---------------------------------------------------------------- Nachschlagen (nur Datenbank)

def _view(row: dict) -> dict:
    return {**row, "key": f"{row['platform']}:{row['id']}"}


def children(platform: str, parent: str | None) -> list[dict]:
    if parent:
        rows = db.q("SELECT * FROM categories WHERE platform = ? AND parent_id = ? ORDER BY position", (platform, parent))
    else:
        rows = db.q("SELECT * FROM categories WHERE platform = ? AND parent_id IS NULL ORDER BY position", (platform,))
    return [_view(r) for r in rows]


def get(platform: str, cid: str) -> dict | None:
    row = db.q1("SELECT * FROM categories WHERE platform = ? AND id = ?", (platform, cid))
    return _view(row) if row else None


def find(platform: str, text: str, limit: int = 50) -> list[dict]:
    """Kategorien, deren Name alle Wörter enthält; Namen, die so anfangen, zuerst."""
    words = [w for w in text.casefold().split() if w][:5]
    if not words:
        return []
    like = " AND ".join("search LIKE ? ESCAPE '\\'" for _ in words)
    esc = [w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for w in words]
    rows = db.q(f"SELECT * FROM categories WHERE platform = ? AND {like} "
                "ORDER BY search LIKE ? ESCAPE '\\' DESC, level, name LIMIT ?",
                (platform, *[f"%{w}%" for w in esc], f"{esc[0]}%", limit))
    return [_view(r) for r in rows]


def parse(keys) -> list[tuple[str, str]]:
    """„ebay:58058“ (einzeln, Liste oder durch Komma getrennt) → [("ebay", "58058")], ohne Unbekanntes."""
    if isinstance(keys, str):
        keys = keys.split(",")
    out: list[tuple[str, str]] = []
    for key in keys or []:
        platform, _, cid = str(key).strip().partition(":")
        p = plattformen.ALL.get(platform)
        if p and p.categories and cid.isdigit() and len(cid) <= 20 and (platform, cid) not in out:
            out.append((platform, cid))
    return out


def ancestors(platform: str, cid: str) -> list[str]:
    out: list[str] = []
    row = db.q1("SELECT parent_id FROM categories WHERE platform = ? AND id = ?", (platform, cid))
    while row and row["parent_id"] and len(out) < 20:
        out.append(row["parent_id"])
        row = db.q1("SELECT parent_id FROM categories WHERE platform = ? AND id = ?", (platform, row["parent_id"]))
    return out


def normalize(keys) -> str:
    """Für searches.categories: gültige, ohne doppelte und ohne Unterkategorien einer ebenfalls gewählten
    (die ist dort schon enthalten und würde nur eine Abfrage mehr kosten), höchstens MAX_PER_SEARCH."""
    chosen = parse(keys)
    picked = set(chosen)
    keep = [(p, c) for p, c in chosen if not any((p, a) in picked for a in ancestors(p, c))]
    return ",".join(f"{p}:{c}" for p, c in keep[:MAX_PER_SEARCH])


def lookup(keys) -> list[dict]:
    """Kategorien eines Gesuchs für die Anzeige. Unbekannte (Baum noch nicht geladen oder von der
    Plattform entfernt) mit known=False und der Nummer als Namen."""
    out = []
    for platform, cid in parse(keys):
        row = get(platform, cid)
        out.append({**row, "known": True} if row else
                   {"platform": platform, "id": cid, "key": f"{platform}:{cid}", "name": f"Kategorie {cid}",
                    "path": "", "leaf": 1, "known": False})
    return out


def names(keys) -> list[str]:
    return [c["name"] for c in lookup(keys)]


# ---------------------------------------------------------------- Vorschläge (Abfrage bei der Plattform)

async def suggestions(p: Plattform, settings: dict, query: str, limit: int = 6) -> list[dict]:
    """Kategorien, die zum Suchbegriff passen, beste zuerst. Ohne geladenen Baum oder bei Fehlern: leer."""
    query = " ".join(query.split())[:100]
    if len(query) < 2 or not p.categories or not p.configured(settings) or not loaded(p.key):
        return []
    key = (p.key, query.casefold())
    cached = _suggested.get(key)
    if cached and time.time() - cached[0] < SUGGEST_FOR:
        ids = cached[1]
    else:
        try:
            ids = await p.category_suggestions(settings, query)
        except Exception as e:   # Vorschläge sind ein Extra, die Auswahl geht auch ohne
            log.info("Kategorie-Vorschläge zu %r: %s", query, e)
            return []
        if len(_suggested) > 500:
            _suggested.clear()
        _suggested[key] = (time.time(), ids)
    rows = [get(p.key, cid) for cid in ids]
    return [r for r in rows if r][:limit]


# ---------------------------------------------------------------- Artikelmerkmale (Issue #12)

MAX_ASPECTS = 10                 # Merkmal-Wert-Paare je Gesuch


def parse_aspects(names: list, values: list) -> tuple[str, list[tuple[str, str]], str | None]:
    """Formularfelder aspect_name/aspect_value → (JSON für searches.aspects, Zeilen zum erneuten Anzeigen,
    Fehlertext). Gleiches Merkmal mehrmals: Werte zusammen (eines reicht). Leere Zeilen fallen weg."""
    rows: list[tuple[str, str]] = []
    error = None
    for name, value in zip(names, values):
        name, value = " ".join(str(name).split())[:60], " ".join(str(value).split())[:60]
        if not name and not value:
            continue
        rows.append((name, value))
        if not name or not value:
            error = "Zu jedem Merkmal gehört ein Wert, zum Beispiel Größe und M."
    if len(rows) > MAX_ASPECTS:
        error = f"Höchstens {MAX_ASPECTS} Merkmale."
    merged: dict[str, dict] = {}
    for name, value in rows[:MAX_ASPECTS]:
        if name and value:
            a = merged.setdefault(name.casefold(), {"name": name, "values": []})
            if value.casefold() not in [v.casefold() for v in a["values"]]:
                a["values"].append(value)
    data = list(merged.values())
    return (json.dumps(data, ensure_ascii=False) if data else ""), rows, error


def aspect_rows(aspects_json: str) -> list[tuple[str, str]]:
    """searches.aspects → Zeilen für das Formular (je Wert eine Zeile)."""
    return [(a["name"], v) for a in search_aspects({"aspects": aspects_json}) for v in a["values"]]


def aspect_text(aspects_json: str) -> str:
    """Für die Anzeige: „Größe M oder L, Farbe Schwarz“."""
    return ", ".join(f"{a['name']} {' oder '.join(a['values'])}" for a in search_aspects({"aspects": aspects_json}))


async def aspect_suggestions(p: Plattform, settings: dict, query: str, keys) -> dict:
    """Merkmale und ihre häufigsten Werte in den gewählten Kategorien, für die Vorschlagsliste im Formular,
    jeweils mit der Zahl der Angebote bei der Plattform, nach Häufigkeit sortiert:
    {"names": [[Merkmal, Anzahl], …], "values": {Merkmal: [[Wert, Anzahl], …]}}. Je Kategorie und Suchbegriff
    eine Abfrage, einen Tag gemerkt."""
    query = " ".join(query.split())[:100]
    cats = [cid for platform, cid in parse(keys) if platform == p.key][:ASPECT_CATEGORIES]
    if not cats or not p.configured(settings):
        return {"names": [], "values": {}}
    counts: dict[str, int] = {}
    spelled: dict[str, str] = {}
    values: dict[str, dict[str, int]] = {}
    for cid in cats:
        key = (p.key, cid, query.casefold())
        cached = _aspects.get(key)
        if cached and time.time() - cached[0] < SUGGEST_FOR:
            found = cached[1]
        else:
            try:
                found = await p.aspect_suggestions(settings, query, cid)
            except Exception as e:   # Vorschläge sind ein Extra
                log.info("Merkmal-Vorschläge für Kategorie %s: %s", cid, e)
                continue
            if len(_aspects) > 500:
                _aspects.clear()
            _aspects[key] = (time.time(), found)
        for name, vals in found:
            k = name.casefold()
            spelled.setdefault(k, name)
            counts[k] = counts.get(k, 0) + sum(n for _, n in vals)
            for v, n in vals:
                values.setdefault(k, {})
                values[k][v] = values[k].get(v, 0) + n
    names = sorted(counts, key=lambda k: -counts[k])[:60]
    return {"names": [[spelled[k], counts[k]] for k in names],
            "values": {spelled[k]: [[v, n] for v, n in sorted(values[k].items(), key=lambda x: -x[1])][:50] for k in names}}
