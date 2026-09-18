"""Serviço HTTP — a superfície que o n8n chama.

Por que um serviço e não só uma CLI: o n8n orquestra bem agendamento, retry,
entrega e notificação, mas não roda Python dentro de um container que não é
dele. Um endpoint pequeno e autenticado é a menor costura entre os dois, e
mantém a lógica testável fora do n8n.

Só stdlib, de propósito: o container fica pequeno e a superfície de ataque
também. Duas rotas, e nada mais:

    GET  /health   → 200 sem autenticação (é o que o health check do Dokku usa)
    POST /run      → exige o token; roda a crítica e devolve o resultado

**Sem token ele não sobe.** Um serviço que decide em qual rede está para saber
se precisa de autenticação acaba sem autenticação nenhuma; aqui o contrato é
explícito e falha alto.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Settings
from .critique import critique
from .judge import JudgeError
from .rubric import Rubric, rubric_for
from .sources import HttpSource, MinifluxSource, SourceError
from .store import VerdictLog

MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "glm-critic"
    sys_version = ""

    # O servidor injeta estes na subclasse via fábrica abaixo.
    settings: Settings
    rubric: Rubric

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = self.headers.get("X-Critic-Token") or ""
        expected = self.settings.service_token
        # compare_digest evita vazar o tamanho do segredo pelo tempo de resposta
        return bool(expected) and hmac.compare_digest(token, expected)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/health", ""):
            self._json(200, {"ok": True, "model": self.settings.judge_model})
            return
        self._json(404, {"ok": False, "error": "rota inexistente"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/run":
            self._json(404, {"ok": False, "error": "rota inexistente"})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "token ausente ou inválido"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._json(413, {"ok": False, "error": "corpo grande demais"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            opts = json.loads(raw or b"{}")
            if not isinstance(opts, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"ok": False, "error": "corpo precisa ser um objeto JSON"})
            return

        s = self.settings
        if opts.get("limit"):
            s = Settings(**{**s.__dict__, "extra": {**s.extra, "limit": int(opts["limit"])}})
        try:
            payload = run_once(
                s,
                self.rubric,
                star=bool(opts.get("star")) and not opts.get("dry_run"),
                status=str(opts.get("status") or "unread"),
                limit=int(opts.get("limit") or 120),
            )
        except SourceError as exc:
            self._json(502, {"ok": False, "error": str(exc)})
            return
        self._json(200, payload)
        return

        self._json(200, payload)

    def log_message(self, fmt: str, *args) -> None:
        # O log padrão vai para stderr com o caminho da requisição, que é o que
        # o `dokku logs` mostra. Silenciar seria perder a única trilha do serviço.
        logging.info("%s %s", self.address_string(), fmt % args)


def run_once(
    settings: Settings,
    rubric: Rubric,
    *,
    star: bool = True,
    status: str = "unread",
    limit: int = 80,
) -> dict:
    """Um ciclo completo: buscar, julgar, marcar. Devolve o payload do resultado.

    Existe separado do handler HTTP porque o modo autônomo (agendar por dentro)
    e o modo sob demanda precisam do mesmo caminho — duas implementações
    divergiriam na primeira correção.
    """
    http = HttpSource(
        settings.source_url, settings.source_key, settings.user_agent, settings.source_timeout
    )
    source = MinifluxSource(http)
    items, total = source.entries(status=status, limit=limit)
    res = critique(items, settings, rubric, VerdictLog(settings.log_path))
    payload = res.as_dict()
    payload["considered_total_at_source"] = total
    payload["ok"] = True
    if star:
        payload["starred"] = sum(
            1 for v in res.signals if v.item_id is not None and source.star(int(v.item_id))
        )
    return payload


def notify(url: str, payload: dict, timeout: int = 30) -> bool:
    """Empurra o resultado para quem orquestra a entrega.

    A direção importa: o critic **chama** o n8n em vez de ser chamado. O node
    HTTP do n8n recusa hostname interno com `Invalid URL`, e expor o serviço de
    IA para contornar isso troca um problema de configuração por superfície de
    ataque. Chamando daqui, a URL interna é só uma URL.
    """
    if not url:
        return False
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "glm-critic"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        logging.error("notify falhou: %s", exc)
        return False


def _scheduler(settings: Settings, rubric: Rubric, stop: threading.Event) -> None:
    """Roda o ciclo a cada CRITIC_EVERY_SECONDS e empurra para NOTIFY_URL."""
    while not stop.wait(settings.every_seconds):
        try:
            payload = run_once(settings, rubric)
            logging.info(
                "ciclo: julgados=%s sinais=%s", payload.get("judged"), payload.get("signals")
            )
            if payload.get("signals"):
                notify(settings.notify_url, payload)
        except (SourceError, JudgeError, OSError, ValueError) as exc:
            # Um ciclo ruim não pode matar o serviço: registra e tenta no próximo.
            logging.error("ciclo falhou: %s", exc)


def make_server(settings: Settings, host: str, port: int, rubric: Rubric | None = None):
    handler = type("BoundHandler", (Handler,), {})
    handler.settings = settings
    handler.rubric = rubric or rubric_for(settings)
    return ThreadingHTTPServer((host, port), handler)


def serve(settings: Settings, host: str = "0.0.0.0", port: int = 8080,
          rubric: Rubric | None = None) -> None:
    rubric = rubric or rubric_for(settings)
    httpd = make_server(settings, host, port, rubric)
    logging.info("glm-critic ouvindo em %s:%s · modelo %s", host, port, settings.judge_model)
    stop = threading.Event()
    if settings.every_seconds > 0:
        logging.info(
            "modo autônomo: ciclo a cada %ss → %s",
            settings.every_seconds,
            settings.notify_url or "(sem NOTIFY_URL)",
        )
        threading.Thread(target=_scheduler, args=(settings, rubric, stop), daemon=True).start()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        stop.set()
        httpd.shutdown()
