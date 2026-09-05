import os
import sqlite3
import tempfile
import unittest

import storage


class ConversationStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "conv.db")
        self.store = storage.ConversationStore(self.path)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_creates_database_file_with_tables(self):
        self.assertTrue(os.path.exists(self.path))
        with sqlite3.connect(self.path) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"sessions", "turns"} <= names)

    def test_save_turn_stores_text_and_languages(self):
        # Arrange / Act
        turn_id = self.store.save_turn("sess-1", "ko", "tl", "안녕하세요", "Kumusta po")

        # Assert
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT session_id, source, target, source_text, translated_text, created_at "
                             "FROM turns WHERE id = ?", (turn_id,)).fetchone()
        self.assertEqual(row[:5], ("sess-1", "ko", "tl", "안녕하세요", "Kumusta po"))
        self.assertRegex(row[5], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_turns_in_same_session_share_one_session_row(self):
        self.store.save_turn("sess-1", "ko", "tl", "하나", "isa")
        self.store.save_turn("sess-1", "tl", "ko", "dalawa", "둘")
        self.store.save_turn("sess-2", "ko", "tl", "셋", "tatlo")
        with sqlite3.connect(self.path) as db:
            sessions = db.execute("SELECT id FROM sessions ORDER BY id").fetchall()
            per_session = db.execute("SELECT session_id, COUNT(*) FROM turns GROUP BY session_id "
                                     "ORDER BY session_id").fetchall()
        self.assertEqual(sessions, [("sess-1",), ("sess-2",)])
        self.assertEqual(per_session, [("sess-1", 2), ("sess-2", 1)])

    def test_turns_are_ordered_by_insertion(self):
        first = self.store.save_turn("s", "ko", "tl", "a", "b")
        second = self.store.save_turn("s", "ko", "tl", "c", "d")
        self.assertLess(first, second)

    def test_reopening_keeps_existing_rows(self):
        self.store.save_turn("s", "ko", "tl", "a", "b")
        self.store.close()
        self.store = storage.ConversationStore(self.path)
        self.store.save_turn("s", "ko", "tl", "c", "d")
        with sqlite3.connect(self.path) as db:
            count = db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        self.assertEqual(count, 2)


class SessionIdTest(unittest.TestCase):
    def test_accepts_uuid_and_url_safe_ids(self):
        self.assertTrue(storage.is_valid_session_id("8f1c2a0e-3b4d-4c5e-9f6a-7b8c9d0e1f2a"))
        self.assertTrue(storage.is_valid_session_id("abcDEF123_-xyz"))

    def test_rejects_missing_short_long_or_odd_ids(self):
        self.assertFalse(storage.is_valid_session_id(None))
        self.assertFalse(storage.is_valid_session_id(""))
        self.assertFalse(storage.is_valid_session_id("short"))
        self.assertFalse(storage.is_valid_session_id("x" * 65))
        self.assertFalse(storage.is_valid_session_id("has space here"))
        self.assertFalse(storage.is_valid_session_id(12345678))


if __name__ == "__main__":
    unittest.main()
