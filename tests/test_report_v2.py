import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import nginx_one_report_v2 as app


def rule(category="best_practice"):
    return {"rule": "default_server_not_set", "category": category, "severity": "warning",
            "info": "default_server parameter is not set for the listen directive",
            "where": [{"file": "/etc/nginx/conf.d/VS_abm-3.conf", "line": 2,
                       "directive": "listen", "message": "default_server parameter is not set for the listen directive"}],
            "documentation": ["https://nginx.org/en/docs/http/ngx_http_core_module.html#listen"]}


def record(rules=None, counts=None):
    item = {"object_id": "inst_test", "hostname": "LNK2843", "system_id": "host_test",
            "nginx_build": {"version": "1.29.1"}}
    if counts is not None:
        item["recommendations"] = counts
    return {"instance": item, "report": [rule()] if rules is None else rules,
            "state": "Consulta completada para esta instancia", "error": ""}


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode())
        self.status = status


class NewReportTests(unittest.TestCase):
    def test_inventory_total_matches_platform_not_rule_count(self):
        data = record(counts=[{"type": "best_practice", "count": 118},
                              {"type": "optimization", "count": 0},
                              {"type": "security", "count": 3}])
        text = app.report_text([data], "test")
        self.assertIn("Mejores prácticas: 118 (contador informado por NGINX One)", text)
        self.assertIn("Seguridad: 3 (contador informado por NGINX One)", text)
        self.assertIn("Optimización: 0 (contador informado por NGINX One)", text)
        self.assertIn("1 reglas y 1 ubicaciones", text)
        self.assertIn("Estado de consulta: Consulta completada para esta instancia", text)

    def test_fallback_counts_locations_instead_of_rules(self):
        finding = rule()
        finding["where"].append({"file": "/etc/nginx/lua/test.lua", "line": 12})
        text = app.report_text([record([finding])], "test")
        self.assertIn("Mejores prácticas: 2 (ubicaciones calculadas desde config-report)", text)
        self.assertIn("1 reglas y 2 ubicaciones", text)
        self.assertIn("Se encontró 1 recomendación para /etc/nginx/lua/test.lua", text)
        self.assertIn("en la línea 12", text)

    def test_duplicate_location_not_counted_twice(self):
        finding = rule()
        finding["where"].append(finding["where"][0].copy())
        text = app.report_text([record([finding])], "test")
        self.assertIn("Mejores prácticas: 1 (ubicaciones calculadas", text)
        self.assertEqual(text.count("1. Mejores prácticas"), 1)

    def test_translation_preserves_original_and_distinguishes_local_advice(self):
        text = app.report_text([record()], "test")
        self.assertIn("Mejores prácticas — Advertencia en la línea 2", text)
        self.assertIn("Qué señala NGINX One: El parámetro default_server no está configurado", text)
        self.assertIn("Comentario original: default_server parameter is not set", text)
        self.assertIn("Directiva a revisar: listen", text)
        self.assertIn("Regla de NGINX One: default_server_not_set", text)
        self.assertIn("Orientación del script:", text)
        self.assertIn("No añadirlo a todos los vhosts", text)

    def test_unknown_comment_is_not_fabricated_or_assigned_example_advice(self):
        finding = rule()
        finding["info"] = "A different recommendation"
        finding["where"][0]["message"] = "Specific location message"
        text = app.report_text([record([finding])], "test")
        self.assertIn("Qué señala NGINX One: Specific location message", text)
        self.assertIn("Contexto de la regla (original): A different recommendation", text)
        self.assertNotIn("añadir default_server", text)
        self.assertNotIn("Comentario original:", text)

    def test_pending_counts_without_false_zero_or_clean_claims(self):
        data = record(counts=[{"type": "best_practice", "count": 118}])
        data["report"] = None
        data["state"] = "PENDIENTE"
        text = app.report_text([data], "test")
        self.assertIn("Mejores prácticas: 118", text)
        self.assertIn("Seguridad: no disponible", text)
        self.assertNotIn("Seguridad: 0", text)
        self.assertNotIn("no reporta cambios", text)

    def test_empty_category_labels_and_no_missing_location_assumptions(self):
        finding = rule("unexpected_category")
        finding["where"] = []
        text = app.report_text([record([finding])], "test")
        self.assertIn("ARCHIVO: Sin archivo informado", text)
        self.assertIn("línea no informada", text)
        self.assertIn("Categoría original: unexpected_category", text)
        self.assertIn("NGINX One no reporta cambios de optimización para este archivo.", text)
        self.assertNotIn("No se requieren cambios", text)

    def test_malformed_inventory_counters_use_explicit_fallback(self):
        for counts in ([{"type": "security", "count": -1}],
                       [{"type": "security", "count": True}],
                       [{"type": "security", "count": "10"}],
                       [{"type": "security", "count": 1}, {"type": "security", "count": 2}]):
            text = app.report_text([record(counts=counts)], "test")
            self.assertNotIn("contador informado por NGINX One", text)
            self.assertIn("ubicaciones calculadas desde config-report", text)

    def test_html_contains_headings_and_escapes_all_dynamic_content(self):
        finding = rule()
        finding["where"][0]["file"] = '<img src=x onerror=alert(1)>'
        finding["where"][0]["message"] = '<script>alert(1)</script>'
        content = app.report_html(app.report_text([record([finding])], "test"))
        self.assertIn("<h1>", content)
        self.assertIn('class="summary"', content)
        self.assertIn('class="file"', content)
        self.assertNotIn("<script>", content)
        self.assertNotIn("<img", content)
        self.assertIn("&lt;script&gt;", content)

    def test_cli_uses_only_get_and_exports_one_file_per_vm_with_counters(self):
        item = record(counts=[{"type": "best_practice", "count": 118}])["instance"]
        other = dict(item, object_id="inst_other")
        client = app.ReadOnlyClient("https://example.console.ves.volterra.io", "test-placeholder")
        client.opener = Mock()
        client.opener.open.side_effect = [Response({"count": 2, "items": [item, other]}),
                                          Response([rule()]), Response([])]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "ReadOnlyClient", return_value=client), patch.dict(
                    app.os.environ, {"NGINX_ONE_API_TOKEN": "test-placeholder"}):
                self.assertEqual(app.main(["--output", directory, "--save-json"]), 0)
            files = list(Path(directory).rglob("*.html"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text()
            self.assertIn("Mejores prácticas: 118", text)
            self.assertIn("inst_test", text)
            self.assertIn("inst_other", text)
            exported = json.loads(next(Path(directory).rglob("*.json")).read_text())
            self.assertEqual(exported["instances"][0]["instance"]["recommendations"][0]["count"], 118)
        for call in client.opener.open.call_args_list:
            request = call.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertIn("/namespaces/desarrollo/instances", request.full_url)
            self.assertIsNone(request.data)


if __name__ == "__main__":
    unittest.main()
