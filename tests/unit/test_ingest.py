"""Ingest service: validation, duplicates, failure handling, reconcile, discovery."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core.errors import (
    ImportRecordError,
    InvalidSourceError,
    OriginalProtectionError,
    UnsupportedMediaError,
)
from iftyedits.core.models import ImportOutcome, VideoStatus
from iftyedits.services.ingest import IngestService
from iftyedits.storage.originals import sha256_file
from tests.conftest import FAKE_AUDIO, FakeProber, MakeMedia


def _vault_files(settings: Settings) -> list[Path]:
    return [p for p in settings.originals_dir.rglob("*") if p.is_file()]


def _staging_files(settings: Settings) -> list[Path]:
    staging = settings.staging_dir
    return [p for p in staging.iterdir() if p.is_file()] if staging.exists() else []


def test_import_new_video(ingest: IngestService, make_media: MakeMedia, settings: Settings) -> None:
    source = make_media("Interview Part 1.mp4")
    before = (sha256_file(source), source.stat().st_mtime_ns)

    result = ingest.import_video(source)

    assert result.outcome is ImportOutcome.IMPORTED
    video = result.video
    assert video.status is VideoStatus.READY
    assert video.original_filename == "Interview Part 1.mp4"
    assert video.sha256 == before[0]
    assert (settings.originals_dir / video.vault_relpath).read_bytes() == source.read_bytes()
    assert [s.codec_type for s in ingest.repo.streams(video.id)] == ["video", "audio"]
    assert [s.source_path for s in ingest.repo.sources(video.id)] == [str(source.resolve())]
    assert (sha256_file(source), source.stat().st_mtime_ns) == before
    assert _staging_files(settings) == []


def test_reimport_same_file_is_duplicate_without_copy(
    ingest: IngestService, make_media: MakeMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_media("a.mp4")
    first = ingest.import_video(source)

    def no_copy(*args: object, **kwargs: object) -> None:
        raise AssertionError("duplicate content must not be copied again")

    monkeypatch.setattr(ingest.vault, "import_file", no_copy)
    second = ingest.import_video(source)

    assert second.outcome is ImportOutcome.DUPLICATE
    assert second.video.id == first.video.id


def test_same_content_different_name_and_extension(
    ingest: IngestService, make_media: MakeMedia, settings: Settings
) -> None:
    source = make_media("original.mp4")
    renamed = make_media("Copy Of Clip.MOV", kind=b"", body=source.read_bytes())

    first = ingest.import_video(source)
    second = ingest.import_video(renamed)

    assert second.outcome is ImportOutcome.DUPLICATE
    assert second.video.id == first.video.id
    assert len(_vault_files(settings)) == 1
    sources = [s.source_path for s in ingest.repo.sources(first.video.id)]
    assert sources == [str(source.resolve()), str(renamed.resolve())]


def test_same_size_different_content_is_imported(
    ingest: IngestService, make_media: MakeMedia
) -> None:
    a = make_media("a.mp4", body=b"1" * 100)
    b = make_media("b.mp4", body=b"2" * 100)
    assert ingest.import_video(a).outcome is ImportOutcome.IMPORTED
    assert ingest.import_video(b).outcome is ImportOutcome.IMPORTED


def test_non_media_file_is_rejected_before_copy(
    ingest: IngestService, make_media: MakeMedia, settings: Settings
) -> None:
    fake = make_media("notes.mp4", kind=b"hello ")
    with pytest.raises(UnsupportedMediaError):
        ingest.import_video(fake)
    assert _vault_files(settings) == []


def test_audio_only_is_rejected(
    ingest: IngestService, make_media: MakeMedia, settings: Settings
) -> None:
    with pytest.raises(UnsupportedMediaError, match="No video stream"):
        ingest.import_video(make_media("song.mp4", kind=FAKE_AUDIO))
    assert _vault_files(settings) == []


def test_unsupported_extension_is_rejected_without_probe(
    ingest: IngestService, make_media: MakeMedia, prober: FakeProber
) -> None:
    with pytest.raises(UnsupportedMediaError):
        ingest.import_video(make_media("document.pdf"))
    assert prober.calls == []


def test_missing_and_directory_sources(ingest: IngestService, tmp_path: Path) -> None:
    with pytest.raises(InvalidSourceError):
        ingest.import_video(tmp_path / "missing.mp4")
    with pytest.raises(InvalidSourceError):
        ingest.import_video(tmp_path)


@pytest.mark.parametrize("area", ["originals", "staging", "derived", "exports", "db"])
def test_import_from_data_directory_is_refused(
    ingest: IngestService, settings: Settings, area: str
) -> None:
    inside = settings.data_dir / area / "x.mp4"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"FAKEVIDEO")
    with pytest.raises(OriginalProtectionError):
        ingest.import_video(inside)


def test_symlink_into_data_directory_is_refused(
    ingest: IngestService, settings: Settings, tmp_path: Path
) -> None:
    target = settings.derived_dir / "proxy.mp4"
    target.write_bytes(b"FAKEVIDEO")
    link = tmp_path / "innocent.mp4"
    link.symlink_to(target)
    with pytest.raises(OriginalProtectionError):
        ingest.import_video(link)


def test_traversal_path_is_resolved_and_checked(
    ingest: IngestService, settings: Settings, tmp_path: Path
) -> None:
    target = settings.exports_dir / "short.mp4"
    target.write_bytes(b"FAKEVIDEO")
    (tmp_path / "user_media").mkdir()
    sneaky = tmp_path / "user_media" / ".." / "data" / "exports" / "short.mp4"
    with pytest.raises(OriginalProtectionError):
        ingest.import_video(sneaky)


def test_database_failure_keeps_original_and_reconcile_recovers(
    ingest: IngestService,
    make_media: MakeMedia,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_media("precious.mp4")
    sha = sha256_file(source)

    def db_down(**kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as m:
        m.setattr(ingest.repo, "record_import", db_down)
        with pytest.raises(ImportRecordError) as caught:
            ingest.import_video(source)
    assert caught.value.sha256 == sha

    stored = _vault_files(settings)
    assert len(stored) == 1
    assert sha256_file(stored[0]) == sha
    assert ingest.repo.get_by_sha(sha) is None

    report = ingest.reconcile()
    assert report.adopted == [sha]
    assert report.corrupt == []
    video = ingest.repo.get_by_sha(sha)
    assert video is not None
    assert video.status is VideoStatus.READY
    assert ingest.reconcile().adopted == []


def test_reconcile_reports_corrupt_file_and_keeps_it(
    ingest: IngestService, settings: Settings
) -> None:
    sha = "c" * 64
    bad = settings.originals_dir / sha[:2] / f"{sha}.mp4"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"FAKEVIDEO tampered")
    report = ingest.reconcile()
    assert report.corrupt == [sha]
    assert bad.exists()
    assert ingest.repo.get_by_sha(sha) is None


def test_probe_failure_of_stored_copy_is_recorded(
    ingest: IngestService,
    make_media: MakeMedia,
    prober: FakeProber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_media("flaky.mp4")
    real_probe = prober.probe

    def probe(path: Path) -> object:
        if path.resolve() != source.resolve():
            from iftyedits.core.errors import ProbeError

            raise ProbeError("moov atom not found")
        return real_probe(path)

    monkeypatch.setattr(prober, "probe", probe)
    result = ingest.import_video(source)
    assert result.video.status is VideoStatus.PROBE_FAILED
    assert result.video.probe_error == "moov atom not found"


def test_progress_callback_is_forwarded(ingest: IngestService, make_media: MakeMedia) -> None:
    calls: list[tuple[int, int]] = []
    source = make_media("p.mp4")
    ingest.import_video(source, progress=lambda done, total: calls.append((done, total)))
    assert calls[-1] == (source.stat().st_size, source.stat().st_size)


# ------------------------------------------------------------------ discovery


def test_discover_top_level_only(ingest: IngestService, make_media: MakeMedia) -> None:
    a = make_media("a.mp4")
    make_media("nested/b.mov")
    make_media("readme.txt")
    make_media(".hidden.mp4")
    found = ingest.discover(a.parent)
    assert found.files == (a,)


def test_discover_recursive(ingest: IngestService, make_media: MakeMedia) -> None:
    a = make_media("a.mp4")
    b = make_media("nested/deeper/b.MKV")
    make_media(".cache/c.mp4")
    found = ingest.discover(a.parent, recursive=True)
    assert set(found.files) == {a, b}


def test_discover_skips_symlinks(
    ingest: IngestService, make_media: MakeMedia, tmp_path: Path
) -> None:
    a = make_media("a.mp4")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.mp4").write_bytes(b"FAKEVIDEO")
    (a.parent / "linked.mp4").symlink_to(elsewhere / "secret.mp4")
    (a.parent / "linked_dir").symlink_to(elsewhere, target_is_directory=True)

    found = ingest.discover(a.parent, recursive=True)

    assert found.files == (a,)
    reasons = {p.name: r for p, r in found.skipped}
    assert reasons == {"linked.mp4": "symbolic link", "linked_dir": "symbolic link to a folder"}


def test_discover_refuses_data_directory(ingest: IngestService, settings: Settings) -> None:
    with pytest.raises(OriginalProtectionError):
        ingest.discover(settings.data_dir)
    with pytest.raises(OriginalProtectionError):
        ingest.discover(settings.data_dir.parent)


def test_discover_missing_folder(ingest: IngestService, tmp_path: Path) -> None:
    with pytest.raises(InvalidSourceError):
        ingest.discover(tmp_path / "nope")
