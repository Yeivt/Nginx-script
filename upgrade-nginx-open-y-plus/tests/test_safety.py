import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('UPGRADE_SCRIPT', str(ROOT / 'actualizar_nginx_open.py')))
if not SOURCE.exists():
    SOURCE = ROOT / 'core.py'
spec = importlib.util.spec_from_file_location('upgrade_core', str(SOURCE))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def pkg(name='nginx', version='1.24.0', release='3.module+el8.10.0+24503+ctx.3', vendor='Red Hat, Inc.', modular='nginx:1.24:8100:ctx', epoch='1', repo='appstream'):
    return dict(name=name, version=version, release=release, vendor=vendor, modular=modular,
                epoch=epoch, arch='x86_64', evr='{}:{}-{}'.format(epoch, version, release), repo=repo)


class SafetyTests(unittest.TestCase):
    def test_server_does_not_execute_on_controller(self):
        with patch.object(m.socket, 'gethostname', return_value='controller.example.com'):
            result = m.ejecutar(m.TIPO_SCRIPT, 'LNK5141', aplicar=True)
        self.assertEqual(result['codigo'], 1)
        self.assertFalse(result['transaccion_intentada'])
        self.assertNotIn('directorio', result)

    def test_fqdn_mismatch_not_accepted_by_short_name(self):
        with patch.object(m.socket, 'gethostname', return_value='lnk5141.desa.example'):
            with self.assertRaises(m.UpgradeError):
                m.assert_server('lnk5141.prod.example')
            m.assert_server('LNK5141')

    def test_plus_never_migrates_r37(self):
        old = pkg('nginx-plus', '36', '7.el8.ngx', 'NGINX Packaging', '', '0')
        new = pkg('nginx-plus', '37.0.7', '1.el8.ngx', 'NGINX Packaging', '', '0')
        self.assertFalse(m.safe_candidate(new, old, 'plus', '8'))
        new['version'] = '36'
        self.assertTrue(m.safe_candidate(new, old, 'plus', '8'))

    def test_wrong_el_major_rejected(self):
        p = pkg('nginx-plus', '36', '9.el9.ngx', 'NGINX Packaging', '', '0')
        self.assertFalse(m.safe_candidate(p, p, 'plus', '8'))

    def test_nginxorg_mainline_not_a_stable_candidate(self):
        old = pkg(version='1.29.7', release='1.el9.ngx', vendor='NGINX Packaging', modular='', epoch='2')
        new = dict(old, version='1.31.8')
        self.assertFalse(m.safe_candidate(new, old, 'nginxorg', '9'))
        new['version'] = '1.30.6'
        self.assertTrue(m.safe_candidate(new, old, 'nginxorg', '9'))

    def test_redhat_cannot_change_stream(self):
        old = pkg()
        new = pkg(version='1.26.3', modular='nginx:1.26:9000:ctx')
        self.assertFalse(m.safe_candidate(new, old, 'redhat', '8'))

    def test_unknown_vendor_refused(self):
        with self.assertRaises(m.UpgradeError):
            m.origin_of(pkg(vendor='Unrelated vendor'))

    def test_agent_is_not_in_transaction_family(self):
        self.assertEqual(m.family_names([pkg(), pkg('nginx-agent'), pkg('php')], 'redhat'), {'nginx'})

    def test_php_dependency_change_refused(self):
        old = pkg()
        request = dict(family=['nginx'], origin='redhat', major='8', main='nginx')
        with self.assertRaisesRegex(m.UpgradeError, 'ajeno'):
            m.validate_transaction([pkg('php')], [], [old], request, old)

    def test_unrelated_erase_refused(self):
        old = pkg()
        request = dict(family=['nginx'], origin='redhat', major='8', main='nginx')
        with self.assertRaisesRegex(m.UpgradeError, 'Borrado'):
            m.validate_transaction([], [pkg('php')], [old], request, old)

    def test_solver_cannot_override_target(self):
        old = pkg()
        target = pkg(release='4.module+el8.10.0+24504+ctx')
        new = pkg(release='5.module+el8.10.0+24505+ctx')
        request = dict(family=['nginx'], origin='redhat', major='8', main='nginx')
        with self.assertRaisesRegex(m.UpgradeError, 'destino principal'):
            m.validate_transaction([new], [old], [old], request, target)

    def test_vendor_cannot_be_replaced(self):
        old = pkg()
        new = pkg(vendor='NGINX Packaging')
        request = dict(family=['nginx'], origin='redhat', major='8', main='nginx')
        with self.assertRaisesRegex(m.UpgradeError, 'fabricante'):
            m.validate_transaction([new], [old], [old], request, new)


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conf = self.root / 'etc' / 'nginx' / 'nginx.conf'
        self.conf.parent.mkdir(parents=True)
        self.conf.write_bytes(b'worker_processes auto;\nevents {}\n')
        self.conf.chmod(0o640)
        self.repos = self.root / 'etc' / 'yum.repos.d'
        (self.repos / 'bkp').mkdir(parents=True)
        self.run = self.root / 'run'
        self.run.mkdir(mode=0o700)
        self.log = self.run / 'test.log'
        self.patches = [patch.object(m, 'CONFIG', self.conf), patch.object(m, 'REPOS', self.repos)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def test_config_received_from_package_is_restored_byte_for_byte(self):
        saved = m.snapshot(self.run, self.log)
        original = self.conf.read_bytes()
        self.conf.write_bytes(b'# version paquete\nevents {}\n')
        result = m.preserve_and_restore_config(self.run, saved, self.log)
        self.assertTrue(result['restored'])
        self.assertEqual(self.conf.read_bytes(), original)
        self.assertEqual(self.conf.stat().st_mode & 0o777, 0o640)
        self.assertIn(b'version paquete', (self.run / 'nginx.conf.recibido_del_paquete').read_bytes())
        self.assertTrue((self.run / 'nginx.conf.diff').is_file())

    def test_identical_config_does_not_get_replaced(self):
        saved = m.snapshot(self.run, self.log)
        inode = self.conf.stat().st_ino
        result = m.preserve_and_restore_config(self.run, saved, self.log)
        self.assertFalse(result['restored'])
        self.assertEqual(self.conf.stat().st_ino, inode)

    def test_config_test_failure_forces_original_restoration(self):
        saved = m.snapshot(self.run, self.log)
        result = m.preserve_and_restore_config(self.run, saved, self.log, force=True)
        self.assertTrue(result['restored'])
        self.assertEqual(m.digest(self.conf), saved['sha256'])

    def test_missing_config_is_restored(self):
        saved = m.snapshot(self.run, self.log)
        self.conf.unlink()
        result = m.preserve_and_restore_config(self.run, saved, self.log)
        self.assertTrue(result['restored'])
        self.assertEqual(m.digest(self.conf), saved['sha256'])

    def test_external_symlink_config_restored(self):
        outside = self.root / 'external.conf'
        outside.write_bytes(self.conf.read_bytes())
        self.conf.unlink()
        self.conf.symlink_to(outside)
        saved = m.snapshot(self.run, self.log)
        self.conf.unlink()
        self.conf.write_bytes(b'# paquete reemplazo el enlace\n')
        outside.write_bytes(b'# contenido modificado\n')
        result = m.preserve_and_restore_config(self.run, saved, self.log)
        self.assertTrue(result['restored'])
        self.assertTrue(self.conf.is_symlink())
        self.assertEqual(m.digest(self.conf), saved['sha256'])

    def test_tampered_backup_cannot_overwrite_config(self):
        saved = m.snapshot(self.run, self.log)
        Path(saved['original']).write_bytes(b'corrupto\n')
        actual = self.conf.read_bytes()
        with self.assertRaises(m.UpgradeError):
            m.preserve_and_restore_config(self.run, saved, self.log, force=True)
        self.assertEqual(self.conf.read_bytes(), actual)

    def test_stable_repo_restored_only_in_temporary_workspace(self):
        text = '[nginx-stable]\nbaseurl=https://nginx.org/packages/centos/$releasever/$basearch/\ngpgcheck=1\nenabled=0\n\n[nginx-mainline]\nbaseurl=https://nginx.org/packages/mainline/centos/$releasever/$basearch/\nenabled=0\n'
        source = self.repos / 'bkp' / 'nginx.repo'
        source.write_text(text)
        (self.repos / 'bkp' / 'nginx-agent.repo').write_text('[nginx-agent]\nenabled=1\n')
        selected, mods = m.repo_workspace(self.run / 'repos', 'nginxorg', '9', [])
        self.assertEqual(selected, ['nginx-stable'])
        self.assertFalse((self.repos / 'nginx.repo').exists())
        self.assertEqual(source.read_text(), text)
        self.assertFalse((self.run / 'repos' / 'nginx-agent.repo').exists())
        p = m.new_parser()
        p.read(str(mods[0][1]))
        self.assertEqual(p['nginx-stable']['enabled'], '1')
        self.assertEqual(p['nginx-mainline']['enabled'], '0')
        self.assertIn('/centos/9/$basearch/', p['nginx-stable']['baseurl'])

    def test_repo_bkp_extension_is_not_loaded(self):
        (self.repos / 'plus9.repo_bkp').write_text('[nginx-plus]\nbaseurl=https://wrong.example/\n')
        with self.assertRaises(m.UpgradeError):
            m.repo_workspace(self.run / 'repos', 'plus', '8', [])

    def test_plus_rejects_el9_url_on_el8(self):
        (self.repos / 'bkp' / 'plus9.repo').write_text('[nginx-plus]\nbaseurl=https://pkgs.nginx.com/plus/R36/centos/9/$basearch/\nenabled=1\ngpgcheck=1\nsslverify=1\n')
        with self.assertRaisesRegex(m.UpgradeError, 'RHEL real'):
            m.repo_workspace(self.run / 'repos', 'plus', '8', [])

    def test_insecure_tls_not_accepted(self):
        (self.repos / 'bkp' / 'nginx.repo').write_text('[nginx-stable]\nbaseurl=https://nginx.org/packages/centos/9/$basearch/\nsslverify=0\n')
        with self.assertRaisesRegex(m.UpgradeError, 'sslverify=1'):
            m.repo_workspace(self.run / 'repos', 'nginxorg', '9', [])

    def test_unsigned_repository_not_accepted(self):
        (self.repos / 'bkp' / 'nginx.repo').write_text('[nginx-stable]\nbaseurl=https://nginx.org/packages/centos/9/$basearch/\ngpgcheck=0\n')
        with self.assertRaisesRegex(m.UpgradeError, 'gpgcheck=1'):
            m.repo_workspace(self.run / 'repos', 'nginxorg', '9', [])

    def test_repo_changes_can_be_reverted_before_transaction(self):
        (self.repos / 'bkp' / 'nginx.repo').write_text('[nginx-stable]\nbaseurl=https://nginx.org/packages/centos/9/$basearch/\ngpgcheck=1\nenabled=0\n')
        m.snapshot(self.run, self.log)
        _, mods = m.repo_workspace(self.run / 'repos', 'nginxorg', '9', [])
        written = []
        with patch.object(m.shutil, 'which', return_value=None):
            m.persist_repositories(mods, self.run, self.log, written)
        self.assertTrue((self.repos / 'nginx.repo').is_file())
        m.restore_repositories(written, self.run, self.log)
        self.assertFalse((self.repos / 'nginx.repo').exists())

    def test_transaction_whitelist_does_not_persist_to_live_repo(self):
        (self.repos / 'bkp' / 'nginx.repo').write_text('[nginx-stable]\nbaseurl=https://nginx.org/packages/centos/9/$basearch/\ngpgcheck=1\nenabled=0\nproxy=http://proxy.example:3128\n')
        _, mods = m.repo_workspace(self.run / 'repos', 'nginxorg', '9', [])
        original = mods[0][1].read_bytes()
        p = pkg(repo='nginx-stable')
        private = m.transaction_repositories(self.run, dict(target=p, packages=[p]))
        parsed = m.new_parser()
        parsed.read(str(private / 'nginx.repo'))
        self.assertEqual(parsed['nginx-stable']['includepkgs'], 'nginx')
        self.assertEqual(parsed['nginx-stable']['proxy'], 'http://proxy.example:3128')
        self.assertEqual(mods[0][1].read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
