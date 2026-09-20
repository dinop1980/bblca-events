#!/usr/bin/env python3
"""Convert public BBLCA calendar appointments into rolling RSS and iCalendar feeds."""

import argparse
import hashlib
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


CALENDAR_URL = "https://bblca.cincwebaxis.com/bblca/calendar/"
FETCH_ATTEMPTS = 6
FETCH_RETRY_SECONDS = 10 * 60
RETRYABLE_HTTP_STATUS = {404, 408, 429}
EASTERN = ZoneInfo("America/New_York")
APPOINTMENT = re.compile(
    r'this\.AddAppointment\("(?P<id>[^"\r\n]+)"\s*,\s*'
    r'new Date\((?P<when>[\d,\s]+)\)\s*,\s*'
    r'(?P<duration>\d+)\s*,\s*\[[^\]]*\]\s*,\s*'
    r'"(?P<title>(?:\\.|[^"\\])*)"',
)
JS_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.DOTALL)


def fetch_calendar_page(
    url=CALENDAR_URL,
    attempts=FETCH_ATTEMPTS,
    retry_seconds=FETCH_RETRY_SECONDS,
    opener=urlopen,
    sleeper=time.sleep,
):
    """Fetch the public calendar, retrying temporary availability failures."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; BBLCAEventsRSS/1.0)"
    })
    for attempt in range(1, attempts + 1):
        try:
            with opener(request, timeout=35) as response:
                return response.read().decode("utf-8")
        except HTTPError as error:
            retryable = (
                error.code in RETRYABLE_HTTP_STATUS
                or 500 <= error.code <= 599
            )
            if not retryable or attempt == attempts:
                raise
            failure = f"HTTP {error.code}: {error.reason}"
        except (URLError, TimeoutError, ConnectionError) as error:
            if attempt == attempts:
                raise
            failure = str(getattr(error, "reason", error))

        print(
            f"Calendar request failed ({failure}) on attempt {attempt}/{attempts}; "
            f"retrying in {retry_seconds // 60} minutes...",
            flush=True,
        )
        sleeper(retry_seconds)

    raise RuntimeError("Calendar fetch retry loop ended unexpectedly")


def decode_js_string(value):
    def replace(match):
        token = match.group(1)
        if token.startswith(("u", "x")) and len(token) in (3, 5):
            return chr(int(token[1:], 16))
        return {"n": "\n", "r": "\r", "t": "\t"}.get(token, token)

    return html.unescape(JS_ESCAPE.sub(replace, value))


def parse_events(page):
    events = []
    for match in APPOINTMENT.finditer(page):
        date_parts = [int(part.strip()) for part in match.group("when").split(",")]
        if not 3 <= len(date_parts) <= 7:
            raise ValueError("Unexpected appointment date in calendar")
        year, js_month, day, hour, minute, second, millisecond = (
            date_parts + [0] * (7 - len(date_parts))
        )
        start = datetime(
            year, js_month + 1, day, hour, minute, second,
            millisecond * 1000, tzinfo=EASTERN,
        )
        end = (start.astimezone(timezone.utc) + timedelta(
            milliseconds=int(match.group("duration"))
        )).astimezone(EASTERN)
        events.append({
            "id": match.group("id"),
            "title": decode_js_string(match.group("title")),
            "start": start,
            "end": end,
        })
    if not events:
        raise ValueError("No appointments found. The calendar format may have changed.")
    return events


def event_fingerprint(event):
    value = "\0".join((
        event["id"], event["title"], event["start"].isoformat(),
        event["end"].isoformat(),
    ))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def format_event_time(event):
    start, end = event["start"], event["end"]
    date = f'{start.strftime("%A, %B")} {start.day}, {start.year}'
    first_time = f'{start.hour % 12 or 12}:{start.minute:02} {start.strftime("%p")}'
    last_time = f'{end.hour % 12 or 12}:{end.minute:02} {end.strftime("%p")}'
    if start.date() == end.date():
        return f"{date}, {first_time} to {last_time} Eastern time"
    end_date = f'{end.strftime("%A, %B")} {end.day}, {end.year}'
    return f"{date}, {first_time} to {end_date}, {last_time} Eastern time"


def build_feed(events, state, now):
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    for name, value in (
        ("title", "Big Bass Lake Community Events"),
        ("link", CALENDAR_URL),
        ("description", "Upcoming events from the public BBLCA calendar."),
        ("language", "en-us"),
    ):
        ET.SubElement(channel, name).text = value
    last_build_date = ET.SubElement(channel, "lastBuildDate")

    for event in sorted(events, key=lambda item: (item["start"], item["id"])):
        if event["end"] <= now:
            continue
        fingerprint = event_fingerprint(event)
        existing = state.get(event["id"], {})
        if existing.get("fingerprint") != fingerprint:
            existing = {
                "fingerprint": fingerprint,
                "published": now.isoformat(),
                "sequence": int(existing.get("sequence", 0)) + 1 if existing else 0,
            }
            state[event["id"]] = existing

        item = ET.SubElement(channel, "item")
        start = event["start"]
        title = f'{start.strftime("%b")} {start.day}, {start.year}: {event["title"]}'
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = CALENDAR_URL
        ET.SubElement(item, "description").text = format_event_time(event)
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = (
            f'bblca-calendar-event:{event["id"]}'
        )
        ET.SubElement(item, "pubDate").text = format_datetime(
            datetime.fromisoformat(existing["published"])
        )

    last_change = max(
        (datetime.fromisoformat(entry["published"]) for entry in state.values()),
        default=now,
    )
    last_build_date.text = format_datetime(last_change)
    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def ical_text(value):
    return (value.replace("\\", "\\\\").replace("\r\n", "\n")
            .replace("\r", "\n").replace("\n", "\\n")
            .replace(";", "\\;").replace(",", "\\,"))


def fold_ical_lines(lines):
    """Fold iCalendar content lines at 75 UTF-8 octets, using CRLF endings."""
    folded = []
    for line in lines:
        current = ""
        width = 0
        for char in line:
            length = len(char.encode("utf-8"))
            if width + length > 75:
                folded.append(current)
                current = " " + char
                width = 1 + length
            else:
                current += char
                width += length
        folded.append(current)
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")


def build_ical(events, state, now):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Community Calendar Mirror//BBLCA Events//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Big Bass Lake Community Events",
    ]
    for event in sorted(events, key=lambda item: (item["start"], item["id"])):
        if event["end"] <= now:
            continue
        record = state[event["id"]]
        modified = datetime.fromisoformat(record["published"]).astimezone(timezone.utc)
        stamp = modified.strftime("%Y%m%dT%H%M%SZ")
        uid = uuid5(NAMESPACE_URL, CALENDAR_URL + "#" + event["id"])
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:urn:uuid:{uid}",
            f"DTSTAMP:{stamp}",
            f"LAST-MODIFIED:{stamp}",
            f"SEQUENCE:{record.get('sequence', 0)}",
            "DTSTART:" + event["start"].astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "DTEND:" + event["end"].astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "SUMMARY:" + ical_text(event["title"]),
            "DESCRIPTION:" + ical_text("See the BBLCA calendar for current details: " + CALENDAR_URL),
            "URL:" + CALENDAR_URL,
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return fold_ical_lines(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Use a saved HTML page for local checks")
    parser.add_argument("--output", type=Path, default=Path("docs/feed.xml"))
    parser.add_argument("--ics-output", type=Path, default=Path("docs/events.ics"))
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument(
        "--exclude-title", action="append", default=[],
        help="Exclude titles containing this text; can be repeated",
    )
    args = parser.parse_args()

    if args.input:
        page = args.input.read_text(encoding="utf-8")
    else:
        page = fetch_calendar_page()

    events = parse_events(page)
    exclusions = tuple(term.casefold() for term in args.exclude_title)
    events = [event for event in events if not any(
        term in event["title"].casefold() for term in exclusions
    )]
    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    now = datetime.now(EASTERN)
    feed = build_feed(events, state, now)
    calendar = build_ical(events, state, now)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.ics_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(feed)
    args.ics_output.write_bytes(calendar)
    args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    count = len(ET.fromstring(feed).find("channel").findall("item"))
    print(f"Published {count} upcoming events to {args.output} and {args.ics_output}")


if __name__ == "__main__":
    main()
