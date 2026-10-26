# Upgrade de NGINX Open y NGINX Plus

Se entregan dos archivos autonomos, sin un tercer modulo obligatorio:

- `actualizar_nginx_plus.py`: exclusivamente parches de una instalacion **R36**.
- `actualizar_nginx_open.py`: **Red Hat/Satellite dentro del stream actual** o
  **nginx.org hacia stable**, segun fabricante del RPM instalado.

Ambos usan las variables `version` (`NginxPlus`/`NginxOpen`) y `server` (`LNKXXX`).
No contienen IP, contrasena, token ni proxy corporativo hardcodeado. Se conserva
el proxy definido en el archivo de repositorios existente.

## Primer uso en DESA

Ejecutar EN la VM destino como root. El framework o Ansible hacen el transporte.
`server` es una comprobacion de identidad local, no abre SSH ni busca servidores.

```bash
# LNK5141: consultar Plus R36 sin actualizar.
/usr/libexec/platform-python actualizar_nginx_plus.py --version NginxPlus --server LNK5141

# LNK9375: consultar Open nginx.org, canal stable.
python3 actualizar_nginx_open.py --version NginxOpen --server LNK9375

# LNK4989: consultar Open de Red Hat dentro de su stream actual.
/usr/libexec/platform-python actualizar_nginx_open.py --version NginxOpen --server LNK4989
```

Sin `--apply` solamente se consulta/resuelve. Se crean informes privados y cache
de metadatos en el servidor; no se copian repositorios al directorio activo,
no se instalan RPM ni se reinicia NGINX.

La salida es JSON. El destino exacto se encuentra en `plan.target.evr`, con su
epoch RPM incluido. Para produccion se recomienda copiar ese valor homologado:

```bash
# Sustituir EVR_DEL_PLAN por el valor real. No ejecutar el marcador literal.
/usr/libexec/platform-python actualizar_nginx_plus.py \
  --version NginxPlus --server LNK5141 --target 'EVR_DEL_PLAN' --apply

python3 actualizar_nginx_open.py \
  --version NginxOpen --server LNK9375 --target 'EVR_DEL_PLAN' --apply
```

`--apply` sin `--target` elige el RPM mas reciente permitido disponible en esa
ejecucion: R36 para Plus, mismo stream para Red Hat y stable para nginx.org.
No se implementan saltos intermedios por todos los parches: los parches R36 son
acumulativos; se elige un destino R36, sin migrar a R37. El numero de revision
del RPM no se interpreta como numero P: se registra tambien `nginx -v`.

No hay un numero de parche hardcodeado ni se extraen versiones de paginas HTML.
La referencia instalable es la metadata del repositorio autorizado/Content View.

## Requisitos

- RHEL 8 o 9 con systemd, root, Python 3.6 o superior.
- Los bindings `dnf`, `rpm` y `hawkey` del Python del sistema RHEL. Se detectan
  automaticamente usando el interprete actual, `/usr/libexec/platform-python`
  o `/usr/bin/python3`. No hace falta pip ni instalar paquetes Python externos.
- Comandos `rpm`, `rpmkeys`, `dnf`, `cp`, `systemctl`, y el binario nginx del RPM.
- NGINX y nginx-agent instalados y activos. El binario en memoria debe coincidir
  con el instalado; un upgrade anterior sin activar debe resolverse antes.
- Claves GPG del proveedor ya confiadas en RPM. No se importan automaticamente.
- Mantenimiento sin otra ejecucion DNF/RPM ni cambios concurrentes del framework,
  Ansible o gestion remota del nginx-agent. Hay bloqueo entre estos dos scripts,
  pero ningun script puede impedir que otro administrador edite archivos.

Un modulo externo no gestionado por los RPM de NGINX detiene la operacion antes
del upgrade: su compatibilidad debe homologarse por separado.

## Respaldo y restauracion exacta de nginx.conf

Al aplicar, ANTES de cualquier cambio en repositorios o paquetes:

1. Validar hostname, SO, fabricante, servicio, modulos y configuracion actual.
2. Crear un directorio nuevo privado y comprobar espacio libre.
3. Respaldar **todo `/etc/nginx`** y **todo `/etc/yum.repos.d`**, incluidos `bkp`,
   conservando permisos, propietario, atributos y contexto mediante `cp -a`.
4. Guardar ademas una copia independiente del contenido de `nginx.conf`; si es
   un enlace, conservar tambien su destino y su topologia original.
5. Verificar las copias con SHA-256. Un respaldo incompleto impide actualizar.
6. Consultar y descargar todos los RPM previstos. Comprobar firmas y hashes.
7. Verificar nuevamente que nadie cambio configuracion/inventario durante la
   preparacion. Si cambio, abortar sin sobrescribir esos cambios ajenos.
8. Restaurar el archivo de repositorios necesario desde `bkp` SOLO si falta y
   preparar el canal permitido. No recuperar nginx-agent.repo automaticamente.
9. Instalar el conjunto acotado mediante DNF CLI y sus plugins, con metadata/RPM
   en cache, versiones exactas y sin exponer al solver otros paquetes candidatos.
10. Comparar el `nginx.conf` posterior con el original respaldado.
11. Si difiere o falta: conservar el recibido, guardar diff privado y **restaurar
    el original automaticamente**, verificando que el SHA-256 vuelve a coincidir.
12. Ejecutar `nginx -t -c /etc/nginx/nginx.conf`. Si falla, forzar otra restauracion
    del original y repetir la prueba. Si sigue fallando, no ordenar el reinicio.
13. Si todo es valido, activar con `systemctl restart nginx` y comprobar servicio,
    nginx-agent, RPM final, configuracion y binario real del proceso en `/proc`.

Si una instalacion falla, tambien se intenta restaurar `nginx.conf` y el resultado
queda como error. **Restaurar nginx.conf NO revierte los RPM.** Una transaccion
parcial o una configuracion antigua incompatible con el binario nuevo requieren
revision del operador; el script no oculta ese fallo con un resultado exitoso.

Los otros archivos de `/etc/nginx` que cambien se informan, pero no se sustituyen
automaticamente: la regla solicitada de restauracion exacta corresponde a
`nginx.conf`. La copia completa queda disponible para una recuperacion revisada.
Includes, certificados o claves externos a `/etc/nginx` no forman parte de esa
copia completa, salvo el contenido de nginx.conf si su enlace apunta afuera.

## Repositorios y alcance de cada producto

**Plus**: buscar `[nginx-plus]` en los `.repo` principales; si falta, buscar un
unico `.repo` que lo contenga en `bkp`. Conservar SSL client cert/key, CA y proxy.
La URL debe ser `https://pkgs.nginx.com/plus/R36/centos/8/$basearch/` en RHEL 8 o
su equivalente `/9/` en RHEL 9. Se ignoran archivos con extension `.repo_bkp` y
lineas comentadas. `plus9.repo` puede ser correcto en RHEL 8 si su URL usa `/8/`.
Se exige `gpgcheck=1` y `sslverify=1`; no se desactiva ninguna verificacion.

**Open nginx.org directo**: buscar `[nginx-stable]`, incluyendo la recuperacion
desde `bkp/nginx.repo`. Habilitar stable y deshabilitar mainline dentro de ese
archivo. Sustituir `$releasever` por la version MAYOR para esa URL de nginx.org
(8 o 9), sin cambiar la version de RHEL ni su configuracion de suscripciones.

**Open nginx.org distribuido por Satellite**: seleccionar el ID del repositorio
custom estable con `--repo ID_DEL_MIRROR_STABLE`. Se conservan su URL y mecanismo
de entrega; no se cambia a Internet por el numero de version instalado.

```bash
python3 actualizar_nginx_open.py --version NginxOpen --server LNKXXX \
  --repo ID_DEL_MIRROR_STABLE
```

**Open Red Hat/Satellite**: conservar repositorios, fabricante, epoch y stream
actual. No crear nginx.repo, no activar nginx.org, no ejecutar `module reset`,
`module enable`, `module disable` ni `switch-to`. Solo usar los RPM visibles en
la Content View ya publicada/promovida. Si no hay RPM superior ni modulos por
actualizar, devolver `sin cambios` sin reiniciar.

No se actualizan PHP, SO, nginx-agent, licencias ni certificados. Si el resolvedor
requiere cambiar esos componentes, se detiene para tratarlo en el flujo de
parcheo correspondiente. No se utiliza `--allowerasing`, `--skip-broken`,
`--nogpgcheck`, borrado de paquetes ni downgrade.

La consulta auxiliar DNF API no ejecuta plugins: la instalacion final usa DNF CLI
con sus plugins/politicas corporativas. Por eso un plan puede ser rechazado al
aplicar por versionlock u otra politica. Ese rechazo no se fuerza ni se ignora.

## Framework Python

Importar el archivo adecuado y llamar `ejecutar()` EN el destino Linux. La
importacion no instala, no consulta red y no inicia la ejecucion.

```python
import actualizar_nginx_plus

version = 'NginxPlus'
server = 'LNK5141'
plan = actualizar_nginx_plus.ejecutar(version=version, server=server)
if plan['codigo'] != 0:
    raise RuntimeError(plan['error'])

# Despues de homologar ese destino; esta llamada aplica realmente.
resultado = actualizar_nginx_plus.ejecutar(
    version=version,
    server=server,
    aplicar=True,
    target=plan['plan']['target']['evr'],
)
```

La CLI acepta tambien variables de entorno `version` y `server`. Parametros
explicitos tienen prioridad. API: `ejecutar(version, server, aplicar=False,
target=None, repos=None, backup_root=...)` devuelve un diccionario serializable.
Codigo CLI `0`: plan/actualizado/sin cambios; `1`: error; `2`: argumentos invalidos.
`changed` en un error posterior al intento de transaccion es conservador: puede
haber cambios parciales y debe revisarse `inventario_despues_del_error`.

## Ansible desde otra VM

Incluidos `actualizar_nginx.yml` e `inventario.ejemplo.ini`. Ansible copia SOLO el
script necesario y lo ejecuta en cada VM, con `become`, `serial: 1` y detencion
ante errores. No usar `--check` como sustituto del modo plan; el playbook tiene
su propia variable `nginx_apply`, falsa por defecto.

```bash
# Consultar un servidor.
ansible-playbook -i inventario.ejemplo.ini actualizar_nginx.yml --limit LNK5141

# Aplicar el destino aprobado. Sustituir EVR_DEL_PLAN.
ansible-playbook -i inventario.ejemplo.ini actualizar_nginx.yml --limit LNK5141 \
  -e nginx_apply=true -e 'nginx_target=EVR_DEL_PLAN'
```

El inventario usa `version`, `server` y `nginx_python`; las credenciales SSH y
sudo se configuran en el framework/Ansible/Vault, nunca dentro de los scripts.
`nginx_python` es el interprete del script, independiente del interprete que la
version de Ansible necesite en el host para sus propios modulos.

## Corte y resultados

Se descarga antes de instalar y no se ordena detener NGINX durante la descarga.
`restart` activa el binario nuevo: `reload` solo relee configuracion y no basta
para certificar que se este ejecutando el nuevo binario. El tiempo registrado es
la duracion de `systemctl restart`, NO una medicion de indisponibilidad HTTP.
Los scriptlets de los RPM tambien pueden actuar sobre el servicio durante una
transaccion. Las verificaciones y restauracion se ejecutan despues de DNF.

**En una VM individual no se garantiza corte cero ni preservacion de todas las
conexiones.** Esta entrega no implementa cambio de binario en caliente, HA,
drenado de trafico ni failover. La medicion del corte se debe hacer en DESA.

Cada ejecucion queda en:

```text
/var/backups/nginx-upgrade/<servidor>/<fecha-UTC-id>/
  resultado.json
  ejecucion.log
  configuracion_antes.txt
  backup/                         # solamente al aplicar
    nginx/
    yum.repos.d/
    nginx.conf.original
    manifest.json
  nginx.conf.recibido_del_paquete  # si se restauro
  nginx.conf.diff                 # si habia contenido para comparar
  repos/                         # propuestas temporales
  transaction-repos/              # whitelist privada, no persistida al SO
  dnf-cache/                     # descargas y metadata
```

Estos informes y respaldos pueden contener configuracion sensible. Directorio
de ejecucion 0700, archivos JSON/log 0600; conservar esa proteccion al archivarlos.
No hay borrado automatico de backups/cache. Aplicar retencion con el framework.
`--backup-root` permite elegir otro filesystem; el directorio debe ser de root,
modo 0700 y estar fuera de `/etc/nginx`.

La comprobacion de VHosts/SNI, URLs y distribucion 2xx/3xx/4xx/5xx en
`/logs/nginx` sigue siendo la segunda fase. Esta entrega no afirma que las
aplicaciones funcionen solo porque NGINX y nginx-agent estan activos.

## Validacion de esta entrega

25 pruebas automatizadas de politica, identidad, respaldo/restauracion,
enlaces, repositorios y whitelist; sintaxis Python 3.6; CLI y JSON probados.
La ejecucion de DNF/RPM/systemd en RHEL real, Satellite, certificados de Plus,
modulos y plugins corporativos requiere la primera ejecucion en DESA.
No se ejecuto ningun upgrade ni conexion a tus servidores desde este entorno.

```bash
python3 -m unittest discover -s tests -v
```

## Referencias

- Historial y soporte: https://docs.nginx.com/nginx/releases/
- Upgrade Plus: https://docs.nginx.com/nginx/admin-guide/installing-nginx/upgrading-nginx-plus/
- Paquetes Open: https://nginx.org/en/linux_packages.html
- AppStream: https://access.redhat.com/support/policy/updates/rhel-app-streams-life-cycle
- API DNF: https://dnf.readthedocs.io/en/latest/api_base.html
- CLI DNF: https://dnf.readthedocs.io/en/stable/command_ref.html

Alcance acordado: mantener R36. Segun la referencia consultada el 25/10/2026,
R36 termino actualizaciones de seguridad el 02/09/2026 y conserva soporte
tecnico hasta el 30/11/2027. Actualizar su ultimo parche no extiende esas fechas.
La evaluacion de R37/LTS queda fuera de estos scripts y no se automatiza.
