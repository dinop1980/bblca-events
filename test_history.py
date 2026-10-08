import unittest

from datetime import date, timedelta

from cinc_history import (
    parse_month_year,
    parse_visible_interval,
    validate_month_grid_dates,
)
from make_feed import EASTERN, parse_events


class HistoricalCalendarTests(unittest.TestCase):
    def test_date_picker_month_label(self):
        self.assertEqual(parse_month_year("September 2026"), (2026, 9))

    def test_parser_accepts_live_and_historical_cinc_call_forms(self):
        source = r'''<script>
        this.AddAppointment("live_1", new Date(2026,9,8,9), 3600000,
            ['null'], "Live &amp; Public", "01110111", "Occurrence", -1, -1, 0);
        dxo.AddAppointment("past_1", new Date(2025,8,1,9), 7200000,
            ['null'], "Tennis Round Robin", "01110111", "Occurrence", -1, -1, 0);
        dxo.AddAppointment("past_2", new Date(2025,8,1,10), 3600000,
            ['null'], "Board Director\'s Meeting", "01110111", "Normal", -1, -1, 0);
        </script>'''

        events = parse_events(source)

        self.assertEqual([event["id"] for event in events], ["live_1", "past_1", "past_2"])
        self.assertEqual(events[0]["title"], "Live & Public")
        self.assertEqual(events[2]["title"], "Board Director's Meeting")
        self.assertEqual(events[1]["start"].isoformat(), "2025-09-01T09:00:00-04:00")
        self.assertEqual(events[1]["end"].astimezone(EASTERN).isoformat(), "2025-09-01T11:00:00-04:00")

    def test_month_view_intervals_parse_shared_and_explicit_years(self):
        self.assertEqual(
            parse_visible_interval("August – October, 2025"),
            ((2025, 8), (2025, 10)),
        )
        self.assertEqual(
            parse_visible_interval("November, 2025 – January, 2026"),
            ((2025, 11), (2026, 1)),
        )
        self.assertEqual(
            parse_visible_interval("February – March, 2026"),
            ((2026, 2), (2026, 3)),
        )

    def test_month_grid_allows_only_the_missing_current_day_header(self):
        start = date(2026, 8, 30)
        grid = [start + timedelta(days=day) for day in range(42)]
        today = date(2026, 10, 8)
        titles = [day.strftime("%d %B %Y").lstrip("0") for day in grid if day != today]

        self.assertEqual(validate_month_grid_dates(titles, today), grid)
        with self.assertRaises(ValueError):
            validate_month_grid_dates(titles, date(2026, 10, 9))


if __name__ == "__main__":
    unittest.main()
