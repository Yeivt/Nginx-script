import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import nginx_one_health as app

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
DATE = "2026-10-12T11:59:00+00:00"
INSTANCE = {"object_id": "inst_test", "hostname": "LNK2843", "status": "online", "os_version": "RHEL 9.8"}


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())
        self.status = 200


def api_response(metric, aggregate, dimension, group, value, **dimensions):
    return {"metrics": [{"metric": metric, "aggregate": aggregate, "series": [
        {"dimensions": {dimension: group, "instance_object_id": "inst_test", **dimensions},
         "data": [{"timestamp": DATE, "value": value}]}]}]}


def fixture_client():
    client = Mock()
    def query(instance_id, dimension, metric, qualifier, now):
        if dimension == "instance_object_id":
            group = instance_id
        elif dimension == "location_zone":
            group = "/location/root/fraudprevention"
        else:
            group = "fraudprevention"
        if metric == app.REQUEST_METRIC:
            value = 100
        elif metric == app.RESPONSE_METRIC:
            value = 4 if qualifier == "4xx" else 2
        else:
            value = 1 if qualifier in ("UP", "DOWN") else 0
        return {group: {DATE: value}}, []
    client.query_health.side_effect = query
    client.instances.return_value = [INSTANCE]
    return client


class HealthTests(unittest.TestCase):
    def test_transport_scope_filters_and_rate_not_counter_sum(self):
        client = app.ReadOnlyClient("https://example.console.ves.volterra.io", "placeholder")
        client.allowed_instances = {"inst_test"}
        client.opener = Mock()
        client.opener.open.return_value = Response(api_response(app.RESPONSE_METRIC, "rate", "server_zone", "fraudprevention", 2))
        groups, warnings = client.query_health("inst_test", "server_zone", app.RESPONSE_METRIC, "5xx", NOW)
        self.assertEqual(groups["fraudprevention"][DATE], 2)
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/namespaces/desarrollo/monitor/metrics_query_topx"))
        payload = json.loads(req.data)
        self.assertEqual(payload["metrics"][0]["aggregate"], "rate")
        self.assertEqual(payload["filter"][0]["filterSet"][0]["values"], ["inst_test"])
        self.assertEqual(payload["filter"][0]["filterSet"][1]["values"], ["5xx"])
        self.assertFalse(warnings)

    def test_upstream_query_uses_state_and_gauge_max(self):
        client = app.ReadOnlyClient("https://example.console.ves.volterra.io", "placeholder")
        client.allowed_instances = {"inst_test"}
        client.opener = Mock()
        client.opener.open.return_value = Response(api_response(app.UPSTREAM_METRIC, "max", "upstream_name", "fraudprevention", 1))
        client.query_health("inst_test", "upstream_name", app.UPSTREAM_METRIC, "DOWN", NOW)
        payload = json.loads(client.opener.open.call_args.args[0].data)
        self.assertEqual(payload["metrics"][0]["aggregate"], "max")
        self.assertEqual(payload["filter"][0]["filterSet"][1]["values"], ["DOWN"])

    def test_unlisted_id_unknown_metric_and_mutating_routes_blocked(self):
        client = app.ReadOnlyClient("https://example.console.ves.volterra.io", "placeholder")
        client.allowed_instances = {"inst_test"}
        client.opener = Mock()
        for args in (("inst_other", "server_zone", app.REQUEST_METRIC, ""),
                     ("inst_test", "server_zone", "invented.metric", ""),
                     ("inst_test", "server_zone", app.RESPONSE_METRIC, "500")):
            with self.assertRaises(app.ReportError):
                client.query_health(*args, NOW)
        for method in ("PUT", "PATCH", "DELETE"):
            with self.assertRaises(app.ReportError):
                client._request(method, "/instances/inst_test", {})
        client.opener.open.assert_not_called()

    def test_matching_timestamps_percentages_and_peer_alerts(self):
        report = app.gather(fixture_client(), INSTANCE, NOW)
        row = app.http_rows(report, "server_zone")[0]
        self.assertEqual(row["pct4"], 4)
        self.assertEqual(row["pct5"], 2)
        self.assertIn("ERRORES 5xx", row["status"])
        upstream = app.upstream_rows(report)[0]
        self.assertIn("1 peer(s) reportados DOWN", upstream["status"])
        self.assertFalse(app.report_partial(report))

    def test_missing_errors_not_zero_and_unmatched_dates_no_percentage(self):
        report = app.gather(fixture_client(), INSTANCE, NOW)
        report["results"]["server_zone/5xx"]["groups"] = {}
        row = app.http_rows(report, "server_zone")[0]
        self.assertIsNone(row["pct5"])
        self.assertIn("5xx: SIN DATOS", row["status"])
        report["results"]["server_zone/5xx"]["groups"] = {"fraudprevention": {"2026-10-12T11:58:00+00:00": 2}}
        row = app.http_rows(report, "server_zone")[0]
        self.assertIsNone(row["pct5"])
        self.assertIsNone(row["percentage_at"])

    def test_impossible_percentages_not_published(self):
        report = app.gather(fixture_client(), INSTANCE, NOW)
        report["results"]["server_zone/requests"]["groups"]["fraudprevention"][DATE] = 1
        self.assertIsNone(app.http_rows(report, "server_zone")[0]["pct5"])

    def test_synthetic_all_ignored_other_means_partial(self):
        data = api_response(app.REQUEST_METRIC, "rate", "server_zone", "all", 100)
        data["metrics"][0]["series"].append({"dimensions": {"server_zone": "other"}, "data": []})
        groups, warnings = app.parse_series(data, app.REQUEST_METRIC, "rate", "server_zone", "", "inst_test", NOW)
        self.assertFalse(groups)
        self.assertTrue(warnings)

    def test_other_instance_or_status_cannot_leak_into_report(self):
        for dims in ({"instance_object_id": "inst_other"}, {"namespace": "production"}, {"status_range": "4xx"}):
            data = api_response(app.RESPONSE_METRIC, "rate", "server_zone", "fraudprevention", 2, **dims)
            with self.assertRaises(app.ReportError):
                app.parse_series(data, app.RESPONSE_METRIC, "rate", "server_zone", "5xx", "inst_test", NOW)

    def test_null_invalid_rates_and_duplicate_groups(self):
        data = api_response(app.REQUEST_METRIC, "rate", "server_zone", "fraudprevention", None)
        groups, _ = app.parse_series(data, app.REQUEST_METRIC, "rate", "server_zone", "", "inst_test", NOW)
        self.assertEqual(groups["fraudprevention"], {})
        for value in (-1, True, float("nan"), float("inf")):
            data = api_response(app.REQUEST_METRIC, "rate", "server_zone", "fraudprevention", value)
            with self.assertRaises(app.ReportError):
                app.parse_series(data, app.REQUEST_METRIC, "rate", "server_zone", "", "inst_test", NOW)
        data = api_response(app.REQUEST_METRIC, "rate", "server_zone", "fraudprevention", 1)
        data["metrics"][0]["series"] *= 2
        with self.assertRaises(app.ReportError):
            app.parse_series(data, app.REQUEST_METRIC, "rate", "server_zone", "", "inst_test", NOW)

    def test_query_failures_stale_and_open_source_no_health_claim(self):
        client = Mock()
        client.query_health.side_effect = app.ReportError("HTTP 403")
        report = app.gather(client, INSTANCE, NOW)
        self.assertEqual(len(report["warnings"]), 15)
        content = app.html_report(report)
        self.assertIn("SIN TELEMETRÍA DE UPSTREAMS", content)
        self.assertNotIn("servicio sano</", content)
        self.assertTrue(app.report_partial(report))
        report = app.gather(fixture_client(), INSTANCE, NOW)
        report["generated_at"] = "2026-10-12T12:05:00+00:00"
        self.assertIn("DESACTUALIZADO", app.http_rows(report, "server_zone")[0]["status"])

    def test_mapping_logs_are_references_and_html_escaped(self):
        report = app.gather(fixture_client(), INSTANCE, NOW)
        report["instance"]["hostname"] = '<script>alert(1)</script>'
        content = app.html_report(report)
        self.assertIn("VS_fraudprevention.conf", content)
        self.assertIn("lua_fraudprevention.conf", content)
        self.assertIn("error_fraudprevention.log", content)
        self.assertIn("No se leen logs", content)
        self.assertNotIn("<script>", content)

    def test_cli_creates_report_and_json_without_agent_keys(self):
        client = fixture_client()
        client.instances.return_value = [dict(INSTANCE, key="must-not-export")]
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(app, "ReadOnlyClient", return_value=client), patch.object(
                    app, "gather", side_effect=lambda c, i: app_gather(c, i, NOW)):
                self.assertEqual(app.main(["--instance-id", "inst_test", "--output", folder, "--save-json"]), 0)
            files = list(Path(folder).rglob("*.html"))
            self.assertEqual(len(files), 1)
            self.assertIn("ERRORES 5xx", files[0].read_text())
            raw = next(Path(folder).rglob("*.json")).read_text()
            self.assertNotIn("must-not-export", raw)


app_gather = app.gather

if __name__ == "__main__":
    unittest.main()
