"""A rubrica — o critério de valor, versionado ao lado do código.

Isto é o coração do projeto e por isso é um módulo, não uma string solta num
prompt. Quando o juiz errar, o conserto é um diff aqui, revisável.

A rubrica nasceu de um acervo real: mil entradas curadas à mão ao longo de
meses, das quais sobreviveram ao teste as que apontavam para um **artefato
primário** — e não as que resumiam bem. Cinco entradas tiveram de ser corrigidas
porque o resumo era errado ou invertia a direção do efeito. É esse histórico
que os três critérios codificam.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

VERDICT_SCHEMA = {
    "i": "<índice do item, entre colchetes>",
    "v": "signal|noise",
    "s": "0-10",
    "art": "repo|paper|release|doc|none",
    "why": "<=120 caracteres, em português, citando o motivo concreto",
}

DEFAULT_RUBRIC_TEXT = """\
Você julga itens de um fluxo de conteúdo técnico destinado a um engenheiro de
software com projetos VIVOS: um limitador de concorrência em Python, uma frota
NixOS que roda agentes de código, e pipelines de recuperação de informação sobre
um acervo pessoal.

Um item VALE A PENA quando satisfaz os TRÊS:

  1. Aponta para um ARTEFATO PRIMÁRIO verificável — paper, repositório,
     release, post-mortem, especificação, documentação oficial, dado bruto.
     Resumo de terceiro sobre o artefato NÃO conta como o artefato.
  2. muda uma decisão num projeto NOMEADO. Se não dá para dizer qual projeto e
     qual decisão, não passa.
  3. O efeito é declarado com NÚMERO, ou o texto diz abertamente que não mediu.
     "Mais rápido" sem número é alegação; "3× em p99" é dado.

Falhando (1) é boato. Falhando (2) é cultura geral. Falhando (3) é marketing.

Marque como NOISE, mesmo sendo bem escrito:
  - opinião, previsão ou "hot take" sem artefato;
  - listicle, "N ferramentas que você precisa", coletânea sem critério;
  - anúncio de evento, webinar, curso, patrocínio, recrutamento;
  - release sem conteúdo decisório (um número de versão e nada mais);
  - conteúdo datado (mais de ~3 anos) sem relação com um projeto vivo hoje;
  - repetição do que já está estabelecido, sem dado novo nem ângulo novo.

Não seja generoso: numa lista de dez itens técnicos, o normal é um a três
passarem. `score` é 0–10 e o corte prático é {min_score}."""


@dataclass(frozen=True)
class Rubric:
    """Critério de julgamento, com o vocabulário que o juiz precisa conhecer."""

    text: str = DEFAULT_RUBRIC_TEXT
    projects: tuple[str, ...] = ()
    min_score: int = 6
    examples: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        """Texto final entregue ao juiz, já com projetos e corte preenchidos."""
        parts = [self.text.format(min_score=self.min_score)]
        if self.projects:
            parts.append(
                "\nProjetos vivos reconhecidos (use estes nomes ao justificar):\n"
                + "\n".join(f"  - {p}" for p in self.projects)
            )
        if self.examples:
            parts.append("\nExemplos de julgamento já aceitos:\n" + "\n".join(self.examples))
        return "\n".join(parts)

    @classmethod
    def from_file(cls, path: str | Path) -> Rubric:
        """Carrega de JSON, para ajustar a dieta sem tocar no código."""
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            text=d.get("text", DEFAULT_RUBRIC_TEXT),
            projects=tuple(d.get("projects", ())),
            min_score=int(d.get("min_score", 6)),
            examples=tuple(d.get("examples", ())),
        )


def rubric_for(settings) -> Rubric:
    """Rubrica padrão, ajustada pelo ambiente.

    `CRITIC_PROJECTS` (separado por vírgula) existe porque a lista de projetos
    vivos muda mais rápido que o critério: o que vale continua valendo, os
    nomes mudam.
    """
    projects = tuple(
        p.strip() for p in (settings.extra.get("projects") or "").split(",") if p.strip()
    )
    return Rubric(projects=projects, min_score=settings.min_score)
