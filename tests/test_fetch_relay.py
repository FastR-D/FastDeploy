import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('fetch_relay', Path(__file__).resolve().parents[1] / 'scripts/fetch_relay.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class RelayTests(unittest.TestCase):
    allowed = ['https://example.org/feed.xml']

    def test_only_allowlisted_https_endpoints(self):
        self.assertEqual(relay.validate_url(self.allowed[0], self.allowed, False), self.allowed[0])
        for url in ['http://example.org/feed.xml', 'file:///etc/passwd', 'https://example.org/private', 'https://attacker.test/feed.xml', 'https://user:password@example.org/feed.xml', 'https://example.org:8443/feed.xml', 'https://example.org/feed.xml#fragment']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                relay.validate_url(url, self.allowed, False)

    def test_private_dns_answers_are_rejected(self):
        for address in ['127.0.0.1', '169.254.169.254', '10.0.0.1', '::1']:
            with patch.object(relay.socket, 'getaddrinfo', return_value=[(None,None,None,None,(address,443))]):
                with self.assertRaises(ValueError):
                    relay.validate_url(self.allowed[0], self.allowed)

    def test_redirect_cannot_escape_allowlist(self):
        with self.assertRaises(ValueError):
            relay.SafeRedirect(self.allowed).redirect_request(None, None, 302, '', {}, 'https://example.net/secret')
