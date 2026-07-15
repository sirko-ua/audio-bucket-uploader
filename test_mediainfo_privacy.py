"""Self-check for MediaInfo path sanitizing: python test_mediainfo_privacy.py"""
from pathlib import Path

from uploader.__main__ import sanitize_media_info_paths


source = Path("/Users/private/Videos/Movie.mkv")
payload = {
    "media": {
        "@ref": str(source),
        "track": [
            {
                "@type": "General",
                "CompleteName": str(source),
                "FolderName": str(source.parent),
                "FileName": source.stem,
            }
        ],
    }
}
text = f"""General
Complete name                            : {source}
Format                                   : Matroska
"""

sanitized_payload, sanitized_text = sanitize_media_info_paths(payload, text, source)
general = sanitized_payload["media"]["track"][0]

assert sanitized_payload["media"]["@ref"] == source.name
assert general["CompleteName"] == source.name
assert "FolderName" not in general
assert str(source.parent) not in str(sanitized_payload)
assert f": {source.name}" in sanitized_text
assert str(source.parent) not in sanitized_text
assert "Format                                   : Matroska" in sanitized_text

print("OK: MediaInfo paths are replaced with the source filename")
