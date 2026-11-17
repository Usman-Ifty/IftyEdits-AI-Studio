# Integration tests

Tests that call FFmpeg, ML models or SQLite on disk. Mark them with
`@pytest.mark.ffmpeg` and/or `@pytest.mark.slow`. Use `tmp_path`; never the real `data/`.
