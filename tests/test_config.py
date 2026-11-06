import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coord.config import load_env_file


class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_load_explicit_file(self) -> None:
        env_file = self.dir / "custom.env"
        env_file.write_text(
            """
# comment line
COORD_API_KEY=test-key-123
COORD_MODEL="gemini-2.5-pro"
COORD_BACKEND='claude-sdk'
EMPTY_VAL=
            """,
            encoding="utf-8",
        )

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env_file(env_file)
            self.assertEqual(loaded, env_file)
            self.assertEqual(os.environ.get("COORD_API_KEY"), "test-key-123")
            self.assertEqual(os.environ.get("COORD_MODEL"), "gemini-2.5-pro")
            self.assertEqual(os.environ.get("COORD_BACKEND"), "claude-sdk")
            self.assertIsNone(os.environ.get("EMPTY_VAL"))

    def test_does_not_override_existing_env(self) -> None:
        env_file = self.dir / ".env"
        env_file.write_text("COORD_MODEL=gemini-2.5-flash\n", encoding="utf-8")

        with patch.dict(os.environ, {"COORD_MODEL": "user-override-model"}, clear=True):
            load_env_file(env_file)
            self.assertEqual(os.environ.get("COORD_MODEL"), "user-override-model")

    def test_missing_explicit_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_env_file(self.dir / "non-existent.env")
