"""Self-check for history, retry, and failure isolation: python test_resilience.py"""
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx

from uploader import __main__ as app
from uploader import history as history_module
from uploader.history import FILE_ITEM, GIVEN_UP, MAX_ATTEMPTS, History, file_fingerprint

app.time.sleep = lambda _seconds: None  # no real backoff in the check
# quiet check output (history.py bound log_event at import, so silence it too)
app.log_event = app.log_request = history_module.log_event = lambda *args, **kwargs: None

URL = "https://example.test/api/uploader"
CHECK_URL = app.hash_check_url(URL)
REQUEST = httpx.Request("POST", URL)

OPEN_HISTORIES: list[History] = []


def open_history(state_dir: Path, filters: str) -> History:
    history = History(state_dir, filters)
    OPEN_HISTORIES.append(history)
    return history


def response(status: int, **kwargs) -> httpx.Response:
    return httpx.Response(status, request=REQUEST, **kwargs)


def responder(*results):
    """A send() that yields each result in turn: a Response or an exception."""
    remaining = list(results)

    def send() -> httpx.Response:
        result = remaining.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    send.remaining = remaining
    return send


def send(sender, url: str = URL, already_done=None) -> httpx.Response:
    return app.send_with_retry("label", url, sender, verbose=False, already_done=already_done)


def reset_http_state() -> None:
    app._consecutive_server_errors.clear()
    app._successful_requests = 1  # pretend the run already reached the server once


def raises(exception_type, call):
    try:
        call()
    except exception_type as exc:
        return exc
    raise AssertionError(f"expected {exception_type.__name__}")


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)

    # --- history -------------------------------------------------------------
    source = root / "movie.mkv"
    source.write_bytes(b"x")
    fingerprint = file_fingerprint(source)
    history = open_history(root / "state", filters='["uk"]')

    assert history.check(source, FILE_ITEM, fingerprint) is None, "unknown file must be processed"

    history.mark(source, "audio:1", fingerprint, "uploaded", "42")
    assert history.check(source, "audio:1", fingerprint), "uploaded track must be skipped"
    assert history.check(source, "audio:2", fingerprint) is None, "other tracks unaffected"

    # An edited file gets a new fingerprint, so its rows no longer apply.
    source.write_bytes(b"changed content")
    changed = file_fingerprint(source)
    assert changed != fingerprint
    assert history.check(source, "audio:1", changed) is None, "edited file must be reprocessed"

    # Filter-dependent statuses expire when the language filters change.
    history.mark(source, FILE_ITEM, changed, "done", "no matching tracks")
    assert history.check(source, FILE_ITEM, changed), "done file must be skipped"
    other = open_history(root / "state", filters='["en"]')
    assert other.check(source, FILE_ITEM, changed) is None, "new filters must reprocess"
    # ...but a track already on the server stays skipped whatever the filters are.
    other.mark(source, "audio:9", changed, "duplicate")
    assert other.check(source, "audio:9", changed), "duplicate must stay skipped"

    # `--audio-language uk,en` and `en,uk` are the same request, same history key.
    assert app.filters_key({"audio": ["uk", "en"]}) == app.filters_key({"audio": ["en", "uk"]})

    # Failures are retried, then given up on — and a give-up is reported as a
    # failure, never as a quiet skip.
    for attempt in range(1, MAX_ATTEMPTS + 1):
        history.record_failure(source, "audio:3", changed, "extract", "boom")
        entry = history.check(source, "audio:3", changed)
        if attempt < MAX_ATTEMPTS:
            assert entry is None, f"attempt {attempt} must be retried, got {entry!r}"
        else:
            assert entry and entry[0] == GIVEN_UP, entry
    lines = history.failures_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == MAX_ATTEMPTS, lines
    assert json.loads(lines[0])["stage"] == "extract"

    # A given-up item is counted as failed, logged at ERROR, and makes the exit
    # code non-zero — the previous behaviour reported "0 failed" and exit 0.
    stats = app.Stats()
    assert app.skipped_by_history(history, stats, source, changed, "audio:3", verbose=False)
    assert (stats.given_up, stats.resumed) == (1, 0), stats

    # A file that cannot even be stat()ed (a dangling symlink, an unmounted
    # disk) still has to reach the history check, or it is retried forever and
    # never given up on.
    missing = root / "gone.mkv"
    assert app.safe_fingerprint(missing) == ""
    for _ in range(MAX_ATTEMPTS):
        history.record_failure(missing, FILE_ITEM, app.safe_fingerprint(missing), "process", "gone")
    entry = history.check(missing, FILE_ITEM, app.safe_fingerprint(missing))
    assert entry and entry[0] == GIVEN_UP, entry

    # --retry-failed brings it back.
    assert history.clear_failures() >= 1
    assert history.check(source, "audio:3", changed) is None, "--retry-failed must retry it"

    # A success clears the attempt count.
    history.record_failure(source, "audio:4", changed, "extract", "boom")
    history.mark(source, "audio:4", changed, "uploaded", "7")
    assert history.check(source, "audio:4", changed)[0] == "final"

    # A file name holding a byte that is not valid UTF-8 (a cp1252 name off a
    # NAS) reaches us as a lone surrogate. sqlite raises UnicodeEncodeError on
    # it — a ValueError, not a sqlite3.Error — so it used to escape from inside
    # the failure handler itself: the file could not even be recorded as failed,
    # and the whole library stopped uploading.
    odd_name = Path(str(root / "caf\udce9.mkv"))
    history.record_failure(odd_name, FILE_ITEM, "1:2", "process", f"broken {odd_name}")
    for _ in range(MAX_ATTEMPTS - 1):
        history.record_failure(odd_name, FILE_ITEM, "1:2", "process", "broken")
    entry = history.check(odd_name, FILE_ITEM, "1:2")
    assert entry and entry[0] == GIVEN_UP, entry
    assert "caf" in history.failures_path.read_text(encoding="utf-8")

    # --- the history must never be able to kill the run ----------------------
    broken = open_history(root / "state2", filters='["uk"]')
    broken._connection.close()  # every write now raises sqlite3.ProgrammingError
    broken.mark(source, FILE_ITEM, changed, "uploaded")  # must not raise
    broken.record_failure(source, FILE_ITEM, changed, "upload", "boom")  # must not raise
    assert broken.check(source, FILE_ITEM, changed) is None
    assert broken.failures_path.exists(), "the jsonl failure log still has to be written"

    # A corrupt database is quarantined instead of bricking every future run.
    corrupt_dir = root / "state3"
    corrupt_dir.mkdir()
    (corrupt_dir / "history.sqlite3").write_bytes(b"this is not a database")
    recovered = open_history(corrupt_dir, filters='["uk"]')
    recovered.mark(source, FILE_ITEM, changed, "done")
    assert recovered.check(source, FILE_ITEM, changed), "must work after quarantining"
    assert (corrupt_dir / "history.sqlite3.corrupt").exists()

    # ...but an environment problem (full disk, read-only mount, unopenable
    # path) raises sqlite3.OperationalError — a SUBCLASS of DatabaseError — and
    # must NOT be treated as corruption: quarantining a healthy history there
    # would throw away the very thing it exists for. It degrades to "no resume".
    blocked_db = root / "state5"
    (blocked_db / "history.sqlite3").mkdir(parents=True)  # sqlite cannot open this
    degraded = History(blocked_db, filters='["uk"]')
    assert not (blocked_db / "history.sqlite3.corrupt").exists(), "must not quarantine on an env error"
    degraded.mark(source, FILE_ITEM, changed, "done")
    assert degraded.check(source, FILE_ITEM, changed) is None
    degraded.close()

    # An unusable state dir degrades to "no resume", never to "no run".
    (root / "a-file").write_bytes(b"not a directory")
    blocked = History(root / "a-file" / "state", filters='["uk"]')
    blocked.mark(source, FILE_ITEM, changed, "uploaded")
    assert blocked.check(source, FILE_ITEM, changed) is None

    # --- http retry / stop conditions ----------------------------------------
    reset_http_state()

    ok = responder(httpx.ConnectError("refused"), response(503), response(200, json={"exists": False}))
    assert send(ok).status_code == 200, "transient errors must be retried"
    assert not ok.remaining

    # A per-file rejection is not retried and does not stop the run.
    bad = responder(response(422, text="bad payload"))
    assert "422" in str(raises(app.UploaderError, lambda: send(bad)))
    assert not bad.remaining, "4xx must not be retried"

    # A rejected key is never one file's answer: nothing will ever succeed.
    raises(app.ServerDown, lambda: send(responder(response(401))))
    reset_http_state()

    # 403/404 only prove a wrong --api-url or key while nothing has worked yet.
    # Once a request has succeeded they are this file's answer ("no release for
    # this unique_id"), and the run must carry on — stopping would also stall
    # every restart on the same file, since a stopped run records no failure.
    for status in (403, 404):
        raises(app.UploaderError, lambda: send(responder(response(status, text="no release"))))
        app._successful_requests = 0
        raises(app.ServerDown, lambda: send(responder(response(status))))
        reset_http_state()

    # An unreachable server stops the run: the first file's failure could still
    # be about that file (a proxy resetting one oversized body looks identical),
    # so it takes a few in a row before we call the server down.
    reset_http_state()
    for _ in range(app.MAX_CONSECUTIVE_UNREACHABLE - 1):
        unreachable = responder(*[httpx.ConnectError("refused")] * app.HTTP_ATTEMPTS)
        raises(app.UploaderError, lambda: send(unreachable))
        assert not unreachable.remaining, "every attempt must be used"
    raises(app.ServerDown, lambda: send(
        responder(*[httpx.ConnectError("refused")] * app.HTTP_ATTEMPTS)
    ))

    # A 5xx proves the server is answering, so a folder of payloads it chokes on
    # is not an outage: it takes far more of them in a row to stop the run than
    # it takes answers that never arrive at all.
    assert app.MAX_CONSECUTIVE_SERVER_ERRORS > app.MAX_CONSECUTIVE_UNREACHABLE

    # 5xx is per-file at first; the run stops once one endpoint fails repeatedly.
    reset_http_state()
    for _ in range(app.MAX_CONSECUTIVE_SERVER_ERRORS - 1):
        raises(app.UploaderError, lambda: send(responder(*[response(500)] * app.HTTP_ATTEMPTS)))
    raises(app.ServerDown, lambda: send(responder(*[response(500)] * app.HTTP_ATTEMPTS)))

    # ...and a healthy hash-check endpoint must NOT reset the upload endpoint's
    # count: every upload is preceded by a hash check, so that reset made a dead
    # upload endpoint undetectable and re-POSTed every track body 5x, forever.
    reset_http_state()
    for _ in range(app.MAX_CONSECUTIVE_SERVER_ERRORS - 1):
        send(responder(response(200, json={"exists": False})), url=CHECK_URL)
        raises(app.UploaderError, lambda: send(responder(*[response(500)] * app.HTTP_ATTEMPTS)))
    send(responder(response(200, json={"exists": False})), url=CHECK_URL)
    raises(app.ServerDown, lambda: send(responder(*[response(500)] * app.HTTP_ATTEMPTS)))

    # A success on the SAME endpoint does reset it, so scattered 5xx never stop.
    reset_http_state()
    raises(app.UploaderError, lambda: send(responder(*[response(500)] * app.HTTP_ATTEMPTS)))
    send(responder(response(200)))
    assert app._consecutive_server_errors[URL] == 0

    # A 4xx is this file's answer ("no release for this unique_id"), and a real
    # library produces long runs of them. However many arrive, the run carries
    # on: only the server being down may stop it.
    reset_http_state()
    for _ in range(25):
        raises(app.UploaderError, lambda: send(responder(response(404, text="no release"))))

    # Retry-After is obeyed, not clamped down: a server asking for an hour used
    # to be retried after 60s, five times, per file.
    reset_http_state()
    throttled = response(429, headers={"Retry-After": "7"})
    assert app.retry_after_seconds(
        httpx.HTTPStatusError("", request=REQUEST, response=throttled)
    ) == 7.0
    # An unparseable Retry-After falls back to our own backoff.
    assert app.retry_after_seconds(
        httpx.HTTPStatusError("", request=REQUEST, response=response(429, headers={"Retry-After": "soon"}))
    ) is None

    # An upload whose answer was lost is NOT sent again: before retrying we ask
    # whether it landed. Otherwise the track gets published five times. The lost
    # answer can be a timeout, a dropped connection, OR a 502/504 from a proxy
    # sitting in front of a backend that already stored the file.
    for lost in (
        httpx.ReadTimeout("no answer"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.HTTPStatusError("", request=REQUEST, response=response(504, text="gateway timeout")),
        httpx.HTTPStatusError("", request=REQUEST, response=response(502, text="bad gateway")),
    ):
        reset_http_state()
        landed = responder(lost, response(200, json={"id": 1}))
        raises(app.AlreadyPublished, lambda: send(landed, already_done=lambda: True))
        assert landed.remaining, f"{type(lost).__name__}: body must not be re-sent once it landed"

    # ...and when it did not land, the retry proceeds normally.
    reset_http_state()
    retried = responder(response(503), response(200, json={"id": 1}))
    assert send(retried, already_done=lambda: False).status_code == 200
    assert not retried.remaining

    # When the "did it land?" check itself fails, the body must NOT be re-sent —
    # re-sending is exactly what publishes it twice — and the failure must still
    # be counted, or a dead endpoint could never be detected.
    def broken_check() -> bool:
        raise app.UploaderError("hash check for x: HTTP 400")

    reset_http_state()
    for _ in range(app.MAX_CONSECUTIVE_SERVER_ERRORS - 1):
        unconfirmable = responder(response(502), response(200, json={"id": 1}))
        raises(app.UploaderError, lambda: send(unconfirmable, already_done=broken_check))
        assert unconfirmable.remaining, "an unconfirmable body must not be re-sent"
    raises(app.ServerDown, lambda: send(
        responder(response(502), response(200)), already_done=broken_check
    ))

    # A 500 is emitted by the application and says nothing about whether its
    # write already committed, so it is probed like a 502/504 — a server that
    # stores the body and then 500s must not have it published five times.
    for status in (500, 502, 503, 504):
        reset_http_state()
        probes = []
        sender = responder(response(status), response(200, json={"id": 1}))
        send(sender, already_done=lambda: probes.append(1) or False)
        assert probes, f"a {status} must be probed before the body is re-sent"

    # A 429 is the one exemption: the body was refused outright, and probing the
    # rate limiter again is precisely what it told us not to do.
    reset_http_state()
    probes = []
    send(responder(response(429), response(200, json={"id": 1})),
         already_done=lambda: probes.append(1) or False)
    assert not probes, "a 429 must not trigger a hash-check probe"

    # A connection that dies without an answer is this file's failure first: a
    # proxy resetting an oversized body looks exactly like an unreachable host,
    # and stopping on the first one would halt a healthy server — and, since a
    # stopped run records no failure, replay that same file on every restart.
    reset_http_state()
    for _ in range(app.MAX_CONSECUTIVE_UNREACHABLE - 1):
        raises(app.UploaderError, lambda: send(
            responder(*[httpx.ReadError("connection reset")] * app.HTTP_ATTEMPTS),
            already_done=lambda: False,
        ))
    raises(app.ServerDown, lambda: send(
        responder(*[httpx.ReadError("connection reset")] * app.HTTP_ATTEMPTS),
        already_done=lambda: False,
    ))

    # Retry-After: seconds, a float, or an HTTP date — all valid HTTP. Reading
    # only integers let the long ones slip past the "back off too long" stop.
    reset_http_state()
    for header in ("3600", "3600.0", format_datetime(datetime.now(timezone.utc) + timedelta(hours=1))):
        long_wait = response(429, headers={"Retry-After": header})
        stop = raises(app.ServerDown, lambda: send(responder(
            httpx.HTTPStatusError("", request=REQUEST, response=long_wait)
        )))
        assert "back off" in str(stop), (header, stop)

    # A malformed --api-url fails at startup, not once per file (httpx raises
    # InvalidURL/UnsupportedProtocol, which are not HTTPError and so bypass the
    # retry classification entirely).
    app.check_api_url("https://audio-bucket.site/api/uploader")
    app.check_api_url("http://127.0.0.1:8000/api/uploader")
    for bad_url in ("audio-bucket.site/api", "ftp://host/api", "https:///api", "https://host:port/api"):
        raises(app.UploaderError, lambda: app.check_api_url(bad_url))

    # The progress bar is the request body httpx reads from, so it must not be
    # able to fail an upload when stdout is gone (a full disk, a closed pipe).
    class DeadStdout:
        def write(self, _text):
            raise OSError("no space left on device")

        def flush(self):
            raise OSError("no space left on device")

    real_stdout, sys.stdout = sys.stdout, DeadStdout()
    try:
        payload = root / "payload.bin"
        payload.write_bytes(b"abc" * 1000)
        with payload.open("rb") as handle:
            progress = app.ProgressFile(handle, payload.stat().st_size, "payload.bin")
            assert progress.read(1000), "reading the body must survive a dead stdout"
            progress.finish()
            progress.close_line()
    finally:
        sys.stdout = real_stdout

    # --- extraction falls back to one track at a time ------------------------
    good = app.PreparedTrack(1, "1", "audio", "uk", {}, "", root / "good.aac")
    damaged = app.PreparedTrack(2, "2", "audio", "uk", {}, "", root / "damaged.aac")

    def fake_extract(command: list[str]) -> None:
        targets = command[3:]
        if any(str(damaged.output_path) in target for target in targets):
            raise app.UploaderError("mkvextract failed (exit 2): damaged track")
        for target in targets:
            Path(target.split(":", 1)[1]).write_bytes(b"data")

    app.run_extract_command = fake_extract
    failures = app.extract_tracks(root / "movie.mkv", [good, damaged])
    assert [track for track, _ in failures] == [damaged], failures
    assert good.output_path.read_bytes() == b"data", "a broken track must not sink its siblings"

    # An empty output file is a failure even when mkvextract claims success.
    app.run_extract_command = lambda command: Path(command[3].split(":", 1)[1]).write_bytes(b"")
    empty = app.PreparedTrack(3, "3", "audio", "uk", {}, "", root / "empty.aac")
    assert app.extract_tracks(root / "movie.mkv", [empty]), "empty extraction must fail"

    # A file mkvmerge cannot read must fail loudly, not look like "no matching
    # tracks" (mkvmerge exits 0 and reports it in the payload).
    raises(app.UploaderError, lambda: app.check_container_readable(
        root / "movie.mkv", {"container": {"recognized": False}}
    ))
    raises(app.UploaderError, lambda: app.check_container_readable(
        root / "movie.mkv", {"errors": ["Could not open the file"]}
    ))
    app.check_container_readable(root / "movie.mkv", {"container": {"recognized": True, "supported": True}})

    # A storage blip (spun-down disk, NAS reconnect, a file still being written)
    # must not burn one of MAX_ATTEMPTS: the probe is retried before giving up.
    attempts = []

    def flaky_probe():
        attempts.append(1)
        if len(attempts) < 3:
            raise app.UploaderError("could not be opened for reading")
        return "payload"

    assert app.retry_probe("movie.mkv", flaky_probe) == "payload"
    assert len(attempts) == 3, attempts
    raises(app.UploaderError, lambda: app.retry_probe(
        "movie.mkv", lambda: (_ for _ in ()).throw(app.UploaderError("damaged"))
    ))

    # A missing tool cannot fix itself: retrying it would add a wait to every
    # file in the library.
    tool_probes = []
    raises(app.MissingTool, lambda: app.retry_probe("movie.mkv", lambda: (
        tool_probes.append(1), (_ for _ in ()).throw(app.MissingTool("no mediainfo"))
    )))
    assert len(tool_probes) == 1, tool_probes

    # A variable-frame-rate video carries no FrameRate in its headers, and the
    # endpoint rejects the whole file without one. A full parse computes it.
    VFR = {"media": {"track": [{"@type": "Video", "FrameRate_Mode": "VFR"}]}}
    DEEP = {"media": {"track": [{"@type": "Video", "FrameRate": "25.875"}]}}
    assert not app.has_video_frame_rate(VFR)
    assert app.has_video_frame_rate(DEEP)

    app.run_json_command = lambda command, timeout=None: DEEP
    app.run_text_command = lambda command, timeout=None: "deep text"
    assert app.add_missing_video_frame_rate(root / "m.mkv", VFR, "text") == (DEEP, "deep text")
    # Already has one: no full parse at all (it would read the whole file).
    app.run_json_command = lambda command, timeout=None: (_ for _ in ()).throw(
        AssertionError("must not reparse a file that already reports a frame rate")
    )
    assert app.add_missing_video_frame_rate(root / "m.mkv", DEEP, "text") == (DEEP, "text")
    # A full parse that still finds nothing must not fail the file.
    app.run_json_command = lambda command, timeout=None: VFR
    app.run_text_command = lambda command, timeout=None: "still nothing"
    assert app.add_missing_video_frame_rate(root / "m.mkv", VFR, "text") == (VFR, "text")
    app.run_json_command = lambda command, timeout=None: (_ for _ in ()).throw(
        app.UploaderError("mediainfo timed out")
    )
    assert app.add_missing_video_frame_rate(root / "m.mkv", VFR, "text") == (VFR, "text")

    for open_entry in OPEN_HISTORIES:
        open_entry.close()
    broken.close()
    blocked.close()

print("OK: history, retry, and extraction-fallback checks passed")
