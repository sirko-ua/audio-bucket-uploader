from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import blake3
import httpx


LANGUAGE_ALIASES = {
    "en": {"en", "eng", "english"},
    "uk": {"uk", "ukr", "ukrainian"},
    "ru": {"ru", "rus", "russian"},
    "es": {"es", "spa", "spanish"},
    "fr": {"fr", "fre", "fra", "french"},
    "de": {"de", "ger", "deu", "german"},
    "it": {"it", "ita", "italian"},
    "pl": {"pl", "pol", "polish"},
    "ja": {"ja", "jpn", "japanese"},
}

CODEC_EXTENSION_MAP = {
    "aac": "aac",
    "ac3": "ac3",
    "alac": "caf",
    "ass": "ass",
    "dts": "dts",
    "eac3": "eac3",
    "flac": "flac",
    "kate": "ogg",
    "mlp": "mlp",
    "mp2": "mp2",
    "mp3": "mp3",
    "opus": "opus",
    "pcm": "wav",
    "pgs": "sup",
    "ssa": "ssa",
    "subrip": "srt",
    "truehd": "thd",
    "tta": "tta",
    "usf": "usf",
    "vobsub": "sub",
    "vorbis": "ogg",
    "wavpack": "wv",
    "webvtt": "webvtt",
}

CODEC_CANONICAL_ALIASES = {
    "ac_3": "ac3",
    "a_ac3": "ac3",
    "a_alac": "alac",
    "a_dts": "dts",
    "a_eac3": "eac3",
    "a_flac": "flac",
    "a_mlp": "mlp",
    "a_mpegl2": "mp2",
    "a_mpegl3": "mp3",
    "a_opus": "opus",
    "a_pcmintbig": "pcm",
    "a_pcmintlit": "pcm",
    "a_truehd": "truehd",
    "a_tta1": "tta",
    "a_vorbis": "vorbis",
    "a_wavpack4": "wavpack",
    "dtses": "dts",
    "dtshdhra": "dts",
    "dtsxll": "dts",
    "e_ac_3": "eac3",
    "mlpfba": "mlp",
    "mpegaudiolayer2": "mp2",
    "mpegaudiolayer3": "mp3",
    "s_ass": "ass",
    "s_hdmvpgs": "pgs",
    "s_kate": "kate",
    "s_ssa": "ssa",
    "s_textascii": "subrip",
    "s_textass": "ass",
    "s_textssa": "ssa",
    "s_textusf": "usf",
    "s_textutf8": "subrip",
    "s_textwebvtt": "webvtt",
    "s_vobsub": "vobsub",
    "textutf8": "subrip",
    "trueaudio": "tta",
    "utf8": "subrip",
}

TRACK_TYPE_EXTENSION_DEFAULTS = {
    "audio": "bin",
    "subtitles": "sub",
}

ALL_SUBTITLE_LANGUAGES = "__all_subtitle_languages__"
DEFAULT_AUDIO_LANGUAGES = ["uk"]
DEFAULT_SUBTITLE_LANGUAGES = ["all"]
DEFAULT_INPUT = "/input"
DEFAULT_VERBOSE = False
DEFAULT_VISIBILITY = "public"
DEFAULT_STANDALONE = True

# Standalone (loose) media files that are uploaded directly rather than
# extracted from an MKV container. Extensions are matched case-insensitively.
STANDALONE_AUDIO_EXTENSIONS = {
    "wav", "mp3", "aac", "flac", "ogg", "m4a", "opus",
    "ac3", "eac3", "ac4", "dts", "dtshd", "truehd", "mlp", "thd",
}
STANDALONE_SUBTITLE_EXTENSIONS = {"ass", "srt", "pgs", "sup"}

# Video container extensions whose MediaInfo can supply the source video's
# unique_id that the uploader endpoint requires for a standalone track.
VIDEO_CONTAINER_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".webm", ".avi", ".mov", ".ts", ".m2ts", ".mpg", ".mpeg",
}

# token (already normalized) -> canonical language, built from LANGUAGE_ALIASES.
LANGUAGE_TOKEN_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in LANGUAGE_ALIASES.items():
    LANGUAGE_TOKEN_LOOKUP[_canonical] = _canonical
    for _alias in _aliases:
        LANGUAGE_TOKEN_LOOKUP[_alias] = _canonical


class UploaderError(RuntimeError):
    pass


class StandaloneSkip(Exception):
    """Raised when a standalone file is intentionally skipped (not an error)."""


@dataclass
class PreparedTrack:
    extraction_track_id: int | None
    media_info_track_id: str
    track_type: str
    language: str
    original_video_mediainfo: dict
    original_video_mediainfo_text: str
    output_path: Path
    cleanup_after_upload: bool = True


class ProgressFile:
    def __init__(self, file_obj, total_size: int, label: str) -> None:
        self._file_obj = file_obj
        self._total_size = max(total_size, 1)
        self._label = label
        self._uploaded = 0
        self._last_percent = -1
        self._line_open = False

    def read(self, size: int = -1) -> bytes:
        chunk = self._file_obj.read(size)
        if chunk:
            self._uploaded += len(chunk)
            self._render()
        return chunk

    def _render(self) -> None:
        percent = min(int(self._uploaded * 100 / self._total_size), 100)
        if percent == self._last_percent:
            return
        self._last_percent = percent
        uploaded_mb = self._uploaded / (1024 * 1024)
        total_mb = self._total_size / (1024 * 1024)
        message = f"{percent:3d}% ({uploaded_mb:.1f}/{total_mb:.1f} MiB)"
        sys.stdout.write(f"\r{format_log_line('INFO', self._label, 'upload', message)}")
        sys.stdout.flush()
        self._line_open = True

    def finish(self) -> None:
        self._uploaded = self._total_size
        self._render()
        sys.stdout.write("\n")
        sys.stdout.flush()
        self._line_open = False

    def close_line(self) -> None:
        if self._line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._line_open = False

    def __getattr__(self, name: str):
        return getattr(self._file_obj, name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract target-language audio and subtitle tracks from MKV files.",
    )
    parser.add_argument("--api-key", required=True, help="Audio Bucket user API key.")
    parser.add_argument(
        "--api-url",
        required=True,
        help="Audio Bucket uploader endpoint URL.",
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help="Path to a .mkv file or a directory containing .mkv files. Defaults to /input.",
    )
    parser.add_argument(
        "--audio-language",
        action="append",
        dest="audio_languages",
        help="Target audio track language. Can be passed multiple times or as a comma-separated list. Defaults to uk.",
    )
    parser.add_argument(
        "--subtitle-language",
        action="append",
        dest="subtitle_languages",
        help="Target subtitle track language. Can be passed multiple times or as a comma-separated list. Defaults to all.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(get_default_output_dir()),
        help=f"Directory where extracted tracks will be written. Defaults to the system temp directory ({get_default_output_dir()}).",
    )
    parser.add_argument(
        "--keep-extracted",
        action="store_true",
        help="Keep extracted files after successful upload instead of deleting them.",
    )
    parser.add_argument(
        "--visibility",
        choices=("draft", "public"),
        default=DEFAULT_VISIBILITY,
        help="Visibility for uploaded tracks. Defaults to public.",
    )
    parser.add_argument(
        "--standalone",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_STANDALONE,
        help=(
            "Also discover and upload standalone audio/subtitle files found next to "
            "or instead of MKV files (audio: "
            f"{', '.join(sorted(STANDALONE_AUDIO_EXTENSIONS))}; subtitles: "
            f"{', '.join(sorted(STANDALONE_SUBTITLE_EXTENSIONS))}). "
            "Language is taken from the file name (e.g. movie.uk.srt) or MediaInfo, and "
            "the file is attached to a sibling video (same base name) that supplies the "
            "required source MediaInfo; files without a determinable language or sibling "
            "video are skipped. Standalone source files are never deleted. Defaults to "
            "true; use --no-standalone to disable."
        ),
    )
    parser.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_VERBOSE,
        help="Print detailed detection, HTTP request, upload result, and cleanup output. Defaults to false.",
    )
    return parser.parse_args()


def run_json_command(command: list[str]) -> dict:
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", check=True
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UploaderError(f"Command did not return valid JSON: {' '.join(command)}") from exc


def run_text_command(command: list[str]) -> str:
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return completed.stdout


def sanitize_media_info_paths(
    media_info_payload: dict,
    media_info_text: str,
    file_path: Path,
) -> tuple[dict, str]:
    """Replace local paths in MediaInfo output with the source filename."""
    filename = file_path.name

    def sanitize_json(value):
        if isinstance(value, dict):
            for key in list(value):
                normalized_key = re.sub(r"[ _]", "", str(key)).lower()
                if normalized_key == "foldername":
                    # This has no value to the uploader and can expose the
                    # user's directory after CompleteName is sanitized.
                    value.pop(key)
                elif normalized_key == "completename" or key == "@ref":
                    value[key] = filename
                else:
                    value[key] = sanitize_json(value[key])
        elif isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = sanitize_json(item)
        return value

    sanitize_json(media_info_payload)

    # MediaInfo's text format labels this field "Complete name". Replace the
    # whole value as well as exact path occurrences, covering localized or
    # version-specific output without changing unrelated metadata.
    media_info_text = re.sub(
        r"(?mi)^([ \t]*Complete name[ \t]*:[ \t]*).*$",
        lambda match: f"{match.group(1)}{filename}",
        media_info_text,
    )
    path_candidates = {str(file_path)}
    try:
        path_candidates.add(str(file_path.resolve()))
    except OSError:
        pass
    for path in sorted(path_candidates, key=len, reverse=True):
        if path and path != filename:
            media_info_text = media_info_text.replace(path, filename)

    return media_info_payload, media_info_text


def run_command(command: list[str]) -> None:
    subprocess.run(command, check=True)


def normalize_language(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    normalized = lowered.replace("-", "_")
    return re.sub(r"[^a-z0-9_]+", "", normalized)


def expand_language_aliases(value: str) -> set[str]:
    normalized = normalize_language(value)
    aliases = {normalized}
    for canonical, known_aliases in LANGUAGE_ALIASES.items():
        if normalized == canonical or normalized in known_aliases:
            aliases.update(known_aliases)
            aliases.add(canonical)
    return aliases


def parse_language_filters(values: list[str] | None, default_values: list[str]) -> list[str]:
    if not values:
        return default_values

    normalized_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in value.split(","):
            normalized = normalize_language(part)
            if normalized and normalized not in seen:
                normalized_values.append(normalized)
                seen.add(normalized)
    return normalized_values or default_values


def parse_subtitle_language_filters(values: list[str] | None) -> list[str]:
    normalized_values = parse_language_filters(values, DEFAULT_SUBTITLE_LANGUAGES)
    if "all" in normalized_values:
        return [ALL_SUBTITLE_LANGUAGES]
    return normalized_values


def language_matches(target_languages: list[str], track_languages: list[str]) -> bool:
    target_aliases = set().union(*(expand_language_aliases(language) for language in target_languages))
    normalized_track_languages = {
        normalized
        for normalized in (normalize_language(value) for value in track_languages)
        if normalized
    }
    return not target_aliases.isdisjoint(normalized_track_languages)


def subtitle_language_matches(target_languages: list[str], track_languages: list[str]) -> bool:
    if target_languages == [ALL_SUBTITLE_LANGUAGES]:
        return True
    return language_matches(target_languages, track_languages)


def sanitize_token(value: str, default: str = "na") -> str:
    normalized = value.strip().lower().replace("-", "_")
    normalized = re.sub(r"[^a-z0-9_]+", "", normalized)
    return normalized or default


def normalize_media_info_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


def is_missing_media_info_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized in {"", "n/a", "na", "unknown"}
    return False


def get_media_info_value(track: dict | None, *keys: str) -> object | None:
    if not track:
        return None

    normalized_track = {
        normalize_media_info_key(str(key)): value
        for key, value in track.items()
    }
    for key in keys:
        value = track.get(key)
        if not is_missing_media_info_value(value):
            return value

        normalized_value = normalized_track.get(normalize_media_info_key(key))
        if not is_missing_media_info_value(normalized_value):
            return normalized_value
    return None


def canonicalize_codec(value: object) -> str:
    normalized = sanitize_token(str(value))
    if normalized.startswith("a_aac"):
        return "aac"
    if normalized.startswith("subrip") or normalized in {"srt", "stextutf8", "stext_utf8", "text_utf8", "utf_8"}:
        return "subrip"
    return CODEC_CANONICAL_ALIASES.get(normalized, normalized)


def normalize_movie_name(file_path: Path) -> str:
    raw_name = file_path.stem.strip().strip("\"'")
    normalized = re.sub(r"[\\/:*?\"<>|]+", " ", raw_name)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or file_path.stem


def detect_extension(codec: str, track_type: str) -> str:
    normalized_codec = canonicalize_codec(codec)
    return CODEC_EXTENSION_MAP.get(normalized_codec, TRACK_TYPE_EXTENSION_DEFAULTS[track_type])


def infer_codec(track: dict, media_info_track: dict | None) -> str:
    candidates = [
        get_media_info_value(media_info_track, "format", "Format"),
        get_media_info_value(media_info_track, "codec_id_hint", "CodecID/Hint", "CodecID", "codec_id"),
        track.get("codec"),
        track.get("properties", {}).get("codec_id"),
    ]
    fallback_codec = "unknown"
    for candidate in candidates:
        if candidate:
            normalized = canonicalize_codec(candidate)
            if normalized != "na":
                if normalized in CODEC_EXTENSION_MAP:
                    return normalized
                if fallback_codec == "unknown":
                    fallback_codec = normalized
    return fallback_codec


def find_media_info_track(
    media_info_by_type: dict[str, list[dict]],
    mkvmerge_track: dict,
    track_type: str,
    index: int,
) -> dict | None:
    tracks = media_info_by_type.get(track_type, [])
    properties = mkvmerge_track.get("properties", {})
    uid = properties.get("uid")
    if uid is not None:
        for track in tracks:
            if str(get_media_info_value(track, "unique_id", "UniqueID")) == str(uid):
                return track

    track_number = properties.get("number")
    if track_number is not None:
        for track in tracks:
            if str(get_media_info_value(track, "id", "ID")) == str(track_number):
                return track

    if index >= len(tracks):
        return None
    return tracks[index]


def get_media_info_track_id(media_info_track: dict | None, mkvmerge_track: dict) -> str:
    media_info_id = get_media_info_value(media_info_track, "id", "ID")
    if media_info_id is not None:
        return str(media_info_id)

    track_number = mkvmerge_track.get("properties", {}).get("number")
    if track_number is not None:
        return str(track_number)

    raise UploaderError(f"Cannot find MediaInfo ID for container track {mkvmerge_track.get('id')}")


def media_info_has_unique_id(media_info_payload: dict) -> bool:
    for track in media_info_payload.get("media", {}).get("track", []):
        if str(track.get("@type", "")).lower() == "general":
            unique_id = get_media_info_value(track, "unique_id", "UniqueID")
            if not is_missing_media_info_value(unique_id):
                return True
    return False


def collect_media_info(file_path: Path) -> tuple[dict[str, list[dict]], dict, str, dict]:
    media_info_payload = run_json_command(["mediainfo", "--Output=JSON", str(file_path)])
    media_info_text = run_text_command(["mediainfo", str(file_path)])
    media_info_payload, media_info_text = sanitize_media_info_paths(
        media_info_payload, media_info_text, file_path
    )
    mkvmerge_payload = run_json_command(["mkvmerge", "-J", str(file_path)])

    tracks_by_type: dict[str, list[dict]] = {"video": [], "audio": [], "text": []}
    for track in media_info_payload.get("media", {}).get("track", []):
        track_type = str(track.get("@type", "")).lower()
        if track_type in tracks_by_type:
            tracks_by_type[track_type].append(track)

    return tracks_by_type, media_info_payload, media_info_text, mkvmerge_payload


def build_prepared_tracks(
    file_path: Path,
    output_dir: Path,
    target_languages_by_type: dict[str, list[str]],
) -> list[PreparedTrack]:
    media_info_by_type, media_info_payload, media_info_text, mkvmerge_payload = collect_media_info(file_path)
    movie_name = normalize_movie_name(file_path)

    type_indices = {"audio": 0, "subtitles": 0}
    prepared_tracks: list[PreparedTrack] = []

    for track in mkvmerge_payload.get("tracks", []):
        track_type = track.get("type")
        if track_type not in {"audio", "subtitles"}:
            continue

        properties = track.get("properties", {})
        candidate_languages = [
            str(properties.get("language") or ""),
            str(properties.get("language_ietf") or ""),
            str(properties.get("track_name") or ""),
        ]
        target_languages = target_languages_by_type[track_type]
        matches_language = (
            language_matches(target_languages, candidate_languages)
            if track_type == "audio"
            else subtitle_language_matches(target_languages, candidate_languages)
        )
        if not matches_language:
            type_indices[track_type] += 1
            continue

        media_info_track = find_media_info_track(
            media_info_by_type,
            track,
            "audio" if track_type == "audio" else "text",
            type_indices[track_type],
        )
        type_indices[track_type] += 1

        fallback_language = "und" if target_languages == [ALL_SUBTITLE_LANGUAGES] else target_languages[0]
        language = normalize_language(properties.get("language_ietf") or properties.get("language") or fallback_language)
        codec = infer_codec(track, media_info_track)
        extension = detect_extension(codec, track_type)
        media_info_track_id = get_media_info_track_id(media_info_track, track)
        output_path = output_dir / f"{movie_name}_track{media_info_track_id}.{extension}"
        prepared_tracks.append(
            PreparedTrack(
                extraction_track_id=int(track["id"]),
                media_info_track_id=media_info_track_id,
                track_type=track_type,
                language=language or sanitize_token(fallback_language),
                original_video_mediainfo=media_info_payload,
                original_video_mediainfo_text=media_info_text,
                output_path=output_path,
            )
        )

    return prepared_tracks


def discover_mkv_files(input_path: Path) -> list[Path]:
    if not input_path.exists():
        raise UploaderError(f"Input path does not exist: {input_path}")
    if input_path.is_file():
        if input_path.suffix.lower() != ".mkv":
            raise UploaderError(f"Input file must be an .mkv file: {input_path}")
        return [input_path]
    return sorted(
        path
        for path in input_path.rglob("*")
        if path.is_file() and path.suffix.lower() == ".mkv"
    )


def file_extension(file_path: Path) -> str:
    return file_path.suffix.lower().lstrip(".")


def standalone_track_type(file_path: Path) -> str | None:
    extension = file_extension(file_path)
    if extension in STANDALONE_AUDIO_EXTENSIONS:
        return "audio"
    if extension in STANDALONE_SUBTITLE_EXTENSIONS:
        return "subtitles"
    return None


def is_standalone_file(file_path: Path) -> bool:
    return standalone_track_type(file_path) is not None


def discover_standalone_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if is_standalone_file(input_path) else []
    return sorted(
        path
        for path in input_path.rglob("*")
        if path.is_file() and is_standalone_file(path)
    )


def guess_language_from_filename(file_path: Path) -> str:
    """Best-effort language from file-name tags, e.g.
    ``Movie.uk.DUBTITLE.subtitles.srt`` -> ``uk``, ``Movie.en-GB.srt`` -> ``en``,
    or ``Movie_track2_[ukr]_DELAY 0ms.eac3`` -> ``uk`` (mkvextract/DVDFab naming).

    Only the trailing dot-separated tag components are inspected (the leading
    component is the title), and only short language codes (<= 3 chars, e.g.
    uk/en/eng/ukr) count, so descriptor words (subtitles, closedcaptions) and
    dotted title words (The.Italian.Job) do not produce false positives.

    Bracketed tags are matched anywhere in the stem, since a bracketed short code
    is unambiguously a tag. Bare underscore-separated tokens are deliberately NOT
    scanned: a film named "It" would make ``It_track2.eac3`` read as Italian.
    """
    stem = file_path.stem
    components = stem.split(".")
    tag_components = components[1:] if len(components) > 1 else []
    for component in reversed(tag_components):
        for token in re.split(r"[^A-Za-z0-9]+", component):
            normalized = normalize_language(token)
            if not normalized or len(normalized) > 3:
                continue
            canonical = LANGUAGE_TOKEN_LOOKUP.get(normalized)
            if canonical:
                return canonical
    for token in reversed(re.findall(r"\[([A-Za-z]{2,3})\]", stem)):
        canonical = LANGUAGE_TOKEN_LOOKUP.get(normalize_language(token))
        if canonical:
            return canonical
    return ""


def detect_standalone_language(media_info_track: dict | None, file_path: Path) -> str:
    media_info_language = get_media_info_value(media_info_track, "language", "Language")
    if media_info_language:
        normalized = normalize_language(str(media_info_language))
        if normalized:
            return normalized
    return guess_language_from_filename(file_path)


def collect_standalone_media_info(file_path: Path) -> tuple[dict, str, dict[str, list[dict]]]:
    media_info_payload = run_json_command(["mediainfo", "--Output=JSON", str(file_path)])
    media_info_text = run_text_command(["mediainfo", str(file_path)])
    media_info_payload, media_info_text = sanitize_media_info_paths(
        media_info_payload, media_info_text, file_path
    )
    tracks_by_type: dict[str, list[dict]] = {"video": [], "audio": [], "text": []}
    for track in media_info_payload.get("media", {}).get("track", []):
        track_type = str(track.get("@type", "")).lower()
        if track_type in tracks_by_type:
            tracks_by_type[track_type].append(track)
    return media_info_payload, media_info_text, tracks_by_type


def find_source_video(standalone_path: Path) -> Path | None:
    """Find the sibling video whose MediaInfo describes this standalone track.

    Downloaders name loose tracks after their video, e.g. ``Movie.mkv`` next to
    ``Movie.en.srt`` or ``Movie_track2_[und].aac``. The video's stem is therefore
    a prefix of the standalone file's stem. The uploader endpoint needs the
    source video's General ``unique_id``, so a matching video must be found.
    """
    directory = standalone_path.parent
    stem_lower = standalone_path.stem.lower()
    matches: list[Path] = []
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return None
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix.lower() not in VIDEO_CONTAINER_EXTENSIONS:
            continue
        candidate_stem = candidate.stem.lower()
        if not candidate_stem:
            continue
        remainder = stem_lower[len(candidate_stem):]
        if stem_lower == candidate_stem or (
            stem_lower.startswith(candidate_stem) and remainder[:1] in {".", "_", "-", " ", "["}
        ):
            matches.append(candidate)
    if not matches:
        return None
    # Prefer MKV (reliably carries a container unique_id), then the longest
    # (most specific) matching stem.
    matches.sort(key=lambda path: (path.suffix.lower() == ".mkv", len(path.stem)), reverse=True)
    return matches[0]


def choose_container_track_id(
    mkvmerge_payload: dict,
    media_info_by_type: dict[str, list[dict]],
    track_type: str,
    language: str,
) -> str | None:
    """Find a track_id_inside_container in the source video for a standalone track.

    The endpoint reads the language of this track from the supplied MediaInfo, so
    the chosen container track must be the same type and resolve to the standalone
    file's language. Returns None when the source video has no such track.
    """
    media_info_type = "audio" if track_type == "audio" else "text"
    type_index = 0
    for track in mkvmerge_payload.get("tracks", []):
        container_type = track.get("type")
        if container_type not in {"audio", "subtitles"}:
            continue
        index = type_index if container_type == track_type else None
        if container_type == track_type:
            type_index += 1
        if index is None:
            continue
        media_info_track = find_media_info_track(media_info_by_type, track, media_info_type, index)
        media_info_language = get_media_info_value(media_info_track, "language", "Language")
        properties = track.get("properties", {})
        candidate_languages = [
            str(media_info_language or ""),
            str(properties.get("language") or ""),
            str(properties.get("language_ietf") or ""),
            str(properties.get("track_name") or ""),
        ]
        if not media_info_language or not language_matches([language], candidate_languages):
            continue
        try:
            return get_media_info_track_id(media_info_track, track)
        except UploaderError:
            continue
    return None


def add_synthetic_media_info_track(
    media_info_payload: dict,
    own_media_info_track: dict | None,
    track_type: str,
    language: str,
) -> str:
    """Append the standalone file's own MediaInfo track to the source video's
    MediaInfo, and return the container track id the endpoint should read it by.

    Mutates ``media_info_payload`` (the dict sent as ``original_video_mediainfo``).
    """
    tracks = media_info_payload.setdefault("media", {}).setdefault("track", [])
    used_ids = set()
    for track in tracks:
        value = str(get_media_info_value(track, "id", "ID") or "")
        if value.isdigit():
            used_ids.add(int(value))
    new_id = max(used_ids, default=0) + 1

    synthetic = dict(own_media_info_track or {})
    synthetic.update(
        {
            "@type": "Text" if track_type == "subtitles" else "Audio",
            "ID": str(new_id),
            "StreamOrder": str(new_id - 1),
            "Language": language,
        }
    )
    # A loose file's MediaInfo carries no container-level track UID, and reusing
    # one would collide with a real track in the container.
    synthetic.pop("UniqueID", None)
    tracks.append(synthetic)
    return str(new_id)


def build_standalone_track(
    file_path: Path,
    target_languages_by_type: dict[str, list[str]],
) -> PreparedTrack:
    track_type = standalone_track_type(file_path)
    if track_type is None:
        raise StandaloneSkip(f"unsupported extension {file_path.suffix}")

    _, _, own_tracks_by_type = collect_standalone_media_info(file_path)
    media_info_type = "audio" if track_type == "audio" else "text"
    own_media_info_track = (
        own_tracks_by_type[media_info_type][0] if own_tracks_by_type[media_info_type] else None
    )
    detected_language = detect_standalone_language(own_media_info_track, file_path)
    target_languages = target_languages_by_type[track_type]

    # The endpoint requires a parseable track language, so a file whose language
    # cannot be determined (from MediaInfo or its name) cannot be uploaded.
    if not detected_language:
        raise StandaloneSkip(
            "could not determine a track language (required by the uploader endpoint); "
            "name the file with a language tag such as .uk or .en"
        )

    if track_type == "audio":
        matches_language = language_matches(target_languages, [detected_language])
    else:
        matches_language = target_languages == [ALL_SUBTITLE_LANGUAGES] or (
            subtitle_language_matches(target_languages, [detected_language])
        )
    if not matches_language:
        requested = "all" if target_languages == [ALL_SUBTITLE_LANGUAGES] else ", ".join(target_languages)
        raise StandaloneSkip(f"language {detected_language!r} not in requested {requested}")

    source_video = find_source_video(file_path)
    if source_video is None:
        raise StandaloneSkip(
            "no sibling video found to supply the source MediaInfo unique_id "
            "required by the uploader endpoint"
        )

    media_info_by_type, video_payload, video_text, mkvmerge_payload = collect_media_info(source_video)
    if not media_info_has_unique_id(video_payload):
        raise StandaloneSkip(
            f"source video {source_video.name} has no MediaInfo unique_id "
            "(e.g. an MP4 container); cannot attach a standalone track to it"
        )
    language = normalize_language(detected_language) or "und"

    media_info_track_id = choose_container_track_id(
        mkvmerge_payload, media_info_by_type, track_type, detected_language
    )
    if media_info_track_id is None:
        # An external track (e.g. a loose .srt for a video with no embedded
        # subtitle track) has no container track to point at, and the endpoint
        # can only read a track's type and language from the MediaInfo we send,
        # keyed by track_id_inside_container. So describe the loose file as a
        # track of the source video, using its own MediaInfo. The General
        # unique_id stays that of the real source video, so the upload still
        # lands on the correct release.
        # ponytail: the payload then describes a track the container does not
        # physically contain. Drop this once the endpoint accepts an explicit
        # track type + language for standalone files.
        media_info_track_id = add_synthetic_media_info_track(
            video_payload, own_media_info_track, track_type, language
        )

    return PreparedTrack(
        extraction_track_id=None,
        media_info_track_id=media_info_track_id,
        track_type=track_type,
        language=language,
        original_video_mediainfo=video_payload,
        original_video_mediainfo_text=video_text,
        output_path=file_path,
        cleanup_after_upload=False,
    )


def extract_tracks(file_path: Path, prepared_tracks: list[PreparedTrack]) -> None:
    extractable = [track for track in prepared_tracks if track.extraction_track_id is not None]
    if not extractable:
        return
    command = ["mkvextract", "tracks", str(file_path)]
    for prepared_track in extractable:
        prepared_track.output_path.parent.mkdir(parents=True, exist_ok=True)
        command.append(f"{prepared_track.extraction_track_id}:{prepared_track.output_path}")
    run_command(command)


def get_file_hash(file_path: Path) -> str:
    digest = blake3.blake3()
    with file_path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_target(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path or "/"


def hash_check_url(api_url: str) -> str:
    parsed = urlsplit(api_url)
    return urlunsplit(parsed._replace(path=f"{parsed.path.rstrip('/')}/hash-check"))


def log_request(
    method: str,
    url: str,
    explanation: str,
    *,
    verbose: bool,
    level: str = "INFO",
) -> None:
    log_event(
        level,
        "request",
        f"{method.upper()} {request_target(url)}",
        explanation,
        verbose=verbose,
    )


def warn_hash_check_prevented_upload(file_path: Path) -> None:
    log_event(
        "WARNING",
        file_path.name,
        "upload",
        "file will not be uploaded because the hash check did not pass",
    )


def is_track_already_published(api_url: str, api_key: str, file_path: Path, *, verbose: bool) -> bool:
    file_hash = get_file_hash(file_path)
    check_url = hash_check_url(api_url)
    request_payload = {
        "file_hash": file_hash,
        "file_hash_algorithm": "blake3-256",
    }
    log_request("POST", check_url, "sending request", verbose=verbose)
    try:
        response = httpx.post(
            check_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=request_payload,
            timeout=60.0,
        )
        response.raise_for_status()
        log_request("POST", check_url, f"response {response.status_code}", verbose=verbose)
    except httpx.HTTPStatusError as exc:
        log_request(
            "POST", check_url, f"exception: HTTP {exc.response.status_code}",
            verbose=True, level="ERROR",
        )
        warn_hash_check_prevented_upload(file_path)
        raise UploaderError(
            f"Hash check failed for {file_path.name}: HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        log_request("POST", check_url, f"exception: {exc}", verbose=True, level="ERROR")
        warn_hash_check_prevented_upload(file_path)
        raise UploaderError(f"Hash check failed for {file_path.name}: {exc}") from exc

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        warn_hash_check_prevented_upload(file_path)
        raise UploaderError(f"Hash check response was not valid JSON for {file_path.name}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("exists"), bool):
        warn_hash_check_prevented_upload(file_path)
        raise UploaderError(f"Hash check response did not contain a boolean 'exists' value for {file_path.name}")
    return payload["exists"]


def upload_prepared_track(
    api_url: str,
    api_key: str,
    prepared_track: PreparedTrack,
    visibility: str,
    *,
    verbose: bool,
) -> dict:
    if not prepared_track.output_path.is_file():
        raise UploaderError(f"Cannot upload missing extracted file: {prepared_track.output_path}")

    file_size = prepared_track.output_path.stat().st_size
    progress_file: ProgressFile | None = None
    log_request("POST", api_url, "sending request", verbose=verbose)
    try:
        with prepared_track.output_path.open("rb") as media_file:
            progress_file = ProgressFile(media_file, file_size, prepared_track.output_path.name)
            response = httpx.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                data={
                    "original_video_mediainfo": json.dumps(prepared_track.original_video_mediainfo),
                    "original_video_mediainfo_text": prepared_track.original_video_mediainfo_text,
                    "track_id_inside_container": prepared_track.media_info_track_id,
                    "visibility": visibility,
                },
                files={"media_file": (prepared_track.output_path.name, progress_file)},
                timeout=60.0 * 10,
            )
            progress_file.finish()
            response.raise_for_status()
            log_request("POST", api_url, f"response {response.status_code}", verbose=verbose)
    except httpx.HTTPStatusError as exc:
        if progress_file is not None:
            progress_file.close_line()
        log_request(
            "POST", api_url, f"exception: HTTP {exc.response.status_code}",
            verbose=True, level="ERROR",
        )
        raise UploaderError(
            f"Upload failed for {prepared_track.output_path.name}: HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        if progress_file is not None:
            progress_file.close_line()
        log_request("POST", api_url, f"exception: {exc}", verbose=True, level="ERROR")
        raise UploaderError(f"Upload failed for {prepared_track.output_path.name}: {exc}") from exc

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise UploaderError(f"Upload response was not valid JSON for {prepared_track.output_path.name}") from exc


def format_table(headers: list[str], rows: list[list[object]]) -> str:
    string_rows = [[str(value) for value in row] for row in rows]
    widths = [
        max(len(header), *(len(row[index]) for row in string_rows)) if string_rows else len(header)
        for index, header in enumerate(headers)
    ]
    separator = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    header_line = "| " + " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)) + " |"
    body_lines = [
        "| " + " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)) + " |"
        for row in string_rows
    ]
    return "\n".join([separator, header_line, separator, *body_lines, separator])


def clean_log_field(value: object) -> str:
    return " ".join(str(value).replace("|", "/").splitlines())


def format_log_line(level: str, target: str, action: str, explanation: str) -> str:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    return (
        f"{timestamp} | {clean_log_field(level).upper()} | {clean_log_field(target)} | "
        f"{clean_log_field(action)} | {clean_log_field(explanation)}"
    )


def log_event(
    level: str,
    target: str,
    action: str,
    explanation: str,
    *,
    verbose: bool = True,
) -> None:
    if verbose:
        print(format_log_line(level, target, action, explanation))


def log_table(
    title: str,
    headers: list[str],
    rows: list[list[object]],
    *,
    verbose: bool = True,
    target: str = "summary",
    action: str = "report",
) -> None:
    if verbose and rows:
        log_event("INFO", target, action, title)
        print(format_table(headers, rows))


def get_default_output_dir() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def remove_extracted_file(prepared_track: PreparedTrack) -> None:
    try:
        prepared_track.output_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UploaderError(f"Uploaded {prepared_track.output_path.name} but failed to remove it: {exc}") from exc


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # line_buffering: Docker sets PYTHONUNBUFFERED, but a direct
            # `python -m uploader > log` block-buffers stdout, so our progress
            # lands in the log long after the mkvextract output it describes.
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    target_languages_by_type = {
        "audio": parse_language_filters(args.audio_languages, DEFAULT_AUDIO_LANGUAGES),
        "subtitles": parse_subtitle_language_filters(args.subtitle_languages),
    }

    if not input_path.exists():
        raise UploaderError(f"Input path does not exist: {input_path}")

    standalone_files: list[Path] = []
    if input_path.is_file():
        if input_path.suffix.lower() == ".mkv":
            mkv_files = [input_path]
        elif args.standalone and is_standalone_file(input_path):
            mkv_files = []
            standalone_files = [input_path]
        else:
            raise UploaderError(
                f"Unsupported input file: {input_path}. Expected an .mkv file"
                + ("" if args.standalone else " (standalone uploads are disabled)")
                + "."
            )
    else:
        mkv_files = discover_mkv_files(input_path)
        if args.standalone:
            standalone_files = discover_standalone_files(input_path)

    if not mkv_files and not standalone_files:
        raise UploaderError(
            f"No .mkv"
            + (" or standalone audio/subtitle" if args.standalone else "")
            + f" files found at: {input_path}"
        )

    log_event(
        "INFO",
        "input",
        "detect",
        f"found {len(mkv_files)} MKV file(s) and {len(standalone_files)} standalone file(s)",
    )
    log_table(
        "Detected MKV files:",
        ["full path"],
        [[file_path] for file_path in mkv_files],
        verbose=args.verbose,
        target="MKV files",
        action="detect",
    )
    log_table(
        "Detected standalone files:",
        ["full path"],
        [[file_path] for file_path in standalone_files],
        verbose=args.verbose,
        target="standalone files",
        action="detect",
    )

    extracted_count = 0
    uploaded_count = 0
    skipped_count = 0
    failed_files: list[tuple[Path, str]] = []
    for file_path in mkv_files:
        try:
            log_event("INFO", file_path.name, "inspect", str(file_path), verbose=args.verbose)
            prepared_tracks = build_prepared_tracks(file_path, output_dir, target_languages_by_type)
            if not prepared_tracks:
                subtitle_filter_label = (
                    "all"
                    if target_languages_by_type["subtitles"] == [ALL_SUBTITLE_LANGUAGES]
                    else ", ".join(target_languages_by_type["subtitles"])
                )
                log_event(
                    "WARNING",
                    file_path.name,
                    "extract",
                    f"skipped: no matching audio tracks for {', '.join(target_languages_by_type['audio'])} "
                    f"or subtitle tracks for {subtitle_filter_label} found",
                )
                continue
            log_event(
                "INFO", file_path.name, "extract",
                f"extracting {len(prepared_tracks)} track(s)",
            )
            extract_tracks(file_path, prepared_tracks)
            extracted_count += len(prepared_tracks)
            log_table(
                f"Extracted tracks for {file_path.name}:",
                ["mediainfo_id", "type", "language", "path"],
                [
                    [
                        prepared_track.media_info_track_id,
                        prepared_track.track_type,
                        prepared_track.language,
                        prepared_track.output_path,
                    ]
                    for prepared_track in prepared_tracks
                ],
                target=file_path.name,
                action="extract",
            )
            for prepared_track in prepared_tracks:
                if is_track_already_published(
                    args.api_url,
                    args.api_key,
                    prepared_track.output_path,
                    verbose=args.verbose,
                ):
                    skipped_count += 1
                    log_event(
                        "WARNING", prepared_track.output_path.name, "upload",
                        "file will not be uploaded because the hash check reports it is already published",
                    )
                    if not args.keep_extracted and prepared_track.cleanup_after_upload:
                        remove_extracted_file(prepared_track)
                        log_event(
                            "INFO", prepared_track.output_path.name, "cleanup",
                            f"removed {prepared_track.output_path}", verbose=args.verbose,
                        )
                    continue
                upload_response = upload_prepared_track(
                    args.api_url,
                    args.api_key,
                    prepared_track,
                    args.visibility,
                    verbose=args.verbose,
                )
                uploaded_count += 1
                log_event(
                    "INFO", prepared_track.output_path.name, "upload",
                    f"uploaded as {args.visibility} track {upload_response.get('id')}",
                    verbose=args.verbose,
                )
                if not args.keep_extracted and prepared_track.cleanup_after_upload:
                    remove_extracted_file(prepared_track)
                    log_event(
                        "INFO", prepared_track.output_path.name, "cleanup",
                        f"removed {prepared_track.output_path}", verbose=args.verbose,
                    )
        except (UploaderError, subprocess.CalledProcessError, OSError, ValueError) as exc:
            failed_files.append((file_path, str(exc)))
            log_event("ERROR", file_path.name, "process", f"skipped: {exc}")
            continue

    standalone_uploaded_count = 0
    standalone_skipped_count = 0
    for file_path in standalone_files:
        try:
            log_event("INFO", file_path.name, "inspect", str(file_path), verbose=args.verbose)
            prepared_track = build_standalone_track(file_path, target_languages_by_type)
            if is_track_already_published(
                args.api_url,
                args.api_key,
                prepared_track.output_path,
                verbose=args.verbose,
            ):
                skipped_count += 1
                standalone_skipped_count += 1
                log_event(
                    "WARNING", file_path.name, "upload",
                    "file will not be uploaded because the hash check reports it is already published",
                )
                continue
            log_event(
                "INFO", file_path.name, "upload",
                f"uploading standalone {prepared_track.track_type}; language={prepared_track.language}",
                verbose=args.verbose,
            )
            upload_response = upload_prepared_track(
                args.api_url,
                args.api_key,
                prepared_track,
                args.visibility,
                verbose=args.verbose,
            )
            uploaded_count += 1
            standalone_uploaded_count += 1
            log_event(
                "INFO", file_path.name, "upload",
                f"uploaded as {args.visibility} track {upload_response.get('id')}",
                verbose=args.verbose,
            )
        except StandaloneSkip as skip:
            standalone_skipped_count += 1
            log_event("WARNING", file_path.name, "upload", f"standalone file will not be uploaded: {skip}")
            continue
        except (UploaderError, subprocess.CalledProcessError, OSError, ValueError) as exc:
            failed_files.append((file_path, str(exc)))
            log_event("ERROR", file_path.name, "process", f"skipped: {exc}")
            continue

    log_event(
        "INFO", "run", "summary",
        f"prepared {extracted_count} extracted track file(s); "
        f"uploaded {uploaded_count} {args.visibility} track(s) "
        f"({standalone_uploaded_count} standalone, {standalone_skipped_count} standalone skipped); "
        f"skipped {skipped_count} already published track(s)",
        verbose=args.verbose,
    )
    if failed_files:
        log_event("ERROR", "run", "summary", f"encountered errors on {len(failed_files)} file(s)")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UploaderError, subprocess.CalledProcessError, OSError, ValueError) as exc:
        log_event("ERROR", "run", "startup", str(exc))
        raise SystemExit(1) from exc
