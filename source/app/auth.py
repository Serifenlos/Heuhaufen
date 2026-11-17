"""Anmeldung: Argon2-Passwörter, serverseitige Sitzungen, Sperre nach Fehlversuchen, TOTP-2FA."""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import secrets
import time

import pyotp
import qrcode
import qrcode.image.svg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from . import config, db

log = logging.getLogger("heuhaufen.auth")

SESSION_COOKIE = "hh_session"
PENDING_COOKIE = "hh_pending"
LOGIN_CSRF_COOKIE = "hh_login"
# Sitzungs-Cookie aus der Zeit als „Ebay Watcher“: gilt weiter und wird beim nächsten Aufruf umbenannt
LEGACY_SESSION_COOKIE = "ew_session"

REMEMBER_SECONDS = 90 * 24 * 3600   # "Angemeldet bleiben": 3 Monate
SESSION_SECONDS = 12 * 3600         # normale Sitzung: höchstens 12 Stunden ...
SESSION_IDLE = 60 * 60              # ... und Abmeldung nach 60 Minuten ohne Aktivität
PENDING_SECONDS = 5 * 60            # Zeit für die Eingabe des 2FA-Codes
MIN_PASSWORD = 10
# Platzhalter aus Vorlagen (docker-compose.yml, Anleitungen): Wer sie stehen lässt, hätte ein Passwort, das
# im Internet nachzulesen ist. Solche Werte nimmt Heuhaufen nicht als Passwort an.
PLACEHOLDER_PASSWORDS = frozenset({"passwort-fuer-den-ersten-login", "dein-passwort", "deinpasswort", "changeme123",
                                   "change-me-please", "passwort123", "password123", "1234567890"})

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- Passwörter

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(user: dict | None, password: str) -> bool:
    """Prüft immer einen Hash, auch bei unbekanntem Benutzer (gleiches Zeitverhalten)."""
    stored = user["password_hash"] if user else _DUMMY_HASH
    try:
        ok = _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        ok = False
    if ok and user and _hasher.check_needs_rehash(stored):
        db.ex("UPDATE users SET password_hash = ? WHERE id = ?", (_hasher.hash(password), user["id"]))
    return ok and user is not None


def usable_password(password: str) -> bool:
    """Taugt als Passwort aus der Docker-Konfiguration: lang genug und kein Platzhalter aus einer Vorlage."""
    return len(password) >= MIN_PASSWORD and password.strip().lower() not in PLACEHOLDER_PASSWORDS


def ensure_admin() -> None:
    """Beim allerersten Start das Konto des Admins anlegen (ADMIN_USER, ADMIN_PASSWORD).
    ADMIN_RESET gilt nur für dieses Konto."""
    existing = db.q1("SELECT id, username FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
    if existing:
        if config.ADMIN_RESET:
            if not usable_password(config.ADMIN_PASSWORD):
                log.error("ADMIN_RESET ist gesetzt, aber ADMIN_PASSWORD fehlt, hat weniger als %d Zeichen oder ist "
                          "der Platzhalter aus der Vorlage. Das Passwort bleibt, wie es war.", MIN_PASSWORD)
                return
            db.ex("UPDATE users SET password_hash = ?, totp_enabled = 0, totp_secret = NULL WHERE id = ?",
                  (hash_password(config.ADMIN_PASSWORD), existing["id"]))
            db.ex("DELETE FROM sessions WHERE user_id = ?", (existing["id"],))
            log.warning("Passwort für '%s' zurückgesetzt, 2FA ausgeschaltet. Entferne ADMIN_RESET jetzt wieder.",
                        existing["username"])
        return
    password = config.ADMIN_PASSWORD
    if not usable_password(password):
        reason = ("Kein ADMIN_PASSWORD gesetzt." if not password else
                  f"ADMIN_PASSWORD hat weniger als {MIN_PASSWORD} Zeichen oder ist der Platzhalter aus der Vorlage "
                  "und wird nicht verwendet.")
        password = secrets.token_urlsafe(12)
        log.warning("%s Erstes Passwort für '%s': %s  (bitte gleich ändern)", reason, config.ADMIN_USER, password)
    db.ex("INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
          (config.ADMIN_USER, hash_password(password), time.time()))
    log.info("Benutzerkonto '%s' angelegt.", config.ADMIN_USER)


# ---------------------------------------------------------------- Konten

INVITE_SECONDS = 7 * 86400      # Einladung gilt eine Woche …
RESET_SECONDS = 24 * 3600       # … der Link zum Zurücksetzen einen Tag, beide nur einmal
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def find_login(name: str) -> dict | None:
    """Konto zum Anmeldenamen: Benutzername oder E-Mail-Adresse."""
    name = (name or "").strip()
    if not name:
        return None
    return db.q1("SELECT * FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE "
                 "ORDER BY username = ? COLLATE NOCASE DESC LIMIT 1", (name, name, name))


def check_username(name: str, exclude_id: int | None = None) -> str | None:
    """Fehlertext oder None."""
    if not USERNAME.match(name or ""):
        return "3 bis 40 Zeichen: Buchstaben, Ziffern, Punkt, Binde- oder Unterstrich, ohne Leerzeichen."
    row = db.q1("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (name,))
    if row and row["id"] != exclude_id:
        return "Diesen Benutzernamen gibt es schon."
    return None


def check_email(email: str, exclude_id: int | None = None) -> str | None:
    if not EMAIL.match(email or ""):
        return "Bitte eine E-Mail-Adresse wie name@beispiel.de eingeben."
    row = db.q1("SELECT id FROM users WHERE email = ? COLLATE NOCASE", (email,))
    if row and row["id"] != exclude_id:
        return "Für diese Adresse gibt es schon ein Konto."
    return None


def check_password(password: str, repeat: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Mindestens {MIN_PASSWORD} Zeichen."
    if password != repeat:
        return "Die beiden Eingaben stimmen nicht überein."
    return None


def create_token(kind: str, created_by: int, email: str | None = None, user_id: int | None = None) -> tuple[str, float]:
    """Neuer Einladungs- bzw. Zurücksetzen-Link. Ein älterer für dieselbe Adresse bzw. dasselbe Konto
    wird ungültig. Liefert (Geheimnis für den Link, gültig bis)."""
    now = time.time()
    if kind == "invite":
        db.ex("DELETE FROM user_tokens WHERE kind = 'invite' AND used_at IS NULL AND email = ? COLLATE NOCASE", (email,))
    else:
        db.ex("DELETE FROM user_tokens WHERE kind = 'reset' AND used_at IS NULL AND user_id = ?", (user_id,))
    token = secrets.token_urlsafe(32)
    expires = now + (INVITE_SECONDS if kind == "invite" else RESET_SECONDS)
    db.ex("INSERT INTO user_tokens(token_hash, kind, email, user_id, created_by, created_at, expires_at) "
          "VALUES (?, ?, ?, ?, ?, ?, ?)", (_digest(token), kind, email, user_id, created_by, now, expires))
    return token, expires


def load_token(token: str | None, kind: str) -> dict | None:
    """Gültiger, noch nicht benutzter Link oder None."""
    if not token or len(token) > 200:
        return None
    row = db.q1("SELECT * FROM user_tokens WHERE token_hash = ? AND kind = ?", (_digest(token), kind))
    if not row or row["used_at"] or row["expires_at"] < time.time():
        return None
    return row


def use_token(row: dict, user_id: int | None = None) -> None:
    db.ex("UPDATE user_tokens SET used_at = ?, user_id = COALESCE(?, user_id) WHERE id = ?",
          (time.time(), user_id, row["id"]))


def create_user(username: str, email: str, password: str) -> int:
    return db.ex("INSERT INTO users(username, password_hash, email, role, created_at) VALUES (?, ?, ?, 'user', ?)",
                 (username, hash_password(password), email, time.time()))


def reset_password(user_id: int, password: str) -> None:
    """Neues Passwort, Zwei-Faktor-Anmeldung aus, alle Geräte abgemeldet (Link vom Admin)."""
    db.ex("UPDATE users SET password_hash = ?, totp_enabled = 0, totp_secret = NULL WHERE id = ?",
          (hash_password(password), user_id))
    db.ex("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------- Sperre nach Fehlversuchen

class Throttle:
    """Zählt Fehlversuche der letzten Viertelstunde je Adresse und je Benutzername, falsche Codes der
    Zwei-Faktor-Anmeldung zusätzlich je Konto. Die Grenze je Benutzername gilt unabhängig von der Adresse:
    Sie hält auch dann, wenn jemand seine Adresse wechselt oder fälscht."""
    WINDOW = 15 * 60
    MAX_PER_IP = 5
    MAX_PER_USER = 20
    MAX_CODES = 5       # falsche 2FA-Codes je Konto (ein Code hat nur sechs Ziffern)
    MAX_KEYS = 5000     # mehr Adressen und Namen merkt sich die Sperre nicht (Schutz vor vollem Speicher)

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.time()
        recent = [t for t in self._fails.get(key, []) if now - t < self.WINDOW]
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return recent

    def wait_seconds(self, ip: str, user: str, codes: bool = False) -> int:
        limits = [(f"ip:{ip}", self.MAX_PER_IP), (f"user:{user.lower()}", self.MAX_PER_USER)]
        if codes:
            limits.append((f"code:{user.lower()}", self.MAX_CODES))
        for key, limit in limits:
            recent = self._recent(key)
            if len(recent) >= limit:
                return int(self.WINDOW - (time.time() - recent[0])) + 1
        return 0

    def fail(self, ip: str, user: str, code: bool = False) -> None:
        now = time.time()
        keys = [f"ip:{ip}", f"user:{user.lower()}"] + ([f"code:{user.lower()}"] if code else [])
        for key in keys:
            self._fails.setdefault(key, []).append(now)
        if len(self._fails) > self.MAX_KEYS:
            self._prune(now)

    def _prune(self, now: float) -> None:
        """Abgelaufene Einträge entfernen. Sind es danach immer noch zu viele, die ältesten."""
        for key in [k for k, times in self._fails.items() if not times or now - times[-1] >= self.WINDOW]:
            del self._fails[key]
        if len(self._fails) > self.MAX_KEYS:
            by_age = sorted(self._fails, key=lambda k: self._fails[k][-1])
            for key in by_age[: len(self._fails) - self.MAX_KEYS // 2]:
                del self._fails[key]

    def reset(self, ip: str, user: str | None = None) -> None:
        """Nach einer gelungenen Anmeldung: Fehlversuche dieser Adresse vergessen, mit user auch die
        falschen 2FA-Codes des Kontos."""
        self._fails.pop(f"ip:{ip}", None)
        if user is not None:
            self._fails.pop(f"code:{user.lower()}", None)


throttle = Throttle()


# ---------------------------------------------------------------- Sitzungen

def create_session(user_id: int, remember: bool, user_agent: str, pending: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    lifetime = PENDING_SECONDS if pending else (REMEMBER_SECONDS if remember else SESSION_SECONDS)
    db.ex(
        "INSERT INTO sessions(token_hash, user_id, csrf, remember, pending_2fa, created_at, last_seen, "
        "expires_at, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_digest(token), user_id, secrets.token_urlsafe(24), int(remember), int(pending),
         now, now, now + lifetime, (user_agent or "")[:300]),
    )
    return token


def session_token(cookies) -> tuple[str | None, bool]:
    """Sitzungs-Token aus den Cookies und ob es noch unter dem alten Namen steht."""
    token = cookies.get(SESSION_COOKIE)
    if token:
        return token, False
    legacy = cookies.get(LEGACY_SESSION_COOKIE)
    return legacy, bool(legacy)


def load_session(token: str | None, pending: bool = False) -> dict | None:
    if not token:
        return None
    row = db.q1("SELECT * FROM sessions WHERE token_hash = ? AND pending_2fa = ?", (_digest(token), int(pending)))
    if not row:
        return None
    now = time.time()
    idle_expired = not row["remember"] and not pending and now - row["last_seen"] > SESSION_IDLE
    if row["expires_at"] < now or idle_expired:
        db.ex("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return None
    if now - row["last_seen"] > 60:
        db.ex("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, row["token_hash"]))
    return row


def destroy_session(token: str | None) -> None:
    if token:
        db.ex("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))


def destroy_other_sessions(user_id: int, keep_hash: str | None) -> None:
    db.ex("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep_hash or ""))


def mask_csrf(token: str) -> str:
    """Formular-Token für die Seite, bei jeder Ausgabe anders verpackt: zufällige Maske + (Token XOR Maske).
    Heuhaufen komprimiert seine Seiten (gzip). Stünde das Token immer gleich im Text, ließe es sich über die
    Größe komprimierter Antworten erraten (BREACH). So steht es in keiner Antwort zweimal gleich da."""
    raw = token.encode()
    mask = secrets.token_bytes(len(raw))
    return base64.urlsafe_b64encode(mask + bytes(a ^ b for a, b in zip(raw, mask))).decode().rstrip("=")


def _unmask_csrf(sent: str) -> str | None:
    try:
        data = base64.urlsafe_b64decode(sent + "=" * (-len(sent) % 4))
    except (ValueError, TypeError):
        return None
    half = len(data) // 2
    if not data or len(data) % 2:
        return None
    try:
        return bytes(a ^ b for a, b in zip(data[half:], data[:half])).decode()
    except UnicodeDecodeError:
        return None


def csrf_ok(sent: object, expected: str | None) -> bool:
    """Passt das gesendete Formular-Token (verpackt, siehe mask_csrf) zum erwarteten?"""
    if not expected or not isinstance(sent, str) or len(sent) > 200:
        return False
    unmasked = _unmask_csrf(sent)
    return unmasked is not None and secrets.compare_digest(unmasked.encode(), expected.encode())


# ---------------------------------------------------------------- Zwei-Faktor (TOTP)

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_qr_data_uri(secret: str, username: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Heuhaufen")
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def verify_totp(user: dict, code: str, secret: str | None = None) -> bool:
    secret = secret or user.get("totp_secret")
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not secret or len(code) != 6:
        return False
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        return False
    stamp = f"{code}:{int(time.time() // 30)}"
    last = user.get("totp_last_code") or ""
    if last.split(":")[0] == code and abs(int(last.split(":")[1]) - int(time.time() // 30)) <= 2:
        return False  # denselben Code nicht zweimal akzeptieren
    db.ex("UPDATE users SET totp_last_code = ? WHERE id = ?", (stamp, user["id"]))
    return True
