#!/usr/bin/env python3
"""Import generated AP templates and encrypted test credentials into a local AP project."""
import argparse
import json
import os
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, os.path.dirname(__file__))
from preflight import ROOT  # noqa: E402

GENERATOR = ROOT / "activepieces" / "generate_workflows.py"
CONNECTIONS = (
    ("tavily_bearer_api_key", "Tavily bearer API key", "AP_E2E_TAVILY_TOKEN",
     "@activepieces/piece-http", "0.12.1", "SECRET_TEXT"),
    ("lightrag_api_key", "LightRAG X-API-Key", "AP_E2E_LIGHTRAG_TOKEN",
     "@activepieces/piece-http", "0.12.1", "SECRET_TEXT"),
    ("google_drive_oauth2", "Google Drive OAuth2 connection", None,
     "@activepieces/piece-http-oauth2", "0.3.0", "OAUTH2"),
)
WEBHOOK_TOKENS = {
    "internet-to-lightrag": "E2E_WEBHOOK_TOKEN",
    "google-notebooks-to-lightrag": "E2E_NOTEBOOK_TOKEN",
}
ALLOWED_PORTS = {18080, 8081}
MOCK_HOST = "mock-integrations"
MOCK_PORT = 9621


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


opener = build_opener(NoRedirect())


def validate_api_base(base_url):
    parsed = urlparse(base_url.rstrip("/"))
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port not in ALLOWED_PORTS or parsed.path or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise ValueError("--base-url must be isolated localhost HTTP on port 18080 or 8081")
    return base_url.rstrip("/")


def validate_mock_url(mock_url):
    parsed_mock = urlparse(mock_url)
    if (parsed_mock.scheme != "http" or parsed_mock.hostname != MOCK_HOST
            or parsed_mock.port != MOCK_PORT or parsed_mock.path not in {"", "/"}
            or parsed_mock.query or parsed_mock.fragment or parsed_mock.username or parsed_mock.password):
        raise ValueError("--mock-url must be the isolated mock-integrations service on port 9621")
    return parsed_mock


def oauth_redirect_url(base_url):
    port = urlparse(validate_api_base(base_url)).port
    return f"http://activepieces.localhost:{port}/redirect"


def connections_for_import(local_demo=False):
    return tuple(connection for connection in CONNECTIONS
                 if not local_demo or connection[0] == "lightrag_api_key")


def rewrite_test_endpoints(operation, mock_url, *, local_demo=False):
    """Route imported HTTP actions to mocks, except the opt-in local demo."""
    validate_mock_url(mock_url)
    base = mock_url.rstrip("/")
    url_count = 0

    def visit(value):
        nonlocal url_count
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "url" and isinstance(child, str):
                    parsed = urlparse(child)
                    if parsed.query or parsed.fragment or parsed.username or parsed.password:
                        raise ValueError("HTTP action URL has unsupported query, fragment, or credentials")
                    if (not local_demo and parsed.scheme == "https" and parsed.hostname == "api.tavily.com"
                            and parsed.path == "/search"):
                        path = "/tavily/search"
                        value[key] = base + path
                    elif (not local_demo and parsed.scheme == "https" and parsed.hostname == "www.googleapis.com"
                          and parsed.path.startswith("/drive/v3/files/") and parsed.path.endswith("/export")):
                        value[key] = base + parsed.path
                    elif (local_demo and parsed.scheme == "http" and parsed.hostname == "lightrag"
                          and parsed.port == 9621 and parsed.path == "/documents/text"):
                        pass
                    elif (not local_demo and parsed.scheme == "http" and parsed.hostname == "lightrag"
                          and parsed.port == 9621 and parsed.path == "/documents/text"):
                        value[key] = base + parsed.path
                    else:
                        raise ValueError("Refusing to import an HTTP action with an unapproved endpoint")
                    url_count += 1
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(operation)
    if url_count == 0:
        raise ValueError("No HTTP action endpoints were validated; refusing unsafe test import")
    # Fail closed if a future template adds a URL field outside the expected mock.
    def assert_local(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "url" and isinstance(child, str):
                    parsed = urlparse(child)
                    expected_host = "lightrag" if local_demo else MOCK_HOST
                    if parsed.scheme != "http" or parsed.port != MOCK_PORT or parsed.hostname != expected_host:
                        raise ValueError("An imported HTTP action still targets a non-local endpoint")
                else:
                    assert_local(child)
        elif isinstance(value, list):
            for child in value:
                assert_local(child)

    assert_local(operation)
    return url_count


def request(base, token, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with opener.open(Request(base + path, data=data, headers=headers, method=method), timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as error:
        # Server bodies can contain sensitive request details; callers only
        # receive status codes, never response text or credentials.
        raise RuntimeError(f"Activepieces API returned HTTP {error.code} for {path.split('?')[0]}") from None
    except (URLError, TimeoutError):
        raise RuntimeError(f"Activepieces API request failed for {path.split('?')[0]}") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="isolated local AP gateway URL")
    parser.add_argument("--mock-url", required=True, help="isolated local integration mock URL")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--flow", action="append", choices=(
        "internet-to-lightrag", "google-notebooks-to-lightrag", "product-knowledge-demo"),
        help="flow to import; repeat to select several (default: all three)")
    parser.add_argument("--enable", action="store_true", help="publish and enable imported webhook flows")
    parser.add_argument("--skip-connections", action="store_true",
                        help="import flow shapes only when offline piece validation prevents test connections")
    parser.add_argument("--acceptance-demo-webhook", action="store_true",
                        help="use a temporary webhook trigger for the demo flow during isolated acceptance")
    parser.add_argument("--local-demo", action="store_true",
                        help="keep the demo flow pointed at the real local LightRAG service")
    args = parser.parse_args()

    try:
        base = validate_api_base(args.base_url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    try:
        validate_mock_url(args.mock_url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    token = os.environ.get("AP_ACCESS_TOKEN", "")
    if not token:
        raise SystemExit("Set AP_ACCESS_TOKEN in the environment; it is never read from a file or printed.")

    slugs = args.flow or ["internet-to-lightrag", "google-notebooks-to-lightrag", "product-knowledge-demo"]
    if args.local_demo and slugs != ["product-knowledge-demo"]:
        raise SystemExit("--local-demo is only valid with --flow product-knowledge-demo")
    operations = {}
    for slug in slugs:
        generated = subprocess.run(
            ["python3", str(GENERATOR), "--import-request", slug] +
            (["--acceptance-webhook"] if slug == "product-knowledge-demo" and args.acceptance_demo_webhook else []),
            check=True, capture_output=True, text=True,
        ).stdout
        operation = json.loads(generated)
        try:
            rewrite_test_endpoints(operation, args.mock_url, local_demo=args.local_demo)
        except ValueError as exc:
            raise SystemExit(f"Refusing unsafe isolated flow import for {slug}: {exc}") from None
        token_env = WEBHOOK_TOKENS.get(slug)
        if slug == "product-knowledge-demo" and args.acceptance_demo_webhook:
            token_env = "E2E_WEBHOOK_TOKEN"
        if token_env:
            token_value = os.environ.get(token_env, "")
            if not token_value:
                raise SystemExit(f"Set {token_env} to inject the private isolated-test webhook secret.")
            trigger_input = operation["request"]["trigger"]["settings"]["input"]
            trigger_input["authFields"]["headerValue"] = token_value
        operations[slug] = operation

    if not args.skip_connections:
        connections = connections_for_import(args.local_demo)
        secrets = {external_id: os.environ.get(env_name, "")
                   for external_id, _, env_name, _, _, _ in connections if env_name}
        missing = [item[2] for item in connections if item[2] and not secrets[item[0]]]
        if missing:
            raise SystemExit("Missing isolated test credential environment variables: " + ", ".join(missing))

        for external_id, display_name, _, piece_name, piece_version, connection_type in connections:
            value = {"type": connection_type, "secret_text": secrets[external_id]} if connection_type == "SECRET_TEXT" else {
                "type": "OAUTH2",
                "client_id": "e2e-google-client",
                "client_secret": "e2e-google-client-secret",
                "code": "e2e-google-auth-code",
                "scope": "https://www.googleapis.com/auth/drive.readonly",
                "redirect_url": oauth_redirect_url(base),
                "props": {
                    "authUrl": "http://mock-integrations:9621/oauth/authorize",
                    "tokenUrl": "http://mock-integrations:9621/oauth/token",
                    "scopes": "https://www.googleapis.com/auth/drive.readonly",
                },
            }
            request(base, token, "POST", "/api/v1/app-connections", {
                "externalId": external_id,
                "displayName": display_name,
                "pieceName": piece_name,
                "pieceVersion": piece_version,
                "projectId": args.project_id,
                "type": connection_type,
                "value": value,
            })

    results = {}
    for slug in slugs:
        operation = operations[slug]
        status, flow = request(base, token, "POST", "/api/v1/flows", {
            "displayName": operation["request"]["displayName"],
            "projectId": args.project_id,
        })
        flow_id = flow.get("id")
        if status not in (200, 201) or not flow_id:
            raise RuntimeError(f"Activepieces did not create flow for {slug}")
        request(base, token, "POST", "/api/v1/flows/" + quote(flow_id, safe=""), operation)
        results[slug] = {"flowId": flow_id, "status": "imported-disabled"}
        if args.enable and (slug != "product-knowledge-demo" or args.acceptance_demo_webhook):
            request(base, token, "POST", "/api/v1/flows/" + quote(flow_id, safe=""), {
                "type": "LOCK_AND_PUBLISH", "request": {"status": "ENABLED"},
            })
            results[slug]["status"] = "enabled"
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
