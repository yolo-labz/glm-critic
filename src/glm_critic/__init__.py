"""glm-critic — um juiz de rubrica sobre fluxos de conteúdo.

Ponto de partida: `critique()` recebe itens de uma fonte (hoje, um leitor RSS) e
devolve um veredito por item contra uma rubrica explícita, usando um modelo de
linguagem como juiz.

O desenho assume três coisas, e cada uma é uma decisão:

1. **A rubrica é texto versionado, não prompt escondido.** Discordar do juiz é
   editar a rubrica; o diff fica no git e a mudança de comportamento é
   auditável.
2. **Nada de estado implícito.** Vereditos ficam num log append-only indexado
   por hash do item, então reexecutar é grátis e reprodutível.
3. **A biblioteca não sabe onde o conteúdo mora.** Fonte e destino entram por
   adaptador; o núcleo só vê dicionários. É o que permite testar sem rede.
"""

from .config import Settings
from .critique import CritiqueResult, critique
from .dedup import collapse
from .judge import JudgeError, verdicts_from_response
from .rubric import DEFAULT_RUBRIC_TEXT, Rubric

__all__ = [
    "CritiqueResult",
    "DEFAULT_RUBRIC_TEXT",
    "JudgeError",
    "Rubric",
    "Settings",
    "collapse",
    "critique",
    "verdicts_from_response",
]
__version__ = "0.1.0"
