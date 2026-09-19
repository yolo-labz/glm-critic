"""Recovery contracts: reader, providers, mutations and notification replay."""

import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from glm_critic.config import ConfigError, Settings
from glm_critic.critique import critique
from glm_critic.judge import JudgeError, call_judge
from glm_critic.rubric import Rubric
from glm_critic.service import notify_pending, run_once
from glm_critic.sources import FreshRSSSource, HttpSource, MinifluxSource, SourceError, source_for
from glm_critic.store import VerdictLog


class Response:
    status = 200

    def __init__(self, data):
        self.data = data

    def read(self):
        return json.dumps(self.data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ReaderTests(unittest.TestCase):
    def test_fever_form_auth_pagination_and_normalization(self):
        calls = []

        def open_request(req, timeout):
            params = urllib.parse.parse_qs(req.data.decode(), keep_blank_values=True)
            self.assertEqual(params["api_key"], ["secret"])
            self.assertIsNone(req.get_header("X-auth-token"))
            calls.append(params)
            if "unread_item_ids" in params:
                return Response(
                    {
                        "auth": 1,
                        "unread_item_ids": ",".join(map(str, range(1, 76))),
                        "feeds": [{"id": 5, "title": "Public source"}],
                    }
                )
            ids = params["with_ids"][0].split(",")
            self.assertLessEqual(len(ids), 50)
            return Response(
                {
                    "auth": 1,
                    "items": [
                        {
                            "id": i,
                            "feed_id": 5,
                            "title": " Test ",
                            "html": "<p>body</p>",
                            "url": f"https://example.org/{i}",
                            "created_on_time": 1700000000 + int(i),
                            "is_saved": 0,
                            "is_read": 0,
                        }
                        for i in ids
                    ],
                }
            )

        source = FreshRSSSource(HttpSource("http://reader", "secret", opener=open_request))
        items, total = source.entries(limit=70)
        self.assertEqual((len(items), total, len(calls)), (70, 75, 3))
        self.assertEqual(items[0]["id"], 75)
        self.assertEqual(items[0]["feed"], "Public source")
        self.assertEqual(items[0]["title"], "Test")
        self.assertEqual(items[0]["hash"], "freshrss:75")

    def test_auth_failure_is_not_empty_success(self):
        source = FreshRSSSource(
            HttpSource("http://reader", opener=lambda *a, **k: Response({"auth": 0}))
        )
        with self.assertRaises(SourceError):
            source.entries()

    def test_unsupported_status_fails(self):
        with self.assertRaises(SourceError):
            FreshRSSSource(HttpSource("http://reader")).entries(status="read")

    def test_star_is_idempotent_and_checks_acknowledgement(self):
        calls = []

        def open_request(req, timeout):
            form = urllib.parse.parse_qs(req.data.decode())
            calls.append(form)
            return Response({"auth": 1, "saved_item_ids": "7" if form["as"] == ["saved"] else ""})

        source = FreshRSSSource(HttpSource("http://reader", "secret", opener=open_request))
        self.assertTrue(source.star(7))
        self.assertTrue(source.star(7))
        self.assertTrue(source.star(7, on=False))
        self.assertEqual([c["as"][0] for c in calls], ["saved", "saved", "unsaved"])

    def test_miniflux_rejects_non_object_star_state(self):
        http = Mock()
        http.request.return_value = (200, [])
        with self.assertRaises(SourceError):
            MinifluxSource(http).star(1)

    def test_source_auth_header_is_not_redirected(self):
        def request(req, timeout):
            self.assertEqual(req.get_header("X-auth-token"), "secret")
            self.assertNotIn("X-auth-token", req.headers)
            return Response({})

        HttpSource("http://reader", "secret", opener=request).request("/v1/feeds")

    def test_factory_selects_freshrss(self):
        self.assertIsInstance(source_for(Settings(source_type="freshrss")), FreshRSSSource)


class ProviderTests(unittest.TestCase):
    def test_wire_contracts(self):
        replies = {
            "chat": {
                "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
            "responses": {
                "status": "completed",
                "output": [
                    {"type": "reasoning"},
                    {"type": "message", "content": [{"type": "output_text", "text": "[]"}]},
                ],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
            "anthropic": {
                "stop_reason": "end_turn",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "[]"},
                ],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        }
        for protocol, reply in replies.items():
            with self.subTest(protocol=protocol):

                def request(req, timeout, protocol=protocol, reply=reply):
                    body = json.loads(req.data)
                    self.assertEqual(body["model"], "test-frontier")
                    if protocol == "responses":
                        self.assertEqual(body["input"], "prompt")
                        self.assertEqual(body["max_output_tokens"], 6000)
                    else:
                        self.assertEqual(body["messages"][0]["content"], "prompt")
                    if protocol == "anthropic":
                        self.assertEqual(req.get_header("X-api-key"), "secret")
                        self.assertNotIn("X-api-key", req.headers)
                        self.assertIsNone(req.get_header("Authorization"))
                    else:
                        self.assertEqual(req.get_header("Authorization"), "Bearer secret")
                        self.assertNotIn("Authorization", req.headers)
                    return Response(reply)

                text, usage = call_judge(
                    "prompt",
                    Settings(judge_api=protocol, judge_model="test-frontier", judge_key="secret"),
                    request,
                )
                self.assertEqual(text, "[]")
                self.assertEqual(usage, {"prompt_tokens": 3, "completion_tokens": 4})

    def test_incomplete_output_is_rejected(self):
        for protocol, data in [
            ("chat", {"choices": [{"finish_reason": "length"}]}),
            ("responses", {"status": "incomplete"}),
            ("anthropic", {"stop_reason": "max_tokens"}),
        ]:
            with self.subTest(protocol=protocol), self.assertRaises(JudgeError):
                call_judge(
                    "p", Settings(judge_api=protocol), lambda *a, data=data, **k: Response(data)
                )

    def test_network_error_is_judge_error(self):
        with self.assertRaises(JudgeError):
            call_judge("p", Settings(), Mock(side_effect=urllib.error.URLError("no network")))

    def test_config_rejects_unknown_protocol_and_unauthenticated_notify(self):
        for env in [
            {"JUDGE_API": "unknown"},
            {"SOURCE_TYPE": "unknown"},
            {"NOTIFY_URL": "http://orchestrator"},
            {"CRITIC_BATCH": "0"},
        ]:
            with self.subTest(env=env), self.assertRaises(ConfigError):
                Settings.from_env(env)


class StateTests(unittest.TestCase):
    def test_service_dry_run_does_not_write_or_star(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "verdicts.jsonl"
            s = Settings(log_path=str(path))
            source = Mock()
            source.entries.return_value = (
                [{"id": 1, "title": "x", "url": "https://example.org"}],
                1,
            )
            result = Mock()
            result.as_dict.return_value = {"failures": [], "items": []}
            result.failures = []
            with (
                patch("glm_critic.service.source_for", return_value=source),
                patch("glm_critic.service.critique", return_value=result) as judge,
            ):
                run_once(s, Rubric(), dry_run=True, star=True)
            self.assertTrue(judge.call_args.kwargs["dry_run"])
            source.star.assert_not_called()
            self.assertFalse(path.exists())

    def test_provider_and_source_changes_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            log = VerdictLog(Path(directory) / "v.jsonl")
            s = Settings()
            items = [{"id": 1, "title": "A", "url": "https://example.org"}]
            judge = Mock(return_value=('[{"i":0,"v":"signal","s":9}]', {}))
            critique(items, s, Rubric(), log, judge_call=judge)
            critique(items, s, Rubric(), log, judge_call=judge)
            self.assertEqual(judge.call_count, 1)
            for key, value in [
                ("judge_model", "another"),
                ("judge_url", "http://other"),
                ("judge_api", "responses"),
                ("source_url", "http://reader"),
            ]:
                critique(items, replace(s, **{key: value}), Rubric(), log, judge_call=judge)
            self.assertEqual(judge.call_count, 5)

    def test_retry_usage_counts_both_paid_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            judge = Mock(
                side_effect=[
                    ("prose", {"prompt_tokens": 10, "completion_tokens": 20}),
                    ('[{"i":0,"s":9}]', {"prompt_tokens": 3, "completion_tokens": 4}),
                ]
            )
            r = critique(
                [{"id": 1, "title": "A"}],
                Settings(),
                Rubric(),
                VerdictLog(Path(directory) / "v.jsonl"),
                judge_call=judge,
            )
            self.assertEqual((r.prompt_tokens, r.completion_tokens), (13, 24))

    def test_notification_auth_replay_and_failed_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            s = Settings(
                log_path=str(Path(directory) / "v.jsonl"),
                notify_url="http://orchestrator",
                notify_token="secret",
            )
            payload = {"items": [{"notification_key": "one"}, {"notification_key": "two"}]}
            with patch("glm_critic.service.notify", return_value=False):
                self.assertFalse(notify_pending(s, payload))
            with patch("glm_critic.service.notify", return_value=True) as send:
                self.assertTrue(notify_pending(s, payload))
                self.assertFalse(notify_pending(s, payload))
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.kwargs["token"], "secret")
                self.assertTrue(
                    notify_pending(s, {"items": payload["items"] + [{"notification_key": "three"}]})
                )
                self.assertEqual(send.call_args.args[1]["signals"], 1)


if __name__ == "__main__":
    unittest.main()
