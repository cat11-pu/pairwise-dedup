"""dedup.core 的验收测试。

只断言期望的判定结果与不变量：首次与重复标记、窗口边界、精确集合与近似
结构的协同、容量淘汰、乱序到达、首次刻度与累计计数。
"""

import unittest

from dedup import DedupWindow


def observe_all(window, arrivals):
    """按顺序喂入 (键, 刻度) 到达，返回每次的判定。"""
    return [window.observe(tick, key) for key, tick in arrivals]


class FirstAndRepeatTests(unittest.TestCase):

    def test_first_arrival_is_flagged_once_and_the_repeat_is_a_duplicate(self):
        window = DedupWindow(span=10, capacity=8)
        first = window.observe(1, "alpha")
        self.assertTrue(first.accepted)
        self.assertTrue(first.first)
        self.assertFalse(first.duplicate)
        self.assertEqual(first.occurrences, 1)
        self.assertEqual(first.evicted, ())
        repeat = window.observe(3, "alpha")
        self.assertTrue(repeat.accepted)
        self.assertTrue(repeat.duplicate)
        self.assertFalse(repeat.first)
        self.assertEqual(repeat.occurrences, 2)
        self.assertEqual(window.occurrences("alpha"), 2)
        self.assertEqual(window.key_count(), 1)
        self.assertEqual(window.size(), 2)


class WindowEdgeTests(unittest.TestCase):

    def test_an_occurrence_exactly_at_the_window_edge_has_left(self):
        window = DedupWindow(span=10, capacity=8)
        window.observe(2, "alpha")
        window.observe(12, "bravo")
        self.assertFalse(window.seen("alpha"))
        self.assertEqual(window.occurrences("alpha"), 0)
        self.assertIsNone(window.first_tick("alpha"))
        self.assertEqual(window.stats()["expired"], 1)
        again = window.observe(12, "alpha")
        self.assertTrue(again.first)
        self.assertFalse(again.duplicate)
        self.assertEqual(again.occurrences, 1)
        self.assertEqual(window.advance(22), 2)
        self.assertEqual(window.size(), 0)
        self.assertEqual(window.stats()["expired"], 3)


class SketchCooperationTests(unittest.TestCase):

    def test_a_key_inside_the_window_survives_another_key_expiring(self):
        window = DedupWindow(span=10, capacity=8)
        window.observe(1, "alpha")
        window.observe(2, "alpha")
        window.observe(9, "bravo")
        window.observe(13, "charlie")
        self.assertEqual(window.stats()["expired"], 2)
        self.assertEqual(window.occurrences("bravo"), 1)
        self.assertEqual(window.first_tick("bravo"), 9)
        self.assertTrue(window.seen("bravo"))
        repeat = window.observe(14, "bravo")
        self.assertTrue(repeat.duplicate)
        self.assertFalse(repeat.first)
        self.assertEqual(repeat.occurrences, 2)

    def test_keys_that_never_arrived_are_not_reported_as_duplicates(self):
        window = DedupWindow(span=1000, capacity=64, sketch_bits=32,
                             sketch_hashes=2, fill_limit=1.0)
        for index in range(20):
            window.observe(index + 1, "member-%02d" % index)
        for index in range(20):
            visitor = "visitor-%02d" % index
            self.assertFalse(window.seen(visitor), visitor)
        verdict = window.observe(21, "visitor-00")
        self.assertFalse(verdict.duplicate)
        self.assertTrue(verdict.first)
        self.assertEqual(window.stats()["duplicates"], 0)
        self.assertEqual(window.size(), 21)


class FirstTickTests(unittest.TestCase):

    def test_the_first_tick_is_recorded_once_and_never_moves(self):
        window = DedupWindow(span=20, capacity=8)
        verdicts = [window.observe(tick, "alpha") for tick in (1, 4, 7)]
        self.assertEqual([verdict.first for verdict in verdicts],
                         [True, False, False])
        self.assertEqual([verdict.duplicate for verdict in verdicts],
                         [False, True, True])
        self.assertEqual(window.first_tick("alpha"), 1)
        self.assertEqual(window.occurrences("alpha"), 3)
        self.assertEqual(window.size(), 3)


class SketchRebuildTests(unittest.TestCase):

    def test_keys_from_before_a_sketch_rebuild_are_still_found(self):
        window = DedupWindow(span=1000, capacity=64, sketch_bits=32,
                             sketch_hashes=2, fill_limit=0.4)
        for index in range(24):
            window.observe(index + 1, "member-%02d" % index)
        self.assertGreaterEqual(window.stats()["rebuilds"], 1)
        self.assertTrue(window.seen("member-00"))
        again = window.observe(25, "member-00")
        self.assertTrue(again.duplicate)
        self.assertFalse(again.first)
        self.assertFalse(window.seen("outsider-aa"))
        self.assertFalse(window.observe(26, "outsider-bb").duplicate)


class CapacityTests(unittest.TestCase):

    def test_capacity_drops_the_oldest_occurrences_only(self):
        window = DedupWindow(span=100, capacity=3, sketch_bits=64)
        verdicts = observe_all(window, (("alpha", 1), ("bravo", 2),
                                        ("charlie", 3), ("alpha", 4),
                                        ("alpha", 5)))
        self.assertEqual([verdict.evicted for verdict in verdicts],
                         [(), (), (), ("alpha",), ("bravo",)])
        self.assertEqual(window.size(), 3)
        self.assertEqual(window.key_count(), 2)
        self.assertEqual(window.occurrences("bravo"), 0)
        self.assertTrue(window.seen("alpha"))
        self.assertEqual(window.occurrences("alpha"), 2)
        self.assertEqual(window.stats()["evictions"], 2)


class OutOfOrderTests(unittest.TestCase):

    def test_late_arrivals_do_not_move_the_watermark_back(self):
        window = DedupWindow(span=10, capacity=8)
        window.observe(20, "alpha")
        late = window.observe(5, "bravo")
        self.assertFalse(late.accepted)
        self.assertEqual(late.occurrences, 0)
        self.assertEqual(late.evicted, ())
        self.assertEqual(window.watermark(), 20)
        self.assertEqual(window.occurrences("bravo"), 0)
        self.assertEqual(window.stats()["rejected"], 1)
        inside = window.observe(15, "gamma")
        self.assertTrue(inside.accepted)
        self.assertEqual(window.watermark(), 20)
        self.assertEqual(window.size(), 2)


class CounterTests(unittest.TestCase):

    def test_counters_match_the_arrival_stream(self):
        window = DedupWindow(span=1000, capacity=16)
        verdicts = observe_all(window, (("alpha", 1), ("bravo", 2),
                                        ("alpha", 3), ("charlie", 4),
                                        ("bravo", 5), ("alpha", 6)))
        self.assertEqual([verdict.first for verdict in verdicts],
                         [True, True, False, True, False, False])
        self.assertEqual(sum(1 for verdict in verdicts if verdict.duplicate), 3)
        stats = window.stats()
        self.assertEqual(stats["arrivals"], 6)
        self.assertEqual(stats["duplicates"], 3)
        self.assertEqual(stats["rejected"], 0)
        self.assertEqual(stats["arrivals"] - stats["duplicates"], 3)
        self.assertEqual(window.size(), 6)


class LeavingAndReturningTests(unittest.TestCase):

    def test_a_key_that_left_the_window_starts_over(self):
        window = DedupWindow(span=10, capacity=8)
        window.observe(1, "alpha")
        self.assertEqual(window.advance(20), 1)
        self.assertEqual(window.size(), 0)
        self.assertIsNone(window.first_tick("alpha"))
        back = window.observe(20, "alpha")
        self.assertTrue(back.first)
        self.assertFalse(back.duplicate)
        self.assertEqual(back.occurrences, 1)
        self.assertEqual(window.first_tick("alpha"), 20)


if __name__ == "__main__":
    unittest.main()
