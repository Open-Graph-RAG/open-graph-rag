#!/usr/bin/env python3
"""Generate sanitized Activepieces flow templates from the n8n contracts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "activepieces" / "workflows"
SCHEMA = "27"
HTTP = "@activepieces/piece-http"
HTTP_OAUTH2 = "@activepieces/piece-http-oauth2"
WEBHOOK = "@activepieces/piece-webhook"
TAVILY_CONNECTION = "tavily_bearer_api_key"
GOOGLE_CONNECTION = "google_drive_oauth2"
LIGHTRAG_CONNECTION = "lightrag_api_key"


def code(name: str, label: str, source: str, inputs: dict | None = None, *,
         continue_on_failure: bool = False, on_success: dict | None = None,
         on_failure: dict | None = None) -> dict:
    step = {
        "name": name, "displayName": label, "valid": True,
        "type": "CODE", "lastUpdatedDate": "2026-10-05T00:00:00.000Z",
        "settings": {
            "sourceCode": {"packageJson": "{}", "code": source},
            "input": inputs or {},
        },
    }
    if continue_on_failure:
        step["settings"]["errorHandlingOptions"] = {
            "continueOnFailure": {"value": True},
            "retryOnFailure": {"value": False},
        }
        if on_success:
            step["continueOnFailureBranches"] = {"onSuccess": on_success}
        if on_failure:
            step.setdefault("continueOnFailureBranches", {})["onFailure"] = on_failure
    return step


def piece(name: str, label: str, piece_name: str, version: str, action: str,
          inputs: dict, *, continue_on_failure: bool = False,
          on_success: dict | None = None, on_failure: dict | None = None) -> dict:
    step = {
        "name": name, "displayName": label, "valid": True,
        "type": "PIECE", "lastUpdatedDate": "2026-10-05T00:00:00.000Z",
        "settings": {
            "pieceName": piece_name, "pieceVersion": version,
            "actionName": action, "propertySettings": {}, "input": inputs,
        },
    }
    if continue_on_failure:
        step["settings"]["errorHandlingOptions"] = {
            "continueOnFailure": {"value": True},
            "retryOnFailure": {"value": False},
        }
        step["continueOnFailureBranches"] = {}
        if on_success:
            step["continueOnFailureBranches"]["onSuccess"] = on_success
        if on_failure:
            step["continueOnFailureBranches"]["onFailure"] = on_failure
    return step


def webhook(auth_header_name: str = "X-Ingest-Token", *, authenticated: bool = True) -> dict:
    input = ({
        "authType": "header",
        "authFields": {
            "headerName": auth_header_name,
            "headerValue": "CONFIGURE_INCOMING_TOKEN",
        },
    } if authenticated else {"authType": "none", "authFields": {}})
    return {
        "name": "trigger", "displayName": "Authenticated JSON webhook",
        "valid": True, "type": "PIECE_TRIGGER",
        "lastUpdatedDate": "2026-10-05T00:00:00.000Z",
        "settings": {
            "pieceName": WEBHOOK, "pieceVersion": "0.1.42",
            "triggerName": "catch_webhook", "propertySettings": {},
            "input": input,
        },
    }


def http_step(name: str, label: str, method: str, url: str,
              headers: dict, body: str | dict | None, *, timeout: int = 5,
              query_params: dict | None = None,
              auth_external_id: str | None = None,
              bearer_auth: bool = False,
              continue_on_failure: bool = False, on_success=None, on_failure=None) -> dict:
    inputs = {
        "method": method, "url": url, "headers": headers,
        "queryParams": query_params or {},
        "authType": "BEARER_TOKEN" if bearer_auth else "NONE",
        "body_type": "json" if body is not None else "none",
        "timeout": timeout,
        "failureMode": "retry_none",
    }
    if body is not None:
        inputs["body"] = {"data": body}
    if auth_external_id and bearer_auth:
        inputs["authFields"] = {"token": "{{connections." + auth_external_id + ".secret_text}}"}
    return piece(name, label, HTTP, "0.12.1", "send_request", inputs,
                 continue_on_failure=continue_on_failure,
                 on_success=on_success, on_failure=on_failure)


def oauth_http_step(name: str, label: str, method: str, url: str,
                    headers: dict, query_params: dict, auth_external_id: str, *,
                    timeout: int = 5, continue_on_failure: bool = False,
                    on_success=None, on_failure=None) -> dict:
    inputs = {
        "method": method,
        "url": url,
        "headers": headers,
        "queryParams": query_params,
        "body_type": "none",
        "timeout": timeout,
        "failsafe": False,
        "use_proxy": False,
        "auth": "{{connections." + auth_external_id + "}}",
    }
    return piece(name, label, HTTP_OAUTH2, "0.3.0", "send-oauth2-request", inputs,
                 continue_on_failure=continue_on_failure,
                 on_success=on_success, on_failure=on_failure)


def source(helper_path: str, invocation: str) -> str:
    text = (ROOT / helper_path).read_text()
    # Helpers are CommonJS-free, self-contained JS. Keeping the same source in
    # exports avoids a runtime package dependency in SANDBOX_CODE_ONLY.
    text = text.replace("'use strict';", "")
    return "const module = { exports: {} };\n" + text + "\n" + invocation


def reply_step(name: str, display_name: str, response_ref: str) -> dict:
    return piece(name, display_name, WEBHOOK, "0.1.42", "return_response", {
        "responseType": "json",
        "fields": {
            "status": "{{" + response_ref + "['output'].statusCode}}",
            "body": "{{" + response_ref + "['output'].body}}",
        },
        "respond": "stop",
    })


def public_response(name: str, display_name: str, result_ref: str) -> dict:
    return code(name, display_name,
                "export const code = async (inputs) => {\n" +
                "const result = inputs.result || {}; const {httpStatus, ...body} = result;\n" +
                "return {statusCode: Number(httpStatus) || 502, body};\n};",
                {"result": "{{" + result_ref + "['output']}}"})


def error_result(name: str, code_name: str, message: str, status_code: int) -> dict:
    return code(name, name.replace("_", " ").title(),
                "export const code = async () => ({httpStatus: " + str(status_code) +
                ", status: 'error', error: {code: '" + code_name + "', message: '" + message +
                "'}, queued: 0, alreadyPresent: 0, trackIds: []});")


def response_ending(response_ref: str, suffix: str) -> dict:
    projection = public_response("public_response" + suffix, "Project public response", response_ref)
    projection["nextAction"] = reply_step("return_response" + suffix,
                                         "Return ingestion response", "public_response" + suffix)
    return projection


def method_gate(next_action: dict) -> dict:
    failure = error_result("method_not_allowed", "method_not_allowed",
                           "Only POST requests are allowed.", 405)
    failure["nextAction"] = response_ending("method_not_allowed", "_method")
    return code("validate_method", "Require POST request",
                "export const code = async (inputs) => {\n" +
                "if (String(inputs.method || '').toUpperCase() !== 'POST') throw new Error('method not allowed');\n" +
                "return {allowed: true};\n};",
                {"method": "{{trigger['output'].method}}"},
                continue_on_failure=True, on_success=next_action, on_failure=failure)


def internet_flow() -> dict:
    reject = code("record_failed", "Record safe failed insertion",
                  "export const code = async (inputs) => {\n" +
                  source("activepieces/helpers/internet.js", "return module.exports.insertionOutcome(null, inputs.source, inputs.errorMessage);") +
                  "\n};",
                  {"source": "{{loop_pages['output'].item.file_source}}",
                   "errorMessage": "{{queue_page['error']['message']}}"})
    not_attempted = code("record_not_attempted", "Record insertion skipped at deadline",
                         "export const code = async (inputs) => ({source: inputs.source, status: 'not_attempted'});",
                         {"source": "{{loop_pages['output'].item.file_source}}"})
    accept = code("record_outcome", "Record insertion outcome",
                  "export const code = async (inputs) => {\n" +
                  source("activepieces/helpers/internet.js", "return module.exports.insertionOutcome(inputs.response, inputs.source, inputs.errorMessage);") +
                  "\n};",
                  {"response": "{{queue_page['output']}}",
                   "errorMessage": "{{queue_page['error']['message']}}",
                   "source": "{{loop_pages['output'].item.file_source}}"})
    insert = http_step("queue_page", "Queue page in LightRAG", "POST",
                       "http://lightrag:9621/documents/text",
                       {"Content-Type": "application/json", "X-API-Key": "{{connections." + LIGHTRAG_CONNECTION + ".secret_text}}"},
                       {"text": "{{loop_pages['output'].item.text}}",
                        "file_source": "{{loop_pages['output'].item.file_source}}",
                        "metadata": "{{loop_pages['output'].item.metadata}}"},
                       auth_external_id=LIGHTRAG_CONNECTION,
                       continue_on_failure=True, on_success=accept, on_failure=reject)
    gate = code("deadline_gate", "Enforce 20 second insertion cutoff",
                "export const code = async (inputs) => {\n" +
                source("activepieces/helpers/internet.js", "if (!module.exports.hasInsertionBudget(inputs.requestStart, Date.now())) throw new Error('insertion deadline reached');\nreturn {allowed: true};") +
                "\n};",
                     {"requestStart": "{{trigger['output'].headers['x-activepieces-request-start']}}"},
                continue_on_failure=True, on_success=insert, on_failure=not_attempted)
    loop = {
        "name": "loop_pages", "displayName": "Insert usable pages sequentially",
        "valid": True, "type": "LOOP_ON_ITEMS",
        "lastUpdatedDate": "2026-10-05T00:00:00.000Z",
        "settings": {"items": "{{prepare_pages['output'].documents}}"},
        "firstLoopAction": gate,
    }
    summarize = code("summarize", "Summarize accepted and failed pages",
                     "export const code = async (inputs) => {\n" +
                     source("activepieces/helpers/internet.js", "const iterations = inputs.iterations || [];\nconst outcomes = iterations.map((row) => row.record_outcome?.output ?? row.record_failed?.output ?? row.record_not_attempted?.output ?? {status: 'failed'});\nreturn module.exports.summarizeInsertions(outcomes);") +
                     "\n};", {"iterations": "{{loop_pages['output'].iterations}}"})
    prepare = code("prepare_pages", "Validate search results and deduplicate pages",
                    "export const code = async (inputs) => {\n" +
                    source("activepieces/helpers/internet.js", "const request = module.exports.validatePrompt(inputs.body);\nconst checked = module.exports.validateSearchResponse(inputs.search);\nconst bundle = module.exports.deduplicateResults(checked.results, request.prompt);\nreturn {...module.exports.prepareGraphDocuments(bundle, checked), search: checked};") +
                    "\n};", {"body": "{{trigger['output'].body}}",
                    "search": "{{search_web['output'].body}}"},
                    continue_on_failure=True, on_success=loop)
    search = http_step("search_web", "Search web with Tavily", "POST",
                       "https://api.tavily.com/search",
                       {"Content-Type": "application/json"},
                       {"query": "{{validate_prompt['output'].prompt}}",
                        "max_results": "{{validate_prompt['output'].maxResults}}",
                        "include_raw_content": True}, auth_external_id=TAVILY_CONNECTION,
                       bearer_auth=True,
                       continue_on_failure=True, on_success=prepare)
    validate = code("validate_prompt", "Validate prompt",
                    "export const code = async (inputs) => {\n" +
                    source("activepieces/helpers/internet.js", "return module.exports.validatePrompt(inputs.body);") +
                    "\n};", {"body": "{{trigger['output'].body}}"},
                    continue_on_failure=True, on_success=search)
    invalid_request = error_result("invalid_input", "invalid_input", "Invalid request input.", 400)
    search_failure = error_result("search_failed", "upstream_failure",
                                  "An upstream service failed during ingestion.", 502)
    prepare_failure = error_result("search_response_failed", "upstream_failure",
                                   "An upstream service failed during ingestion.", 502)
    validate["continueOnFailureBranches"]["onFailure"] = invalid_request
    search["continueOnFailureBranches"]["onFailure"] = search_failure
    prepare["continueOnFailureBranches"]["onFailure"] = prepare_failure
    for failure in (invalid_request, search_failure, prepare_failure):
        failure["nextAction"] = response_ending(failure["name"], "_" + failure["name"])
    webhook_trigger = webhook()
    webhook_trigger["nextAction"] = method_gate(validate)
    loop["nextAction"] = summarize
    summarize["nextAction"] = response_ending("summarize", "")
    return {
        "displayName": "Internet prompt to LightRAG",
        "description": "Authenticated Tavily search with bounded sequential LightRAG insertion.",
        "schemaVersion": SCHEMA, "trigger": webhook_trigger, "notes": [],
        "pieces": [WEBHOOK, HTTP],
    }


def google_flow() -> dict:
    trigger = webhook()
    validate = code("validate_request", "Validate Google Doc request",
                    "export const code = async (inputs) => {\n" +
                    source("activepieces/helpers/google-notebooks.js", "return module.exports.validateDocumentRequest(inputs.body);") +
                    "\n};", {"body": "{{trigger['output'].body}}"},
                    continue_on_failure=True)
    # OAuth connection is deliberately represented only as a connection
    # reference; no token material belongs in an exported template.
    export_doc = oauth_http_step("export_google_doc", "Export Google Doc as text", "GET",
                                 "https://www.googleapis.com/drive/v3/files/{{validate_request['output'].driveFileId}}/export",
                                 {"Accept": "text/plain"}, {"mimeType": "text/plain"}, GOOGLE_CONNECTION)
    validated = code("validate_export", "Validate exported text",
                     "export const code = async (inputs) => {\n" +
                     source("activepieces/helpers/google-notebooks.js", "return module.exports.validateExport(inputs.response, inputs.request);") +
                     "\n};", {"response": "{{export_google_doc['output']}}",
                     "request": "{{validate_request['output']}}"},
                     continue_on_failure=True)
    # The 5 second HTTP timeout leaves time for a /sync response before the
    # gateway's 30 second bound.
    invalid_request = error_result("invalid_input", "invalid_input", "Invalid request input.", 400)
    export_failure = error_result("google_export_failed", "upstream_failure",
                                  "An upstream service failed during ingestion.", 502)
    google_upstream_failure = error_result("google_ingestion_failed", "upstream_failure",
                                           "An upstream service failed during ingestion.", 502)
    validated["continueOnFailureBranches"] = {
        "onSuccess": None,
        "onFailure": google_upstream_failure,
    }
    export_doc["settings"]["errorHandlingOptions"] = {
        "continueOnFailure": {"value": True}, "retryOnFailure": {"value": False},
    }
    export_doc["continueOnFailureBranches"] = {
        "onSuccess": validated, "onFailure": export_failure,
    }
    validate["continueOnFailureBranches"] = {
        "onSuccess": export_doc, "onFailure": invalid_request,
    }
    insert_success = code("record_google_success", "Record LightRAG acceptance",
                          "export const code = async (inputs) => {\n" +
                          source("activepieces/helpers/google-notebooks.js", "return module.exports.summarizeIngestion(inputs.response, inputs.source, inputs.errorMessage);") +
                          "\n};", {"response": "{{queue_google_doc['output']}}",
                                "source": "{{validate_request['output'].file_source}}"})
    insert_failure = code("record_google_failure", "Record safe LightRAG failure",
                           "export const code = async (inputs) => {\n" +
                           source("activepieces/helpers/google-notebooks.js", "return module.exports.summarizeIngestion(null, inputs.source, inputs.errorMessage);") +
                           "\n};", {"source": "{{validate_export['output'].file_source}}",
                                    "errorMessage": "{{queue_google_doc['error']['message']}}"})
    insert = http_step("queue_google_doc", "Queue exported text in LightRAG", "POST",
                       "http://lightrag:9621/documents/text",
                       {"Content-Type": "application/json", "X-API-Key": "{{connections." + LIGHTRAG_CONNECTION + ".secret_text}}"},
                       {"text": "{{validate_export['output'].text}}",
                        "file_source": "{{validate_export['output'].file_source}}"},
                       auth_external_id=LIGHTRAG_CONNECTION,
                       continue_on_failure=True, on_success=insert_success,
                       on_failure=insert_failure)
    deadline_failure = code("record_google_deadline", "Record Google insertion deadline",
                            "export const code = async () => ({httpStatus: 502, status: 'error', error: {code: 'upstream_failure', message: 'An upstream service failed during ingestion.'}, queued: 0, alreadyPresent: 0, trackIds: []});")
    deadline = code("google_deadline_gate", "Enforce 20 second insertion cutoff",
                     "export const code = async (inputs) => {\n" +
                     source("activepieces/helpers/internet.js", "if (!module.exports.hasInsertionBudget(inputs.requestStart, Date.now())) throw new Error('insertion deadline reached');\nreturn {allowed: true};") +
                     "\n};", {"requestStart": "{{trigger['output'].headers['x-activepieces-request-start']}}"},
                     continue_on_failure=True, on_success=insert, on_failure=deadline_failure)
    # Each continue-on-failure branch returns through the same response piece.
    # Keeping the projection at the boundary prevents internal httpStatus from
    # becoming part of the public JSON body.
    for result_step in (insert_success, insert_failure, deadline_failure,
                        invalid_request, export_failure, google_upstream_failure):
        projected = response_ending(result_step["name"], "_" + result_step["name"])
        result_step["nextAction"] = projected
    validated["continueOnFailureBranches"]["onSuccess"] = deadline
    trigger["nextAction"] = method_gate(validate)
    return {"displayName": "NotebookLM Google Doc to LightRAG",
            "description": "Exports a Google Doc as plain text and queues it in LightRAG.",
            "schemaVersion": SCHEMA, "trigger": trigger, "notes": [],
            "pieces": [WEBHOOK, HTTP, HTTP_OAUTH2]}


def demo_flow(*, acceptance_webhook: bool = False) -> dict:
    dataset = json.loads((ROOT / "sample-data/product-knowledge-demo/dataset.json").read_text())
    docs_literal = json.dumps(dataset, ensure_ascii=False, separators=(",", ":"))
    prep = code("prepare_demo", "Prepare fictional demo documents",
                "export const code = async () => {\n" +
                source("activepieces/helpers/product-knowledge-demo.js",
                       f"return module.exports.prepareDemoDocuments({docs_literal});") +
                "\n};")
    failed = code("record_demo_failure", "Record safe demo failure",
                  "export const code = async (inputs) => {\n" +
                  source("activepieces/helpers/product-knowledge-demo.js", "return module.exports.insertionOutcome(null, inputs.source, inputs.errorMessage);") +
                  "\n};",
                  {"source": "{{loop_demo['output'].item.file_source}}",
                   "errorMessage": "{{queue_demo_doc['error']['message']}}"})
    success = code("record_demo_outcome", "Record demo insertion outcome",
                   "export const code = async (inputs) => {\n" +
                   source("activepieces/helpers/product-knowledge-demo.js", "return module.exports.insertionOutcome(inputs.response, inputs.source, inputs.errorMessage);") +
                   "\n};",
                   {"response": "{{queue_demo_doc['output']}}",
                    "errorMessage": "{{queue_demo_doc['error']['message']}}",
                    "source": "{{loop_demo['output'].item.file_source}}"})
    insert = http_step("queue_demo_doc", "Queue demo document in LightRAG", "POST",
                       "http://lightrag:9621/documents/text",
                       {"Content-Type": "application/json", "X-API-Key": "{{connections." + LIGHTRAG_CONNECTION + ".secret_text}}"},
                       {"text": "{{loop_demo['output'].item.text}}",
                        "file_source": "{{loop_demo['output'].item.file_source}}"},
                       auth_external_id=LIGHTRAG_CONNECTION,
                       continue_on_failure=True, on_success=success, on_failure=failed)
    loop = {"name": "loop_demo", "displayName": "Insert demo documents sequentially",
            "valid": True, "type": "LOOP_ON_ITEMS",
            "lastUpdatedDate": "2026-10-05T00:00:00.000Z",
            "settings": {"items": "{{prepare_demo['output']}}"},
            "firstLoopAction": insert}
    summary = code("summarize_demo", "Summarize demo insertion", "export const code = async (inputs) => {\n" +
                   source("activepieces/helpers/product-knowledge-demo.js",
                          "const iterations = inputs.iterations || [];\nreturn module.exports.summarizeDemoInsertions(iterations.map((row) => row.record_demo_outcome?.output ?? row.record_demo_failure?.output ?? {status: 'failed'}));") +
                   "\n};", {"iterations": "{{loop_demo['output'].iterations}}"})
    prep["nextAction"] = loop
    loop["nextAction"] = summary
    if acceptance_webhook:
        summary["nextAction"] = response_ending("summarize_demo", "_demo")
    trigger = ({**webhook(), "nextAction": prep} if acceptance_webhook else {
        "name": "trigger", "displayName": "Manual trigger", "valid": True,
        "type": "EMPTY", "lastUpdatedDate": "2026-10-05T00:00:00.000Z", "settings": {},
        "nextAction": prep,
    })
    if acceptance_webhook:
        trigger["displayName"] = "Temporary acceptance webhook"
    return {"displayName": "Fictional product knowledge demo to LightRAG",
            "description": "Manually queues the embedded fictional product knowledge dataset.",
            "schemaVersion": SCHEMA, "trigger": trigger, "notes": [],
            "pieces": [HTTP, *([WEBHOOK] if acceptance_webhook else [])]}


def flows(*, acceptance_webhook: bool = False) -> dict[str, dict]:
    return {"internet-to-lightrag": internet_flow(),
            "google-notebooks-to-lightrag": google_flow(),
            "product-knowledge-demo": demo_flow(acceptance_webhook=acceptance_webhook)}


def template(flow: dict) -> dict:
    # FlowVersionTemplate fields only; omit ids, state, credentials, and runtime state.
    return {"displayName": flow["displayName"], "description": flow["description"],
            "valid": False, "schemaVersion": SCHEMA, "trigger": flow["trigger"],
            "agentIds": [], "connectionIds": [], "notes": []}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if checked-in exports drift")
    parser.add_argument("--import-request", choices=list(flows()), help="print reusable IMPORT_FLOW operation request")
    parser.add_argument("--acceptance-webhook", action="store_true",
                        help="temporarily expose the demo flow as a webhook for isolated acceptance")
    args = parser.parse_args()
    built = flows(acceptance_webhook=args.acceptance_webhook)
    if args.import_request:
        flow = template(built[args.import_request])
        print(json.dumps({"type": "IMPORT_FLOW", "request": {
            "displayName": flow["displayName"], "schemaVersion": flow["schemaVersion"],
            "trigger": flow["trigger"], "notes": flow["notes"],
        }}, indent=2, ensure_ascii=False))
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for slug, flow in built.items():
        path = OUT / f"{slug}.json"
        artifact = {"schemaVersion": SCHEMA, "flows": [template(flow)]}
        expected = json.dumps(artifact, indent=2, ensure_ascii=False) + "\n"
        if args.check:
            if not path.exists() or path.read_text() != expected:
                print(f"stale generated flow: {path.relative_to(ROOT)}")
                return 1
        else:
            path.write_text(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
