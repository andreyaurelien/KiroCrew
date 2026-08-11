"""Tests for the cycle-5 review findings.

Three defects that all shared one shape: a failure was absorbed into a success. A
failed database copy warned and continued, a failed tree copy left the tree deleted and
exited zero, and an oversized bundle uploaded fine and only failed on the host trying to
restore it.
"""

from __future__ import annotations

import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import snapshot as snap


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    _setup_fake_kirocrew(d)
    return d


class TestAFailedDatabaseCopyIsNotSilentlyDowngraded:
    def _db(self, path):
        with sqlite3.connect(str(path)) as c:
            c.execute("CREATE TABLE t (v TEXT)")
            c.execute("INSERT INTO t VALUES ('real')")
        return path

    def _staged_pair(self, tmp_path):
        """Mirror production: the tree copy already placed a byte copy at dst, so dst is
        itself a valid database before _restage_databases replaces it."""
        import shutil

        src, dst = tmp_path / "s", tmp_path / "d"
        src.mkdir()
        dst.mkdir()
        self._db(src / "memory.db")
        shutil.copy2(src / "memory.db", dst / "memory.db")
        return src, dst

    def test_a_non_database_file_keeps_the_plain_copy(self, tmp_path, capsys):
        src, dst = tmp_path / "s", tmp_path / "d"
        src.mkdir()
        dst.mkdir()
        (src / "notes.db").write_text("this is not a database", encoding="utf-8")
        (dst / "notes.db").write_text("this is not a database", encoding="utf-8")
        snap._restage_databases(src, dst)
        assert "not a readable SQLite database" in capsys.readouterr().out
        assert (dst / "notes.db").read_text(encoding="utf-8") == "this is not a database"

    def test_a_real_database_is_copied_consistently(self, tmp_path):
        src, dst = self._staged_pair(tmp_path)
        snap._restage_databases(src, dst)
        with closing(sqlite3.connect(str(dst / "memory.db"))) as c:
            assert c.execute("SELECT v FROM t").fetchone()[0] == "real"

    def _break_backup(self, monkeypatch, message):
        """`sqlite3.Connection` is immutable, so the failure is injected at the
        `connect` seam the module actually calls.

        The exception class comes from ``snap.sqlite3``, NOT this file's ``import
        sqlite3``: the package binds a different DB-API implementation, so an error
        built from the stdlib module is not caught by production's ``except
        sqlite3.Error`` at all. A first version of these tests did exactly that and
        passed against a mutant that swallowed the failure -- the error escaped for the
        wrong reason.
        """
        prod = snap.sqlite3
        real_connect = prod.connect
        exc = prod.OperationalError(message)

        class _NoBackup:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def backup(self, *a, **kw):
                raise exc

        monkeypatch.setattr(prod, "connect", lambda *a, **kw: _NoBackup(real_connect(*a, **kw)))

    def test_a_failed_backup_on_a_real_database_propagates(self, tmp_path, monkeypatch):
        """The dangerous case: readable database, failing copy. The staged file is a raw
        byte copy without its WAL sidecars, so success here would ship a torn database.
        """
        src, dst = self._staged_pair(tmp_path)
        self._break_backup(monkeypatch, "database is locked")
        with pytest.raises(snap.DatabaseCopyFailed) as e:
            snap._restage_databases(src, dst)
        assert e.value.path.name == "memory.db", "the error must name the file"

    def test_the_probe_does_not_mask_a_backup_failure_as_a_plain_file(
        self, tmp_path, monkeypatch, capsys
    ):
        """Guards the specific regression: catching every sqlite3.Error again."""
        src, dst = self._staged_pair(tmp_path)
        self._break_backup(monkeypatch, "disk I/O error")
        with pytest.raises(snap.DatabaseCopyFailed):
            snap._restage_databases(src, dst)
        assert "not a readable SQLite database" not in capsys.readouterr().out

    def test_the_command_reports_it_instead_of_crashing(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """Propagating is right, but the operator must get a message and a nonzero exit,
        not a traceback: `snapshot` is a command, and a traceback reads as a crash the
        operator would retry rather than act on."""
        db = home / "workspace" / "knowledge" / "knowledge.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(snap.sqlite3.connect(str(db))) as c:
            c.execute("CREATE TABLE t (v TEXT)")
        self._break_backup(monkeypatch, "database is locked")

        rc = snap.snapshot_main([str(tmp_path / "out"), "--components", "memory"])
        out = capsys.readouterr().out
        assert rc == 1, out
        # Core files are staged before trees, so memory.db is the one that fails first.
        # Both paths raise the same typed error -- there were two `backup()` call sites
        # and wrapping only the tree one left this path exiting on a traceback.
        assert "memory.db" in out, "the failing database must be named"
        assert "No bundle was written" in out
        assert not list((tmp_path / "out").glob("*.tar.gz")) if (tmp_path / "out").exists() else True

    def test_both_database_copy_sites_raise_the_typed_error(self):
        """Structural: core files and trees are separate `backup()` call sites, and only
        one was wrapped at first -- the other exited on a traceback."""
        import inspect
        import re

        src = inspect.getsource(snap)
        calls = len(re.findall(r"src_conn\.backup\(dst_conn\)", src))
        wrapped = len(re.findall(r"raise DatabaseCopyFailed\(src, e\) from e", src))
        assert calls >= 2, "expected both the core-file and tree copy sites"
        assert wrapped == calls, (
            f"{calls} backup call sites but {wrapped} raise the typed error; an "
            "unwrapped site exits the command on a traceback"
        )

    def test_the_module_under_test_and_this_file_agree_on_the_driver(self):
        """Pins the reason the tests above use snap.sqlite3: if the package ever binds
        the stdlib module, these tests keep working, and if it binds another one they
        still exercise the real except clause."""
        assert hasattr(snap.sqlite3, "Error") and hasattr(snap.sqlite3, "OperationalError")
        assert issubclass(snap.sqlite3.OperationalError, snap.sqlite3.Error)


class TestAFailedTreeReplacementPutsTheTreeBack:
    def test_the_whole_rollback_set_is_restored_not_just_the_failing_tree(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """The unit of atomicity is the restore, not one tree.

        Recovering only the tree that failed leaves every EARLIER tree replaced and the
        databases already swapped -- memory split across two restore generations, which
        is the state a rollback exists to prevent. So the assertion is on the whole set:
        the database content and the tree content both come back.
        """
        tree_root = home / "workspace"
        marker = tree_root / "memory" / "keep.md"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("live content", encoding="utf-8")
        live_db = home / "memory.db"
        live_db.write_bytes(b"LIVE DATABASE")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "new.md").write_text("x", encoding="utf-8")
        (payload / "memory.db").write_bytes(b"INCOMING DATABASE")
        bundle = tmp_path / "b.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        real = snap._copytree_safe

        def flaky(src, dst, **kw):
            # Fail the incoming copy into the live root; the rollback save and the
            # recovery copies must both go through.
            if Path(dst) == tree_root and "kirocrew-snapshot-" in str(src):
                raise OSError("No space left on device")
            return real(src, dst, **kw)

        monkeypatch.setattr(snap, "_copytree_safe", flaky)
        with pytest.raises(OSError):
            snap.restore_main([str(bundle), "--mode", "replace", "--force"])
        out = capsys.readouterr().out

        assert "Restoring the previous state" in out, out
        assert marker.is_file(), "the live tree was not put back"
        assert marker.read_text(encoding="utf-8") == "live content"
        assert live_db.read_bytes() == b"LIVE DATABASE", (
            "the database stayed on the incoming generation while the tree rolled back"
        )

    def test_the_rollback_directory_is_kept_when_recovery_itself_fails(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """If recovery cannot put something back, it must name it and leave the saved
        copy in place -- by that point the operator's own data is what is at stake."""
        backup = home / "pre-restore-test"
        (backup / "workspace").mkdir(parents=True)
        (backup / "workspace" / "saved.md").write_text("saved", encoding="utf-8")

        def refuse(src, dst, **kw):
            raise OSError("Read-only file system")

        monkeypatch.setattr(snap, "_copytree_safe", refuse)
        snap._restore_everything_from_rollback(backup, home)
        out = capsys.readouterr().out
        assert "Could not put these back" in out, out
        assert "workspace" in out
        assert backup.is_dir(), "the rollback directory must survive a failed recovery"

    def test_a_missing_rollback_directory_is_reported_not_crashed(
        self, home, capsys
    ):
        snap._restore_everything_from_rollback(home / "nope", home)
        assert "No rollback directory" in capsys.readouterr().out

    def test_a_successful_replace_is_unaffected(self, home, tmp_path):
        live = home / "workspace" / "memory"
        live.mkdir(parents=True, exist_ok=True)
        (live / "old.md").write_text("old", encoding="utf-8")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "new.md").write_text("new", encoding="utf-8")
        bundle = tmp_path / "ok.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        assert snap.restore_main([str(bundle), "--mode", "replace", "--force"]) == 0
        assert (live / "new.md").read_text(encoding="utf-8") == "new"


class TestAnUnrestorableBundleIsNeverUploaded:
    def test_a_bundle_over_the_download_bound_is_not_published(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """The bound was applied on download only, so a bundle past it uploaded fine and
        failed on the host trying to restore it."""
        monkeypatch.setattr(snap, "_MAX_ARCHIVE_BYTES", 512)
        big = home / "workspace" / "memory" / "big.md"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_text("x" * 8192, encoding="utf-8")

        uploaded: list[object] = []
        monkeypatch.setattr(
            snap, "_resolve_aws_profile", lambda p: ("someprofile", "us-west-2")
        )
        monkeypatch.setattr(
            snap.remote, "upload", lambda *a, **k: uploaded.append(a) or "s3://x/y"
        )
        monkeypatch.setattr(
            snap.remote, "load_destination", lambda *a, **k: _FakeDest()
        )

        rc = snap.snapshot_main([str(tmp_path / "out"), "--components", "memory", "--to-s3"])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert not uploaded, "an unrestorable bundle was published"
        assert "could not restore" in out or "uncompressed content" in out

    def test_an_ordinary_bundle_still_uploads(self, home, tmp_path, monkeypatch, capsys):
        uploaded: list[object] = []
        monkeypatch.setattr(
            snap, "_resolve_aws_profile", lambda p: ("someprofile", "us-west-2")
        )
        monkeypatch.setattr(
            snap.remote, "upload", lambda *a, **k: uploaded.append(a) or "s3://x/y"
        )
        monkeypatch.setattr(
            snap.remote, "load_destination", lambda *a, **k: _FakeDest()
        )
        rc = snap.snapshot_main([str(tmp_path / "out2"), "--components", "memory", "--to-s3"])
        assert rc == 0, capsys.readouterr().out
        assert uploaded, "an ordinary bundle must still be published"


class _FakeDest:
    bucket = "some-bucket"
    region = "us-west-2"
    account = "123456789012"

    def url_for(self, name):
        return f"s3://{self.bucket}/backups/host/{name}"
