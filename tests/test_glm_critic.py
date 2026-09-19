"""Checagens do critic — sem rede, sem credencial, sem fixture externa.

O que está coberto aqui é a lógica que **falha em silêncio**: alinhamento de
veredito, colapso de duplicata, escopo do cache pela rubrica, e o serviço
recusando subir sem token. Um erro nessas quatro não aparece como exceção —
aparece como um placar errado que ninguém questiona.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from glm_critic.config import ConfigError, Settings
from glm_critic.critique import critique
from glm_critic.dedup import collapse, normalize
from glm_critic.judge import JudgeError, build_prompt, verdicts_from_response
from glm_critic.rubric import Rubric
from glm_critic.service import make_server
from glm_critic.sources import HttpSource, MinifluxSource, normalize_entry
from glm_critic.store import VerdictLog, item_key, rubric_hash


def item(
    i: int, title: str, feed: str = "F", when: str = "2026-09-01T00:00:00Z", content: str = "corpo"
) -> dict:
    return {
        "id": i,
        "title": title,
        "url": f"https://ex.invalid/{i}",
        "content": content,
        "published_at": when,
        "feed": feed,
        "hash": f"h{i}",
    }


class TestDedup(unittest.TestCase):
    def test_identical_titles_collapse(self):
        got, n = collapse(
            [item(1, "Recursive Language Models", "A"), item(2, "Recursive Language Models", "B")]
        )
        self.assertEqual((len(got), n), (1, 1))

    def test_newsletter_prefix_is_ignored(self):
        got, _ = collapse(
            [
                item(1, "Recursive Language Models", "A"),
                item(2, "[AINews] Recursive Language Models", "B"),
            ]
        )
        self.assertEqual(len(got), 1)

    def test_short_suffix_collapses(self):
        got, _ = collapse(
            [
                item(1, "Recursive Language Models", "A"),
                item(2, "Recursive Language Models — curto", "B"),
            ]
        )
        self.assertEqual(len(got), 1, "sufixo curto deveria colar")

    def test_long_suffix_does_not_collapse(self):
        got, _ = collapse(
            [
                item(1, "Latency lags bandwidth", "A"),
                item(2, "Latency lags bandwidth in a distributed cache with contention", "B"),
            ]
        )
        self.assertEqual(len(got), 2, "sufixo longo não deveria colar")

    def test_distinct_titles_stay(self):
        got, _ = collapse([item(1, "Throttling com fair queue"), item(2, "Latency lags bandwidth")])
        self.assertEqual(len(got), 2)

    def test_keeps_most_recent_copy(self):
        got, _ = collapse(
            [
                item(1, "Mesmo Titulo", when="2026-08-01T00:00:00Z"),
                item(2, "Mesmo Titulo", when="2026-09-01T00:00:00Z"),
            ]
        )
        self.assertEqual(got[0]["id"], 2)

    def test_normalize_strips_punctuation_and_case(self):
        self.assertEqual(normalize("Rust: 1.90 — Released!"), "rust 1 90 released")


class TestJudgeParsing(unittest.TestCase):
    def test_plain_array(self):
        got = verdicts_from_response(
            '[{"i":1,"v":"signal","s":9,"art":"repo","why":"x"},'
            '{"i":0,"v":"noise","s":1,"art":"none","why":"y"}]',
            2,
        )
        self.assertEqual({v["i"]: v["s"] for v in got}, {0: 1, 1: 9})

    def test_fenced_array(self):
        got = verdicts_from_response('```json\n[{"i":0,"v":"signal","s":7}]\n```', 1)
        self.assertEqual(got[0]["s"], 7)

    def test_trailing_comma(self):
        got = verdicts_from_response('[{"i":0,"v":"signal","s":8},]', 1)
        self.assertEqual(got[0]["s"], 8)

    def test_missing_index_falls_back_to_position(self):
        got = verdicts_from_response('[{"v":"signal","s":7},{"v":"noise","s":2}]', 2)
        self.assertEqual({v["i"]: v["s"] for v in got}, {0: 7, 1: 2})

    def test_duplicate_index_is_ambiguous_and_uses_position(self):
        # Dois itens reivindicando o índice 0 não podem ser atribuídos por
        # adivinhação: a posição é a única leitura defensável.
        got = verdicts_from_response('[{"i":0,"s":9},{"i":0,"s":3}]', 2)
        self.assertEqual({v["i"]: v["s"] for v in got}, {0: 9, 1: 3})

    def test_out_of_range_index_uses_position(self):
        got = verdicts_from_response('[{"i":99,"s":8}]', 1)
        self.assertEqual(got[0]["i"], 0)

    def test_score_is_clamped(self):
        got = verdicts_from_response('[{"i":0,"s":77},{"i":1,"s":-3}]', 2)
        self.assertEqual([v["s"] for v in got], [10, 0])

    def test_no_array_raises(self):
        with self.assertRaises(JudgeError):
            verdicts_from_response("desculpe, não consegui responder", 3)

    def test_all_entries_malformed_raises(self):
        with self.assertRaises(JudgeError):
            verdicts_from_response('[{"sem":"i"},{"nada":true}]', 2)

    def test_prompt_contains_every_item_and_the_cut(self):
        p = build_prompt([item(1, "A"), item(2, "B")], Rubric(min_score=7))
        self.assertIn("[0]", p)
        self.assertIn("[1]", p)
        self.assertIn("7", p)


class TestRubricScope(unittest.TestCase):
    def test_rubric_change_changes_hash(self):
        self.assertNotEqual(rubric_hash("a"), rubric_hash("b"))

    def test_projects_are_rendered(self):
        r = Rubric(projects=("Throttler", "frota NixOS"))
        self.assertIn("Throttler", r.render())
        self.assertIn("frota NixOS", r.render())


class TestCritique(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = Settings(judge_model="test-model", batch_size=2, min_score=6)
        self.rubric = Rubric(text="rubrica de teste {min_score}", min_score=6)

    def _judge(self, prompt, settings):
        n = prompt.count("\n[") + (1 if prompt.lstrip().startswith("[") else 0)
        # devolve sempre: sinal se o texto mencionar "bom", senão ruído
        verdicts = [
            {
                "i": i,
                "v": "signal" if "bom" in prompt else "noise",
                "s": 9 if "bom" in prompt else 2,
                "art": "repo",
                "why": "teste",
            }
            for i in range(n)
        ]
        return json.dumps(verdicts), {"prompt_tokens": 100, "completion_tokens": 50}

    def test_signals_respect_the_cut(self):
        log = VerdictLog(self.tmp / "v.jsonl")
        items = [item(1, "bom item"), item(2, "bom item também")]
        res = critique(items, self.settings, self.rubric, log, judge_call=self._judge)
        self.assertEqual(len(res.signals), 2)
        self.assertEqual(res.judged, 2)

    def test_low_scores_produce_no_signal(self):
        log = VerdictLog(self.tmp / "v.jsonl")
        res = critique([item(1, "ruim")], self.settings, self.rubric, log, judge_call=self._judge)
        self.assertEqual(len(res.signals), 0)

    def test_dry_run_does_not_write(self):
        path = self.tmp / "dry.jsonl"
        critique(
            [item(1, "bom")],
            self.settings,
            self.rubric,
            VerdictLog(path),
            judge_call=self._judge,
            dry_run=True,
        )
        self.assertFalse(path.exists(), "dry-run não pode gravar no log")

    def test_second_run_is_served_from_cache(self):
        path = self.tmp / "cache.jsonl"
        items = [item(1, "bom item")]
        first = critique(
            items, self.settings, self.rubric, VerdictLog(path), judge_call=self._judge
        )
        calls = []

        def counting_judge(prompt, settings):
            calls.append(prompt)
            return self._judge(prompt, settings)

        second = critique(
            items, self.settings, self.rubric, VerdictLog(path), judge_call=counting_judge
        )
        self.assertEqual(first.judged, 1)
        self.assertEqual(second.judged, 0)
        self.assertEqual(second.from_cache, 1)
        self.assertEqual(calls, [], "cache quente não deve chamar o juiz")

    def test_changed_rubric_invalidates_cache(self):
        path = self.tmp / "scope.jsonl"
        items = [item(1, "bom item")]
        critique(items, self.settings, self.rubric, VerdictLog(path), judge_call=self._judge)
        calls = []

        def counting_judge(prompt, settings):
            calls.append(1)
            return self._judge(prompt, settings)

        other = Rubric(text="outra rubrica {min_score}", min_score=6)
        res = critique(items, self.settings, other, VerdictLog(path), judge_call=counting_judge)
        self.assertEqual(res.judged, 1, "rubrica nova exige rejulgamento")
        self.assertEqual(len(calls), 1)

    def test_failed_batch_is_recorded_not_silent(self):
        def failing_judge(prompt, settings):
            raise JudgeError("lote ilegível")

        res = critique(
            [item(1, "bom")],
            self.settings,
            self.rubric,
            VerdictLog(self.tmp / "f.jsonl"),
            judge_call=failing_judge,
        )
        self.assertEqual(len(res.signals), 0)
        self.assertTrue(res.failures, "lote perdido precisa aparecer")

    def test_dedup_collapses_before_paying_the_judge(self):
        seen = []

        def counting_judge(prompt, settings):
            seen.append(prompt.count("titulo:"))
            return self._judge(prompt, settings)

        items = [item(1, "Mesma Coisa", "A"), item(2, "Mesma Coisa", "B")]
        res = critique(
            items,
            self.settings,
            self.rubric,
            VerdictLog(self.tmp / "d.jsonl"),
            judge_call=counting_judge,
        )
        self.assertEqual(res.considered, 2)
        self.assertEqual(res.collapsed, 1)
        self.assertEqual(sum(seen), 1, "o juiz não pode pagar por duplicata")


class TestStore(unittest.TestCase):
    def test_corrupt_line_is_tolerated(self):
        p = Path(tempfile.mkdtemp()) / "l.jsonl"
        p.write_text(
            '{"key":"a","item_id":1,"score":9,"verdict":"signal","artifact":"repo",'
            '"why":"","title":"","url":"","feed":"","rubric_hash":"x","model":"m","at":"t"}\n'
            "{isso nao e json\n"
        )
        self.assertEqual(len(VerdictLog(p).load()), 1)

    def test_key_includes_url_so_repeated_titles_do_not_collide(self):
        a = {"id": 1, "title": "Changelog", "url": "https://a", "hash": "h1"}
        b = {"id": 1, "title": "Changelog", "url": "https://b", "hash": "h1"}
        self.assertNotEqual(item_key(a), item_key(b))


class TestSources(unittest.TestCase):
    def test_normalize_entry_flattens_the_feed(self):
        got = normalize_entry(
            {
                "id": 7,
                "title": " T ",
                "feed": {"title": "Fonte"},
                "content": "<p>oi</p>",
                "hash": "h",
            }
        )
        self.assertEqual(got["feed"], "Fonte")
        self.assertEqual(got["title"], "T")

    def test_normalize_entry_survives_missing_fields(self):
        got = normalize_entry({})
        self.assertEqual(got["feed"], "")
        self.assertEqual(got["title"], "")

    def test_unbookmark_reads_toggles_and_verifies(self):
        # Upstream 2.3.3 toggleStarredHandler ignores the body. The previous
        # test only asserted an invented request, not the server's state.
        calls = []

        class FakeHttp(HttpSource):
            starred = True

            def request(self, path, method="GET", body=None, key_header="X-Auth-Token"):
                calls.append((path, method, body))
                if method == "PUT":
                    self.starred = not self.starred
                    return 204, {}
                return 200, {"starred": self.starred}

        source = MinifluxSource(FakeHttp("http://x"))
        self.assertTrue(source.star(7, on=False))
        self.assertTrue(source.star(7, on=False))
        self.assertEqual([c[1] for c in calls], ["GET", "PUT", "GET", "GET"])
        self.assertEqual(calls[1], ("/v1/entries/7/bookmark", "PUT", None))


class TestService(unittest.TestCase):
    def test_refuses_to_start_without_token(self):
        s = Settings(service_token="")
        with self.assertRaises(ConfigError):
            s.require_service_token()

    def test_health_needs_no_token_but_run_does(self):
        s = Settings(service_token="segredo", source_url="http://ex.invalid")
        httpd = make_server(s, "127.0.0.1", 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                self.assertEqual(r.status, 200)
                self.assertTrue(json.loads(r.read())["ok"])

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/run",
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 401)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRetry(unittest.TestCase):
    """O juiz às vezes responde em prosa; uma segunda tentativa recupera o lote.

    Sem isto o lote inteiro é pago e descartado — medido uma vez em cada ~cinco
    lotes, o que não é raro o bastante para ignorar.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = Settings(judge_model="m", batch_size=1, min_score=6)
        self.rubric = Rubric(text="r {min_score}", min_score=6)

    def test_prose_then_json_succeeds_and_is_counted(self):
        attempts = []

        def flaky(prompt, settings):
            attempts.append(prompt)
            if len(attempts) == 1:
                return "desculpe, não consigo responder em JSON", {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                }
            return '[{"i":0,"v":"signal","s":9,"art":"repo","why":"ok"}]', {
                "prompt_tokens": 10,
                "completion_tokens": 5,
            }

        res = critique(
            [item(1, "bom")],
            self.settings,
            self.rubric,
            VerdictLog(self.tmp / "r.jsonl"),
            judge_call=flaky,
        )
        self.assertEqual(len(attempts), 2, "deveria tentar duas vezes")
        self.assertEqual(res.retried, 1)
        self.assertEqual(len(res.signals), 1)
        self.assertEqual(res.failures, [])

    def test_second_attempt_uses_the_strict_suffix(self):
        seen = []

        def flaky(prompt, settings):
            seen.append(prompt)
            return "prosa", {}

        critique(
            [item(1, "x")],
            self.settings,
            self.rubric,
            VerdictLog(self.tmp / "r2.jsonl"),
            judge_call=flaky,
        )
        self.assertIn("SOMENTE com o array JSON", seen[1])
        self.assertNotIn("SOMENTE com o array JSON", seen[0])

    def test_both_attempts_failing_is_reported(self):
        def always_prose(prompt, settings):
            return "nada de JSON aqui", {}

        res = critique(
            [item(1, "x")],
            self.settings,
            self.rubric,
            VerdictLog(self.tmp / "r3.jsonl"),
            judge_call=always_prose,
        )
        self.assertEqual(res.judged, 0)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("primeira tentativa", res.failures[0])


class TestNotifyDirection(unittest.TestCase):
    """A inversão: o critic CHAMA o n8n, em vez de ser chamado.

    O node HTTP do n8n recusa hostname interno (`Invalid URL`), e expor o serviço
    de IA para contornar isso troca um problema de configuração por superfície de
    ataque. Chamando daqui, a URL interna é só uma URL.
    """

    def test_notify_without_url_is_a_noop(self):
        from glm_critic.service import notify

        self.assertFalse(notify("", {"signals": 1}))

    def test_notify_posts_the_payload(self):
        from glm_critic.service import notify

        seen = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=None):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["body"] = json.loads(req.data.decode())
            return FakeResponse()

        import unittest.mock as mock

        with mock.patch("urllib.request.urlopen", fake_open):
            ok = notify("http://n8n.web.1:5678/webhook/critic", {"signals": 2, "items": []})
        self.assertTrue(ok)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"]["signals"], 2)

    def test_notify_failure_is_reported_not_raised(self):
        import unittest.mock as mock
        import urllib.error

        from glm_critic.service import notify

        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            self.assertFalse(notify("http://x.invalid/", {"signals": 1}))

    def test_every_seconds_defaults_to_off(self):
        s = Settings.from_env({})
        self.assertEqual(s.every_seconds, 0, "o modo autônomo não pode ligar sozinho")
