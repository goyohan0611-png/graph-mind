"""Which search model an install uses: new memories English, existing ones multilingual,
an explicit choice (turnbook model, GRAPH_MIND_MODEL) always wins."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from graph_mind.local_embedder import PRESETS, chosen_model


class ModelChoiceTests(unittest.TestCase):
    def test_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.dict(os.environ, {"GRAPH_MIND_HOME": directory, "GRAPH_MIND_MODEL": ""}):
                self.assertEqual(chosen_model(), PRESETS["english"])
                (home / "development.semantic-embed-mpnet8.vec").mkdir()
                self.assertEqual(chosen_model(), PRESETS["multilingual"])
                (home / "model.txt").write_text("english\n", encoding="utf-8")
                self.assertEqual(chosen_model(), PRESETS["english"])
                with mock.patch.dict(os.environ, {"GRAPH_MIND_MODEL": "multilingual"}):
                    self.assertEqual(chosen_model(), PRESETS["multilingual"])


if __name__ == "__main__":
    unittest.main()
