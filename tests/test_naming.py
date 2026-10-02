import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core.naming import Naming  # noqa: E402


class NamingTests(unittest.TestCase):
    def test_default_scheme_is_the_historical_one(self):
        n = Naming("gdc")
        self.assertEqual(n.app("vc-apigw"), "sirosid-gdc-vc-apigw")
        self.assertEqual(n.internal("pdp"), "sirosid-gdc-pdp.internal")
        self.assertEqual(n.host("wallet-frontend"), "sirosid-gdc-wallet-frontend.fly.dev")
        self.assertEqual(n.url("wallet-proxy"), "https://sirosid-gdc-wallet-proxy.fly.dev")
        self.assertEqual(n.network(), "sirosid-gdc")

    def test_matches_fly_common_functions(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import fly_common as fc
        n = Naming("alice")
        for c in ("mongodb", "vc-issuer", "wallet-frontend", "conformance-server"):
            self.assertEqual(n.app(c), fc.app_name("alice", c))
            self.assertEqual(n.url(c), fc.app_url("alice", c))
        self.assertEqual(n.network(), fc.network_name("alice"))

    def test_host_moves_independently_of_app_and_internal(self):
        n = Naming("k7m2qx4d", app_prefix="sid", host_pattern="{env}-{component}.sandbox.example")
        self.assertEqual(n.app("vc-apigw"), "sid-k7m2qx4d-vc-apigw")
        self.assertEqual(n.internal("vc-apigw"), "sid-k7m2qx4d-vc-apigw.internal")
        self.assertEqual(n.host("vc-apigw"), "k7m2qx4d-vc-apigw.sandbox.example")
        self.assertEqual(n.url("vc-apigw"), "https://k7m2qx4d-vc-apigw.sandbox.example")


if __name__ == "__main__":
    unittest.main()
