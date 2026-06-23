from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

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
    "alac": "m4a",
    "ass": "ass",
    "e_ac_3": "eac3",
    "eac3": "eac3",
    "flac": "flac",
    "opus": "opus",
    "pcm": "wav",
    "pgs": "sup",
    "ssa": "ssa",
    "subrip": "srt",
    "srt": "srt",
    "sup": "sup",
    "utf8": "srt",
    "textutf8": "srt",
    "truehd": "thd",
    "dts": "dts",
}

TRACK_TYPE_EXTENSION_DEFAULTS = {
    "audio": "bin",
    "subtitles": "sub",
}

ALL_SUBTITLE_LANGUAGES = "__all_subtitle_languages__"
DEFAULT_AUDIO_LANGUAGES = ["uk"]
DEFAULT_SUBTITLE_LANGUAGES = ["all"]
DEFAULT_INPUT = "/input"
DEFAULT_VERBOSE = True


class UploaderError(RuntimeError):
    pass


@dataclass
class PreparedTrack:
    track_id: int
    track_type: str
    language: str
    codec: str
    channels: str
    bitrate: str
    fps: str
    output_path: Path


class ProgressFile:
    def __init__(self, file_obj, total_size: int, label: str) -> None:
        self._file_obj = file_obj
        self._total_size = max(total_size, 1)
        self._label = label
        self._uploaded = 0
        self._last_percent = -1

    def read(self, size: int = -1) -> bytes:
        chunk = self._file_obj.read(size)
        if chunk:
            self._uploaded += len(chunk)
            self._render()
        return chunk

    def _render(self) -> None:
        percent = min(int(self._uploaded * 100 / self._total_size), 100)
        if percent == self._last_percent and self._uploaded != self._total_size:
            return
        self._last_percent = percent
        uploaded_mb = self._uploaded / (1024 * 1024)
        total_mb = self._total_size / (1024 * 1024)
        sys.stdout.write(f"\rUploading {self._label}: {percent:3d}% ({uploaded_mb:.1f}/{total_mb:.1f} MiB)")
        sys.stdout.flush()

    def finish(self) -> None:
        self._uploaded = self._total_size
        self._render()
        sys.stdout.write("\n")
        sys.stdout.flush()

    def close_line(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()

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
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_VERBOSE,
        help="Print detailed extraction planning output. Defaults to true; use --no-verbose to disable.",
    )
    return parser.parse_args()


def run_json_command(command: list[str]) -> dict:
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UploaderError(f"Command did not return valid JSON: {' '.join(command)}") from exc


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
    if normalized.startswith("subrip") or normalized in {"srt", "stextutf8", "stext_utf8", "textutf8", "text_utf8", "utf8", "utf_8"}:
        return "subrip"
    return normalized


def title_has_marker(value: object, marker: str) -> bool:
    if not value:
        return False
    return marker.lower() in str(value).lower()


def subtitle_disposition_tokens(track: dict) -> list[str]:
    properties = track.get("properties", {})
    title = properties.get("track_name") or properties.get("title") or ""
    tokens: list[str] = []

    if properties.get("forced_track") or properties.get("flag_forced") or title_has_marker(title, "forced"):
        tokens.append("forced")
    if title_has_marker(title, "sdh"):
        tokens.append("sdh")
    return tokens


def format_bitrate(value: object) -> str:
    if value in (None, ""):
        return "na"
    match = re.search(r"\d+", str(value))
    if not match:
        return "na"
    bitrate = int(match.group(0))
    if bitrate >= 1000:
        return f"{round(bitrate / 1000)}kbps"
    return f"{bitrate}bps"


def format_channels(value: object) -> str:
    if value in (None, ""):
        return "na"
    match = re.search(r"\d+(\.\d+)?", str(value))
    if not match:
        return "na"
    number = float(match.group(0))
    if number.is_integer():
        return f"{int(number)}ch"
    return f"{number:g}ch"


def format_fps(value: object) -> str:
    if value in (None, ""):
        return "na"
    match = re.search(r"\d+(\.\d+)?", str(value))
    if not match:
        return "na"
    number = float(match.group(0))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def normalize_movie_name(file_path: Path) -> str:
    raw_name = file_path.stem.strip().strip("\"'")
    normalized = re.sub(r"[\\/:*?\"<>|]+", " ", raw_name)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or file_path.stem


def detect_extension(codec: str, track_type: str) -> str:
    normalized_codec = sanitize_token(codec)
    return CODEC_EXTENSION_MAP.get(normalized_codec, TRACK_TYPE_EXTENSION_DEFAULTS[track_type])


def infer_codec(track: dict, media_info_track: dict | None) -> str:
    candidates = [
        get_media_info_value(media_info_track, "format", "Format"),
        get_media_info_value(media_info_track, "codec_id_hint", "CodecID/Hint", "CodecID", "codec_id"),
        track.get("codec"),
        track.get("properties", {}).get("codec_id"),
    ]
    for candidate in candidates:
        if candidate:
            normalized = canonicalize_codec(candidate)
            if normalized != "na":
                return normalized
    return "unknown"


def find_media_info_track(media_info_by_type: dict[str, list[dict]], track_type: str, index: int) -> dict | None:
    tracks = media_info_by_type.get(track_type, [])
    if index >= len(tracks):
        return None
    return tracks[index]


def collect_media_info(file_path: Path) -> tuple[dict[str, list[dict]], dict]:
    media_info_payload = run_json_command(["mediainfo", "--Output=JSON", str(file_path)])
    mkvmerge_payload = run_json_command(["mkvmerge", "-J", str(file_path)])

    tracks_by_type: dict[str, list[dict]] = {"video": [], "audio": [], "text": []}
    for track in media_info_payload.get("media", {}).get("track", []):
        track_type = str(track.get("@type", "")).lower()
        if track_type in tracks_by_type:
            tracks_by_type[track_type].append(track)

    return tracks_by_type, mkvmerge_payload


def build_prepared_tracks(
    file_path: Path,
    output_dir: Path,
    target_languages_by_type: dict[str, list[str]],
) -> list[PreparedTrack]:
    media_info_by_type, mkvmerge_payload = collect_media_info(file_path)
    movie_name = normalize_movie_name(file_path)
    video_track = media_info_by_type.get("video", [{}])[0]
    fps = format_fps(get_media_info_value(video_track, "frame_rate", "FrameRate"))

    type_indices = {"audio": 0, "subtitles": 0}
    prepared_tracks: list[PreparedTrack] = []
    used_output_paths: set[Path] = set()

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
            "audio" if track_type == "audio" else "text",
            type_indices[track_type],
        )
        type_indices[track_type] += 1

        fallback_language = "und" if target_languages == [ALL_SUBTITLE_LANGUAGES] else target_languages[0]
        language = normalize_language(properties.get("language_ietf") or properties.get("language") or fallback_language)
        codec = infer_codec(track, media_info_track)
        channels = format_channels(get_media_info_value(media_info_track, "channel_s", "Channel(s)"))
        bitrate = format_bitrate(
            get_media_info_value(media_info_track, "bit_rate", "BitRate")
            if media_info_track
            else properties.get("audio_bits_per_sample")
        )
        extension = detect_extension(codec, track_type)
        if track_type == "subtitles":
            subtitle_tokens = [
                language or sanitize_token(fallback_language),
                codec,
                *subtitle_disposition_tokens(track),
                fps,
            ]
            channels = "na"
            bitrate = "na"
            base_name = f"{movie_name}_[{'_'.join(subtitle_tokens)}]"
        else:
            base_name = f"{movie_name}_[{language or sanitize_token(fallback_language)}_{codec}_{channels}_{bitrate}_{fps}]"
        output_path = output_dir / f"{base_name}.{extension}"
        if output_path in used_output_paths:
            output_path = output_dir / f"{base_name}_track{track['id']}.{extension}"
        used_output_paths.add(output_path)
        prepared_tracks.append(
            PreparedTrack(
                track_id=int(track["id"]),
                track_type=track_type,
                language=language or sanitize_token(fallback_language),
                codec=codec,
                channels=channels,
                bitrate=bitrate,
                fps=fps,
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


def extract_tracks(file_path: Path, prepared_tracks: list[PreparedTrack]) -> None:
    if not prepared_tracks:
        return
    command = ["mkvextract", "tracks", str(file_path)]
    for prepared_track in prepared_tracks:
        prepared_track.output_path.parent.mkdir(parents=True, exist_ok=True)
        command.append(f"{prepared_track.track_id}:{prepared_track.output_path}")
    run_command(command)


def upload_prepared_track(api_url: str, api_key: str, prepared_track: PreparedTrack) -> dict:
    if prepared_track.fps == "na":
        raise UploaderError(f"Cannot upload {prepared_track.output_path.name}: original video FPS is unknown.")
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
                    "track_type": prepared_track.track_type,
                    "language": prepared_track.language,
                    "fps": prepared_track.fps,
                },
                files={"media_file": (prepared_track.output_path.name, progress_file)},
                timeout=60.0 * 10,
            )
            progress_file.finish()
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if progress_file is not None:
            progress_file.close_line()
        raise UploaderError(
            f"Upload failed for {prepared_track.output_path.name}: "
            f"HTTP {exc.response.status_code} {exc.response.text}"
        ) from exc
    except httpx.HTTPError as exc:
        if progress_file is not None:
            progress_file.close_line()
        raise UploaderError(f"Upload failed for {prepared_track.output_path.name}: {exc}") from exc

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise UploaderError(f"Upload response was not valid JSON for {prepared_track.output_path.name}") from exc


def log(message: str, *, verbose: bool) -> None:
    if verbose:
        print(message)


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
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
    )
    target_languages_by_type = {
        "audio": parse_language_filters(args.audio_languages, DEFAULT_AUDIO_LANGUAGES),
        "subtitles": parse_subtitle_language_filters(args.subtitle_languages),
    }

    mkv_files = discover_mkv_files(input_path)
    if not mkv_files:
        raise UploaderError(f"No .mkv files found at: {input_path}")

    log(f"Uploading extracted tracks to {args.api_url}", verbose=args.verbose)
    print(f"Found {len(mkv_files)} MKV file(s).")

    extracted_count = 0
    uploaded_count = 0
    for file_path in mkv_files:
        log(f"Inspecting {file_path}", verbose=args.verbose)
        prepared_tracks = build_prepared_tracks(file_path, output_dir, target_languages_by_type)
        if not prepared_tracks:
            subtitle_filter_label = (
                "all"
                if target_languages_by_type["subtitles"] == [ALL_SUBTITLE_LANGUAGES]
                else ", ".join(target_languages_by_type["subtitles"])
            )
            print(
                f"Skipping {file_path.name}: no matching audio tracks for {', '.join(target_languages_by_type['audio'])} "
                f"or subtitle tracks for {subtitle_filter_label} found."
            )
            continue
        for prepared_track in prepared_tracks:
            log(
                f"Prepared filename for track {prepared_track.track_id}: {prepared_track.output_path.name}",
                verbose=args.verbose,
            )
        extract_tracks(file_path, prepared_tracks)
        extracted_count += len(prepared_tracks)
        for prepared_track in prepared_tracks:
            print(f"Extracted {prepared_track.track_type} track {prepared_track.track_id} -> {prepared_track.output_path}")
            upload_response = upload_prepared_track(args.api_url, args.api_key, prepared_track)
            uploaded_count += 1
            print(f"Uploaded {prepared_track.output_path.name} as draft track {upload_response.get('id')}")
            if not args.keep_extracted:
                remove_extracted_file(prepared_track)
                log(f"Removed extracted file {prepared_track.output_path}", verbose=args.verbose)

    print(f"Prepared {extracted_count} extracted track file(s). Uploaded {uploaded_count} draft track(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
