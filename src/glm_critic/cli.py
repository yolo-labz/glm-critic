"""Linha de comando. `run` julga uma vez; `serve` vira serviço para o n8n."""

from __future__ import annotations

import argparse
import json
import logging

from .config import ConfigError, Settings
from .critique import critique
from .rubric import Rubric, rubric_for
from .sources import HttpSource, MinifluxSource, SourceError
from .store import VerdictLog


def _settings(args) -> Settings:
    s = Settings.from_env()
    if args.model:
        s = Settings(**{**s.__dict__, "judge_model": args.model})
    if args.limit:
        s = Settings(**{**s.__dict__, "extra": {**s.extra, "limit": args.limit}})
    return s


def _build(args):
    s = Settings.from_env()
    overrides = {}
    if getattr(args, "model", None):
        overrides["judge_model"] = args.model
    if getattr(args, "min_score", None) is not None:
        overrides["min_score"] = args.min_score
    if getattr(args, "batch", None):
        overrides["batch_size"] = args.batch
    if getattr(args, "max_tokens", None):
        overrides["max_tokens"] = args.max_tokens
    if getattr(args, "log", None):
        overrides["log_path"] = args.log
    if overrides:
        s = Settings(**{**s.__dict__, **overrides})
    return s


def cmd_run(args) -> int:
    s = _build(args)
    s.require_judge()
    if not s.source_url:
        logging.error("SOURCE_URL não definido — não há de onde tirar itens")
        return 2

    rubric = Rubric.from_file(args.rubric) if args.rubric else rubric_for(s)
    http = HttpSource(s.source_url, s.source_key, s.user_agent, s.source_timeout)
    source = MinifluxSource(http)

    try:
        items, total = source.entries(status=args.status, limit=args.limit)
    except SourceError as exc:
        # Código de saída distinto por classe de falha: o n8n decide retry
        # olhando o status, então "fonte fora do ar" não pode virar "config
        # errada".
        logging.error("fonte inacessível: %s", exc)
        return 3

    logging.info("status=%s: %s no total · %s puxados", args.status, total, len(items))
    res = critique(items, s, rubric, VerdictLog(s.log_path), dry_run=args.dry_run)
    logging.info(
        "dedup: %s colapsados · cache: %s · julgados: %s · sinal: %s",
        res.collapsed,
        res.from_cache,
        res.judged,
        len(res.signals),
    )
    if res.failures:
        for f in res.failures:
            logging.warning("lote perdido: %s", f)
    logging.info("tokens: %s entrada / %s saída", res.prompt_tokens, res.completion_tokens)

    if args.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
        return 0

    for v in res.signals[: args.top]:
        print(f"- [{v.score}] {v.title}  ({v.artifact})")
        print(f"      {v.why}")
        print(f"      {v.url}")

    if args.star and not args.dry_run:
        n = sum(1 for v in res.signals if v.item_id is not None and source.star(int(v.item_id)))
        logging.info("estrelados: %s", n)
    return 0


def cmd_stats(args) -> int:
    s = _build(args)
    print(json.dumps(VerdictLog(s.log_path).stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_rubric(args) -> int:
    s = _build(args)
    rubric = Rubric.from_file(args.rubric) if args.rubric else rubric_for(s)
    from .store import rubric_hash

    print(f"# rubrica (hash {rubric_hash(rubric.render())}, corte {rubric.min_score})")
    print(rubric.render())
    return 0


def cmd_serve(args) -> int:
    from .service import serve

    s = _build(args)
    s.require_service_token()
    serve(s, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="glm-critic", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--model", help="modelo do juiz (padrão: env JUDGE_MODEL)")
        p.add_argument("--min-score", type=int, help="corte para virar sinal")
        p.add_argument("--batch", type=int, help="itens por chamada ao juiz")
        p.add_argument("--max-tokens", type=int, help="orçamento de saída por chamada")
        p.add_argument("--log", help="caminho do log de vereditos")
        p.add_argument("--rubric", help="rubrica em JSON (padrão: embutida)")

    run = sub.add_parser("run", help="julga itens uma vez")
    common(run)
    run.add_argument("--status", default="unread", choices=["unread", "read", "removed"])
    run.add_argument("--limit", type=int, default=120)
    run.add_argument("--top", type=int, default=25, help="quantos sinais imprimir")
    run.add_argument("--dry-run", action="store_true", help="não grava no log")
    run.add_argument("--star", action="store_true", help="estrela os sinais na fonte")
    run.add_argument("--json", action="store_true", help="saída legível por máquina")
    run.set_defaults(func=cmd_run)

    stats = sub.add_parser("stats", help="resumo do log")
    common(stats)
    stats.set_defaults(func=cmd_stats)

    rub = sub.add_parser("rubric", help="imprime a rubrica em vigor")
    common(rub)
    rub.set_defaults(func=cmd_rubric)

    srv = sub.add_parser("serve", help="endpoint HTTP para o n8n")
    common(srv)
    srv.add_argument("--host", default="0.0.0.0")
    srv.add_argument("--port", type=int, default=8080)
    srv.set_defaults(func=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        logging.error("configuração inválida: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
