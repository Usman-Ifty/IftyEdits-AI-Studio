"""Original-media protection rules. These tests guard CLAUDE.md section 8."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core.errors import InvalidSourceError, MediaIntegrityError, OriginalProtectionError
from iftyedits.storage import originals
from iftyedits.storage.originals import OriginalsVault, assert_safe_output_path, sha256_file


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ hashing


def test_sha256_matches_hashlib(source_video: Path) -> None:
    assert sha256_file(source_video) == _sha(source_video.read_bytes())


def test_sha256_handles_multi_chunk_files(tmp_path: Path) -> None:
    data = os.urandom(originals.CHUNK_SIZE * 2 + 17)
    path = tmp_path / "big.bin"
    path.write_bytes(data)
    assert sha256_file(path) == _sha(data)


# ------------------------------------------------------------------ independent copies


def test_import_creates_verified_copy(vault: OriginalsVault, source_video: Path) -> None:
    result = vault.import_file(source_video)

    assert result.sha256 == _sha(source_video.read_bytes())
    assert result.path.read_bytes() == source_video.read_bytes()
    assert result.path.name == f"{result.sha256}.mp4"
    assert result.path.parent.name == result.sha256[:2]
    assert result.size_bytes == source_video.stat().st_size
    assert not result.already_present


def test_copy_is_independent_not_a_hard_link(vault: OriginalsVault, source_video: Path) -> None:
    result = vault.import_file(source_video)
    src_stat, copy_stat = source_video.stat(), result.path.stat()

    assert src_stat.st_ino != copy_stat.st_ino
    assert copy_stat.st_nlink == 1
    assert src_stat.st_nlink == 1
    assert not result.path.is_symlink()


def test_changing_source_after_import_leaves_copy_intact(
    vault: OriginalsVault, source_video: Path
) -> None:
    original_bytes = source_video.read_bytes()
    result = vault.import_file(source_video)

    source_video.write_bytes(b"user edited their file")

    assert result.path.read_bytes() == original_bytes
    assert vault.verify(result.path, result.sha256)


def test_deleting_source_after_import_leaves_copy_intact(
    vault: OriginalsVault, source_video: Path
) -> None:
    result = vault.import_file(source_video)
    source_video.unlink()
    assert vault.verify(result.path, result.sha256)


# ------------------------------------------------------------------ source untouched


def test_source_is_unchanged_by_import(vault: OriginalsVault, source_video: Path) -> None:
    before_bytes = source_video.read_bytes()
    before = source_video.stat()

    vault.import_file(source_video)

    after = source_video.stat()
    assert source_video.read_bytes() == before_bytes
    assert after.st_mtime_ns == before.st_mtime_ns
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_ino == before.st_ino


# ------------------------------------------------------------------ read-only


def test_vault_copy_is_read_only(vault: OriginalsVault, source_video: Path) -> None:
    result = vault.import_file(source_video)
    assert stat.S_IMODE(result.path.stat().st_mode) == originals.READ_ONLY_MODE
    with pytest.raises(PermissionError):
        result.path.open("ab")


# ------------------------------------------------------------------ never overwrite


def test_reimport_is_deduplicated_without_rewrite(
    vault: OriginalsVault, source_video: Path
) -> None:
    first = vault.import_file(source_video)
    first_stat = first.path.stat()

    second = vault.import_file(source_video)

    assert second.already_present
    assert second.path == first.path
    assert second.path.stat().st_ino == first_stat.st_ino
    assert second.path.stat().st_mtime_ns == first_stat.st_mtime_ns


def test_corrupt_vault_file_is_never_replaced(vault: OriginalsVault, source_video: Path) -> None:
    sha = sha256_file(source_video)
    dest = vault.path_for(sha, ".mp4")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"corrupt")

    with pytest.raises(MediaIntegrityError):
        vault.import_file(source_video)
    assert dest.read_bytes() == b"corrupt"


def test_exclusive_copy_refuses_existing_destination(tmp_path: Path, source_video: Path) -> None:
    dest = tmp_path / "existing.mp4"
    dest.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        OriginalsVault._exclusive_copy(source_video, dest)
    assert dest.read_bytes() == b"keep me"


def test_source_changing_during_import_is_rejected(
    vault: OriginalsVault, source_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_copy = OriginalsVault._exclusive_copy

    def copy_then_tamper(source: Path, dest: Path) -> str:
        digest = real_copy(source, dest)
        source.write_bytes(b"modified mid-import")
        return digest

    monkeypatch.setattr(OriginalsVault, "_exclusive_copy", staticmethod(copy_then_tamper))

    with pytest.raises(MediaIntegrityError):
        vault.import_file(source_video)
    assert not any(p.is_file() for p in vault.root.rglob("*"))


# ------------------------------------------------------------------ invalid sources


def test_missing_source_raises(vault: OriginalsVault, tmp_path: Path) -> None:
    with pytest.raises(InvalidSourceError):
        vault.import_file(tmp_path / "nope.mp4")


def test_directory_source_raises(vault: OriginalsVault, tmp_path: Path) -> None:
    with pytest.raises(InvalidSourceError):
        vault.import_file(tmp_path)


def test_importing_from_inside_vault_is_rejected(vault: OriginalsVault, source_video: Path) -> None:
    result = vault.import_file(source_video)
    with pytest.raises(OriginalProtectionError):
        vault.import_file(result.path)


def test_symlink_source_copies_target_bytes(
    vault: OriginalsVault, source_video: Path, tmp_path: Path
) -> None:
    link = tmp_path / "link.mp4"
    link.symlink_to(source_video)
    result = vault.import_file(link)
    assert not result.path.is_symlink()
    assert result.path.read_bytes() == source_video.read_bytes()


def test_unsafe_suffix_is_dropped(vault: OriginalsVault) -> None:
    sha = "a" * 64
    assert vault.path_for(sha, ".mp4/../x").name == sha
    with pytest.raises(ValueError):
        vault.path_for("../../etc", ".mp4")


# ------------------------------------------------------------------ output separation


def test_output_inside_vault_is_rejected(settings: Settings) -> None:
    with pytest.raises(OriginalProtectionError):
        assert_safe_output_path(
            settings.originals_dir / "ab" / "clip.mp4",
            originals_root=settings.originals_dir,
            allowed_roots=settings.output_roots,
        )


def test_output_traversal_into_vault_is_rejected(settings: Settings) -> None:
    sneaky = settings.derived_dir / ".." / "originals" / "x.mp4"
    with pytest.raises(OriginalProtectionError):
        assert_safe_output_path(
            sneaky, originals_root=settings.originals_dir, allowed_roots=settings.output_roots
        )


def test_output_via_symlink_into_vault_is_rejected(settings: Settings) -> None:
    link = settings.derived_dir / "escape"
    link.symlink_to(settings.originals_dir, target_is_directory=True)
    with pytest.raises(OriginalProtectionError):
        assert_safe_output_path(
            link / "x.mp4",
            originals_root=settings.originals_dir,
            allowed_roots=settings.output_roots,
        )


def test_output_outside_allowed_roots_is_rejected(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(OriginalProtectionError):
        assert_safe_output_path(
            tmp_path / "elsewhere.mp4",
            originals_root=settings.originals_dir,
            allowed_roots=settings.output_roots,
        )


def test_output_in_derived_is_allowed(settings: Settings) -> None:
    target = settings.derived_dir / "proxy" / "clip.mp4"
    assert (
        assert_safe_output_path(
            target, originals_root=settings.originals_dir, allowed_roots=settings.output_roots
        )
        == target.resolve()
    )
