"""O juiz: monta o lote, chama o modelo, interpreta a resposta.

Duas coisas aqui são menos triviais do que parecem.

**Interpretar a resposta.** O juiz devolve texto livre que *deveria* ser um
array JSON. Na prática ele às vezes embrulha em cerca de código, às vezes usa
vírgula final, e às vezes omite o índice que foi pedido. Cada um desses casos
tem um conserto barato e um custo caro se ignorado: o lote inteiro é pago e
perdido. Por isso o parser tenta variantes e, quando o índice falta, cai para a
ordem posicional — mas nunca inventa alinhamento quando há ambiguidade real
(índices duplicados), porque aí o veredito seria atribuído ao item errado, que é
pior que perder o lote.

**Alinhamento é a invariante.** Um veredito no item errado é um erro silencioso
que contamina o placar; um lote perdido é ruído visível. O código prefere o
segundo.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .rubric import VERDICT_SCHEMA, Rubric

# Repetição com instrução mais dura: o juiz ocasionalmente responde em prosa
# em vez do array pedido. Medido uma vez em cada ~cinco lotes; recuperar é mais
# barato que descartar o lote já pago.
STRICT_SUFFIX = (
    "\n\nIMPORTANTE: responda SOMENTE com o array JSON, começando por [ e "
    "terminando em ]. Sem explicação, sem cerca de código, sem texto antes ou depois."
)


class JudgeError(RuntimeError):
    """O juiz não devolveu algo utilizável."""


def build_prompt(items: list[dict], rubric: Rubric, excerpt_chars: int = 700) -> str:
    """Monta o prompt do lote. `items` são dicts já normalizados pela fonte."""
    blocks = []
    for i, it in enumerate(items):
        body = re.sub(r"<[^>]+>", " ", it.get("content") or "")
        body = " ".join(body.split())[:excerpt_chars]
        blocks.append(
            f"[{i}] titulo: {(it.get('title') or '')[:160]}\n"
            f"    fonte: {it.get('feed') or '?'} | "
            f"publicado: {(it.get('published_at') or '')[:10]} | "
            f"url: {it.get('url') or ''}\n"
            f"    trecho: {body}"
        )
    schema = json.dumps(VERDICT_SCHEMA, ensure_ascii=False)
    return (
        f"{rubric.render()}\n\n"
        f"Julgue cada item abaixo. Responda APENAS com um array JSON, sem texto "
        f"fora dele, um objeto por item, no formato {schema}.\n\n" + "\n\n".join(blocks)
    )


def _extract_json_array(text: str) -> list:
    """Tenta as variantes que o juiz realmente produz, da mais limpa à mais suja."""
    if not text:
        return []
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    for candidate in (fenced.group(1) if fenced else None, None):
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
    raw = re.search(r"\[.*\]", text, re.S)
    if not raw:
        return []
    body = raw.group(0)
    for candidate in (
        body,
        re.sub(r",\s*\]", "]", re.sub(r",\s*([}\]])", r"\1", body)),
        re.sub(r"\]\s*,\s*\[", "],[", body),
    ):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue
    return []


def verdicts_from_response(text: str, n_items: int) -> list[dict]:
    """Converte a resposta do juiz em vereditos alinhados por índice.

    Devolve uma lista de dicts com `i`, `v`, `s`, `art`, `why`. Itens sem
    veredito simplesmente não aparecem — o chamador decide o que fazer.
    """
    parsed = _extract_json_array(text)
    if not parsed:
        raise JudgeError(f"resposta sem array JSON utilizável ({len(text)} caracteres)")

    raw_indexes = [v.get("i") for v in parsed if isinstance(v, dict)]
    declared = [i for i in raw_indexes if i is not None]
    # Índice duplicado significa que não dá para saber a quem o veredito
    # pertence; nesse caso a posição é a única leitura defensável.
    ambiguous = len(set(map(str, declared))) != len(declared)

    out: list[dict] = []
    for pos, v in enumerate(parsed):
        if not isinstance(v, dict):
            continue
        # Um veredito precisa de forma reconhecível. Sem este corte, uma resposta
        # lixo ("[{\"sem\":\"i\"}]") viraria vereditos de ruído por posição —
        # isto é, o juiz falharia e o item bom seria marcado como ruído em
        # silêncio, que é o pior desfecho possível aqui.
        if "v" not in v and "s" not in v:
            continue
        idx = pos if (ambiguous or v.get("i") is None) else _as_index(v.get("i"), pos)
        if not 0 <= idx < n_items:
            idx = pos if 0 <= pos < n_items else None
        if idx is None:
            continue
        out.append(
            {
                "i": idx,
                "v": "signal" if str(v.get("v", "")).lower() == "signal" else "noise",
                "s": _as_score(v.get("s")),
                "art": str(v.get("art") or "none")[:12],
                "why": " ".join(str(v.get("why") or "").split())[:160],
            }
        )
    if not out:
        raise JudgeError("array JSON presente, mas nenhum veredito aproveitável")
    return out


def _as_index(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_score(value) -> int:
    try:
        return max(0, min(10, int(float(value))))
    except (TypeError, ValueError):
        return 0


def call_judge(prompt: str, settings, opener=urllib.request.urlopen) -> tuple[str, dict]:
    """Uma chamada ao modelo. `opener` é injetável para teste sem rede."""
    payload = {
        "model": settings.judge_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": settings.max_tokens,
    }
    req = urllib.request.Request(
        settings.judge_url,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {settings.judge_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with opener(req, timeout=settings.judge_timeout) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        raise JudgeError(f"juiz respondeu {exc.code}: {exc.read().decode()[:200]}") from exc

    choices = d.get("choices") or []
    if not choices:
        raise JudgeError(f"resposta sem choices: {json.dumps(d)[:200]}")
    message = choices[0].get("message") or {}
    usage = d.get("usage") or {}
    return message.get("content") or "", usage
