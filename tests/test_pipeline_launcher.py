import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

import app as app_module
import pipeline


class FakeProcess:
    def __init__(self, output=b"", return_code=0):
        self.stdout = io.BytesIO(output)
        self.return_code = return_code

    def wait(self):
        return self.return_code

    def poll(self):
        return self.return_code


class PipelineCommandTests(unittest.TestCase):
    def test_pipeline_forwards_stage_specific_arguments(self):
        args = pipeline.create_parser().parse_args(
            [
                "--input-dir", "/input path", "--output-dir", "/output path",
                "--input-mode", "images", "--macro-workers", "6",
                "--macro-extensions", "svs", "ndpi",
                "--macro-image-extensions", "png", "tif",
                "--macro-thumbnail-size", "400", "250",
                "--ocr-workers", "2", "--ocr-use-cpu",
                "--naming-accession-pattern", "ABC.*", "--naming-workers", "3",
            ]
        )

        commands = pipeline.build_stage_commands(args)

        self.assertIn("6", commands[1])
        self.assertIn("ndpi", commands[1])
        self.assertIn("--use-cpu", commands[2])
        self.assertIn("2", commands[2])
        self.assertIn("ABC.*", commands[3])
        self.assertIn("3", commands[3])
        self.assertTrue(all(isinstance(command, list) for command in commands.values()))

    def test_portable_bundle_includes_launcher_scripts_and_template(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            output_app = pipeline.copy_app_bundle(output_dir)

            self.assertTrue(output_app.is_file())
            self.assertTrue((output_app.parent / "pipeline.py").is_file())
            self.assertTrue((output_app.parent / "1_get_macro.py").is_file())
            self.assertTrue((output_app.parent / "2_run_dual_ocr.py").is_file())
            self.assertTrue((output_app.parent / "3_name-files.py").is_file())
            self.assertTrue((output_app.parent / "renaming.py").is_file())
            self.assertTrue((output_app.parent / "openapi.json").is_file())
            self.assertTrue(
                (output_app.parent / "copath_utilities" / "copath_texttypes.py").is_file()
            )
            self.assertTrue((output_app.parent / "templates" / "pipeline.html").is_file())

    def test_successful_pipeline_records_run_type(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with mock.patch.object(
                sys,
                "argv",
                [
                    "pipeline.py", "--input-dir", str(root / "input"),
                    "--output-dir", str(root / "output"), "--end-at", "1",
                    "--run-type", "nightly",
                ],
            ), mock.patch.object(pipeline, "run_stage"):
                result = pipeline.main()

            marker = root / "output" / pipeline.RUN_TYPE_FILE
            self.assertEqual(0, result)
            self.assertEqual("nightly", marker.read_text(encoding="utf-8"))


class PipelineLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_store = (
            app_module.api_store.db_path,
            app_module.api_store.output_dir,
        )
        root = Path(self.temporary_directory.name)
        self.original_pipeline_config = {
            "PIPELINE_INPUT_ROOTS": app_module.app.config.get("PIPELINE_INPUT_ROOTS"),
            "PIPELINE_OUTPUT_ROOTS": app_module.app.config.get("PIPELINE_OUTPUT_ROOTS"),
            "PIPELINE_MAX_WORKERS": app_module.app.config.get("PIPELINE_MAX_WORKERS"),
            "PIPELINE_MAX_THUMBNAIL_DIMENSION": app_module.app.config.get(
                "PIPELINE_MAX_THUMBNAIL_DIMENSION"
            ),
        }
        app_module.api_store.configure(
            str(root / "api.sqlite3"), str(root / "pipeline-output")
        )
        app_module.app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            PIPELINE_INPUT_ROOTS=(tempfile.gettempdir(),),
            PIPELINE_OUTPUT_ROOTS=(tempfile.gettempdir(),),
            PIPELINE_MAX_WORKERS=8,
            PIPELINE_MAX_THUMBNAIL_DIMENSION=4096,
        )
        with app_module._pipeline_jobs_lock:
            app_module._pipeline_jobs.clear()
            app_module._pipeline_active_job_id = None

    def tearDown(self):
        with app_module._pipeline_jobs_lock:
            app_module._pipeline_jobs.clear()
            app_module._pipeline_active_job_id = None
        app_module.api_store.configure(*self.original_store)
        app_module.app.config.update(self.original_pipeline_config)
        self.temporary_directory.cleanup()

    def test_form_defaults_and_command(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            values = app_module._pipeline_form_values()
            values.update({"input_dir": str(input_dir), "output_dir": str(output_dir)})

            command, errors = app_module._pipeline_command(values)

        self.assertEqual([], errors)
        self.assertIsNotNone(command)
        self.assertEqual("1", command[command.index("--start-from") + 1])
        self.assertEqual("3", command[command.index("--end-at") + 1])
        self.assertEqual("4", command[command.index("--ocr-workers") + 1])
        self.assertEqual("on_demand", command[command.index("--run-type") + 1])
        self.assertNotIn("--ocr-use-cpu", command)

    def test_inventory_dates_drive_web_paths(self):
        root = Path(self.temporary_directory.name)
        gt450 = root / "gt450"
        inventories = root / "inventories"
        batches = root / "batches"
        newest = gt450 / "SS100" / "2026-10-02"
        older = gt450 / "SS100" / "2026-09-30"
        newest.mkdir(parents=True)
        older.mkdir(parents=True)
        inventories.mkdir()
        (inventories / "SS100_inventory.csv").write_text(
            "directory\n"
            f"{older}\n{newest}\n{newest}\n"
            f"{gt450 / 'SS100' / 'not-a-date'}\n",
            encoding="utf-8",
        )
        previous = (
            app_module.Config.GT450_IMAGES,
            app_module.Config.SCANNER_INVENTORIES,
            app_module.Config.INSLIDE_BATCHES,
        )
        app_module.Config.GT450_IMAGES = str(gt450)
        app_module.Config.SCANNER_INVENTORIES = str(inventories)
        app_module.Config.INSLIDE_BATCHES = str(batches)
        try:
            options, warnings = app_module._pipeline_date_options()
            values = app_module._pipeline_form_values(
                {"scanner": "SS100", "input_dir": str(newest)}
            )
            errors = app_module._pipeline_web_paths(values, options)
            forged = app_module._pipeline_form_values(
                {
                    "scanner": "SS100",
                    "input_dir": str(gt450 / "SS100" / "2026-10-03"),
                }
            )
            forged_errors = app_module._pipeline_web_paths(forged, options)
            mismatched = app_module._pipeline_form_values(
                {"scanner": "SS999", "input_dir": str(newest)}
            )
            mismatched_errors = app_module._pipeline_web_paths(mismatched, options)
        finally:
            (
                app_module.Config.GT450_IMAGES,
                app_module.Config.SCANNER_INVENTORIES,
                app_module.Config.INSLIDE_BATCHES,
            ) = previous

        self.assertEqual([], warnings)
        self.assertEqual(["SS100/2026-10-02", "SS100/2026-09-30"], [item["label"] for item in options])
        self.assertEqual([], errors)
        self.assertEqual(["Choose an available scanner and date directory."], forged_errors)
        self.assertEqual(["Choose an available scanner and date directory."], mismatched_errors)
        self.assertEqual(str(batches / "SS100" / "2026-10-02"), values["output_dir"])

    def test_starting_later_requires_the_previous_stage_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            values = app_module._pipeline_form_values()
            values.update(
                {
                    "input_dir": str(input_dir),
                    "output_dir": str(output_dir),
                    "start_from": "2",
                }
            )

            _command, errors = app_module._pipeline_command(values)
            (output_dir / "slide_mapping.csv").write_text("header\n", encoding="utf-8")
            command, resolved_errors = app_module._pipeline_command(values)

        self.assertTrue(any("slide_mapping.csv" in error for error in errors))
        self.assertEqual([], resolved_errors)
        self.assertIsNotNone(command)

    def test_pipeline_rejects_resource_values_above_limits(self):
        root = Path(self.temporary_directory.name)
        input_dir = root / "bounded-input"
        input_dir.mkdir()
        values = app_module._pipeline_form_values()
        values.update(
            {
                "input_dir": str(input_dir),
                "output_dir": str(root / "bounded-output"),
                "macro_workers": "9",
                "thumbnail_width": "4097",
            }
        )

        command, errors = app_module._pipeline_command(values)

        self.assertIsNone(command)
        self.assertTrue(any("must not exceed 8" in error for error in errors))
        self.assertTrue(any("must not exceed 4096" in error for error in errors))

    def test_pipeline_rejects_paths_outside_allowlists_and_symlink_escape(self):
        root = Path(self.temporary_directory.name)
        allowed = root / "allowed"
        outside = root / "outside"
        allowed.mkdir()
        outside.mkdir()
        app_module.app.config.update(
            PIPELINE_INPUT_ROOTS=(str(allowed),),
            PIPELINE_OUTPUT_ROOTS=(str(allowed),),
        )
        values = app_module._pipeline_form_values()
        values.update({"input_dir": str(outside), "output_dir": str(outside / "out")})

        _command, outside_errors = app_module._pipeline_command(values)

        self.assertTrue(any("outside" in error.lower() for error in outside_errors))
        symlink = allowed / "escape"
        try:
            symlink.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable")
        values.update({"input_dir": str(symlink), "output_dir": str(symlink / "out")})
        _command, symlink_errors = app_module._pipeline_command(values)
        self.assertTrue(any("outside" in error.lower() for error in symlink_errors))

    def test_reader_merges_output_and_marks_success(self):
        process = FakeProcess(b"line one\nline two\n", return_code=0)
        job = app_module.PipelineJob("job", "user", process)
        with app_module._pipeline_jobs_lock:
            app_module._pipeline_jobs[job.id] = job
            app_module._pipeline_active_job_id = job.id

        app_module._read_pipeline_output(job)

        self.assertEqual("line one\nline two\n", job.output)
        self.assertEqual("succeeded", job.status)
        self.assertEqual(0, job.return_code)
        self.assertIsNone(app_module._pipeline_active_job_id)

    def test_launcher_enforces_one_process_and_does_not_use_a_shell(self):
        process = FakeProcess()
        reader_thread = mock.Mock()
        with mock.patch.object(app_module.subprocess, "Popen", return_value=process) as popen:
            with mock.patch.object(
                app_module.threading, "Thread", return_value=reader_thread
            ):
                job = app_module._start_pipeline_job(["python", "pipeline.py"], "user")
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    app_module._start_pipeline_job(["python", "pipeline.py"], "other")

        self.assertEqual("running", job.status)
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertIs(app_module.subprocess.PIPE, popen.call_args.kwargs["stdout"])
        self.assertIs(app_module.subprocess.STDOUT, popen.call_args.kwargs["stderr"])
        reader_thread.start.assert_called_once_with()

    def test_reader_marks_nonzero_exit_as_failed(self):
        job = app_module.PipelineJob("job", "user", FakeProcess(b"bad\n", 7))
        app_module._read_pipeline_output(job)
        self.assertEqual("failed", job.status)
        self.assertEqual(7, job.return_code)

    def test_page_filters_dates_and_streams_pipeline_output(self):
        user = app_module.User("operator", "", is_admin=False)
        job = app_module.PipelineJob("visible-job", user.id, FakeProcess())
        job.output = "first line\nsecond line\n"
        with app_module._pipeline_jobs_lock:
            app_module._pipeline_jobs[job.id] = job
        client = app_module.app.test_client()

        with mock.patch.object(app_module.user_manager, "get", return_value=user), mock.patch.object(
            app_module, "_pipeline_date_options", return_value=([], [])
        ):
            with client.session_transaction() as flask_session:
                flask_session["_user_id"] = user.id
                flask_session["_fresh"] = True
                flask_session["pipeline_job_id"] = job.id
            page = client.get("/pipeline")
            output = client.get(f"/pipeline/jobs/{job.id}/output?offset=11")

        self.assertIn(b'id="scanner"', page.data)
        self.assertIn(b'id="input_dir"', page.data)
        self.assertIn(b'aria-label="Pipeline output"', page.data)
        self.assertIn(b"function updateDateOptions(", page.data)
        self.assertEqual("second line\n", output.get_json()["output"])
        self.assertEqual(len(job.output), output.get_json()["next_offset"])

    def test_authenticated_page_and_launch(self):
        user = app_module.User("operator", "", is_admin=False)
        fake_job = SimpleNamespace(id="new-job", status="running")
        client = app_module.app.test_client()

        with mock.patch.object(app_module.user_manager, "get", return_value=user):
            with client.session_transaction() as flask_session:
                flask_session["_user_id"] = user.id
                flask_session["_fresh"] = True
            response = client.get("/pipeline")

            with tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                input_dir = root / "input"
                input_dir.mkdir()
                option = {
                    "value": str(input_dir), "label": "SS100/2026-10-02",
                    "scanner": "SS100", "date": "2026-10-02",
                }
                with mock.patch.object(app_module, "_pipeline_date_options", return_value=([option], [])), mock.patch.object(
                    app_module, "_start_pipeline_job", return_value=fake_job
                ) as start_job, mock.patch.object(
                    app_module.Config, "INSLIDE_BATCHES", str(root)
                ):
                    launch_response = client.post(
                        "/pipeline/run",
                        data={"scanner": "SS100", "input_dir": str(input_dir)},
                    )

        self.assertEqual(200, response.status_code)
        self.assertIn(b"Run Label-Check", response.data)
        self.assertEqual(302, launch_response.status_code)
        self.assertTrue(start_job.called)


if __name__ == "__main__":
    unittest.main()
