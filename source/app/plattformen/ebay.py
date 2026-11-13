"""eBay als Plattform: Browse API (Nachfolger der abgeschalteten Finding API).

Alles, was nur eBay betrifft, steht hier: Anmeldung mit App ID und Cert ID, Filtersprache der Suche,
Aufbau der Antworten, Zustandsnummern, Bildgrößen (s-l500 …), Felder der Einzelabfrage getItem.
Der Rest von Heuhaufen bekommt Angebote nur in der gemeinsamen Form (plattformen/basis.py).

Suchstrategie (spart Abfragen, siehe ReadSimon „eBay-Abfragen sparen“):
* Mit Versand nach Deutschland: Bei „Auktion und Sofortkauf“ zuerst eine gemeinsame Abfrage. Passen alle
  Treffer auf eine Seite (bis 200), reicht sie. Sonst eine Weile (SPLIT_FOR) gleich getrennt: Auktionen
  nach Ende sortiert, Sofortkauf nach Neuheit, je bis zu 3 Seiten.
* Abholung im Umkreis: höchstens einmal pro Stunde (PICKUP_EVERY). Dazwischen gilt, was die letzte
  Abholsuche gefunden hat.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.parse
from datetime import datetime

import httpx

from .. import config, db, geheim
from .basis import (Credential, Found, Plattform, PlatformError, Results, result_limit, search_aspects,
                    search_categories)

log = logging.getLogger("heuhaufen.ebay")

API = "https://api.ebay.com"
TAXONOMY = "/commerce/taxonomy/v1"   # Kategorien; eigenes Tageslimit, zählt nicht bei der Browse API
SCOPE = "https://api.ebay.com/oauth/api_scope"
PAGE_SIZE = 200                # Treffer je Abfrage (höchstens)
PICKUP_RADIUS_ALL_KM = 1000    # Abholmodus „alle anzeigen“: praktisch ganz Deutschland
PICKUP_EVERY = 3600            # Abholsuche höchstens stündlich; Abholangebote ändern sich selten
SPLIT_FOR = 6 * 3600           # „Beides“ mit mehr als 200 Treffern: so lange gleich getrennt suchen
FORMATS = {"auction": ["AUCTION"], "fixed": ["FIXED_PRICE"], "both": ["AUCTION", "FIXED_PRICE"]}


class EbayError(PlatformError):
    pass


def _error_text(r: httpx.Response) -> str:
    if r.status_code == 429:
        return "Das Tageslimit der eBay-API ist erreicht. Morgen geht es automatisch weiter."
    try:
        errors = r.json().get("errors") or []
        msg = errors[0].get("longMessage") or errors[0].get("message") if errors else ""
    except ValueError:
        msg = ""
    return f"eBay antwortet mit Fehler {r.status_code}" + (f": {msg[:200]}" if msg else ".")


# ---------------------------------------------------------------- Zugriff auf die Browse API

class EbayClient:
    def __init__(self) -> None:
        self._client_id = ""
        self._secret = ""
        self.user_id: int | None = None   # wessen Abfragen gezählt werden
        self._token: str | None = None
        self._token_exp = 0.0
        self._http = httpx.AsyncClient(timeout=25.0, headers={"User-Agent": f"heuhaufen/{config.APP_VERSION}"})

    def use(self, client_id: str, secret: str) -> None:
        """Zugangsdaten aus den Einstellungen. Geänderte Daten: neues Token holen."""
        if (client_id, secret) != (self._client_id, self._secret):
            self._client_id, self._secret = client_id, secret
            self._token = None

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._secret)

    async def close(self) -> None:
        await self._http.aclose()

    async def _token_value(self) -> str:
        if self._token and time.time() < self._token_exp - 120:
            return self._token
        if not self.configured:
            raise EbayError("Die eBay-Zugangsdaten fehlen. Trage App ID und Cert ID unter Einstellungen → "
                            "eBay-Verbindung ein.")
        basic = base64.b64encode(f"{self._client_id}:{self._secret}".encode()).decode()
        try:
            r = await self._http.post(
                f"{API}/identity/v1/oauth2/token",
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": SCOPE},
            )
        except httpx.HTTPError as e:
            raise EbayError(f"eBay ist nicht erreichbar ({e.__class__.__name__}).") from e
        if r.status_code != 200:
            raise EbayError(f"Die Anmeldung bei eBay ist fehlgeschlagen ({r.status_code}). Prüfe App ID und Cert ID.")
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 7200))
        return self._token

    def _headers(self, token: str, plz: str) -> dict[str, str]:
        ctx = "country=DE" + (f",zip={plz}" if plz else "")
        return {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": config.EBAY_MARKETPLACE,
            "X-EBAY-C-ENDUSERCTX": "contextualLocation=" + urllib.parse.quote(ctx, safe=""),
            "Accept-Language": "de-DE",
        }

    async def _get(self, path: str, params: dict | None = None, plz: str = "",
                   purpose: str | None = "suche", timeout: float | None = None) -> httpx.Response:
        for attempt in (1, 2):
            token = await self._token_value()
            if purpose:
                db.count_api_call(purpose, platform="ebay", user_id=self.user_id)
            try:
                extra = {"timeout": timeout} if timeout else {}
                r = await self._http.get(API + path, params=params, headers=self._headers(token, plz), **extra)
            except httpx.HTTPError as e:
                raise EbayError(f"eBay ist nicht erreichbar ({e.__class__.__name__}).") from e
            if r.status_code == 401 and attempt == 1:
                self._token = None  # Token abgelaufen oder widerrufen: einmal neu holen
                continue
            return r
        return r

    async def search(self, q: str | None, filters: list[str], sort: str | None = None,
                     plz: str = "", max_pages: int = 3, extra: dict | None = None,
                     purpose: str = "suche") -> Results:
        """Angebotssuche, je Seite eine Abfrage. extra: weitere Parameter, z. B. {"epid": …} für ein
        Produkt aus dem eBay-Katalog (dann ohne Suchbegriff)."""
        items = Results()
        offset = 0
        for _ in range(max_pages):
            params = {"filter": ",".join(filters), "limit": str(PAGE_SIZE), "offset": str(offset), **(extra or {})}
            if q:
                params["q"] = q
            if sort:
                params["sort"] = sort
            r = await self._get("/buy/browse/v1/item_summary/search", params, plz, purpose)
            if r.status_code != 200:
                raise EbayError(_error_text(r))
            data = r.json()
            ignored = _aspects_ignored(data, params)
            if ignored:
                raise EbayError(ignored)
            batch = data.get("itemSummaries") or []
            items.extend(batch)
            offset += len(batch)
            total = int(data.get("total") or 0)
            if not batch or not data.get("next") or offset >= total:
                items.total = len(items)   # alles geholt
                break
            items.total = max(total, len(items))   # es gäbe weitere Seiten
        return items

    async def aspect_distributions(self, q: str | None, category: str, plz: str = "") -> list[dict]:
        """Artikelmerkmale der Treffer in einer Kategorie mit Anzahl je Wert (fieldgroups=ASPECT_REFINEMENTS).
        Eine Abfrage, gezählt als „merkmale“."""
        params = {"category_ids": category, "fieldgroups": "ASPECT_REFINEMENTS", "limit": "1"}
        if q:
            params["q"] = q
        r = await self._get("/buy/browse/v1/item_summary/search", params, plz, purpose="merkmale")
        if r.status_code != 200:
            raise EbayError(_error_text(r))
        return ((r.json().get("refinement") or {}).get("aspectDistributions")) or []

    async def get_item(self, item_id: str, plz: str = "", purpose: str = "suche") -> dict | None:
        r = await self._get("/buy/browse/v1/item/" + urllib.parse.quote(item_id, safe=""), plz=plz, purpose=purpose)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise EbayError(_error_text(r))
        return r.json()

    # ------------------------------------------------------------ Kategorien (Taxonomy API)

    async def _taxonomy(self, path: str, params: dict | None = None, timeout: float | None = None) -> dict | None:
        r = await self._get(TAXONOMY + path, params, purpose=None, timeout=timeout)
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            raise EbayError(_error_text(r))
        return r.json()

    async def default_tree(self) -> tuple[str, str]:
        """(Baum-Nummer, Fassung) für den Marktplatz, bei eBay.de der Baum 77."""
        data = await self._taxonomy("/get_default_category_tree_id",
                                    {"marketplace_id": config.EBAY_MARKETPLACE}) or {}
        if not data.get("categoryTreeId"):
            raise EbayError("eBay liefert keinen Kategorienbaum für diesen Marktplatz.")
        return str(data["categoryTreeId"]), str(data.get("categoryTreeVersion") or "")

    async def category_tree(self, tree_id: str) -> dict:
        """Der ganze Baum. Groß (viele MB), httpx fragt gzip-komprimiert an."""
        return await self._taxonomy(f"/category_tree/{urllib.parse.quote(tree_id, safe='')}",
                                    timeout=180.0) or {}

    async def category_suggestions(self, tree_id: str, q: str) -> list[dict]:
        data = await self._taxonomy(f"/category_tree/{urllib.parse.quote(tree_id, safe='')}/get_category_suggestions",
                                    {"q": q}) or {}
        return data.get("categorySuggestions") or []

    async def rate_limits(self) -> dict:
        """Kontingent der Browse API laut eBay (Developer Analytics API)."""
        r = await self._get("/developer/analytics/v1_beta/rate_limit/",
                            {"api_context": "buy", "api_name": "Browse"}, purpose=None)
        if r.status_code != 200:
            raise EbayError(_error_text(r))
        for limit in r.json().get("rateLimits") or []:
            if (limit.get("apiName") or "").lower() != "browse":
                continue
            for resource in limit.get("resources") or []:
                for rate in resource.get("rates") or []:
                    return {"limit": rate.get("limit"), "remaining": rate.get("remaining"),
                            "reset": rate.get("reset")}
        return {}


# ---------------------------------------------------------------- Antworten auswerten

def _money(obj: dict | None) -> tuple[float | None, str | None]:
    if not obj or obj.get("value") in (None, ""):
        return None, None
    try:
        return float(obj["value"]), obj.get("currency")
    except (TypeError, ValueError):
        return None, None


def _iso_ts(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def condition_class(condition_id: object) -> str | None:
    """eBay-Zustandsnummer → new | used | parts. Generalüberholt zählt als gebraucht."""
    try:
        cid = int(condition_id)
    except (TypeError, ValueError):
        return None
    if cid in (1000, 1500, 1750):
        return "new"
    return "parts" if cid == 7000 else "used"


def image_size(url: str | None, size: int = 500) -> str | None:
    """eBay-Bildadresse in anderer Größe: …/s-l225.jpg → …/s-l1600.jpg"""
    if not url:
        return None
    return re.sub(r"s-l\d+", f"s-l{size}", url)


def parse_listing(d: dict, pickup: bool = False) -> dict:
    """Wandelt eine eBay item_summary- oder getItem-Antwort in die gemeinsame Form um (basis.LISTING_FIELDS).
    Verkäuferdaten werden absichtlich nicht übernommen."""
    buying = d.get("buyingOptions") or []
    is_auction = "AUCTION" in buying
    has_bin = "FIXED_PRICE" in buying

    price, currency = _money(d.get("price"))
    bin_price = price if has_bin else None
    if is_auction:
        bid, bid_cur = _money(d.get("currentBidPrice"))
        if bid is not None:
            price, currency = bid, bid_cur or currency

    ship_opts = d.get("shippingOptions") or []
    costs = []
    for opt in ship_opts:
        value, _cur = _money(opt.get("shippingCost"))
        if value is not None:
            costs.append(value)
    shipping, shipping_known = (min(costs), 1) if costs else (None, 0)
    pickup_only = 1 if pickup and not ship_opts else 0
    if pickup_only:
        shipping, shipping_known = 0.0, 1

    loc = d.get("itemLocation") or {}
    loc_text = " ".join(x for x in [loc.get("postalCode"), loc.get("city")] if x)

    distance = None
    dist = d.get("distanceFromPickupLocation") or {}
    try:
        if dist.get("value") is not None:
            distance = float(dist["value"])
            if (dist.get("unit") or "").upper().startswith("MI"):
                distance *= 1.609
    except (TypeError, ValueError):
        distance = None

    # Hauptbild und weitere Bilder (für die Galerie), jedes nur einmal, egal in welcher Größe eBay es nennt
    urls: list[str] = []
    for img in [d.get("image"), *(d.get("additionalImages") or [])]:
        url = img.get("imageUrl") if isinstance(img, dict) else None
        if url and image_size(url) not in {image_size(u) for u in urls}:
            urls.append(url)
    if not urls:
        thumbs = d.get("thumbnailImages") or []
        if thumbs and thumbs[0].get("imageUrl"):
            urls.append(thumbs[0]["imageUrl"])
    image = urls[0] if urls else None

    return {
        "item_id": d.get("itemId"),
        "platform": "ebay",
        "title": d.get("title") or "",
        "url": d.get("itemWebUrl") or d.get("itemAffiliateWebUrl") or "",
        "image": image,
        "images": json.dumps(urls) if urls else None,
        "condition_text": (d.get("condition") or "").strip()[:120] or None,
        "is_auction": int(is_auction),
        "has_bin": int(has_bin),
        "price": price,
        "bin_price": bin_price,
        "currency": currency or "EUR",
        "shipping": shipping,
        "shipping_known": shipping_known,
        "bid_count": d.get("bidCount"),
        "end_time": _iso_ts(d.get("itemEndDate")),
        "country": loc.get("country"),
        "location_text": loc_text,
        "pickup": int(pickup),
        "pickup_only": pickup_only,
        "distance_km": distance,
        "near": 0,   # setzt der Watcher, er kennt den Umkreis
        "epid": d.get("epid") or None,
        "cond": condition_class(d.get("conditionId")),
    }


def parse_detail(d: dict) -> dict:
    """Angaben für die Galerie aus einer getItem-Antwort (basis.DETAIL_FIELDS)."""
    reserve = d.get("reservePriceMet")
    return {
        "description_html": d.get("description"),
        "condition_note": d.get("conditionDescription"),
        "aspects": [(a.get("name"), a.get("value")) for a in d.get("localizedAspects") or []],
        "bidders": d.get("uniqueBidderCount"),
        "min_bid": _money(d.get("minimumPriceToBid"))[0],
        "reserve_met": None if reserve is None else bool(reserve),
    }


def _aspects_ignored(data: dict, params: dict) -> str | None:
    """Wendet eBay einen Merkmal-Filter nicht an (etwa in einer Kategorie, in der es das Merkmal nicht gibt),
    meldet es das nur als Warnung (z. B. errorId 12017) und liefert alle Angebote der Kategorie. Dann lieber
    als Fehler melden, sonst kämen Treffer, die gar nicht passen."""
    if "aspect_filter" not in params:
        return None
    for w in data.get("warnings") or []:
        message = str(w.get("message") or "")
        if w.get("errorId") == 12017 or "aspect" in message.lower():
            hint = f" (eBay: {message[:160]})" if message else ""
            return ("eBay hat die Artikelmerkmale nicht angewendet" + hint +
                    ". Wähle eine tiefere Kategorie oder prüfe die Schreibweise der Merkmale.")
    return None


def aspect_filter(category: str | None, aspects: list[dict]) -> str | None:
    """Filter für Artikelmerkmale, z. B. „categoryId:57988,Größe:{M|L},Farbe:{Schwarz}“. eBay nimmt ihn nur
    zusammen mit einer Kategorie an (dieselbe Nummer auch in category_ids). | im Wert schützt ein \\,
    geschweifte Klammern fallen weg."""
    if not category or not aspects:
        return None

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("|", "\\|").replace("{", "").replace("}", "").strip()

    parts = [f"categoryId:{category}"]
    for a in aspects:
        values = [esc(v) for v in a["values"] if esc(v)]
        if esc(a["name"]) and values:
            parts.append(f"{esc(a['name'])}:{{{'|'.join(values)}}}")
    return ",".join(parts) if len(parts) > 1 else None


def flatten_tree(tree: dict) -> list[dict]:
    """getCategoryTree → Liste mit CATEGORY_FIELDS, Eltern vor Kindern. Die Wurzel selbst (laut eBay
    ohne verlässliche Nummer und Namen) fällt weg; ihre Kinder sind die oberste Ebene."""
    out: list[dict] = []
    root = tree.get("rootCategoryNode") or {}
    stack = [(child, None, 1, n) for n, child in enumerate(root.get("childCategoryTreeNodes") or [])]
    stack.reverse()
    while stack:
        node, parent, level, position = stack.pop()
        cat = node.get("category") or {}
        cid, name = str(cat.get("categoryId") or ""), (cat.get("categoryName") or "").strip()
        if not cid or not name:
            continue
        children = node.get("childCategoryTreeNodes") or []
        out.append({"id": cid, "parent": parent, "name": name, "level": level,
                    "leaf": bool(node.get("leafCategoryTreeNode")) or not children, "position": position})
        stack.extend(reversed([(child, cid, level + 1, n) for n, child in enumerate(children)]))
    return out


# ---------------------------------------------------------------- Plattform

class EbayPlattform(Plattform):
    key = "ebay"
    name = "eBay"
    credentials = (
        Credential("ebay_client_id", "App ID", "Aus dem eBay Developer Program, Keyset „Production“ (Client ID)."),
        Credential("ebay_client_secret", "Cert ID", "Aus demselben Keyset (Client Secret).", secret=True),
    )
    image_hosts = (".ebayimg.com", ".ebaystatic.com")
    auctions = True
    details = True
    categories = True

    def __init__(self) -> None:
        self._clients: dict[object, EbayClient] = {}   # je Konto ein Client mit eigenem Token
        self._pickup_runs: dict[int, float] = {}    # Gesuch → letzte Abholsuche
        self._split_until: dict[tuple, float] = {}  # (Gesuch, Kategorie) → bis dann Auktion und Sofortkauf getrennt
        self._tree_id: str | None = None             # Kategorienbaum des Marktplatzes (eBay.de: 77)

    parse = staticmethod(parse_listing)
    condition_class = staticmethod(condition_class)

    # ------------------------------------------------------------ Zugangsdaten

    @staticmethod
    def _keys(settings: dict) -> tuple[str, str]:
        return ((settings.get("ebay_client_id") or "").strip(),
                geheim.decrypt(settings.get("ebay_client_secret")).strip())

    def configured(self, settings: dict) -> bool:
        return all(self._keys(settings))

    def _client(self, settings: dict) -> EbayClient:
        """Client des Kontos, dem diese Einstellungen gehören, mit dessen Zugangsdaten."""
        uid = settings.get("_user_id")
        client = self._clients.get(uid)
        if client is None:
            client = self._clients[uid] = EbayClient()
        client.use(*self._keys(settings))
        client.user_id = uid
        return client

    @staticmethod
    def _plz(settings: dict) -> str:
        """PLZ für Versandkosten und Entfernung."""
        return (settings.get("plz") or "").strip()

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()

    def forget_pickup(self, search_ids: list[int]) -> None:
        for sid in search_ids:
            self._pickup_runs.pop(sid, None)

    # ------------------------------------------------------------ Filter

    @staticmethod
    def _base_filters(s: dict) -> list[str]:
        f = [f"price:[..{s['max_price']:.2f}]", "priceCurrency:EUR"]
        f += EbayPlattform._condition_filter(s)
        return f

    @staticmethod
    def _condition_filter(s: dict) -> list[str]:
        if s["condition"] == "new":
            return ["conditions:{NEW}"]
        if s["condition"] == "used":
            return ["conditions:{USED}"]
        return []

    @staticmethod
    def _location_filter(s: dict) -> str:
        return {
            "DE": "itemLocationCountry:DE",
            "EU": "itemLocationRegion:EUROPEAN_UNION",
        }.get(s["location"], "itemLocationRegion:WORLDWIDE")

    # ------------------------------------------------------------ Suchen

    @staticmethod
    def _category_params(s: dict, category: str | None) -> dict | None:
        """Zusätzliche Parameter für die Suche in einer Kategorie, mit den Artikelmerkmalen des Gesuchs."""
        if not category:
            return None
        params = {"category_ids": category}
        af = aspect_filter(category, search_aspects(s))
        if af:
            params["aspect_filter"] = af
        return params

    async def _search_shipped(self, client: EbayClient, s: dict, formats: list[str], plz: str,
                              now: float, category: str | None = None, pages: int = 3) -> tuple[list[dict], bool]:
        """Angebote mit Versand nach Deutschland (Strategie siehe oben), höchstens pages Seiten je Format.
        Dazu, ob eBay mehr Treffer hatte (Grenze des Gesuchs erreicht)."""
        def filters(fmts: list[str]) -> list[str]:
            f = self._base_filters(s) + [
                self._location_filter(s),
                "buyingOptions:{" + "|".join(fmts) + "}",
                "deliveryCountry:DE",  # nur Angebote, die nach Deutschland versendet werden
            ]
            if s["free_shipping_only"]:
                f.append("maxDeliveryCost:0")
            return f

        extra = self._category_params(s, category)
        split = (s["id"], category)
        found: list[dict] = []
        capped = False
        if len(formats) > 1 and now >= self._split_until.get(split, 0):
            together = await client.search(s["query"] or None, filters(formats), None, plz, max_pages=1, extra=extra)
            if getattr(together, "complete", True):
                return list(together), False
            self._split_until[split] = now + SPLIT_FOR   # zu viele Treffer: eine Weile gleich getrennt
            found.extend(together)
        for fmt in formats:
            # Bei mehr Treffern als der Grenze zählen die vorne: Auktionen, die zuerst enden, neueste Sofortkäufe
            sort = "endingSoonest" if fmt == "AUCTION" else "newlyListed"
            got = await client.search(s["query"] or None, filters([fmt]), sort, plz, max_pages=pages, extra=extra)
            capped = capped or not getattr(got, "complete", True)
            found.extend(got)
        return found, capped

    async def find(self, s: dict, settings: dict, now: float) -> Found:
        client, plz = self._client(settings), self._plz(settings)
        radius = db.setting_int(settings, "radius_km", 50)
        formats = FORMATS.get(s["format"], ["AUCTION"])
        pickup_wanted = s["pickup_mode"] != "hide" and bool(plz)
        pickup_now = pickup_wanted and (s["last_run"] is None
                                        or now - self._pickup_runs.get(s["id"], 0) >= PICKUP_EVERY)
        # eBay nimmt je Abfrage nur eine Kategorie: mehrere Kategorien = je Kategorie eine eigene Suche.
        # Eine Kategorie schließt ihre Unterkategorien ein.
        categories: list[str | None] = search_categories(s, self.key) or [None]
        pages = max(1, result_limit(s, settings) // PAGE_SIZE)   # Treffergrenze des Gesuchs (Issue #15)
        found: dict[str, tuple[dict, bool]] = {}
        capped = False
        for category in categories:
            shipped, more = await self._search_shipped(client, s, formats, plz, now, category, pages)
            capped = capped or more
            for summary in shipped:
                if summary.get("itemId"):
                    found[summary["itemId"]] = (summary, False)
        if pickup_now:
            search_radius = radius if s["pickup_mode"] == "radius" else PICKUP_RADIUS_ALL_KM
            filters = self._base_filters(s) + [
                "buyingOptions:{" + "|".join(formats) + "}",
                "deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}",
                "pickupCountry:DE",
                f"pickupPostalCode:{plz}",
                f"pickupRadius:{search_radius}",
                "pickupRadiusUnit:km",
            ]
            for category in categories:
                extra = self._category_params(s, category)
                got = await client.search(s["query"] or None, filters, None, plz, max_pages=pages, extra=extra)
                capped = capped or not getattr(got, "complete", True)
                for summary in got:
                    if summary.get("itemId"):   # auch Angebote mit Versand: so kennt Heuhaufen die Entfernung
                        found[summary["itemId"]] = (summary, True)
            self._pickup_runs[s["id"]] = now
        return Found(found, keep_pickup=pickup_wanted and not pickup_now, capped=capped)

    async def fetch(self, item_id: str, settings: dict, purpose: str,
                    pickup: bool = False) -> tuple[dict, dict | None] | None:
        raw = await self._client(settings).get_item(item_id, self._plz(settings), purpose=purpose)
        if raw is None:
            return None
        return parse_listing(raw, pickup), parse_detail(raw)

    async def compare(self, settings: dict, s: dict | None = None, query: str | None = None,
                      catalog: str | None = None) -> tuple[list[dict], bool]:
        if s is not None and not s["query"]:
            return [], True   # ohne Suchbegriff (nur Kategorie, Issue #15) gibt es nichts Vergleichbares
        client, plz = self._client(settings), self._plz(settings)
        if s is not None:
            # wie die Suche des Gesuchs, aber nur Sofortkauf und ohne Höchstpreis
            q, extra = s["query"], None
            filters = ["priceCurrency:EUR", "buyingOptions:{FIXED_PRICE}", "deliveryCountry:DE",
                       self._location_filter(s), *self._condition_filter(s)]
        else:
            # gezielte Abfragen gelten für alle Gesuche: Sofortkauf aus Deutschland, jeder Zustand
            q, extra = (None, {"epid": catalog}) if catalog else (query, None)
            filters = ["priceCurrency:EUR", "buyingOptions:{FIXED_PRICE}", "deliveryCountry:DE",
                       "itemLocationCountry:DE"]
        results = await client.search(q, filters, None, plz, max_pages=1, extra=extra, purpose="durchschnitt")
        complete = len(results) < PAGE_SIZE   # vor dem Filtern: hat eBay alles geliefert?
        return [parse_listing(x) for x in results], complete

    # ------------------------------------------------------------ Kategorien

    async def _tree(self, client: EbayClient) -> tuple[str, str]:
        tree_id, version = await client.default_tree()
        self._tree_id = tree_id
        return tree_id, version

    async def category_version(self, settings: dict) -> str:
        return (await self._tree(self._client(settings)))[1]

    async def category_tree(self, settings: dict) -> tuple[str, list[dict]]:
        client = self._client(settings)
        tree_id, version = await self._tree(client)
        data = await client.category_tree(tree_id)
        return str(data.get("categoryTreeVersion") or version), flatten_tree(data)

    async def category_suggestions(self, settings: dict, query: str) -> list[str]:
        client = self._client(settings)
        tree_id = self._tree_id or (await self._tree(client))[0]
        return [str((x.get("category") or {}).get("categoryId")) for x in await client.category_suggestions(tree_id, query)
                if (x.get("category") or {}).get("categoryId")]

    async def aspect_suggestions(self, settings: dict, query: str, category: str) -> list[tuple[str, list[tuple[str, int]]]]:
        dists = await self._client(settings).aspect_distributions(query or None, category, self._plz(settings))
        out = []
        for d in dists:
            name = (d.get("localizedAspectName") or "").strip()
            values = [((v.get("localizedAspectValue") or "").strip(), int(v.get("matchCount") or 0))
                      for v in d.get("aspectValueDistributions") or []]
            values = sorted([v for v in values if v[0]], key=lambda v: -v[1])
            if name and values:
                out.append((name, values))
        out.sort(key=lambda a: -sum(n for _, n in a[1]))
        return out

    # ------------------------------------------------------------ Bilder, Verbindung

    def image(self, url: str | None, size: int) -> str | None:
        return image_size(url, size)

    async def check(self, settings: dict) -> dict:
        return await self._client(settings).rate_limits()
