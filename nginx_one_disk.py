#!/usr/bin/env python3
"""Lista instancias de NGINX One y consulta el uso de disco de una seleccionada."""
import argparse
import json
import math
import csv
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

METRIC = "system.filesystem.usage"
WINDOW_MINUTES = 5
RESOLUTION = "30s"
STALE_SECONDS = 120
SERIES_LIMIT = 1000


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

    def disk_usage(self, instance_id, now=None):
        if instance_id not in self.allowed_instances:
            raise ReportError("La instancia debe pertenecer al inventario del namespace seleccionado.")
        now = now or datetime.now(timezone.utc)
        payload = {
            "start_time": (now - timedelta(minutes=WINDOW_MINUTES)).isoformat(),
            "end_time": now.isoformat(),
            "resolution": RESOLUTION,
            "metrics": [{"name": METRIC, "aggregate": "avg"}],
            "filter": [{"filterSet": [
                {"dimension": "instance_object_id", "operator": "=", "values": [instance_id]},
                {"predicate": "AND", "dimension": "state", "operator": "=", "values": ["in_use"]},
            ]}],
            "group_series_by": "mount_point",
            "dimensions": ["instance_object_id", "state"],
            "series_limit": SERIES_LIMIT,
        }
        data = self._request("POST", "/monitor/metrics_query_topx", payload)
        return parse_disk_usage(data, instance_id, now)


def timestamp(value):
    if not isinstance(value, str):
        raise ReportError("Fecha de medición inválida.")
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("Fecha sin zona horaria")
        return date.astimezone(timezone.utc)
    except ValueError:
        raise ReportError("Fecha de medición inválida o sin zona horaria.") from None


def parse_disk_usage(data, instance_id, now):
    if not isinstance(data, dict) or not isinstance(data.get("metrics"), list):
        raise ReportError("Respuesta incompatible: falta la lista de métricas.")
    rows, warnings, seen = [], [], set()
    for metric in data["metrics"]:
        if not isinstance(metric, dict) or metric.get("metric") != METRIC:
            raise ReportError("La API devolvió una métrica diferente a la solicitada.")
        if metric.get("aggregate", "avg") != "avg" or not isinstance(metric.get("series"), list):
            raise ReportError("Agregación o series de métricas incompatibles.")
        for series in metric["series"]:
            if not isinstance(series, dict) or not isinstance(series.get("dimensions"), dict):
                raise ReportError("Serie de métricas incompatible.")
            dimensions = series["dimensions"]
            mount = dimensions.get("mount_point")
            if mount == "all":
                continue  # Total sintético de TopX; no es una partición.
            if mount == "other":
                warnings.append("Hay series agrupadas en 'other'; el listado de puntos de montaje es incompleto.")
                continue
            if not isinstance(mount, str) or not mount.startswith("/"):
                raise ReportError("La serie no identifica un punto de montaje válido.")
            # Nunca mostrar una serie atribuida a otra instancia o a otro estado.
            if dimensions.get("instance_object_id", instance_id) != instance_id:
                raise ReportError("La API devolvió datos de otra instancia.")
            if dimensions.get("state", "in_use") != "in_use":
                raise ReportError("La API devolvió datos de otro estado del disco.")
            if dimensions.get("namespace", NAMESPACE) != NAMESPACE:
                raise ReportError("La API devolvió datos de otro namespace.")
            if mount in seen:
                raise ReportError("Hay varias series para el mismo montaje; no se mezclarán sus valores.")
            seen.add(mount)
            points = series.get("data")
            if not isinstance(points, list):
                raise ReportError("Puntos de medición incompatibles.")
            candidates = []
            for point in points:
                if not isinstance(point, dict) or "value" not in point:
                    raise ReportError("Punto de medición incompatible.")
                measured = timestamp(point.get("timestamp"))
                value = point["value"]
                if value is None:
                    continue  # Hueco de telemetría: no significa cero.
                if (type(value) not in (int, float) or not math.isfinite(value)
                        or not 0 <= value <= 100):
                    raise ReportError("Porcentaje inválido; no se adivinará ni convertirá su escala.")
                if measured > now + timedelta(seconds=30):
                    raise ReportError("La medición tiene una fecha futura inesperada.")
                candidates.append((measured, float(value)))
            if not candidates:
                rows.append({"mount_point": mount, "usage_percent": None,
                             "measured_at": None, "age_seconds": None, "status": "SIN DATOS"})
                warnings.append(f"No hay valores disponibles para {mount} en los últimos {WINDOW_MINUTES} minutos.")
                continue
            measured, value = max(candidates, key=lambda point: point[0])
            age = max(0, (now - measured).total_seconds())
            stale = age > STALE_SECONDS
            rows.append({"mount_point": mount, "usage_percent": value,
                         "measured_at": measured.isoformat(), "age_seconds": round(age),
                         "status": "DESACTUALIZADO" if stale else "DISPONIBLE"})
            if stale:
                warnings.append(f"La última medición de {mount} tiene {round(age)} segundos de antigüedad.")
    if not rows:
        warnings.append(f"NGINX One no devolvió puntos de montaje con telemetría para esta instancia en los últimos {WINDOW_MINUTES} minutos.")
    return sorted(rows, key=lambda row: row["mount_point"]), warnings


def list_instances(instances):
    print(f"\nINSTANCIAS DEL NAMESPACE: {NAMESPACE}\n")
    for index, instance in enumerate(instances, 1):
        print(f"{index:>3}. {instance.get('hostname', 'Sin hostname')} | "
              f"{instance['object_id']} | Estado: {instance.get('status', 'No informado')}")


def select_instance(instances, selected_id=None):
    if selected_id is not None:
        for instance in instances:
            if instance["object_id"] == selected_id:
                return instance
        raise ReportError("El ID indicado no pertenece al inventario de este namespace.")
    while True:
        try:
            choice = input("\nSelecciona el número de la instancia (0 para salir): ").strip()
        except EOFError:
            raise ReportError("No hay entrada interactiva; usar --instance-id o --list-only.") from None
        if choice == "0":
            return None
        if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(instances):
            return instances[int(choice) - 1]
        print("Selección inválida. Ingresa un número de la lista.")


def display_disk(instance, rows, warnings):
    print(f"\nUSO DE DISCO — {instance.get('hostname', 'Sin hostname')}")
    print(f"Instancia: {instance['object_id']} | Namespace: {NAMESPACE}")
    print(f"NGINX One: último intervalo disponible, promedio de {RESOLUTION}. No es una consulta directa a la VM.")
    width = max([18] + [len(row["mount_point"]) for row in rows])
    print(f"{'Punto de montaje':<{width}}  {'Uso':>9}  {'Medición (UTC)':<32}  Estado")
    print("-" * (width + 65))
    for row in rows:
        usage = "SIN DATOS" if row["usage_percent"] is None else f"{row['usage_percent']:.2f} %"
        print(f"{row['mount_point']:<{width}}  {usage:>9}  {row['measured_at'] or '-':<32}  {row['status']}")
    for message in warnings:
        print("AVISO: " + message)


def save_csv(path, instance, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    # No sobrescribir reportes existentes. UTF-8 BOM facilita abrirlo en Excel.
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["VM", "Instancia", "Namespace", "Montaje", "Uso (%)",
                         "Medición UTC", "Antigüedad (s)", "Estado"])
        for row in rows:
            fields = [instance.get("hostname", ""), instance["object_id"], NAMESPACE,
                      row["mount_point"], "" if row["usage_percent"] is None else f"{row['usage_percent']:.2f}",
                      row["measured_at"] or "", row["age_seconds"] if row["age_seconds"] is not None else "", row["status"]]
            # Evitar interpretación como fórmulas de campos textuales devueltos por la API.
            writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v for v in fields])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-url", default=os.environ.get("NGINX_ONE_TENANT_URL") or TENANT_URL)
    parser.add_argument("--instance-id", help="Seleccionar un ID del inventario sin menú interactivo.")
    parser.add_argument("--list-only", action="store_true", help="Solo listar instancias; no consultar métricas.")
    parser.add_argument("--csv", type=Path, help="Guardar también un CSV local nuevo, sin sobrescribir.")
    parser.add_argument("--ca-bundle", default=os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE"))
    args = parser.parse_args(argv)
    try:
        token = API_TOKEN if API_TOKEN and API_TOKEN != "aca va el token de apitoken" else os.environ.get("NGINX_ONE_API_TOKEN")
        client = ReadOnlyClient(args.tenant_url, token, args.ca_bundle)
        instances = sorted(client.instances(), key=lambda item: (str(item.get("hostname", "")), item["object_id"]))
        if not instances:
            print(f"NGINX One no devolvió instancias en {NAMESPACE}.")
            return 0
        list_instances(instances)
        if args.list_only:
            return 0
        selected = select_instance(instances, args.instance_id)
        if selected is None:
            return 0
        rows, warnings = client.disk_usage(selected["object_id"])
        display_disk(selected, rows, warnings)
        if args.csv:
            save_csv(args.csv, selected, rows)
            print(f"CSV guardado: {args.csv}")
        return 2 if warnings else 0
    except ReportError as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Error de configuración TLS/URL o escritura local. Si el CSV existe, utiliza otro nombre.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nConsulta cancelada.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

