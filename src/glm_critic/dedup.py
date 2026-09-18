"""Deduplicação: a mesma peça chegando por três fontes é UM item.

Sem isto, o juiz é pago três vezes pela mesma coisa e pode devolver três
vereditos que discordam entre si — o que é pior que o custo, porque destrói a
confiança no placar.
"""

from __future__ import annotations

import re

# Abaixo disto o título é curto e genérico demais para servir de prefixo: colar
# "Rust 1.90" com "Rust 1.91" por prefixo seria pior que não colar.
MIN_PREFIX = 20

# Um sufixo só conta como "mesma peça" se for curto em relação ao prefixo.
# "Recursive Language Models — curto" cola; "Latency lags bandwidth in a
# distributed cache with contention" não.
_SUFFIX_SLACK = 12
_SUFFIX_RATIO = 0.5

_PREFIX_NOISE = re.compile(r"^\s*(\[[^\]]*\]|\([^)]*\)|#\d+\s*[-—:]\s*)\s*")


def normalize(title: str) -> str:
    """Minúsculas, sem prefixo de boletim, sem pontuação, espaços colapsados."""
    t = _PREFIX_NOISE.sub("", title or "")
    t = re.sub(r"\W+", " ", t.lower())
    return " ".join(t.split())


def key_for(item: dict) -> str:
    """Chave de agrupamento. Cai para a URL quando não há título útil."""
    k = normalize(item.get("title", ""))[:80]
    return k or item.get("url") or str(item.get("id", ""))


def collapse(items: list[dict], *, published: str = "published_at") -> tuple[list[dict], int]:
    """Colapsa duplicatas e devolve `(únicos, quantos foram absorvidos)`.

    Duas regras, nesta ordem:

    1. título normalizado idêntico;
    2. um título é prefixo do outro **e** o sufixo é curto — que é como a mesma
       peça chega como "Recursive Language Models" numa fonte e "Recursive
       Language Models — curto" em outra.

    Em ambos os casos fica a publicação mais recente: entre duas cópias da mesma
    peça, a mais nova é a que provavelmente tem correção ou atualização.
    """
    groups: dict[str, dict] = {}
    for it in items:
        k = key_for(it)
        cur = groups.get(k)
        if cur is None or (it.get(published) or "") > (cur.get(published) or ""):
            groups[k] = it

    keys = sorted(groups, key=len)
    absorbed: set[str] = set()
    for i, short in enumerate(keys):
        if short in absorbed or len(short) < MIN_PREFIX:
            continue
        for long in keys[i + 1 :]:
            if long in absorbed or not long.startswith(short):
                continue
            if len(long) - len(short) > max(_SUFFIX_SLACK, int(len(short) * _SUFFIX_RATIO)):
                continue
            if (groups[long].get(published) or "") > (groups[short].get(published) or ""):
                groups[short] = groups[long]
            absorbed.add(long)

    out = [v for k, v in groups.items() if k not in absorbed]
    return out, len(items) - len(out)
