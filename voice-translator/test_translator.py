import json
import os
import stat
import tempfile
import time
import unittest
from unittest import mock

import translator

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "echo_run.jsonl")
FIXTURE_402 = os.path.join(HERE, "fixtures", "meta_402.jsonl")
FIXTURE_STREAM = os.path.join(HERE, "fixtures", "agy_stream.jsonl")

STUB_SESSION = r"""#!/bin/sh
# Fake `agy --input-format stream-json`: answers every stdin line with a streamed reply + result.
printf '%s\n' '{"event":"init","conversation_id":"c1","init":{"model":"stub"}}'
while IFS= read -r line; do
  case "$line" in *DIE*) exit 3;; esac
  printf '%s\n' '{"event":"step_update","step_update":{"step_type":"agent_response","state":"ACTIVE","text_delta":"Kumus"}}'
  printf '%s\n' '{"event":"step_update","step_update":{"step_type":"agent_response","state":"DONE","text_delta":"ta"}}'
  printf '%s\n' "{\"event\":\"result\",\"result\":{\"status\":\"SUCCESS\",\"response\":\"Kumusta pid=$$\"}}"
done
"""


class BuildPromptTest(unittest.TestCase):
    def test_ko_to_tl_names_languages_and_wraps_text(self):
        p = translator.build_prompt("안녕하세요", "ko", "tl")
        self.assertIn("Korean", p)
        self.assertIn("Tagalog", p)
        self.assertIn("<text>\n안녕하세요\n</text>", p)
        self.assertIn("ONLY the translation", p)

    def test_tl_to_ko(self):
        p = translator.build_prompt("Magandang umaga", "tl", "ko")
        self.assertIn("from Tagalog", p)
        self.assertIn("into Korean", p)

    def test_unknown_language_rejected(self):
        with self.assertRaises(ValueError):
            translator.build_prompt("x", "ko", "xx")


class ParseMuseLineTest(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE, encoding="utf-8") as f:
            self.lines = [l for l in f.read().splitlines() if l.strip()]

    def test_delta_and_terminal_events_are_recognised(self):
        kinds = [translator.parse_muse_line(l) for l in self.lines]
        deltas = [k for k in kinds if k and k[0] == "delta"]
        dones = [k for k in kinds if k and k[0] == "done"]
        self.assertEqual(deltas, [("delta", "echo: Translate: 안녕하세요")])
        self.assertEqual(dones, [("done", "echo: Translate: 안녕하세요")])

    def test_irrelevant_events_and_garbage_return_none(self):
        self.assertIsNone(translator.parse_muse_line(self.lines[0]))
        self.assertIsNone(translator.parse_muse_line("not json"))
        self.assertIsNone(translator.parse_muse_line(""))

    def test_completed_with_empty_text_becomes_error(self):
        rec = json.loads(self.lines[-1])
        rec["payload"]["text"] = "   "
        kind, msg = translator.parse_muse_line(json.dumps(rec))
        self.assertEqual(kind, "error")
        self.assertIn("empty", msg)

    def test_failed_terminal_becomes_error(self):
        rec = json.loads(self.lines[-1])
        rec["payload"]["terminal"] = "failed"
        rec["payload"]["reason"] = "boom"
        rec["payload"]["text"] = ""
        self.assertEqual(translator.parse_muse_line(json.dumps(rec)), ("error", "boom"))


class ProviderStatusTest(unittest.TestCase):
    """Real `muse exec --json` output from a run where the Meta API answered 402 on every attempt."""

    def setUp(self):
        with open(FIXTURE_402, encoding="utf-8") as f:
            self.lines = [l for l in f.read().splitlines() if l.strip()]
        self.by_seq = {json.loads(l)["sequence"]: l for l in self.lines}

    def test_opening_stream_is_a_status_event(self):
        kind, msg = translator.parse_muse_line(self.by_seq[12])
        self.assertEqual(kind, "status")
        self.assertIn("attempt 1/10", msg)

    def test_402_retry_is_a_fatal_error_with_billing_hint(self):
        kind, msg = translator.parse_muse_line(self.by_seq[13])
        self.assertEqual(kind, "error")
        self.assertIn("402", msg)
        self.assertIn(translator.META_BILLING_URL, msg)

    def test_transient_http_retry_stays_a_status(self):
        rec = json.loads(self.by_seq[13])
        rec["payload"]["event"]["details"]["facets"][0]["http_status"] = 503
        rec["payload"]["event"]["message"] = "retrying meta model stream in 1000ms (attempt 2/10)"
        kind, msg = translator.parse_muse_line(json.dumps(rec))
        self.assertEqual(kind, "status")
        self.assertIn("503", msg)

    def test_backend_fails_fast_on_402_instead_of_waiting_for_retries(self):
        with tempfile.TemporaryDirectory() as d:
            stub = os.path.join(d, "muse")
            with open(stub, "w") as f:
                f.write(f"#!/bin/sh\nhead -13 '{FIXTURE_402}'\nexec sleep 60\n")
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            t0 = time.monotonic()
            events = list(translator.MuseBackend(muse_bin=stub).translate("안녕", "ko", "tl"))
            self.assertLess(time.monotonic() - t0, 10)
            self.assertEqual(events[-1][0], "error")
            self.assertIn("402", events[-1][1])
            self.assertTrue(any(e[0] == "status" for e in events))


class CleanOutputTest(unittest.TestCase):
    def test_strips_wrapping_quotes_and_whitespace(self):
        self.assertEqual(translator.clean_output('  "Kumusta"  \n'), "Kumusta")

    def test_strips_code_fence(self):
        self.assertEqual(translator.clean_output("```\nKumusta\n```"), "Kumusta")

    def test_plain_text_unchanged(self):
        self.assertEqual(translator.clean_output("Kumusta, kaibigan."), "Kumusta, kaibigan.")


class MuseCommandTest(unittest.TestCase):
    def test_command_uses_prompt_file_json_and_single_step(self):
        cmd = translator.muse_command("/tmp/p.txt", muse_bin="/x/muse")
        self.assertEqual(cmd[:2], ["/x/muse", "exec"])
        self.assertIn("--json", cmd)
        self.assertIn("--prompt-file", cmd)
        self.assertIn("/tmp/p.txt", cmd)
        self.assertIn("--max-model-steps", cmd)
        self.assertEqual(cmd[cmd.index("--max-model-steps") + 1], "1")
        self.assertIn("--user-input-auto-resolve", cmd)
        self.assertEqual(cmd[cmd.index("--reasoning-effort") + 1], "minimal")

    def test_echo_provider_flag(self):
        cmd = translator.muse_command("/tmp/p.txt", muse_bin="/x/muse", provider="echo")
        self.assertEqual(cmd[cmd.index("--provider") + 1], "echo")


class OllamaRequestTest(unittest.TestCase):
    def test_request_disables_hidden_thinking(self):
        """gemma4 spends ~1000 hidden reasoning tokens per sentence unless think is off (37s vs 1s measured)."""
        captured = {}

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self):
                yield json.dumps({"message": {"content": "Kumusta"}, "done": True}).encode()

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return FakeResponse()

        with mock.patch.object(translator.urllib.request, "urlopen", fake_urlopen):
            events = list(translator.OllamaBackend().translate("안녕", "ko", "tl"))
        self.assertIs(captured["body"].get("think"), False)
        self.assertEqual(events[-1], ("done", "Kumusta"))


class AgyBackendTest(unittest.TestCase):
    """Antigravity CLI (`agy -p --output-format json`) using the user's subscription login."""

    def test_command_uses_print_json_and_model(self):
        cmd = translator.agy_command("PROMPT", agy_bin="/x/agy", model="gemini-3.7-flash-low")
        self.assertEqual(cmd[0], "/x/agy")
        self.assertEqual(cmd[cmd.index("-p") + 1], "PROMPT")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-3.7-flash-low")
        self.assertIn("--disable-slash-commands", cmd)

    def test_parse_success(self):
        out = '{"conversation_id":"x","status":"SUCCESS","response":"Nasaan po ang banyo?\\n","duration_seconds":1.4}'
        self.assertEqual(translator.parse_agy_output(out), ("done", "Nasaan po ang banyo?"))

    def test_parse_failure_status(self):
        out = '{"status":"ERROR","response":"","error":"quota exceeded"}'
        kind, msg = translator.parse_agy_output(out)
        self.assertEqual(kind, "error")
        self.assertIn("quota exceeded", msg)

    def test_parse_garbage(self):
        kind, msg = translator.parse_agy_output("not json at all")
        self.assertEqual(kind, "error")
        self.assertIn("not json", msg)

    def test_stub_binary_round_trip_passes_prompt_as_argument(self):
        with tempfile.TemporaryDirectory() as d:
            stub = os.path.join(d, "agy")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\n"
                        "case \"$*\" in *'<text>'*) echo '{\"status\":\"SUCCESS\",\"response\":\"Kumusta\"}';;"
                        " *) echo '{\"status\":\"ERROR\",\"error\":\"no prompt argument\"}';; esac\n")
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            events = list(translator.AgyBackend(agy_bin=stub).translate("안녕", "ko", "tl"))
            self.assertEqual(events[-1], ("done", "Kumusta"))

    def test_hung_agy_is_killed(self):
        with tempfile.TemporaryDirectory() as d:
            stub = os.path.join(d, "agy")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\nexec sleep 60\n")
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            t0 = time.monotonic()
            with mock.patch.object(translator, "MUSE_TIMEOUT_SECONDS", 1):
                events = list(translator.AgyBackend(agy_bin=stub).translate("안녕", "ko", "tl"))
            self.assertLess(time.monotonic() - t0, 10)
            self.assertEqual(events[-1][0], "error")
            self.assertIn("timed out", events[-1][1])


class AgyStreamParseTest(unittest.TestCase):
    """Real `agy --output-format stream-json` events (fixtures/agy_stream.jsonl)."""

    def setUp(self):
        with open(FIXTURE_STREAM, encoding="utf-8") as f:
            self.lines = [l for l in f.read().splitlines() if l.strip()]

    def test_turn_boundaries_and_deltas(self):
        kinds = [translator.parse_stream_line(l) for l in self.lines]
        self.assertIsNone(kinds[0])                                   # init
        self.assertIsNone(kinds[1])                                   # user_input step
        self.assertEqual(kinds[2], ("delta", "Kamusta, kamusta\n"))
        self.assertEqual(kinds[3], ("result", "Kamusta, kamusta"))
        results = [k for k in kinds if k and k[0] == "result"]
        self.assertEqual([r[1] for r in results], ["Kamusta, kamusta", "화장실이 어디에 있나요?"])
        deltas = [k for k in kinds[4:] if k and k[0] == "delta"]
        self.assertEqual("".join(d[1] for d in deltas).strip(), "화장실이 어디에 있나요?")

    def test_error_result_and_garbage(self):
        self.assertEqual(translator.parse_stream_line('{"event":"result","result":{"status":"ERROR","error":"quota"}}'),
                         ("error", "agy ERROR: quota"))
        self.assertIsNone(translator.parse_stream_line("garbage"))

    def test_turn_prompt_names_direction(self):
        p = translator.session_turn_prompt("안녕", "ko", "tl")
        self.assertIn("Korean", p); self.assertIn("Tagalog", p); self.assertIn("안녕", p)
        with self.assertRaises(ValueError):
            translator.session_turn_prompt("x", "ko", "xx")


class AgySessionTest(unittest.TestCase):
    """One long-lived agy process serves many sentences; it is restarted when it dies."""

    def _stub(self, d):
        stub = os.path.join(d, "agy")
        with open(stub, "w") as f:
            f.write(STUB_SESSION)
        os.chmod(stub, 0o755)
        return stub

    def test_reuses_one_process_across_turns_and_streams_deltas(self):
        with tempfile.TemporaryDirectory() as d:
            b = translator.AgySessionBackend(agy_bin=self._stub(d))
            try:
                e1 = list(b.translate("안녕", "ko", "tl"))
                e2 = list(b.translate("고마워", "ko", "tl"))
            finally:
                b.close()
            self.assertEqual([k for k, _ in e1 if k == "delta"], ["delta", "delta"])
            self.assertTrue(e1[-1][1].startswith("Kumusta pid="))
            self.assertEqual(e1[-1][1], e2[-1][1])  # same pid -> same process

    def test_restarts_after_process_death(self):
        with tempfile.TemporaryDirectory() as d:
            b = translator.AgySessionBackend(agy_bin=self._stub(d))
            try:
                first = list(b.translate("안녕", "ko", "tl"))[-1][1]
                dead = list(b.translate("DIE", "ko", "tl"))
                self.assertEqual(dead[-1][0], "error")
                again = list(b.translate("다시", "ko", "tl"))[-1]
            finally:
                b.close()
            self.assertEqual(again[0], "done")
            self.assertNotEqual(again[1], first)  # a new process answered

    def test_turn_timeout_restarts_session(self):
        with tempfile.TemporaryDirectory() as d:
            stub = os.path.join(d, "agy")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\necho '{\"event\":\"init\"}'\nexec sleep 60\n")
            os.chmod(stub, 0o755)
            b = translator.AgySessionBackend(agy_bin=stub, turn_timeout=1)
            try:
                t0 = time.monotonic()
                ev = list(b.translate("안녕", "ko", "tl"))
            finally:
                b.close()
            self.assertLess(time.monotonic() - t0, 10)
            self.assertEqual(ev[-1][0], "error")
            self.assertIn("timed out", ev[-1][1])


class BackendFactoryTest(unittest.TestCase):
    def test_agy_backend(self):
        self.assertIsInstance(translator.get_backend("agy"), translator.AgySessionBackend)
        self.assertIsInstance(translator.get_backend("agy-oneshot"), translator.AgyBackend)

    def test_known_backends(self):
        self.assertIsInstance(translator.get_backend("muse"), translator.MuseBackend)
        self.assertIsInstance(translator.get_backend("echo"), translator.MuseBackend)
        self.assertIsInstance(translator.get_backend("ollama"), translator.OllamaBackend)

    def test_unknown_backend(self):
        with self.assertRaises(ValueError):
            translator.get_backend("nope")


class MuseTimeoutTest(unittest.TestCase):
    """A muse that produces no output must be killed after MUSE_TIMEOUT_SECONDS."""

    def test_hung_process_is_killed_and_reported(self):
        with tempfile.TemporaryDirectory() as d:
            stub = os.path.join(d, "muse")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\nexec sleep 60\n")
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            backend = translator.MuseBackend(muse_bin=stub)
            t0 = time.monotonic()
            with mock.patch.object(translator, "MUSE_TIMEOUT_SECONDS", 1):
                events = list(backend.translate("안녕", "ko", "tl"))
            self.assertLess(time.monotonic() - t0, 10)
            self.assertEqual(events[-1][0], "error")
            self.assertIn("timed out", events[-1][1])


class EchoBackendIntegrationTest(unittest.TestCase):
    """Runs the real muse binary with the echo provider (no credentials needed)."""

    def test_stream_yields_deltas_then_done(self):
        if not os.path.exists(translator.DEFAULT_MUSE_BIN):
            self.skipTest("muse not installed")
        events = list(translator.get_backend("echo").translate("안녕하세요", "ko", "tl"))
        self.assertEqual(events[-1][0], "done")
        self.assertIn("안녕하세요", events[-1][1])
        self.assertTrue(any(e[0] == "delta" for e in events))


if __name__ == "__main__":
    unittest.main()
