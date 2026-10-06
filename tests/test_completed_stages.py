import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as qc_app


class CompletedStagesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.batch_base = self.root / "batches"
        self.batch_root = self.batch_base / "SS100" / "batch-1"
        (self.batch_root / "label").mkdir(parents=True)
        (self.batch_root / "macro").mkdir()
        with open(
            self.batch_root / "enriched.csv", "w", newline="", encoding="utf-8"
        ) as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=[
                    "AccessionID",
                    "BlockNumber",
                    "Stain",
                    "ParsingQCPassed",
                    "original_slide_path",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "AccessionID": "A12-123",
                    "BlockNumber": "A1",
                    "Stain": "HE",
                    "ParsingQCPassed": "TRUE",
                    "original_slide_path": "slide.svs",
                }
            )

        self.old_batch_base = qc_app.Config.INSLIDE_BATCHES
        self.old_instance_dir = qc_app.Config.INSTANCE_DIR
        qc_app.Config.INSLIDE_BATCHES = str(self.batch_base)
        qc_app.Config.INSTANCE_DIR = str(self.root / "instance")
        qc_app.batch_contexts.clear()
        qc_app.app.config.update(TESTING=True, SECRET_KEY="completed-stages-test")

    def tearDown(self):
        qc_app.batch_contexts.clear()
        qc_app.Config.INSLIDE_BATCHES = self.old_batch_base
        qc_app.Config.INSTANCE_DIR = self.old_instance_dir
        self.temp_dir.cleanup()

    def test_discovery_creates_catalog_row_and_ignores_row_completion(self):
        batches, warnings = qc_app.discover_batches()

        self.assertEqual(warnings, [])
        self.assertEqual(len(batches), 1)
        self.assertFalse(batches[0].qc_complete)
        self.assertEqual("nightly", batches[0].run_type)
        self.assertTrue(
            (Path(qc_app.Config.INSTANCE_DIR) / "batch_catalog.sqlite3").exists()
        )
        self.assertFalse((self.batch_root / "completed_stages.csv").exists())

        with qc_app.app.test_request_context("/qc"):
            selected, available, selection_warnings = qc_app._selected_batch()
            self.assertIsNone(selected)
            self.assertEqual(len(available), 1)
            self.assertEqual(selection_warnings, [])

    def test_qc_chooser_groups_on_demand_batches(self):
        (self.batch_root / ".inslide_run_type").write_text(
            "on_demand", encoding="utf-8"
        )
        user = qc_app.User("operator", "", is_admin=False)
        client = qc_app.app.test_client()
        with mock.patch.object(qc_app.user_manager, "get", return_value=user):
            with client.session_transaction() as flask_session:
                flask_session["_user_id"] = user.id
                flask_session["_fresh"] = True
            chooser = client.get("/qc")
            category = client.get("/qc?run_type=on_demand")

        self.assertIn(b"Nightly", chooser.data)
        self.assertIn(b"On-demand", chooser.data)
        self.assertIn(b"SS100/batch-1", category.data)

    def test_qc_true_is_not_available_but_can_be_selected_explicitly(self):
        batches, _ = qc_app.discover_batches()
        batches[0].mark_qc_complete()

        with qc_app.app.test_request_context(f"/qc?batch={batches[0].id}"):
            selected, available, _ = qc_app._selected_batch()
            self.assertIsNone(selected)
            self.assertEqual(available, [])

        with qc_app.app.test_request_context(f"/history?batch={batches[0].id}"):
            selected, available, _ = qc_app._selected_batch(allow_completed=True)
            self.assertEqual(selected.id, batches[0].id)
            self.assertEqual(available, [])

    def test_legacy_stage_file_is_not_read_or_replaced(self):
        status_path = self.batch_root / "completed_stages.csv"
        malformed = "QC,Renamed\nmaybe,False\n"
        status_path.write_text(malformed, encoding="utf-8")

        batches, warnings = qc_app.discover_batches()

        self.assertEqual(len(batches), 1)
        self.assertFalse(batches[0].qc_complete)
        self.assertEqual(status_path.read_text(encoding="utf-8"), malformed)
        self.assertEqual(warnings, [])

    def test_mark_qc_complete_preserves_renamed(self):
        context = qc_app.discover_batches()[0][0]
        qc_app.batch_catalog.update_stages(
            qc_app.Config.INSTANCE_DIR, context.id, renamed_complete=True
        )
        context.load_completed_stages()

        context.mark_qc_complete()
        context.load_completed_stages()

        self.assertEqual(context.completed_stages, {"QC": True, "Renamed": True})

    def test_qc_row_validation_requires_nonblank_accession_and_safe_name_fields(self):
        valid_row = {
            "AccessionID": "NP25-1234",
            "BlockNumber": "A1",
            "Stain": "HE",
        }

        self.assertEqual(qc_app._qc_row_validation_errors(valid_row), [])
        self.assertEqual(
            [], qc_app._qc_row_validation_errors(
                {**valid_row, "AccessionID": "mixed Case / custom_id"}
            )
        )
        self.assertIn(
            "Accession ID is required",
            qc_app._qc_row_validation_errors({**valid_row, "AccessionID": "  "}),
        )
        self.assertIn(
            "Block Number is required",
            qc_app._qc_row_validation_errors({**valid_row, "BlockNumber": "  "}),
        )
        self.assertIn(
            "Stain is required",
            qc_app._qc_row_validation_errors({**valid_row, "Stain": ""}),
        )

    def test_qc_values_are_normalized_before_validation(self):
        normalized = qc_app._normalize_qc_values(
            {
                "AccessionID": "np25-1234",
                "Stain": "gfap_dab",
                "BlockNumber": "b-4",
                "_is_complete": True,
            }
        )

        self.assertEqual(normalized["AccessionID"], "np25-1234")
        self.assertEqual(normalized["Stain"], "GFAP-DAB")
        self.assertEqual(normalized["BlockNumber"], "B-4")
        self.assertTrue(normalized["_is_complete"])
        self.assertEqual(qc_app._qc_row_validation_errors(normalized), [])

    def test_qc_validation_rejects_non_filename_safe_characters_in_every_field(self):
        valid_row = {
            "AccessionID": "NP25-1234",
            "BlockNumber": "A1",
            "Stain": "HE",
        }
        unsafe_values = (".", "/", "\\", " ", "_", "&", ":", "*", "?", '"', "<", ">", "|")

        for field in ("Stain", "BlockNumber"):
            for unsafe in unsafe_values:
                with self.subTest(field=field, unsafe=unsafe):
                    malformed = dict(valid_row)
                    malformed[field] = f"A{unsafe}1"
                    self.assertTrue(qc_app._qc_row_validation_errors(malformed))

    def test_data_manager_preserves_unsafe_whitespace_for_final_validation(self):
        csv_path = self.batch_root / "enriched.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=[
                    "AccessionID",
                    "BlockNumber",
                    "Stain",
                    "ParsingQCPassed",
                    "original_slide_path",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "AccessionID": "A12-123",
                    "BlockNumber": " A1",
                    "Stain": "HE ",
                    "ParsingQCPassed": "TRUE",
                    "original_slide_path": "slide.svs",
                }
            )

        manager = qc_app.DataManager(self.batch_root, csv_path)
        manager.load_data()

        self.assertEqual(manager.data[0]["BlockNumber"], " A1")
        self.assertEqual(manager.data[0]["Stain"], "HE ")
        self.assertTrue(qc_app._qc_row_validation_errors(manager.data[0]))

    def test_final_validation_requeues_invalid_completed_row(self):
        batches, _ = qc_app.discover_batches()
        context = batches[0]
        context.refresh()
        row = context.data_manager.data[0]
        row["AccessionID"] = "  "
        item = context.queue_manager.get(0)
        item.completed_by_id = "reviewer"
        item.completed_at = "2026-07-22T12:00:00"

        invalid_indices = qc_app._requeue_invalid_qc_rows(context)

        self.assertEqual(invalid_indices, [0])
        self.assertFalse(row["_is_complete"])
        self.assertEqual(item.status, "pending")
        self.assertIsNone(item.completed_by_id)
        self.assertIsNone(item.completed_at)

    def test_final_validation_leaves_valid_completed_row_unchanged(self):
        batches, _ = qc_app.discover_batches()
        context = batches[0]
        context.refresh()
        item = context.queue_manager.get(0)

        self.assertEqual(qc_app._requeue_invalid_qc_rows(context), [])
        self.assertTrue(context.data_manager.data[0]["_is_complete"])
        self.assertEqual(item.status, "completed")


if __name__ == "__main__":
    unittest.main()
