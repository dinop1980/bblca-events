"""Read a one-time historical appointment backfill from the public CINC calendar."""

import calendar
import re
import time
from datetime import date, datetime, timedelta

from make_feed import parse_events


CALENDAR_URL = "https://bblca.cincwebaxis.com/bblca/calendar/"
VIEW_MONTH = "#schCalendar_viewSelectorBlock_ctl00_viewSelectorMenu_DXI3_"
DATE_BUTTON = "#schCalendar_viewNavigatorBlock_ctl00_BG_GTDBI"
DATE_POPUP = "#schCalendar_viewNavigatorBlock_ctl00_calendarPopupDiv"
DATE_PICKER = "#schCalendar_viewNavigatorBlock_ctl00_gotodateCalendar"
DATE_PICKER_MONTH = DATE_PICKER + "_T"
DATE_PICKER_PREVIOUS_MONTH = DATE_PICKER + "_PMC"
DATE_PICKER_NEXT_MONTH = DATE_PICKER + "_NMC"
DATE_PICKER_DAYS = DATE_PICKER + " td.dxeCalendarDay:not(.dxeCalendarOtherMonth)"
VIEW_INTERVAL = "#schCalendar_viewVisibleIntervalBlock_ctl00_mainCell"
MONTH_GRID_DATES = "#schCalendar td.dxscDateCellHeader[title]"
APPOINTMENT_SCRIPT = re.compile(r"\bdxo\.AddAppointment\(")
SELECTION_DATE = re.compile(r"SetSelectionInternal\(new Date\((\d+),(\d+),(\d+)")
MONTH_YEAR = re.compile(r"^([A-Za-z]+)\s+(\d{4})$")
RANGE_INTERVAL = re.compile(
    r"^([A-Za-z]+)(?:\s*,\s*(\d{4}))?\s*[–-]\s*"
    r"([A-Za-z]+)\s*,\s*(\d{4})$"
)


def _month_number(name):
    for number in range(1, 13):
        if calendar.month_name[number].casefold() == name.casefold():
            return number
    raise ValueError(f"Unrecognized calendar month: {name}")


def parse_month_year(value):
    match = MONTH_YEAR.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Unexpected CINC date-picker month: {value!r}")
    return int(match.group(2)), _month_number(match.group(1))


def parse_visible_interval(value):
    """Return the first and last (year, month) displayed in Month view."""
    value = value.strip()
    match = RANGE_INTERVAL.fullmatch(value)
    if match:
        first_name, first_year, last_name, last_year = match.groups()
        last_year = int(last_year)
        first_month = _month_number(first_name)
        last_month = _month_number(last_name)
        first_year = int(first_year) if first_year else last_year
        if not match.group(2) and first_month > last_month:
            first_year -= 1
        return (first_year, first_month), (last_year, last_month)

    one_month = MONTH_YEAR.fullmatch(value)
    if one_month:
        year, month = int(one_month.group(2)), _month_number(one_month.group(1))
        return (year, month), (year, month)
    raise ValueError(f"Unexpected CINC Month view interval: {value!r}")


def _next_month(year_month):
    year, month = year_month
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _previous_month(year_month):
    year, month = year_month
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _current_payload(page, expected_date):
    scripts = page.locator("script").all_text_contents()
    candidates = [
        script for script in scripts
        if APPOINTMENT_SCRIPT.search(script)
        and (selection := SELECTION_DATE.search(script))
        and tuple(map(int, selection.groups()))
        == (expected_date.year, expected_date.month - 1, expected_date.day)
    ]
    if len(candidates) != 1:
        raise ValueError(
            "Expected exactly one active CINC Month view appointment payload; "
            f"found {len(candidates)}"
        )

    events = parse_events(candidates[0])
    rendered = page.locator(".dxscApt").count()
    if rendered != len(events):
        raise ValueError(
            "CINC Month view payload does not match rendered appointments: "
            f"{len(events)} in script versus {rendered} rendered"
        )
    return events, rendered


def validate_month_grid_dates(titles, allowed_missing_date=None):
    try:
        dates = [datetime.strptime(title, "%d %B %Y").date() for title in titles]
    except ValueError as error:
        raise ValueError("CINC Month view date headers changed format") from error

    if len(dates) == 41 and allowed_missing_date:
        gaps = [
            (previous + timedelta(days=1), current)
            for previous, current in zip(dates, dates[1:])
            if current != previous + timedelta(days=1)
        ]
        if len(gaps) == 1 and gaps[0][0] == allowed_missing_date:
            dates.insert(dates.index(gaps[0][1]), allowed_missing_date)

    if len(dates) != 42 or any(
        current != dates[0] + timedelta(days=index)
        for index, current in enumerate(dates)
    ):
        raise ValueError(
            "CINC Month view must render 42 consecutive date cells; "
            f"found {len(dates)}"
        )
    return dates


def _month_grid_dates(page, allowed_missing_date=None):
    titles = page.locator(MONTH_GRID_DATES).evaluate_all(
        "elements => elements.map(element => element.title)"
    )
    return validate_month_grid_dates(titles, allowed_missing_date)


def _wait_for_window(page, expected_date, allowed_missing_date=None, timeout=60):
    deadline = time.monotonic() + timeout
    last_problem = "calendar view has not loaded"
    while time.monotonic() < deadline:
        try:
            interval_text = page.locator(VIEW_INTERVAL).inner_text().strip()
            interval = parse_visible_interval(interval_text)
            events, rendered = _current_payload(page, expected_date)
            grid_dates = _month_grid_dates(page, allowed_missing_date=allowed_missing_date)
            if expected_date not in grid_dates:
                last_problem = f"selected date {expected_date} is outside the rendered grid"
                time.sleep(0.2)
                continue
            if rendered == 0:
                last_problem = f"no events rendered for {interval_text}"
                time.sleep(0.2)
                continue
            return interval_text, interval, events, rendered, grid_dates
        except (ValueError, TimeoutError) as error:
            last_problem = str(error)
        time.sleep(0.2)
    raise TimeoutError(f"CINC calendar view did not validate within {timeout}s: {last_problem}")


def _choose_date(page, target_date):
    popup = page.locator(DATE_POPUP)
    if not popup.is_visible():
        page.locator(DATE_BUTTON).click()
        popup.wait_for(state="visible", timeout=15000)
    picker_month = page.locator(DATE_PICKER_MONTH)
    target = (target_date.year, target_date.month)
    for _ in range(240):
        current = parse_month_year(picker_month.inner_text())
        if current == target:
            break
        forward = current < target
        control = page.locator(
            DATE_PICKER_NEXT_MONTH if forward else DATE_PICKER_PREVIOUS_MONTH
        )
        control.click()
        expected = _next_month(current) if forward else _previous_month(current)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if parse_month_year(picker_month.inner_text()) == expected:
                break
            time.sleep(0.05)
        else:
            raise TimeoutError(f"CINC date picker did not move from {current} to {expected}")
    else:
        raise ValueError(f"Could not navigate CINC date picker to {target}")

    days = page.locator(DATE_PICKER_DAYS)
    if days.count() < target_date.day:
        raise ValueError(f"CINC date picker has no day {target_date.day} in {target}")
    days.nth(target_date.day - 1).click()
    page.wait_for_timeout(500)


def fetch_historical_events(start_date, end_date):
    """Scrape and validate every overlapping Month view from start through end."""
    if end_date < start_date:
        raise ValueError("Historical range end precedes its start")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Historical backfill requires Playwright; install it before the first run"
        ) from error

    all_events = {}
    windows = []
    month_coverage = set()
    target_month = (end_date.year, end_date.month)
    anchors = [start_date]
    month = _next_month((start_date.year, start_date.month))
    while month <= target_month:
        anchors.append(date(month[0], month[1], 1))
        month = _next_month(month)
    previous_grid_end = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=60000)
            page.locator(DATE_BUTTON).wait_for(state="visible", timeout=60000)
            page.locator(VIEW_MONTH).click(timeout=15000)
            for anchor in anchors:
                _choose_date(page, anchor)
                interval_text, interval, events, rendered, grid_dates = _wait_for_window(
                    page, expected_date=anchor, allowed_missing_date=end_date
                )
                grid_start, grid_end = grid_dates[0], grid_dates[-1]
                if previous_grid_end and grid_start > previous_grid_end + timedelta(days=1):
                    raise ValueError(
                        "CINC historical windows leave a gap between "
                        f"{previous_grid_end} and {grid_start}"
                    )
                if previous_grid_end and grid_end <= previous_grid_end:
                    raise ValueError(
                        f"CINC historical window did not advance beyond {previous_grid_end}"
                    )
                previous_grid_end = grid_end

                in_range = [
                    event for event in events
                    if start_date <= event["start"].date() <= end_date
                ]
                for event in in_range:
                    prior = all_events.get(event["id"])
                    if prior and (
                        prior["title"], prior["start"], prior["end"]
                    ) != (event["title"], event["start"], event["end"]):
                        raise ValueError(
                            f"Conflicting appointment data for CINC ID {event['id']} "
                            "in overlapping Month view windows"
                        )
                    all_events[event["id"]] = event
                    month_coverage.add((event["start"].year, event["start"].month))

                windows.append({
                    "anchor": anchor.isoformat(),
                    "interval": interval_text,
                    "grid_start": grid_start.isoformat(),
                    "grid_end": grid_end.isoformat(),
                    "payload_count": rendered,
                })
                print(
                    f"Verified CINC Month view at {anchor} ({interval_text}, "
                    f"{grid_start} through {grid_end}): "
                    f"{rendered} appointments, {len(in_range)} in backfill range",
                    flush=True,
                )

            if grid_dates[-1] < end_date:
                raise ValueError(
                    f"The final CINC Month view ends {grid_dates[-1]}, before {end_date}"
                )

            required_months = []
            month = (start_date.year, start_date.month)
            while month <= target_month:
                required_months.append(month)
                month = _next_month(month)
            missing_months = [month for month in required_months if month not in month_coverage]
            if missing_months:
                raise ValueError(
                    "CINC returned no historical appointments in these requested months: "
                    + ", ".join(f"{year:04d}-{month:02d}" for year, month in missing_months)
                )
            if not all_events:
                raise ValueError("CINC returned no appointments for the requested range")
        finally:
            browser.close()

    return sorted(all_events.values(), key=lambda event: (event["start"], event["id"])), {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "event_count": len(all_events),
        "window_count": len(windows),
        "windows": [window["interval"] for window in windows],
    }
