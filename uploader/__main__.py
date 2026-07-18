from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import blake3
import httpx

from .history import FILE_ITEM, GIVEN_UP, MAX_ATTEMPTS, History, file_fingerprint
from .logs import format_log_line, log_event, log_table


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

# mediainfo/mkvmerge only read headers, so they are quick; a probe that runs
# this long is hung on a broken file and must not stall the whole run.
PROBE_TIMEOUT = 300.0
# A full parse reads every frame, so it is bounded separately: it only ever runs
# as the VFR fallback below, and a slow scan there must not look like a hang.
DEEP_PROBE_TIMEOUT = 900.0
# A spun-down disk, a NAS reconnect, or a file sonarr is still writing makes a
# perfectly good container fail one probe and pass the next. Without a retry that
# blip burns one of MAX_ATTEMPTS, and three unlucky runs give up on the file for
# good.
PROBE_ATTEMPTS = 3
PROBE_RETRY_DELAY = 5.0
# ponytail: one hour per mkvextract call. Raise it if a legitimate extraction of
# a very large track on very slow storage ever trips it.
EXTRACT_TIMEOUT = 3600.0

# The one "the server, not this file, is the problem" exit code. 2 is taken by
# argparse for usage errors.
SERVER_DOWN_EXIT_CODE = 3

HTTP_ATTEMPTS = 5
HTTP_RETRY_BASE_DELAY = 2.0
HTTP_RETRY_MAX_DELAY = 60.0
# A bad key is never one file's answer: nothing will ever succeed, so stop
# instead of marking every file failed.
FATAL_HTTP_STATUSES = {401}
# Only fatal before the first success, where they prove a misconfigured URL or
# key. After a success they are this file's answer ("no release for this
# unique_id") — a normal 4xx that must not stop the run.
FATAL_BEFORE_FIRST_SUCCESS_STATUSES = {403, 404}
# A connection that never gets an answer means the host is gone: stop quickly.
MAX_CONSECUTIVE_UNREACHABLE = 3
# A 5xx is different in kind: the server answered, so it is up and reachable —
# it choked on this payload. A season the endpoint cannot digest is a run of
# per-file rejections, not an outage, and must not stop a whole library. Kept
# finite so a genuinely broken endpoint (its database down, every upload 500)
# still stops the run instead of grinding through every file.
MAX_CONSECUTIVE_SERVER_ERRORS = 12

STANDALONE_AUDIO_EXTENSIONS = {
    "wav", "mp3", "aac", "flac", "ogg", "m4a", "opus",
    "ac3", "eac3", "ac4", "dts", "dtshd", "truehd", "mlp", "thd",
}
STANDALONE_SUBTITLE_EXTENSIONS = {"ass", "srt", "pgs", "sup"}

VIDEO_CONTAINER_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".webm", ".avi", ".mov", ".ts", ".m2ts", ".mpg", ".mpeg",
}

LANGUAGE_TOKEN_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in LANGUAGE_ALIASES.items():
    LANGUAGE_TOKEN_LOOKUP[_canonical] = _canonical
    for _alias in _aliases:
        LANGUAGE_TOKEN_LOOKUP[_alias] = _canonical


class UploaderError(RuntimeError):
    """A problem with one file or one track. The run continues."""


class MissingTool(UploaderError):
    """mediainfo/mkvmerge/mkvextract is not installed. Unlike every other probe
    failure this cannot fix itself, so it must never be retried: doing so would
    add a pointless wait to every file in the library."""


class ServerDown(RuntimeError):
    """The server is unreachable or rejecting everything. The run stops."""


class AlreadyPublished(Exception):
    """A retried upload turned out to have landed after all (the first attempt
    reached the server, only its answer did not reach us)."""


class StandaloneSkip(Exception):
    """Raised when a standalone file is intentionally skipped (not an error).

    ``durable`` skips depend only on the file itself and the language filters,
    so they are remembered. A skip caused by the file's surroundings (no sibling
    video yet) is re-evaluated on every run, because the user may add the missing
    video without touching the standalone file.
    """

    def __init__(self, message: str, *, durable: bool = True) -> None:
        super().__init__(message)
        self.durable = durable


@dataclass
class Stats:
    extracted: int = 0
    uploaded: int = 0
    standalone_uploaded: int = 0
    duplicates: int = 0
    skipped: int = 0
    resumed: int = 0
    failed: int = 0
    given_up: int = 0  # failed in earlier runs often enough that we stopped retrying


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

    def _write(self, text: str) -> None:
        # This object IS the request body httpx reads from, so an OSError here
        # (a full disk under the log, `python -m uploader | head`) would fail the
        # upload itself. A progress bar must never be able to do that.
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except OSError:
            pass

    def _render(self) -> None:
        percent = min(int(self._uploaded * 100 / self._total_size), 100)
        if percent == self._last_percent:
            return
        self._last_percent = percent
        uploaded_mb = self._uploaded / (1024 * 1024)
        total_mb = self._total_size / (1024 * 1024)
        message = f"{percent:3d}% ({uploaded_mb:.1f}/{total_mb:.1f} MiB)"
        self._write(f"\r{format_log_line('INFO', self._label, 'upload', message)}")
        self._line_open = True

    def finish(self) -> None:
        self._uploaded = self._total_size
        self._render()
        self._write("\n")
        self._line_open = False

    def close_line(self) -> None:
        if self._line_open:
            self._write("\n")
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
        "--state-dir",
        default=str(get_default_state_dir()),
        help=(
            "Directory holding the run history (history.sqlite3) and the failure log "
            "(failures.jsonl). Files already extracted, hash-checked, or uploaded in an "
            f"earlier run are skipped without touching the server. Defaults to {get_default_state_dir()} "
            "(override with the UPLOADER_STATE_DIR environment variable). Mount it as a volume "
            "when running in Docker, otherwise the history dies with the container."
        ),
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Forget earlier failures before starting, so files that were given up on "
            f"(after {MAX_ATTEMPTS} failed attempts) are tried again. Use it once the "
            "cause is fixed. To reprocess everything from scratch, delete the state "
            "directory instead."
        ),
    )
    parser.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_VERBOSE,
        help="Print detailed detection, HTTP request, upload result, and cleanup output. Defaults to false.",
    )
    return parser.parse_args()


def tail(text: str | None, limit: int = 300) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return "..." + collapsed[-limit:]


def run_capture(command: list[str], timeout: float = PROBE_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a tool and capture everything it says.

    Never raises on a non-zero exit: mkvmerge and mkvextract exit 1 for mere
    warnings while still producing perfectly good output, so the caller judges
    the result, not the exit code. errors="replace" keeps a tool that emits
    non-UTF-8 bytes (a cp1252 track name) from crashing the run.
    """
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MissingTool(f"{command[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise UploaderError(f"{command[0]} timed out after {timeout:.0f}s") from exc


def tool_diagnostics(completed: subprocess.CompletedProcess) -> str:
    """The mkvtoolnix tools print their errors on stdout, not stderr, so a
    failure reason has to be looked for in both."""
    return tail(f"{completed.stderr} {completed.stdout}".strip()) or "no output"


def run_json_command(command: list[str], timeout: float = PROBE_TIMEOUT) -> dict:
    completed = run_capture(command, timeout=timeout)
    if completed.returncode > 1:  # 1 means "warnings" for the mkvtoolnix tools
        raise UploaderError(
            f"{command[0]} failed (exit {completed.returncode}): {tool_diagnostics(completed)}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UploaderError(
            f"{command[0]} did not return valid JSON (exit {completed.returncode}): "
            f"{tool_diagnostics(completed)}"
        ) from exc
    if not isinstance(payload, dict):
        raise UploaderError(f"{command[0]} returned JSON that is not an object")
    return payload


def run_text_command(command: list[str], timeout: float = PROBE_TIMEOUT) -> str:
    completed = run_capture(command, timeout=timeout)
    if not completed.stdout.strip():
        raise UploaderError(
            f"{command[0]} returned no output (exit {completed.returncode}): "
            f"{tool_diagnostics(completed)}"
        )
    return completed.stdout


def run_extract_command(command: list[str]) -> None:
    """mkvextract: 0 = success, 1 = success with warnings, 2 = error."""
    completed = run_capture(command, timeout=EXTRACT_TIMEOUT)
    if completed.returncode > 1:
        raise UploaderError(
            f"mkvextract failed (exit {completed.returncode}): {tool_diagnostics(completed)}"
        )


def retry_probe(label: str, probe):
    """Run a probe, retrying the kind of failure that fixes itself.

    Reading a container is not a pure function of the container: a disk that has
    spun down, a NAS that drops a connection, or a file the downloader is still
    writing all make a healthy file fail one probe and pass the next. Those
    arrive as the same UploaderError as genuine damage, and the difference only
    shows on a second look.

    Retrying costs seconds. Not retrying costs the file permanently: every failed
    probe consumes one of MAX_ATTEMPTS, so three unlucky moments across three runs
    give up on a file that was never broken.
    """
    for attempt in range(1, PROBE_ATTEMPTS + 1):
        try:
            return probe()
        except MissingTool:
            raise
        except UploaderError as exc:
            if attempt == PROBE_ATTEMPTS:
                raise
            log_event(
                "WARNING", label, "detect",
                f"{exc}; retrying in {PROBE_RETRY_DELAY:.0f}s "
                f"(attempt {attempt}/{PROBE_ATTEMPTS})",
            )
            time.sleep(PROBE_RETRY_DELAY)
    raise AssertionError("unreachable")


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
    for track in (media_info_payload.get("media") or {}).get("track") or []:
        if str(track.get("@type", "")).lower() == "general":
            unique_id = get_media_info_value(track, "unique_id", "UniqueID")
            if not is_missing_media_info_value(unique_id):
                return True
    return False


def check_container_readable(file_path: Path, mkvmerge_payload: dict) -> None:
    """mkvmerge exits 0 on a file it cannot read, reporting it in the payload.
    Without this, an unreadable file looks exactly like a healthy file with no
    matching tracks: silently 'done', never retried, never in the log.

    A merely truncated MKV still parses, so it is not caught here; mkvextract
    reads whatever is there and the short track is uploaded as-is."""
    errors = mkvmerge_payload.get("errors") or []
    if errors:
        raise UploaderError(f"mkvmerge cannot read {file_path.name}: {tail('; '.join(map(str, errors)))}")
    container = mkvmerge_payload.get("container") or {}
    if not container.get("recognized", True):
        raise UploaderError(f"mkvmerge does not recognize {file_path.name} (damaged or not a container)")
    if not container.get("supported", True):
        raise UploaderError(f"mkvmerge does not support the container of {file_path.name}")


def video_track_of(media_info_payload: dict) -> dict | None:
    for track in (media_info_payload.get("media") or {}).get("track") or []:
        if str(track.get("@type", "")).lower() == "video":
            return track
    return None


def has_video_frame_rate(media_info_payload: dict) -> bool:
    return not is_missing_media_info_value(
        get_media_info_value(video_track_of(media_info_payload), "frame_rate", "FrameRate")
    )


def add_missing_video_frame_rate(
    file_path: Path, media_info_payload: dict, media_info_text: str
) -> tuple[dict, str]:
    """Recover a video frame rate that the container headers do not carry.

    A variable-frame-rate encode stores no frame rate to read, so a header-only
    probe reports ``FrameRate_Mode: VFR`` and no ``FrameRate`` at all. The
    uploader endpoint needs that FPS and rejects the file without it ("Could not
    parse original video FPS from MediaInfo") — which loses every track of every
    VFR-encoded file, a whole season at a time.

    A full parse walks the frame timestamps and computes the real average. That
    reads the entire file, so it runs only for the files that actually need it,
    and is best-effort: if it still yields nothing, the original payload goes out
    unchanged and the server keeps the final say.
    """
    if has_video_frame_rate(media_info_payload):
        return media_info_payload, media_info_text

    log_event(
        "INFO", file_path.name, "detect",
        "no frame rate in the container headers (variable frame rate); "
        "reparsing the file to compute it",
    )
    try:
        deep_payload = run_json_command(
            ["mediainfo", "--ParseSpeed=1.0", "--Output=JSON", str(file_path)],
            timeout=DEEP_PROBE_TIMEOUT,
        )
        deep_text = run_text_command(
            ["mediainfo", "--ParseSpeed=1.0", str(file_path)], timeout=DEEP_PROBE_TIMEOUT
        )
    except UploaderError as exc:
        log_event(
            "WARNING", file_path.name, "detect",
            f"could not compute the frame rate ({exc}); uploading without it",
        )
        return media_info_payload, media_info_text

    if not has_video_frame_rate(deep_payload):
        log_event(
            "WARNING", file_path.name, "detect",
            "a full parse found no frame rate either; uploading without it",
        )
        return media_info_payload, media_info_text

    frame_rate = get_media_info_value(video_track_of(deep_payload), "frame_rate", "FrameRate")
    log_event("INFO", file_path.name, "detect", f"computed frame rate {frame_rate}")
    return deep_payload, deep_text


def collect_media_info(file_path: Path) -> tuple[dict[str, list[dict]], dict, str, dict]:
    def probe() -> tuple[dict, str, dict]:
        payload = run_json_command(["mediainfo", "--Output=JSON", str(file_path)])
        text = run_text_command(["mediainfo", str(file_path)])
        mkvmerge = run_json_command(["mkvmerge", "-J", str(file_path)])
        # Inside the retry: an unreadable container is exactly the symptom a
        # storage blip produces, and it is reported in the payload rather than
        # raised by the command, so it has to be checked here to be retried.
        check_container_readable(file_path, mkvmerge)
        return payload, text, mkvmerge

    media_info_payload, media_info_text, mkvmerge_payload = retry_probe(file_path.name, probe)
    media_info_payload, media_info_text = add_missing_video_frame_rate(
        file_path, media_info_payload, media_info_text
    )

    tracks_by_type: dict[str, list[dict]] = {"video": [], "audio": [], "text": []}
    for track in (media_info_payload.get("media") or {}).get("track") or []:
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


def is_media_file(file_path: Path) -> bool:
    return file_path.suffix.lower() == ".mkv" or is_standalone_file(file_path)


def walk_files(input_path: Path) -> tuple[list[Path], int]:
    """Every media file under a directory, plus the number of directories that
    could not be read.

    Not rglob: rglob silently swallows a directory it may not read, and an empty
    result then looks exactly like a finished library.

    Symlinked and junctioned directories ARE followed: a library assembled from
    links to several disks (mergerfs, unRAID, a `D:\\Media` junction to
    `E:\\Movies`) is a normal layout, and skipping those — os.walk's default —
    silently uploads nothing at all.

    What makes that safe is that each directory is visited at most once per real
    path, so a link *cycle* stops the first time it leads back somewhere already
    walked, instead of yielding the same movie under dozens of paths, each a
    distinct history key, each re-extracted.
    """
    found: list[Path] = []
    visited: set[str] = set()
    unreadable = 0

    def on_error(exc: OSError) -> None:
        nonlocal unreadable
        unreadable += 1
        log_event("WARNING", getattr(exc, "filename", "input"), "detect", f"cannot read: {exc}")

    for directory, subdirectories, names in os.walk(
        input_path, onerror=on_error, followlinks=True
    ):
        real_directory = os.path.realpath(directory)
        if real_directory in visited:
            subdirectories[:] = []
            continue
        visited.add(real_directory)
        # Only media: a 500k-file library would otherwise retain a Path for every
        # cover, nfo and sample in it.
        found.extend(
            path for path in (Path(directory) / name for name in names) if is_media_file(path)
        )
    return found, unreadable


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
    media_info_payload, media_info_text = retry_probe(
        file_path.name,
        lambda: (
            run_json_command(["mediainfo", "--Output=JSON", str(file_path)]),
            run_text_command(["mediainfo", str(file_path)]),
        ),
    )
    tracks_by_type: dict[str, list[dict]] = {"video": [], "audio": [], "text": []}
    for track in (media_info_payload.get("media") or {}).get("track") or []:
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
        # Not durable: the user may drop the matching video in later.
        raise StandaloneSkip(
            "no sibling video found to supply the source MediaInfo unique_id "
            "required by the uploader endpoint",
            durable=False,
        )

    media_info_by_type, video_payload, video_text, mkvmerge_payload = collect_media_info(source_video)
    if not media_info_has_unique_id(video_payload):
        raise StandaloneSkip(
            f"source video {source_video.name} has no MediaInfo unique_id "
            "(e.g. an MP4 container); cannot attach a standalone track to it",
            durable=False,
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


def extract_command(file_path: Path, prepared_tracks: list[PreparedTrack]) -> list[str]:
    command = ["mkvextract", "tracks", str(file_path)]
    for prepared_track in prepared_tracks:
        prepared_track.output_path.parent.mkdir(parents=True, exist_ok=True)
        command.append(f"{prepared_track.extraction_track_id}:{prepared_track.output_path}")
    return command


def verify_extracted(prepared_track: PreparedTrack) -> None:
    if not prepared_track.output_path.is_file():
        raise UploaderError("mkvextract wrote no file")
    if prepared_track.output_path.stat().st_size == 0:
        raise UploaderError("mkvextract wrote an empty file")


def extract_tracks(
    file_path: Path, prepared_tracks: list[PreparedTrack]
) -> list[tuple[PreparedTrack, str]]:
    """Extract every track, returning the ones that could not be extracted.

    mkvextract takes all tracks in one call, but a single damaged track then
    sinks its healthy siblings, so a failed batch is retried track by track.
    """
    extractable = [track for track in prepared_tracks if track.extraction_track_id is not None]
    if not extractable:
        return []

    if len(extractable) > 1:
        try:
            run_extract_command(extract_command(file_path, extractable))
            for prepared_track in extractable:
                verify_extracted(prepared_track)
            return []
        except (UploaderError, OSError) as exc:
            log_event(
                "WARNING", file_path.name, "extract",
                f"extracting all tracks at once failed ({exc}); retrying track by track",
            )

    failures: list[tuple[PreparedTrack, str]] = []
    for prepared_track in extractable:
        try:
            run_extract_command(extract_command(file_path, [prepared_track]))
            verify_extracted(prepared_track)
        except (UploaderError, OSError) as exc:
            failures.append((prepared_track, str(exc)))
    return failures


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


# Consecutive server-side errors, counted PER ENDPOINT: every upload is preceded
# by a hash check, so a single shared counter would be reset by the healthy
# hash-check endpoint and a dead upload endpoint could never be detected.
# ponytail: module-level because the uploader is a single-threaded CLI; make it
# an object if it ever grows a worker pool.
_consecutive_server_errors: dict[str, int] = {}
_successful_requests = 0


def retry_after_seconds(exc: httpx.HTTPError) -> float | None:
    """The server's own instruction, if it gave one.

    Both forms are valid HTTP and both appear in the wild: a number of seconds,
    or an HTTP date. Reading only integers let `Retry-After: 3600.0` and the
    date form slip past the "back off longer than a retry can" stop.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    header = response.headers.get("retry-after", "").strip()
    if not header:
        return None
    try:
        return max(float(header), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


def note_server_error(url: str, label: str, reason: str, exc: Exception) -> Exception:
    """Give up on this request, and decide whether the server itself is the
    problem. Returns the exception to raise, so every caller stays one `raise`.

    One failure is this file's problem; several in a row against the same
    endpoint are the server's. That includes a connection that dies without an
    answer: a proxy resetting an oversized body looks exactly like an unreachable
    host, and treating the first one as "the server is down" would stop the run
    on a healthy server — and, because a stopped run records no failure, replay
    that same file on every restart, forever.

    How many in a row it takes depends on what came back. A 5xx response proves
    the server is alive and talking, so a streak of them is far more likely to be
    a folder of payloads it cannot digest than an outage; a streak of answers
    that never arrive at all is the host being gone.
    """
    server_errors = _consecutive_server_errors.get(url, 0) + 1
    _consecutive_server_errors[url] = server_errors
    limit = (
        MAX_CONSECUTIVE_SERVER_ERRORS
        if isinstance(exc, httpx.HTTPStatusError)
        else MAX_CONSECUTIVE_UNREACHABLE
    )
    if server_errors >= limit:
        return ServerDown(
            f"{request_target(url)} failed {server_errors} times in a row ({reason})"
        )
    return UploaderError(f"{label}: {reason}")


def send_with_retry(
    label: str,
    url: str,
    send,
    *,
    verbose: bool,
    already_done=None,
) -> httpx.Response:
    """Send a request, retrying anything that looks transient.

    A permanent per-file rejection (4xx) raises UploaderError so the run moves
    on to the next file. An unreachable server, or repeated server-side errors
    from one endpoint, raises ServerDown so the run stops instead of burning
    through the whole library.

    ``already_done`` is checked before re-sending: an upload whose answer never
    came back may still have landed, and re-sending it would publish it twice.
    """
    global _successful_requests

    delay = HTTP_RETRY_BASE_DELAY
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        log_request("POST", url, "sending request", verbose=verbose)
        try:
            response = send()
            response.raise_for_status()
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            reason = (
                f"HTTP {status} {tail(exc.response.text, 120)}".strip()
                if status is not None
                else f"{type(exc).__name__}: {exc}"
            )
            log_request("POST", url, f"exception: {reason}", verbose=True, level="ERROR")

            if status in FATAL_HTTP_STATUSES:
                raise ServerDown(f"server rejected the request: {reason} (check --api-key)") from exc
            if status in FATAL_BEFORE_FIRST_SUCCESS_STATUSES and _successful_requests == 0:
                raise ServerDown(
                    f"server rejected the very first request: {reason} (check --api-url and --api-key)"
                ) from exc
            if status is not None and status < 500 and status != 429:
                # This file's answer ("no release for this unique_id", a payload
                # the endpoint won't take), not a verdict on the run. Never stop
                # for it: a library legitimately produces long runs of these.
                raise UploaderError(f"{label}: {reason}") from exc

            wait = retry_after_seconds(exc)
            if wait is not None and wait > HTTP_RETRY_MAX_DELAY:
                # Waiting an hour inside a retry loop is not a retry, and
                # ignoring the instruction is how a client gets banned.
                raise ServerDown(
                    f"server asked us to back off for {wait:.0f}s ({reason}); stopping"
                ) from exc

            if attempt < HTTP_ATTEMPTS:
                # The body may have landed even though the answer never reached
                # us: a read timeout, a dropped connection, a 502/504 from a
                # proxy — but equally a 500, which the application emits and
                # which says nothing about whether its write already committed.
                # Ask before re-sending, or the track is published twice. Only a
                # 429 is exempt: the body was refused outright, and probing the
                # rate limiter again is the one thing it told us not to do.
                if already_done is not None and status != 429:
                    try:
                        if already_done():
                            raise AlreadyPublished(label) from exc
                    except UploaderError as check_exc:
                        # We cannot tell whether it landed, so we must not send
                        # it again: re-sending is what publishes it twice. Give
                        # up on this file, but keep the accounting honest, or a
                        # dead endpoint would never be detected.
                        log_event(
                            "WARNING", label, "retry",
                            f"cannot confirm whether it landed ({check_exc}); not re-sending",
                        )
                        raise note_server_error(
                            url, label,
                            f"{reason} (could not confirm whether it landed: {check_exc})",
                            exc,
                        ) from exc
                wait = delay if wait is None else max(wait, delay)
                log_event(
                    "WARNING", label, "retry",
                    f"{reason}; retrying in {wait:.0f}s (attempt {attempt}/{HTTP_ATTEMPTS})",
                )
                time.sleep(wait)
                delay = min(delay * 2, HTTP_RETRY_MAX_DELAY)
                continue

            raise note_server_error(
                url, label, f"{reason} after {attempt} attempt(s)", exc
            ) from exc
        else:
            _consecutive_server_errors[url] = 0
            _successful_requests += 1
            log_request("POST", url, f"response {response.status_code}", verbose=verbose)
            return response

    raise AssertionError("unreachable")


def response_json(label: str, response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise UploaderError(f"{label}: response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise UploaderError(f"{label}: response was not a JSON object")
    return payload


def is_track_already_published(
    api_url: str, api_key: str, file_path: Path, *, verbose: bool, file_hash: str | None = None
) -> bool:
    # The hash never changes while we hold the file, and a retried upload asks
    # again — hashing a multi-GB track once per attempt would be pure waste.
    file_hash = file_hash or get_file_hash(file_path)
    check_url = hash_check_url(api_url)
    label = f"hash check for {file_path.name}"

    response = send_with_retry(
        label,
        check_url,
        lambda: httpx.post(
            check_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"file_hash": file_hash, "file_hash_algorithm": "blake3-256"},
            timeout=60.0,
        ),
        verbose=verbose,
    )

    payload = response_json(label, response)
    if not isinstance(payload.get("exists"), bool):
        raise UploaderError(f"{label}: response has no boolean 'exists' value")
    return payload["exists"]


def upload_prepared_track(
    api_url: str,
    api_key: str,
    prepared_track: PreparedTrack,
    visibility: str,
    *,
    verbose: bool,
    file_hash: str | None = None,
) -> dict:
    if not prepared_track.output_path.is_file():
        raise UploaderError(f"Cannot upload missing extracted file: {prepared_track.output_path}")

    name = prepared_track.output_path.name
    label = f"upload of {name}"
    form_data = {
        "original_video_mediainfo": json.dumps(prepared_track.original_video_mediainfo),
        "original_video_mediainfo_text": prepared_track.original_video_mediainfo_text,
        "track_id_inside_container": prepared_track.media_info_track_id,
        "visibility": visibility,
    }

    def send() -> httpx.Response:
        # Reopened per attempt: a retried upload has to replay the body from the
        # start, and the progress bar has to restart with it.
        file_size = prepared_track.output_path.stat().st_size
        with prepared_track.output_path.open("rb") as media_file:
            progress_file = ProgressFile(media_file, file_size, name)
            try:
                response = httpx.post(
                    api_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    data=form_data,
                    files={"media_file": (name, progress_file)},
                    timeout=60.0 * 10,
                )
            except BaseException:
                progress_file.close_line()
                raise
            progress_file.finish()
            return response

    def already_published() -> bool:
        return is_track_already_published(
            api_url, api_key, prepared_track.output_path, verbose=verbose, file_hash=file_hash
        )

    response = send_with_retry(
        label, api_url, send, verbose=verbose, already_done=already_published
    )
    return response_json(label, response)


def get_default_output_dir() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def get_default_state_dir() -> Path:
    from_env = os.environ.get("UPLOADER_STATE_DIR")
    if from_env:
        return Path(from_env)
    return Path.home() / ".audio-bucket-uploader"


def remove_extracted_file(prepared_track: PreparedTrack, *, verbose: bool = False) -> None:
    """Never fatal: a leftover temp file must not sink a track that already
    uploaded successfully."""
    if not prepared_track.cleanup_after_upload:
        return
    try:
        prepared_track.output_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log_event(
            "WARNING", prepared_track.output_path.name, "cleanup",
            f"failed to remove {prepared_track.output_path}: {exc}",
        )
    else:
        log_event(
            "INFO", prepared_track.output_path.name, "cleanup",
            f"removed {prepared_track.output_path}", verbose=verbose,
        )


def track_item(prepared_track: PreparedTrack) -> str:
    return f"{prepared_track.track_type}:{prepared_track.media_info_track_id}"


def safe_fingerprint(file_path: Path) -> str:
    """Fingerprint that never raises. A file whose stat() fails (a dangling
    symlink, an unmounted disk) must still reach the history check, or it can
    never be given up on and is retried on every run forever. The empty string
    never matches a stored fingerprint, so the entry is reconsidered next run."""
    try:
        return file_fingerprint(file_path)
    except OSError:
        return ""


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def record_file_failure(
    history: History, stats: Stats, file_path: Path, stage: str, exc: BaseException
) -> None:
    stats.failed += 1
    history.record_failure(file_path, FILE_ITEM, safe_fingerprint(file_path), stage, describe(exc))
    log_event("ERROR", file_path.name, stage, f"skipped: {exc}")


def skipped_by_history(
    history: History, stats: Stats, file_path: Path, fingerprint: str, item: str, *, verbose: bool
) -> bool:
    """True when the history says this item needs no work. A given-up item is
    reported as the failure it is, never as quiet success."""
    entry = history.check(file_path, item, fingerprint)
    if entry is None:
        return False

    kind, reason = entry
    label = file_path.name if item == FILE_ITEM else f"{file_path.name} track {item}"
    if kind == GIVEN_UP:
        stats.given_up += 1
        log_event("ERROR", label, "history", reason)
    else:
        stats.resumed += 1
        log_event("INFO", label, "history", f"skipped: {reason}", verbose=verbose)
    return True


def process_track(
    args: argparse.Namespace,
    history: History,
    stats: Stats,
    source_path: Path,
    fingerprint: str,
    item: str,
    prepared_track: PreparedTrack,
) -> bool:
    """Hash-check and upload one track. True when it reached a final state.

    Only a dead server escapes: any other problem is recorded against this one
    track and the run moves on.
    """
    name = prepared_track.output_path.name
    try:
        file_hash = get_file_hash(prepared_track.output_path)
        if is_track_already_published(
            args.api_url, args.api_key, prepared_track.output_path,
            verbose=args.verbose, file_hash=file_hash,
        ):
            history.mark(source_path, item, fingerprint, "duplicate")
            stats.duplicates += 1
            log_event(
                "WARNING", name, "upload",
                "file will not be uploaded because the hash check reports it is already published",
            )
        else:
            upload_response = upload_prepared_track(
                args.api_url, args.api_key, prepared_track, args.visibility,
                verbose=args.verbose, file_hash=file_hash,
            )
            history.mark(source_path, item, fingerprint, "uploaded", str(upload_response.get("id", "")))
            stats.uploaded += 1
            if prepared_track.extraction_track_id is None:
                stats.standalone_uploaded += 1
            log_event(
                "INFO", name, "upload",
                f"uploaded as {args.visibility} track {upload_response.get('id')}",
                verbose=args.verbose,
            )
        if not args.keep_extracted:
            remove_extracted_file(prepared_track, verbose=args.verbose)
        return True
    except AlreadyPublished:
        history.mark(source_path, item, fingerprint, "duplicate", "answer lost, hash check confirmed it landed")
        stats.duplicates += 1
        log_event("WARNING", name, "upload", "the answer was lost, but the track did reach the server")
        if not args.keep_extracted:
            remove_extracted_file(prepared_track, verbose=args.verbose)
        return True
    except ServerDown:
        raise
    except Exception as exc:
        stats.failed += 1
        history.record_failure(source_path, item, fingerprint, "upload", describe(exc))
        log_event("ERROR", name, "upload", f"failed: {exc}")
        return False


def process_container(
    args: argparse.Namespace,
    history: History,
    stats: Stats,
    file_path: Path,
    output_dir: Path,
    target_languages_by_type: dict[str, list[str]],
) -> None:
    fingerprint = safe_fingerprint(file_path)
    if skipped_by_history(history, stats, file_path, fingerprint, FILE_ITEM, verbose=args.verbose):
        return

    log_event("INFO", file_path.name, "inspect", str(file_path), verbose=args.verbose)
    prepared_tracks = build_prepared_tracks(file_path, output_dir, target_languages_by_type)
    if not prepared_tracks:
        subtitle_filter_label = (
            "all"
            if target_languages_by_type["subtitles"] == [ALL_SUBTITLE_LANGUAGES]
            else ", ".join(target_languages_by_type["subtitles"])
        )
        log_event(
            "WARNING", file_path.name, "extract",
            f"skipped: no matching audio tracks for {', '.join(target_languages_by_type['audio'])} "
            f"or subtitle tracks for {subtitle_filter_label} found",
        )
        stats.skipped += 1
        history.mark(file_path, FILE_ITEM, fingerprint, "done", "no matching tracks")
        return

    given_up_before = stats.given_up
    pending: list[PreparedTrack] = []
    for prepared_track in prepared_tracks:
        item = track_item(prepared_track)
        if not skipped_by_history(history, stats, file_path, fingerprint, item, verbose=args.verbose):
            pending.append(prepared_track)
    # A track we have given up on is an unfinished failure, so the file as a
    # whole is not done — marking it done would hide it from every future run.
    has_given_up_track = stats.given_up > given_up_before

    if not pending:
        if not has_given_up_track:
            history.mark(file_path, FILE_ITEM, fingerprint, "done", "all tracks already processed")
        return

    log_event("INFO", file_path.name, "extract", f"extracting {len(pending)} track(s)")
    try:
        failures = extract_tracks(file_path, pending)
        failed_ids = {id(prepared_track) for prepared_track, _ in failures}
        for prepared_track, error in failures:
            stats.failed += 1
            history.record_failure(
                file_path, track_item(prepared_track), fingerprint, "extract", error
            )
            log_event(
                "ERROR", file_path.name, "extract",
                f"track {prepared_track.media_info_track_id} failed: {error}",
            )

        extracted = [track for track in pending if id(track) not in failed_ids]
        stats.extracted += len(extracted)
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
                for prepared_track in extracted
            ],
            target=file_path.name,
            action="extract",
        )

        complete = not failures and not has_given_up_track
        for prepared_track in extracted:
            if not process_track(
                args, history, stats, file_path, fingerprint,
                track_item(prepared_track), prepared_track,
            ):
                complete = False
        if complete:
            history.mark(
                file_path, FILE_ITEM, fingerprint, "done", f"{len(prepared_tracks)} track(s)"
            )
    finally:
        # Whatever happened — a failed upload, a dead server, a crash — extracted
        # files must not pile up in the temp directory until the disk is full.
        if not args.keep_extracted:
            for prepared_track in pending:
                remove_extracted_file(prepared_track)


def process_standalone(
    args: argparse.Namespace,
    history: History,
    stats: Stats,
    file_path: Path,
    target_languages_by_type: dict[str, list[str]],
) -> None:
    fingerprint = safe_fingerprint(file_path)
    if skipped_by_history(history, stats, file_path, fingerprint, FILE_ITEM, verbose=args.verbose):
        return

    log_event("INFO", file_path.name, "inspect", str(file_path), verbose=args.verbose)
    try:
        prepared_track = build_standalone_track(file_path, target_languages_by_type)
    except StandaloneSkip as skip:
        stats.skipped += 1
        if skip.durable:
            history.mark(file_path, FILE_ITEM, fingerprint, "skipped", str(skip))
        log_event(
            "WARNING", file_path.name, "upload",
            f"standalone file will not be uploaded: {skip}",
        )
        return

    log_event(
        "INFO", file_path.name, "upload",
        f"uploading standalone {prepared_track.track_type}; language={prepared_track.language}",
        verbose=args.verbose,
    )
    process_track(args, history, stats, file_path, fingerprint, FILE_ITEM, prepared_track)


def discover_inputs(args: argparse.Namespace, input_path: Path) -> tuple[list[Path], list[Path]]:
    if not input_path.is_file():
        files, unreadable = walk_files(input_path)  # one walk, both kinds
        mkv_files = sorted(path for path in files if path.suffix.lower() == ".mkv")
        standalone_files = (
            sorted(path for path in files if is_standalone_file(path)) if args.standalone else []
        )
        if not mkv_files and not standalone_files and unreadable:
            # "Found nothing" and "could not look" must not report the same way,
            # or a badly mounted /input reports success forever.
            raise UploaderError(
                f"Found no media under {input_path} and could not read {unreadable} "
                "director(y/ies) there; check the permissions of the mount"
            )
        return mkv_files, standalone_files
    if input_path.suffix.lower() == ".mkv":
        return [input_path], []
    if args.standalone and is_standalone_file(input_path):
        return [], [input_path]
    raise UploaderError(
        f"Unsupported input file: {input_path}. Expected an .mkv file"
        + ("" if args.standalone else " (standalone uploads are disabled)")
        + "."
    )


def check_api_url(api_url: str) -> None:
    """A typo in the URL is a startup error. Without this, httpx raises on every
    file instead (InvalidURL is not an HTTPError, so it is not classified as a
    per-file rejection), and a missing scheme is retried five times before the
    run stops with a misleading "server unreachable"."""
    parsed = urlsplit(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UploaderError(
            f"--api-url must be a full http(s) URL, for example "
            f"https://audio-bucket.site/api/uploader (got: {api_url})"
        )
    try:
        httpx.URL(api_url)  # catches what urlsplit accepts, e.g. a bad port
    except httpx.InvalidURL as exc:
        raise UploaderError(f"--api-url is not a valid URL ({exc}): {api_url}") from exc


def filters_key(target_languages_by_type: dict[str, list[str]]) -> str:
    """The language selection, as the history sees it. Sorted, so that
    `--audio-language uk,en` and `--audio-language en,uk` — the same request —
    do not expire each other's history."""
    return json.dumps(
        {key: sorted(values) for key, values in target_languages_by_type.items()},
        sort_keys=True,
    )


def main() -> int:
    # A launcher that closes stdout (`python -m uploader >&-`) leaves sys.stdout
    # as None, and every write then raises AttributeError, not OSError — which
    # would fail every upload, since the progress bar IS the request body.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # line_buffering: Docker sets PYTHONUNBUFFERED, but a direct
            # `python -m uploader > log` block-buffers stdout, so our progress
            # lands in the log long after the mkvextract output it describes.
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    # `docker stop` sends SIGTERM, which by default kills the process without
    # unwinding, stranding a multi-GB half-extracted track in the output dir.
    # Turning it into SystemExit lets the cleanup handlers run. One-shot: a
    # second SIGTERM landing in the cleanup itself would abort the cleanup.
    def on_terminate(*_args) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sys.exit(143)

    signal.signal(signal.SIGTERM, on_terminate)

    args = parse_args()
    check_api_url(args.api_url)
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    target_languages_by_type = {
        "audio": parse_language_filters(args.audio_languages, DEFAULT_AUDIO_LANGUAGES),
        "subtitles": parse_subtitle_language_filters(args.subtitle_languages),
    }

    if not input_path.exists():
        raise UploaderError(f"Input path does not exist: {input_path}")

    mkv_files, standalone_files = discover_inputs(args, input_path)
    if not mkv_files and not standalone_files:
        log_event(
            "INFO", "input", "detect",
            f"nothing to upload: no .mkv"
            + (" or standalone audio/subtitle" if args.standalone else "")
            + f" files found at {input_path}",
        )
        return 0

    history = History(
        Path(args.state_dir).expanduser().resolve(),
        filters=filters_key(target_languages_by_type),
    )
    if args.retry_failed:
        cleared = history.clear_failures()
        log_event(
            "INFO", "history", "retry",
            f"forgot {cleared} earlier failure(s); retrying them"
            if cleared
            else "no earlier failures to retry",
        )
    log_event(
        "INFO", "input", "detect",
        f"found {len(mkv_files)} MKV file(s) and {len(standalone_files)} standalone file(s); "
        f"history in {history.state_dir}",
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

    stats = Stats()
    stopped = ""
    try:
        for file_path in mkv_files:
            try:
                process_container(
                    args, history, stats, file_path, output_dir, target_languages_by_type
                )
            except ServerDown:
                raise
            except Exception as exc:  # a broken file must never sink the run
                record_file_failure(history, stats, file_path, "process", exc)

        for file_path in standalone_files:
            try:
                process_standalone(args, history, stats, file_path, target_languages_by_type)
            except ServerDown:
                raise
            except Exception as exc:
                record_file_failure(history, stats, file_path, "process", exc)
    except ServerDown as exc:
        stopped = str(exc)
        log_event("ERROR", "run", "stop", f"stopping: {exc}")

    log_event(
        "INFO", "run", "summary",
        f"extracted {stats.extracted} track file(s); "
        f"uploaded {stats.uploaded} {args.visibility} track(s) ({stats.standalone_uploaded} standalone); "
        f"{stats.duplicates} already published; {stats.skipped} not uploadable; "
        f"{stats.resumed} skipped from history; {stats.failed} failed; "
        f"{stats.given_up} given up on after {MAX_ATTEMPTS} attempts",
    )
    if stats.failed or stats.given_up:
        log_event(
            "ERROR", "run", "summary",
            f"{stats.failed} failure(s) this run and {stats.given_up} item(s) given up on; "
            f"see {history.failures_path}"
            + (". Fix the cause and re-run with --retry-failed" if stats.given_up else ""),
        )
    history.close()

    if stopped:
        return SERVER_DOWN_EXIT_CODE
    return 1 if stats.failed or stats.given_up else 0


def detach_dead_stdout() -> None:
    """If stdout is gone (`python -m uploader | head`, a full disk), CPython's
    shutdown flush fails outside every guard we have and turns a perfectly good
    run into exit 120. Point stdout at the void instead."""
    try:
        sys.stdout.flush()
    except OSError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log_event("WARNING", "run", "stop", "interrupted")
        raise SystemExit(130) from None
    except Exception as exc:  # startup problems only; the run itself never lands here
        log_event("ERROR", "run", "startup", describe(exc))
        raise SystemExit(1) from exc
    finally:
        # Every exit path, including SIGTERM (143) and Ctrl-C (130): the flush
        # at interpreter shutdown must not turn them into 120.
        detach_dead_stdout()
