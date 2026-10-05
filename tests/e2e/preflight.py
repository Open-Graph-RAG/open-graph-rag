#!/usr/bin/env python3
"""Fail closed unless the migration Compose project and endpoints are isolated."""
import argparse
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "ap-migration-probe"
DEFAULT_PORTS = {8080, 8081, 11435, 13080, 15678, 18000, 18080, 19621, 19622}
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "activepieces.localhost", "activepieces-app",
                 "activepieces-worker", "activepieces-postgres", "activepieces-redis",
                 "postgres", "redis", "ollama", "lightrag", "mcp", "librechat", "mongodb",
                 "mock-integrations"}
ALLOWED_BINDS = {ROOT / p for p in (
    "tests/e2e/mock_server.py", "tests/e2e/librechat.yaml", "tests/e2e/librechat.local.yaml", "librechat.yaml",
    "postgres/init.sql", "docs/assets/logo.svg", "docs/assets/logo.png",
    "activepieces/nginx.conf", "activepieces/gateway.conf", "activepieces.nginx.conf",
)}
MOCK_SUBNET = "192.168.176.0/20"
MOCK_IP = "192.168.176.10"


def fail(message):
    raise SystemExit("E2E preflight rejected configuration: " + message)


def run(args):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        fail("Compose/Docker inspection failed; interpolated details withheld")
    return result.stdout


def check_bind(source, writable):
    # Docker Desktop additionally reports the same host mount through /host_mnt.
    # Normalize that fixed prefix, then require an exact allowlisted path.
    host_source = source.removeprefix("/host_mnt") if source.startswith("/host_mnt/") else source
    if writable or Path(host_source).resolve() not in ALLOWED_BINDS:
        fail("a bind mount is writable or outside the fixture/config allowlist")


def check_local_librechat_config(settings):
    import yaml
    expected = ROOT / "tests/e2e/librechat.local.yaml"
    mounts = [m for m in settings.get("volumes", []) if m.get("target") == "/app/librechat.yaml"]
    if len(mounts) != 1 or Path(mounts[0].get("source", "/")).resolve() != expected:
        fail("LibreChat must mount the isolated local configuration")
    try:
        content = expected.read_text()
        config = yaml.safe_load(content)
        endpoints = config["endpoints"]
        custom = endpoints["custom"]
        endpoint = custom[0]
        valid = (set(endpoints) == {"custom"} and len(custom) == 1
                 and endpoint["name"] == "Ollama" and endpoint["apiKey"] == "ollama"
                 and endpoint["baseURL"] == "http://ollama:11434/v1"
                 and endpoint["models"]["default"] == ["qwen2.5:3b"]
                 and endpoint["models"]["fetch"] is False)
    except (OSError, KeyError, TypeError, IndexError, yaml.YAMLError):
        fail("the isolated LibreChat configuration is invalid")
    if not valid:
        fail("LibreChat must configure only isolated Ollama and qwen2.5:3b")
    for endpoint_url in re.findall(r"https?://[^\s'\"]+", content):
        if urlparse(endpoint_url).hostname not in ALLOWED_HOSTS:
            fail("LibreChat configuration contains a non-local endpoint")


def check_mock_isolation(services, networks):
    """Allow AP's OAuth exchange to reach only the isolated mock endpoint."""
    mock = services.get("mock-integrations", {})
    app = services.get("activepieces-app", {})
    worker = services.get("activepieces-worker", {})
    mock_networks = mock.get("networks", {})
    if set(mock_networks) != {"activepieces_internal", "mock_control"}:
        fail("mock integrations must use only the isolated and host-control networks")
    if mock_networks["activepieces_internal"].get("ipv4_address") != MOCK_IP:
        fail("mock integrations must use the pinned isolated IP")
    if mock_networks["mock_control"].get("aliases") or mock_networks["mock_control"].get("ipv4_address"):
        fail("mock control network cannot expose aliases or a pinned address")
    if app.get("environment", {}).get("AP_SSRF_ALLOW_LIST") != MOCK_IP or worker.get("environment", {}).get("AP_SSRF_ALLOW_LIST") != MOCK_IP:
        fail("Activepieces SSRF allow-list must contain only the mock IP")
    if worker.get("environment", {}).get("AP_NETWORK_MODE") != "STRICT":
        fail("Activepieces worker must use strict network mode")
    configs = networks.get("activepieces_internal", {}).get("ipam", {}).get("config", [])
    if len(configs) != 1 or configs[0].get("subnet") != MOCK_SUBNET:
        fail("mock integrations require the dedicated isolated subnet")
    control = networks.get("mock_control", {})
    if (control.get("internal") or control.get("driver") != "bridge"
            or control.get("driver_opts", {}).get("com.docker.network.bridge.enable_ip_masquerade") != "false"):
        fail("mock host-control network must be a non-masquerading bridge")
    aliases = [alias for network in mock_networks.values() for alias in network.get("aliases", [])]
    if "lightrag" in aliases:
        fail("mock integrations cannot alias the real LightRAG service")


def main():
    global PROJECT
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", choices=["ap-migration-probe", "activepieces-local-e2e"], default=PROJECT)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--allowed-ports", default=",".join(map(str, sorted(DEFAULT_PORTS))))
    parser.add_argument("--local-overlay", help="repository-relative local-only E2E overlay")
    args = parser.parse_args()
    PROJECT = args.project
    env = Path(args.env_file).resolve()
    if not env.is_file() or Path("/tmp") not in env.parents or ROOT in env.parents or env == ROOT / ".env":
        fail("use a separate existing environment file outside the repository")
    allowed = {int(p) for p in args.allowed_ports.split(",") if p}
    if not allowed or allowed.intersection({3080, 5678, 9621}):
        fail("approved host ports include production ports")
    prefix = ["docker", "compose", "-p", PROJECT, "--env-file", str(env),
              "-f", "compose.yaml", "-f", "compose.activepieces.yaml"]
    if PROJECT == "ap-migration-probe":
        prefix.extend(["-f", "tests/e2e/compose.mock.yaml"])
    if args.local_overlay:
        overlay = (ROOT / args.local_overlay).resolve()
        if ROOT / "tests/e2e" not in overlay.parents or not overlay.is_file():
            fail("local overlay must be a file under tests/e2e")
        prefix.extend(["-f", str(overlay)])
    try:
        config = json.loads(run(prefix + ["config", "--format", "json"]))
    except json.JSONDecodeError:
        fail("Compose configuration is not JSON")
    services, networks, volumes = (config.get(k, {}) for k in ("services", "networks", "volumes"))
    if PROJECT == "activepieces-local-e2e":
        check_local_librechat_config(services.get("librechat", {}))
        for caller, target in (("librechat", "ollama"), ("lightrag", "ollama"),
                               ("librechat", "mcp"), ("mcp", "lightrag"),
                               ("activepieces-worker", "lightrag")):
            if caller not in services or target not in services:
                fail("a required local service is missing")
            if not set(services[caller].get("networks", {})) & set(services[target].get("networks", {})):
                fail(f"{caller} cannot resolve {target} on a shared isolated network")
        if services["librechat"].get("environment", {}).get("ENDPOINTS") != "custom,agents":
            fail("LibreChat must expose only the explicitly configured local endpoints")
    required = {"activepieces-app", "activepieces-worker"}
    if PROJECT == "ap-migration-probe":
        required.add("mock-integrations")
    if not required.issubset(services):
        fail("required isolated engine services are missing")
    if PROJECT == "ap-migration-probe":
        check_mock_isolation(services, networks)
    for kind, collection in (("network", networks), ("volume", volumes)):
        for name, settings in collection.items():
            if settings.get("external") or not settings.get("name", name).startswith(PROJECT + "_"):
                fail(f"{kind} {name} is not scoped to the isolated project")
    for service, settings in services.items():
        if service.startswith("activepieces-") and "@sha256:" not in settings.get("image", ""):
            fail(f"{service} image is not digest-pinned")
        if settings.get("network_mode") or settings.get("privileged"):
            fail(f"{service} bypasses isolated networking/security")
        for mount in settings.get("volumes", []):
            if mount.get("type") == "bind":
                check_bind(mount.get("source", "/"), not mount.get("read_only", False))
            elif mount.get("type") == "volume" and mount.get("source") not in volumes:
                fail(f"{service} uses an undeclared volume")
        for port in settings.get("ports", []):
            if port.get("host_ip") not in {"127.0.0.1", "localhost"} or int(port.get("published", 0)) not in allowed:
                fail(f"{service} publishes an unapproved/non-loopback port")
        environment = settings.get("environment") or {}
        for key, value in environment.items():
            if isinstance(value, str) and re.search(r"(URL|BASE|HOST)$", key, re.I):
                if value.startswith(("http://", "https://")) and urlparse(value).hostname not in ALLOWED_HOSTS:
                    fail(f"{service} has a non-local endpoint in {key}")
        if service == "lightrag":
            if environment.get("LLM_BINDING_HOST") != "http://ollama:11434/v1" or environment.get("LLM_MODEL") != "qwen2.5:3b":
                fail("LightRAG generation must use isolated Ollama qwen2.5:3b")
            if environment.get("EMBEDDING_BINDING_HOST") != "http://ollama:11434/v1" or environment.get("EMBEDDING_MODEL") != "bge-m3":
                fail("LightRAG embeddings must use isolated Ollama bge-m3")
        if service == "librechat" and (
                environment.get("OPENAI_REVERSE_PROXY") != "http://ollama:11434/v1"
                or environment.get("OPENAI_MODELS") != "qwen2.5:3b"):
            fail("LibreChat must use isolated Ollama qwen2.5:3b")
    if args.runtime:
        ids = run(prefix + ["ps", "-q"]).split()
        if not ids:
            fail("no containers found for the isolated project")
        inspected = json.loads(run(["docker", "inspect", *ids]))
        approved_networks = {v.get("name", k) for k, v in networks.items()}
        for item in inspected:
            labels = item.get("Config", {}).get("Labels", {}) or {}
            if labels.get("com.docker.compose.project") != PROJECT:
                fail("a container is outside the isolated project")
            for mount in item.get("Mounts", []):
                if mount.get("Type") == "volume" and not mount.get("Name", "").startswith(PROJECT + "_"):
                    fail("a runtime container uses a foreign volume")
                if mount.get("Type") == "bind":
                    check_bind(mount.get("Source", "/"), mount.get("RW", True))
            if not set(item.get("NetworkSettings", {}).get("Networks", {})).issubset(approved_networks):
                fail("a runtime container joined a foreign network")
    print("PASS isolated Compose preflight" + (" (runtime)" if args.runtime else " (config)"))


if __name__ == "__main__":
    main()
