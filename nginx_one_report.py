#!/usr/bin/env python3
"""Exporta recomendaciones existentes de NGINX One mediante GET exclusivamente."""
import argparse
import hashlib
import html
import json
import os
import re
import ssl
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

NAMESPACE = "desarrollo"
CATEGORIES = {
    "best_practice": "Mejores prácticas",
    "optimization": "Optimización",
    "security": "Seguridad",
    "other": "Otras categorías",
}


class ReportError(Exception):
    """Fallo de consulta o respuesta incompatible; no implica ausencia de hallazgos."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReportError("Redirección bloqueada para proteger el token y el alcance de lectura.")


class ReadOnlyClient:
    def __init__(self, tenant_url, token, ca_bundle=None, timeout=30):
        parsed = urlsplit(tenant_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ReportError("El tenant debe ser un origen HTTPS sin ruta, usuario ni parámetros.")
        if not token or any(c.isspace() for c in token):
            raise ReportError("NGINX_ONE_API_TOKEN debe contener un token, sin espacios.")
        self.base = tenant_url.rstrip("/") + "/api/nginx/one/namespaces/" + NAMESPACE
        self.token = token
        self.timeout = timeout
        context = ssl.create_default_context(cafile=ca_bundle)
        self.opener = build_opener(HTTPSHandler(context=context), NoRedirects())

    def get(self, path, params=""):
        # Lista cerrada de rutas: no acepta URLs arbitrarias ni otros namespaces.
        if path != "/instances" and not re.fullmatch(
                r"/instances/inst_[A-Za-z0-9_-]+/config-report", path):
            raise ReportError("Ruta fuera de las consultas de lectura permitidas.")
        if params and (path != "/instances" or not re.fullmatch(
                r"\?paginated=true&limit=100&offset=[1-9][0-9]*", params)):
            raise ReportError("Parámetros fuera de las consultas permitidas.")
        request = Request(self.base + path + params, method="GET", headers={
            "Authorization": "APIToken " + self.token,
            "Accept": "application/json",
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status == 204:
                    return None
                if response.status != 200:
                    raise ReportError(f"Respuesta HTTP inesperada: {response.status}.")
                return json.load(response)
        except HTTPError as exc:
            # No registrar headers, cuerpos de error ni credenciales.
            raise ReportError(f"HTTP {exc.code} en la consulta GET.") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise ReportError("Falló la conexión HTTPS; revisar red, certificado y timeout.") from None
        except (ValueError, UnicodeError):
            raise ReportError("La API no devolvió JSON válido.") from None

    def instances(self):
        result, seen, offset = [], set(), 1
        expected = None
        while True:
            page = self.get("/instances", f"?paginated=true&limit=100&offset={offset}")
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise ReportError("Formato inesperado en la lista de instancias.")
            count = page.get("count")
            if type(count) is not int or count < 0:
                raise ReportError("Falta un contador de paginación válido.")
            if expected is None:
                expected = count
            elif expected != count:
                raise ReportError("Cambió el inventario durante la lectura; volver a ejecutar.")
            items = page["items"]
            for item in items:
                if not isinstance(item, dict):
                    raise ReportError("Instancia con formato inesperado.")
                instance_id = item.get("object_id", "")
                if not isinstance(instance_id, str) or not re.fullmatch(r"inst_[A-Za-z0-9_-]+", instance_id):
                    raise ReportError("Identificador de instancia ausente o inválido.")
                if instance_id in seen:
                    raise ReportError("Instancia repetida al paginar; inventario incompleto.")
                if item.get("namespace", NAMESPACE) != NAMESPACE:
                    raise ReportError("La API devolvió una instancia de otro namespace.")
                seen.add(instance_id)
                result.append(item)
            if len(result) == expected:
                return result
            if not items or len(result) > expected:
                raise ReportError("Paginación inconsistente; no se generará un inventario parcial.")
            offset += len(items)


def validate_report(data):
    if not isinstance(data, list):
        raise ReportError("config-report debe devolver una lista de recomendaciones.")
    for rule in data:
        if not isinstance(rule, dict):
            raise ReportError("Recomendación con formato inesperado.")
        for name in ("rule", "info", "severity", "category"):
            if name in rule and not isinstance(rule[name], str):
                raise ReportError(f"Campo {name} con formato inesperado.")
        for name in ("where", "documentation"):
            if name in rule and not isinstance(rule[name], list):
                raise ReportError(f"Campo {name} con formato inesperado.")
        if any(not isinstance(x, str) for x in rule.get("documentation", [])):
            raise ReportError("Enlaces de documentación con formato inesperado.")
        for location in rule.get("where", []):
            if not isinstance(location, dict):
                raise ReportError("Ubicación de recomendación con formato inesperado.")
            for name in ("file", "directive", "message"):
                if name in location and not isinstance(location[name], str):
                    raise ReportError(f"Ubicación: campo {name} inválido.")
            if "line" in location and type(location["line"]) is not int:
                raise ReportError("Número de línea inválido.")
    return data


def collect(client):
    groups = defaultdict(list)
    failed = False
    for instance in client.instances():
        # Exportar solo metadatos necesarios, nunca claves de registro del agente.
        metadata = {name: instance[name] for name in (
            "object_id", "hostname", "system_id", "nginx_build", "last_reported"
        ) if name in instance}
        record = {"instance": metadata, "report": None, "state": "", "error": ""}
        try:
            data = client.get(f"/instances/{instance['object_id']}/config-report")
            if data is None:
                record["state"] = "PENDIENTE: NGINX One todavía no completó el análisis (HTTP 204)."
                failed = True
            else:
                record["report"] = validate_report(data)
                record["state"] = "Consulta completada"
        except ReportError as exc:
            record["state"] = "NO DISPONIBLE"
            record["error"] = str(exc)
            failed = True
        # system_id identifica el host; varias instancias de la misma VM se consolidan.
        key = instance.get("system_id") or instance.get("hostname") or instance["object_id"]
        if not isinstance(key, str):
            raise ReportError("Identificador de host inválido.")
        groups[key].append(record)
    return groups, failed


def report_text(records, generated):
    hostnames = sorted({str(r["instance"].get("hostname") or "Sin hostname") for r in records})
    lines = ["RECOMENDACIONES DE NGINX ONE", "VM: " + ", ".join(hostnames),
             "Namespace: desarrollo", "Generado (UTC): " + generated,
             "Origen: GET config-report. Consultas de lectura; ninguna corrección aplicada.",
             "Los comentarios se conservan tal como los devuelve la plataforma.",
             "Las ubicaciones son archivos/directivas; no se infiere la relación archivo-servicio.", ""]
    for record in records:
        instance = record["instance"]
        build = instance.get("nginx_build") or {}
        version = build.get("version", "No informada") if isinstance(build, dict) else "No informada"
        lines.extend(["INSTANCIA: " + instance["object_id"],
                      "Hostname: " + str(instance.get("hostname", "No informado")),
                      "Versión NGINX: " + str(version),
                      "Último reporte del agente: " + str(instance.get("last_reported", "No informado")),
                      "Estado de consulta: " + record["state"]])
        if record["report"] is None:
            if record["error"]:
                lines.append(record["error"])
            lines.extend(["No se pudo determinar si existen recomendaciones.", ""])
            continue
        rules = record["report"]
        lines.append(f"Reglas reportadas: {len(rules)}")
        if not rules:
            lines.extend(["La API devolvió una lista vacía de recomendaciones; esto no certifica seguridad.", ""])
            continue
        files = defaultdict(lambda: defaultdict(list))
        for rule in rules:
            category = rule.get("category", "").lower()
            bucket = category if category in CATEGORIES else "other"
            for location in rule.get("where") or [{}]:
                files[location.get("file") or "Sin archivo informado"][bucket].append((rule, location))
        for filename in sorted(files):
            lines.extend(["", "ARCHIVO: " + filename])
            for category, title in CATEGORIES.items():
                lines.append("  " + title)
                findings = files[filename][category]
                if not findings:
                    lines.append("    Sin hallazgos devueltos para este archivo y categoría.")
                for rule, location in findings:
                    lines.extend([
                        "    Regla: " + (rule.get("rule") or "No informada"),
                        "    Severidad: " + (rule.get("severity") or "No informada"),
                        "    Categoría original: " + (rule.get("category") or "No informada"),
                        "    Comentario de la plataforma: " + (rule.get("info") or "No informado"),
                    ])
                    if "line" in location:
                        lines.append("    Línea: " + str(location["line"]))
                    if location.get("directive"):
                        lines.append("    Directiva: " + location["directive"])
                    if location.get("message"):
                        lines.append("    Detalle de ubicación: " + location["message"])
                    for link in rule.get("documentation", []):
                        lines.append("    Documentación: " + link)
                    lines.append("")
    return "\n".join(lines) + "\n"


def report_html(text):
    # Sin scripts ni contenido remoto; el contenido de la API siempre se escapa.
    return ('<!doctype html><html lang="es"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Recomendaciones NGINX One</title><style>'
            'body{font:16px system-ui;margin:2rem;color:#17202a}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.5}'
            '@media print{body{margin:0;font-size:11pt}}</style>'
            '<body><pre>' + html.escape(text) + '</pre></body></html>\n')


def write_reports(groups, output, file_format, save_json=False):
    generated = datetime.now(timezone.utc).isoformat()
    output.mkdir(parents=True, exist_ok=True)
    # Carpeta nueva por ejecución; no sobrescribe informes existentes.
    run_dir = output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir.mkdir(mode=0o700)
    for key, records in groups.items():
        hostname = str(records[0]["instance"].get("hostname") or "VM")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", hostname).strip("_")[:80] or "VM"
        suffix = hashlib.sha256(key.encode()).hexdigest()[:12]
        name = safe + "_" + suffix
        text = report_text(records, generated)
        content = report_html(text) if file_format == "html" else text
        with (run_dir / (name + "." + file_format)).open("x", encoding="utf-8") as stream:
            stream.write(content)
        if save_json:
            with (run_dir / (name + ".json")).open("x", encoding="utf-8") as stream:
                json.dump({"namespace": NAMESPACE, "generated_at": generated,
                           "instances": records}, stream, ensure_ascii=False, indent=2)
    return run_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-url", default=os.environ.get("NGINX_ONE_TENANT_URL"),
                        help="Origen HTTPS del tenant, o NGINX_ONE_TENANT_URL.")
    parser.add_argument("--output", type=Path, default=Path("reportes"))
    parser.add_argument("--format", choices=("html", "txt"), default="html")
    parser.add_argument("--save-json", action="store_true", help="Agrega la respuesta original por VM.")
    parser.add_argument("--ca-bundle", default=os.environ.get("REQUESTS_CA_BUNDLE") or
                        os.environ.get("SSL_CERT_FILE"), help="CA corporativa PEM; TLS siempre verificado.")
    args = parser.parse_args(argv)
    try:
        if not args.tenant_url:
            raise ReportError("Falta --tenant-url o NGINX_ONE_TENANT_URL.")
        client = ReadOnlyClient(args.tenant_url, os.environ.get("NGINX_ONE_API_TOKEN"), args.ca_bundle)
        groups, failed = collect(client)
        if not groups:
            print("La API devolvió cero instancias en desarrollo. No se generaron archivos.")
            return 0
        run_dir = write_reports(groups, args.output, args.format, args.save_json)
        print(f"Generados {len(groups)} informes por VM en {run_dir}")
        if failed:
            print("Reporte incompleto: hay análisis pendientes o consultas fallidas.", file=sys.stderr)
        return 2 if failed else 0
    except ReportError as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Error de configuración TLS/URL o escritura de archivos locales.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
