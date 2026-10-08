import tempfile
import unittest
from pathlib import Path

from associative_memory import AssociativeMemoryIndex
from semantic_recall import auto_associate, semantic_associate


class FakeEmbedder:
    """Deterministic keyword-axis embedder so cosine is fully controllable, no torch/network."""

    def __init__(self, axis):
        self.axis = [w.casefold() for w in axis]

    def embed(self, texts):
        out = []
        for t in texts:
            low = t.casefold()
            # all-zero when nothing matches: cosine() treats a zero-norm vector as similarity 0
            out.append([1.0 if w in low else 0.0 for w in self.axis])
        return out


def _index(path):
    ix = AssociativeMemoryIndex(path)
    ix.add_engram(engram_id="development:a", source_kind="DEVELOPMENT_EVENT", source_id="a",
                  scope="project:x", happened_at="2026-01-01T00:00:00", strength=0.6,
                  text="soak stability experiment memory", cues=["soak"])
    ix.add_engram(engram_id="development:b", source_kind="DEVELOPMENT_EVENT", source_id="b",
                  scope="project:x", happened_at="2026-01-02T00:00:00", strength=0.6,
                  text="dental appointment schedule", cues=["dental"])
    return ix


class SemanticRecallTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.index = _index(Path(self.dir.name) / "assoc.sqlite")

    def tearDown(self):
        self.index.close()
        self.dir.cleanup()

    def test_known_when_cue_semantically_matches_seed(self):
        emb = FakeEmbedder(["soak", "stability"])
        out = semantic_associate(self.index, "안정성 시험", ["soak stability"],
                                 embedder=emb, threshold=0.35)
        self.assertEqual(out["status"], "KNOWN")
        self.assertIn("development:a", [r["engram_id"] for r in out["results"]])
        self.assertGreaterEqual(out["semantic_coverage"], 0.35)

    def test_abstains_when_fts_matches_but_embedding_is_unrelated(self):
        # cue "soak" retrieves engram a via FTS, but the embedding axis shares nothing -> gate shut
        emb = FakeEmbedder(["xray", "dental"])
        out = semantic_associate(self.index, "무언가", ["soak"], embedder=emb, threshold=0.35)
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertEqual(out["reason"], "INSUFFICIENT_SEMANTIC_COVERAGE")
        self.assertEqual(out["results"], [])

    def test_no_seed_when_cues_match_nothing(self):
        out = semantic_associate(self.index, "질문", ["zzz nonexistent term"],
                                 embedder=FakeEmbedder(["soak"]), threshold=0.35)
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertEqual(out["reason"], "NO_ACTIVATION_SEED")

    def test_planner_abstain_yields_unknown(self):
        out = semantic_associate(self.index, "질문", [], embedder=FakeEmbedder(["soak"]))
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertEqual(out["reason"], "PLANNER_ABSTAINED")

    def test_auto_uses_literal_when_literal_gate_passes(self):
        # cue fully overlaps engram a's signature -> literal coverage 1.0 >= 0.75 -> no embedder needed
        out = auto_associate(self.index, "질문", ["soak stability experiment memory"],
                             embedder=FakeEmbedder(["nothing"]))
        self.assertEqual(out["grounding"], "auto:literal")
        self.assertIn("development:a", [r["engram_id"] for r in out["results"]])

    def test_auto_falls_back_to_semantic_when_literal_abstains(self):
        # cue has a term absent from the signature -> literal coverage 0.5 < 0.75 -> semantic path
        out = auto_associate(self.index, "질문", ["soak zzzznovelterm"],
                             embedder=FakeEmbedder(["soak"]))
        self.assertEqual(out["grounding"], "auto:semantic")
        self.assertEqual(out["status"], "KNOWN")
        self.assertIn("development:a", [r["engram_id"] for r in out["results"]])


if __name__ == "__main__":
    unittest.main()
