import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import preflight


class LocalProviderGuardTests(unittest.TestCase):
    def check_config(self, content):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "tests/e2e/librechat.local.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(content)
            settings = {"volumes": [{"target": "/app/librechat.yaml", "source": str(config)}]}
            with patch.object(preflight, "ROOT", root):
                preflight.check_local_librechat_config(settings)

    def fixture(self):
        return (preflight.ROOT / "tests/e2e/librechat.local.yaml").read_text()

    def test_isolated_provider_passes(self):
        self.check_config(self.fixture())

    def test_remote_provider_rejected(self):
        with self.assertRaises(SystemExit):
            self.check_config(self.fixture().replace("http://ollama:11434/v1", "https://api.openai.com/v1"))

    def test_extra_paid_endpoint_rejected(self):
        with self.assertRaises(SystemExit):
            self.check_config(self.fixture().replace("endpoints:\n", "endpoints:\n  openAI: {}\n"))

    def test_unapproved_model_rejected(self):
        with self.assertRaises(SystemExit):
            self.check_config(self.fixture().replace("qwen2.5:3b", "other-model"))

    def test_remote_tool_rejected(self):
        with self.assertRaises(SystemExit):
            self.check_config(self.fixture().replace("http://mcp:8000/mcp", "https://remote.example/mcp"))

class MockIsolationTests(unittest.TestCase):
    def test_default_ports_include_the_mock_gateway_port(self):
        self.assertIn(18080, preflight.DEFAULT_PORTS)
        self.assertIn(18000, preflight.DEFAULT_PORTS)

    def config(self):
        services = {
            "mock-integrations": {"networks": {
                "activepieces_internal": {"ipv4_address": "192.168.176.10"},
                "mock_control": {},
            }},
            "activepieces-app": {"environment": {"AP_SSRF_ALLOW_LIST": "192.168.176.10"}},
            "activepieces-worker": {"environment": {
                "AP_SSRF_ALLOW_LIST": "192.168.176.10", "AP_NETWORK_MODE": "STRICT"}},
        }
        networks = {
            "activepieces_internal": {"ipam": {"config": [{"subnet": "192.168.176.0/20"}]}},
            "mock_control": {"driver": "bridge", "driver_opts": {
                "com.docker.network.bridge.enable_ip_masquerade": "false"}},
        }
        return services, networks

    def test_allows_only_the_pinned_mock_ip_on_the_dedicated_network(self):
        preflight.check_mock_isolation(*self.config())

    def test_rejects_an_allow_list_that_includes_other_targets(self):
        services, networks = self.config()
        services["activepieces-app"]["environment"]["AP_SSRF_ALLOW_LIST"] = "192.168.176.10,127.0.0.1"
        with self.assertRaisesRegex(SystemExit, "allow-list"):
            preflight.check_mock_isolation(services, networks)

    def test_rejects_worker_without_strict_network_mode(self):
        services, networks = self.config()
        del services["activepieces-worker"]["environment"]["AP_NETWORK_MODE"]
        with self.assertRaisesRegex(SystemExit, "strict network mode"):
            preflight.check_mock_isolation(services, networks)

    def test_rejects_mock_alias_on_lightrag_network(self):
        services, networks = self.config()
        services["mock-integrations"]["networks"]["activepieces_internal"]["aliases"] = ["lightrag"]
        with self.assertRaisesRegex(SystemExit, "LightRAG"):
            preflight.check_mock_isolation(services, networks)

    def test_rejects_mock_control_alias(self):
        services, networks = self.config()
        services["mock-integrations"]["networks"]["mock_control"]["aliases"] = ["lightrag"]
        with self.assertRaisesRegex(SystemExit, "control network"):
            preflight.check_mock_isolation(services, networks)


if __name__ == "__main__":
    unittest.main()
