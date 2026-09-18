"""Log de vereditos — append-only, indexado por hash do item.

Existe por três razões, e a terceira é a que importa:

1. **Cache.** Reexecutar não repaga nem muda o veredito só porque rodou de novo.
2. **Trilha.** Dá para reconstruir o que o juiz disse, quando, com que rubrica.
3. **Calibração.** É o único insumo possível para medir se o juiz acerta: o
   veredito fica ao lado do que a pessoa de fato leu depois. Enquanto essa
   comparação não acontece, o limiar é escolha à mão — e dizer isso é parte do
   contrato, não uma ressalva de rodapé.

Append-only e tolerante a linha corrompida: um log que derruba o processo por
causa de uma linha truncada não serve como log.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Verdict:
    key: str
    item_id: object
    score: int
    verdict: str
    artifact: str
    why: str
    title: str = ""
    url: str = ""
    feed: str = ""
    rubric_hash: str = ""
    model: str = ""
    at: str = ""

    @property
    def is_signal(self) -> bool:
        return self.verdict == "signal"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def item_key(item: dict) -> str:
    """Chave estável do item. O hash da fonte quando existe, senão id+url.

    Incluir a URL evita o caso em que um feed semanal repete título ("Changelog")
    e itens distintos colapsam entre execuções.
    """
    raw = f"{item.get('hash') or ''}|{item.get('id') or ''}|{item.get('url') or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def rubric_hash(rubric_text: str) -> str:
    """A rubrica faz parte da chave do veredito.

    Sem isto, mudar o critério reaproveitaria vereditos dados sob o critério
    antigo — e o placar ficaria misturando duas réguas diferentes sem avisar.
    """
    return hashlib.sha256(rubric_text.encode()).hexdigest()[:12]


class VerdictLog:
    """Log append-only em JSONL, com leitura tolerante."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, Verdict]:
        """Último veredito por chave. Linhas ruins são ignoradas em silêncio."""
        if not self.path.exists():
            return {}
        out: dict[str, Verdict] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                v = Verdict(**{k: d[k] for k in d if k in Verdict.__dataclass_fields__})
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            out[v.key] = v
        return out

    def append(self, verdicts: list[Verdict]) -> None:
        if not verdicts:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for v in verdicts:
                fh.write(v.to_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def stats(self) -> dict:
        """Contagem por veredito, lida do arquivo — não de memória."""
        v = self.load()
        signals = [x for x in v.values() if x.is_signal]
        return {
            "total": len(v),
            "signals": len(signals),
            "noise": len(v) - len(signals),
            "log": str(self.path),
            "log_exists": self.path.exists(),
        }


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
