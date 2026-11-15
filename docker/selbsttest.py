"""Selbsttest beim Bauen des Images (siehe Dockerfile).

Startet Heuhaufen ohne Netzwerk in einem Wegwerf-Datenordner und prüft die Grundfunktionen mit genau den
Paketen, die im Image installiert sind: Start, Anmeldung, Formulare, Vorlagen, statische Dateien,
Zwei-Faktor-Code, Verschlüsselung und die Schutzschichten (Anmeldepflicht, Größenbegrenzung, Kopfzeilen).
Passt ein Paket nicht zum Code, bricht der Build hier ab. Dann entsteht kein neues Image, und auf dem
Server läuft das bisherige weiter.

Aufruf im Image:  python /tmp/selbsttest.py   (Arbeitsordner /srv, dort liegt app/)
"""
import asyncio
import os
import re
import secrets
import shutil
import sys
import tempfile
from urllib.parse import urlencode

DATA = tempfile.mkdtemp(prefix="heuhaufen-selbsttest-")
PASSWORD = secrets.token_urlsafe(12)
os.environ.update(DATA_DIR=DATA, ADMIN_USER="admin", ADMIN_PASSWORD=PASSWORD)
for name in ("ADMIN_RESET", "FORWARDED_ALLOW_IPS"):
    os.environ.pop(name, None)
sys.path.insert(0, os.getcwd())

import httpx  # noqa: E402
import pyotp  # noqa: E402

from app import auth, db, geheim, main, netz  # noqa: E402

failed = []


def check(label, cond, extra=""):
    print(("ok     " if cond else "FEHLER ") + label, extra if not cond else "")
    if not cond:
        failed.append(label)


def call(method, path, form=None, cookies=None, headers=None, chunks=None, client="127.0.0.1"):
    """Eine Anfrage durch die ganze App (alle Schutzschichten), ohne Server. Liefert (Status, Kopfzeilen, Text)."""
    body = urlencode(form).encode() if form is not None else b""
    head = [(b"host", b"heuhaufen.test")]
    if form is not None:
        head += [(b"content-type", b"application/x-www-form-urlencoded"), (b"content-length", str(len(body)).encode())]
    if chunks is not None:
        head += [(b"content-type", b"application/x-www-form-urlencoded"), (b"transfer-encoding", b"chunked")]
    if cookies:
        head.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    head += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": head,
             "server": ("127.0.0.1", 8000), "client": (client, 50000), "state": {}}
    parts = list(chunks) if chunks is not None else [body]
    sent = []

    async def receive():
        if parts:
            return {"type": "http.request", "body": parts.pop(0), "more_body": bool(parts)}
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)

    async def run():
        await main.app(scope, receive, send)
    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    out_headers = {}
    for key, value in start["headers"]:
        out_headers.setdefault(key.decode().lower(), []).append(value.decode())
    text = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode("utf-8", "replace")
    return start["status"], out_headers, text


def cookie(headers, name):
    for line in headers.get("set-cookie", []):
        if line.startswith(name + "=") and "Max-Age=0" not in line:
            return line.split(";", 1)[0].split("=", 1)[1]
    return None


def login(password):
    status, headers, text = call("GET", "/login")
    token = re.search(r'name="csrf" value="([^"]+)"', text)
    form = {"csrf": token.group(1) if token else "", "username": "admin", "password": password, "next": "/"}
    return call("POST", "/login", form, {auth.LOGIN_CSRF_COOKIE: cookie(headers, auth.LOGIN_CSRF_COOKIE) or ""})


db.init()
auth.ensure_admin()

status, headers, text = call("GET", "/healthz")
check("Lebenszeichen /healthz", status == 200 and text == "ok", status)
status, headers, text = call("GET", "/")
check("ohne Anmeldung zur Anmeldeseite", status == 303 and headers.get("location") == ["/login"], (status, headers.get("location")))
status, headers, text = call("GET", "/login")
check("Anmeldeseite mit Schutz-Kopfzeilen", status == 200 and 'name="csrf"' in text
      and "frame-ancestors 'none'" in headers.get("content-security-policy", [""])[0]
      and headers.get("x-content-type-options") == ["nosniff"] and headers.get("x-frame-options") == ["DENY"], status)
status, headers, text = call("GET", "/static/css/app-min.css")
check("statische Dateien", status == 200 and len(text) > 1000, status)
status, headers, text = login("falsches-passwort")
check("falsches Passwort abgelehnt", status == 401, status)
status, headers, text = login(PASSWORD)
session = cookie(headers, auth.SESSION_COOKIE)
check("Anmeldung", status == 303 and bool(session), status)
jar = {auth.SESSION_COOKIE: session or ""}
status, headers, text = call("GET", "/", cookies=jar)
check("Übersicht", status == 200 and "Heuhaufen" in text, status)
status, headers, text = call("GET", "/settings", cookies=jar)
csrf = re.search(r'name="csrf" value="([^"]+)"', text)
check("Einstellungen", status == 200 and bool(csrf), status)
status, headers, text = call("GET", "/searches/new", cookies=jar)
check("Formular für Gesuche", status == 200 and 'name="max_price"' in text, status)
status, headers, text = call("POST", "/settings/display", {"csrf": csrf.group(1) if csrf else "", "price_with_shipping": "1"}, jar)
check("Formular speichern", status == 303, status)
status, headers, text = call("POST", "/settings/display", {"csrf": "falsch"}, jar)
check("Formular ohne gültiges Token abgelehnt", status == 400, status)
status, headers, text = call("GET", "/searches/+1", cookies=jar)
check("Nummern im Pfad: nur Ziffern", status == 404, status)
status, headers, text = call("GET", "/settings", headers={"host": "x/login?"})
check("gefälschter Host öffnet nichts", status == 303, status)
status, headers, text = call("POST", "/login", chunks=[b"a=" + b"x" * 200000, b"&b=" + b"x" * 200000])
check("zu große Anfrage abgewiesen", status == 413, status)
status, headers, text = call("GET", "/login", headers={"x-forwarded-proto": "https"}, client="203.0.113.9")
check("fremde Proxy-Angaben zählen nicht", "Secure" not in "; ".join(headers.get("set-cookie", [])))
status, headers, text = call("GET", "/login", headers={"x-forwarded-proto": "https"})
check("Proxy-Angaben vom eigenen Rechner zählen", "Secure" in "; ".join(headers.get("set-cookie", [])))

secret = auth.new_totp_secret()
user = db.q1("SELECT * FROM users WHERE role = 'admin'")
check("Zwei-Faktor-Code und QR-Code", auth.verify_totp(user, pyotp.TOTP(secret).now(), secret)
      and auth.totp_qr_data_uri(secret, "admin").startswith("data:image/svg+xml;base64,"))
check("Verschlüsselung der Zugangsdaten", geheim.decrypt(geheim.encrypt("PRD-selbsttest")) == "PRD-selbsttest"
      and geheim.encrypt("PRD-selbsttest") != "PRD-selbsttest")
check("Passwort-Hash (Argon2)", PASSWORD not in user["password_hash"] and auth.verify_password(user, PASSWORD)
      and not auth.verify_password(user, PASSWORD + "x"))


async def http_client():
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False):
        return True
check("HTTP-Client lässt sich anlegen", asyncio.run(http_client()))
check("ntfy-Ziel: Heimnetz für eingeladene Konten gesperrt",
      asyncio.run(netz.ntfy_problem("http://192.168.1.1", False)) is not None
      and asyncio.run(netz.ntfy_problem("http://192.168.1.1", True)) is None)

db._conn.close()
shutil.rmtree(DATA, ignore_errors=True)
if failed:
    print(f"\nSelbsttest fehlgeschlagen ({len(failed)}): " + "; ".join(failed))
    sys.exit(1)
print("\nSelbsttest bestanden.")
