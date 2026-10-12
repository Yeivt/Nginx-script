#!/usr/bin/env python3
"""Reportes por VM con resumen y recomendaciones por línea; solo consultas GET."""
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

# CONFIGURACIÓN PRINCIPAL: editar estos valores en tu copia local.
TENANT_URL = "https://redlink.console.ves.volterra.io"
NAMESPACE = "desarrollo"

# >>>>> PEGA TU API TOKEN AQUÍ DENTRO DE LAS COMILLAS <<<<<
API_TOKEN = "aca va el token de apitoken"

CATEGORIES = {
    "best_practice": "Mejores prácticas",
    "optimization": "Optimización",
    "security": "Seguridad",
    "other": "Otras categorías",
}

# Traducciones locales exactas. Los mensajes no incluidos se conservan en su idioma.
# No se usa ningún servicio de traducción externo ni se envía información del tenant.
MESSAGE_TRANSLATIONS = {
    "default_server parameter is not set for the listen directive":
        "El parámetro default_server no está configurado para la directiva listen",
}
SEVERITIES = {
    "warning": "Advertencia", "warn": "Advertencia", "error": "Error",
    "critical": "Crítica", "high": "Alta", "medium": "Media", "low": "Baja",
    "info": "Información", "informational": "Información",
}
EMPTY_MESSAGES = {
    "best_practice": "NGINX One no reporta cambios de mejores prácticas para este archivo.",
    "optimization": "NGINX One no reporta cambios de optimización para este archivo.",
    "security": "NGINX One no reporta cambios de seguridad para este archivo.",
    "other": "NGINX One no reporta cambios adicionales para este archivo.",
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
        if not isinstance(NAMESPACE, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", NAMESPACE):
            raise ReportError("NAMESPACE debe ser un nombre válido, sin rutas ni parámetros.")
        if not token or token == "aca va el token de apitoken" or any(c.isspace() for c in token):
            raise ReportError("Completa API_TOKEN o NGINX_ONE_API_TOKEN con un token válido, sin espacios.")
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
            "object_id", "hostname", "system_id", "nginx_build", "last_reported", "recommendations"
        ) if name in instance}
        record = {"instance": metadata, "report": None, "state": "", "error": ""}
        try:
            data = client.get(f"/instances/{instance['object_id']}/config-report")
            if data is None:
                record["state"] = "PENDIENTE: NGINX One todavía no completó el análisis (HTTP 204)."
                failed = True
            else:
                record["report"] = validate_report(data)
                record["state"] = "Consulta completada para esta instancia"
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


def category_of(rule):
    category = rule.get("category", "").strip().lower()
    return category if category in CATEGORIES else "other"


def locations_of(rule):
    """Una misma ubicación repetida dentro de una regla no duplica el detalle."""
    result, seen = [], set()
    for location in rule.get("where") or [{}]:
        identity = json.dumps(location, sort_keys=True, ensure_ascii=False)
        if identity not in seen:
            seen.add(identity)
            result.append(location)
    return result


def inventory_counts(instance):
    """Contadores del inventario; no asumir cero para categorías ausentes o inválidas."""
    summary = instance.get("recommendations")
    if not isinstance(summary, list):
        return {}
    counts, seen = {}, set()
    for item in summary:
        if not isinstance(item, dict):
            return {}
        kind, count = item.get("type"), item.get("count")
        if not isinstance(kind, str) or type(count) is not int or count < 0:
            return {}
        kind = kind.strip().lower()
        if kind in seen:
            return {}
        seen.add(kind)
        category = kind if kind in CATEGORIES else "other"
        counts[category] = counts.get(category, 0) + count
    return counts


def summary_lines(record):
    counts = inventory_counts(record["instance"])
    rules = record["report"]
    lines = ["", "RESUMEN GENERAL DE LA INSTANCIA"]
    for category, title in CATEGORIES.items():
        selected = [] if rules is None else [r for r in rules if category_of(r) == category]
        occurrences = sum(len(locations_of(r)) for r in selected)
        if category in counts:
            lines.append(f"  {title}: {counts[category]} (contador informado por NGINX One)")
        elif rules is not None:
            lines.append(f"  {title}: {occurrences} (ubicaciones calculadas desde config-report)")
        else:
            lines.append(f"  {title}: no disponible")
    if rules is not None:
        lines.append(f"  Detalle recuperado: {len(rules)} reglas y "
                     f"{sum(len(locations_of(r)) for r in rules)} ubicaciones.")
        lines.append("  Una regla puede tener varias ubicaciones; los contadores del inventario y el análisis pueden actualizarse en momentos distintos.")
    else:
        lines.append("  Los contadores disponibles no sustituyen el detalle: el análisis no se pudo recuperar.")
    return lines


def translated_message(message):
    return MESSAGE_TRANSLATIONS.get(message.strip().rstrip(".").casefold(), message)


def finding_lines(rule, location, index):
    message = location.get("message") or rule.get("info") or "La plataforma no informó un comentario."
    friendly = translated_message(message)
    severity = rule.get("severity") or "No informada"
    level = SEVERITIES.get(severity.lower(), severity)
    line_number = location.get("line")
    position = f" en la línea {line_number}" if line_number is not None else "; línea no informada"
    title = CATEGORIES[category_of(rule)]
    lines = [f"    {index}. {title} — {level}{position}",
             "       Qué señala NGINX One: " + friendly]
    if location.get("directive"):
        lines.append("       Directiva a revisar: " + location["directive"])
    if rule.get("rule"):
        lines.append("       Regla de NGINX One: " + rule["rule"])
    if category_of(rule) == "other":
        lines.append("       Categoría original: " + (rule.get("category") or "No informada"))
    if friendly != message:
        lines.append("       Comentario original: " + message)
    if rule.get("info") and rule["info"] != message:
        lines.append("       Contexto de la regla (original): " + rule["info"])
    # Orientación local únicamente para el mensaje conocido del ejemplo.
    # El endpoint no garantiza un campo separado de remediación.
    if (rule.get("rule") == "default_server_not_set" and
            message.strip().rstrip(".").casefold() in MESSAGE_TRANSLATIONS):
        lines.append("       Orientación del script: revisar qué bloque server debe ser el predeterminado y, si corresponde, añadir default_server a su directiva listen. No añadirlo a todos los vhosts: debe ser único para cada combinación de dirección y puerto.")
    else:
        lines.append("       Qué revisar: la ubicación indicada, siguiendo el comentario y la documentación de NGINX One.")
    for link in rule.get("documentation", []):
        lines.append("       Documentación de NGINX One: " + link)
    return lines + [""]


def report_text(records, generated):
    hostnames = sorted({str(r["instance"].get("hostname") or "Sin hostname") for r in records})
    lines = ["RECOMENDACIONES DE NGINX ONE", "VM: " + ", ".join(hostnames),
             "Namespace: " + NAMESPACE, "Generado (UTC): " + generated,
             "Los comentarios originales se muestran tal cual los devuelve NGINX One; las traducciones locales se acompañan del original.",
             "Las ubicaciones de archivos se toman según las directivas informadas por NGINX One.",
             "Consultas de lectura; ninguna corrección aplicada.", ""]
    for record in records:
        instance = record["instance"]
        build = instance.get("nginx_build") or {}
        version = build.get("version", "No informada") if isinstance(build, dict) else "No informada"
        lines.extend(["INSTANCIA: " + instance["object_id"],
                      "Hostname: " + str(instance.get("hostname", "No informado")),
                      "Versión NGINX: " + str(version),
                      "Último reporte del agente: " + str(instance.get("last_reported", "No informado")),
                      "Estado de consulta: " + record["state"]])
        lines.extend(summary_lines(record))
        if record["report"] is None:
            if record["error"]:
                lines.append(record["error"])
            lines.extend(["No se pudo determinar si existen recomendaciones.", ""])
            continue
        rules = record["report"]
        if not rules:
            lines.extend(["La API devolvió una lista vacía de recomendaciones; esto no certifica seguridad.", ""])
            continue
        files = defaultdict(lambda: defaultdict(list))
        for rule in rules:
            for location in locations_of(rule):
                files[location.get("file") or "Sin archivo informado"][category_of(rule)].append((rule, location))
        for filename in sorted(files):
            lines.extend(["", "ARCHIVO: " + filename])
            for category, title in CATEGORIES.items():
                lines.append("  " + title)
                findings = files[filename][category]
                if not findings:
                    lines.extend(["    " + EMPTY_MESSAGES[category], ""])
                    continue
                number = len(findings)
                sentence = "Se encontró 1 recomendación" if number == 1 else f"Se encontraron {number} recomendaciones"
                lines.append(f"    {sentence} para {filename} (una por ubicación señalada).")
                for index, (rule, location) in enumerate(findings, 1):
                    lines.extend(finding_lines(rule, location, index))
    return "\n".join(lines) + "\n"


def report_html(text):
    # HTML autónomo. Escapar siempre el contenido que proviene de la API.
    parts = []
    for line in text.splitlines():
        escaped = html.escape(line.strip())
        if not line:
            continue
        if line == "RECOMENDACIONES DE NGINX ONE":
            parts.append("<h1>" + escaped + "</h1>")
        elif line.startswith("INSTANCIA:"):
            parts.append("<h2>" + escaped + "</h2>")
        elif line.startswith("ARCHIVO:"):
            parts.append('<h3 class="file">' + escaped + "</h3>")
        elif line == "RESUMEN GENERAL DE LA INSTANCIA":
            parts.append('<h3 class="summary">' + escaped + "</h3>")
        elif line.startswith("  ") and line.strip() in CATEGORIES.values():
            parts.append("<h4>" + escaped + "</h4>")
        else:
            emphasis = bool(re.match(r"\s+\d+\. ", line))
            parts.append('<p class="' + ("finding" if emphasis else "detail") + '">' + escaped + "</p>")
    return ('<!doctype html><html lang="es"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Recomendaciones NGINX One</title><style>'
            'body{font:16px system-ui;margin:2rem auto;padding:0 1rem;max-width:1050px;color:#17202a;line-height:1.5}'
            'h1{font-size:1.8rem}h2{border-bottom:2px solid #19736a;padding-top:1rem}'
            '.file{background:#e8f3f0;padding:.7rem;border-left:4px solid #19736a;overflow-wrap:anywhere}'
            '.summary{color:#19736a}h4{margin-bottom:.4rem;font-size:1.1rem}'
            'p{white-space:pre-wrap;overflow-wrap:anywhere;margin:.3rem 0}'
            '.finding{font-weight:bold;margin-top:1rem}.detail{padding-left:1rem}'
            '@media print{body{margin:0;font-size:11pt}h2,h3,h4{break-after:avoid}.finding{break-after:avoid}}</style>'
            '<body>' + "\n".join(parts) + '</body></html>\n')


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
    parser.add_argument("--tenant-url", default=os.environ.get("NGINX_ONE_TENANT_URL") or TENANT_URL,
                        help="Origen HTTPS del tenant; por defecto NGINX_ONE_TENANT_URL o TENANT_URL.")
    parser.add_argument("--output", type=Path, default=Path("reportes"))
    parser.add_argument("--format", choices=("html", "txt"), default="html")
    parser.add_argument("--save-json", action="store_true", help="Agrega la respuesta original por VM.")
    parser.add_argument("--ca-bundle", default=os.environ.get("REQUESTS_CA_BUNDLE") or
                        os.environ.get("SSL_CERT_FILE"), help="CA corporativa PEM; TLS siempre verificado.")
    args = parser.parse_args(argv)
    try:
        if not args.tenant_url:
            raise ReportError("Falta --tenant-url o NGINX_ONE_TENANT_URL.")
        token = API_TOKEN if API_TOKEN and API_TOKEN != "aca va el token de apitoken" else os.environ.get("NGINX_ONE_API_TOKEN")
        client = ReadOnlyClient(args.tenant_url, token, args.ca_bundle)
        groups, failed = collect(client)
        if not groups:
            print(f"La API devolvió cero instancias en {NAMESPACE}. No se generaron archivos.")
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
