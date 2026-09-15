import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from stock_crawler.storage import RawStore, RunLock, RunLocked, SnapshotMissing, StateStore, StorageError, combined_content_hash

from .conftest import FakeClock


def test_combined_hash_is_order_independent_and_excludes_metadata():
    a = combined_content_hash({"source.html": "aa", "table.json": "bb"})
    b = combined_content_hash({"table.json": "bb", "source.html": "aa"})
    assert a == b and len(a) == 64
    assert combined_content_hash({"source.html": "aa"}) != a


def test_snapshot_is_atomic_idempotent_and_readable(tmp_path):
    store = RawStore(tmp_path)
    files = {"source.html": b"<html>1</html>", "assets/table.json": b"{}"}
    ref, created = store.write_snapshot(market_source="kap", source_company_id="c1", notification_id="100", files=files, manifest={"published_at": "2025-03-05T15:45:00+00:00"})
    assert created and ref.path.is_dir()
    assert list((store.root / ".tmp").iterdir()) == []
    manifest = store.read_manifest(ref)
    assert manifest["files"]["source.html"]["sha256"] and manifest["content_hash"] == ref.content_hash
    assert store.read_files(ref) == files
    again, created_again = store.write_snapshot(market_source="kap", source_company_id="c1", notification_id="100", files=files, manifest={"published_at": "x"})
    assert not created_again and again.path == ref.path
    assert store.read_manifest(ref)["published_at"] == "2025-03-05T15:45:00+00:00"  # first manifest preserved


def test_changed_content_creates_a_second_snapshot_and_latest_prefers_publication(tmp_path):
    store = RawStore(tmp_path)
    ref1, _ = store.write_snapshot(market_source="kap", source_company_id="c1", notification_id="100", files={"source.html": b"v1"}, manifest={"published_at": "2025-03-05", "first_retrieved_at": "2025-03-06"})
    ref2, created = store.write_snapshot(market_source="kap", source_company_id="c1", notification_id="100", files={"source.html": b"v2"}, manifest={"published_at": "2025-03-05", "first_retrieved_at": "2025-03-20"})
    assert created and ref1.content_hash != ref2.content_hash
    assert len(store.snapshots_for_notification("kap", "c1", "100")) == 2
    ref3, _ = store.write_snapshot(market_source="kap", source_company_id="c1", notification_id="99", files={"source.html": b"old"}, manifest={"published_at": "2024-03-05", "first_retrieved_at": "2025-04-01"})
    assert store.latest_snapshot("kap", "c1").notification_id == "100"


def test_corrupt_or_missing_snapshot_is_reported(tmp_path):
    store = RawStore(tmp_path)
    ref, _ = store.write_snapshot(market_source="kap", source_company_id="c1", notification_id="1", files={"source.html": b"abc"}, manifest={})
    (ref.path / "source.html").write_bytes(b"tampered")
    with pytest.raises(StorageError, match="hash mismatch"):
        store.read_files(ref)
    (ref.path / "source.html").unlink()
    with pytest.raises(SnapshotMissing):
        store.read_files(ref)
    assert store.list_snapshots("kap", "nobody") == []


def test_snapshot_rejects_credentials_and_bad_names(tmp_path):
    store = RawStore(tmp_path)
    with pytest.raises(StorageError):
        store.write_snapshot(market_source="kap", source_company_id="c", notification_id="1", files={"../x": b""}, manifest={})
    with pytest.raises(StorageError):
        store.write_snapshot(market_source="kap", source_company_id="c", notification_id="1", files={"a": b""}, manifest={"Authorization": "x"})
    with pytest.raises(StorageError):
        store.write_snapshot(market_source="kap", source_company_id="c", notification_id="1", files={}, manifest={})


def test_parsed_output_lives_beside_snapshot(tmp_path):
    store = RawStore(tmp_path)
    ref, _ = store.write_snapshot(market_source="kap", source_company_id="c", notification_id="1", files={"source.html": b"x"}, manifest={})
    path = store.write_parsed(ref, "1.0.0", {"status": "valid"})
    assert path == ref.path / "parsed" / "1.0.0.json" and json.loads(path.read_text())["status"] == "valid"


def test_state_store_cooldowns_survive_restart_and_expire(tmp_path):
    clock = FakeClock()
    state = StateStore(tmp_path, clock=clock)
    state.set_host_cooldown("www.kap.org.tr", clock() + timedelta(hours=1), "HTTP 429")
    reloaded = StateStore(tmp_path, clock=clock)
    until, reason = reloaded.host_cooldown("www.kap.org.tr")
    assert until == clock() + timedelta(hours=1) and reason == "HTTP 429"
    clock.advance(hours=2)
    assert reloaded.host_cooldown("www.kap.org.tr") == (None, None)
    state.set_revalidation("kap/c/1", validators={"source.html:etag": '"abc"'})
    assert state.revalidation("kap/c/1")["validators"] == {"source.html:etag": '"abc"'}
    assert state.revalidation("missing") == {}


def test_run_lock_rejects_overlap_even_after_six_hours(tmp_path):
    clock = FakeClock()
    with RunLock(tmp_path, clock=clock):
        clock.advance(hours=7)
        with pytest.raises(RunLocked):
            RunLock(tmp_path, clock=clock).acquire()
    assert not (tmp_path / "state" / "crawler.lock").exists()
    # Diagnostic metadata never blocks recovery once the OS owner has exited.
    (tmp_path / "state" / "crawler.lock").write_text("corrupt old metadata")
    with RunLock(tmp_path, clock=clock):
        pass


def test_run_lock_is_released_when_owner_process_dies(tmp_path):
    import subprocess, sys
    source = "from pathlib import Path; import os; from stock_crawler.storage import RunLock; lock=RunLock(Path(__import__('sys').argv[1])); lock.acquire(); os._exit(0)"
    result = subprocess.run([sys.executable, "-c", source, str(tmp_path)], timeout=10)
    assert result.returncode == 0
    with RunLock(tmp_path):
        pass


def test_corrupt_host_state_fails_closed(tmp_path):
    state = StateStore(tmp_path)
    state.root.mkdir(parents=True)
    (state.root / "hosts.json").write_text("broken json")
    with pytest.raises(StorageError, match="corrupt operational state"):
        state.host_cooldown("www.kap.org.tr")


@pytest.mark.parametrize("bad", ["../outside", "/tmp/out", "a/b", "a\\b"])
def test_snapshot_identifier_cannot_escape_root(tmp_path, bad):
    with pytest.raises(StorageError):
        RawStore(tmp_path).write_snapshot(market_source="kap", source_company_id=bad, notification_id="1", files={"a": b"data"}, manifest={})
