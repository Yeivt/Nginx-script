# Reportes de recomendaciones de NGINX One

Script Python 3.9+ sin dependencias externas. Consulta **exclusivamente el namespace `desarrollo`**, obtiene las recomendaciones existentes de NGINX One y genera un HTML por VM, agrupado por archivo y categoría. También permite TXT.

Solo realiza estas consultas GET:

```text
/api/nginx/one/namespaces/desarrollo/instances?paginated=true&limit=100&offset=1
/api/nginx/one/namespaces/desarrollo/instances/{object_id}/config-report
```

El cliente tiene una lista cerrada de rutas, no sigue redirecciones y verifica TLS. No contiene operaciones PATCH, PUT, POST o DELETE ni se conecta directamente a las VMs. Las únicas escrituras son archivos de reporte locales. Como protección adicional, usar una credencial con permisos de lectura de `desarrollo`.

## Ejecutar

En Linux/macOS, desde el directorio del repositorio:

```bash
export NGINX_ONE_TENANT_URL='https://redlink.console.ves.volterra.io'
read -r -s -p 'Token de NGINX One: ' NGINX_ONE_API_TOKEN
echo
export NGINX_ONE_API_TOKEN
python3 nginx_one_report.py
unset NGINX_ONE_API_TOKEN
```

Se usa `Authorization: APIToken …`, como en el script original, para el tenant F5 indicado. El token se recibe exclusivamente desde `NGINX_ONE_API_TOKEN`: no pegarlo en el código, en argumentos de comandos ni en Git. Reutilizar un mecanismo seguro de inyección de credenciales si ya existe.

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
