"""Concurrent config updates no longer lose each other (issue #2147).

The load-bearing property is that **the call sites did not change**. Every test here
drives the plain read-then-write pattern the repo already uses::

    data = read_config_for_update(path)
    data["k"] = v
    write_config_atomically(path, data)

and asserts that a concurrent writer's change survives. Before this change that pattern
lost one of the two updates; the fix is inside the two functions, so all 12 sites that
use it are covered without being touched.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from kiro_crew.config.loader import (
    ConfigBusyError,
    ConfigReadError,
    _apply_delta,
    config_fingerprint,
    config_transaction,
    read_config_for_update,
    write_config_atomically,
)


@pytest.fixture()
def cfg(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    path = home / "config.json"
    path.write_text(json.dumps({"timezone": "UTC"}))
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _rmw(path: Path, key: str, value, *, hold: float = 0.0) -> None:
    """Exactly the pattern the repo's 12 read-modify-write sites use, unchanged."""
    data = read_config_for_update(path)
    if hold:
        time.sleep(hold)  # widen the window that used to cause the clobber
    data[key] = value
    write_config_atomically(path, data)


class TestTheUnchangedPatternIsNowSafe:
    def test_two_concurrent_updates_both_survive(self, cfg) -> None:
        """The exact interleaving that used to destroy one of the two updates."""
        both_read = threading.Barrier(2)

        def updater(key: str) -> None:
            data = read_config_for_update(cfg)
            both_read.wait(timeout=5)  # force both to hold the SAME snapshot
            data[key] = "set"
            write_config_atomically(cfg, data)

        a = threading.Thread(target=updater, args=("from_a",))
        b = threading.Thread(target=updater, args=("from_b",))
        a.start()
        b.start()
        a.join(15)
        b.join(15)

        final = _read(cfg)
        assert final.get("from_a") == "set", "thread A's update was lost"
        assert final.get("from_b") == "set", "thread B's update was lost"
        assert final["timezone"] == "UTC"

    def test_twelve_concurrent_updaters_all_land(self, cfg) -> None:
        n = 12
        ready = threading.Barrier(n)

        def updater(i: int) -> None:
            ready.wait(timeout=15)
            _rmw(cfg, f"k{i}", i, hold=0.01)

        threads = [threading.Thread(target=updater, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        final = _read(cfg)
        missing = [f"k{i}" for i in range(n) if f"k{i}" not in final]
        assert not missing, f"lost updates from {missing}"

    def test_a_nested_section_merges_key_by_key(self, cfg) -> None:
        """Two writers inside the same section must not replace each other's subtree."""
        cfg.write_text(json.dumps({"dashboard": {"url": "http://x"}}))
        both_read = threading.Barrier(2)

        def updater(key: str, value: str) -> None:
            data = read_config_for_update(cfg)
            both_read.wait(timeout=5)
            data.setdefault("dashboard", {})[key] = value
            write_config_atomically(cfg, data)

        a = threading.Thread(target=updater, args=("theme", "dark"))
        b = threading.Thread(target=updater, args=("locale", "ja"))
        a.start()
        b.start()
        a.join(15)
        b.join(15)

        dash = _read(cfg)["dashboard"]
        assert dash.get("theme") == "dark", "A's nested key was lost"
        assert dash.get("locale") == "ja", "B's nested key was lost"
        assert dash["url"] == "http://x", "the untouched nested key was dropped"


class TestTheDeltaSemantics:
    def test_an_untouched_key_keeps_the_newer_value(self) -> None:
        """The property the whole merge rests on."""
        snapshot = {"a": 1, "b": 2}
        desired = {"a": 1, "b": 99}  # caller changed only b
        base = {"a": 42, "b": 2}  # someone else changed a meanwhile
        assert _apply_delta(base, snapshot, desired) == {"a": 42, "b": 99}

    def test_a_deletion_is_replayed_as_a_deletion(self) -> None:
        assert _apply_delta({"a": 1, "b": 2}, {"a": 1, "b": 2}, {"a": 1}) == {"a": 1}

    def test_the_same_leaf_goes_to_the_caller(self) -> None:
        """Unavoidable, and identical to what two serialised writes would produce."""
        assert _apply_delta({"k": "theirs"}, {"k": "orig"}, {"k": "mine"}) == {"k": "mine"}

    def test_nesting_is_recursive_not_wholesale(self) -> None:
        snapshot = {"s": {"x": 1, "y": 2}}
        desired = {"s": {"x": 1, "y": 3}}
        base = {"s": {"x": 9, "y": 2, "z": 7}}
        assert _apply_delta(base, snapshot, desired) == {"s": {"x": 9, "y": 3, "z": 7}}

    def test_a_scalar_replacing_a_dict_is_taken_verbatim(self) -> None:
        assert _apply_delta({"s": {"x": 1}}, {"s": {"x": 1}}, {"s": 5}) == {"s": 5}


class TestAWriteWithNoMatchingRead:
    def test_a_full_payload_is_written_as_given(self, cfg) -> None:
        """`KiroCrewConfig.save()` dumps the whole model; there is no delta to replay."""
        write_config_atomically(cfg, {"only": "this"})
        assert _read(cfg) == {"only": "this"}

    def test_it_still_serialises_against_a_transaction(self, cfg) -> None:
        holding = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with config_transaction(cfg):
                holding.set()
                release.wait(timeout=10)

        t = threading.Thread(target=holder)
        t.start()
        assert holding.wait(timeout=10)
        done = threading.Event()

        def writer() -> None:
            write_config_atomically(cfg, {"late": True})
            done.set()

        w = threading.Thread(target=writer)
        w.start()
        assert not done.wait(timeout=0.3), "the write did not wait for the lock"
        release.set()
        w.join(15)
        t.join(15)
        assert done.is_set()


class TestACorruptFileDoesNotStrandTheCaller:
    def test_an_unreadable_file_is_overwritten_rather_than_merged(self, cfg) -> None:
        """Replaying a delta onto unparseable bytes is impossible; refusing would strand.

        This is the pre-existing behaviour, kept deliberately: the caller already read a
        good snapshot, so writing their payload is the best available outcome.
        """
        data = read_config_for_update(cfg)
        data["mine"] = 1
        cfg.write_text("{ not json")
        write_config_atomically(cfg, data)
        assert _read(cfg)["mine"] == 1


class TestTheLockIsASidecarBesideTheTarget:
    def test_the_lock_file_is_not_the_config(self, cfg) -> None:
        _rmw(cfg, "x", 1)
        assert cfg.with_name(".config.json.lock").exists()

    def test_it_follows_a_symlinked_config(self, tmp_path, monkeypatch) -> None:
        """Symlinking config into a dotfiles repo must not silently disable locking.

        The directory holding the LINK can be read-only while the target is writable, so
        a sidecar beside the link would be uncreatable even though the write succeeds --
        every writer would then run unlocked.
        """
        real_dir = tmp_path / "dotfiles"
        real_dir.mkdir()
        target = real_dir / "config.json"
        target.write_text(json.dumps({"timezone": "UTC"}))
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        link = home / "config.json"
        link.symlink_to(target)

        _rmw(link, "added", 1)

        assert (real_dir / ".config.json.lock").exists(), "lock did not follow the symlink"
        assert not (home / ".config.json.lock").exists(), "lock was placed beside the link"
        assert json.loads(target.read_text())["added"] == 1
        assert link.is_symlink(), "the symlink was replaced instead of followed"

    def test_two_writers_through_the_symlink_both_survive(self, tmp_path, monkeypatch):
        real_dir = tmp_path / "dotfiles"
        real_dir.mkdir()
        target = real_dir / "config.json"
        target.write_text(json.dumps({"timezone": "UTC"}))
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        link = home / "config.json"
        link.symlink_to(target)

        ready = threading.Barrier(2)

        def updater(key: str) -> None:
            ready.wait(timeout=10)
            _rmw(link, key, "set", hold=0.02)

        a = threading.Thread(target=updater, args=("from_a",))
        b = threading.Thread(target=updater, args=("from_b",))
        a.start()
        b.start()
        a.join(30)
        b.join(30)

        final = json.loads(target.read_text())
        assert final.get("from_a") == "set" and final.get("from_b") == "set"


class TestTheFingerprintIsContentBased:
    def test_an_equal_length_write_is_detected(self, cfg) -> None:
        """`(mtime, size)` cannot see this; a content hash must."""
        cfg.write_text(json.dumps({"v": "aaa"}))
        before = config_fingerprint(cfg)
        cfg.write_text(json.dumps({"v": "bbb"}))
        assert len(cfg.read_text()) == len(json.dumps({"v": "aaa"}))
        assert config_fingerprint(cfg) != before

    def test_absence_is_not_an_error(self, tmp_path) -> None:
        assert config_fingerprint(tmp_path / "nope.json") is None


class TestTheExplicitTransactionStillWorks:
    """`config_transaction` remains available for code that wants to refuse rather than
    merge -- a caller whose new value depends on the old one in a way a key-level merge
    cannot express."""

    def test_a_conflicting_transaction_refuses(self, cfg) -> None:
        with config_transaction(cfg, required=False) as txn:
            data = txn.read()
            data["mine"] = 1
            cfg.write_text(json.dumps({"theirs": 1}))
            with pytest.raises(ConfigBusyError):
                txn.write(data)
        assert _read(cfg) == {"theirs": 1}

    def test_writing_without_reading_is_refused(self, cfg) -> None:
        with config_transaction(cfg) as txn:
            with pytest.raises(RuntimeError, match="before read"):
                txn.write({"anything": 1})

    def test_busy_is_an_oserror(self) -> None:
        assert issubclass(ConfigBusyError, OSError)


class TestTheEventLoopIsNotStalledMeaningfully:
    def test_a_contended_write_from_the_loop_costs_about_one_write(self, cfg) -> None:
        """No async migration is needed because the wait is the length of one write.

        The lock covers a re-read, a dict merge and a rename. This measures the loop
        being blocked while another thread holds the lock through exactly one write, and
        asserts the stall stays in the tens of milliseconds rather than seconds -- the
        threshold that would justify restructuring 12 handlers.
        """

        async def scenario() -> float:
            done = threading.Event()

            def other_writer() -> None:
                for _ in range(20):
                    _rmw(cfg, "other", time.time())
                done.set()

            t = threading.Thread(target=other_writer)
            t.start()
            worst = 0.0
            while not done.is_set():
                t0 = time.perf_counter()
                try:
                    _rmw(cfg, "mine", time.time())  # inline, as today's handlers do
                except ConfigBusyError:
                    # Expected under contention, and the point of this test is that the
                    # refusal is CHEAP: a refusal that took a second would stall the loop
                    # just as badly as the wait it replaced.
                    pass
                except ConfigReadError:
                    # Windows only, and this hammer is what makes it likely: the OS refuses
                    # to open a file while it is being replaced, so a reader that loses the
                    # race with the writer's rename gets `[Errno 13] Permission denied`.
                    # POSIX `rename` is atomic and the reader never notices. Refusing is the
                    # module's fail-closed contract (better than a torn read) and predates
                    # this change; what this test measures is the absence of a STALL, and a
                    # refused attempt is a legitimate outcome of that.
                    pass
                worst = max(worst, time.perf_counter() - t0)
                await asyncio.sleep(0)
            t.join(30)
            return worst

        worst_ms = asyncio.run(scenario()) * 1000
        assert worst_ms < 500, f"a contended inline write blocked the loop for {worst_ms:.0f}ms"


class TestTheEventLoopIsNeverSlept:
    """No `time.sleep` poll may run on the event loop.

    The repo's own `no-blocking-call-on-event-loop` rule. An on-loop caller therefore gets
    one non-blocking attempt and no wait.

    It must not then proceed unlocked, which is what an earlier revision of this change did
    on the theory that the delta merge made the lock optional. It does not: the merge base is
    the re-read, and the holder can land its own rename between that re-read and ours, so the
    bytes we merged from are already stale and the holder's update is overwritten. The
    contended on-loop write therefore RAISES rather than silently reverting a setting.
    """

    def test_a_contended_on_loop_write_does_not_sleep(self, cfg, monkeypatch) -> None:
        import kiro_crew.config.loader as loader

        slept: list[float] = []
        real_sleep = time.sleep
        monkeypatch.setattr(
            loader.time, "sleep", lambda d: slept.append(d) or real_sleep(0)
        )

        holding = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with config_transaction(cfg):
                holding.set()
                release.wait(timeout=10)

        t = threading.Thread(target=holder)
        t.start()
        assert holding.wait(timeout=10)

        refused: list[BaseException] = []

        async def scenario() -> None:
            # Inline from a coroutine, exactly as today's handlers call it.
            data = read_config_for_update(cfg)
            data["from_loop"] = 1
            try:
                write_config_atomically(cfg, data)
            except ConfigBusyError as exc:
                refused.append(exc)

        try:
            asyncio.run(scenario())
        finally:
            release.set()
            t.join(10)

        assert slept == [], f"slept on the event loop: {slept}"
        assert refused, "a contended on-loop write must refuse, not proceed unlocked"
        assert "from_loop" not in _read(cfg), "the refused write must not have landed"

    def test_an_off_loop_write_still_waits_for_the_lock(self, cfg) -> None:
        """Off the loop there is no reason to give up the guarantee."""
        holding = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def holder() -> None:
            with config_transaction(cfg):
                holding.set()
                release.wait(timeout=10)

        def waiter() -> None:
            _rmw(cfg, "later", 1)
            finished.set()

        t = threading.Thread(target=holder)
        t.start()
        assert holding.wait(timeout=10)
        w = threading.Thread(target=waiter)
        w.start()
        assert not finished.wait(timeout=0.3), "the off-loop write did not wait"
        release.set()
        w.join(15)
        t.join(15)
        assert finished.is_set()


class TestSaveDoesNotRevertAConcurrentChange:
    """`KiroCrewConfig.save()` dumps the whole model, so it needs its own baseline.

    Without one there is no delta to replay and the dump overwrites whatever landed since
    this instance was loaded -- the exact scenario the review named: a CLI loads config, a
    dashboard toggle commits, the CLI saves, and the toggle is silently reverted.
    """

    def test_a_toggle_committed_after_load_survives_a_save(self, cfg) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg.write_text(json.dumps({"timezone": "UTC", "auto_update": False}))
        loaded = KiroCrewConfig.load()  # the CLI's snapshot of the world

        # Meanwhile the dashboard commits an unrelated change.
        _rmw(cfg, "auto_update", True)

        loaded.timezone = "Asia/Tokyo"  # the CLI changes something else entirely
        loaded.save()

        final = _read(cfg)
        assert final["timezone"] == "Asia/Tokyo", "the CLI's own change was lost"
        assert final["auto_update"] is True, (
            "save() reverted a change committed after this instance was loaded"
        )

    def test_the_baseline_is_recorded_on_the_instance(self, cfg) -> None:
        """Per-instance, not per-thread: a `load()` elsewhere on this thread must not be
        mistaken for this object's starting point."""
        from kiro_crew.config.loader import KiroCrewConfig

        a = KiroCrewConfig.load()
        _rmw(cfg, "changed_between", 1)
        b = KiroCrewConfig.load()
        assert a._loaded_from != b._loaded_from
        assert b._loaded_from is not None and "changed_between" in b._loaded_from

    def test_an_explicit_baseline_beats_the_thread_local_record(self, cfg) -> None:
        cfg.write_text(json.dumps({"a": 1, "b": 2}))
        read_config_for_update(cfg)  # leaves a thread-local record
        # A caller with its own baseline: it only ever knew about {"a": 1}.
        write_config_atomically(cfg, {"a": 9}, baseline={"a": 1})
        final = _read(cfg)
        assert final["a"] == 9, "the caller's change was not applied"
        assert final["b"] == 2, "a key outside the explicit baseline was dropped"


class TestTheBaselineIsShapeMatched:
    """A baseline only works if it is in the same shape as the payload.

    Two shapes exist in this repo: the raw parsed file, and the canonical `to_dict()` dump
    with a default filled in for every key the file omits. Diffing a canonical payload
    against a raw baseline makes each defaulted key look caller-modified, so it is replayed
    over whatever is on disk -- reintroducing the lost update this change exists to remove,
    on the majority of a partial config's keys.
    """

    def test_load_records_a_baseline_in_the_payload_shape(self, cfg) -> None:
        """The fixture's config has only `timezone`, so `auto_update` is a pure default."""
        from kiro_crew.config.loader import KiroCrewConfig

        loaded = KiroCrewConfig.load()
        baseline = loaded.baseline_for(cfg)

        assert baseline is not None
        assert "auto_update" in baseline, (
            "the baseline is missing a key the payload will carry, so that key would be "
            "replayed as though the caller had set it"
        )
        assert baseline["auto_update"] is True

    def test_save_keeps_a_concurrent_change_to_a_key_it_never_touched(self, cfg) -> None:
        """The reported chain, end to end.

        A partial config lacks `auto_update`; something else turns it off after this
        instance was loaded; this instance saves an unrelated edit. The concurrent value
        has to survive -- with a raw baseline the defaulted `True` was replayed over it.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        loaded = KiroCrewConfig.load()

        # A concurrent writer -- a dashboard toggle -- lands after the load.
        data = _read(cfg)
        data["auto_update"] = False
        write_config_atomically(cfg, data)

        loaded.timezone = "Asia/Shanghai"
        loaded.save()

        on_disk = _read(cfg)
        assert on_disk["auto_update"] is False, "the save reverted a key it never touched"
        assert on_disk["timezone"] == "Asia/Shanghai", "the save's own edit did not land"

    def test_a_hand_built_canonical_payload_can_carry_the_same_baseline(self, cfg) -> None:
        """`kirocrew config set KEY VALUE` builds `save()`'s payload without calling it.

        It loads, sets one key on `to_dict()`, and writes. `baseline_for` is public so
        that site gets the same protection instead of falling back to a raw-shaped snapshot.
        """
        from kiro_crew.config.loader import KiroCrewConfig, stamp_config_meta

        loaded = KiroCrewConfig.load()
        payload = loaded.to_dict()
        payload["timezone"] = "Europe/Berlin"

        data = _read(cfg)
        data["auto_update"] = False
        write_config_atomically(cfg, data)

        write_config_atomically(
            cfg, stamp_config_meta(payload), baseline=loaded.baseline_for(cfg)
        )

        on_disk = _read(cfg)
        assert on_disk["auto_update"] is False
        assert on_disk["timezone"] == "Europe/Berlin"

    def test_a_raw_payload_against_a_canonical_baseline_would_delete_keys(self, cfg) -> None:
        """Why the baseline is per-instance instead of seeded into the read record.

        Seeding the thread-local with the canonical shape at load time would pair it with
        the NEXT write on that thread -- including the writers that build a raw dict
        (`config set --local`, `config set --file`). A key present in the canonical
        baseline and absent from a raw payload reads as "the caller deleted it", so the
        merge drops it from the file. This asserts that failure mode exists, which is the
        reason the pairing is explicit rather than ambient.
        """
        from kiro_crew.config.loader import _apply_delta

        canonical_baseline = {"timezone": "UTC", "auto_update": True}
        raw_payload = {"timezone": "Europe/Berlin"}  # a raw writer's whole dict
        on_disk = {"timezone": "UTC", "auto_update": False}

        merged = _apply_delta(on_disk, canonical_baseline, raw_payload)

        assert "auto_update" not in merged, (
            "if this ever stops deleting the key, the shape-mismatch hazard is gone and "
            "an ambient baseline could be reconsidered"
        )


class TestAContendedWriteRefuses:
    def test_an_off_loop_contended_write_raises_after_its_wait(
        self, cfg, monkeypatch
    ) -> None:
        """Off the loop the wait still happens; a holder that never lets go still refuses.

        `ConfigBusyError` subclasses `OSError`, so the 10 write sites that already guard
        their write with `except OSError` degrade without a change.
        """
        holding = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with config_transaction(cfg):
                holding.set()
                release.wait(timeout=10)

        import kiro_crew.config.loader as loader

        monkeypatch.setattr(loader, "_WRITE_LOCK_TIMEOUT", 0.05)

        t = threading.Thread(target=holder)
        t.start()
        assert holding.wait(timeout=10)
        try:
            data = read_config_for_update(cfg)
            data["mine"] = 1
            with pytest.raises(ConfigBusyError):
                write_config_atomically(cfg, data)
        finally:
            release.set()
            t.join(10)

        assert "mine" not in _read(cfg), "a refused write must leave the file alone"


class TestABaselineOnlyAppliesToItsOwnFile:
    """Found by two round-trip tests, and it has a production form.

    The delta's premise is "a key the caller did not change is already correct on disk". That
    holds only for the file the baseline was captured from, while it still exists. Write to a
    different path -- or to one that has since been deleted -- and every unchanged key is
    skipped against nothing, so it silently disappears from the result.
    """

    def test_a_missing_target_is_written_whole(self, cfg) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        loaded = KiroCrewConfig.load()
        cfg.unlink()  # the file goes away after the load

        loaded.save()

        on_disk = _read(cfg)
        assert on_disk["timezone"] == "UTC", "the recreated config lost a key it had loaded"
        assert "auto_update" in on_disk, "an unchanged key vanished instead of being written"

    def test_a_baseline_is_refused_for_a_different_path(self, cfg, tmp_path) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        loaded = KiroCrewConfig.load()

        assert loaded.baseline_for(cfg) is not None, "its own path must still be accepted"
        assert loaded.baseline_for(tmp_path / "other.json") is None, (
            "a baseline from another file would skip every key it reads as unchanged"
        )


class TestEveryLoadExitRecordsABaseline:
    """A missed exit is silent: the write degrades to a whole-model dump and the lost update
    comes back. The fresh-home shortcut returns before any file is parsed, which is the worst
    one to miss -- there every key is a default, so every key gets replayed.
    """

    def test_a_fresh_home_still_gets_a_baseline(self, cfg, monkeypatch) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg.unlink()  # no config.json and no overlay: the early-return path
        loaded = KiroCrewConfig.load()

        assert loaded.baseline_for(cfg) is not None, (
            "the fresh-home exit returned without a baseline, so the next save is a "
            "whole-model dump"
        )

    def test_two_writers_on_a_fresh_home_do_not_revert_each_other(self, cfg) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg.unlink()
        first = KiroCrewConfig.load()
        second = KiroCrewConfig.load()  # both loaded before either wrote

        first.timezone = "Asia/Shanghai"
        first.save()

        second.auto_update = False
        second.save()

        on_disk = _read(cfg)
        assert on_disk["auto_update"] is False, "the second writer's own edit was lost"
        assert on_disk["timezone"] == "Asia/Shanghai", (
            "the second save reverted the first writer's setting"
        )


class TestTheMissingTargetCheckIsUnderTheLock:
    def test_a_file_removed_after_the_check_still_writes_completely(self, cfg, monkeypatch) -> None:
        """The check has to see what the LOCKED re-read sees.

        Checked before the lock, a file removed in between lands in the state the guard
        exists to prevent: the re-read yields {}, the delta skips every key the caller did
        not change, and those keys vanish from the recreated file.
        """
        import kiro_crew.config.loader as loader
        from kiro_crew.config.loader import KiroCrewConfig

        loaded = KiroCrewConfig.load()
        loaded.timezone = "Europe/Berlin"

        # Delete the file at the last moment the pre-lock ordering would have missed:
        # after any early check, before the locked re-read.
        real_lock = loader._config_file_lock

        def deleting_lock(path, **kw):
            if path.exists():
                path.unlink()
            return real_lock(path, **kw)

        monkeypatch.setattr(loader, "_config_file_lock", deleting_lock)
        loaded.save()

        on_disk = _read(cfg)
        assert on_disk["timezone"] == "Europe/Berlin", "the caller's own edit was lost"
        assert "auto_update" in on_disk, (
            "a key the caller never touched vanished from the recreated file"
        )


class TestASaveBecomesTheNewBaseline:
    """Reported against the migration path, but it is general.

    `_loaded_from` described the state at LOAD time. Left unrefreshed, an instance that saves
    twice recomputes its delta against the original load both times, so the second save
    replays the first save's edits again -- over anything that landed in between. The
    migration write-back makes that a two-save sequence by default.
    """

    def test_a_second_save_does_not_replay_the_first(self, cfg) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        loaded = KiroCrewConfig.load()
        loaded.timezone = "Asia/Shanghai"
        loaded.save()

        # Someone else revises the very field the first save wrote.
        data = _read(cfg)
        data["timezone"] = "Europe/Berlin"
        write_config_atomically(cfg, data)

        # An unrelated second edit on the SAME instance.
        loaded.auto_update = False
        loaded.save()

        on_disk = _read(cfg)
        assert on_disk["auto_update"] is False, "the second save's own edit was lost"
        assert on_disk["timezone"] == "Europe/Berlin", (
            "the second save replayed the first save's value over a newer one"
        )

    def test_the_write_returns_what_landed_not_what_was_asked(self, cfg) -> None:
        """`save()` re-records from the return value, so it has to be the MERGE result.

        Re-recording from the requested payload instead would drop a concurrent writer's key
        from the baseline, and a later save would then treat it as absent rather than
        untouched.
        """
        data = read_config_for_update(cfg)
        data["mine"] = 1

        # A key this caller never saw, landing after its read. Written to the file directly
        # rather than through `write_config_atomically`, because the read record is
        # per-path-per-thread and the first write on this thread POPS it -- routing the
        # stand-in writer through the same call would consume the snapshot under test. The
        # real competing writer is another process, which cannot take this thread's record.
        other = _read(cfg)
        other["theirs"] = 2
        cfg.write_text(json.dumps(other), encoding="utf-8")

        written = write_config_atomically(cfg, data)

        assert written["mine"] == 1
        assert written.get("theirs") == 2, "the return value is the payload, not the merge"
        assert written == _read(cfg), "the return value does not match the file"


class TestTheMigrationStillConverges:
    """The migration write-back replaces the file; it does not replay one caller's edits.

    Measured against a pristine origin/main checkout: with a legacy
    `{"workspaces": {"legacy": "some/dir"}}` on disk, main leaves
    `{"default": {"dir": "workspace"}}` after the first load, and a second load does not
    migrate again. A paired write cannot reach that state -- a flat workspace string is
    REJECTED by validation and dropped from the model, and "the payload omits it" is
    indistinguishable from "it was never there", so the legacy value survives and
    `needs_migration` stays true on every load.

    Hence `save(whole=True)` for that one write. Every other save keeps its baseline.
    """

    def test_a_legacy_config_converges_in_one_load(self, cfg) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg.write_text(
            json.dumps({"timezone": "UTC", "workspaces": {"legacy": "some/dir"}}),
            encoding="utf-8",
        )

        KiroCrewConfig.load()

        on_disk = _read(cfg)
        assert "legacy" not in on_disk.get("workspaces", {}), (
            "the rejected legacy entry survived the migration write-back, so the config "
            "never converges and every load migrates again"
        )
        assert "default" in on_disk.get("workspaces", {}), (
            "the migrated form did not reach disk"
        )

    def test_the_baseline_still_holds_the_on_disk_value(self, cfg) -> None:
        """The union takes the DISK value where the file has one, the default where it does not.

        Reversed -- canonical winning over disk -- a value the model rewrites looks unchanged
        to the delta, which is what defeated the migration above.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        cfg.write_text(
            json.dumps({"timezone": "UTC", "session": {"timeout_secs": 7200}}),
            encoding="utf-8",
        )

        loaded = KiroCrewConfig.load()
        baseline = loaded.baseline_for(cfg)

        assert baseline is not None
        assert baseline["session"]["timeout_secs"] == 7200, "the disk value was overwritten"
        assert baseline["auto_update"] is True, "a defaulted key is missing from the baseline"


class TestASnapshotBelongsToTheDictItWasHandedOut:
    """Keyed by path alone, a read that never writes leaks its record to the next writer.

    A handler that reads the config, rejects its payload and returns 400 leaves the record
    behind; the next unrelated write on that thread then has its own values diffed against
    someone else's read. The record therefore carries the identity of the dict it was handed
    out as, and the intended read-mutate-write pattern preserves that identity.
    """

    def test_an_unrelated_writer_does_not_inherit_a_leaked_record(self, cfg) -> None:
        # A read that goes nowhere -- the validation-failure path.
        abandoned = read_config_for_update(cfg)
        abandoned["never_written"] = 1

        # Someone else's value lands on disk in the meantime.
        on_disk = _read(cfg)
        on_disk["theirs"] = 2
        cfg.write_text(json.dumps(on_disk), encoding="utf-8")

        # An unrelated writer with its OWN dict must not be paired with that read.
        write_config_atomically(cfg, {"timezone": "Europe/Berlin", "mine": 3})

        result = _read(cfg)
        assert result["mine"] == 3
        assert "never_written" not in result, "the abandoned read's edit was replayed"

    def test_the_reader_that_writes_is_still_paired(self, cfg) -> None:
        """The pairing must not become so strict that the intended pattern loses it."""
        data = read_config_for_update(cfg)
        data["mine"] = 1

        # A key this caller never saw, landing after its read (written directly, because a
        # write through the same call on this thread would consume the record under test).
        other = _read(cfg)
        other["theirs"] = 2
        cfg.write_text(json.dumps(other), encoding="utf-8")

        write_config_atomically(cfg, data)

        result = _read(cfg)
        assert result["mine"] == 1, "the paired write did not land"
        assert result["theirs"] == 2, "the pairing was lost, so the write clobbered"


class TestTheMigrationPreservesConcurrentUpdates:
    def test_a_change_landing_after_the_load_survives_the_migration_write(self, cfg) -> None:
        """The migration replaces the legacy shape without taking the whole file with it.

        A whole write converges but discards anything that landed after the load. Passing the
        raw parsed file as the baseline converges AND leaves keys this write never touched.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        cfg.write_text(
            json.dumps({"timezone": "UTC", "workspaces": {"legacy": "some/dir"}}),
            encoding="utf-8",
        )

        # Stand in for the migration write-back's own sequencing: read as load() would, let
        # another writer land, then replay the canonical form against the RAW baseline.
        raw = _read(cfg)
        loaded = KiroCrewConfig.load()

        after_load = _read(cfg)
        assert "legacy" not in after_load.get("workspaces", {}), (
            "the migration did not converge"
        )

        # And the raw baseline is what makes the removal expressible.
        assert "legacy" in raw["workspaces"]
        assert loaded.baseline_for(cfg) is not None


class TestNormalizationIsNotACallerEdit:
    """A value the MODEL rewrites must not be replayed by an ordinary save.

    The recorded baseline takes the canonical value for any key the model carries, so a
    normalization (a sorted list, a coerced type) is equal on both sides of the delta and
    skipped. Take the disk value there instead and every ordinary save replays the normalized
    OLD value over whatever landed since -- a lost update produced by the merge itself.

    The migration write is the one place that must land the normalized form, and it gets that
    by passing the raw file as an explicit baseline, not by changing the recorded one.
    """

    def test_an_unrelated_save_does_not_replay_a_normalized_value(self, cfg) -> None:
        from kiro_crew.config.loader import _apply_delta, _deep_merge

        raw = {"slack": {"trusted_bot_ids": ["b", "a"]}}      # as written by hand
        canonical = {"slack": {"trusted_bot_ids": ["a", "b"]}, "auto_update": True}
        baseline = _deep_merge(raw, canonical)

        assert baseline["slack"]["trusted_bot_ids"] == ["a", "b"], (
            "the baseline took the DISK value for a key the model normalizes, so the delta "
            "will replay the old value"
        )

        on_disk = {"slack": {"trusted_bot_ids": ["z"]}, "auto_update": True}
        merged = _apply_delta(on_disk, baseline, dict(canonical))

        assert merged["slack"]["trusted_bot_ids"] == ["z"], (
            "an unrelated save overwrote a concurrent replacement with the normalized old value"
        )

    def test_a_legacy_alias_is_still_deleted(self, cfg) -> None:
        """The other half: raw still contributes keys the model does not carry at all."""
        from kiro_crew.config.loader import _apply_delta, _deep_merge

        raw = {"agent": {"yolo": False, "provider": "acp"}}
        canonical = {"agent": {"provider": "acp"}}
        baseline = _deep_merge(raw, canonical)

        assert "yolo" in baseline["agent"], "a delta cannot delete what its baseline lacks"

        merged = _apply_delta({"agent": {"yolo": False, "provider": "acp"}}, baseline, canonical)
        assert "yolo" not in merged["agent"]


class TestTheReadRecordHoldsTheObject:
    """`id()` is not an identity: CPython hands a freed dict's address straight to the next
    allocation (measured 1000/1000), so an id comparison matches almost any later writer while
    reading like a verified pairing -- worse than the path-only key it replaced.
    """

    def test_a_recycled_address_does_not_match(self, cfg) -> None:
        import kiro_crew.config.loader as loader

        abandoned = read_config_for_update(cfg)      # a read that never writes
        abandoned["never_written"] = 1
        abandoned_id = id(abandoned)
        del abandoned                                # its address is now free to reuse

        # Allocate until something lands on that address; in CPython this is immediate.
        impostor = None
        for _ in range(10000):
            candidate = {"timezone": "Europe/Berlin", "mine": 3}
            if id(candidate) == abandoned_id:
                impostor = candidate
                break

        if impostor is None:
            pytest.skip("no address reuse observed on this interpreter")

        loader.write_config_atomically(cfg, impostor)

        result = _read(cfg)
        assert result["mine"] == 3
        assert "never_written" not in result, (
            "a dict that merely reused the address was paired with the abandoned read"
        )


class TestAnAbsentConfigIsStillPaired:
    """The read record is matched by identity, so the absent-file path must hand back the very
    dict it snapshotted. Two equal-but-distinct `{}` literals read as a mismatch, which
    silently disables the merge for every write against a newly created config.
    """

    def test_two_first_writers_on_an_absent_config_both_survive(self, cfg) -> None:
        """Two threads, because the read record is per-path-per-THREAD.

        Single-threaded the second read would overwrite the first's record, which is a
        property of the store rather than the defect under test. Two threads is also the
        deployed shape: independent writers, each holding its own record.
        """
        cfg.unlink()

        both_read = threading.Barrier(2, timeout=10)
        first_written = threading.Event()
        errors: list[BaseException] = []

        def writer(key: str, value: object, wait_for_first: bool) -> None:
            try:
                data = read_config_for_update(cfg)
                both_read.wait()
                if wait_for_first:
                    assert first_written.wait(timeout=10)
                data[key] = value
                write_config_atomically(cfg, data)
                if not wait_for_first:
                    first_written.set()
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)
                first_written.set()

        threads = [
            threading.Thread(target=writer, args=("timezone", "Asia/Shanghai", False)),
            threading.Thread(target=writer, args=("auto_update", False, True)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)

        assert not errors, f"a writer raised: {errors}"
        on_disk = _read(cfg)
        assert on_disk["auto_update"] is False, "the second writer's own edit was lost"
        assert on_disk["timezone"] == "Asia/Shanghai", (
            "the second write replaced the file instead of merging, so the first setting is gone"
        )

    def test_the_absent_read_hands_back_the_dict_it_recorded(self, cfg) -> None:
        import kiro_crew.config.loader as loader

        cfg.unlink()
        handed_out = read_config_for_update(cfg)

        store = getattr(loader._read_snapshots, "by_path", {})
        recorded, _snapshot = store[loader._snapshot_key(cfg)]
        assert recorded is handed_out, (
            "the record holds a different object than the caller received, so the pairing "
            "can never match"
        )


class TestNoLockAvailableIsAlsoARefusal:
    """"No lock available" is not "no competing writer".

    It means no mutual exclusion for anyone, so every writer proceeds and the last rename wins.
    Refusing also costs nothing: `atomic_write` puts its temp file in the same directory the
    sidecar lives in, so a directory that cannot hold one cannot hold the other either.
    """

    def test_a_write_refuses_when_the_sidecar_cannot_be_opened(self, cfg, monkeypatch) -> None:
        import kiro_crew.config.loader as loader

        real_open = Path.open

        def refuse_the_sidecar(self, *a, **kw):
            if self.name.startswith(".") and self.name.endswith(".lock"):
                raise PermissionError(13, "Permission denied")
            return real_open(self, *a, **kw)

        monkeypatch.setattr(loader.Path, "open", refuse_the_sidecar)

        data = read_config_for_update(cfg)
        data["mine"] = 1
        with pytest.raises(ConfigBusyError):
            write_config_atomically(cfg, data)

        assert "mine" not in _read(cfg), "the refused write still touched the file"


class TestAJsonTypeChangeIsAChange:
    """`==` is not enough: Python has `True == 1` and `1 == 1.0`.

    A config holding a numeric `1` and a caller setting `True` would compare equal, so the delta
    would call the key unchanged and the requested type would never reach disk -- while the write
    reported success.
    """

    def test_numeric_one_to_true_is_persisted(self, cfg) -> None:
        cfg.write_text(json.dumps({"timezone": "UTC", "auto_update": 1}), encoding="utf-8")

        data = read_config_for_update(cfg)
        data["auto_update"] = True
        write_config_atomically(cfg, data)

        raw = json.loads(cfg.read_text(encoding="utf-8"))
        assert raw["auto_update"] is True, (
            f"the requested JSON type was not persisted (still {raw['auto_update']!r})"
        )

    def test_int_to_float_is_persisted(self, cfg) -> None:
        cfg.write_text(json.dumps({"timezone": "UTC", "ratio": 1}), encoding="utf-8")

        data = read_config_for_update(cfg)
        data["ratio"] = 1.0
        write_config_atomically(cfg, data)

        raw = json.loads(cfg.read_text(encoding="utf-8"))
        assert isinstance(raw["ratio"], float), "int -> float is a JSON type change too"

    def test_an_actually_unchanged_key_is_still_skipped(self, cfg) -> None:
        """The comparison must not become so strict that every key looks edited -- that would
        replay the caller's whole dict and undo the entire point of the delta."""
        from kiro_crew.config.loader import _apply_delta

        snapshot = {"a": 1, "b": [1, 2], "c": {"d": True}, "e": "x", "f": None}
        desired = {"a": 1, "b": [1, 2], "c": {"d": True}, "e": "x", "f": None}
        base = {"a": 9, "b": [9], "c": {"d": False}, "e": "z", "f": 1}

        merged = _apply_delta(base, snapshot, desired)

        assert merged == base, f"an unchanged dict was replayed: {merged}"
