from pathlib import Path
import unittest

from longmemeval_ingestion_v7 import (
    deterministic_entity_id, normalized, sanitize_enrichment, select_sessions,
)


class IngestionV7Tests(unittest.TestCase):
    def test_backend_entity_id_is_stable(self):
        a = {"object_canonical_name": "The Art Cube", "object_type": "PLACE"}
        b = {"object_canonical_name": "the-art cube", "object_type": "PLACE"}
        self.assertEqual(deterministic_entity_id(a), deterministic_entity_id(b))

    def test_backend_entity_id_is_namespaced_by_type(self):
        place = {"object_canonical_name": "Apple", "object_type": "PLACE"}
        organization = {"object_canonical_name": "Apple", "object_type": "ORGANIZATION"}
        self.assertNotEqual(deterministic_entity_id(place), deterministic_entity_id(organization))

    def test_normalization(self):
        self.assertEqual(normalized("Natural-History Museum!"), "natural history museum")

    @unittest.skipUnless(Path("external/longmemeval/longmemeval_s_cleaned.json").exists(),
                         "LongMemEval data not downloaded (see README)")
    def test_selection_is_frozen_to_twelve(self):
        rows = select_sessions()
        self.assertEqual(len(rows), 12)
        self.assertEqual(sum(row["selection_role"] == "targeted_failure_case" for row in rows), 5)

    def test_unsupported_alias_is_removed(self):
        event = {"object_text": "The Art Cube", "object_canonical_name": "The Art Cube",
                 "object_aliases": ["contact The Art Cube", "The Art Cube"],
                 "category_tags": [], "source_turn_ids": ["TURN_000"]}
        clean, removed, removed_tags = sanitize_enrichment(
            event, "TURN_000 [user]: I visited The Art Cube yesterday.")
        self.assertEqual(clean["object_aliases"], ["The Art Cube"])
        self.assertEqual(removed, ["contact The Art Cube"])
        self.assertEqual(removed_tags, [])

    def test_category_can_use_entity_specific_corroborating_turn(self):
        event = {"object_text": "The Art Cube", "object_canonical_name": "The Art Cube",
                 "object_aliases": ["The Art Cube"], "category_tags": ["GALLERY"],
                 "source_turn_ids": ["TURN_000"]}
        content = ("TURN_000 [user]: I visited The Art Cube.\n"
                   "TURN_002 [user]: The Art Cube is a new gallery.")
        clean, _, removed_tags = sanitize_enrichment(event, content)
        self.assertEqual(clean["category_tags"], ["GALLERY"])
        self.assertEqual(clean["category_source_turn_ids"], {"GALLERY": ["TURN_002"]})
        self.assertEqual(removed_tags, [])


if __name__ == "__main__":
    unittest.main()
