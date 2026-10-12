#!/usr/bin/env python3
"""Salud HTTP y upstreams mediante consultas de lectura a NGINX One."""
import argparse
import json
import math
import html
import os
import re
import ssl
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

# CONFIGURACIÓN PRINCIPAL: editar estos valores en tu copia local.
TENANT_URL = "https://redlink.console.ves.volterra.io"
NAMESPACE = "desarrollo"

# >>>>> PEGA TU API TOKEN AQUÍ DENTRO DE LAS COMILLAS <<<<<
API_TOKEN = "aca va el token de apitoken"

WINDOW_MINUTES = 15
RESOLUTION = "1m"
STALE_SECONDS = 180
SERIES_LIMIT = 1000
REQUEST_METRIC = "nginx.http.request.count"
RESPONSE_METRIC = "nginx.http.response.count"
UPSTREAM_METRIC = "nginx.http.upstream.peer.count"
PEER_STATES = ("UP", "DOWN", "UNHEALTHY", "UNAVAILABLE", "DRAINING", "CHECKING")

# Asociaciones explícitas de tu ejemplo. Solo se muestran si hay telemetría
# de alguna de estas zonas/upstreams; no se presupone que existan en cada VM.
SERVICE_MAP = {
    "fraudprevention": {
        "server_zone": "fraudprevention",
        "location_zone": "/location/root/fraudprevention",
        "upstreams": ["fraudprevention"],
        "vhost": "/etc/nginx/conf.d/VS_fraudprevention.conf",
        "includes": ["/etc/nginx/conf.d/lua/lua_fraudprevention.conf",
                     "/etc/nginx/conf.d/upstreams/upstream_fraudprevention.conf"],
        "access_log": "/var/log/nginx/access_fraudprevention.log",
        "error_log": "/var/log/nginx/error_fraudprevention.log",
    }
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
        self.allowed_instances = set()
        self.token = token
        self.timeout = timeout
        context = ssl.create_default_context(cafile=ca_bundle)
        self.opener = build_opener(HTTPSHandler(context=context), NoRedirects())

    def get(self, path, params=""):
        if path != "/instances" or not re.fullmatch(
                r"\?paginated=true&limit=100&offset=[1-9][0-9]*", params):
            raise ReportError("Solo se permite listar las instancias del namespace configurado.")
        return self._request("GET", path + params)

    def _request(self, method, path, payload=None):
        if method == "GET":
            if payload is not None or not re.fullmatch(
                    r"/instances\?paginated=true&limit=100&offset=[1-9][0-9]*", path):
                raise ReportError("Consulta GET no permitida.")
        elif method == "POST":
            if path != "/monitor/metrics_query_topx" or not isinstance(payload, dict):
                raise ReportError("Solo se permite POST al endpoint de consulta de métricas.")
        else:
            raise ReportError("Método no permitido: no se pueden modificar recursos.")
        headers = {"Authorization": "APIToken " + self.token, "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        request = Request(self.base + path, method=method, headers=headers, data=body)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status == 204:
                    return None
                if response.status != 200:
                    raise ReportError(f"Respuesta HTTP inesperada: {response.status}.")
                return json.load(response)
        except HTTPError as exc:
            # No registrar headers, cuerpos de error ni credenciales.
            raise ReportError(f"HTTP {exc.code} en la consulta de NGINX One.") from None
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
                self.allowed_instances = seen
                return result
            if not items or len(result) > expected:
                raise ReportError("Paginación inconsistente; no se generará un inventario parcial.")
            offset += len(items)

    def query_health(self, instance_id, dimension, metric, qualifier, now):
        if instance_id not in self.allowed_instances:
            raise ReportError("La instancia debe pertenecer al inventario de este namespace.")
        http = metric in (REQUEST_METRIC, RESPONSE_METRIC) and dimension in (
            "instance_object_id", "server_zone", "location_zone")
        upstream = metric == UPSTREAM_METRIC and dimension == "upstream_name"
        if not (http or upstream):
            raise ReportError("Consulta de métricas de salud no permitida.")
        filters = [{"dimension": "instance_object_id", "operator": "=", "values": [instance_id]}]
        if metric == RESPONSE_METRIC:
            if qualifier not in ("4xx", "5xx"):
                raise ReportError("Familia HTTP no permitida.")
            filters.append({"predicate": "AND", "dimension": "status_range", "operator": "=", "values": [qualifier]})
        elif upstream:
            if qualifier not in PEER_STATES:
                raise ReportError("Estado de upstream no permitido.")
            filters.append({"predicate": "AND", "dimension": "peer_state", "operator": "=", "values": [qualifier]})
        elif qualifier:
            raise ReportError("Filtro inesperado en la consulta de solicitudes.")
        aggregate = "max" if upstream else "rate"
        payload = {
            "start_time": (now - timedelta(minutes=WINDOW_MINUTES)).isoformat(),
            "end_time": now.isoformat(), "resolution": RESOLUTION,
            "metrics": [{"name": metric, "aggregate": aggregate}],
            "filter": [{"filterSet": filters}], "group_series_by": dimension,
            "dimensions": [] if dimension == "instance_object_id" else ["instance_object_id"],
            "series_limit": SERIES_LIMIT,
        }
        data = self._request("POST", "/monitor/metrics_query_topx", payload)
        return parse_series(data, metric, aggregate, dimension, qualifier, instance_id, now)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Sin zona horaria")
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ReportError("Fecha de medición inválida o sin zona horaria.") from None


def parse_series(data, metric, aggregate, dimension, qualifier, instance_id, now):
    if not isinstance(data, dict) or not isinstance(data.get("metrics"), list):
        raise ReportError("Respuesta incompatible: falta la lista de métricas.")
    groups, warnings = {}, []
    for entry in data["metrics"]:
        if (not isinstance(entry, dict) or entry.get("metric") != metric
                or entry.get("aggregate", aggregate) != aggregate
                or not isinstance(entry.get("series"), list)):
            raise ReportError("Métrica, agregación o series diferentes a las solicitadas.")
        for series in entry["series"]:
            if not isinstance(series, dict) or not isinstance(series.get("dimensions"), dict):
                raise ReportError("Serie de métricas incompatible.")
            dims = series["dimensions"]
            group = dims.get(dimension)
            if group == "all":
                continue
            if group == "other":
                warnings.append("TopX devolvió 'other': hay series no desglosadas; reporte parcial.")
                continue
            if dims.get("instance_object_id", instance_id) != instance_id or dims.get("namespace", NAMESPACE) != NAMESPACE:
                raise ReportError("La API devolvió datos de otra instancia o namespace.")
            qualifier_name = "peer_state" if metric == UPSTREAM_METRIC else "status_range"
            if qualifier and dims.get(qualifier_name, qualifier) != qualifier:
                raise ReportError("La API devolvió datos de otro estado o familia HTTP.")
            if not isinstance(group, str) or not group:
                warnings.append(f"Hay métricas sin {dimension}; no se asignan a un servicio.")
                continue
            if dimension == "instance_object_id" and group != instance_id:
                raise ReportError("La métrica pertenece a otra instancia.")
            if group in groups:
                raise ReportError("Varias series para el mismo grupo; no se mezclarán sus valores.")
            if not isinstance(series.get("data"), list):
                raise ReportError("Puntos de medición incompatibles.")
            points = {}
            for point in series["data"]:
                if not isinstance(point, dict) or "value" not in point:
                    raise ReportError("Punto de medición incompatible.")
                measured = timestamp(point.get("timestamp"))
                value = point["value"]
                if measured > now + timedelta(seconds=30):
                    raise ReportError("Medición con fecha futura inesperada.")
                if value is None:
                    continue
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ReportError("Valor de métrica inválido.")
                if measured < now - timedelta(minutes=WINDOW_MINUTES):
                    warnings.append("La API devolvió puntos fuera de la ventana solicitada; se omitieron.")
                    continue
                date = measured.isoformat()
                if date in points and points[date] != value:
                    raise ReportError("Valores contradictorios para una misma fecha.")
                points[date] = float(value)
            groups[group] = points
    return groups, warnings


def gather(client, instance, now=None):
    now = now or datetime.now(timezone.utc)
    queries, warnings = {}, []
    for dimension in ("instance_object_id", "server_zone", "location_zone"):
        for label, metric, qualifier in (("requests", REQUEST_METRIC, ""),
                                         ("4xx", RESPONSE_METRIC, "4xx"),
                                         ("5xx", RESPONSE_METRIC, "5xx")):
            queries[f"{dimension}/{label}"] = (dimension, metric, qualifier)
    for state in PEER_STATES:
        queries[f"upstream_name/{state}"] = ("upstream_name", UPSTREAM_METRIC, state)
    results = {}
    for key, args in queries.items():
        try:
            groups, notes = client.query_health(instance["object_id"], *args, now)
            results[key] = {"groups": groups, "error": None}
            warnings.extend(f"{key}: {note}" for note in notes)
        except ReportError as exc:
            results[key] = {"groups": {}, "error": str(exc)}
            warnings.append(f"{key}: {exc}")
    return {"instance": {k: instance[k] for k in ("object_id", "hostname", "status", "nginx_build", "os_version", "last_reported") if k in instance},
            "generated_at": now.isoformat(), "namespace": NAMESPACE,
            "results": results, "warnings": warnings}


def last(points):
    if not points:
        return None, None
    date = max(points, key=timestamp)
    return points[date], date


def http_rows(report, dimension):
    queries = report["results"]
    groups = set()
    for label in ("requests", "4xx", "5xx"):
        groups.update(queries[f"{dimension}/{label}"]["groups"])
    if dimension == "instance_object_id":
        groups.add(report["instance"]["object_id"])
    rows = []
    for group in sorted(groups):
        series = {label: queries[f"{dimension}/{label}"]["groups"].get(group, {})
                  for label in ("requests", "4xx", "5xx")}
        values = {label: last(points) for label, points in series.items()}
        dates = set(series["requests"]) & set(series["4xx"]) & set(series["5xx"])
        shared = max(dates, key=timestamp) if dates else None
        pct4 = pct5 = None
        # Nunca dividir puntos de fechas diferentes ni imputar ceros a series ausentes.
        if shared and series["requests"][shared] > 0:
            denominator = series["requests"][shared]
            e4, e5 = series["4xx"][shared], series["5xx"][shared]
            if e4 + e5 <= denominator * 1.001:
                pct4, pct5 = 100 * e4 / denominator, 100 * e5 / denominator
        messages = []
        if shared and (timestamp(report["generated_at"]) - timestamp(shared)).total_seconds() > STALE_SECONDS:
            messages.append("Porcentajes: DESACTUALIZADOS (ver fecha del intervalo común)")
        for label, (value, date) in values.items():
            if value is None:
                messages.append(f"{label}: SIN DATOS")
            elif (timestamp(report["generated_at"]) - timestamp(date)).total_seconds() > STALE_SECONDS:
                messages.append(f"{label}: DESACTUALIZADO")
            if label in ("4xx", "5xx") and value is not None and value > 0:
                messages.append(f"ERRORES {label} observados en el último intervalo")
        if not messages:
            messages.append("Sin 4xx/5xx en el último intervalo disponible; no prueba disponibilidad")
        rows.append({"group": group, "values": values, "pct4": pct4, "pct5": pct5,
                     "percentage_at": shared, "status": "; ".join(messages)})
    return rows


def upstream_rows(report):
    results = report["results"]
    names = set()
    for state in PEER_STATES:
        names.update(results[f"upstream_name/{state}"]["groups"])
    rows = []
    for name in sorted(names):
        values = {state: last(results[f"upstream_name/{state}"]["groups"].get(name, {})) for state in PEER_STATES}
        alerts = []
        for state, (value, date) in values.items():
            if value is not None and value > 0 and state in ("DOWN", "UNHEALTHY", "UNAVAILABLE"):
                alerts.append(f"{value:g} peer(s) reportados {state} en el intervalo")
            if date and (timestamp(report["generated_at"]) - timestamp(date)).total_seconds() > STALE_SECONDS:
                alerts.append(f"{state}: DESACTUALIZADO")
        if any(value is None for value, date in values.values()):
            alerts.append("Hay estados SIN DATOS: no se consideran cero")
        if not alerts:
            alerts.append("Sin peers DOWN/UNHEALTHY/UNAVAILABLE en los intervalos disponibles; no prueba disponibilidad")
        rows.append({"name": name, "values": values, "status": "; ".join(alerts)})
    return rows


def escaped(value):
    return html.escape(str(value))


def cell(value):
    return "SIN DATOS" if value is None else f"{value:.3f}"


def html_report(report):
    instance = report["instance"]
    parts = ['<!doctype html><html lang="es"><meta charset="utf-8"><title>Salud de servicios — NGINX One</title>',
             '<style>body{font:15px system-ui;margin:2rem;color:#17202a}table{border-collapse:collapse;width:100%;margin-bottom:2rem}td,th{border:1px solid #ccd4da;padding:.6rem;text-align:left;overflow-wrap:anywhere}th{background:#e8f3f0}.notice{background:#fff3cd;padding:1rem}small{display:block}h2{margin-top:2rem}@media print{body{margin:0}h2{break-after:avoid}}</style><body>',
             '<h1>Salud de servicios — ' + escaped(instance.get("hostname", instance["object_id"])) + '</h1>',
             '<p>Namespace: ' + escaped(report["namespace"]) + ' · Instancia: ' + escaped(instance["object_id"]) + '</p>',
             '<p>Generado UTC: ' + escaped(report["generated_at"]) + ' · Estado del agente/instancia: ' + escaped(instance.get("status", "No informado")) + '</p>',
             '<p>SO informado: ' + escaped(instance.get("os_version", "No informado")) + ' · Último reporte del agente: ' + escaped(instance.get("last_reported", "No informado")) + '</p>',
             '<p class="notice">Familias HTTP 4xx/5xx, no códigos exactos 400/500. Ventana de ' + str(WINDOW_MINUTES) + ' minutos. Tasas por segundo del último intervalo disponible de ' + RESOLUTION + '; no son conteos absolutos. Las zonas de servidor y de ubicación se muestran por separado y no deben sumarse. Sin datos no significa cero ni servicio sano.</p>']
    all_rows = {}
    for dimension, title in (("instance_object_id", "Resumen de la instancia"),
                              ("server_zone", "Servicios por server_zone"),
                              ("location_zone", "Ubicaciones por location_zone")):
        rows = http_rows(report, dimension)
        all_rows[dimension] = rows
        parts.append('<h2>' + title + '</h2>')
        if not rows:
            parts.append('<p>No hay telemetría desglosada para esta dimensión. En Open Source o servicios sin zonas instrumentadas no se puede asignar el dato de instancia a un vhost.</p>')
            continue
        parts.append('<table><tr><th>Zona / instancia</th><th>Solicitudes/s</th><th>4xx/s</th><th>5xx/s</th><th>% 4xx / % 5xx</th><th>Observaciones</th></tr>')
        for row in rows:
            parts.append('<tr><td>' + escaped(row["group"]) + '</td>')
            for label in ("requests", "4xx", "5xx"):
                value, date = row["values"][label]
                parts.append('<td>' + cell(value) + '<small>' + escaped(date or "Sin fecha") + '</small></td>')
            parts.append('<td>' + ("No calculable" if row["pct4"] is None else f'{row["pct4"]:.2f} % / {row["pct5"]:.2f} %') + '<small>' + escaped(row["percentage_at"] or "Sin intervalo común") + '</small></td><td>' + escaped(row["status"]) + '</td></tr>')
        parts.append('</table>')
    upstreams = upstream_rows(report)
    parts.append('<h2>Upstreams y peers</h2><p>Máximo de peers por estado en cada intervalo de ' + RESOLUTION + '. No sumar estados: un peer puede cambiar de estado dentro del intervalo. DOWN es el estado reportado por NGINX; no demuestra por sí solo que la VM del backend esté apagada. Esta métrica no permite identificar la dirección IP del peer afectado.</p>')
    if not upstreams:
        parts.append('<p>SIN TELEMETRÍA DE UPSTREAMS. No se puede determinar si los backends están disponibles. Puede tratarse de NGINX Open Source, un servicio estático o telemetría Plus no publicada.</p>')
    else:
        parts.append('<table><tr><th>Upstream</th>' + ''.join('<th>' + state + '</th>' for state in PEER_STATES) + '<th>Observaciones</th></tr>')
        for row in upstreams:
            parts.append('<tr><td>' + escaped(row["name"]) + '</td>')
            for state in PEER_STATES:
                value, date = row["values"][state]
                parts.append('<td>' + cell(value) + '<small>' + escaped(date or "Sin fecha") + '</small></td>')
            parts.append('<td>' + escaped(row["status"]) + '</td></tr>')
        parts.append('</table>')
    observed_servers = {row["group"] for row in all_rows["server_zone"]}
    observed_locations = {row["group"] for row in all_rows["location_zone"]}
    observed_upstreams = {row["name"] for row in upstreams}
    parts.append('<h2>Asociaciones de servicios y logs</h2><p>Asociaciones explícitas del bloque SERVICE_MAP; no se deducen del nombre del archivo. Los paths son referencias aportadas por el operador, no comprobados en la VM. No se leen logs ni archivos de configuración, certificados, claves o passwords. La API pública consultada no documenta acceso a logs de acceso/error.</p>')
    matched = False
    for service, mapping in SERVICE_MAP.items():
        if (mapping.get("server_zone") not in observed_servers and mapping.get("location_zone") not in observed_locations
                and not set(mapping.get("upstreams", [])) & observed_upstreams):
            continue
        matched = True
        parts.append('<h3>' + escaped(service) + '</h3><ul>')
        for key in ("server_zone", "location_zone", "upstreams", "vhost", "includes", "access_log", "error_log"):
            value = mapping.get(key, "No informado")
            if isinstance(value, list):
                value = ", ".join(value)
            parts.append('<li>' + escaped(key) + ': ' + escaped(value) + '</li>')
        parts.append('</ul>')
    if not matched:
        parts.append('<p>No hay asociaciones configuradas para las zonas/upstreams observados. Una ruta como /etc/nginx/conf.d/vhost.conf no constituye una dimensión de métricas de esta API.</p>')
    notes = list(report["warnings"])
    for key, result in report["results"].items():
        if not result["groups"] and not result["error"]:
            notes.append(key + ': sin series disponibles')
    parts.append('<h2>Disponibilidad de datos</h2>')
    parts.extend('<p>' + escaped(note) + '</p>' for note in notes)
    parts.append('<p>RHEL 8/9: el script consulta la consola HTTPS, no ejecuta comandos en el sistema operativo ni activa instrumentación. Un 4xx suele corresponder a una solicitud rechazada; un 5xx no identifica por sí solo la causa ni un backend caído.</p></body></html>')
    return '\n'.join(parts)


def report_partial(report):
    if report["warnings"]:
        return True
    if any(not result["groups"] or result["error"] for result in report["results"].values()):
        return True
    now = timestamp(report["generated_at"])
    if any(row["percentage_at"] and (now - timestamp(row["percentage_at"])).total_seconds() > STALE_SECONDS
           for dimension in ("instance_object_id", "server_zone", "location_zone") for row in http_rows(report, dimension)):
        return True
    return any(not points or (now - timestamp(last(points)[1])).total_seconds() > STALE_SECONDS
               for result in report["results"].values() for points in result["groups"].values())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-url", default=os.environ.get("NGINX_ONE_TENANT_URL") or TENANT_URL)
    parser.add_argument("--instance-id", help="ID de una instancia del inventario del namespace.")
    parser.add_argument("--all", action="store_true", help="Consultar todas las instancias: 15 consultas de métricas por instancia.")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("reportes_salud"))
    parser.add_argument("--save-json", action="store_true")
    parser.add_argument("--ca-bundle", default=os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE"))
    args = parser.parse_args(argv)
    if args.all and args.instance_id:
        parser.error("Usar --all o --instance-id, no ambos.")
    try:
        token = API_TOKEN if API_TOKEN and API_TOKEN != "aca va el token de apitoken" else os.environ.get("NGINX_ONE_API_TOKEN")
        client = ReadOnlyClient(args.tenant_url, token, args.ca_bundle)
        instances = sorted(client.instances(), key=lambda i: (str(i.get("hostname", "")), i["object_id"]))
        for index, instance in enumerate(instances, 1):
            print(f"{index}. {instance.get('hostname', 'Sin hostname')} | {instance['object_id']} | {instance.get('status', 'No informado')}")
        if not instances or args.list_only:
            if not instances:
                print(f"No hay instancias en {NAMESPACE}.")
            return 0
        if args.all:
            selected = instances
        elif args.instance_id:
            selected = [i for i in instances if i["object_id"] == args.instance_id]
            if not selected:
                raise ReportError("El ID no pertenece al inventario del namespace.")
        else:
            while True:
                try:
                    choice = input("Selecciona una instancia (0 para salir): ").strip()
                except EOFError:
                    raise ReportError("Usa --instance-id, --all o --list-only cuando no haya entrada interactiva.") from None
                if choice == "0":
                    return 0
                if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(instances):
                    selected = [instances[int(choice) - 1]]
                    break
                print("Selección inválida.")
        args.output.mkdir(parents=True, exist_ok=True)
        folder = args.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder.mkdir(mode=0o700)
        partial = False
        for instance in selected:
            print(f"Consultando salud de {instance.get('hostname', instance['object_id'])}…")
            report = gather(client, instance)
            name = re.sub(r"[^A-Za-z0-9_-]", "_", str(instance.get("hostname") or "VM"))[:80] + '_' + instance["object_id"]
            with (folder / (name + '.html')).open('x', encoding='utf-8') as stream:
                stream.write(html_report(report))
            if args.save_json:
                with (folder / (name + '.json')).open('x', encoding='utf-8') as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2)
            partial |= report_partial(report)
        print(f"Informes guardados en {folder}")
        if partial:
            print("Hay telemetría ausente, parcial o desactualizada; revisar Disponibilidad de datos.")
        return 2 if partial else 0
    except ReportError as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Error de configuración TLS/URL o escritura local.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nConsulta cancelada; pueden quedar informes locales parciales.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
