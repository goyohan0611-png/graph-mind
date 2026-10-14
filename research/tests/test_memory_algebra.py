import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import unittest
from datetime import datetime

from memory_algebra import (MemoryAtom, count, date_diff, earliest, latest,
                            subtract, sum_values)


def atom(kind, value, event, unit=None, date=None):
    return MemoryAtom(kind, value, (event,), date, unit)


class MemoryAlgebraTests(unittest.TestCase):
    def test_latest_and_earliest_preserve_winning_evidence(self):
        old = atom("TEXT", "old", "e1", date=datetime(2024, 1, 1))
        new = atom("TEXT", "new", "e2", date=datetime(2024, 2, 1))
        self.assertEqual(earliest([new, old]).evidence_event_ids, ("e1",))
        self.assertEqual(latest([old, new]).value, "new")

    def test_count_distinct_uses_only_counted_evidence(self):
        first = atom("ENTITY", "museum-a", "e1")
        duplicate = atom("ENTITY", "museum-a", "e2")
        second = atom("ENTITY", "museum-b", "e3")
        result = count([first, duplicate, second])
        self.assertEqual(result.value, 2)
        self.assertEqual(result.evidence_event_ids, ("e1", "e3"))

    def test_sum_and_subtract_require_compatible_units(self):
        ten = atom("MONEY", 10, "e1", "USD")
        three = atom("MONEY", 3, "e2", "USD")
        self.assertEqual(sum_values([ten, three]).value, 13)
        self.assertEqual(subtract(ten, three).value, 7)
        incompatible = atom("MONEY", 3, "e3", "EUR")
        self.assertEqual(sum_values([ten, incompatible]).status, "UNKNOWN")

    def test_date_difference_keeps_both_sources(self):
        start = atom("DATE", "2024-01-01", "e1")
        end = atom("DATE", "2024-01-15", "e2")
        result = date_diff(start, end)
        self.assertEqual((result.value, result.unit), (14, "days"))
        self.assertEqual(result.evidence_event_ids, ("e1", "e2"))


if __name__ == "__main__":
    unittest.main()
