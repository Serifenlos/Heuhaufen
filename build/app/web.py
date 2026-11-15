"""Dünne Schicht über Starlette: Routen mit @app.get und @app.post, ohne weiteres Framework.

Heuhaufen braucht vom Web-Framework nur Routen, Formulare, Vorlagen und statische Dateien. Das bringt
Starlette alles mit. Jedes Paket weniger im Image ist eines weniger, das eine Sicherheitslücke haben kann.

Zwei Dinge regelt diese Schicht für alle Routen gleich:

1. Zahlen im Pfad stehen als {sid:nr} in der Route. Dort passen nur 1 bis 15 Ziffern, sonst gibt es die
   Seite nicht (404). Schreibweisen wie „+5“, „5.0“ oder „ 5“ kommen so gar nicht erst bei einer Route an.
2. Jede Route verlangt eine Anmeldung, außer sie ist mit public=True angelegt. Die Anmeldung prüft schon
   guard() in main.py; das hier ist die zweite Sicherung, falls dort je ein Pfad durchrutscht.
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.convertors import Convertor, register_url_convertor
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route


class NumberConvertor(Convertor):
    """Nummer eines Datensatzes im Pfad: nur Ziffern, höchstens 15 (passt sicher in SQLite)."""
    regex = "[0-9]{1,15}"

    def convert(self, value: str) -> int:
        return int(value)

    def to_string(self, value: int) -> str:
        return str(int(value))


register_url_convertor("nr", NumberConvertor())


class App(Starlette):
    def __init__(self, lifespan=None) -> None:
        super().__init__(lifespan=lifespan)

    def _add(self, path: str, methods: list[str], public: bool):
        def register(func):
            async def endpoint(request: Request):
                if not public and getattr(request.state, "session", None) is None:
                    return PlainTextResponse("Nicht angemeldet.", 401)
                return await func(request, **request.path_params)

            endpoint.__name__ = func.__name__
            self.router.routes.append(Route(path, endpoint, methods=methods, name=func.__name__))
            return func

        return register

    def get(self, path: str, public: bool = False):
        return self._add(path, ["GET"], public)

    def post(self, path: str, public: bool = False):
        return self._add(path, ["POST"], public)

    def api_route(self, path: str, methods: list[str], public: bool = False):
        return self._add(path, methods, public)

    def middleware(self, _kind: str):
        def register(func):
            self.add_middleware(BaseHTTPMiddleware, dispatch=func)
            return func

        return register

    def exception_handler(self, exc):
        def register(func):
            self.add_exception_handler(exc, func)
            return func

        return register
