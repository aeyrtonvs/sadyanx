"""TLS certificates for the harness's own HTTPS calls."""

import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sadyanxlib import net  # noqa: E402

NONE = SimpleNamespace(cafile=None, capath=None)
MACOS_BUNDLE = Path("/etc/ssl/cert.pem")


class TestCaSource(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_python_own_certificates_win(self):
        cafile = self.tmp / "cert.pem"
        cafile.write_text("x")
        bundle = self.tmp / "system.pem"
        bundle.write_text("x")
        self.assertEqual(net.ca_source(SimpleNamespace(cafile=str(cafile), capath=None), [str(bundle)]),
                         net.PYTHON_DEFAULT)

    def test_python_capath_with_certificates_counts(self):
        (self.tmp / "certs").mkdir()
        (self.tmp / "certs" / "abcd1234.0").write_text("x")
        self.assertEqual(net.ca_source(SimpleNamespace(cafile=None, capath=str(self.tmp / "certs")), []),
                         net.PYTHON_DEFAULT)

    def test_empty_capath_does_not_count(self):
        (self.tmp / "certs").mkdir()
        bundle = self.tmp / "system.pem"
        bundle.write_text("x")
        self.assertEqual(net.ca_source(SimpleNamespace(cafile=None, capath=str(self.tmp / "certs")), [str(bundle)]),
                         str(bundle))

    def test_falls_back_to_the_first_system_bundle_that_exists(self):
        bundle = self.tmp / "system.pem"
        bundle.write_text("x")
        self.assertEqual(net.ca_source(NONE, [str(self.tmp / "missing.pem"), str(bundle)]), str(bundle))

    def test_no_certificates_anywhere(self):
        self.assertIsNone(net.ca_source(NONE, [str(self.tmp / "missing.pem")]))


class TestContext(unittest.TestCase):
    def assert_verifies(self, ctx: ssl.SSLContext):
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_without_certificates_it_still_verifies(self):
        # Fail closed: no bundle means calls fail, never that verification is skipped.
        self.assert_verifies(net.context(NONE, []))

    @unittest.skipUnless(MACOS_BUNDLE.is_file(), "no /etc/ssl/cert.pem on this machine")
    def test_system_bundle_is_loaded_and_verifies(self):
        ctx = net.context(NONE, [str(MACOS_BUNDLE)])
        self.assert_verifies(ctx)
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 0)

    def test_a_bad_bundle_is_an_error_not_a_silent_pass(self):
        bundle = Path(tempfile.mkdtemp()) / "broken.pem"
        bundle.write_text("not a certificate")
        with self.assertRaises(ssl.SSLError):
            net.context(NONE, [str(bundle)])


if __name__ == "__main__":
    unittest.main()
