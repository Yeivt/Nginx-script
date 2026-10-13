#!/usr/bin/env python3
"""V4: ambiente -> servidor -> archivo; recomendaciones JSON por archivo, solo GET."""
import argparse
import json
import os
import posixpath
import re
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

# CONFIGURACIÓN PRINCIPAL: editar estos valores en tu copia local.
TENANT_URL = "https://redlink.console.ves.volterra.io"
AMBIENTES = ("default", "desarrollo", "homologacion", "produccion")
ambiente = "produccion"
server = ""       # Ejemplo: "LNKXXX". Vacío permite seleccionar del inventario.
vhost = ""        # Ejemplo: "VS_XXXX.conf".
lua = ""          # Ejemplo: "lua_XXX.conf".
upstream = ""     # Ejemplo: "upstream_XXX.conf".
# Seleccionar solo UNO de vhost/lua/upstream para generar un reporte.

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
class ReportError(Exception):
    """Fallo de consulta o respuesta incompatible; no implica ausencia de hallazgos."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReportError("Redirección bloqueada para proteger el token y el alcance de lectura.")


class ReadOnlyClient:
    def __init__(self, tenant_url, token, ambiente, ca_bundle=None, timeout=30):
        parsed = urlsplit(tenant_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ReportError("El tenant debe ser un origen HTTPS sin ruta, usuario ni parámetros.")
        if ambiente not in AMBIENTES:
            raise ReportError("Ambiente no permitido. Usar default, desarrollo, homologacion o produccion.")
        self.ambiente = ambiente
        self._instances = None
        self._catalogs = {}
        if not token or token == "aca va el token de apitoken" or any(c.isspace() for c in token):
            raise ReportError("Completa API_TOKEN o NGINX_ONE_API_TOKEN con un token válido, sin espacios.")
        self.base = tenant_url.rstrip("/") + "/api/nginx/one/namespaces/" + self.ambiente
        self.token = token
        self.timeout = timeout
        context = ssl.create_default_context(cafile=ca_bundle)
        self.opener = build_opener(HTTPSHandler(context=context), NoRedirects())

    def get(self, path, params=""):
        # Lista cerrada de rutas: no acepta URLs arbitrarias ni otros namespaces.
        if path != "/instances" and not re.fullmatch(
                r"/instances/inst_[A-Za-z0-9_-]+/(config|config-report)", path):
            raise ReportError("Ruta fuera de las consultas de lectura permitidas.")
        if path != "/instances" and path.split("/")[2] not in {i["object_id"] for i in self._instances or []}:
            raise ReportError("La instancia debe estar en el inventario del ambiente seleccionado.")
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
        if self._instances is not None:
            return self._instances
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
                if item.get("namespace", self.ambiente) != self.ambiente:
                    raise ReportError("La API devolvió una instancia de otro namespace.")
                seen.add(instance_id)
                result.append(item)
            if len(result) == expected:
                self._instances = result
                return result
            if not items or len(result) > expected:
                raise ReportError("Paginación inconsistente; no se generará un inventario parcial.")
            offset += len(items)

    def listar_servidores(self):
        return sorted([{"ambiente": self.ambiente, "server": i.get("hostname") or "Sin hostname",
                        "instance_id": i["object_id"], "system_id": i.get("system_id"),
                        "estado": i.get("status", "No informado")} for i in self.instances()],
                      key=lambda i: (i["server"], i["instance_id"]))

    def _resolve_server(self, server, instance_id=None):
        matches = [i for i in self.instances() if
                   (not server or i.get("hostname") == server or i["object_id"] == server)
                   and (not instance_id or i["object_id"] == instance_id)]
        if not matches:
            raise ReportError("Servidor/instancia no encontrado en el ambiente seleccionado.")
        if len(matches) != 1:
            raise ReportError("El servidor tiene varias instancias; seleccionar un instance_id de la lista.")
        return matches[0]

    def listar_archivos(self, server, instance_id=None):
        instance = self._resolve_server(server, instance_id)
        identifier = instance["object_id"]
        if identifier in self._catalogs:
            # Devolver copias para que el framework no modifique la caché del cliente.
            return json.loads(json.dumps(self._catalogs[identifier]))
        data = self.get(f"/instances/{identifier}/config")
        files, version, main_path = catalog_files(data)
        result = {"ambiente": self.ambiente, "server": instance.get("hostname"),
                  "instance_id": identifier, "config_version": version,
                  "conf_path": main_path, "origen": "NGINX One: GET config",
                  "archivos": {kind: [f for f in files if f["tipo"] == kind]
                               for kind in ("vhost", "lua", "upstream")}}
        self._catalogs[identifier] = result
        return json.loads(json.dumps(result))

    def obtener_reporte(self, server, *, vhost=None, lua=None, upstream=None, instance_id=None):
        selectors = [(kind, value) for kind, value in
                     (("vhost", vhost), ("lua", lua), ("upstream", upstream)) if value]
        if len(selectors) != 1:
            raise ReportError("Seleccionar exactamente un archivo: vhost, lua o upstream.")
        kind, value = selectors[0]
        instance = self._resolve_server(server, instance_id)
        catalog = self.listar_archivos(server, instance_id=instance["object_id"])
        selected = select_file(catalog, kind, value)
        data = self.get(f"/instances/{instance['object_id']}/config-report")
        return selected_report(instance, catalog, selected, data)


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


def translated_message(message):
    return MESSAGE_TRANSLATIONS.get(message.strip().rstrip(".").casefold(), message)


def listar_ambientes():
    return list(AMBIENTES)


def canonical_path(path):
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise ReportError("Ruta de archivo inválida.")
    if ".." in PurePosixPath(path).parts or not path.startswith("/"):
        raise ReportError("La ruta debe ser absoluta y no contener '..'.")
    return posixpath.normpath(path)


def file_type(path):
    name = PurePosixPath(path).name.lower()
    parent = [part.lower() for part in PurePosixPath(path).parts[:-1]]
    if name.endswith(".lua") or (name.endswith(".conf") and (name.startswith("lua_") or "lua" in parent)):
        return "lua"
    if name.endswith(".conf") and (name.startswith("upstream_") or "upstreams" in parent or "upstream" in parent):
        return "upstream"
    if name.endswith(".conf") and (name.startswith("vs_") or name.startswith("vhost")):
        return "vhost"
    return None


def catalog_files(data):
    if not isinstance(data, dict) or not isinstance(data.get("configs"), list):
        raise ReportError("No se pudo obtener un catálogo de configuración válido; no se asumirá que no hay archivos.")
    main_path = canonical_path(data.get("conf_path"))
    files = {}
    # El endpoint devuelve contenidos; se extraen únicamente nombres/rutas.
    # No decodificar ni exportar contents, payloads, claves o passwords.
    for field in ("configs", "aux"):
        directories = data.get(field, [])
        if not isinstance(directories, list):
            raise ReportError("Catálogo de directorios incompatible.")
        for directory in directories:
            if not isinstance(directory, dict) or not isinstance(directory.get("files"), list):
                raise ReportError("Directorio de configuración incompatible.")
            parent = canonical_path(directory.get("name"))
            for item in directory["files"]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise ReportError("Entrada de archivo incompatible.")
                name = item["name"]
                if not name or name.startswith("/") or ".." in PurePosixPath(name).parts:
                    raise ReportError("El nombre del archivo debe ser relativo al directorio de la API.")
                path = canonical_path(posixpath.join(parent, name))
                kind = file_type(path)
                if kind:
                    files[path] = {"tipo": kind, "nombre": PurePosixPath(path).name, "ruta": path}
    return sorted(files.values(), key=lambda f: f["ruta"]), data.get("config_version"), main_path


def select_file(catalog, kind, value):
    if kind not in ("vhost", "lua", "upstream") or not isinstance(value, str):
        raise ReportError("Selector de archivo inválido.")
    files = catalog["archivos"][kind]
    if value.startswith("/"):
        value = canonical_path(value)
        matches = [f for f in files if f["ruta"] == value]
    else:
        matches = [f for f in files if f["nombre"] == value]
    if not matches:
        raise ReportError("El archivo seleccionado no está en el catálogo del servidor y tipo indicados.")
    if len(matches) > 1:
        raise ReportError("Hay archivos con el mismo nombre; seleccionar la ruta completa del catálogo.")
    return matches[0]


def location_matches(raw, catalog, selected):
    if not isinstance(raw, str) or not raw:
        return False
    if raw.startswith("/"):
        return canonical_path(raw) == selected["ruta"]
    if ".." in PurePosixPath(raw).parts or "\\" in raw:
        raise ReportError("Ubicación relativa incompatible en las recomendaciones.")
    all_files = [f for values in catalog["archivos"].values() for f in values]
    if "/" in raw:
        candidate = canonical_path(posixpath.join(posixpath.dirname(catalog["conf_path"]), raw))
        return candidate == selected["ruta"]
    matches = [f for f in all_files if f["nombre"] == raw]
    if len(matches) > 1 and any(f["ruta"] == selected["ruta"] for f in matches):
        raise ReportError("NGINX One informó solo un nombre repetido; no es posible asignar el hallazgo a una ruta con certeza.")
    return len(matches) == 1 and matches[0]["ruta"] == selected["ruta"]


def selected_report(instance, catalog, selected, data):
    pending = data is None
    build = instance.get("nginx_build") or {}
    report = {"schema_version": "4.0", "ambiente": catalog["ambiente"],
              "server": instance.get("hostname"), "instance_id": instance["object_id"],
              "archivo": dict(selected), "generado_utc": datetime.now(timezone.utc).isoformat(),
              "nginx_version": build.get("version") if isinstance(build, dict) else None,
              "ultimo_reporte_agente": instance.get("last_reported"),
              "config_version_catalogo": catalog.get("config_version"),
              "estado": "pendiente" if pending else "completado",
              "total_recomendaciones_archivo": None if pending else 0,
              "resumen_archivo": {key: None if pending else 0 for key in CATEGORIES},
              "recomendaciones": {key: [] for key in CATEGORIES},
              "avisos": ["El catálogo y config-report son consultas separadas; la API no garantiza un snapshot atómico."]}
    if pending:
        report["avisos"].append("HTTP 204: el análisis todavía no está disponible; no equivale a ausencia de recomendaciones.")
        return report
    unmatched = 0
    for rule in validate_report(data):
        matching = []
        for location in locations_of(rule):
            if location_matches(location.get("file"), catalog, selected):
                matching.append(location)
            elif not location.get("file"):
                unmatched += 1
        if not matching:
            continue
        category = category_of(rule)
        for location in matching:
            message = location.get("message") or rule.get("info") or ""
            severity = rule.get("severity") or ""
            # Lista explícita de campos: no exportar ubicaciones de otros archivos.
            report["recomendaciones"][category].append({
                "regla": rule.get("rule"), "categoria_original": rule.get("category"),
                "severidad": severity, "severidad_es": SEVERITIES.get(severity.lower(), severity),
                "archivo": selected["ruta"], "linea": location.get("line"),
                "directiva": location.get("directive"), "comentario_original": message,
                "comentario_es": translated_message(message), "contexto_original": rule.get("info"),
                "documentacion": rule.get("documentation", []),
            })
            report["resumen_archivo"][category] += 1
    report["total_recomendaciones_archivo"] = sum(report["resumen_archivo"].values())
    if unmatched:
        report["avisos"].append(f"Hay {unmatched} ubicaciones sin archivo informado; no se atribuyen al archivo seleccionado.")
    if not report["total_recomendaciones_archivo"]:
        report["avisos"].append("NGINX One no devolvió recomendaciones con ubicación en este archivo; esto no certifica que no requiera mejoras.")
    return report


def choose(items, labels, title, default=None):
    for index, label in enumerate(labels, 1):
        print(f"{index}. {label}", file=sys.stderr)
    while True:
        print(f"{title} (número; 0 para salir" + (f"; Enter: {default}" if default else "") + "): ", end="", file=sys.stderr)
        try:
            answer = input().strip()
        except EOFError:
            raise ReportError("Faltan selectores: usar argumentos para ejecutar sin interacción.") from None
        if answer == "0":
            return None
        if not answer and default in items:
            return default
        if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        print("Selección inválida.", file=sys.stderr)


def emit_json(data, output=None):
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(text)
    sys.stdout.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ambiente", choices=AMBIENTES)
    parser.add_argument("--server")
    parser.add_argument("--instance-id")
    parser.add_argument("--tenant-url", default=os.environ.get("NGINX_ONE_TENANT_URL") or TENANT_URL)
    files = parser.add_mutually_exclusive_group()
    files.add_argument("--vhost")
    files.add_argument("--lua")
    files.add_argument("--upstream")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--listar-ambientes", action="store_true")
    modes.add_argument("--listar-servidores", action="store_true")
    modes.add_argument("--listar-archivos", action="store_true")
    parser.add_argument("--output", type=Path, help="Guardar también el JSON en un archivo nuevo.")
    parser.add_argument("--ca-bundle", default=os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE"))
    args = parser.parse_args(argv)
    try:
        if args.listar_ambientes:
            emit_json({"ambientes": listar_ambientes()}, args.output)
            return 0
        selected_environment = args.ambiente or choose(listar_ambientes(), listar_ambientes(), "Ambiente", ambiente)
        if selected_environment is None:
            return 0
        token = API_TOKEN if API_TOKEN and API_TOKEN != "aca va el token de apitoken" else os.environ.get("NGINX_ONE_API_TOKEN")
        client = ReadOnlyClient(args.tenant_url, token, selected_environment, args.ca_bundle)
        servers = client.listar_servidores()
        if args.listar_servidores:
            emit_json({"ambiente": selected_environment, "servidores": servers}, args.output)
            return 0
        selected_server = args.server or args.instance_id or server
        instance_id = args.instance_id
        if not selected_server:
            if not servers:
                raise ReportError("No hay servidores en el ambiente seleccionado.")
            choice = choose(servers, [f"{s['server']} | {s['instance_id']}" for s in servers], "Servidor")
            if choice is None:
                return 0
            selected_server, instance_id = choice["server"], choice["instance_id"]
        catalog = client.listar_archivos(selected_server, instance_id)
        if args.listar_archivos:
            emit_json(catalog, args.output)
            return 0
        cli_selectors = {k: getattr(args, k) for k in ("vhost", "lua", "upstream") if getattr(args, k)}
        selectors = cli_selectors or {k: value for k, value in (("vhost", vhost), ("lua", lua), ("upstream", upstream)) if value}
        if not selectors:
            options = [f for values in catalog["archivos"].values() for f in values]
            if not options:
                raise ReportError("No hay archivos vhost/lua/upstream según las convenciones del catálogo.")
            choice = choose(options, [f"{f['tipo']} | {f['ruta']}" for f in options], "Archivo")
            if choice is None:
                return 0
            selectors = {choice["tipo"]: choice["ruta"]}
        result = client.obtener_reporte(selected_server, instance_id=instance_id, **selectors)
        emit_json(result, args.output)
        return 2 if result["estado"] == "pendiente" else 0
    except ReportError as exc:
        emit_json({"schema_version": "4.0", "estado": "error", "error": str(exc)})
        return 1
    except (OSError, ValueError):
        emit_json({"schema_version": "4.0", "estado": "error", "error": "Error de configuración TLS/URL o escritura local; el archivo de salida debe ser nuevo."})
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
