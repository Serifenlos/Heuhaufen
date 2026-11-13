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

ASPECT_CATEGORIES = MAX_PER_SEARCH   # Merkmal-Vorschläge aus allen gewählten Kategorien (je eine Abfrage, einen Tag gemerkt)
CHILD_ASPECTS = 8                # bei einer Oberkategorie zusätzlich Merkmale aus so vielen Unterkategorien (Issue #15)
ASPECT_CALLS = 30                # Abfragen für Merkmal-Vorschläge je Aufruf höchstens

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

MAX_ASPECTS = 30                 # Werte je Gesuch, über alle Merkmale zusammen (je Merkmal eine Zeile im Formular)


def _clean(text) -> str:
    return " ".join(str(text).split())[:100]   # eBay erlaubt 65 Zeichen


def parse_aspects(names: list, values: list) -> tuple[str, list[tuple[str, list[str]]], str | None]:
    """Formularfelder aspect_name/aspect_value → (JSON für searches.aspects, Zeilen zum erneuten Anzeigen,
    Fehlertext). Je Zeile ein Merkmal; aspect_value enthält dessen Werte, getrennt durch Zeilenumbrüche
    (app.js, Issue #16), einer reicht. Gleiches Merkmal in mehreren Zeilen: Werte zusammen. Leere Zeilen fallen weg."""
    rows: list[tuple[str, list[str]]] = []
    error = None
    for name, raw in zip(names, values):
        name = _clean(name)
        vals: list[str] = []
        for v in str(raw).split("\n"):
            v = _clean(v)
            if v and v.casefold() not in [x.casefold() for x in vals]:
                vals.append(v)
        if not name and not vals:
            continue
        rows.append((name, vals))
        if not name or not vals:
            error = "Zu jedem Merkmal gehört mindestens ein Wert, zum Beispiel Größe und M."
    count = 0
    merged: dict[str, dict] = {}
    for name, vals in rows:
        for v in vals if name else []:
            count += 1
            if count > MAX_ASPECTS:
                break
            a = merged.setdefault(name.casefold(), {"name": name, "values": []})
            if v.casefold() not in [x.casefold() for x in a["values"]]:
                a["values"].append(v)
    if count > MAX_ASPECTS:
        error = f"Höchstens {MAX_ASPECTS} Werte je Gesuch."
    data = list(merged.values())
    return (json.dumps(data, ensure_ascii=False) if data else ""), rows, error


def aspect_rows(aspects_json: str) -> list[tuple[str, list[str]]]:
    """searches.aspects → Zeilen für das Formular (je Merkmal eine Zeile mit seinen Werten)."""
    return [(a["name"], list(a["values"])) for a in search_aspects({"aspects": aspects_json})]


def aspect_text(aspects_json: str) -> str:
    """Für die Anzeige: „Größe M oder L, Farbe Schwarz“."""
    return ", ".join(f"{a['name']} {' oder '.join(a['values'])}" for a in search_aspects({"aspects": aspects_json}))


async def aspect_suggestions(p: Plattform, settings: dict, query: str, keys) -> dict:
    """Merkmale und ihre Werte in den gewählten Kategorien, für die Vorschlagsliste im Formular, jeweils mit
    der Zahl der Angebote bei der Plattform, nach Häufigkeit sortiert:
    {"names": [[Merkmal, Anzahl], …], "values": {Merkmal: [[Wert, Anzahl], …]}}.
    eBay nennt je Abfrage nur die Merkmale der häufigsten Unterkategorie. Bei einer Oberkategorie (Issue #15,
    z. B. „Herren“) fragt Heuhaufen deshalb auch ihre Unterkategorien (höchstens CHILD_ASPECTS) und nimmt alles
    zusammen; innerhalb einer gewählten Kategorie zählt je Wert die höchste Zahl (die Oberkategorie enthält
    die Unterkategorien schon), über mehrere gewählte Kategorien werden die Zahlen addiert.
    Je Kategorie und Suchbegriff eine Abfrage, einen Tag gemerkt, zusammen höchstens ASPECT_CALLS."""
    query = " ".join(query.split())[:100]
    cats = [cid for platform, cid in parse(keys) if platform == p.key][:ASPECT_CATEGORIES]
    if not cats or not p.configured(settings):
        return {"names": [], "values": {}}
    groups: list[list[str]] = []
    budget = ASPECT_CALLS
    for cid in cats:
        row = get(p.key, cid)
        group = [cid]
        if row and not row["leaf"]:
            group += [c["id"] for c in children(p.key, cid)[:CHILD_ASPECTS]]
        group = group[:max(1, budget)]
        budget -= len(group)
        groups.append(group)
    spelled: dict[str, str] = {}
    values: dict[str, dict[str, int]] = {}
    for group in groups:
        best: dict[str, dict[str, int]] = {}   # innerhalb der gewählten Kategorie: höchste Zahl je Wert
        for cid in group:
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
                for v, n in vals:
                    seen = best.setdefault(k, {})
                    seen[v] = max(seen.get(v, 0), n)
        for k, vals in best.items():   # über die gewählten Kategorien: addieren
            for v, n in vals.items():
                values.setdefault(k, {})
                values[k][v] = values[k].get(v, 0) + n
    counts = {k: sum(vals.values()) for k, vals in values.items()}
    # alles, was die Plattform liefert, ohne Kürzung (Issue #16: auch seltene Marken); die Liste filtert beim Tippen
    names = sorted(counts, key=lambda k: -counts[k])
    return {"names": [[spelled[k], counts[k]] for k in names],
            "values": {spelled[k]: [[v, n] for v, n in sorted(values[k].items(), key=lambda x: -x[1])] for k in names}}
