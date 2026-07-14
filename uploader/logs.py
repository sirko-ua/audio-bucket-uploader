"""Console logging, shared by the uploader and its history."""
from __future__ import annotations

from datetime import datetime


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
    if not verbose:
        return
    try:
        print(format_log_line(level, target, action, explanation))
    except (OSError, UnicodeError):
        # A closed pipe, a full disk, or a file name holding a byte the console
        # cannot encode must not sink the run.
        pass


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


def log_table(
    title: str,
    headers: list[str],
    rows: list[list[object]],
    *,
    verbose: bool = True,
    target: str = "summary",
    action: str = "report",
) -> None:
    if not verbose or not rows:
        return
    log_event("INFO", target, action, title)
    try:
        print(format_table(headers, rows))
    except (OSError, UnicodeError):
        # Same reason as log_event: a table full of file paths must not sink the run.
        pass
