import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nginx_one_report_v5 as app
from test_report_v4 import INSTANCE, VHOST, LUA, FakeOpener, Response, config, findings


def client(ambiente="desarrollo", data=None, pending=False):
    result = app.ReadOnlyClient("https://example.console.ves.volterra.io", "placeholder", ambiente)
    report = findings() if data is None else data
    if report:
        report[0]["documentation"] = ["https://nginx.org/en/docs/"]
    result.opener = FakeOpener([Response({"count": 1, "items": [INSTANCE]}), Response(config()),
                               Response(None, 204) if pending else Response(report)])
    return result


class V5Tests(unittest.TestCase):
    def test_compact_report_exact_requested_fields_and_selected_location(self):
        c = client()
        result = c.obtener_reporte("LNK2843", vhost=VHOST)
        self.assertEqual(list(result), ["generado_utc", "estado", "ambiente", "server", "archivo",
                                       "total_recomendaciones_archivo", "resumen_archivo", "recomendaciones"])
        self.assertEqual(result["estado"], "reporte completado")
        self.assertEqual(result["total_recomendaciones_archivo"], 1)
        finding = result["recomendaciones"]["best_practice"][0]
        self.assertEqual(set(finding), {"regla", "categoria_original", "severidad", "severidad_es",
                                        "archivo", "linea", "directiva", "comentario_original",
                                        "comentario_es", "contexto_original"})
        self.assertEqual(finding["linea"], 2)
        self.assertNotIn(LUA, json.dumps(result))
        self.assertNotIn("agent-secret", json.dumps(result))
        self.assertEqual(result["resumen_archivo"]["security"], 0)

    def test_pending_counts_null_and_cli_returns_two(self):
        c = client(pending=True)
        stdout = io.StringIO()
        with patch.object(app, "ReadOnlyClient", return_value=c), patch("sys.stdout", stdout):
            status = app.main(["--ambiente", "desarrollo", "--server", "LNK2843", "--vhost", VHOST])
        result = json.loads(stdout.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(result["estado"], "reporte pendiente")
        self.assertIsNone(result["total_recomendaciones_archivo"])
        self.assertTrue(all(v is None for v in result["resumen_archivo"].values()))
        self.assertNotIn("avisos", result)

    def test_four_environments_and_catalog_contract_still_available(self):
        for env in app.AMBIENTES:
            c = client(env)
            servers = c.listar_servidores()
            self.assertEqual(servers[0]["instance_id"], "inst_test")
            catalog = c.listar_archivos("LNK2843")
            self.assertIn("instance_id", catalog)
            self.assertIn("config_version", catalog)
            self.assertEqual(len(catalog["archivos"]["vhost"]), 2)
            result = c.obtener_reporte("LNK2843", lua="lua_fraudprevention.conf")
            self.assertEqual(result["ambiente"], env)
            self.assertEqual(result["archivo"]["tipo"], "lua")
            for request in c.opener.requests:
                self.assertEqual(request.get_method(), "GET")
                self.assertIn(f"/namespaces/{env}/", request.full_url)

    def test_cli_stdout_and_saved_file_have_identical_compact_json(self):
        c = client()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report.json"
            stdout = io.StringIO()
            with patch.object(app, "ReadOnlyClient", return_value=c), patch("sys.stdout", stdout):
                status = app.main(["--ambiente", "desarrollo", "--server", "LNK2843", "--vhost", VHOST, "--output", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stdout.getvalue()), json.loads(output.read_text()))
            self.assertNotIn("documentacion", output.read_text())

    def test_failed_selection_stays_error_instead_of_empty_success(self):
        c = client()
        stdout = io.StringIO()
        with patch.object(app, "ReadOnlyClient", return_value=c), patch("sys.stdout", stdout):
            status = app.main(["--ambiente", "desarrollo", "--server", "LNK2843", "--vhost", "VS_missing.conf"])
        self.assertEqual(status, 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(set(result), {"estado", "error"})
        self.assertEqual(result["estado"], "error")

    def test_existing_output_reports_actual_reason_before_api_and_preserves_file(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report.json"
            output.write_text("informe anterior", encoding="utf-8")
            stdout = io.StringIO()
            with patch.object(app, "ReadOnlyClient") as constructor, patch("sys.stdout", stdout):
                status = app.main(["--ambiente", "desarrollo", "--server", "LNK2843", "--vhost", VHOST, "--output", str(output)])
            constructor.assert_not_called()
            self.assertEqual(status, 1)
            self.assertIn("ya existe", json.loads(stdout.getvalue())["error"])
            self.assertEqual(output.read_text(), "informe anterior")

    def test_output_directory_and_parent_file_are_distinguished(self):
        with tempfile.TemporaryDirectory() as folder:
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                self.assertEqual(app.main(["--listar-ambientes", "--output", folder]), 1)
            self.assertIn("apunta a una carpeta", json.loads(stdout.getvalue())["error"])
            parent = Path(folder) / "parent-file"
            parent.write_text("preservar", encoding="utf-8")
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                self.assertEqual(app.main(["--listar-ambientes", "--output", str(parent / "report.json")]), 1)
            self.assertIn("parte de la ruta es un archivo", json.loads(stdout.getvalue())["error"])
            self.assertEqual(parent.read_text(), "preservar")

    def test_missing_or_invalid_ca_is_not_reported_as_output_collision(self):
        with tempfile.TemporaryDirectory() as folder:
            ca = Path(folder) / "ca.pem"
            with self.assertRaisesRegex(app.ReportError, "No se encontró el archivo de certificados CA"):
                app.ReadOnlyClient("https://example.console.ves.volterra.io", "placeholder", "desarrollo", str(ca))
            ca.write_text("not-a-certificate", encoding="utf-8")
            with self.assertRaisesRegex(app.ReportError, "PEM válido"):
                app.ReadOnlyClient("https://example.console.ves.volterra.io", "placeholder", "desarrollo", str(ca))

    def test_invalid_url_is_identified_without_request(self):
        for url in ("https://[bad", "https://example.com:not-a-port"):
            with self.assertRaisesRegex(app.ReportError, "URL HTTPS válida"):
                app.ReadOnlyClient(url, "placeholder", "desarrollo")

    def test_write_permission_failure_has_specific_json_message(self):
        with tempfile.TemporaryDirectory() as folder:
            stdout = io.StringIO()
            with patch.object(Path, "open", side_effect=PermissionError), patch("sys.stdout", stdout):
                self.assertEqual(app.main(["--listar-ambientes", "--output", str(Path(folder) / "report.json")]), 1)
            self.assertIn("permisos para escribir", json.loads(stdout.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
