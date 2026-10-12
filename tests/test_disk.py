import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import nginx_one_disk as app

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def series(mount="/", points=None, **dimensions):
    return {"dimensions": {"mount_point": mount, "instance_object_id": "inst_test",
                           "state": "in_use", **dimensions},
            "data": points if points is not None else [{"timestamp": "2026-10-11T11:59:30Z", "value": 0.48}]}


def response(*items):
    return {"query_metadata": {"resolution": "30s"},
            "metrics": [{"metric": app.METRIC, "aggregate": "avg", "series": list(items)}]}


class HttpResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())
        self.status = 200


class DiskTests(unittest.TestCase):
    def client(self):
        client = app.ReadOnlyClient("https://example.console.ves.volterra.io", "test-placeholder")
        client.opener = Mock()
        return client

    def test_selected_instance_scoped_query_and_only_read_endpoints(self):
        client = self.client()
        client.opener.open.side_effect = [HttpResponse({"count": 1, "items": [
            {"object_id": "inst_test", "hostname": "LNK2843"}]}), HttpResponse(response(series()))]
        self.assertEqual(client.instances()[0]["object_id"], "inst_test")
        rows, warnings = client.disk_usage("inst_test", NOW)
        self.assertFalse(warnings)
        self.assertEqual(rows[0]["usage_percent"], 0.48)  # No multiplicar por 100.
        calls = client.opener.open.call_args_list
        self.assertEqual(calls[0].args[0].get_method(), "GET")
        req = calls[1].args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/namespaces/desarrollo/monitor/metrics_query_topx"))
        body = json.loads(req.data)
        self.assertEqual(body["group_series_by"], "mount_point")
        self.assertEqual(body["metrics"], [{"name": app.METRIC, "aggregate": "avg"}])
        filters = body["filter"][0]["filterSet"]
        self.assertEqual(filters[0]["values"], ["inst_test"])
        self.assertEqual(filters[1]["values"], ["in_use"])
        self.assertEqual(body["resolution"], "30s")

    def test_unknown_id_and_mutating_routes_blocked_before_network(self):
        client = self.client()
        with self.assertRaises(app.ReportError):
            client.disk_usage("inst_other", NOW)
        for method, path, payload in (("DELETE", "/instances/inst_test", None),
                                      ("PATCH", "/instances/inst_test", {}),
                                      ("POST", "/instances", {}),
                                      ("PUT", "/monitor/metrics_query_topx", {}),
                                      ("GET", "/namespaces/production/instances", None)):
            with self.assertRaises(app.ReportError):
                client._request(method, path, payload)
        client.opener.open.assert_not_called()

    def test_latest_non_null_and_zero_is_available(self):
        points = [{"timestamp": "2026-10-11T11:59:30Z", "value": None},
                  {"timestamp": "2026-10-11T11:58:30Z", "value": 1},
                  {"timestamp": "2026-10-11T11:59:00Z", "value": 0}]
        rows, warnings = app.parse_disk_usage(response(series(points=points)), "inst_test", NOW)
        self.assertEqual(rows[0]["usage_percent"], 0)
        self.assertEqual(rows[0]["age_seconds"], 60)
        self.assertFalse(warnings)

    def test_topx_synthetic_all_is_not_a_mount_other_warns(self):
        rows, warnings = app.parse_disk_usage(response(series(), series("all"), series("other")), "inst_test", NOW)
        self.assertEqual(len(rows), 1)
        self.assertIn("incompleto", warnings[0])

    def test_missing_and_stale_metrics_distinguished(self):
        empty = series("/boot", [{"timestamp": "2026-10-11T11:59:30Z", "value": None}])
        old = series("/var", [{"timestamp": "2026-10-11T11:56:00Z", "value": 42.73}])
        rows, warnings = app.parse_disk_usage(response(empty, old), "inst_test", NOW)
        self.assertIsNone(rows[0]["usage_percent"])
        self.assertEqual(rows[0]["status"], "SIN DATOS")
        self.assertEqual(rows[1]["status"], "DESACTUALIZADO")
        self.assertEqual(len(warnings), 2)

    def test_another_instance_namespace_or_state_is_rejected(self):
        for dimensions in ({"instance_object_id": "inst_other"}, {"namespace": "production"}, {"state": "free"}):
            with self.assertRaises(app.ReportError):
                app.parse_disk_usage(response(series(**dimensions)), "inst_test", NOW)

    def test_invalid_percentage_timestamps_and_duplicate_mounts_rejected(self):
        for value in (True, -1, 101, float("nan"), float("inf"), "89.83"):
            with self.assertRaises(app.ReportError):
                app.parse_disk_usage(response(series(points=[{"timestamp": "2026-10-11T11:59:30Z", "value": value}])), "inst_test", NOW)
        for date in ("bad", "2026-10-11T11:59:30", "2026-10-11T12:30:00Z"):
            with self.assertRaises(app.ReportError):
                app.parse_disk_usage(response(series(points=[{"timestamp": date, "value": 3}])), "inst_test", NOW)
        with self.assertRaises(app.ReportError):
            app.parse_disk_usage(response(series(), series()), "inst_test", NOW)

    def test_empty_metrics_is_not_zero_disk_usage(self):
        rows, warnings = app.parse_disk_usage(response(), "inst_test", NOW)
        self.assertEqual(rows, [])
        self.assertTrue(warnings)

    def test_selection_retries_and_id_must_belong_to_inventory(self):
        items = [{"object_id": "inst_test"}, {"object_id": "inst_other"}]
        with patch("builtins.input", side_effect=["bad", "3", "2"]):
            self.assertEqual(app.select_instance(items)["object_id"], "inst_other")
        with self.assertRaises(app.ReportError):
            app.select_instance(items, "inst_missing")
        with patch("builtins.input", return_value="0"):
            self.assertIsNone(app.select_instance(items))

    def test_csv_protects_formula_fields_and_existing_files(self):
        rows, _ = app.parse_disk_usage(response(series()), "inst_test", NOW)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "disk.csv"
            app.save_csv(path, {"object_id": "inst_test", "hostname": "=fake-formula"}, rows)
            self.assertIn("'=fake-formula", path.read_text(encoding="utf-8-sig"))
            with self.assertRaises(FileExistsError):
                app.save_csv(path, {"object_id": "inst_test"}, rows)

    def test_list_only_never_queries_metrics(self):
        client = Mock()
        client.instances.return_value = [{"object_id": "inst_test", "hostname": "LNK2843"}]
        with patch.object(app, "ReadOnlyClient", return_value=client):
            self.assertEqual(app.main(["--list-only"]), 0)
        client.disk_usage.assert_not_called()

    def test_cli_end_to_end_simulated_selected_vm(self):
        rows, warnings = app.parse_disk_usage(response(series("/logs/nginx")), "inst_test", NOW)
        client = Mock()
        client.instances.return_value = [{"object_id": "inst_test", "hostname": "LNK2843"}]
        client.disk_usage.return_value = (rows, warnings)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "disk.csv"
            with patch.object(app, "ReadOnlyClient", return_value=client):
                self.assertEqual(app.main(["--instance-id", "inst_test", "--csv", str(path)]), 0)
            self.assertIn("/logs/nginx", path.read_text(encoding="utf-8-sig"))
        client.disk_usage.assert_called_once_with("inst_test")


if __name__ == "__main__":
    unittest.main()
