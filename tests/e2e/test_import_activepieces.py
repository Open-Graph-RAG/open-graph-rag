import unittest

from import_activepieces import connections_for_import, oauth_redirect_url, rewrite_test_endpoints, validate_api_base


class ImportEndpointRewriteTests(unittest.TestCase):
    def test_local_demo_needs_only_the_light_rag_connection(self):
        self.assertEqual([row[0] for row in connections_for_import(local_demo=True)],
                         ["lightrag_api_key"])

    def test_api_base_must_be_local_and_cannot_redirect_with_credentials(self):
        self.assertEqual(validate_api_base("http://127.0.0.1:18080/"), "http://127.0.0.1:18080")
        self.assertEqual(oauth_redirect_url("http://127.0.0.1:8081"),
                         "http://activepieces.localhost:8081/redirect")
        for url in (
            "http://user:pass@127.0.0.1:18080",
            "http://127.0.0.1:18080/?next=https://example.com",
            "http://127.0.0.1:18080/#other",
            "https://127.0.0.1:18080",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_api_base(url)

    def test_rewrites_every_http_endpoint_to_exact_mock_service(self):
        operation = {"urls": [
            {"settings": {"input": {"url": "https://api.tavily.com/search"}}},
            {"settings": {"input": {
                "url": "https://www.googleapis.com/drive/v3/files/{{id}}/export",
            }}},
            {"settings": {"input": {"url": "http://lightrag:9621/documents/text"}}},
        ]}

        count = rewrite_test_endpoints(operation, "http://mock-integrations:9621")

        self.assertEqual(count, 3)
        self.assertEqual(operation["urls"][0]["settings"]["input"]["url"],
                         "http://mock-integrations:9621/tavily/search")
        self.assertEqual(operation["urls"][1]["settings"]["input"]["url"],
                         "http://mock-integrations:9621/drive/v3/files/{{id}}/export")
        self.assertEqual(operation["urls"][2]["settings"]["input"]["url"],
                         "http://mock-integrations:9621/documents/text")

    def test_only_explicit_local_demo_preserves_light_rag(self):
        operation = {"settings": {"input": {"url": "http://lightrag:9621/documents/text"}}}

        count = rewrite_test_endpoints(operation, "http://mock-integrations:9621", local_demo=True)

        self.assertEqual(count, 1)
        self.assertEqual(operation["settings"]["input"]["url"],
                         "http://lightrag:9621/documents/text")

    def test_local_demo_rejects_non_lightrag_endpoint(self):
        operation = {"settings": {"input": {"url": "https://api.tavily.com/search"}}}

        with self.assertRaisesRegex(ValueError, "unapproved endpoint"):
            rewrite_test_endpoints(operation, "http://mock-integrations:9621", local_demo=True)

    def test_rejects_non_local_mock_url(self):
        with self.assertRaises(ValueError):
            rewrite_test_endpoints({}, "http://127.0.0.1:19621")

    def test_rejects_unapproved_endpoint_before_import(self):
        operation = {"settings": {"input": {"url": "https://example.com/collect"}}}

        with self.assertRaisesRegex(ValueError, "unapproved endpoint"):
            rewrite_test_endpoints(operation, "http://mock-integrations:9621")


if __name__ == "__main__":
    unittest.main()
