import csv
import tempfile
import unittest
from pathlib import Path
import sys

from openpyxl import Workbook


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

import app as app_module


class TableSearchHelperTests(unittest.TestCase):
    def test_global_and_column_filters_combine(self):
        rows = [
            ["NP25-10", "BRAIN", "Scanner 2"],
            ["NP25-2", "BRAIN", "Scanner 10"],
            ["NP25-1", "BREAST", "Scanner 2"],
        ]

        matched = app_module._filter_sort_records(
            rows,
            lambda row: row,
            global_query="scanner 2",
            column_filters={1: "brain"},
        )

        self.assertEqual([rows[0]], matched)

    def test_natural_sort_is_stable_and_keeps_blanks_last(self):
        rows = [
            ["Scanner 10", "first"],
            ["", "blank"],
            ["scanner 2", "second"],
            ["Scanner 2", "third"],
        ]

        ascending = app_module._filter_sort_records(
            rows, lambda row: row, sort_column=0
        )
        descending = app_module._filter_sort_records(
            rows, lambda row: row, sort_column=0, sort_direction="desc"
        )

        self.assertEqual(
            ["second", "third", "first", "blank"],
            [row[1] for row in ascending],
        )
        self.assertEqual(
            ["first", "second", "third", "blank"],
            [row[1] for row in descending],
        )

    def test_inventory_filters_and_sorts_before_pagination(self):
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / "inventory.csv"
            with inventory.open("w", encoding="utf-8", newline="") as output:
                writer = csv.writer(output)
                writer.writerow(["Slide", "Organ", "Note"])
                for number in range(1, 121):
                    writer.writerow(
                        [
                            f"Slide {number}",
                            "BRAIN" if number % 2 else "BREAST",
                            "keep",
                        ]
                    )
                writer.writerow(["Slide 200", "BRAIN"])

            (
                headers,
                rows,
                total_rows,
                matching_rows,
                current_page,
                total_pages,
            ) = app_module._read_inventory_page(
                inventory,
                requested_page=2,
                rows_per_page=25,
                global_query="keep",
                column_filters={1: "brain"},
                sort_column=0,
                sort_direction="asc",
            )

        self.assertEqual(["Slide", "Organ", "Note"], headers)
        self.assertEqual(121, total_rows)
        self.assertEqual(60, matching_rows)
        self.assertEqual(2, current_page)
        self.assertEqual(3, total_pages)
        self.assertEqual("Slide 51", rows[0][0])
        self.assertTrue(all(len(row) == len(headers) for row in rows))


class TableSearchRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inventory_directory = self.root / "inventories"
        self.inventory_directory.mkdir()
        self.workbook_path = self.root / "Slide_Digitization_Log.xlsx"
        self.old_inventory_directory = app_module.Config.SCANNER_INVENTORIES
        self.old_sdl_path = app_module.Config.SDL_FILE_PATH
        self.old_users = app_module.user_manager.users.copy()
        app_module.Config.SCANNER_INVENTORIES = str(self.inventory_directory)
        app_module.Config.SDL_FILE_PATH = str(self.workbook_path)

        with (self.inventory_directory / "scanner.csv").open(
            "w", encoding="utf-8", newline=""
        ) as output:
            writer = csv.writer(output)
            writer.writerow(["Slide", "Rack"])
            writer.writerow(["Slide 10", "Rack B"])
            writer.writerow(["Slide 2", "Rack A"])

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = app_module.Config.SDL_SHEET_NAME
        worksheet.append(app_module.SDL_HEADERS)
        for accession, organ, slides in (
            ("NP25-10", "BRAIN", 2),
            ("NP25-2", "BRAIN", 3),
            ("np25-2", "BREAST", 4),
        ):
            row = ["" for _ in app_module.SDL_HEADERS]
            row[0] = accession
            row[1] = organ
            row[3] = slides
            worksheet.append(row)
        workbook.save(self.workbook_path)
        workbook.close()

        self.user = app_module.User("table-user", "", False)
        app_module.user_manager.users[self.user.id] = self.user
        app_module.app.config.update(
            TESTING=True,
            SECRET_KEY="table-search-test",
        )
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = self.user.id
            session["_fresh"] = True

    def tearDown(self):
        app_module.Config.SCANNER_INVENTORIES = self.old_inventory_directory
        app_module.Config.SDL_FILE_PATH = self.old_sdl_path
        app_module.user_manager.users = self.old_users
        self.temporary.cleanup()

    def test_sdl_route_renders_filter_and_sort_state(self):
        response = self.client.get(
            "/sdl?filter_0=NP25-2&sort=0&direction=asc"
        )

        self.assertEqual(200, response.status_code)
        self.assertIn(b'Search entire log', response.data)
        self.assertIn(b'value="NP25-2"', response.data)
        self.assertIn(b'NP25-2', response.data)
        self.assertNotIn(b"NP25-10", response.data)
        self.assertIn(b"await fetch(url", response.data)
        self.assertNotIn(b"requestSubmit()", response.data)
        self.assertIn(b"updateTable(link.href)", response.data)

    def test_sdl_statistics_follow_filters(self):
        full_page = self.client.get("/sdl")
        filtered_page = self.client.get("/sdl?filter_1=BRAIN")

        self.assertIn(b"2 accessions", full_page.data)
        self.assertIn(b"9 slides", full_page.data)
        self.assertIn(b"2 accessions", filtered_page.data)
        self.assertIn(b"5 slides", filtered_page.data)
        self.assertIn(b"nextStatistics.innerHTML", filtered_page.data)

    def test_sdl_route_sorts_by_workbook_row(self):
        response = self.client.get("/sdl?sort=16&direction=asc")

        self.assertEqual(200, response.status_code)
        self.assertLess(
            response.data.index(b"NP25-10"),
            response.data.index(b"NP25-2"),
        )
        self.assertIn(b"Row", response.data)
        self.assertIn("▲".encode(), response.data)
        self.assertIn("↕".encode(), response.data)

    def test_inventory_route_renders_naturally_sorted_results(self):
        response = self.client.get(
            "/inventories?file=scanner.csv&sort=0&direction=asc"
        )

        self.assertEqual(200, response.status_code)
        self.assertIn(b"Search entire inventory", response.data)
        self.assertLess(
            response.data.index(b"Slide 2"),
            response.data.index(b"Slide 10"),
        )
        self.assertIn(b'name="filter_1"', response.data)
        self.assertIn(b"await fetch(url", response.data)
        self.assertNotIn(b"requestSubmit()", response.data)
        self.assertIn(b"updateTable(link.href)", response.data)


if __name__ == "__main__":
    unittest.main()
