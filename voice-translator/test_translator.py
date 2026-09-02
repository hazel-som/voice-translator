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


class BackendFactoryTest(unittest.TestCase):
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
