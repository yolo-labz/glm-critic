"""Fontes e destinos — o adaptador que mantém o núcleo ignorante de HTTP.

O núcleo do critic não sabe o que é um leitor RSS. Ele recebe dicionários e
devolve vereditos; quem fala HTTP é a fonte. Isso é o que permite testar o
julgamento sem rede e trocar Miniflux por outra coisa sem tocar na rubrica.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime


class SourceError(RuntimeError):
    """A fonte recusou, sumiu ou respondeu algo que não dá para interpretar."""


class HttpSource:
    """Cliente HTTP mínimo, com a única sutileza que importa aqui.

    **User-Agent não é detalhe.** Um leitor atrás do Cloudflare responde
    `403 código 1010` ("banned based on your browser's signature") ao
    User-Agent padrão das bibliotecas — a API fica inalcançável por script e o
    erro não diz por quê. O UA é configurável e tem um padrão honesto.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        user_agent: str = "",
        timeout: int = 45,
        opener=urllib.request.urlopen,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_agent = user_agent or "glm-critic"
        self.timeout = timeout
        self._open = opener

    def request(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        key_header: str = "X-Auth-Token",
        form: dict | None = None,
    ) -> tuple[int, dict]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.api_key and form is None:
            headers[key_header] = self.api_key
        data = json.dumps(body).encode() if body is not None else None
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            method=method,
            data=data,
            headers=headers,
        )
        try:
            with self._open(req, timeout=self.timeout) as r:
                raw = r.read().decode() or "{}"
                return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {})
        except urllib.error.HTTPError as exc:
            return exc.code, {"error": exc.read().decode()[:200]}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SourceError("fonte inalcançável ou resposta inválida") from exc


class MinifluxSource:
    """Leitor RSS self-hosted. Uma entrada vira um dict normalizado."""

    def __init__(self, http: HttpSource):
        self.http = http

    def entries(
        self,
        status: str = "unread",
        limit: int = 120,
        order: str = "published_at",
        direction: str = "desc",
    ) -> tuple[list[dict], int]:
        qs = urllib.parse.urlencode(
            {"status": status, "limit": limit, "order": order, "direction": direction}
        )
        status_code, d = self.http.request(f"/v1/entries?{qs}")
        if status_code != 200:
            raise SourceError(f"miniflux {status_code}: {d}")
        return [normalize_entry(e) for e in (d.get("entries") or [])], int(d.get("total") or 0)

    def star(self, entry_id: int, on: bool = True) -> bool:
        """Miniflux PUT /bookmark toggles; its handler ignores the request body.

        Read first and verify after toggling. One critic writer is required;
        Miniflux exposes no compare-and-swap against a concurrent UI change.
        """
        path = f"/v1/entries/{entry_id}"
        code, entry = self.http.request(path)
        if code != 200 or not isinstance(entry.get("starred"), bool):
            raise SourceError("Miniflux não informou o estado da estrela")
        if entry["starred"] == on:
            return True
        status_code, _ = self.http.request(path + "/bookmark", method="PUT")
        if status_code not in (200, 204):
            return False
        code, entry = self.http.request(path)
        return code == 200 and entry.get("starred") == on

    def mark_category_read(self, category_id: int) -> bool:
        status_code, _ = self.http.request(
            f"/v1/categories/{category_id}/mark-all-as-read", method="PUT"
        )
        return status_code in (200, 204)

    def categories(self) -> list[dict]:
        status_code, d = self.http.request("/v1/categories")
        if status_code != 200:
            raise SourceError(f"miniflux {status_code}: {d}")
        return d if isinstance(d, list) else []


class FreshRSSSource:
    """Native Fever API; SOURCE_API_KEY is md5(username:API-password).

    ponytail: unread-only is the intake contract, not a complete Fever client.
    Read/archive migration belongs to FreshRSS exports, not the critic.
    """

    def __init__(self, http: HttpSource):
        self.http = http

    def _request(self, **params) -> dict:
        code, data = self.http.request(
            "/api/fever.php?api", method="POST",
            form={"api_key": self.http.api_key, **params},
        )
        if code != 200 or not isinstance(data, dict) or data.get("auth") != 1:
            raise SourceError(f"FreshRSS Fever recusou a chamada (HTTP {code})")
        return data

    def feeds(self) -> list[dict]:
        return self._request(feeds="").get("feeds", [])

    def entries(self, status: str = "unread", limit: int = 120) -> tuple[list[dict], int]:
        if status != "unread":
            raise SourceError("FreshRSS intake suporta somente status=unread")
        if not 1 <= limit <= 1000:
            raise SourceError("limit deve estar entre 1 e 1000")
        data = self._request(unread_item_ids="", feeds="")
        try:
            ids = sorted({int(i) for i in data["unread_item_ids"].split(",") if i}, reverse=True)
            feeds = {int(f["id"]): f["title"] for f in data["feeds"]}
            items = []
            selected = ids[:limit]
            for offset in range(0, len(selected), 50):
                batch = selected[offset:offset + 50]
                page = self._request(items="", with_ids=",".join(map(str, batch)))
                for e in page["items"]:
                    if int(e["id"]) not in batch or e.get("is_read"):
                        continue  # The user may have read it since the ID snapshot.
                    items.append({
                        "id": int(e["id"]), "title": e.get("title", "").strip(),
                        "url": e.get("url", ""), "content": e.get("html", ""),
                        "published_at": datetime.fromtimestamp(
                            int(e["created_on_time"]), UTC
                        ).isoformat(),
                        "feed": feeds.get(int(e["feed_id"]), ""),
                        "hash": f"freshrss:{e['id']}", "starred": bool(e.get("is_saved")),
                    })
            return sorted(items, key=lambda e: e["published_at"], reverse=True), len(ids)
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
            raise SourceError("resposta Fever inválida") from exc

    def star(self, entry_id: int, on: bool = True) -> bool:
        data = self._request(mark="item", **{"as": "saved" if on else "unsaved"}, id=entry_id)
        saved = {i for i in data.get("saved_item_ids", "").split(",") if i}
        return (str(entry_id) in saved) == on


def source_for(settings):
    http = HttpSource(
        settings.source_url, settings.source_key, settings.user_agent, settings.source_timeout
    )
    if settings.source_type == "freshrss":
        return FreshRSSSource(http)
    if settings.source_type == "miniflux":
        return MinifluxSource(http)
    raise SourceError("tipo de fonte desconhecido")


def normalize_entry(e: dict) -> dict:
    """Achata uma entrada do leitor na forma que o núcleo entende.

    O núcleo não deve conhecer o vocabulário de nenhuma fonte específica; se
    conhecer, trocar de leitor vira reescrever o julgamento.
    """
    feed = e.get("feed") or {}
    return {
        "id": e.get("id"),
        "title": (e.get("title") or "").strip(),
        "url": e.get("url") or "",
        "content": e.get("content") or "",
        "published_at": e.get("published_at") or "",
        "feed": feed.get("title") or feed.get("feed_url") or "",
        "hash": str(e.get("hash") or e.get("id") or ""),
        "starred": bool(e.get("starred")),
    }


def strip_html(text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())
