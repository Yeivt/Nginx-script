# Reportes de recomendaciones de NGINX One

## V5: reporte JSON simplificado

`nginx_one_report_v5.py` conserva la selección ambiente → servidor → archivo, los cuatro namespaces y las consultas GET de v4. Genera el formato solicitado con estos campos, en este orden:

```text
generado_utc
estado
ambiente
server
archivo { tipo, nombre, ruta }
total_recomendaciones_archivo
resumen_archivo { best_practice, optimization, security, other }
recomendaciones { best_practice, optimization, security, other }
```

Cada recomendación mantiene `regla`, `categoria_original`, `severidad`, `severidad_es`, `archivo`, `linea`, `directiva`, `comentario_original`, `comentario_es` y `contexto_original`. Se quitan del **reporte final** `schema_version`, `instance_id`, versión NGINX, última fecha del agente, versión de catálogo, avisos y documentación. Las listas de servidores y archivos mantienen sus metadatos e IDs para que el framework pueda seleccionar y desambiguar recursos.

```bat
python C:\nginx\nginx_one_report_v5.py --ambiente desarrollo --server LNK2843 --vhost VS_abm-3.conf --output C:\nginx\reportes\vhost.json
```

Los bloques editables, argumentos, menús y funciones de integración funcionan como en v4, importando ahora `nginx_one_report_v5`. La salida es JSON válido, sin comas finales. Ver [ejemplo de salida v5 con datos ficticios](examples/reporte_ejemplo_v5.json).

`estado` usa **`reporte completado`** o **`reporte pendiente`**. En pendientes, el total y los contadores son `null` y la CLI devuelve `2`. Si falla la selección o consulta, la CLI devuelve `{"estado": "error", "error": "…"}` y código `1`; no presenta un reporte completado vacío. Los conteos siguen correspondiendo únicamente a las ubicaciones del archivo seleccionado. Los hallazgos sin archivo informado no se atribuyen a ese archivo. Las traducciones locales siguen limitadas al diccionario existente; los textos desconocidos conservan el idioma original.

V4 sigue disponible con su salida detallada. La validación de v5 usa respuestas simuladas; no se consultó el tenant real con esta versión.

### Errores de salida o certificados en v5

La v5 actualizada distingue el motivo del fallo: archivo de salida existente, `--output` apuntando a una carpeta, un componente de la ruta que es archivo, permisos de escritura, CA inexistente/ilegible o PEM inválido y URL mal formada. Ya no agrupa estos casos en el mensaje genérico «Error de configuración TLS/URL o escritura local». Valida un archivo de salida existente antes de consultar la API y conserva los informes anteriores.

Si el JSON de salida ya existe, elegir otro nombre, por ejemplo `--output C:\nginx\reportes\reporte_nuevo.json`, o ejecutar sin `--output` para ver la respuesta en consola. Si el error menciona CA, revisar la ruta de `--ca-bundle`, `REQUESTS_CA_BUNDLE` o `SSL_CERT_FILE` y usar un certificado PEM válido; TLS sigue verificándose.

## V4: ambiente → servidor → archivo, con salida JSON

`nginx_one_report_v4.py` es la versión nueva solicitada, basada en la consulta y validación de v2. Es autónomo (Python 3.9+, sin dependencias externas). Mantiene v2 disponible y usa exclusivamente GET. Permite `default`, `desarrollo`, `homologacion` y `produccion`, consultando **un único ambiente por cliente**; no mezcla inventarios. Las opciones locales de ambiente no garantizan acceso: el token debe estar autorizado en el namespace elegido.

Su bloque editable incluye:

```python
TENANT_URL = "https://redlink.console.ves.volterra.io"
ambiente = "produccion"
server = ""       # "LNKXXX"
vhost = ""        # "VS_XXXX.conf"
lua = ""          # "lua_XXX.conf"
upstream = ""     # "upstream_XXX.conf"
API_TOKEN = "aca va el token de apitoken"
```

Seleccionar **uno** de `vhost`, `lua` o `upstream` para cada reporte, no los tres simultáneamente. Con selectores vacíos se muestran menús. En el menú de ambiente, Enter usa el valor editable de `ambiente`. Los argumentos de archivo tienen prioridad sobre los selectores editados. El token real se recibe de `API_TOKEN` o, si se conserva el ejemplo/vacío, de `NGINX_ONE_API_TOKEN`.

### Uso desde el framework mediante CLI

Cada comando devuelve un documento JSON en stdout. Los menús se escriben en stderr; para ejecución automática, proporcionar todos los selectores necesarios. Listar los cuatro ambientes no requiere token ni red.

```bat
python C:\nginx\nginx_one_report_v4.py --listar-ambientes
python C:\nginx\nginx_one_report_v4.py --ambiente produccion --listar-servidores
python C:\nginx\nginx_one_report_v4.py --ambiente produccion --server LNK2843 --listar-archivos
python C:\nginx\nginx_one_report_v4.py --ambiente produccion --server LNK2843 --vhost VS_fraudprevention.conf
python C:\nginx\nginx_one_report_v4.py --ambiente produccion --server LNK2843 --lua lua_fraudprevention.conf
python C:\nginx\nginx_one_report_v4.py --ambiente produccion --server LNK2843 --upstream upstream_fraudprevention.conf --output C:\nginx\reportes\upstream.json
```

`--output` guarda también el mismo JSON en un archivo nuevo; no sobrescribe uno existente. `--instance-id inst_...` permite identificar exactamente la instancia si un hostname está repetido o una VM tiene varios NGINX. Usar la ruta completa recibida en el catálogo si un nombre de archivo se repite. `--tenant-url` y `--ca-bundle` mantienen TLS y configuración equivalentes a los demás scripts.

### Uso como módulo Python

```python
from nginx_one_report_v4 import ReadOnlyClient, listar_ambientes

opciones_ambiente = listar_ambientes()
client = ReadOnlyClient(tenant_url, token, ambiente="produccion")
opciones_server = client.listar_servidores()
catalogo = client.listar_archivos("LNK2843", instance_id="inst_ID_DE_LA_LISTA")
reporte = client.obtener_reporte(
    "LNK2843", instance_id="inst_ID_DE_LA_LISTA",
    vhost="/etc/nginx/conf.d/VS_fraudprevention.conf",
)
```

Las funciones devuelven listas/diccionarios serializables a JSON; `ReportError` informa un fallo de API o selección. Crear un cliente por flujo/ambiente y renovarlo cuando se necesite refrescar inventario o catálogo: las listas se cachean en memoria durante su uso. El constructor no consulta red hasta listar/seleccionar recursos y no hace trabajo al importar el módulo.

### Catálogo y reporte seleccionado

GET `/instances` obtiene servidores; GET `/instances/{id}/config` obtiene el catálogo; GET `/instances/{id}/config-report` obtiene las recomendaciones. Todas las rutas se encuentran bajo `/api/nginx/one/namespaces/{ambiente}`. Un ID debe existir en el inventario del ambiente antes de consultar su configuración. El endpoint de configuración incluye contenidos; v4 **solo extrae nombres y rutas** de `configs`/`aux`: no decodifica, guarda ni exporta `contents` o `payloads`. No genera un volcado de configuración ni conecta por SSH.

Clasificación del catálogo por tus convenciones: `VS_*.conf` o `vhost*.conf` son vhosts; `lua_*.conf`, `.conf` dentro de un directorio `lua` o archivos `.lua` son Lua; `upstream_*.conf` o `.conf` dentro de `upstream`/`upstreams` son upstreams. El framework recibe `{tipo, nombre, ruta}` agrupados en esas tres categorías. Archivos con otras convenciones no se clasifican automáticamente; se puede adaptar `file_type` en una tarea aparte. Los archivos sin hallazgos también se pueden seleccionar porque la lista procede de la configuración y no de las recomendaciones.

El JSON del reporte incluye `schema_version: "4.0"`, ambiente, server, instance_id, archivo, fecha, estado, `resumen_archivo`, `total_recomendaciones_archivo` y recomendaciones por categoría. Cada recomendación contiene línea, directiva, regla, severidad, comentario original, traducción local si existe y documentación. Los conteos son ubicaciones únicas por regla **solo del archivo seleccionado**, no los totales generales de la consola o VM. No exporta las ubicaciones de otros archivos ni navega automáticamente a los includes relacionados.

La comparación de rutas Linux distingue mayúsculas y minúsculas. Una ubicación que solo informa un nombre se asigna únicamente si el catálogo tiene una coincidencia única. Si ese nombre está repetido, el reporte falla en lugar de adivinar. Ubicaciones sin archivo no se atribuyen al archivo seleccionado. El catálogo y el análisis son consultas separadas: la API no garantiza que correspondan a un snapshot atómico; el JSON incluye la versión de catálogo y esa advertencia.

HTTP 204 produce `estado: "pendiente"` y contadores `null`, no cero. Un archivo válido sin hallazgos produce `estado: "completado"` y cero, con la aclaración de que no certifica ausencia de mejoras. Los errores CLI devuelven JSON con `estado: "error"`; no se reemplazan por un reporte vacío. Códigos de salida: `0` completado/listado/cancelación de menú, `1` error, `2` análisis pendiente, `130` interrupción; errores de argumentos también usan el código `2` estándar de argparse.

Ver [JSON de ejemplo con datos ficticios](examples/reporte_ejemplo_v4.json). Se validó con respuestas simuladas, sin acceso al tenant real. Las versiones previas permanecen disponibles.

## Salud HTTP y upstreams desde NGINX One

`nginx_one_health.py` es autónomo, Python 3.9+ sin paquetes adicionales. Conservar `NAMESPACE = "desarrollo"` y completar el bloque editable `TENANT_URL` y `API_TOKEN`, o reutilizar las variables de entorno de los otros scripts. Solo consulta NGINX One: GET de inventario y POST al endpoint documentado de consulta de métricas. No usa SSH ni modifica la instancia, sus vhosts, Lua, logs, agente o config sync groups.

En Windows:

```bat
python C:\nginx\nginx_one_health.py --output C:\nginx\reportes_salud
python C:\nginx\nginx_one_health.py --instance-id inst_ID_DE_LA_LISTA --output C:\nginx\reportes_salud
python C:\nginx\nginx_one_health.py --all --output C:\nginx\reportes_salud --save-json
python C:\nginx\nginx_one_health.py --list-only
```

Sin selector, muestra el inventario y permite elegir una instancia (`0` para salir). Cada ejecución crea una carpeta nueva y un HTML por **instancia**, con hostname e ID en el nombre. Si una VM tiene varias instancias, conserva informes separados. `--all` recorre el namespace; son 15 consultas de métricas por instancia, realizadas secuencialmente. JSON es opcional y no exporta claves de registro del agente. El bloque `SERVICE_MAP` permite registrar asociaciones explícitas entre servicios, zonas, upstreams, archivos e identificación de logs; incluye el ejemplo `fraudprevention` aportado por el usuario. Solo muestra una asociación si existe telemetría de alguna de sus zonas/upstreams.

### Qué detecta

- Solicitudes HTTP, respuestas **4xx y 5xx**, a nivel de instancia y, cuando existan, por `server_zone` y `location_zone`. Usa `rate` de contadores, no suma contadores acumulados como si fueran errores nuevos. Presenta solicitudes/segundo y errores/segundo del último intervalo disponible. Las dos dimensiones se muestran por separado: no deben sumarse.
- Porcentaje 4xx/5xx calculado solo con tasas de solicitudes y respuestas de la misma dimensión y fecha. Una serie ausente no se convierte en cero. Con tráfico cero, puntos sin fecha común o numeradores incompatibles con las solicitudes, el porcentaje se muestra como no calculable. La fecha del porcentaje puede ser anterior a las tasas más recientes y se muestra explícitamente.
- Upstreams HTTP con peers reportados `UP`, `DOWN`, `UNHEALTHY`, `UNAVAILABLE`, `DRAINING` o `CHECKING`. Se consulta `max` por estado para el intervalo; un peer puede cambiar de estado dentro del mismo minuto, de modo que **no se suman esos estados para obtener un total**. DOWN no establece la causa ni demuestra que la VM del backend esté apagada: podría representar un estado administrado o un problema observado por NGINX.
- Advertencias sobre ausencia de telemetría, consultas fallidas, TopX incompleto y datos con más de 180 segundos de antigüedad. No clasifica como sano un servicio sin métricas.

Ventana predeterminada: últimos 15 minutos, resolución de 1 minuto. Las tablas muestran el último intervalo con valor por serie, no una comprobación activa del servicio ni una lectura instantánea. Se conservan los puntos de la ventana en el JSON opcional. Las métricas documentadas son `nginx.http.request.count`, `nginx.http.response.count` y `nginx.http.upstream.peer.count`, consultadas mediante [queryMetricsInputTopX](https://docs.nginx.com/nginx-one-console/api/api-reference-guide#operation/queryMetricsInputTopX).

### Tus estructuras de servicio

Para `VS_fraudprevention.conf`, se asocia `server_zone = fraudprevention`, `location_zone = /location/root/fraudprevention` y `upstream_name = fraudprevention`. Las rutas de los includes Lua/upstream y los logs de `/var/log/nginx` figuran como referencias de `SERVICE_MAP`: no se verifican ni leen en la VM, no se extraen de una configuración descargada y no se ejecuta Lua.

Un vhost estático como `vs_bee3-app`, con `try_files` y sin upstream, puede no tener métricas de backend por diseño. NGINX Open Source puede carecer de telemetría por servicio y peers disponible en Plus. Si el agente solo entrega métricas globales, el script conserva la vista de instancia sin atribuir esos datos a un vhost. La existencia del archivo o de un access log por servicio no garantiza una dimensión de métricas. RHEL 8.10 o 9.x no cambia la consulta: depende de la versión/edición de NGINX y la telemetría que ya reciba NGINX One.

**Limitaciones de esta API pública:** `status_range` documenta familias 4xx/5xx, no códigos exactos 400, 404, 500, 502 o 504. El catálogo de `nginx.http.upstream.peer.count` distingue upstream y estado, pero no dirección IP del peer afectado: el informe identifica el upstream con peers problemáticos, no afirma cuál backend concreto está caído. Tampoco hay un endpoint documentado de lectura de access/error logs en la referencia consultada; los paths `/logs/nginx/...` y `/var/log/nginx/...` son referencias para una investigación posterior. No se inventan errores de logs ni se accede a certificados, claves o passwords.

Salidas: `0` consultas completas o listado/cancelación; `1` fallo de inventario/configuración/escritura; `2` informes generados con telemetría ausente, parcial, desactualizada o consultas de métricas fallidas; `130` interrupción. La ausencia de una métrica opcional puede producir `2` aun cuando las otras tablas sean útiles. Las pruebas son simuladas; no se ha consultado el tenant real con este nuevo script. Ver [ejemplo con datos ficticios](examples/reporte_ejemplo_salud.html).

## Uso de disco desde NGINX One

`nginx_one_disk.py` es un script autónomo Python 3.9+, sin dependencias externas. Editar su bloque `TENANT_URL`, `NAMESPACE = "desarrollo"` y `API_TOKEN`, igual que en los scripts de recomendaciones. Mantener el token real en tu copia local o usar `NGINX_ONE_API_TOKEN`; no subirlo a Git.

En Windows:

```bat
python C:\nginx\nginx_one_disk.py
```

Lista todas las instancias del namespace configurado, muestra hostname, ID y estado, y permite seleccionar un número. `0` sale sin consultar métricas. La tabla resultante muestra uso porcentual por punto de montaje, fecha UTC y estado de disponibilidad de cada medición. No usa SSH, comandos remotos ni acceso directo a las VMs.

Opciones:

```bat
python C:\nginx\nginx_one_disk.py --list-only
python C:\nginx\nginx_one_disk.py --instance-id inst_ID_DE_LA_LISTA
python C:\nginx\nginx_one_disk.py --csv C:\nginx\reportes\disco.csv
```

`--csv` guarda también una tabla local para Excel, sin sobrescribir archivos existentes. Las opciones `--tenant-url` y `--ca-bundle` conservan el mismo funcionamiento que en los otros scripts. Un ID proporcionado por argumento debe encontrarse primero en el inventario del namespace: no se consulta un ID arbitrario.

### Consultas y significado del porcentaje

- **GET** `/api/nginx/one/namespaces/desarrollo/instances`: inventario paginado.
- **POST** `/api/nginx/one/namespaces/desarrollo/monitor/metrics_query_topx`: consulta documentada de métricas, filtrada por el ID seleccionado y `state = in_use`, agrupada por `mount_point`.

La API oficial usa POST para consultar métricas: este endpoint ejecuta una consulta y no despliega configuraciones ni modifica las VMs. El cliente bloquea otros métodos y rutas; no incluye PATCH, PUT o DELETE. Los scripts de recomendaciones siguen utilizando únicamente GET.

Se solicita `system.filesystem.usage`, que la documentación describe como porcentaje y reemplaza las métricas antiguas `system.disk.*`. Se usa el valor directamente en escala 0–100; no se multiplica automáticamente por 100 ni se infiere la escala según su magnitud. Referencia: [queryMetricsInputTopX](https://docs.nginx.com/nginx-one-console/api/api-reference-guide#operation/queryMetricsInputTopX) y [OpenAPI oficial](https://github.com/nginx/documentation/blob/main/static/nginx-one-console/api/one.json).

Se consultan los últimos 5 minutos con intervalos de 30 segundos y agregación `avg`. Para cada montaje se presenta el **último intervalo con valor disponible**, que es un promedio de ese intervalo, no una medición instantánea de `df`. El reporte conserva la hora y marca como desactualizado un dato con más de 120 segundos. Un hueco `null` no se convierte en cero. Si falta telemetría, muestra «SIN DATOS» o un aviso: no significa que el disco esté vacío. Se necesita que NGINX One ya reciba esas métricas del agente y que la credencial tenga permiso para consultarlas; el script no modifica el agente para activarlas.

Se descarta el total sintético `all`. Si TopX devuelve `other`, se informa que el listado está incompleto; no se presenta como un montaje. Las rutas que no aparecen en la telemetría no se inventan. Se mide la ocupación de los sistemas de archivos montados, no cuánto consume cada directorio. Varias instancias pueden representar la misma VM: seleccionar una de ellas por su ID.

Salidas: `0` consulta completa, listado o cancelación con `0`; `1` error de API/configuración/archivo; `2` datos ausentes, desactualizados o listado incompleto; `130` interrupción por teclado. Las pruebas usan respuestas simuladas del esquema oficial. La conexión al tenant real no se ha probado en este entorno.

## Nueva versión: resumen y detalle por línea

`nginx_one_report_v2.py` es un script autónomo basado en el anterior. Conserva su cliente de lectura, configuración editable, paginación, TLS, agrupación por VM, formatos HTML/TXT y JSON opcional. El script anterior sigue disponible.

En Windows, descargar `nginx_one_report_v2.py`, editar su bloque `TENANT_URL`, `NAMESPACE` y `API_TOKEN`, y ejecutar:

```bat
python C:\nginx\nginx_one_report_v2.py --output C:\nginx\reportes
```

La versión nueva agrega:

- Resumen por instancia de mejores prácticas, optimización, seguridad y otras categorías. Usa los contadores `recommendations[].count` del inventario de NGINX One cuando están disponibles; no presupone que el número de reglas sea el contador de la consola. Si falta un contador, muestra el número de ubicaciones calculadas desde `config-report`, indicando expresamente esa fuente. Los dos endpoints pueden actualizarse en momentos diferentes.
- Cantidad de recomendaciones por archivo y categoría: una por ubicación señalada, sin duplicar ubicaciones idénticas de una misma regla. Conserva todas las líneas de una regla que afecta a varias ubicaciones.
- Resumen legible de severidad y línea, seguido del comentario de esa ubicación, la directiva que debe revisarse, el identificador de la regla y su documentación. Si existe un comentario general diferente, también se conserva.
- Mensajes de categorías vacías del tipo «NGINX One no reporta cambios de seguridad para este archivo». Son ausencia de hallazgos devueltos para ese archivo, no una certificación de que un servicio completo no requiere cambios.
- HTML con títulos, archivos resaltados y recomendaciones destacadas; TXT con el mismo contenido.

La traducción es local y limitada al diccionario `MESSAGE_TRANSLATIONS` del script: incluye el mensaje `default_server parameter is not set for the listen directive`. Siempre muestra el original cuando se traduce. Los demás textos conservan el idioma de la API; se pueden agregar traducciones exactas al diccionario. No se envía información a servicios externos de traducción.

Para el ejemplo conocido de `default_server_not_set`, aparece una orientación del script para revisar el servidor predeterminado, explícitamente separada del comentario de NGINX One. No se inventa una solución específica para otras reglas: se muestra el comentario y la documentación que devuelve la plataforma. Ninguna sugerencia se aplica automáticamente.

Ver [reporte de ejemplo con datos ficticios](examples/reporte_ejemplo_v2.html). Las pruebas de esta versión cubren el contador de ejemplo `118`, la diferencia entre reglas y ubicaciones, categorías sin datos, traducción y texto original, HTML escapado y una ejecución completa simulada que genera un archivo por VM. La nueva salida no se ha consultado contra el tenant real en este entorno.

## Versión original

Script Python 3.9+ sin dependencias externas. Consulta **exclusivamente el namespace configurado en `NAMESPACE` (por defecto `desarrollo`)**, obtiene las recomendaciones existentes de NGINX One y genera un HTML por VM, agrupado por archivo y categoría. También permite TXT.

Solo realiza estas consultas GET:

```text
/api/nginx/one/namespaces/desarrollo/instances?paginated=true&limit=100&offset=1
/api/nginx/one/namespaces/desarrollo/instances/{object_id}/config-report
```

El cliente tiene una lista cerrada de rutas, no sigue redirecciones y verifica TLS. No contiene operaciones PATCH, PUT, POST o DELETE ni se conecta directamente a las VMs. Las únicas escrituras son archivos de reporte locales. Como protección adicional, usar una credencial con permisos de lectura de `desarrollo`.

## Ejecutar

Editar el bloque al inicio de `nginx_one_report.py` en tu copia local:

```python
TENANT_URL = "https://redlink.console.ves.volterra.io"
NAMESPACE = "desarrollo"

# >>>>> PEGA TU API TOKEN AQUÍ DENTRO DE LAS COMILLAS <<<<<
API_TOKEN = "aca va el token de apitoken"
```

Reemplazar el texto de ejemplo por el token y ejecutar `python3 nginx_one_report.py`. Se consulta un solo namespace, el indicado en `NAMESPACE`. Mantenerlo en `desarrollo` para el alcance solicitado. No subir el archivo con un token real a Git.

Alternativamente, dejar `API_TOKEN` con el texto de ejemplo y usar variables de entorno. En Linux/macOS, desde el directorio del repositorio:

```bash
export NGINX_ONE_TENANT_URL='https://redlink.console.ves.volterra.io'
read -r -s -p 'Token de NGINX One: ' NGINX_ONE_API_TOKEN
echo
export NGINX_ONE_API_TOKEN
python3 nginx_one_report.py
unset NGINX_ONE_API_TOKEN
```

Se usa `Authorization: APIToken …`, como en el script original, para el tenant F5 indicado. Un token editado en `API_TOKEN` tiene prioridad; si está vacío o conserva el texto de ejemplo, se usa `NGINX_ONE_API_TOKEN`. Para el tenant, la prioridad es `--tenant-url`, `NGINX_ONE_TENANT_URL` y finalmente `TENANT_URL`. Reutilizar un mecanismo seguro de inyección de credenciales si ya existe.

Opciones:

```bash
python3 nginx_one_report.py --format txt --output ./reportes
python3 nginx_one_report.py --save-json
python3 nginx_one_report.py --ca-bundle /ruta/ca-corporativa.pem
python3 -m unittest discover -s tests -v
```

Si se requiere una CA corporativa, proporcionar su PEM válido por `--ca-bundle`, `REQUESTS_CA_BUNDLE` o `SSL_CERT_FILE`. No desactivar verificación TLS. La máquina debe poder acceder por HTTPS al hostname del tenant. No hacen falta paquetes adicionales ni un servicio local en ejecución.

## Qué contiene cada archivo

```text
reportes/<fecha UTC de ejecución>/LNK8265_<identificador de host>.html
  VM: LNK8265
  Instancia: inst_...
  ARCHIVO: /etc/nginx/conf.d/vS_pruebasocp.conf
    Mejores prácticas
      Regla, severidad, comentario de la plataforma, línea y directiva
    Optimización
      Comentarios de la plataforma y ubicaciones
    Seguridad
      Comentarios de la plataforma y ubicaciones
  ARCHIVO: /etc/nginx/lua/archivo.lua
    ... si NGINX One devuelve hallazgos para ese archivo
```

El HTML es autónomo, sin recursos externos, y se puede abrir sin ingresar a NGINX One o imprimir como PDF desde un navegador. Los nombres incorporan un identificador para evitar mezclar VMs con nombres iguales. Las instancias con el mismo `system_id` se consolidan en un archivo; si la API omite ese campo, se agrupan por hostname. Revisar esa agrupación si existen hosts diferentes con el mismo nombre y sin `system_id`.

Se conservan `rule`, `info`, `severity`, `category`, `documentation` y todas las ubicaciones de `where` (`file`, `line`, `directive`, `message`). Una regla puede aparecer en varios archivos. Las categorías desconocidas se muestran en «Otras categorías», con su valor original. Una recomendación sin ubicación se muestra en «Sin archivo informado».

El reporte contiene lo que devuelve la plataforma: no crea recomendaciones propias, no analiza código Lua por su cuenta ni asigna una recomendación de un upstream compartido a un vhost sin información de la API. Tampoco enumera todos los archivos de configuración: muestra aquellos asociados a hallazgos. El nombre de servicio y la asociación entre archivos no forman parte de este endpoint. Los CVEs son una API separada y no se incluyen; «Seguridad» reúne las recomendaciones de configuración.

HTTP 204 se marca como análisis pendiente. Un error HTTP o una respuesta incompatible se marca como no disponible; nunca como ausencia de problemas. Una lista vacía solo significa que la consulta no devolvió recomendaciones. Se conservan los informes de las otras instancias si una consulta falla. Un error en el inventario impide generar un lote parcial sin advertencia.

Cada ejecución crea una carpeta nueva. Salidas: `0` consultas completas (también inventario vacío), `1` error que impide completar el inventario o escribir el reporte, `2` informes generados con consultas fallidas o análisis pendientes. Un lote con código `1` durante la escritura puede contener archivos parciales: revisar o descartar ese lote.

`--save-json` añade un JSON por VM para diagnóstico, con los hallazgos originales y metadatos seleccionados; no se exportan claves de registro del agente. Revisar el contenido antes de distribuir: comentarios/directivas pueden contener nombres internos o detalles de configuración. Por defecto se genera únicamente un HTML por VM.

## Referencia y validación

La API oficial documenta `getInstanceConfigReport` en **`/instances/{instanceObjectID}/config-report`**, no `/config/report`, y su respuesta es una **lista**, no un objeto con `findings` o `recommendations`. Esto corrige las dos diferencias principales del script original.

- [Referencia oficial de getInstanceConfigReport](https://docs.nginx.com/nginx-one-console/api/api-reference-guide#operation/getInstanceConfigReport)
- [OpenAPI publicado por NGINX, consultado para esta implementación](https://github.com/nginx/documentation/blob/a8f0dd7a5ab5fd604142cda3419ebd7560d3c6ee/static/nginx-one-console/api/one.json)

Las pruebas usan respuestas simuladas del esquema oficial: no consultan el tenant ni requieren token. La conexión real y la correspondencia con los comentarios visibles en tu consola deben verificarse ejecutando el script con acceso de lectura; no se han validado contra tu tenant.
