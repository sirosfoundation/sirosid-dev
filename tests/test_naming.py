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


    def test_addr_in_the_apps_layout_is_the_internal_name_and_image_port(self):
        n = Naming("gdc")
        self.assertEqual(n.addr("pdp"), "sirosid-gdc-pdp.internal:8080")
        self.assertEqual(n.addr("vc-issuer"), "sirosid-gdc-vc-issuer.internal:8081")
        self.assertEqual(n.addr("vc-issuer", "grpc"), "sirosid-gdc-vc-issuer.internal:8090")
        self.assertEqual(n.addr("wallet-backend", "admin"), "sirosid-gdc-wallet-backend.internal:8081")
        self.assertEqual(n.addr("mongodb"), "sirosid-gdc-mongodb.internal:27017")

    def test_single_machine_layout_is_localhost_with_unique_ports(self):
        from sirosid_core.naming import SINGLE_MACHINE_PORTS
        n = Naming("t1", app_prefix="sid", host_pattern="{env}-{component}.example", layout="single-machine")
        self.assertTrue(n.single_machine)
        self.assertEqual(n.machine_app(), "sid-t1")
        self.assertEqual(n.addr("pdp"), "127.0.0.1:8104")
        self.assertEqual(n.addr("vc-registry", "grpc"), "127.0.0.1:8190")
        ports = [p for kinds in SINGLE_MACHINE_PORTS.values() for p in kinds.values()]
        self.assertEqual(len(ports), len(set(ports)), "two components would bind the same port")

    def test_layout_round_trips_and_old_rows_mean_apps(self):
        n = Naming("t1", app_prefix="sid", host_pattern="{env}-{component}.example", layout="single-machine")
        self.assertEqual(Naming.from_dict(n.to_dict()), n)
        old = {"env": "t1", "app_prefix": "sid", "host_pattern": "{app}.fly.dev"}
        self.assertEqual(Naming.from_dict(old).layout, "apps")
        self.assertNotIn("layout", Naming.from_dict(old).to_dict())
        with self.assertRaises(ValueError):
            Naming("x", layout="nope")


if __name__ == "__main__":
    unittest.main()
