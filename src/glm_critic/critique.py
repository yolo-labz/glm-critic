"""A orquestração: buscar → deduplicar → julgar → registrar → devolver o sinal.

Esta função é o contrato do projeto. Ela não fala HTTP diretamente, não lê
arquivo de configuração e não decide o que fazer com o resultado — recebe tudo
injetado, o que a torna testável sem rede e sem credencial.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dedup import collapse
from .judge import JudgeError, build_prompt, call_judge, verdicts_from_response
from .rubric import Rubric
from .store import Verdict, VerdictLog, item_key, now_iso, rubric_hash


@dataclass
class CritiqueResult:
    """O que aconteceu, com números — não só o que sobrou."""

    considered: int = 0
    collapsed: int = 0
    judged: int = 0
    from_cache: int = 0
    signals: list[Verdict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "collapsed": self.collapsed,
            "judged": self.judged,
            "from_cache": self.from_cache,
            "signals": len(self.signals),
            "failures": list(self.failures),
            "tokens": {"prompt": self.prompt_tokens, "completion": self.completion_tokens},
            "model": self.model,
            "items": [
                {
                    "id": v.item_id,
                    "score": v.score,
                    "artifact": v.artifact,
                    "why": v.why,
                    "title": v.title,
                    "url": v.url,
                    "feed": v.feed,
                }
                for v in self.signals
            ],
        }


def critique(
    items: list[dict],
    settings,
    rubric: Rubric,
    log: VerdictLog,
    *,
    judge_call=call_judge,
    dry_run: bool = False,
) -> CritiqueResult:
    """Julga `items` contra `rubric` e devolve o resultado agregado.

    `dry_run` julga mas **não grava** — serve para medir a rubrica sem
    envenenar o log com vereditos de teste.
    """
    res = CritiqueResult(considered=len(items), model=settings.judge_model)
    if not items:
        return res

    unique, collapsed = collapse(items)
    res.collapsed = collapsed

    rhash = rubric_hash(rubric.render())
    cached = log.load()
    verdicts: list[Verdict] = []
    fresh: list[Verdict] = []  # só o que foi julgado nesta execução

    pending = []
    for it in unique:
        k = item_key(it)
        prior = cached.get(k)
        # Um veredito dado sob OUTRA rubrica não é reaproveitável: seria comparar
        # réguas diferentes. Ele é rejulgado.
        if prior and prior.rubric_hash == rhash:
            verdicts.append(prior)
            res.from_cache += 1
        else:
            pending.append(it)

    for i in range(0, len(pending), max(1, settings.batch_size)):
        chunk = pending[i : i + settings.batch_size]
        try:
            text, usage = judge_call(build_prompt(chunk, rubric), settings)
            parsed = verdicts_from_response(text, len(chunk))
        except JudgeError as exc:
            # Um lote perdido é ruído visível; um veredito no item errado é erro
            # silencioso. O código prefere o primeiro, e registra qual lote caiu.
            res.failures.append(f"lote {i // settings.batch_size + 1}: {exc}")
            continue

        res.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        res.completion_tokens += int(usage.get("completion_tokens") or 0)

        for p in parsed:
            it = chunk[p["i"]]
            v = Verdict(
                key=item_key(it),
                item_id=it.get("id"),
                score=p["s"],
                verdict=p["v"],
                artifact=p["art"],
                why=p["why"],
                title=(it.get("title") or "")[:160],
                url=it.get("url") or "",
                feed=it.get("feed") or "",
                rubric_hash=rhash,
                model=settings.judge_model,
                at=now_iso(),
            )
            verdicts.append(v)
            fresh.append(v)

    res.judged = len(fresh)
    res.signals = sorted(
        (v for v in verdicts if v.score >= settings.min_score), key=lambda v: -v.score
    )

    if not dry_run:
        log.append(fresh)

    return res
