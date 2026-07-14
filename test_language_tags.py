"""Self-check for filename language detection: python test_language_tags.py"""
from pathlib import Path

from uploader.__main__ import guess_language_from_filename as guess

CASES = [
    # dot-separated tags
    ("Movie.uk.srt", "uk"),
    ("Movie.uk-ua.forced.srt", "uk"),
    ("Show.en-GB.forced.srt", "en"),
    ("Movie.uk.DUBTITLE.subtitles.srt", "uk"),
    ("Mad Men_S01E01_Smoke Gets in Your Eyes.uk.subtitles.srt", "uk"),
    # bracketed tags (mkvextract / DVDFab naming)
    ("Battle for Skyark 2015_track2_[ukr]_DELAY 0ms.eac3", "uk"),
    ("Blacktalon S01E01_track2_[eng]_DELAY 0ms.aac", "en"),
    # no determinable language
    ("Blacktalon S01E01_track2_[und]_DELAY 0ms.aac", ""),
    ("Harry Potter and the Chamber of Secrets_Audio02.eac3", ""),
    # title words must not be read as languages
    ("The.Italian.Job.mkv", ""),
    ("It_track2.eac3", ""),  # bare underscore tokens are not scanned
]

for name, expected in CASES:
    actual = guess(Path(name))
    assert actual == expected, f"{name!r}: expected {expected!r}, got {actual!r}"

print(f"OK: {len(CASES)} language-tag cases passed")
