# Audio Bucket Uploader

Extracts audio and subtitle tracks from `.mkv` files by language and uploads them to Audio Bucket.

## Overview

- finds one `.mkv` file or recursively scans a directory for `.mkv` files
- extracts matching audio and subtitle tracks with `mkvextract`
- names extracted files as:

```text
{original_movie_name}_track{track_id_inside_container}.{detected_extension}
```

- uploads each extracted track to Audio Bucket as a draft or public track through `POST /api/uploader`
- computes a BLAKE3-256 hash and checks `POST /api/uploader/hash-check` before uploading to prevent duplicates
- sends the extracted `media_file`, original MKV MediaInfo JSON/text, MediaInfo `ID` as `track_id_inside_container`, and the selected `visibility` in the upload request
- shows per-file upload progress while each extracted track is being sent (extraction output is captured, so that a failing tool can be reported with its reason)
- with `--verbose`, prints detailed detection, HTTP request, upload result, and cleanup events
- removes each extracted file once it is dealt with — uploaded, found to be a duplicate, or failed — unless `--keep-extracted` is set, so that a long run cannot fill the temp directory
- optionally also uploads standalone (loose) audio and subtitle files found next to their source video (see [Standalone files](#standalone-files))
- remembers what it already did, so a restarted run resumes instead of re-extracting and re-hash-checking (see [History and resuming](#history-and-resuming))
- survives broken files, failed extractions, and network errors: it records them and keeps going (see [Failures](#failures))

## Arguments

| Argument | Required | Default | Description |
| --- | --- | --- | --- |
| `--api-key` | Yes | none | Audio Bucket user API key. It is sent as a bearer token in the upload request. |
| `--api-url` | Yes | none | Audio Bucket uploader endpoint URL, for example `https://audio-bucket.site/api/uploader`. |
| `--input` | No | `/input` | Path to a single `.mkv` file or a directory containing `.mkv` files. Directories are scanned recursively. |
| `--audio-language` | No | `uk` | Target audio track language. Pass it multiple times or use comma-separated values, for example `--audio-language uk --audio-language en` or `--audio-language uk,en`. |
| `--subtitle-language` | No | `all` | Target subtitle track language. Pass it multiple times or use comma-separated values. `all` uploads every subtitle track regardless of language. |
| `--output-dir` | No | OS-specific temp directory | Directory where extracted tracks are written before upload. On macOS and Linux this is typically `/tmp`; on Windows it follows the standard temp location from the OS environment. |
| `--keep-extracted` | No | `false` | Keep extracted files instead of deleting them once they have been dealt with (uploaded, found to be a duplicate, or failed). |
| `--visibility` | No | `public` | Visibility for uploaded tracks: `draft` or `public`. |
| `--standalone`, `--no-standalone` | No | `true` | Also discover and upload standalone (loose) audio/subtitle files. See [Standalone files](#standalone-files). Use `--no-standalone` to process `.mkv` files only. |
| `--state-dir` | No | `~/.audio-bucket-uploader` | Directory holding the run history (`history.sqlite3`) and the failure log (`failures.jsonl`). Can also be set with the `UPLOADER_STATE_DIR` environment variable; the Docker image defaults it to `/state`. |
| `--retry-failed` | No | `false` | Forget earlier failures before starting, so files that were given up on after 3 failed attempts are tried once more. Use it after fixing the cause. |
| `--verbose`, `--no-verbose` | No | `false` | Print detailed file detection, HTTP request, upload result, and cleanup output. Detected files are shown as one-column full-path tables. Requests show only the method, path, and response code — never the request body. A failed request also logs a short excerpt of the server's error message, so that a rejection can be diagnosed. |

Language filters are normalized, and common aliases are supported for languages such as `uk`, `ukr`, and `ukrainian`.

## History and resuming

Extracting a track and hashing it costs minutes; asking the server about a hash it has already answered for costs it a request it did not need. The uploader therefore records every source file and every track it finishes in `history.sqlite3` inside the state directory, and a restarted run skips them **before** running MediaInfo, before extracting, and before contacting the server.

- Each entry is keyed by the file's path plus its size and modification time, so replacing or re-encoding a file makes the uploader process it again.
- Tracks that were uploaded, or that the server's hash check reported as already published, are never re-checked.
- Files with no matching tracks, and standalone files skipped for a reason that cannot change on its own (no language in the name, language not requested), are remembered too. Changing `--audio-language` or `--subtitle-language` re-examines them; a standalone file skipped because its source video was missing is always re-examined, since the video may have been added since.
- Failures are retried on the next run, up to 3 times. After that the item is **given up on**: it is reported at `ERROR` on every later run, counted in the summary, and makes the run exit non-zero — it is never silently treated as finished. Once you have fixed the cause, `--retry-failed` clears those failures and tries again.
- To reprocess a library from scratch, delete the state directory (or point `--state-dir` somewhere new).
- Visibility is decided by the first upload. Re-running with a different `--visibility` will not change an already-published track: the server's hash check reports it as existing, whatever its visibility.

**In Docker, mount the state directory** or the history dies with the container:

```bash
docker run --rm -v /path/to/movies:/input:ro -v /path/to/state:/state ...
```

The `ukrab-uploader.sh` and `ukrab-uploader.bat` helpers do this for you, keeping the state in `.audio-bucket-uploader` inside the media directory. Give **each library its own state directory**: inside the container every library is mounted at `/input`, so one shared state directory would let a mirrored library be mistaken for one already uploaded.

## Failures

A single broken `.mkv`, a failed extraction, a rejected upload, or a network blip never ends the run. Each one is logged, recorded, and the uploader moves on to the next file. Only two things stop it: **nothing left to upload**, or **the server being down**.

- Every failure is appended to `failures.jsonl` in the state directory, one JSON object per line, with the time, path, stage, and error — including the output of the tool that failed, so a broken file can be diagnosed afterwards.
- A damaged track no longer sinks its healthy siblings: if extracting a file's tracks in one pass fails, each track is retried on its own.
- A file that `mkvmerge` cannot read is reported as unreadable, not quietly counted as "no matching tracks".
- Requests are retried up to 5 times with exponential backoff. Before an upload is sent again — after a timeout, a dropped connection, or a `5xx` — the uploader asks the hash-check endpoint whether the file landed after all, so a lost answer never publishes the same track twice.
- A rejection of one file (`400`, `403`, `404`, `422` — for example "no release for this unique_id") is that file's answer, not a verdict on the run: it is recorded and the run continues, however many files in a row are rejected.
- The run stops with exit code `3` only when the server itself is the problem: it rejects the API key (`401`), the **very first** request comes back `403`/`404` (a wrong `--api-url` or key, before anything has worked), one endpoint fails repeatedly in a row after exhausting its retries, or the server asks — via `Retry-After` — for a longer pause than a retry can honour. A single dead connection counts as that one file's failure first: on a healthy server it usually is one (a proxy refusing an oversized body), and stopping on it would replay that file on every restart.
- How many failures in a row it takes depends on what came back. A connection that never answers means the host is gone, so **3** in a row stop the run. A `5xx` is different in kind: the server answered, so it is up and reachable — it choked on that payload. A folder of files the endpoint cannot digest is a run of per-file rejections, not an outage, so it takes **12** in a row before the run stops. Either counter resets on the first success against that endpoint.
- A probe that fails is retried 3 times before the file is blamed. A spun-down disk, a NAS reconnect, or a file the downloader is still writing makes a healthy container fail one `mediainfo`/`mkvmerge` read and pass the next — and without the retry that blip would consume one of the file's 3 attempts and eventually give up on it for good.
- A variable-frame-rate video stores no frame rate in its headers, and the endpoint rejects the upload without one ("Could not parse original video FPS from MediaInfo"). Those files are automatically reparsed in full so the real frame rate is computed and sent. The full parse reads the whole file, so it runs only for the files that need it, and never fails a file on its own: if it still yields nothing, the original MediaInfo is sent and the server keeps the final say.
- Exit codes: `0` finished, `1` finished with failures or with items given up on (see `failures.jsonl`), `2` bad command line (argparse), `3` stopped because the server is down, `130` interrupted, `143` terminated (`docker stop`, after cleaning up).

## Standalone files

With `--standalone` (the default) the uploader also picks up loose audio and subtitle files, not just tracks inside `.mkv` containers:

- **Audio:** `wav`, `mp3`, `aac`, `flac`, `ogg`, `m4a`, `opus`, `ac3`, `eac3`, `ac4`, `dts`, `dtshd`, `truehd`, `mlp`, `thd`
- **Subtitles:** `ass`, `srt`, `pgs`, `sup`

The uploader endpoint identifies a track by the source video's MediaInfo `unique_id` and reads the track's type and language from that video's MediaInfo. A standalone file is therefore uploaded only when both of the following hold, and is otherwise skipped with a printed reason:

1. Its language can be determined from the file name (e.g. `Movie.uk.srt`, `Show.en-GB.forced.srt`, `Movie_track2_[ukr]_DELAY 0ms.eac3`) or from MediaInfo.
2. A **sibling video** with the same base name sits in the same directory (e.g. `Movie.mkv` next to `Movie.uk.srt`, or `Movie.mkv` next to `Movie_track2.eac3`). The video must expose a MediaInfo `unique_id` — `.mkv` does; most `.mp4` files do not.

The source video does **not** need to already contain a matching track. An external subtitle usually has no counterpart inside the container (`Movie.mkv` carries Ukrainian audio but no Ukrainian subtitle track), so the standalone file's own MediaInfo is appended to the source video's MediaInfo as an extra track and `track_id_inside_container` points at it. The General `unique_id` stays that of the real source video, so the upload lands on the correct release and the endpoint reads the correct type and language.

Standalone source files are never deleted (`--keep-extracted` does not apply to them).

## Duplicate Check

Before each upload, the uploader computes the extracted file’s 64-character BLAKE3-256 digest and sends it to `POST /api/uploader/hash-check`. If the API returns `{"exists": true}`, the file is treated as already published and the upload is skipped.

The duplicate check is based only on the file hash and is not affected by `--visibility`: it runs the same way for both `public` and `draft`. The `--visibility` value is used only for an actual upload after the API returns `{"exists": false}`. Therefore, rerunning with a different visibility also skips the file if the API already finds that hash.

The check applies to standalone files as well.

## Easiest Way to Run (macOS and Linux)

Install and start [Docker](https://www.docker.com/get-started/) first. Then download the helper script and make it executable:

```bash
curl -fsSLO https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.sh
chmod +x ukrab-uploader.sh
```

Or, with `wget`:

```bash
wget https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.sh
chmod +x ukrab-uploader.sh
```

Run it with your API key and either one `.mkv` file or a directory of `.mkv` files. Directories are scanned recursively:

```bash
./ukrab-uploader.sh <your_api_key> /path/to/movie-or-directory
```

Uploads are public by default. To create drafts instead, add `draft`:

```bash
./ukrab-uploader.sh <your_api_key> /path/to/movie-or-directory draft
```

The named form also works: `./ukrab-uploader.sh --api-key <your_api_key> --input /path/to/movie-or-directory --visibility draft`. Add `--verbose` to either form for detailed output (or `--no-verbose` to explicitly select concise output). The helper pulls `ghcr.io/sirko-ua/audio-bucket-uploader:latest` automatically when needed and uses `https://ukrab.work/api/uploader`. It keeps the standard uploader defaults: Ukrainian audio, all subtitles, temporary extracted files, and concise output.

## Easiest Way to Run (Windows)

Install and start [Docker Desktop](https://www.docker.com/products/docker-desktop/) first. In PowerShell, download the Windows helper script:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.bat -OutFile ukrab-uploader.bat
```

Run it with your API key and the path to one `.mkv` file or a directory of `.mkv` files:

```powershell
.\ukrab-uploader.bat <your_api_key> "C:\path\to\movie-or-directory"
```

Uploads are public by default. Add `draft` as the final argument to create drafts instead:

```powershell
.\ukrab-uploader.bat <your_api_key> "C:\path\to\movie-or-directory" draft
```

The named form also works: `.\ukrab-uploader.bat --api-key <your_api_key> --input "C:\path\to\movie-or-directory" --visibility draft`. Add `--verbose` to either form for detailed output (or `--no-verbose` to explicitly select concise output).

## Run With Docker Directly

Pull the published image:

```bash
docker pull ghcr.io/sirko-ua/audio-bucket-uploader:latest
```

Minimum required parameters:

```bash
docker run --rm \
  -v /path/to/movies:/input:ro \
  ghcr.io/sirko-ua/audio-bucket-uploader:latest \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader
```

This uses the default `--input /input`, so the uploader scans the mounted movie directory.

Full version with all available parameters:

```bash
docker run --rm \
  -v /path/to/movies:/input:ro \
  -v /path/to/extracted:/output \
  -v /path/to/state:/state \
  ghcr.io/sirko-ua/audio-bucket-uploader:latest \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader \
  --input /input \
  --audio-language uk \
  --subtitle-language all \
  --output-dir /output \
  --state-dir /state \
  --keep-extracted \
  --visibility public \
  --verbose
```

## Run Locally

Needs Python 3.10 or newer, plus `mediainfo` and `mkvtoolnix` (`mkvmerge`, `mkvextract`) on the `PATH`. Install the Python dependencies first:

```bash
python -m pip install -r requirements.txt
```

Minimum required parameters:

```bash
python -m uploader \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader
```

Full version:

```bash
python -m uploader \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader \
  --input /media/movies \
  --audio-language uk,en \
  --subtitle-language all \
  --output-dir ./extracted \
  --keep-extracted \
  --visibility public \
  --verbose
```
