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
    ) -> tuple[int, dict]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.api_key:
            headers[key_header] = self.api_key
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            with self._open(req, timeout=self.timeout) as r:
                raw = r.read().decode() or "{}"
                return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {})
        except urllib.error.HTTPError as exc:
            return exc.code, {"error": exc.read().decode()[:200]}
        except urllib.error.URLError as exc:
            raise SourceError(f"fonte inalcançável: {exc.reason}") from exc


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
        """Marca/desmarca. Desmarcar NÃO é `/unbookmark` (404) nem `DELETE`
        (405): é o mesmo `PUT /bookmark` com `{"bookmark": false}` — detalhe que
        não está na documentação e custou uma rodada de tentativa."""
        status_code, _ = self.http.request(
            f"/v1/entries/{entry_id}/bookmark", method="PUT", body={"bookmark": bool(on)}
        )
        return status_code in (200, 204)

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
