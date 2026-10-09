"""Safety checks for restoring saved configuration without production access."""
import json
from pathlib import Path
import tempfile
import unittest
from scripts.backup import restore_engine


class IsolationTests(unittest.TestCase):
    def test_resolved_compose_isolates_builds_mounts_credentials_and_volumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config'
            (config / 'mcp').mkdir(parents=True)
            (config / 'compose.yaml').write_text('services: {}\n')
            (config / '.env').write_text('LIGHTRAG_API_KEY=internal-key\n')
            source = {
                'name': 'production',
                'services': {
                    'postgres': {'image': 'postgres', 'volumes': [{'type': 'volume', 'source': 'postgres_data', 'target': '/data'}]},
                    'mongodb': {'image': 'mongo'},
                    'mcp': {'build': {'context': '/production/mcp'}, 'environment': {'LIGHTRAG_API_KEY': 'internal-key'}},
                    'librechat': {'image': 'chat', 'ports': ['3080:3080'], 'container_name': 'production-chat', 'depends_on': {'mcp': {'condition': 'service_started'}}, 'environment': {'OPENAI_API_KEY': 'production-paid-key'}},
                },
                'volumes': {'postgres_data': {'name': 'production_database', 'external': True}},
            }
            (config / 'compose.resolved.json').write_text(json.dumps(source))
            manifest = {'compose_files': ['compose.yaml'], 'restore_compose_file': 'config/compose.resolved.json', 'project_root': '/production', 'images': {k: v.get('image', '') for k, v in source['services'].items()}, 'volumes': []}
            env = {'COMPOSE_PROJECT_NAME': 'ogr-restore-test'}
            output = restore_engine._render_sandbox_compose(None, config, root, env, root, manifest)
            saved = json.loads(output.read_text())
            self.assertEqual(saved['name'], 'ogr-restore-test')
            self.assertEqual(saved['volumes']['postgres_data'], {})
            self.assertTrue(saved['networks']['default']['internal'])
            self.assertNotIn('ports', saved['services']['librechat'])
            self.assertNotIn('container_name', saved['services']['librechat'])
            self.assertEqual(saved['services']['mcp']['build']['context'], str(config / 'mcp'))
            self.assertEqual(saved['services']['mcp']['environment']['LIGHTRAG_API_KEY'], 'internal-key')
            self.assertNotEqual(saved['services']['librechat']['environment']['OPENAI_API_KEY'], 'production-paid-key')
            self.assertIn('mcp', saved['services']['librechat']['depends_on'])

    def test_resolved_compose_rejects_bind_mount_outside_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config'
            config.mkdir()
            (config / 'compose.yaml').write_text('services: {}')
            source = {'services': {'postgres': {'image': 'postgres', 'volumes': [{'type': 'bind', 'source': '/outside/secret', 'target': '/secret'}]}, 'mongodb': {'image': 'mongo'}}}
            (config / 'compose.resolved.json').write_text(json.dumps(source))
            manifest = {'compose_files': ['compose.yaml'], 'restore_compose_file': 'config/compose.resolved.json', 'project_root': '/production', 'images': {'postgres':'postgres','mongodb':'mongo'}, 'volumes': []}
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                restore_engine._render_sandbox_compose(None, config, root, {'COMPOSE_PROJECT_NAME': 'ogr-restore-test'}, root, manifest)


if __name__ == '__main__':
    unittest.main()
