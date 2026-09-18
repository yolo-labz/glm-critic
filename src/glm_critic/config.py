"""Configuração por ambiente. Nenhum segredo e nenhuma URL de deploy no código.

Um serviço que carrega o próprio destino em constante não é configurável nem
testável: o teste teria de falar com produção. Aqui tudo que varia por
implantação entra por variável de ambiente, e o que falta falha alto.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_JUDGE_HOST = "api.z.ai"
DEFAULT_JUDGE_PATH = "/api/coding/paas/v4/chat/completions"
DEFAULT_JUDGE_URL = f"https://{DEFAULT_JUDGE_HOST}{DEFAULT_JUDGE_PATH}"

# O juiz é um modelo de raciocínio: gasta tokens pensando antes de responder.
# Orçamento curto não economiza, ele devolve conteúdo vazio — e o lote pago se
# perde. Por isso o padrão é generoso e o custo é controlado por lote, não aqui.
DEFAULT_MAX_TOKENS = 6000
DEFAULT_BATCH = 8


class ConfigError(RuntimeError):
    """Falta configuração obrigatória, ou ela é inválida."""


@dataclass(frozen=True)
class Settings:
    judge_url: str = DEFAULT_JUDGE_URL
    judge_model: str = "glm-5.3"
    judge_key: str = ""
    judge_timeout: int = 300
    batch_size: int = DEFAULT_BATCH
    max_tokens: int = DEFAULT_MAX_TOKENS
    min_score: int = 6
    user_agent: str = "glm-critic/0.1 (+https://github.com/yolo-labz/glm-critic)"
    source_url: str = ""
    source_key: str = ""
    source_timeout: int = 45
    service_token: str = ""
    notify_url: str = ""
    every_seconds: int = 0
    log_path: str = "verdicts.jsonl"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict | None = None) -> Settings:
        e = os.environ if env is None else env

        def _int(name: str, default: int) -> int:
            raw = e.get(name)
            if raw in (None, ""):
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} deve ser inteiro, veio {raw!r}") from exc

        s = cls(
            judge_url=e.get("JUDGE_URL") or DEFAULT_JUDGE_URL,
            judge_model=e.get("JUDGE_MODEL") or "glm-5.3",
            judge_key=e.get("JUDGE_API_KEY", ""),
            judge_timeout=_int("JUDGE_TIMEOUT", 300),
            batch_size=_int("CRITIC_BATCH", DEFAULT_BATCH),
            max_tokens=_int("CRITIC_MAX_TOKENS", DEFAULT_MAX_TOKENS),
            min_score=_int("CRITIC_MIN_SCORE", 6),
            user_agent=e.get("CRITIC_USER_AGENT") or cls.user_agent,
            source_url=e.get("SOURCE_URL", ""),
            source_key=e.get("SOURCE_API_KEY", ""),
            source_timeout=_int("SOURCE_TIMEOUT", 45),
            service_token=e.get("SERVICE_TOKEN", ""),
            notify_url=e.get("NOTIFY_URL", ""),
            every_seconds=_int("CRITIC_EVERY_SECONDS", 0),
            log_path=e.get("CRITIC_LOG", "verdicts.jsonl"),
        )
        return s

    def require_judge(self) -> None:
        if not self.judge_key:
            raise ConfigError("JUDGE_API_KEY ausente — o critic não julga sem juiz")

    def require_service_token(self) -> None:
        if not self.service_token:
            raise ConfigError(
                "SERVICE_TOKEN ausente — o endpoint HTTP ficaria aberto a quem "
                "alcançar a rede; o serviço se recusa a subir sem ele"
            )
