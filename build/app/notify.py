"""Push-Benachrichtigungen über eine ntfy-Instanz (JSON-Veröffentlichung, Umlaute problemlos)."""
from __future__ import annotations

import httpx

from . import db, netz


class NtfyError(Exception):
    pass


def configured(settings: dict) -> bool:
    return bool(settings.get("ntfy_url") and settings.get("ntfy_topic"))


def _is_admin(user_id: int | None) -> bool:
    """Ohne Konto (Installation aus der Zeit vor den Konten) gilt wie für den Admin."""
    if user_id is None:
        return True
    user = db.q1("SELECT role FROM users WHERE id = ?", (user_id,))
    return bool(user and user["role"] == "admin")


async def target_problem(url: str, user_id: int | None) -> str | None:
    """Fehlertext, wenn dieses Konto an diese Adresse nichts schicken darf (netz.ntfy_problem), sonst None.
    Der Admin darf jede Adresse eintragen, eingeladene Konten nur seinen ntfy-Server oder einen öffentlichen."""
    is_admin = _is_admin(user_id)
    admin_url = "" if is_admin else (db.get_settings().get("ntfy_url") or "")
    return await netz.ntfy_problem(url, is_admin, admin_url)


async def send(settings: dict, *, title: str, message: str, click: str | None = None,
               image: str | None = None, priority: int = 3, tags: list[str] | None = None,
               actions: list[dict] | None = None) -> None:
    base = (settings.get("ntfy_url") or "").strip().rstrip("/")
    topic = (settings.get("ntfy_topic") or "").strip()
    if not base or not topic:
        raise NtfyError("ntfy ist noch nicht eingerichtet.")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    # Bei jedem Senden neu prüfen, nicht nur beim Speichern: Die Adresse kann aus einer früheren Version
    # stammen, und ein Rechnername kann inzwischen woandershin zeigen.
    admin = _is_admin(settings.get("_user_id"))
    problem = await target_problem(base, settings.get("_user_id"))
    if problem:
        raise NtfyError(problem)
    if not admin:
        base = netz.canonical(base) or base

    payload: dict = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        payload["tags"] = tags
    if click:
        payload["click"] = click
    if image:
        payload["attach"] = image
        payload["filename"] = "artikel.jpg"
    if actions:
        payload["actions"] = actions

    headers = {}
    token = (settings.get("ntfy_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        # Weiterleitungen folgt Heuhaufen hier nicht: Sonst könnte ein Server die Anfrage samt Token
        # an eine andere Adresse umlenken, zum Beispiel ins Heimnetz.
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            r = await client.post(base, json=payload, headers=headers)
    except httpx.HTTPError as e:
        # Die Art des Fehlers erfährt nur der Admin: Sie verrät, ob unter einer Adresse etwas antwortet
        detail = f" ({e.__class__.__name__})" if admin else ""
        raise NtfyError(f"ntfy unter {base} ist nicht erreichbar{detail}.") from e
    except (UnicodeError, ValueError) as e:   # Adresse oder Token, die sich nicht als Anfrage schreiben lassen
        raise NtfyError(f"Die Anfrage an ntfy unter {base} ließ sich nicht aufbauen.") from e
    if r.status_code in (401, 403):
        raise NtfyError("ntfy lehnt den Zugriff ab. Prüfe den Access-Token und die Rechte für das Topic.")
    if r.status_code >= 300:
        raise NtfyError(f"ntfy antwortet mit Fehler {r.status_code}.")
