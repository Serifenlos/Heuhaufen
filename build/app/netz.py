"""Netz-Schutz: wem Heuhaufen die Angaben eines Reverse Proxys glaubt, wie groß eine Anfrage sein darf und
wohin ein Konto Benachrichtigungen schicken darf.

Proxy-Angaben (TrustedProxy)
    Hinter einem Reverse Proxy (Synology, nginx, Traefik) sieht Heuhaufen nur dessen Adresse. Die echte
    Adresse der Besucherin und „kam über HTTPS“ stehen in X-Forwarded-For und X-Forwarded-Proto. Diese
    Kopfzeilen kann aber jeder mitschicken. Heuhaufen glaubt sie deshalb nur von Adressen, die als Proxy
    gelten (FORWARDED_ALLOW_IPS):
      nicht gesetzt   der Container selbst und sein Docker-Gateway (über das kommt der Reverse Proxy
                      der Synology an)
      "none"          niemandem
      "10.0.0.5, 172.20.0.0/16"   genau diesen Adressen und Netzen
      "*"             allen (nur sinnvoll, wenn wirklich jede Anfrage über einen Proxy kommt)

Größe (BodyLimit)
    Formulare von Heuhaufen sind wenige Kilobyte groß. Alles über MAX_BODY wird abgewiesen, bevor es
    gelesen oder ausgewertet wird.

Ziel der Benachrichtigungen (ntfy_problem)
    Eingeladene Konten tragen ihre eigene ntfy-Adresse ein. Ohne Prüfung ließe sich Heuhaufen so als
    Sprungbrett ins Heimnetz benutzen (Anfragen an Router, NAS, andere Container). Erlaubt sind deshalb für
    sie nur der ntfy-Server des Admins oder ein öffentlicher Server über https://. Der Admin darf jede
    Adresse eintragen, ihm gehört die Installation.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import struct
from urllib.parse import quote, urlsplit

log = logging.getLogger("heuhaufen.netz")

MAX_BODY = 256 * 1024   # Bytes je Anfrage


# ---------------------------------------------------------------- Proxy-Angaben

def _in_container() -> bool:
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def default_gateways(route_file: str = "/proc/net/route") -> list[str]:
    """Standard-Gateways (IPv4) aus der Routing-Tabelle. Im Docker-Netz ist das die Adresse, unter der
    der Host und damit sein Reverse Proxy beim Container ankommt."""
    found = []
    try:
        with open(route_file, encoding="ascii") as f:
            for line in list(f)[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "00000000" and int(parts[3], 16) & 2:   # RTF_GATEWAY
                    found.append(socket.inet_ntoa(struct.pack("<L", int(parts[2], 16))))
    except (OSError, ValueError, struct.error):
        pass
    return found


def trusted_proxies(value: str | None = None) -> tuple[bool, list]:
    """(allen glauben?, Netze, deren Proxy-Angaben gelten). value: Inhalt von FORWARDED_ALLOW_IPS."""
    raw = (os.environ.get("FORWARDED_ALLOW_IPS", "") if value is None else value).strip()
    if raw == "*":
        return True, []
    if raw.lower() in ("", "auto"):
        entries = ["127.0.0.0/8", "::1"] + (default_gateways() if _in_container() else [])
    elif raw.lower() in ("none", "keine", "off", "aus", "0", "-"):
        entries = []
    else:
        entries = [e.strip() for e in raw.split(",") if e.strip()]
    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            log.warning("FORWARDED_ALLOW_IPS: „%s“ ist keine Adresse und kein Netz, wird übersprungen.", entry)
    return False, networks


def _address(text: str):
    """IP-Adresse aus einem Eintrag wie „1.2.3.4“, „1.2.3.4:5678“ oder „[2001:db8::1]:443“, sonst None."""
    text = text.strip()
    if text.startswith("["):
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


class TrustedProxy:
    """Übernimmt X-Forwarded-For und X-Forwarded-Proto, aber nur von Adressen, die als Proxy gelten.
    Danach stehen in scope["client"] und scope["scheme"] die Angaben, auf die sich der Rest verlässt
    (Sperre nach Fehlversuchen, Secure-Flag der Cookies)."""

    def __init__(self, app, allow: str | None = None) -> None:
        self.app = app
        self.trust_all, self.networks = trusted_proxies(allow)
        self._ignored: set[str] = set()

    def describe(self) -> str:
        if self.trust_all:
            return "allen Adressen (FORWARDED_ALLOW_IPS=*)"
        return ", ".join(str(n) if n.num_addresses > 1 else str(n.network_address) for n in self.networks) or "niemandem"

    def trusted(self, host: str | None) -> bool:
        if self.trust_all:
            return True
        ip = _address(host or "")
        return ip is not None and any(ip.version == n.version and ip in n for n in self.networks)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            client = scope.get("client")
            peer = client[0] if client else None
            forwarded_for, proto = [], None
            for name, value in scope.get("headers", ()):
                if name == b"x-forwarded-for":
                    forwarded_for.extend(part for part in value.decode("latin-1").split(",") if part.strip())
                elif name == b"x-forwarded-proto":
                    proto = value.decode("latin-1").split(",")[0].strip().lower()
            if forwarded_for or proto:
                if self.trusted(peer):
                    self._apply(scope, forwarded_for, proto)
                elif peer and peer not in self._ignored and len(self._ignored) < 20:
                    self._ignored.add(peer)
                    log.info("X-Forwarded-Angaben von %s ignoriert: Die Adresse gilt nicht als Proxy "
                             "(FORWARDED_ALLOW_IPS).", peer)
        await self.app(scope, receive, send)

    def _apply(self, scope, forwarded_for: list[str], proto: str | None) -> None:
        if proto in ("http", "https"):
            scope["scheme"] = proto
        # Von rechts lesen: Jeder Proxy hängt die Adresse an, von der er die Anfrage bekam. Der erste
        # Eintrag, der selbst kein Proxy ist, ist die Besucherin. Alles links davon kann erfunden sein.
        chosen = None
        for entry in reversed(forwarded_for):
            ip = _address(entry)
            if ip is None:
                break
            chosen = str(ip)
            if not self.trusted(chosen):
                break
        if chosen:
            scope["client"] = (chosen, 0)


# ---------------------------------------------------------------- Größe einer Anfrage

async def _plain(send, status: int, text: str) -> None:
    body = text.encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode()),
        (b"x-content-type-options", b"nosniff"), (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


class BodyLimit:
    """Weist Anfragen über max_bytes ab, bevor Heuhaufen sie auswertet.

    Mit angekündigter Länge (Content-Length, so schicken Browser Formulare) entscheidet die Ankündigung;
    mehr als angekündigt nimmt der Server gar nicht an. Stückweise gesendete Anfragen (Transfer-Encoding:
    chunked) kündigen nichts an: Die liest BodyLimit selbst bis zur Grenze und reicht sie erst weiter, wenn
    sie vollständig und klein genug sind."""

    def __init__(self, app, max_bytes: int = MAX_BODY) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", ())
        if not any(k == b"transfer-encoding" for k, _ in headers):
            declared = next((v for k, v in headers if k == b"content-length"), None)
            if declared is not None and not declared.isdigit():
                await _plain(send, 400, "Ungültige Anfrage.")
            elif declared is not None and int(declared) > self.max_bytes:
                await _plain(send, 413, "Die Anfrage ist zu groß.")
            else:
                await self.app(scope, receive, send)
            return

        chunks, seen = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":   # Verbindung beendet, bevor alles da war
                return
            body = message.get("body") or b""
            seen += len(body)
            if seen > self.max_bytes:
                await _plain(send, 413, "Die Anfrage ist zu groß.")
                return
            chunks.append(body)
            if not message.get("more_body"):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, replay, send)


# ---------------------------------------------------------------- Ziel der Benachrichtigungen

NOT_ALLOWED = ("Diese Adresse ist für dein Konto nicht erlaubt. Möglich sind der ntfy-Server dieser "
               "Heuhaufen-Installation oder ein öffentlicher Server mit https://, zum Beispiel https://ntfy.sh.")
NAT64 = ipaddress.ip_network("64:ff9b::/96")
HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9._-]{0,251}[a-z0-9])?")


def origin(url: str) -> tuple[str, str, int] | None:
    """(Protokoll, Rechnername, Port) einer http(s)-Adresse, sonst None."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    return scheme, parts.hostname.lower().rstrip("."), port or (443 if scheme == "https" else 80)


def has_credentials(url: str) -> bool:
    """Adresse mit Benutzername oder Passwort (https://name:passwort@…)."""
    try:
        parts = urlsplit((url or "").strip())
        return parts.username is not None or parts.password is not None
    except ValueError:
        return False


def is_public(address: str) -> bool:
    """Öffentliche Adresse im Internet? Heimnetz, der eigene Rechner, Docker-Netze, Link-Local und
    ähnliche Bereiche sind es nicht, auch nicht in IPv6 verpackt."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and ip in NAT64:
            inner = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
        if inner is not None:
            ip = inner
    return ip.is_global


async def resolve(host: str, port: int) -> list[str]:
    """Alle Adressen, auf die der Rechnername zeigt."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), 5)
    return sorted({info[4][0] for info in infos})


def canonical(url: str) -> str | None:
    """Die Adresse, neu zusammengesetzt nur aus den geprüften Teilen: Protokoll, Rechnername, Port, Pfad.
    Zugangsdaten, Abfrage und Anker fallen weg. So schickt Heuhaufen die Anfrage sicher an genau den
    Rechner, den ntfy_problem geprüft hat, egal wie eine Adresse geschrieben ist."""
    target = origin(url)
    if target is None:
        return None
    scheme, host, port = target
    if ":" in host:
        host = f"[{host}]"
    if port != (443 if scheme == "https" else 80):
        host = f"{host}:{port}"
    path = quote(urlsplit(url.strip()).path.rstrip("/"), safe="/%-._~")
    return f"{scheme}://{host}{path}"


async def ntfy_problem(url: str, is_admin: bool, admin_url: str = "", resolver=resolve) -> str | None:
    """Fehlertext, wenn dieses Konto an diese ntfy-Adresse nichts schicken darf, sonst None."""
    target = origin(url)
    if target is None:
        return "Die Adresse beginnt mit https://, zum Beispiel https://ntfy.deine-domain.de."
    if is_admin:
        return None
    if has_credentials(url):
        return "Zugangsdaten gehören nicht in die Adresse. Trage stattdessen unten einen Access-Token ein."
    if not HOSTNAME.fullmatch(target[1]) and _address(f"[{target[1]}]") is None:
        return "Der Rechnername in der Adresse enthält Zeichen, die dort nicht vorkommen dürfen."
    if admin_url and target == origin(admin_url):
        return None
    if target[0] != "https":
        return NOT_ALLOWED
    if _address(f"[{target[1]}]") is not None:   # schon eine IP-Adresse
        addresses = [target[1]]
    else:
        try:
            addresses = await resolver(target[1], target[2])
        except (OSError, asyncio.TimeoutError):
            return "Diese Adresse lässt sich gerade nicht auflösen. Prüfe die Schreibweise."
    if not addresses or not all(is_public(a) for a in addresses):
        return NOT_ALLOWED
    return None
