import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

import nginx_one_report as app


def instance(identifier="inst_a", hostname="LNK8265", system="host1"):
    return {"object_id": identifier, "hostname": hostname, "system_id": system}


def finding():
    return {"rule": "example_rule", "info": "Comentario literal de la plataforma",
            "severity": "warning", "category": "best_practice",
            "documentation": ["https://nginx.org/en/docs/"],
            "where": [{"file": "/etc/nginx/conf.d/vS_pruebasocp.conf", "line": 12,
                       "directive": "proxy_pass", "message": "Detalle literal"},
                      {"file": "/etc/nginx/lua/test.lua", "line": 3}]}


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode())
        self.status = status


class ReportTests(unittest.TestCase):
    def client(self, responses):
        client = app.ReadOnlyClient("https://example.console.ves.volterra.io", "test-placeholder")
        client.opener = Mock()
        client.opener.open.side_effect = responses
        return client

    def test_get_only_correct_endpoint_scope_and_auth(self):
        client = self.client([Response([finding()])])
        self.assertEqual(client.get("/instances/inst_a/config-report")[0]["rule"], "example_rule")
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.full_url, "https://example.console.ves.volterra.io/api/nginx/one/namespaces/desarrollo/instances/inst_a/config-report")
        self.assertEqual(req.get_header("Authorization"), "APIToken test-placeholder")
        self.assertIsNone(req.data)

    def test_block_other_routes_and_query_injection(self):
        client = self.client([])
        for route in ("/instances/inst_a/config", "/instances/../config-report",
                      "https://evil.invalid", "/namespaces/produccion/instances"):
            with self.assertRaises(app.ReportError):
                client.get(route)
        with self.assertRaises(app.ReportError):
            client.get("/instances", "?namespace=produccion")
        client.opener.open.assert_not_called()

    def test_redirects_blocked(self):
        with self.assertRaises(app.ReportError):
            app.NoRedirects().redirect_request(None, None, 302, "", {}, "https://evil.invalid")

    def test_reject_unsafe_tenant(self):
        for url in ("http://example.com", "https://user:pass@example.com",
                    "https://example.com/path", "https://example.com?namespace=prod"):
            with self.assertRaises(app.ReportError):
                app.ReadOnlyClient(url, "placeholder")

    def test_pagination_one_based_short_page(self):
        client = self.client([Response({"count": 3, "items": [instance()]}),
                              Response({"count": 3, "items": [instance("inst_b"), instance("inst_c")]})])
        self.assertEqual(len(client.instances()), 3)
        urls = [call.args[0].full_url for call in client.opener.open.call_args_list]
        self.assertTrue(urls[0].endswith("offset=1"))
        self.assertTrue(urls[1].endswith("offset=2"))

    def test_duplicate_page_is_error(self):
        client = self.client([Response({"count": 2, "items": [instance()]}),
                              Response({"count": 2, "items": [instance()]})])
        with self.assertRaises(app.ReportError):
            client.instances()

    def test_inventory_count_change_or_premature_end(self):
        for page in ({"count": 2, "items": []}, {"count": 3, "items": [instance("inst_b")]}):
            client = self.client([Response({"count": 2, "items": [instance()]}), Response(page)])
            with self.assertRaises(app.ReportError):
                client.instances()

    def test_namespace_mismatch(self):
        item = instance()
        item["namespace"] = "produccion"
        client = self.client([Response({"count": 1, "items": [item]})])
        with self.assertRaises(app.ReportError):
            client.instances()

    def test_pending_and_failure_not_reported_as_clean(self):
        client = self.client([Response({"count": 2, "items": [instance(), instance("inst_b")]}),
                              Response(None, 204), HTTPError("https://example.com", 403, "", {}, None)])
        groups, failed = app.collect(client)
        self.assertTrue(failed)
        text = app.report_text(groups["host1"], "test")
        self.assertIn("PENDIENTE", text)
        self.assertIn("NO DISPONIBLE", text)
        self.assertNotIn("Sin hallazgos", text)
        self.assertIn("HTTP 403", text)

    def test_invalid_report_does_not_mask_missing_data(self):
        for data in ({"findings": []}, ["bad"], [{"where": {}}],
                     [{"where": [{"line": "12"}]}], [{"documentation": [42]}]):
            with self.assertRaises(app.ReportError):
                app.validate_report(data)

    def test_grouping_files_original_comments_and_unknown_category(self):
        other = {"rule": "other_rule", "category": "future_category", "info": "Sin archivo"}
        record = {"instance": instance(), "report": [finding(), other], "state": "Consulta completada", "error": ""}
        text = app.report_text([record], "test")
        self.assertIn("vS_pruebasocp.conf", text)
        self.assertIn("/etc/nginx/lua/test.lua", text)
        self.assertEqual(text.count("Comentario literal de la plataforma"), 2)
        self.assertIn("Detalle literal", text)
        self.assertIn("Línea: 12", text)
        self.assertIn("Sin archivo informado", text)
        self.assertIn("future_category", text)
        self.assertIn("Otras categorías", text)

    def test_html_escapes_platform_text(self):
        result = app.report_html('<script>alert("x")</script>')
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)

    def test_group_vm_and_do_not_export_agent_key(self):
        item = instance()
        item["key"] = "agent-secret-placeholder"
        client = self.client([Response({"count": 2, "items": [item, instance("inst_b")]}),
                              Response([finding()]), Response([])])
        groups, failed = app.collect(client)
        self.assertFalse(failed)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups["host1"]), 2)
        self.assertNotIn("agent-secret-placeholder", json.dumps(groups))

    def test_outputs_unique_per_host_and_repeatable_without_overwrite(self):
        groups = {}
        for key in ("host1", "host2"):
            groups[key] = [{"instance": instance(system=key), "report": [finding()],
                            "state": "Consulta completada", "error": ""}]
        with tempfile.TemporaryDirectory() as directory:
            first = app.write_reports(groups, Path(directory), "html", True)
            second = app.write_reports(groups, Path(directory), "txt")
            self.assertNotEqual(first, second)
            self.assertEqual(len(list(first.glob("*.html"))), 2)
            self.assertEqual(len(list(first.glob("*.json"))), 2)
            self.assertEqual(len(list(second.glob("*.txt"))), 2)
            for path in first.glob("*.html"):
                self.assertIn("vS_pruebasocp.conf", path.read_text())

    def test_empty_report_and_empty_inventory(self):
        client = self.client([Response({"count": 0, "items": []})])
        groups, failed = app.collect(client)
        self.assertFalse(failed)
        self.assertEqual(groups, {})
        text = app.report_text([{"instance": instance(), "report": [],
                                "state": "Consulta completada", "error": ""}], "test")
        self.assertIn("lista vacía", text)
        self.assertNotIn("Sin CVEs", text)


if __name__ == "__main__":
    unittest.main()
