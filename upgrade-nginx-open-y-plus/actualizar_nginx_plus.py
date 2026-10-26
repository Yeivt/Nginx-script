#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Actualizacion local de NGINX en RHEL 8/9. Python 3.6+.

Archivo autonomo: framework -> ejecutar() o CLI; Ansible lo ejecuta en el destino.
El proceso auxiliar usa los bindings DNF/RPM del Python de RHEL. Las instalaciones
se realizan con DNF CLI, que conserva sus plugins/politicas corporativas.
"""
import argparse
import configparser
import datetime
import difflib
import fcntl
import functools
import hashlib
import io
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import urlsplit

# El generador fija esta constante para cada archivo entregable.
TIPO_SCRIPT = "NginxPlus"
version = TIPO_SCRIPT
server = ""  # LNKXXX; tambien --server o variable de entorno server.
CONFIG = Path('/etc/nginx/nginx.conf')
REPOS = Path('/etc/yum.repos.d')
BACKUP_ROOT = '/var/backups/nginx-upgrade'
RPM_FIELDS = '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}|%{VENDOR}|%{MODULARITYLABEL}\\n'


class UpgradeError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise UpgradeError(message)


def digest(path):
    h = hashlib.sha256()
    with open(str(path), 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def json_write(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()


def command(args, log=None, check=True, timeout=90):
    env = os.environ.copy()
    env.update(LC_ALL='C', LANG='C')
    proc = subprocess.run([str(x) for x in args], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True,
                          env=env, timeout=timeout)
    if log:
        with open(str(log), 'a') as f:
            f.write('\n$ ' + ' '.join(shlex.quote(str(x)) for x in args) + '\n')
            f.write(proc.stdout)
    if check and proc.returncode:
        raise UpgradeError('Fallo {} (rc={}); revisar el log protegido.'.format(args[0], proc.returncode))
    return proc


def inventory(log=None):
    p = command(['rpm', '-qa', '--qf', RPM_FIELDS], log)
    result = []
    for line in p.stdout.splitlines():
        a = line.split('|')
        if len(a) != 7 or not a[0].startswith('nginx'):
            continue
        name, epoch, ver, rel, arch, vendor, modular = a
        result.append(dict(name=name, epoch=epoch, version=ver, release=rel,
                           arch=arch, vendor=vendor, modular=modular,
                           evr='{}:{}-{}'.format(epoch, ver, rel)))
    return sorted(result, key=lambda x: (x['name'], x['arch']))


def origin_of(pkg):
    vendor = pkg['vendor'].lower()
    if 'red hat' in vendor:
        return 'redhat'
    if 'nginx' in vendor or 'f5' in vendor:
        return 'plus' if pkg['name'] == 'nginx-plus' else 'nginxorg'
    raise UpgradeError('Fabricante no reconocido; no se cambiara automaticamente de proveedor.')


def same_stream(a, b):
    def stream(p):
        label = p.get('modular', '')
        if label and label not in ('(none)', '(null)'):
            return tuple(label.split(':')[:2])
        return ('nginx', '.'.join(p['version'].split('.')[:2]))
    return stream(a) == stream(b)


def safe_candidate(pkg, current, origin, major):
    if pkg['arch'] != current['arch'] or origin_of(pkg) != origin:
        return False
    if not re.search(r'(?:^|[.+])el' + re.escape(major) + r'(?:[.+]|$)', pkg['release']):
        return False
    if origin == 'plus':
        return pkg['version'] == '36'
    if origin == 'redhat':
        return same_stream(current, pkg)
    parts = pkg['version'].split('.')
    return len(parts) >= 2 and parts[0] == '1' and parts[1].isdigit() and int(parts[1]) % 2 == 0


def family_names(items, origin):
    if origin == 'plus':
        return {p['name'] for p in items if p['name'] == 'nginx-plus' or p['name'].startswith('nginx-plus-module-')}
    return {p['name'] for p in items if p['name'] == 'nginx' or p['name'].startswith(('nginx-module-', 'nginx-mod-', 'nginx-filesystem', 'nginx-all-modules'))}


def tree_hashes(root):
    result = {}
    for parent, dirs, files in os.walk(str(root), followlinks=False):
        for name in sorted(dirs + files):
            path = Path(parent) / name
            key = str(path.relative_to(root))
            if path.is_symlink():
                result[key] = 'link:' + os.readlink(str(path))
            elif path.is_file():
                result[key] = digest(path)
    return result


def new_parser():
    return configparser.ConfigParser(interpolation=None, strict=True)


def repo_workspace(dest, origin, major, selected):
    """Copia temporal para consultar; jamas modifica los repos del servidor."""
    dest.mkdir(mode=0o700)
    sections = {}
    for src in sorted(REPOS.glob('*.repo')):
        parser = new_parser()
        parser.read(str(src), encoding='utf-8')
        for section in parser.sections():
            require(section not in sections, 'ID de repositorio duplicado: ' + section)
            sections[section] = (src, parser)
        shutil.copy2(str(src), str(dest / src.name))
    chosen = selected or ([] if origin == 'redhat' else ['nginx-plus' if origin == 'plus' else 'nginx-stable'])
    modifications = []
    if not chosen:
        return [], modifications
    for rid in chosen:
        require(re.fullmatch(r'[A-Za-z0-9_.:-]+', rid), 'ID de repositorio invalido.')
        if rid not in sections:
            matches = []
            for src in sorted((REPOS / 'bkp').glob('*.repo')):
                parser = new_parser()
                parser.read(str(src), encoding='utf-8')
                if parser.has_section(rid):
                    matches.append((src, parser))
            require(len(matches) == 1, 'No se encontro un unico .repo para {} en el directorio principal/bkp.'.format(rid))
            src, parser = matches[0]
            require(not (REPOS / src.name).exists(), 'El nombre de repositorio ya existe con contenido diferente: ' + src.name)
            require(not (dest / src.name).exists(), 'Colision de archivos de repositorios.')
            # Copiar solamente este archivo, nunca nginx-agent.repo ni un .repo_bkp.
            shutil.copy2(str(src), str(dest / src.name))
            for section in parser.sections():
                require(section not in sections, 'ID duplicado al recuperar el archivo: ' + section)
                sections[section] = (src, parser)
        src, _ = sections[rid]
        target = dest / src.name
        parser = new_parser()
        parser.read(str(target), encoding='utf-8')
        settings = parser[rid]
        require(settings.get('gpgcheck', '1').strip().lower() in ('1', 'true', 'yes'), 'El repositorio debe tener gpgcheck=1.')
        require(settings.get('sslverify', '1').strip().lower() in ('1', 'true', 'yes'), 'El repositorio debe tener sslverify=1.')
        settings['enabled'] = '1'
        settings['gpgcheck'] = '1'
        settings['sslverify'] = '1'
        if origin == 'plus':
            require(len(chosen) == 1, 'Plus admite un solo repositorio R36.')
            expected = 'https://pkgs.nginx.com/plus/R36/centos/{}/$basearch/'.format(major)
            url = settings.get('baseurl', '').strip().replace('$releasever', major).replace('${releasever}', major)
            require(url.rstrip('/') == expected.rstrip('/'), 'Plus requiere una URL R36 del RHEL real ({}). No se cambiara de rama.'.format(major))
            for field in ('sslclientcert', 'sslclientkey'):
                require(Path(settings.get(field, '')).is_file(), 'Falta el archivo existente indicado por ' + field)
            settings['baseurl'] = expected
        elif origin == 'nginxorg':
            if rid == 'nginx-stable':
                url = settings.get('baseurl', '').strip()
                parsed = urlsplit(url)
                require(parsed.scheme == 'https' and parsed.hostname == 'nginx.org' and '/mainline/' not in parsed.path,
                        'nginx-stable debe apuntar al canal stable oficial; para Satellite usar --repo ID_STABLE.')
                settings['baseurl'] = url.replace('$releasever', major).replace('${releasever}', major)
            settings['module_hotfixes'] = 'true'
            if parser.has_section('nginx-mainline'):
                parser['nginx-mainline']['enabled'] = '0'
        with open(str(target), 'w', encoding='utf-8') as f:
            parser.write(f)
        modifications.append((src.name, target))
    # Agrupar cambios de secciones que compartan archivo.
    return chosen, list({name: (name, path) for name, path in modifications}.values())


def worker(request):
    """DNF API solamente consulta/resuelve/descarga. Nunca instala paquetes."""
    import dnf
    import dnf.rpm
    import rpm
    import hawkey

    errors = []
    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                errors.append(record.getMessage())
    handler = Capture()
    logging.getLogger().addHandler(handler)
    def info(p):
        return dict(name=p.name, epoch=str(p.epoch), version=p.version, release=p.release,
                    arch=p.arch, vendor=p.vendor or '', modular=p.modularitylabel or '',
                    evr='{}:{}-{}'.format(p.epoch, p.version, p.release), nevra=str(p), repo=p.reponame)
    def compare(a, b):
        return rpm.labelCompare((str(a.epoch), a.version, a.release), (str(b.epoch), b.version, b.release))
    try:
        with dnf.Base() as base:
            base.conf.read()
            # Sin plugins en la consulta auxiliar. La instalacion DNF CLI SI los aplica.
            base.conf.reposdir = [request['reposdir']]
            base.conf.cachedir = str(Path(request['run']) / 'dnf-cache')
            base.conf.logdir = str(Path(request['run']) / 'dnf-log')
            base.conf.persistdir = str(Path(request['run']) / 'dnf-state')
            base.conf.keepcache = True
            base.conf.install_weak_deps = False
            base.conf.best = True
            base.conf.obsoletes = False
            base.conf.substitutions['releasever'] = dnf.rpm.detect_releasever('/') or request['major']
            base.read_all_repos()
            for repo in list(base.repos.iter_enabled()):
                repo.skip_if_unavailable = False
                repo.metadata_expire = 0
                repo.timeout = 30
                repo.retries = 2
                # No se habilitan otros canales nginx, ni nginx-agent.
                if repo.id.startswith(('nginx-', 'nginx_')) and repo.id not in request['repos']:
                    repo.disable()
            for rid in request['repos']:
                base.repos[rid].enable()
            base.fill_sack()
            require(not errors, 'DNF informo errores al cargar metadatos: ' + '; '.join(errors))
            installed = list(base.sack.query().installed())
            mains = [p for p in installed if p.name == request['main']]
            require(len(mains) == 1, 'Se requiere un solo paquete principal instalado.')
            current = mains[0]
            require(info(current)['evr'] == request['current']['evr'], 'Cambio el RPM instalado durante la consulta.')
            candidates = []
            for p in base.sack.query().available().filter(name=current.name):
                if request['repos'] and p.reponame not in request['repos']:
                    continue
                try:
                    valid = safe_candidate(info(p), info(current), request['origin'], request['major'])
                except UpgradeError:
                    valid = False
                if valid and compare(p, current) >= 0:
                    if not request['target'] or request['target'] in (info(p)['evr'], p.evr, '{}-{}'.format(p.version, p.release), str(p)):
                        candidates.append(p)
            require(candidates, 'No hay un destino permitido disponible. Revisar el target, repositorio, stream y Content View.')
            candidate = max(candidates, key=functools.cmp_to_key(compare))
            selected_repo = candidate.reponame
            require(base.repos[selected_repo].gpgcheck and base.repos[selected_repo].sslverify,
                    'El repositorio seleccionado debe verificar firmas y TLS.')
            main_changed = compare(candidate, current) > 0
            if main_changed:
                base.package_upgrade(candidate)
            # Actualizar juntos los paquetes auxiliares instalados. No instalar todos los modulos.
            for p in installed:
                if p.name in request['family'] and p.name != current.name:
                    base.upgrade(p.name, reponame=selected_repo)
            base.resolve(allow_erasing=False)
            require(not errors, 'DNF informo errores de resolucion: ' + '; '.join(errors))
            added = list(base.transaction.install_set)
            removed = list(base.transaction.remove_set)
            validate_transaction([info(p) for p in added], [info(p) for p in removed],
                                 [info(p) for p in installed], request, info(candidate))
            # Si el principal no cambia, solamente se permiten actualizaciones compatibles de modulos.
            result = dict(ok=True, current=info(current), target=info(candidate),
                          packages=[info(p) for p in added], replaced=[info(p) for p in removed],
                          changed=bool(added), policy='R36' if request['origin'] == 'plus' else request['origin'])
            if request['download'] and added:
                needed = sum(p.downloadsize for p in added) + 128 * 1024 * 1024
                require(shutil.disk_usage(request['run']).free > needed, 'Espacio insuficiente para descargar todos los RPM.')
                require(shutil.disk_usage('/usr').free > sum(p.installsize for p in added) + 128 * 1024 * 1024,
                        'Espacio insuficiente en /usr para la transaccion.')
                base.download_packages(added)
                files = []
                for p in added:
                    code, message = base.package_signature_check(p)
                    require(code == 0 and base.repos[p.reponame].gpgcheck, 'Firma RPM no validada; no se importan claves automaticamente: ' + message)
                    path = p.localPkg()
                    files.append(dict(path=path, sha256=digest(path)))
                result['files'] = files
            return result
    finally:
        logging.getLogger().removeHandler(handler)


def validate_transaction(added, removed, installed, request, target):
    """Bloquea downgrades, borrados, cambios de proveedor/stream y otros productos."""
    allowed = set(request['family'])
    existing = {(p['name'], p['arch']): p for p in installed}
    newkeys = {(p['name'], p['arch']) for p in added}
    for p in added:
        require(p['name'] in allowed and (p['name'], p['arch']) in existing,
                'La transaccion incluye un paquete nuevo/ajeno al alcance: ' + p['name'])
        old = existing[(p['name'], p['arch'])]
        require(origin_of(p) == origin_of(old), 'Cambio de fabricante bloqueado: ' + p['name'])
        require(p['repo'] == target['repo'], 'Paquete fuera del repositorio elegido: ' + p['name'])
        if p['name'] == request['main']:
            require(p['evr'] == target['evr'], 'El resolvedor intento cambiar el destino principal.')
            require(safe_candidate(p, old, request['origin'], request['major']), 'El resolvedor eligio un destino fuera de la politica.')
        if request['origin'] == 'redhat':
            require(same_stream(p, old), 'Cambio de stream bloqueado: ' + p['name'])
        if request['origin'] == 'plus' and p['name'] == 'nginx-plus':
            require(p['version'] == '36', 'Migracion fuera de R36 bloqueada.')
    for p in removed:
        require((p['name'], p['arch']) in newkeys, 'Borrado sin reemplazo bloqueado: ' + p['name'])
    # Comparacion RPM nativa, sin comparar versiones como texto.
    if added:
        import rpm
        for p in added:
            old = existing[(p['name'], p['arch'])]
            require(rpm.labelCompare((p['epoch'], p['version'], p['release']),
                                     (old['epoch'], old['version'], old['release'])) >= 0,
                    'Downgrade bloqueado: ' + p['name'])


def dnf_python(log):
    candidates = [sys.executable, '/usr/libexec/platform-python', '/usr/bin/python3']
    for interpreter in dict.fromkeys(candidates):
        if Path(interpreter).is_file() and command([interpreter, '-c', 'import dnf, rpm, hawkey'], log, check=False).returncode == 0:
            return interpreter
    raise UpgradeError('No se encontro el Python de RHEL con bindings dnf/rpm/hawkey.')


def get_plan(request, interpreter, run, log):
    req = run / 'dnf-request.json'
    result = run / 'dnf-result.json'
    request['result'] = str(result)
    json_write(req, request)
    proc = command([interpreter, str(Path(__file__).resolve()), '--_worker', str(req)], log, check=False, timeout=None)
    require(result.is_file(), 'DNF no devolvio un resultado; revisar ejecucion.log.')
    value = json.loads(result.read_text())
    require(proc.returncode == 0 and value.get('ok'), value.get('error', 'Fallo la consulta DNF.'))
    return value


def snapshot(run, log):
    size = sum((Path(parent) / name).lstat().st_size
               for directory in (CONFIG.parent, REPOS)
               for parent, _, files in os.walk(str(directory), followlinks=False)
               for name in files)
    require(shutil.disk_usage(str(run)).free > size * 2 + 64 * 1024 * 1024,
            'Espacio insuficiente para crear y verificar el respaldo.')
    root = run / 'backup'
    root.mkdir(mode=0o700)
    command(['cp', '-a', '--', str(CONFIG.parent), str(root / 'nginx')], log)
    command(['cp', '-a', '--', str(REPOS), str(root / 'yum.repos.d')], log)
    original = root / 'nginx.conf.original'
    command(['cp', '--preserve=all', '--dereference', '--', str(CONFIG), str(original)], log)
    value = dict(sha256=digest(CONFIG), symlink=CONFIG.is_symlink(),
                 link=os.readlink(str(CONFIG)) if CONFIG.is_symlink() else None,
                 realpath=str(CONFIG.resolve()), original=str(original), tree=tree_hashes(CONFIG.parent))
    require(digest(original) == value['sha256'], 'El respaldo de nginx.conf no coincide con el original.')
    require(tree_hashes(root / 'nginx') == value['tree'], 'El respaldo de /etc/nginx no pudo verificarse.')
    json_write(root / 'manifest.json', value)
    return value


def preserve_and_restore_config(run, saved, log, force=False):
    original = Path(saved['original'])
    require(digest(original) == saved['sha256'], 'El respaldo original fue alterado; restauracion cancelada.')
    current_hash = digest(CONFIG) if CONFIG.is_file() else None
    topology_changed = CONFIG.is_symlink() != saved['symlink'] or (CONFIG.is_symlink() and os.readlink(str(CONFIG)) != saved['link'])
    changed = current_hash != saved['sha256'] or topology_changed
    result = dict(changed=changed, restored=False, before_sha256=saved['sha256'], package_sha256=current_hash)
    if changed or force:
        # Conservar la primera version recibida del paquete, sin pisarla en un segundo intento.
        received = run / 'nginx.conf.recibido_del_paquete'
        if CONFIG.is_file() and not received.exists():
            command(['cp', '--preserve=all', '--dereference', '--', str(CONFIG), str(received)], log)
            before = original.read_bytes().decode('utf-8', errors='replace').splitlines(True)
            after = received.read_bytes().decode('utf-8', errors='replace').splitlines(True)
            (run / 'nginx.conf.diff').write_text(''.join(difflib.unified_diff(before, after, fromfile='backup/nginx.conf', tofile='paquete/nginx.conf')))
        destination = Path(saved['realpath'])
        fd, temp = tempfile.mkstemp(prefix='.nginx-restore-', dir=str(destination.parent))
        os.close(fd)
        try:
            command(['cp', '--preserve=all', '--', str(original), temp], log)
            os.replace(temp, str(destination))
        finally:
            if os.path.lexists(temp):
                os.unlink(temp)
        if saved['symlink']:
            temp_link = str(CONFIG) + '.' + uuid.uuid4().hex + '.link'
            try:
                os.symlink(saved['link'], temp_link)
                os.replace(temp_link, str(CONFIG))
            finally:
                if os.path.lexists(temp_link):
                    os.unlink(temp_link)
            # Recuperar metadatos del propio enlace.
            source_link = run / 'backup' / 'nginx' / 'nginx.conf'
            stat = source_link.lstat()
            os.lchown(str(CONFIG), stat.st_uid, stat.st_gid)
        elif CONFIG.is_symlink():
            # El RPM sustituyo un archivo regular por un enlace.
            require(str(destination) == str(CONFIG), 'Topologia inesperada de nginx.conf.')
        require(digest(CONFIG) == saved['sha256'], 'La restauracion no recupero exactamente nginx.conf.')
        result['restored'] = True
    result['final_sha256'] = digest(CONFIG)
    return result


def assert_server(expected):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', expected or '') is not None,
            'server es obligatorio y debe ser un hostname.')
    local = socket.gethostname().rstrip('.').lower()
    given = expected.rstrip('.').lower()
    require(given == local if '.' in given else given == local.split('.')[0],
            'server={} no coincide con esta VM ({}). Ejecutar en el destino via framework/Ansible.'.format(expected, local))


def service_pid(log=None):
    p = command(['systemctl', 'show', 'nginx', '--property=MainPID', '--value'], log)
    require(p.stdout.strip().isdigit() and int(p.stdout.strip()) > 1, 'NGINX no tiene un MainPID valido.')
    return int(p.stdout.strip())


def runtime_matches(binary, log=None):
    pid = service_pid(log)
    running = os.stat('/proc/{}/exe'.format(pid))
    disk = os.stat(str(binary))
    require((running.st_dev, running.st_ino) == (disk.st_dev, disk.st_ino), 'El binario en ejecucion no coincide con el instalado en disco.')
    return pid


def persist_repositories(modifications, run, log, written=None):
    if written is None:
        written = []
    for name, proposed in modifications:
        target = REPOS / name
        require(not target.is_symlink(), 'No se modifican archivos .repo enlazados.')
        if target.exists() and target.read_bytes() == proposed.read_bytes():
            continue
        existed = target.exists()
        fd, temp = tempfile.mkstemp(prefix='.nginx-repo-', dir=str(REPOS))
        os.close(fd)
        try:
            if existed:
                command(['cp', '--preserve=all', '--', str(target), temp], log)
            else:
                command(['cp', '--preserve=all', '--', str(proposed), temp], log)
            Path(temp).write_bytes(proposed.read_bytes())
            os.replace(temp, str(target))
            written.append(dict(name=name, existed=existed, sha256=digest(target)))
            if shutil.which('restorecon'):
                command(['restorecon', str(target)], log)
        finally:
            if os.path.lexists(temp):
                os.unlink(temp)
    return written


def restore_repositories(written, run, log):
    for item in reversed(written):
        target = REPOS / item['name']
        require(target.is_file() and digest(target) == item['sha256'], 'El .repo cambio concurrentemente; revisar manualmente: ' + item['name'])
        if item['existed']:
            command(['cp', '--preserve=all', '--', str(run / 'backup' / 'yum.repos.d' / item['name']), str(target)], log)
        else:
            target.unlink()


def transaction_repositories(run, plan):
    """Whitelist privada para la DNF CLI: solo los nombres ya resueltos."""
    dest = run / 'transaction-repos'
    shutil.copytree(str(run / 'repos'), str(dest))
    chosen = plan['target']['repo']
    names = sorted({p['name'] for p in plan['packages']})
    found = False
    for path in dest.glob('*.repo'):
        parser = new_parser()
        parser.read(str(path), encoding='utf-8')
        for rid in parser.sections():
            parser[rid]['enabled'] = '1' if rid == chosen else '0'
            if rid == chosen:
                found = True
                parser[rid]['includepkgs'] = ' '.join(names)
                parser[rid]['gpgcheck'] = '1'
                parser[rid]['sslverify'] = '1'
        with open(str(path), 'w', encoding='utf-8') as f:
            parser.write(f)
    require(found, 'No se encontro el repositorio de la transaccion.')
    return dest


def validate_modules(binary, allowed, run, log):
    dump = command([str(binary), '-T', '-c', str(CONFIG)], log)
    (run / 'configuracion_antes.txt').write_text(dump.stdout)
    prefix_text = command([str(binary), '-V'], log).stdout
    match = re.search(r'--prefix=([^\s]+)', prefix_text)
    prefix = Path(match.group(1).strip("'\"")) if match else Path('/usr/local/nginx')
    for module in re.findall(r'^\s*load_module\s+([^;]+);', dump.stdout, re.MULTILINE):
        fields = shlex.split(module)
        require(len(fields) == 1 and '$' not in fields[0], 'load_module no resoluble automaticamente.')
        path = Path(fields[0])
        if not path.is_absolute():
            path = prefix / path
        owner = command(['rpm', '-qf', '--qf', '%{NAME}', str(path.resolve())], log, check=False)
        require(owner.returncode == 0 and owner.stdout.strip() in allowed,
                'Modulo externo al conjunto de RPM gestionado: {}. Homologarlo antes del upgrade.'.format(path))


def ejecutar(version, server, aplicar=False, target=None, repos=None, backup_root=BACKUP_ROOT):
    """API publica. Devuelve dict JSON; no hace SSH. Ejecutar en la VM destino."""
    report = dict(estado='error', version=version, server=server, changed=False,
                  modo='aplicar' if aplicar else 'plan', codigo=1)
    run = None
    saved = None
    tx_attempted = False
    written = []
    lock = None
    try:
        require(version == TIPO_SCRIPT, 'Este archivo solamente admite version=' + TIPO_SCRIPT)
        assert_server(server)
        require(os.geteuid() == 0, 'Ejecutar como root o mediante become de Ansible.')
        for executable in ('rpm', 'rpmkeys', 'dnf', 'systemctl', 'cp'):
            require(shutil.which(executable), 'Falta el comando ' + executable)
        require(Path('/run/lock').is_dir(), 'No existe /run/lock.')
        lock = open('/run/lock/nginx-upgrade.lock', 'a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpgradeError('Ya hay otro script de upgrade NGINX ejecutandose.')
        release = {}
        for line in Path('/etc/os-release').read_text().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                release[k] = v.strip('"')
        require(release.get('ID') == 'rhel', 'Solamente se admite Red Hat Enterprise Linux.')
        major = release.get('VERSION_ID', '').split('.')[0]
        require(major in ('8', '9'), 'Solamente se admiten RHEL 8 y 9.')
        require(CONFIG.is_file(), 'No existe /etc/nginx/nginx.conf.')
        require(not CONFIG.parent.is_symlink() and not REPOS.is_symlink(),
                'Los directorios /etc/nginx y /etc/yum.repos.d no deben ser enlaces; adaptar el respaldo para ese esquema.')
        # Directorios nuevos privados; no cambiar el umask global del framework.
        root = Path(backup_root).resolve()
        for source_dir in (CONFIG.parent.resolve(), REPOS.resolve()):
            require(root != source_dir and not str(root).startswith(str(source_dir) + '/'),
                    'El respaldo debe quedar fuera de /etc/nginx y /etc/yum.repos.d.')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(root.stat().st_uid == 0 and root.stat().st_mode & 0o077 == 0, 'backup_root debe ser de root con permisos 0700.')
        hostdir = root / server.lower()
        hostdir.mkdir(mode=0o700, exist_ok=True)
        require(not hostdir.is_symlink() and hostdir.stat().st_uid == 0 and hostdir.stat().st_mode & 0o077 == 0, 'Directorio de host inseguro.')
        run = hostdir / (datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
        run.mkdir(mode=0o700)
        log = run / 'ejecucion.log'
        log.touch(mode=0o600)
        report.update(directorio=str(run), reporte=str(run / 'resultado.json'), log=str(log), rhel=release['VERSION_ID'])
        items = inventory(log)
        main_name = 'nginx-plus' if version == 'NginxPlus' else 'nginx'
        main_items = [p for p in items if p['name'] == main_name]
        require(len(main_items) == 1, 'No hay un unico paquete instalado de tipo ' + version)
        require(not any(p['name'] == ('nginx' if main_name == 'nginx-plus' else 'nginx-plus') for p in items), 'Instalacion mezclada Open/Plus.')
        current = main_items[0]
        origin = origin_of(current)
        require((origin == 'plus') == (version == 'NginxPlus'), 'Tipo y fabricante no coinciden.')
        if origin == 'plus':
            require(current['version'] == '36', 'El script Plus solo actualiza una instalacion R36 existente.')
        report.update(origen=origin, antes=current)
        family = family_names(items, origin)
        require(any(p['name'] == 'nginx-agent' for p in items), 'nginx-agent no esta instalado.')
        for unit in ('nginx', 'nginx-agent'):
            command(['systemctl', 'is-active', '--quiet', unit], log)
        binary = Path(shutil.which('nginx') or '/usr/sbin/nginx').resolve()
        owner = command(['rpm', '-qf', '--qf', '%{NAME}', str(binary)], log).stdout.strip()
        require(owner == main_name, 'El binario nginx no pertenece al RPM esperado.')
        pid_before = runtime_matches(binary, log)
        cmdline = Path('/proc/{}/cmdline'.format(pid_before)).read_bytes().decode(errors='replace').replace('\x00', ' ')
        args = shlex.split(cmdline)
        for i, arg in enumerate(args):
            if arg == '-c':
                require(i + 1 < len(args) and Path(args[i + 1]) == CONFIG, 'La unidad usa un nginx.conf diferente.')
            elif arg.startswith('-c') and len(arg) > 2:
                require(Path(arg[2:]) == CONFIG, 'La unidad usa un nginx.conf diferente.')
            require(not (arg == '-p' or arg.startswith('-p/')), 'Prefijo -p personalizado: requiere adaptacion del script.')
        validate_modules(binary, family, run, log)
        report['binario_antes'] = command([str(binary), '-v'], log).stdout.strip()
        if aplicar:
            # Siempre ANTES de preparar/restaurar repositorios y de cualquier transaccion.
            saved = snapshot(run, log)
            report['backup'] = str(run / 'backup')
        selected, modifications = repo_workspace(run / 'repos', origin, major, repos or [])
        interpreter = dnf_python(log)
        request = dict(run=str(run), reposdir=str(run / 'repos'), origin=origin, major=major,
                       repos=selected, main=main_name, current=current, family=sorted(family),
                       target=target, download=bool(aplicar))
        plan = get_plan(request, interpreter, run, log)
        report['plan'] = plan
        if not aplicar:
            report.update(estado='plan generado', codigo=0)
            return report
        if not plan['changed']:
            require(tree_hashes(CONFIG.parent) == saved['tree'] and digest(CONFIG) == saved['sha256'], 'La configuracion cambio concurrentemente.')
            report.update(estado='sin cambios', despues=current, codigo=0)
            report['nginx_conf'] = preserve_and_restore_config(run, saved, log)
            return report
        # Revalidar despues de descargar: RPMDB, configuracion y procesos no deben cambiar.
        require(inventory(log) == items, 'El inventario cambio concurrentemente; ejecutar de nuevo el plan.')
        require(tree_hashes(CONFIG.parent) == saved['tree'] and digest(CONFIG) == saved['sha256'], 'La configuracion cambio durante la descarga; no se instalara ni restaurara sobre cambios ajenos.')
        require(runtime_matches(binary, log) == pid_before, 'NGINX cambio de proceso durante la preparacion.')
        for f in plan['files']:
            require(digest(f['path']) == f['sha256'], 'Un RPM descargado cambio antes de instalar.')
        persist_repositories(modifications, run, log, written)
        # Resolver otra vez con DNF CLI y sus plugins (incluido versionlock).
        # Cache-only evita descargar durante la instalacion. La whitelist privada
        # no expone bibliotecas/SO/PHP/agent como candidatos al resolvedor.
        txrepos = transaction_repositories(run, plan)
        tx_attempted = True
        report.update(estado='instalando', transaccion_intentada=True, changed=True)
        json_write(run / 'resultado.json', report)
        transaction = ['dnf', '-y', '--cacheonly', '--disablerepo=*',
                       '--enablerepo=' + plan['target']['repo'],
                       '--setopt=reposdir=' + str(txrepos),
                       '--setopt=cachedir=' + str(run / 'dnf-cache'),
                       '--setopt=obsoletes=False', '--setopt=install_weak_deps=False',
                       '--setopt=clean_requirements_on_remove=False', '--best', 'upgrade']
        transaction += [p['nevra'] for p in plan['packages']]
        command(transaction, log, timeout=None)
        report['changed'] = True
        # Restauracion exacta ANTES de probar/activar el nuevo binario.
        report['nginx_conf'] = preserve_and_restore_config(run, saved, log)
        after_tree = tree_hashes(CONFIG.parent)
        report['otros_archivos_config_cambiados'] = sorted(k for k in set(saved['tree']) | set(after_tree)
                                                         if k != 'nginx.conf' and saved['tree'].get(k) != after_tree.get(k))
        tested = command([str(binary), '-t', '-c', str(CONFIG)], log, check=False)
        if tested.returncode:
            report['nginx_conf'] = preserve_and_restore_config(run, saved, log, force=True)
            command([str(binary), '-t', '-c', str(CONFIG)], log)
        after = inventory(log)
        now = next(p for p in after if p['name'] == main_name)
        require(now['evr'] == plan['target']['evr'], 'El RPM principal final no coincide con el destino.')
        expected = {(p['name'], p['arch']): p['evr'] for p in plan['packages']}
        actual = {(p['name'], p['arch']): p['evr'] for p in after}
        require(all(actual.get(k) == v for k, v in expected.items()), 'Los RPM finales no coinciden con el plan.')
        require(next(p for p in after if p['name'] == 'nginx-agent')['evr'] == next(p for p in items if p['name'] == 'nginx-agent')['evr'], 'La version de nginx-agent cambio inesperadamente.')
        start = time.monotonic()
        command(['systemctl', 'restart', 'nginx'], log, timeout=None)
        report['reinicio_segundos'] = round(time.monotonic() - start, 3)
        for unit in ('nginx', 'nginx-agent'):
            command(['systemctl', 'is-active', '--quiet', unit], log)
        report['pid_despues'] = runtime_matches(binary, log)
        require(digest(CONFIG) == saved['sha256'], 'nginx.conf cambio despues de activar.')
        report.update(estado='actualizado', despues=now, codigo=0,
                      binario_despues=command([str(binary), '-v'], log).stdout.strip())
        # No afirmar validacion funcional de VHosts; corresponde a la segunda fase.
        report['validacion_funcional_vhosts'] = 'pendiente de segunda fase'
        return report
    except Exception as exc:
        report.update(estado='error', error=str(exc), codigo=1, transaccion_intentada=tx_attempted)
        if run:
            with open(str(run / 'ejecucion.log'), 'a') as f:
                f.write(traceback.format_exc())
            if saved and tx_attempted:
                try:
                    recovery = preserve_and_restore_config(run, saved, run / 'ejecucion.log', force=True)
                    report['recuperacion_nginx_conf'] = recovery
                except Exception as restore_error:
                    report['error_restauracion'] = str(restore_error)
                report['requiere_revision_manual'] = True
                report['aviso'] = 'Se intento recuperar nginx.conf; esto NO revierte RPM. Revisar transaccion/servicio antes de continuar.'
                try:
                    report['inventario_despues_del_error'] = inventory(run / 'ejecucion.log')
                except Exception:
                    pass
            elif written:
                try:
                    restore_repositories(written, run, run / 'ejecucion.log')
                except Exception as restore_error:
                    report['error_restauracion_repos'] = str(restore_error)
        return report
    finally:
        if run:
            try:
                json_write(run / 'resultado.json', report)
            except Exception as output_error:
                report.update(estado='error', codigo=1, error_reporte=str(output_error))
        if lock:
            lock.close()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--_worker':
        request = json.loads(Path(sys.argv[2]).read_text())
        try:
            result = worker(request)
        except Exception as exc:
            traceback.print_exc()
            result = dict(ok=False, error=str(exc))
        json_write(request['result'], result)
        return 0 if result.get('ok') else 1
    parser = argparse.ArgumentParser(description='Upgrade local de ' + TIPO_SCRIPT + ' en RHEL. Por defecto solo genera un plan.')
    parser.add_argument('--version', default=os.environ.get('version', version), choices=['NginxOpen', 'NginxPlus'])
    parser.add_argument('--server', default=os.environ.get('server', server), help='Hostname esperado de la VM destino.')
    parser.add_argument('--apply', action='store_true', help='Aplicar; sin esta opcion solo consulta/resuelve.')
    parser.add_argument('--target', help='EVR exacto del paquete principal (copiar plan.target.evr). Opcional: ultimo permitido.')
    parser.add_argument('--repo', action='append', default=[], help='ID de repo fuente; repetir para mirrors de Satellite. No cambia proveedor/stream.')
    parser.add_argument('--backup-root', default=BACKUP_ROOT)
    args = parser.parse_args()
    result = ejecutar(args.version, args.server, args.apply, args.target, args.repo, args.backup_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result['codigo']


if __name__ == '__main__':
    sys.exit(main())
