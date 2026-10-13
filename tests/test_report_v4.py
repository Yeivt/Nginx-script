import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nginx_one_report_v4 as app

INSTANCE = {"object_id": "inst_test", "hostname": "LNK2843", "status": "online", "key": "agent-secret"}
VHOST = "/etc/nginx/conf.d/VS_fraudprevention.conf"
LUA = "/etc/nginx/conf.d/lua/lua_fraudprevention.conf"
UPSTREAM = "/etc/nginx/conf.d/upstreams/upstream_fraudprevention.conf"


def config():
    return {"conf_path": "/etc/nginx/nginx.conf", "config_version": "version-test",
            "configs": [{"name": "/etc/nginx/conf.d", "files": [
                {"name": "VS_fraudprevention.conf", "contents": "private-config-content"},
                {"name": "lua/lua_fraudprevention.conf"}, {"name": "upstreams/upstream_fraudprevention.conf"},
                {"name": "VS_without_findings.conf"}]}],
            "aux": [{"name": "/etc/nginx/ssl", "files": [{"name": "secret.key", "contents": "private-key"}]}],
            "payloads": [{"type": "inline_secret", "contents": "must-not-export"}]}


def findings():
    return [{"rule": "default_server_not_set", "category": "best_practice", "severity": "warning",
             "info": "default_server parameter is not set for the listen directive",
             "where": [{"file": VHOST, "line": 2, "directive": "listen"},
                       {"file": LUA, "line": 5, "directive": "proxy_pass"},
                       {"file": "/etc/nginx/conf.d/VS_other.conf", "line": 8}]}]


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode())
        self.status = status


class FakeOpener:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append(request)
        return next(self.answers)


def client(ambiente="produccion", report=None):
    result = app.ReadOnlyClient("https://example.console.ves.volterra.io", "placeholder", ambiente)
    result.opener = FakeOpener([Response({"count": 1, "items": [INSTANCE]}),
                               Response(config()), Response(findings() if report is None else report)])
    return result


class V4Tests(unittest.TestCase):
    def test_four_environments_isolated_and_only_get(self):
        self.assertEqual(app.listar_ambientes(), ["default", "desarrollo", "homologacion", "produccion"])
        for env in app.AMBIENTES:
            c = client(env)
            result = c.obtener_reporte("LNK2843", vhost="VS_fraudprevention.conf")
            self.assertEqual(result["ambiente"], env)
            for request in c.opener.requests:
                self.assertEqual(request.get_method(), "GET")
                self.assertIn(f"/namespaces/{env}/instances", request.full_url)
                self.assertIsNone(request.data)

    def test_environment_and_routes_rejected_before_network(self):
        for env in ("unknown", "../produccion", "produccion?x=1"):
            with self.assertRaises(app.ReportError):
                client(env)
        c = client()
        for path in ("/instances/inst_test/config", "/instances/inst_other/config-report",
                     "/monitor/metrics_query_topx", "/namespaces/default/instances"):
            with self.assertRaises(app.ReportError):
                c.get(path)
        self.assertEqual(c.opener.requests, [])

    def test_catalog_includes_files_without_findings_not_secret_contents(self):
        c = client()
        catalog = c.listar_archivos("LNK2843")
        self.assertEqual(len(catalog["archivos"]["vhost"]), 2)
        self.assertEqual(catalog["archivos"]["lua"][0]["ruta"], LUA)
        self.assertEqual(catalog["archivos"]["upstream"][0]["ruta"], UPSTREAM)
        self.assertEqual(len(c.opener.requests), 2)  # No consulta de recomendaciones para listar.
        serialized = json.dumps(catalog)
        for secret in ("agent-secret", "private-config-content", "private-key", "must-not-export", "contents"):
            self.assertNotIn(secret, serialized)
        catalog["archivos"]["vhost"].clear()
        self.assertEqual(len(c.listar_archivos("LNK2843")["archivos"]["vhost"]), 2)

    def test_selected_file_locations_counts_translation_only(self):
        c = client()
        result = c.obtener_reporte("LNK2843", vhost=VHOST)
        self.assertEqual(result["total_recomendaciones_archivo"], 1)
        self.assertEqual(result["resumen_archivo"]["best_practice"], 1)
        finding = result["recomendaciones"]["best_practice"][0]
        self.assertEqual(finding["linea"], 2)
        self.assertEqual(finding["directiva"], "listen")
        self.assertIn("El parámetro default_server", finding["comentario_es"])
        self.assertEqual(finding["archivo"], VHOST)
        self.assertNotIn(LUA, json.dumps(result))
        self.assertNotIn("VS_other.conf", json.dumps(result))

    def test_lua_selection_and_empty_file_are_distinct(self):
        result = client().obtener_reporte("LNK2843", lua="lua_fraudprevention.conf")
        self.assertEqual(result["recomendaciones"]["best_practice"][0]["linea"], 5)
        result = client().obtener_reporte("LNK2843", vhost="VS_without_findings.conf")
        self.assertEqual(result["estado"], "completado")
        self.assertEqual(result["total_recomendaciones_archivo"], 0)

    def test_selector_exactly_one_and_missing_file_not_clean_report(self):
        c = client()
        for kwargs in ({}, {"vhost": VHOST, "lua": LUA}, {"vhost": "not-present.conf"}, {"upstream": VHOST}):
            with self.assertRaises(app.ReportError):
                c.obtener_reporte("LNK2843", **kwargs)
        self.assertFalse(any(r.full_url.endswith("/config-report") for r in c.opener.requests))

    def test_duplicate_hostname_requires_instance_id(self):
        c = client()
        c.opener = FakeOpener([Response({"count": 2, "items": [INSTANCE, dict(INSTANCE, object_id="inst_second")]})])
        self.assertEqual(len(c.listar_servidores()), 2)
        with self.assertRaises(app.ReportError):
            c._resolve_server("LNK2843")
        self.assertEqual(c._resolve_server("LNK2843", "inst_second")["object_id"], "inst_second")
        with self.assertRaises(app.ReportError):
            c._resolve_server("OTHER", "inst_test")

    def test_duplicate_basename_requires_path_and_ambiguous_recommendation_rejected(self):
        data = config()
        data["configs"].append({"name": "/alternate", "files": [{"name": "VS_fraudprevention.conf"}]})
        files, version, main = app.catalog_files(data)
        catalog = {"archivos": {k: [f for f in files if f["tipo"] == k] for k in ("vhost", "lua", "upstream")}, "conf_path": main}
        with self.assertRaises(app.ReportError):
            app.select_file(catalog, "vhost", "VS_fraudprevention.conf")
        selected = app.select_file(catalog, "vhost", VHOST)
        self.assertTrue(app.location_matches(VHOST, catalog, selected))
        with self.assertRaises(app.ReportError):
            app.location_matches("VS_fraudprevention.conf", catalog, selected)

    def test_relative_report_paths_and_case_sensitive_match(self):
        catalog = client().listar_archivos("LNK2843")
        selected = app.select_file(catalog, "vhost", VHOST)
        self.assertTrue(app.location_matches("conf.d/VS_fraudprevention.conf", catalog, selected))
        self.assertTrue(app.location_matches("VS_fraudprevention.conf", catalog, selected))
        self.assertFalse(app.location_matches(VHOST.lower(), catalog, selected))
        with self.assertRaises(app.ReportError):
            app.location_matches("../VS_fraudprevention.conf", catalog, selected)

    def test_pending_not_zero_and_unlocated_not_assigned(self):
        c = client()
        c.opener = FakeOpener([Response({"count": 1, "items": [INSTANCE]}), Response(config()), Response(None, 204)])
        result = c.obtener_reporte("LNK2843", vhost=VHOST)
        self.assertEqual(result["estado"], "pendiente")
        self.assertIsNone(result["total_recomendaciones_archivo"])
        self.assertTrue(all(v is None for v in result["resumen_archivo"].values()))
        data = findings() + [{"rule": "without_location", "category": "security", "info": "general"}]
        result = client(report=data).obtener_reporte("LNK2843", vhost=VHOST)
        self.assertEqual(result["resumen_archivo"]["security"], 0)
        self.assertTrue(any("sin archivo informado" in text for text in result["avisos"]))

    def test_invalid_catalog_and_path_not_silently_ignored(self):
        for data in ({}, {"configs": [], "conf_path": "relative/nginx.conf"},
                     {"configs": [{"name": "/etc/nginx", "files": [{"name": "../secret.conf"}]}], "conf_path": "/etc/nginx/nginx.conf"}):
            with self.assertRaises(app.ReportError):
                app.catalog_files(data)

    def test_cli_framework_output_is_json_and_saves_same_report(self):
        c = client()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "selected.json"
            stdout = io.StringIO()
            with patch.object(app, "ReadOnlyClient", return_value=c), patch("sys.stdout", stdout):
                status = app.main(["--ambiente", "produccion", "--server", "LNK2843", "--vhost", VHOST, "--output", str(target)])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stdout.getvalue()), json.loads(target.read_text()))

    def test_list_environments_needs_no_token_or_network(self):
        stdout = io.StringIO()
        with patch.object(app, "ReadOnlyClient") as constructor, patch("sys.stdout", stdout):
            self.assertEqual(app.main(["--listar-ambientes"]), 0)
        constructor.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue())["ambientes"], list(app.AMBIENTES))


if __name__ == "__main__":
    unittest.main()
