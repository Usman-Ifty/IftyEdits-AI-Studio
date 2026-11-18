"""Phase 1A vault hardening: single pass, staging, disk space, interruption, dedupe."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core.errors import InsufficientSpaceError, OriginalProtectionError
from iftyedits.storage import originals
from iftyedits.storage.originals import PARTIAL_SUFFIX, OriginalsVault, sha256_file


def _vault_files(vault: OriginalsVault) -> list[Path]:
    return [p for p in vault.root.rglob("*") if p.is_file()]


def _staging_files(vault: OriginalsVault) -> list[Path]:
    if not vault.staging_dir.exists():
        return []
    return [p for p in vault.staging_dir.iterdir() if p.is_file()]


def test_source_is_read_exactly_once(
    vault: OriginalsVault, source_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []
    real_open = Path.open

    def tracking_open(self: Path, *args: object, **kwargs: object) -> object:
        if self.resolve() == source_video.resolve():
            opened.append(self)
        return real_open(self, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(Path, "open", tracking_open)
    vault.import_file(source_video)
    assert len(opened) == 1


def test_same_bytes_with_different_name_and_extension_stored_once(
    vault: OriginalsVault, source_video: Path, tmp_path: Path
) -> None:
    renamed = tmp_path / "completely different name.MOV"
    renamed.write_bytes(source_video.read_bytes())

    first = vault.import_file(source_video)
    second = vault.import_file(renamed)

    assert second.already_present
    assert second.path == first.path
    assert second.sha256 == first.sha256
    assert len(_vault_files(vault)) == 1
    assert _staging_files(vault) == []


def test_find_existing_ignores_unrelated_files(vault: OriginalsVault, source_video: Path) -> None:
    result = vault.import_file(source_video)
    assert vault.find_existing(result.sha256) == result.path
    assert vault.find_existing("f" * 64) is None


def test_symlink_in_vault_is_never_trusted(
    vault: OriginalsVault, source_video: Path, tmp_path: Path
) -> None:
    sha = sha256_file(source_video)
    shard = vault.root / sha[:2]
    shard.mkdir(parents=True)
    decoy = tmp_path / "decoy.mp4"
    decoy.write_bytes(source_video.read_bytes())
    (shard / f"{sha}.mp4").symlink_to(decoy)

    with pytest.raises(OriginalProtectionError):
        vault.import_file(source_video)


def test_symlinked_shard_directory_is_rejected(
    vault: OriginalsVault, source_video: Path, tmp_path: Path
) -> None:
    sha = sha256_file(source_video)
    outside = tmp_path / "outside"
    outside.mkdir()
    vault.root.mkdir(parents=True, exist_ok=True)
    (vault.root / sha[:2]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(OriginalProtectionError):
        vault.import_file(source_video)
    assert list(outside.iterdir()) == []


def test_insufficient_space_refused_before_copy(
    settings: Settings, source_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = OriginalsVault(settings.originals_dir, reserve_bytes=10**18)
    with pytest.raises(InsufficientSpaceError):
        vault.import_file(source_video)
    assert _vault_files(vault) == []
    assert _staging_files(vault) == []


def test_disk_full_during_copy_cleans_up(
    vault: OriginalsVault, source_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(copied: int, total: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(InsufficientSpaceError):
        vault.import_file(source_video, progress=disk_full)
    assert _vault_files(vault) == []
    assert _staging_files(vault) == []


def test_interrupted_import_leaves_no_partial_and_source_intact(
    vault: OriginalsVault, source_video: Path
) -> None:
    original = source_video.read_bytes()

    def interrupt(copied: int, total: int) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        vault.import_file(source_video, progress=interrupt)
    assert _vault_files(vault) == []
    assert _staging_files(vault) == []
    assert source_video.read_bytes() == original


def test_progress_reports_total(vault: OriginalsVault, source_video: Path) -> None:
    calls: list[tuple[int, int]] = []
    vault.import_file(source_video, progress=lambda done, total: calls.append((done, total)))
    size = source_video.stat().st_size
    assert calls[-1] == (size, size)


def test_clean_staging_removes_only_stale_unlocked_partials(vault: OriginalsVault) -> None:
    vault.staging_dir.mkdir(parents=True)
    stale = vault.staging_dir / f"old{PARTIAL_SUFFIX}"
    fresh = vault.staging_dir / f"new{PARTIAL_SUFFIX}"
    other = vault.staging_dir / "notes.txt"
    for path in (stale, fresh, other):
        path.write_bytes(b"x")
    old = 1_000_000_000
    os.utime(stale, (old, old))
    os.utime(other, (old, old))

    assert vault.clean_staging() == 1
    assert not stale.exists()
    assert fresh.exists()
    assert other.exists()


def test_clean_staging_skips_locked_partial(vault: OriginalsVault) -> None:
    import fcntl

    vault.staging_dir.mkdir(parents=True)
    active = vault.staging_dir / f"active{PARTIAL_SUFFIX}"
    active.write_bytes(b"x")
    os.utime(active, (1_000_000_000, 1_000_000_000))
    fd = os.open(active, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert vault.clean_staging() == 0
        assert active.exists()
    finally:
        os.close(fd)


def test_clean_staging_never_touches_vault(vault: OriginalsVault, source_video: Path) -> None:
    result = vault.import_file(source_video)
    os.utime(result.path, (1_000_000_000, 1_000_000_000), follow_symlinks=True)
    vault.clean_staging(max_age_s=0)
    assert result.path.exists()


def test_staging_inside_vault_is_rejected(settings: Settings) -> None:
    with pytest.raises(OriginalProtectionError):
        OriginalsVault(settings.originals_dir, staging_dir=settings.originals_dir / "tmp")


def test_import_from_staging_is_rejected(vault: OriginalsVault) -> None:
    vault.staging_dir.mkdir(parents=True)
    inside = vault.staging_dir / "x.mp4"
    inside.write_bytes(b"x")
    with pytest.raises(OriginalProtectionError):
        vault.import_file(inside)


def test_iter_originals_lists_stored_files(vault: OriginalsVault, source_video: Path) -> None:
    result = vault.import_file(source_video)
    assert list(vault.iter_originals()) == [(result.sha256, result.path)]
    assert vault.relative_path(result.path) == Path(result.sha256[:2]) / result.path.name


def test_vault_file_mode_and_single_link(vault: OriginalsVault, source_video: Path) -> None:
    result = vault.import_file(source_video)
    st = result.path.stat()
    assert stat.S_IMODE(st.st_mode) == originals.READ_ONLY_MODE
    assert st.st_nlink == 1
    assert source_video.stat().st_nlink == 1
