import unittest

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from services.ontology.gateway import create_gateway_app


class RetrievalGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.forwarded = []

        async def upstream_handler(request):
            self.forwarded.append(request)
            return httpx.Response(200, json={"status": "success"})

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
        self.app = create_gateway_app("http://lightrag:9621", "server-secret", client=self.upstream)
        self.client = httpx.AsyncClient(transport=ASGITransport(app=self.app), base_url="http://gateway")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()

    async def test_forwards_only_allowlisted_retrieval_with_server_key(self):
        response = await self.client.post(
            "/query/data?workspace=acme",
            json={"query": "find decisions"},
            headers={"X-API-Key": "server-secret"},
        )
        self.assertEqual(response.status_code, 200)
        upstream = self.forwarded[0]
        self.assertEqual(upstream.url.path, "/query/data")
        self.assertEqual(upstream.url.params["workspace"], "acme")
        self.assertEqual(upstream.headers["X-API-Key"], "server-secret")

    async def test_rejects_invalid_credentials_and_all_unlisted_writes(self):
        invalid = await self.client.post("/query/data", headers={"X-API-Key": "wrong"}, json={})
        write = await self.client.post(
            "/graph/entity/create", headers={"X-API-Key": "server-secret"}, json={}
        )
        ingest = await self.client.post(
            "/documents/upload", headers={"X-API-Key": "server-secret"}, json={}
        )
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(write.status_code, 404)
        self.assertEqual(ingest.status_code, 404)
        self.assertEqual(self.forwarded, [])

    async def test_graph_read_route_is_available(self):
        response = await self.client.get(
            "/graphs", params={"label": "ogr_fact_abc"}, headers={"X-API-Key": "server-secret"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.forwarded[0].method, "GET")
        self.assertEqual(self.forwarded[0].url.params["label"], "ogr_fact_abc")

    async def test_rejects_oversized_body_before_forwarding(self):
        app = create_gateway_app(
            "http://lightrag:9621", "server-secret", client=self.upstream,
            max_request_bytes=16,
        )
        client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway")
        try:
            response = await client.post(
                "/query/data", content=b"x" * 17,
                headers={"X-API-Key": "server-secret"},
            )
        finally:
            await client.aclose()
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.forwarded, [])


if __name__ == "__main__":
    unittest.main()
