"""In-memory cache for large tool outputs.

The module had no test file. It is what lets an agent page through a long
Odoo log instead of drowning in it, so the rules worth pinning are: ids never
collide (two outputs sharing a 1000-char prefix within one clock tick used
to), error lines are indexed so the agent can jump to them, and neither the
entry count nor a single output can grow without bound.
"""

from __future__ import annotations

from oduflow import output_cache as cache_mod
from oduflow.output_cache import OutputCache


class TestStore:
    def test_returns_an_entry_describing_the_output(self):
        entry = OutputCache().store("a\nb\nc", "run_odoo_tests", "-m sale")

        assert entry.lines == ["a", "b", "c"]
        assert entry.total_lines == 3
        assert entry.total_chars == 5
        assert entry.source_tool == "run_odoo_tests"
        assert entry.source_args == "-m sale"

    def test_the_entry_is_retrievable_by_its_id(self):
        cache = OutputCache()
        entry = cache.store("payload", "tool", "")

        assert cache.get(entry.output_id) is entry

    def test_an_unknown_id_returns_nothing(self):
        assert OutputCache().get("deadbeef") is None

    def test_ids_are_unique_for_identical_output(self):
        # Same text, same second, same 1000-char prefix: without the sequence
        # counter the second store would overwrite the first.
        cache = OutputCache()
        first = cache.store("x" * 2000, "tool", "")
        second = cache.store("x" * 2000, "tool", "")

        assert first.output_id != second.output_id
        assert cache.get(first.output_id) is first
        assert cache.get(second.output_id) is second

    def test_an_oversized_output_is_truncated(self):
        entry = OutputCache().store("x" * (cache_mod._MAX_OUTPUT_SIZE + 500), "t", "")

        assert entry.total_chars == cache_mod._MAX_OUTPUT_SIZE

    def test_an_oversized_output_keeps_its_head_and_its_end(self):
        # A failing Odoo run prints its fatal traceback last.
        output = "HEAD\n" + "x" * cache_mod._MAX_OUTPUT_SIZE + "\nFATAL TRACEBACK"
        entry = OutputCache().store(output, "t", "")

        assert entry.lines[0] == "HEAD"
        assert entry.lines[-1] == "FATAL TRACEBACK"
        assert any("omitted" in line for line in entry.lines)

    def test_an_output_at_the_limit_is_kept_whole(self):
        entry = OutputCache().store("x" * cache_mod._MAX_OUTPUT_SIZE, "t", "")

        assert entry.total_chars == cache_mod._MAX_OUTPUT_SIZE

    def test_empty_output_is_storable(self):
        entry = OutputCache().store("", "t", "")

        assert entry.lines == []
        assert entry.total_lines == 0


class TestErrorDetection:
    def _indices(self, text: str) -> list[int]:
        return OutputCache().store(text, "t", "").error_line_indices

    def test_error_and_warning_lines_are_indexed(self):
        text = "ok\n2026-01-01 ERROR something broke\nfine\n2026 WARNING careful"

        assert self._indices(text) == [1, 3]

    def test_detection_is_case_insensitive(self):
        assert self._indices("2026 error lowercase") == [0]

    def test_tracebacks_and_exceptions_are_flagged(self):
        text = "Traceback (most recent call last):\nValueError exception here"

        assert self._indices(text) == [0, 1]

    def test_a_clean_log_flags_nothing(self):
        entry = OutputCache().store("all\nfine\nhere", "t", "")

        assert entry.error_line_indices == []
        assert entry.has_errors is False

    def test_has_errors_reflects_the_indices(self):
        assert OutputCache().store("x ERROR y", "t", "").has_errors is True

    def test_a_substring_of_a_word_does_not_count(self):
        # The markers are space-delimited so "TERRORISM" is not an error.
        assert self._indices("mentions TERRORISM in passing") == []


class TestExpiry:
    def test_an_expired_entry_is_not_returned(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(cache_mod.time, "time", lambda: clock["t"])
        cache = OutputCache()
        entry = cache.store("payload", "t", "")

        clock["t"] += cache_mod._CACHE_TTL + 1

        assert cache.get(entry.output_id) is None

    def test_an_entry_at_exactly_the_ttl_is_still_returned(self, monkeypatch):
        # The check is `age > TTL`, so the entry survives its own deadline.
        clock = {"t": 1000.0}
        monkeypatch.setattr(cache_mod.time, "time", lambda: clock["t"])
        cache = OutputCache()
        entry = cache.store("payload", "t", "")

        clock["t"] += cache_mod._CACHE_TTL

        assert cache.get(entry.output_id) is entry

    def test_an_expired_entry_is_dropped_from_the_store(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(cache_mod.time, "time", lambda: clock["t"])
        cache = OutputCache()
        entry = cache.store("payload", "t", "")

        clock["t"] += cache_mod._CACHE_TTL + 1
        cache.get(entry.output_id)

        assert entry.output_id not in cache._store


class TestEviction:
    def test_the_entry_count_stays_bounded(self):
        cache = OutputCache()

        for i in range(cache_mod._MAX_ENTRIES + 10):
            cache.store(f"output {i}", "t", "")

        assert len(cache._store) <= cache_mod._MAX_ENTRIES

    def test_the_newest_entry_survives_eviction(self):
        cache = OutputCache()
        for i in range(cache_mod._MAX_ENTRIES + 5):
            newest = cache.store(f"output {i}", "t", "")

        assert cache.get(newest.output_id) is newest

    def test_the_oldest_entry_is_evicted_first(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(cache_mod.time, "time", lambda: clock["t"])
        cache = OutputCache()

        oldest = cache.store("first", "t", "")
        for i in range(cache_mod._MAX_ENTRIES):
            clock["t"] += 1
            cache.store(f"output {i}", "t", "")

        assert cache.get(oldest.output_id) is None

    def test_expired_entries_are_swept_on_store(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(cache_mod.time, "time", lambda: clock["t"])
        cache = OutputCache()
        stale = cache.store("stale", "t", "")

        clock["t"] += cache_mod._CACHE_TTL + 1
        cache.store("fresh", "t", "")

        assert stale.output_id not in cache._store
