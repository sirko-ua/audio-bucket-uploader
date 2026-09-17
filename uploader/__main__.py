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

FONT_ATTACHMENT_CONTENT_TYPES = {
    # RFC 8081 media types recommended by Matroska.
    "font/collection",
    "font/otf",
    "font/sfnt",
    "font/ttf",
    "font/woff",
    "font/woff2",
    # Legacy media types found in older Matroska files.
    "application/font-sfnt",
    "application/font-woff",
    "application/vnd.ms-opentype",
    "application/x-font-ttf",
    "application/x-truetype-font",
}
FONT_ATTACHMENT_EXTENSIONS = {".otf", ".ttc", ".ttf", ".woff", ".woff2"}
GENERIC_ATTACHMENT_CONTENT_TYPES = {"", "application/octet-stream"}

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
    # MediaInfo's ID is the public track_id_inside_container used by the API.
    # It is not the same namespace as mkvextract's (mkvmerge) track ID.
    media_info_track_id: int
    track_type: str
    language: str
    original_video_mediainfo: dict
    original_video_mediainfo_text: str
    output_path: Path
    cleanup_after_upload: bool = True


@dataclass
class PreparedAttachment:
    extraction_attachment_id: int
    uid: int
    content_type: str
    original_file_name: str
    original_video_mediainfo: dict
    original_video_mediainfo_text: str
    output_path: Path


@dataclass(frozen=True)
class OriginalVideoCheck:
    exists: bool
    track_ids_inside_container: frozenset[int]
    attachment_original_filenames: frozenset[str]
    attachment_uids: frozenset[str]


class ProgressFile:
    def __init__(self, file_obj, total_size: int, label: str) -> None:
        self._file_obj = file_obj
        self._total_size = max(total_size, 1)
        self._label = label
        self._uploaded = 0
        self._last_percent = -1
        self._last_log_bucket = 0
        self._interactive = sys.stdout.isatty()
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
        if not self._interactive:
            bucket = 4 if percent >= 100 else percent // 25
            if bucket == 0 or bucket <= self._last_log_bucket:
                return
            self._last_log_bucket = bucket
        uploaded_mb = self._uploaded / (1024 * 1024)
        total_mb = self._total_size / (1024 * 1024)
        message = f"{percent:3d}% {uploaded_mb:.1f}/{total_mb:.1f} MiB"
        prefix = "\r" if self._interactive else ""
        suffix = "" if self._interactive else "\n"
        sys.stdout.write(
            f"{prefix}{format_log_line('INFO', self._label, 'upload', message)}{suffix}"
        )
        sys.stdout.flush()
        self._line_open = self._interactive

    def finish(self) -> None:
        self._uploaded = self._total_size
        self._render()
        if self._line_open:
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


class ExtractionProgress:
    def __init__(self, label: str) -> None:
        self._label = label
        self._last_percent = -1
        self._last_log_bucket = 0
        self._interactive = sys.stdout.isatty()
        self._line_open = False

    def update(self, percent: int) -> None:
        percent = max(0, min(percent, 100))
        if percent == self._last_percent:
            return
        self._last_percent = percent
        if not self._interactive:
            bucket = 4 if percent >= 100 else percent // 25
            if (
                bucket == 0
                or bucket <= self._last_log_bucket
                or (percent < 100 and percent % 25 != 0)
            ):
                return
            self._last_log_bucket = bucket
        prefix = "\r" if self._interactive else ""
        suffix = "" if self._interactive else "\n"
        sys.stdout.write(
            f"{prefix}{format_log_line('INFO', self._label, 'extract', f'{percent:3d}%')}{suffix}"
        )
        sys.stdout.flush()
        self._line_open = self._interactive

    def finish(self) -> None:
        if self._last_percent < 100:
            self.update(100)
        if self._line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._line_open = False

    def close_line(self) -> None:
        if self._line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._line_open = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract and upload target-language audio/subtitle tracks and embedded "
            "font attachments from MKV files."
        ),
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
        help=f"Directory where extracted tracks and font attachments will be written. Defaults to the system temp directory ({get_default_output_dir()}).",
    )
    parser.add_argument(
        "--keep-extracted",
        action="store_true",
        help="Keep extracted tracks and font attachments after successful upload instead of deleting them.",
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
            "a matching sibling video is used when available; otherwise the standalone "
            "file's own MediaInfo is sent. Files without a determinable language are "
            "skipped. Standalone source files are never deleted. Defaults to true; use "
            "--no-standalone to disable."
        ),
    )
    parser.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_VERBOSE,
        help=(
            "Print file lists, extracted-media tables, HTTP status, mkvextract "
            "details, and cleanup paths. Defaults to false."
        ),
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


def run_command(
    command: list[str],
    *,
    progress_target: str,
    verbose: bool,
) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdout is None:
        raise UploaderError("Could not read mkvextract output")

    progress = ExtractionProgress(progress_target)
    messages: list[str] = []
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue

        progress_match = re.fullmatch(r"#GUI#progress\s+(\d+)%", line)
        if progress_match:
            progress.update(int(progress_match.group(1)))
            continue

        progress.close_line()
        gui_message = re.fullmatch(r"#GUI#(warning|error)\s+(.*)", line)
        if gui_message:
            message = gui_message.group(2)
            messages.append(message)
            if gui_message.group(1) == "warning":
                log_event("WARNING", progress_target, "extract", f"warning={message}")
            continue

        messages.append(line)
        log_event(
            "INFO",
            progress_target,
            "extract-detail",
            line,
            verbose=verbose,
        )

    return_code = process.wait()
    if return_code == 0:
        progress.finish()
        return

    progress.close_line()
    detail = messages[-1] if messages else "no error details"
    raise UploaderError(f"mkvextract failed code={return_code} error={detail}")


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


def safe_attachment_file_name(value: object, attachment_id: int) -> str:
    """Return a filename only, never a path supplied by the MKV attachment."""
    raw_name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    sanitized = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", raw_name)
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
    return sanitized or f"attachment{attachment_id}.bin"


def is_font_attachment(attachment: dict) -> bool:
    content_type = str(attachment.get("content_type") or "").strip().lower()
    if content_type in FONT_ATTACHMENT_CONTENT_TYPES:
        return True
    if content_type not in GENERIC_ATTACHMENT_CONTENT_TYPES:
        return False
    suffix = Path(str(attachment.get("file_name") or "")).suffix.lower()
    return suffix in FONT_ATTACHMENT_EXTENSIONS


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

    # MediaInfo's StreamOrder identifies the stream used by mkvmerge/mkvextract.
    # This is deliberately only used to find the matching MediaInfo record; the
    # ID sent to the server is read from that record below.
    extraction_track_id = mkvmerge_track.get("id")
    if extraction_track_id is not None:
        for track in tracks:
            stream_order = parse_media_info_track_id(
                get_media_info_value(track, "stream_order", "StreamOrder")
            )
            if stream_order == int(extraction_track_id):
                return track

    track_number = properties.get("number")
    if track_number is not None:
        for track in tracks:
            media_info_id = parse_media_info_track_id(
                get_media_info_value(track, "id", "ID")
            )
            if media_info_id == int(track_number):
                return track

    if index >= len(tracks):
        return None
    return tracks[index]


def parse_media_info_track_id(value: object | None) -> int | None:
    """Parse MediaInfo's numeric ID without confusing it with stream order."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None

    # Some MediaInfo formats include the hexadecimal rendering after the
    # decimal ID (for example ``2 (0x2)``).
    match = re.fullmatch(r"\s*(\d+)(?:\s+\(0x[0-9a-fA-F]+\))?\s*", str(value))
    return int(match.group(1)) if match else None


def get_media_info_track_id(media_info_track: dict | None, mkvmerge_track: dict) -> int:
    media_info_id = get_media_info_value(media_info_track, "id", "ID")
    parsed_id = parse_media_info_track_id(media_info_id)
    if parsed_id is not None:
        return parsed_id

    raise UploaderError(
        f"Cannot find numeric MediaInfo ID for mkvextract track "
        f"{mkvmerge_track.get('id')}"
    )


def get_original_video_unique_id(media_info_payload: dict) -> str:
    for track in media_info_payload.get("media", {}).get("track", []):
        if str(track.get("@type", "")).lower() != "general":
            continue
        unique_id = get_media_info_value(track, "unique_id", "UniqueID")
        if not is_missing_media_info_value(unique_id):
            normalized = str(unique_id).strip()
            if normalized:
                return normalized
    raise UploaderError("Cannot check original video: MediaInfo unique_id is missing")


def media_info_has_unique_id(media_info_payload: dict) -> bool:
    try:
        get_original_video_unique_id(media_info_payload)
    except UploaderError:
        return False
    return True


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


def build_prepared_tracks_from_metadata(
    file_path: Path,
    output_dir: Path,
    target_languages_by_type: dict[str, list[str]],
    media_info_by_type: dict[str, list[dict]],
    media_info_payload: dict,
    media_info_text: str,
    mkvmerge_payload: dict,
) -> list[PreparedTrack]:
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


def build_prepared_attachments_from_metadata(
    file_path: Path,
    output_dir: Path,
    media_info_payload: dict,
    media_info_text: str,
    mkvmerge_payload: dict,
) -> list[PreparedAttachment]:
    movie_name = normalize_movie_name(file_path)
    prepared_attachments: list[PreparedAttachment] = []

    for attachment in mkvmerge_payload.get("attachments", []):
        if not is_font_attachment(attachment):
            continue

        attachment_id = attachment.get("id")
        uid = attachment.get("properties", {}).get("uid")
        if attachment_id is None:
            raise UploaderError(f"Font attachment has no extraction ID in {file_path.name}")
        if uid is None:
            raise UploaderError(
                f"Font attachment {attachment_id} has no UID in {file_path.name}"
            )

        container_file_name = str(attachment.get("file_name") or "")
        safe_file_name = safe_attachment_file_name(
            container_file_name, int(attachment_id)
        )
        original_file_name = (
            f"{movie_name}_attachment{int(attachment_id)}_{safe_file_name}"
        )
        output_path = output_dir / original_file_name
        prepared_attachments.append(
            PreparedAttachment(
                extraction_attachment_id=int(attachment_id),
                uid=int(uid),
                content_type=str(attachment.get("content_type") or ""),
                original_file_name=original_file_name,
                original_video_mediainfo=media_info_payload,
                original_video_mediainfo_text=media_info_text,
                output_path=output_path,
            )
        )

    return prepared_attachments


def build_prepared_media(
    file_path: Path,
    output_dir: Path,
    target_languages_by_type: dict[str, list[str]],
) -> tuple[list[PreparedTrack], list[PreparedAttachment]]:
    media_info_by_type, media_info_payload, media_info_text, mkvmerge_payload = collect_media_info(
        file_path
    )
    prepared_tracks = build_prepared_tracks_from_metadata(
        file_path,
        output_dir,
        target_languages_by_type,
        media_info_by_type,
        media_info_payload,
        media_info_text,
        mkvmerge_payload,
    )
    prepared_attachments = build_prepared_attachments_from_metadata(
        file_path,
        output_dir,
        media_info_payload,
        media_info_text,
        mkvmerge_payload,
    )
    return prepared_tracks, prepared_attachments


def build_prepared_tracks(
    file_path: Path,
    output_dir: Path,
    target_languages_by_type: dict[str, list[str]],
) -> list[PreparedTrack]:
    """Compatibility wrapper for callers that only need container tracks."""
    prepared_tracks, _ = build_prepared_media(
        file_path, output_dir, target_languages_by_type
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
) -> int | None:
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
) -> int:
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
    return new_id


def standalone_media_info_track_id(
    media_info_payload: dict,
    own_media_info_track: dict | None,
    track_type: str,
    language: str,
) -> int:
    """Return the standalone track's MediaInfo ID, creating one if needed."""
    media_info_id = parse_media_info_track_id(
        get_media_info_value(own_media_info_track, "id", "ID")
    )
    if media_info_id is not None:
        return media_info_id
    return add_synthetic_media_info_track(
        media_info_payload, own_media_info_track, track_type, language
    )


def build_standalone_track(
    file_path: Path,
    target_languages_by_type: dict[str, list[str]],
) -> PreparedTrack:
    track_type = standalone_track_type(file_path)
    if track_type is None:
        raise StandaloneSkip(f"unsupported extension {file_path.suffix}")

    own_payload, own_text, own_tracks_by_type = collect_standalone_media_info(file_path)
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
    language = normalize_language(detected_language) or "und"

    if source_video is not None:
        media_info_by_type, video_payload, video_text, mkvmerge_payload = collect_media_info(
            source_video
        )
        if media_info_has_unique_id(video_payload):
            media_info_track_id = choose_container_track_id(
                mkvmerge_payload, media_info_by_type, track_type, detected_language
            )
            if media_info_track_id is None:
                # An external track (e.g. a loose .srt for a video with no
                # embedded subtitle track) is represented in the source
                # video's MediaInfo so the endpoint can read its type and
                # language by track_id_inside_container.
                media_info_track_id = add_synthetic_media_info_track(
                    video_payload, own_media_info_track, track_type, language
                )
        else:
            # A sibling container without a usable unique_id cannot identify
            # the upload. Treat the loose file as its own source instead.
            video_payload = own_payload
            video_text = own_text
            media_info_track_id = standalone_media_info_track_id(
                video_payload, own_media_info_track, track_type, language
            )
    else:
        # A standalone upload does not require its original movie to be
        # present locally. Its own MediaInfo describes the uploaded track.
        video_payload = own_payload
        video_text = own_text
        media_info_track_id = standalone_media_info_track_id(
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


def extract_media(
    file_path: Path,
    prepared_tracks: list[PreparedTrack],
    prepared_attachments: list[PreparedAttachment],
    *,
    verbose: bool = False,
) -> None:
    extractable = [track for track in prepared_tracks if track.extraction_track_id is not None]
    if not extractable and not prepared_attachments:
        return

    # Source-first syntax allows multiple extraction modes in one invocation,
    # so MKVToolNix can extract tracks and attachments in a single pass.
    command = ["mkvextract", "--gui-mode", str(file_path)]
    if extractable:
        command.append("tracks")
    for prepared_track in extractable:
        prepared_track.output_path.parent.mkdir(parents=True, exist_ok=True)
        command.append(f"{prepared_track.extraction_track_id}:{prepared_track.output_path}")

    if prepared_attachments:
        command.append("attachments")
    for prepared_attachment in prepared_attachments:
        prepared_attachment.output_path.parent.mkdir(parents=True, exist_ok=True)
        command.append(
            f"{prepared_attachment.extraction_attachment_id}:{prepared_attachment.output_path}"
        )
    run_command(command, progress_target=file_path.name, verbose=verbose)


def extract_tracks(file_path: Path, prepared_tracks: list[PreparedTrack]) -> None:
    """Compatibility wrapper for callers that only extract tracks."""
    extract_media(file_path, prepared_tracks, [])


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


def original_video_check_url(api_url: str) -> str:
    parsed = urlsplit(api_url)
    return urlunsplit(
        parsed._replace(path=f"{parsed.path.rstrip('/')}/original-video-check")
    )


def attachments_upload_url(api_url: str) -> str:
    parsed = urlsplit(api_url)
    return urlunsplit(parsed._replace(path=f"{parsed.path.rstrip('/')}/attachments"))


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
        f"{method.upper()} {request_target(url)}",
        "request",
        explanation,
        verbose=verbose,
    )


def check_original_video(
    api_url: str,
    api_key: str,
    unique_id: str,
    *,
    verbose: bool,
) -> OriginalVideoCheck:
    unique_id = unique_id.strip()
    if not unique_id:
        raise UploaderError("Cannot check original video: unique_id is empty")

    check_url = original_video_check_url(api_url)
    try:
        response = httpx.post(
            check_url,
            headers={"X-API-Key": api_key},
            json={"unique_id": unique_id},
            timeout=60.0,
        )
        response.raise_for_status()
        log_request("POST", check_url, f"status={response.status_code}", verbose=verbose)
    except httpx.HTTPStatusError as exc:
        log_request(
            "POST",
            check_url,
            f"status={exc.response.status_code}",
            verbose=verbose,
            level="ERROR",
        )
        raise UploaderError(
            f"original-video-check failed status={exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        log_request(
            "POST", check_url, f"error={exc}", verbose=verbose, level="ERROR"
        )
        raise UploaderError(f"original-video-check failed error={exc}") from exc

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise UploaderError("original-video-check failed error=invalid-json") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("exists"), bool):
        raise UploaderError("original-video-check failed error=missing-exists")

    track_ids = payload.get("track_ids_inside_container")
    attachment_names = payload.get("attachment_original_filenames")
    attachment_uids = payload.get("attachment_uids", [])
    if not isinstance(track_ids, list) or any(
        isinstance(track_id, bool) or not isinstance(track_id, int)
        for track_id in track_ids
    ):
        raise UploaderError(
            "original-video-check failed error=invalid-track-ids-inside-container"
        )
    if not isinstance(attachment_names, list) or any(
        not isinstance(file_name, str) for file_name in attachment_names
    ):
        raise UploaderError(
            "original-video-check failed error=invalid-attachment-original-filenames"
        )
    if not isinstance(attachment_uids, list) or any(
        not isinstance(uid, str) for uid in attachment_uids
    ):
        raise UploaderError(
            "original-video-check failed error=invalid-attachment-uids"
        )

    return OriginalVideoCheck(
        exists=payload["exists"],
        track_ids_inside_container=frozenset(track_ids),
        attachment_original_filenames=frozenset(attachment_names),
        attachment_uids=frozenset(attachment_uids),
    )


def filter_missing_media(
    prepared_tracks: list[PreparedTrack],
    prepared_attachments: list[PreparedAttachment],
    original_video: OriginalVideoCheck,
) -> tuple[list[PreparedTrack], list[PreparedAttachment]]:
    if not original_video.exists:
        return prepared_tracks, prepared_attachments

    missing_tracks = [
        track
        for track in prepared_tracks
        if track.media_info_track_id
        not in original_video.track_ids_inside_container
    ]
    missing_attachments = [
        attachment
        for attachment in prepared_attachments
        if str(attachment.uid) not in original_video.attachment_uids
        and attachment.original_file_name
        not in original_video.attachment_original_filenames
    ]
    return missing_tracks, missing_attachments


def find_track_by_hash(
    api_url: str,
    api_key: str,
    file_path: Path,
    *,
    verbose: bool,
) -> dict:
    file_hash = get_file_hash(file_path)
    check_url = hash_check_url(api_url)
    request_payload = {
        "file_hash": file_hash,
        "file_hash_algorithm": "blake3-256",
    }
    try:
        response = httpx.post(
            check_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=request_payload,
            timeout=60.0,
        )
        response.raise_for_status()
        log_request("POST", check_url, f"status={response.status_code}", verbose=verbose)
    except httpx.HTTPStatusError as exc:
        log_request(
            "POST",
            check_url,
            f"status={exc.response.status_code}",
            verbose=verbose,
            level="ERROR",
        )
        raise UploaderError(
            f"hash-check failed file={file_path.name} status={exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        log_request(
            "POST", check_url, f"error={exc}", verbose=verbose, level="ERROR"
        )
        raise UploaderError(f"hash-check failed file={file_path.name} error={exc}") from exc

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise UploaderError(
            f"hash-check failed file={file_path.name} error=invalid-json"
        ) from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("exists"), bool):
        raise UploaderError(
            f"hash-check failed file={file_path.name} error=missing-exists"
        )
    return payload


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
    try:
        with prepared_track.output_path.open("rb") as media_file:
            progress_file = ProgressFile(media_file, file_size, prepared_track.output_path.name)
            response = httpx.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                data={
                    "original_video_mediainfo": json.dumps(prepared_track.original_video_mediainfo),
                    "original_video_mediainfo_text": prepared_track.original_video_mediainfo_text,
                    "track_id_inside_container": str(prepared_track.media_info_track_id),
                    "visibility": visibility,
                },
                files={"media_file": (prepared_track.output_path.name, progress_file)},
                timeout=60.0 * 10,
            )
            progress_file.finish()
            response.raise_for_status()
            log_request("POST", api_url, f"status={response.status_code}", verbose=verbose)
    except httpx.HTTPStatusError as exc:
        if progress_file is not None:
            progress_file.close_line()
        log_request(
            "POST",
            api_url,
            f"status={exc.response.status_code}",
            verbose=verbose,
            level="ERROR",
        )
        raise UploaderError(
            f"upload failed file={prepared_track.output_path.name} "
            f"status={exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        if progress_file is not None:
            progress_file.close_line()
        log_request(
            "POST", api_url, f"error={exc}", verbose=verbose, level="ERROR"
        )
        raise UploaderError(
            f"upload failed file={prepared_track.output_path.name} error={exc}"
        ) from exc

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise UploaderError(
            f"upload failed file={prepared_track.output_path.name} error=invalid-json"
        ) from exc


def upload_prepared_attachment(
    api_url: str,
    api_key: str,
    prepared_attachment: PreparedAttachment,
    *,
    verbose: bool,
) -> dict:
    if not prepared_attachment.output_path.is_file():
        raise UploaderError(
            f"Cannot upload missing extracted attachment: {prepared_attachment.output_path}"
        )

    upload_url = attachments_upload_url(api_url)
    file_size = prepared_attachment.output_path.stat().st_size
    progress_file: ProgressFile | None = None
    try:
        with prepared_attachment.output_path.open("rb") as media_file:
            progress_file = ProgressFile(
                media_file, file_size, prepared_attachment.output_path.name
            )
            response = httpx.post(
                upload_url,
                headers={"Authorization": f"Bearer {api_key}"},
                data={
                    "original_video_mediainfo": json.dumps(
                        prepared_attachment.original_video_mediainfo
                    ),
                    "original_video_mediainfo_text": (
                        prepared_attachment.original_video_mediainfo_text
                    ),
                    "original_filename": prepared_attachment.original_file_name,
                    "uid": str(prepared_attachment.uid),
                },
                files={
                    "media_file": (
                        prepared_attachment.output_path.name,
                        progress_file,
                    )
                },
                timeout=60.0 * 10,
            )
            progress_file.finish()
            response.raise_for_status()
            log_request(
                "POST", upload_url, f"status={response.status_code}", verbose=verbose
            )
    except httpx.HTTPStatusError as exc:
        if progress_file is not None:
            progress_file.close_line()
        log_request(
            "POST",
            upload_url,
            f"status={exc.response.status_code}",
            verbose=verbose,
            level="ERROR",
        )
        raise UploaderError(
            f"attachment upload failed file={prepared_attachment.output_path.name} "
            f"uid={prepared_attachment.uid} status={exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        if progress_file is not None:
            progress_file.close_line()
        log_request(
            "POST", upload_url, f"error={exc}", verbose=verbose, level="ERROR"
        )
        raise UploaderError(
            f"attachment upload failed file={prepared_attachment.output_path.name} "
            f"uid={prepared_attachment.uid} error={exc}"
        ) from exc

    if not response.content:
        return {}
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise UploaderError(
            f"attachment upload failed file={prepared_attachment.output_path.name} "
            f"uid={prepared_attachment.uid} error=invalid-json"
        ) from exc
    return payload if isinstance(payload, dict) else {}


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


def remove_extracted_file(
    prepared_media: PreparedTrack | PreparedAttachment,
) -> None:
    try:
        prepared_media.output_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UploaderError(
            f"Uploaded {prepared_media.output_path.name} but failed to remove it: {exc}"
        ) from exc


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
        f"mkv={len(mkv_files)} standalone={len(standalone_files)}",
    )
    log_table(
        "mkv-files",
        ["path"],
        [[file_path] for file_path in mkv_files],
        verbose=args.verbose,
        target="input",
        action="detect",
    )
    log_table(
        "standalone-files",
        ["path"],
        [[file_path] for file_path in standalone_files],
        verbose=args.verbose,
        target="input",
        action="detect",
    )

    extracted_count = 0
    extracted_attachment_count = 0
    uploaded_count = 0
    reused_track_count = 0
    uploaded_attachment_count = 0
    skipped_count = 0
    existing_attachment_count = 0
    failed_files: list[tuple[Path, str]] = []
    for file_path in mkv_files:
        try:
            log_event(
                "INFO",
                file_path.name,
                "inspect",
                f"path={file_path}",
                verbose=args.verbose,
            )
            prepared_tracks, prepared_attachments = build_prepared_media(
                file_path, output_dir, target_languages_by_type
            )
            if not prepared_tracks and not prepared_attachments:
                subtitle_filter_label = (
                    "all"
                    if target_languages_by_type["subtitles"] == [ALL_SUBTITLE_LANGUAGES]
                    else ",".join(target_languages_by_type["subtitles"])
                )
                log_event(
                    "WARNING",
                    file_path.name,
                    "skip",
                    f"reason=no-matching-media "
                    f"audio={','.join(target_languages_by_type['audio'])} "
                    f"subtitles={subtitle_filter_label} attachments=0",
                )
                continue

            media_info_payload = (
                prepared_tracks[0].original_video_mediainfo
                if prepared_tracks
                else prepared_attachments[0].original_video_mediainfo
            )
            unique_id = get_original_video_unique_id(media_info_payload)
            original_video = check_original_video(
                args.api_url,
                args.api_key,
                unique_id,
                verbose=args.verbose,
            )
            candidate_track_count = len(prepared_tracks)
            candidate_attachment_count = len(prepared_attachments)
            prepared_tracks, prepared_attachments = filter_missing_media(
                prepared_tracks,
                prepared_attachments,
                original_video,
            )
            present_track_count = candidate_track_count - len(prepared_tracks)
            present_attachment_count = (
                candidate_attachment_count - len(prepared_attachments)
            )
            skipped_count += present_track_count
            existing_attachment_count += present_attachment_count
            log_event(
                "INFO",
                file_path.name,
                "original-video-check",
                f"exists={str(original_video.exists).lower()} "
                f"tracks_missing={len(prepared_tracks)} "
                f"tracks_present={present_track_count} "
                f"attachments_missing={len(prepared_attachments)} "
                f"attachments_present={present_attachment_count}",
            )
            if not prepared_tracks and not prepared_attachments:
                log_event(
                    "WARNING",
                    file_path.name,
                    "skip",
                    "reason=no-missing-media",
                )
                continue
            log_event(
                "INFO", file_path.name, "extract",
                f"tracks={len(prepared_tracks)} attachments={len(prepared_attachments)}",
            )
            extract_media(
                file_path,
                prepared_tracks,
                prepared_attachments,
                verbose=args.verbose,
            )
            extracted_count += len(prepared_tracks)
            extracted_attachment_count += len(prepared_attachments)
            log_table(
                "tracks",
                ["id", "type", "language", "path"],
                [
                    [
                        prepared_track.media_info_track_id,
                        prepared_track.track_type,
                        prepared_track.language,
                        prepared_track.output_path,
                    ]
                    for prepared_track in prepared_tracks
                ],
                verbose=args.verbose,
                target=file_path.name,
                action="extract",
            )
            log_table(
                "attachments",
                ["uid", "mime", "original_name", "path"],
                [
                    [
                        prepared_attachment.uid,
                        prepared_attachment.content_type,
                        prepared_attachment.original_file_name,
                        prepared_attachment.output_path,
                    ]
                    for prepared_attachment in prepared_attachments
                ],
                verbose=args.verbose,
                target=file_path.name,
                action="extract",
            )
            for prepared_track in prepared_tracks:
                hash_match = find_track_by_hash(
                    args.api_url,
                    args.api_key,
                    prepared_track.output_path,
                    verbose=args.verbose,
                )
                if hash_match["exists"]:
                    log_event(
                        "INFO",
                        prepared_track.output_path.name,
                        "hash-check",
                        f"candidate_track_id={hash_match.get('track_id')} "
                        "action=submit-for-scoped-check",
                        verbose=args.verbose,
                    )
                upload_response = upload_prepared_track(
                    args.api_url,
                    args.api_key,
                    prepared_track,
                    args.visibility,
                    verbose=args.verbose,
                )
                reused_existing = upload_response.get("reused_existing") is True
                if reused_existing:
                    reused_track_count += 1
                else:
                    uploaded_count += 1
                response_id = upload_response.get("id")
                id_detail = f" id={response_id}" if response_id is not None else ""
                reuse_detail = " reused_existing=true" if reused_existing else ""
                log_event(
                    "INFO",
                    prepared_track.output_path.name,
                    "upload",
                    f"kind=track type={prepared_track.track_type} "
                    f"visibility={args.visibility}{id_detail}{reuse_detail}",
                )
                if not args.keep_extracted and prepared_track.cleanup_after_upload:
                    remove_extracted_file(prepared_track)
                    log_event(
                        "INFO", prepared_track.output_path.name, "cleanup",
                        f"path={prepared_track.output_path}",
                        verbose=args.verbose,
                    )
            for prepared_attachment in prepared_attachments:
                upload_response = upload_prepared_attachment(
                    args.api_url,
                    args.api_key,
                    prepared_attachment,
                    verbose=args.verbose,
                )
                uploaded_attachment_count += 1
                response_id = upload_response.get("id")
                id_detail = f" id={response_id}" if response_id is not None else ""
                log_event(
                    "INFO",
                    prepared_attachment.output_path.name,
                    "upload",
                    f"kind=attachment uid={prepared_attachment.uid}{id_detail}",
                )
                if not args.keep_extracted:
                    remove_extracted_file(prepared_attachment)
                    log_event(
                        "INFO",
                        prepared_attachment.output_path.name,
                        "cleanup",
                        f"path={prepared_attachment.output_path}",
                        verbose=args.verbose,
                    )
        except (UploaderError, subprocess.CalledProcessError, OSError, ValueError) as exc:
            failed_files.append((file_path, str(exc)))
            log_event("ERROR", file_path.name, "process", f"status=failed error={exc}")
            continue

    standalone_uploaded_count = 0
    standalone_skipped_count = 0
    for file_path in standalone_files:
        try:
            log_event(
                "INFO",
                file_path.name,
                "inspect",
                f"path={file_path}",
                verbose=args.verbose,
            )
            prepared_track = build_standalone_track(file_path, target_languages_by_type)
            hash_match = find_track_by_hash(
                args.api_url,
                args.api_key,
                prepared_track.output_path,
                verbose=args.verbose,
            )
            if hash_match["exists"]:
                log_event(
                    "INFO",
                    file_path.name,
                    "hash-check",
                    f"candidate_track_id={hash_match.get('track_id')} "
                    "action=submit-for-scoped-check",
                    verbose=args.verbose,
                )
            log_event(
                "INFO", file_path.name, "upload",
                f"kind=standalone type={prepared_track.track_type} "
                f"language={prepared_track.language}",
                verbose=args.verbose,
            )
            upload_response = upload_prepared_track(
                args.api_url,
                args.api_key,
                prepared_track,
                args.visibility,
                verbose=args.verbose,
            )
            reused_existing = upload_response.get("reused_existing") is True
            if reused_existing:
                reused_track_count += 1
            else:
                uploaded_count += 1
                standalone_uploaded_count += 1
            response_id = upload_response.get("id")
            id_detail = f" id={response_id}" if response_id is not None else ""
            reuse_detail = " reused_existing=true" if reused_existing else ""
            log_event(
                "INFO",
                file_path.name,
                "upload",
                f"kind=standalone type={prepared_track.track_type} "
                f"visibility={args.visibility}{id_detail}{reuse_detail}",
            )
        except StandaloneSkip as skip:
            standalone_skipped_count += 1
            log_event("WARNING", file_path.name, "skip", f"reason={skip}")
            continue
        except (UploaderError, subprocess.CalledProcessError, OSError, ValueError) as exc:
            failed_files.append((file_path, str(exc)))
            log_event("ERROR", file_path.name, "process", f"status=failed error={exc}")
            continue

    log_event(
        "ERROR" if failed_files else "INFO",
        "run",
        "summary",
        f"tracks_extracted={extracted_count} "
        f"attachments_extracted={extracted_attachment_count} "
        f"tracks_uploaded={uploaded_count} "
        f"tracks_reused={reused_track_count} "
        f"attachments_uploaded={uploaded_attachment_count} "
        f"tracks_already_present={skipped_count} "
        f"attachments_already_present={existing_attachment_count} "
        f"standalone_uploaded={standalone_uploaded_count} "
        f"standalone_skipped={standalone_skipped_count} "
        f"failed={len(failed_files)}",
    )
    if failed_files:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UploaderError, subprocess.CalledProcessError, OSError, ValueError) as exc:
        log_event("ERROR", "run", "startup", f"status=failed error={exc}")
        raise SystemExit(1) from exc
