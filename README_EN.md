# Audio Bucket Uploader

Extracts selected audio and subtitle tracks plus embedded fonts from `.mkv` files, then uploads them to Audio Bucket.

## Overview

- Process one `.mkv` file or recursively scan a directory.
- Upload Ukrainian audio and all subtitle tracks by default; choose other languages when needed.
- Extract tracks and font attachments in one `mkvextract` pass.
- Avoid work already stored on the server and duplicate uploads.
- Show extraction and upload progress, outcomes, skips, errors, and a final summary.
- Optionally upload loose audio and subtitle files beside their source video.

## Install and use

The easiest option is the Docker-based helper script. Install and start [Docker](https://www.docker.com/get-started/) first.

### macOS and Linux

Download the helper and make it executable:

```bash
curl -fsSLO https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.sh
chmod +x ukrab-uploader.sh
```

Or use `wget`:

```bash
wget https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.sh
chmod +x ukrab-uploader.sh
```

Upload one `.mkv` file or a directory (directories are scanned recursively):

```bash
./ukrab-uploader.sh <your_api_key> /path/to/movie-or-directory
```

Uploads are public by default. Add `draft` to create drafts:

```bash
./ukrab-uploader.sh <your_api_key> /path/to/movie-or-directory draft
```

The named form also works: `./ukrab-uploader.sh --api-key <your_api_key> --input /path/to/movie-or-directory --visibility draft`. Add `--verbose` for detailed output. The helper pulls `ghcr.io/sirko-ua/audio-bucket-uploader:latest` as needed, uses `https://ukrab.work/api/uploader`, and keeps the standard defaults.

### Windows

Install and start [Docker Desktop](https://www.docker.com/products/docker-desktop/). In PowerShell, download the helper:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.bat -OutFile ukrab-uploader.bat
```

Run it with your API key and a file or directory:

```powershell
.\ukrab-uploader.bat <your_api_key> "C:\path\to\movie-or-directory"
```

Add `draft` as the final argument to create drafts:

```powershell
.\ukrab-uploader.bat <your_api_key> "C:\path\to\movie-or-directory" draft
```

The named form also works: `.\ukrab-uploader.bat --api-key <your_api_key> --input "C:\path\to\movie-or-directory" --visibility draft`. Add `--verbose` for detailed output.

## Arguments

| Argument | Required | Default | Description |
| --- | --- | --- | --- |
| `--api-key` | Yes | none | Audio Bucket user API key. Sent as a bearer token for uploads and in `X-API-Key` for the original-video check. |
| `--api-url` | Yes | none | Audio Bucket uploader endpoint URL, for example `https://audio-bucket.site/api/uploader`. |
| `--input` | No | `/input` | A single `.mkv` file or a directory of `.mkv` files. Directories are scanned recursively. |
| `--audio-language` | No | `uk` | Audio languages to upload. Repeat it or use comma-separated values, for example `--audio-language uk,en`. |
| `--subtitle-language` | No | `all` | Subtitle languages to upload. Repeat it or use comma-separated values; `all` uploads every subtitle track. |
| `--output-dir` | No | OS temp directory | Where extracted tracks and fonts are written before upload. |
| `--keep-extracted` | No | `false` | Keep extracted files after successful upload. |
| `--visibility` | No | `public` | Track visibility: `draft` or `public`. |
| `--standalone`, `--no-standalone` | No | `true` | Also discover loose audio/subtitle files. See [Standalone files](#standalone-files). |
| `--verbose`, `--no-verbose` | No | `false` | Show file lists, extracted-media tables, HTTP status, `mkvextract` details, and cleanup paths. Request bodies are never shown. |

Language filters are normalized, with aliases such as `uk`, `ukr`, and `ukrainian` supported.

## How it works under the hood

### Overview

The uploader reads the source video’s MediaInfo, selects matching tracks, and invokes `mkvextract` once for tracks and font attachments. Extracted tracks are named:

```text
{original_movie_name}_track{track_id_inside_container}.{detected_extension}
```

Font attachments are named:

```text
{original_movie_name}_attachment{attachment_id}_{safe_container_filename}
```

Each track upload includes the extracted `media_file`, original MKV MediaInfo in JSON and text, the MediaInfo `ID` as `track_id_inside_container`, and the selected `visibility`. Successfully uploaded extracted files are removed unless `--keep-extracted` is set.

Every log event follows `timestamp | level | target | action | details`. Default output includes extraction/upload progress, outcomes, skips, errors, and a summary; `--verbose` adds diagnostics without changing that format.

```text
<timestamp> | INFO | Movie.mkv | extract | tracks=2 attachments=4
<timestamp> | INFO | Movie.mkv | extract |  50%
<timestamp> | INFO | Movie_track2.eac3 | upload | kind=track type=audio visibility=public id=123
<timestamp> | INFO | run | summary | tracks_extracted=2 attachments_extracted=4 tracks_uploaded=2 attachments_uploaded=4 already_published=0 attachments_already_present=0 standalone_uploaded=0 standalone_skipped=0 failed=0
```

### Duplicate check

Before `mkvextract` runs, the uploader sends the original video’s MediaInfo `unique_id` to `POST /api/uploader/original-video-check` using `X-API-Key`. If the video exists, it extracts only MediaInfo track IDs absent from `track_ids_inside_container` and fonts whose composite names are absent from `attachment_original_filenames`. MediaInfo IDs are mapped to the usually different, zero-based `mkvextract` track IDs; the MediaInfo ID remains the value sent as `track_id_inside_container`.

Before each track upload, it computes a 64-character BLAKE3-256 digest and calls `POST /api/uploader/hash-check`. `{"exists": true}` skips the upload. This check applies equally to `public` and `draft`, and applies to standalone tracks too.

### Font attachments

Fonts come from `mkvmerge -J`’s structured `attachments` array. The uploader recognizes official Matroska font MIME types, common legacy forms, and font extensions on generic `application/octet-stream` attachments; covers and other attachments are ignored. Container filenames are reduced to safe filename components before writing to `--output-dir`.

Fonts are uploaded to `--api-url` with `/attachments` appended—for example, `https://audio-bucket.site/api/uploader/attachments`. The multipart request includes the source MediaInfo, the extracted file, the composite filename as `original_filename`, and the attachment’s unsigned 64-bit Matroska UID. Font uploads use this UID-based endpoint rather than the track hash-check endpoint.

### Standalone files

With `--standalone` (the default), the uploader also looks for loose files beside their source video:

- **Audio:** `wav`, `mp3`, `aac`, `flac`, `ogg`, `m4a`, `opus`, `ac3`, `eac3`, `ac4`, `dts`, `dtshd`, `truehd`, `mlp`, `thd`
- **Subtitles:** `ass`, `srt`, `pgs`, `sup`

A standalone file is uploaded only when its language can be determined from its filename (for example, `Movie.uk.srt` or `Movie_track2_[ukr]_DELAY 0ms.eac3`) or MediaInfo, and a sibling video with the same base name has a MediaInfo `unique_id`. The source video does not need to contain a matching track: the standalone file’s MediaInfo is appended as an extra track while retaining the real source video’s `unique_id`. Standalone source files are never deleted.

## Run with Docker or locally

### Docker

Pull the published image:

```bash
docker pull ghcr.io/sirko-ua/audio-bucket-uploader:latest
```

Minimum invocation:

```bash
docker run --rm \
  -v /path/to/movies:/input:ro \
  ghcr.io/sirko-ua/audio-bucket-uploader:latest \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader
```

This uses the default `--input /input`. To keep extracted output, mount a writable directory and set `--output-dir`:

```bash
docker run --rm \
  -v /path/to/movies:/input:ro \
  -v /path/to/extracted:/output \
  ghcr.io/sirko-ua/audio-bucket-uploader:latest \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader \
  --output-dir /output \
  --keep-extracted \
  --verbose
```

### Locally

Install Python dependencies plus `mediainfo` and MKVToolNix (which provides `mkvmerge` and `mkvextract`), then run:

```bash
python -m pip install -r requirements.txt
python -m uploader \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader \
  --input /media/movies
```
