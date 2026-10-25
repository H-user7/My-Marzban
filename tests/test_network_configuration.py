"""Run with: python -m unittest discover -s tests -v

Keep application bootstrap, Xray and public IP detection out of these tests.
Startup configuration, Pydantic user models and subscription tokens run as-is.
"""

import importlib
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import runpy
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def load_config(self, environment):
        from decouple import Config, RepositoryEmpty

        with patch.dict(os.environ, environment, clear=True), patch("dotenv.load_dotenv"), \
                patch("decouple.config", Config(RepositoryEmpty())):
            return runpy.run_path(str(ROOT / "config.py"))

    def test_default_listener_and_port(self):
        config = self.load_config({})
        self.assertEqual(config["UVICORN_HOST"], "0.0.0.0")
        self.assertEqual(config["UVICORN_PORT"], 8000)
        self.assertIsNone(config["UVICORN_SSL_CERTFILE"])
        self.assertIsNone(config["UVICORN_SSL_KEYFILE"])

    def test_prefix_normalization_and_custom_port_from_environment(self):
        config = self.load_config({"XRAY_SUBSCRIPTION_URL_PREFIX": " https://example.com/panel/// \n",
                                   "UVICORN_PORT": "9000"})
        self.assertEqual(config["XRAY_SUBSCRIPTION_URL_PREFIX"], "https://example.com/panel")
        self.assertEqual(config["UVICORN_PORT"], 9000)


class NetworkConfigurationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from decouple import Config, RepositoryEmpty

        # Import the real modules without starting the scheduler, querying the
        # database, detecting public IPs or requiring an installed Xray binary.
        app = ModuleType("app")
        app.__path__ = [str(ROOT / "app")]
        app.app = Mock()
        app.logger = logging.getLogger("uvicorn.error")
        app.scheduler = Mock()
        app.xray = SimpleNamespace(config=SimpleNamespace(
            inbounds_by_protocol={}, inbounds_by_tag={},
        ))
        admin = ModuleType("app.models.admin")
        admin.Admin = BaseModel
        cls.share = ModuleType("app.subscription.share")
        cls.share.SERVER_IP = "203.0.113.10"
        cls.share.generate_v2ray_links = Mock(return_value=[])
        modules = {"app": app, "app.models.admin": admin,
                   "app.subscription.share": cls.share}
        cls.module_patch = patch.dict(sys.modules, modules)
        cls.module_patch.start()
        cls.addClassCleanup(cls.module_patch.stop)
        # Ignore host environment variables and local .env files as well.
        with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv"), \
                patch("decouple.config", Config(RepositoryEmpty())):
            cls.main = importlib.import_module("main")
            cls.subscription = importlib.import_module("app.utils.subscription")

    def setUp(self):
        self.startup_patch = patch.multiple(
            self.main, UVICORN_HOST="0.0.0.0", UVICORN_PORT=8000,
            UVICORN_UDS=None, UVICORN_SSL_CERTFILE=None,
            UVICORN_SSL_KEYFILE=None, UVICORN_SSL_CA_TYPE="public", DEBUG=False,
        )
        self.startup_patch.start()
        self.addCleanup(self.startup_patch.stop)
        self.subscription_patch = patch.multiple(
            self.subscription, XRAY_SUBSCRIPTION_URL_PREFIX="",
            XRAY_SUBSCRIPTION_PATH="sub", UVICORN_HOST="0.0.0.0",
            UVICORN_PORT=8000, UVICORN_SSL_CERTFILE=None, UVICORN_SSL_KEYFILE=None,
        )
        self.subscription_patch.start()
        self.addCleanup(self.subscription_patch.stop)


class StartupTests(NetworkConfigurationTestCase):
    def test_http_respects_host_and_custom_port(self):
        for host in ("0.0.0.0", "127.0.0.1", "localhost", "192.0.2.10", "::", "::1", "2001:db8::10"):
            with self.subTest(host=host), patch.multiple(self.main, UVICORN_HOST=host, UVICORN_PORT=9000):
                with self.assertLogs("uvicorn.error", level="WARNING") as logs:
                    self.assertEqual(self.main.get_bind_args(), {"host": host, "port": 9000})
                self.assertIn("HTTP does not encrypt credentials or subscription links", " ".join(logs.output))

    def test_http_unix_socket(self):
        with patch.object(self.main, "UVICORN_UDS", "/tmp/marzban.sock"):
            with self.assertLogs("uvicorn.error", level="WARNING"):
                self.assertEqual(self.main.get_bind_args(), {"uds": "/tmp/marzban.sock"})

    def test_incomplete_ssl_ignores_both_files(self):
        for cert, key in (("cert.pem", None), (None, "key.pem"), ("cert.pem", ""), ("", "key.pem")):
            with self.subTest(cert=cert, key=key), patch.multiple(
                self.main, UVICORN_SSL_CERTFILE=cert, UVICORN_SSL_KEYFILE=key,
            ), patch.object(self.main, "validate_cert_and_key") as validate:
                with self.assertLogs("uvicorn.error", level="WARNING") as logs:
                    self.assertEqual(self.main.get_bind_args(), {"host": "0.0.0.0", "port": 8000})
                validate.assert_not_called()
                self.assertIn("incomplete SSL configuration", " ".join(logs.output))
                self.assertIn("HTTP does not encrypt", " ".join(logs.output))

    def test_https_validates_both_files_and_preserves_binding(self):
        for uds in (None, "/tmp/marzban.sock"):
            for ca_type in ("public", "private", "invalid"):
                with self.subTest(uds=uds, ca_type=ca_type), patch.multiple(
                    self.main, UVICORN_HOST="::1", UVICORN_PORT=8443,
                    UVICORN_UDS=uds, UVICORN_SSL_CERTFILE="cert.pem",
                    UVICORN_SSL_KEYFILE="key.pem", UVICORN_SSL_CA_TYPE=ca_type,
                ), patch.object(self.main, "validate_cert_and_key") as validate:
                    expected = {"uds": uds} if uds else {"host": "::1", "port": 8443}
                    expected.update(ssl_certfile="cert.pem", ssl_keyfile="key.pem")
                    self.assertEqual(self.main.get_bind_args(), expected)
                    validate.assert_called_once_with(
                        "cert.pem", "key.pem", "private" if ca_type == "private" else "public",
                    )

    def test_certificate_validation_failure_is_not_downgraded_to_http(self):
        with patch.multiple(self.main, UVICORN_SSL_CERTFILE="cert.pem", UVICORN_SSL_KEYFILE="key.pem"):
            with patch.object(self.main, "validate_cert_and_key", side_effect=ValueError("invalid certificate")):
                with self.assertRaisesRegex(ValueError, "invalid certificate"):
                    self.main.get_bind_args()

    def test_public_certificate_validation_rejects_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing.pem")
            with self.assertRaisesRegex(ValueError, "certificate file.*does not exist"):
                self.main.validate_cert_and_key(missing, missing, "public")
            cert = Path(directory) / "cert.pem"
            cert.touch()
            with self.assertRaisesRegex(ValueError, "key file.*does not exist"):
                self.main.validate_cert_and_key(str(cert), missing, "public")

    def test_public_certificate_validation_still_rejects_self_signed_certificates(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
                .sign(key, hashes.SHA256()))
        with tempfile.TemporaryDirectory() as directory:
            cert_path = Path(directory) / "cert.pem"
            key_path = Path(directory) / "key.pem"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                  serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
            with patch.multiple(self.main, UVICORN_SSL_CERTFILE=str(cert_path), UVICORN_SSL_KEYFILE=str(key_path)):
                with self.assertRaisesRegex(ValueError, "self-signed"):
                    self.main.get_bind_args()

    def test_debug_keeps_reload_binding_and_configured_port(self):
        for uds in (None, "/tmp/marzban.sock"):
            with self.subTest(uds=uds), patch.multiple(
                self.main, DEBUG=True, UVICORN_HOST="localhost", UVICORN_PORT=9000, UVICORN_UDS=uds,
            ), self.assertLogs("uvicorn.error", level="WARNING"):
                self.assertEqual(self.main.get_bind_args(), {"host": "0.0.0.0", "port": 9000, "uds": None})

    def test_entrypoint_preserves_single_worker_reload_and_log_level(self):
        config = importlib.import_module("config")
        for debug in (False, True):
            with self.subTest(debug=debug), patch.multiple(
                config, DEBUG=debug, UVICORN_HOST="127.0.0.1", UVICORN_PORT=9000,
                UVICORN_UDS=None, UVICORN_SSL_CERTFILE=None, UVICORN_SSL_KEYFILE=None,
            ), patch("uvicorn.run") as run, self.assertLogs("uvicorn.error", level="WARNING"):
                runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
                expected = dict(host="0.0.0.0" if debug else "127.0.0.1", port=9000,
                                workers=1, reload=debug, log_level=logging.DEBUG if debug else logging.INFO)
                if debug:
                    expected["uds"] = None
                run.assert_called_once_with("main:app", **expected)


class SubscriptionURLTests(NetworkConfigurationTestCase):
    def test_explicit_prefixes_override_automatic_settings(self):
        for prefix in ("https://example.com", "http://192.0.2.1:9000", "https://example.com/panel",
                       "http://[2001:db8::1]:9000/panel"):
            with self.subTest(prefix=prefix), patch.multiple(
                self.subscription, XRAY_SUBSCRIPTION_URL_PREFIX=f" \t{prefix}///\n",
                UVICORN_SSL_CERTFILE="cert.pem", UVICORN_SSL_KEYFILE="key.pem",
            ):
                self.assertEqual(self.subscription.get_subscription_url("token"), f"{prefix}/sub/token")

    def test_whitespace_only_prefix_uses_ip_fallback(self):
        with patch.object(self.subscription, "XRAY_SUBSCRIPTION_URL_PREFIX", " \t\n"):
            self.assertEqual(self.subscription.get_subscription_url("token"), "http://203.0.113.10:8000/sub/token")

    def test_wildcards_share_one_random_salt_within_each_url(self):
        with patch.object(self.subscription, "XRAY_SUBSCRIPTION_URL_PREFIX", " https://*.example.com/*/// "):
            with patch.object(self.subscription.secrets, "token_hex", side_effect=["a" * 16, "b" * 16]) as salt:
                for letter in ("a", "b"):
                    self.assertEqual(self.subscription.get_subscription_url("token"),
                                     f"https://{letter * 16}.example.com/{letter * 16}/sub/token")
                self.assertEqual(salt.call_args_list, [call(8), call(8)])

    def test_explicit_ips_take_priority_over_detected_public_ip(self):
        for host, authority in (("192.0.2.1", "192.0.2.1"), ("127.0.0.1", "127.0.0.1"),
                                ("2001:db8::10", "[2001:db8::10]"), ("[2001:db8::10]", "[2001:db8::10]"),
                                ("::1", "[::1]")):
            with self.subTest(host=host), patch.object(self.subscription, "UVICORN_HOST", host):
                self.assertEqual(self.subscription.get_subscription_url("token"), f"http://{authority}:8000/sub/token")

    def test_wildcard_and_hostname_listeners_use_detected_ip(self):
        for host in ("0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0", "localhost", "panel.example.com"):
            with self.subTest(host=host), patch.object(self.subscription, "UVICORN_HOST", host):
                self.assertEqual(self.subscription.get_subscription_url("token"), "http://203.0.113.10:8000/sub/token")

    def test_detected_ipv6_is_bracketed_once(self):
        for detected in ("2001:db8::10", "[2001:db8::10]"):
            with self.subTest(detected=detected), patch.object(self.share, "SERVER_IP", detected):
                self.assertEqual(self.subscription.get_subscription_url("token"), "http://[2001:db8::10]:8000/sub/token")

    def test_detection_failure_never_advertises_wildcard_or_invalid_ip(self):
        for detected in (None, "", "invalid", "0.0.0.0", "::", "127.0.0.1"):
            with self.subTest(detected=detected), patch.object(self.share, "SERVER_IP", detected):
                self.assertEqual(self.subscription.get_subscription_url("token"), "http://127.0.0.1:8000/sub/token")

    def test_custom_port_and_subscription_path(self):
        with patch.multiple(self.subscription, UVICORN_PORT=9000, XRAY_SUBSCRIPTION_PATH="custom/sub"):
            self.assertEqual(self.subscription.get_subscription_url("unchanged-token"),
                             "http://203.0.113.10:9000/custom/sub/unchanged-token")

    def test_https_requires_both_ssl_files(self):
        for cert, key, scheme in ((None, None, "http"), ("cert.pem", None, "http"),
                                  (None, "key.pem", "http"), ("cert.pem", "", "http"),
                                  ("cert.pem", "key.pem", "https")):
            with self.subTest(cert=cert, key=key), patch.multiple(
                self.subscription, UVICORN_SSL_CERTFILE=cert, UVICORN_SSL_KEYFILE=key,
            ):
                self.assertEqual(self.subscription.get_subscription_url("token"), f"{scheme}://203.0.113.10:8000/sub/token")


class UserResponseTests(NetworkConfigurationTestCase):
    def setUp(self):
        super().setUp()
        self.users = importlib.import_module("app.models.user")
        self.jwt = importlib.import_module("app.utils.jwt")
        secret = patch.object(self.jwt, "get_secret_key", return_value="test-secret")
        secret.start()
        self.addCleanup(secret.stop)
        self.payload = dict(username="test_user", status="active", used_traffic=0,
                            created_at="2026-01-01T00:00:00", proxies={"vmess": {}}, links=["vmess://existing"])

    def test_dict_and_orm_responses_use_shared_url_and_original_token_format(self):
        for from_attributes in (False, True):
            with self.subTest(from_attributes=from_attributes), patch.multiple(
                self.subscription, XRAY_SUBSCRIPTION_URL_PREFIX=" https://example.com/panel/ ",
                XRAY_SUBSCRIPTION_PATH="custom/sub",
            ):
                source = SimpleNamespace(**self.payload) if from_attributes else self.payload
                user = self.users.UserResponse.model_validate(source)
                self.assertTrue(user.subscription_url.startswith("https://example.com/panel/custom/sub/"))
                token = user.subscription_url.rsplit("/", 1)[1]
                self.assertEqual(self.jwt.get_subscription_payload(token)["username"], "test_user")
                self.assertEqual(user.links, ["vmess://existing"])

    def test_response_without_prefix_has_absolute_ip_url(self):
        user = self.users.UserResponse.model_validate(self.payload)
        self.assertTrue(user.subscription_url.startswith("http://203.0.113.10:8000/sub/"))

    def test_existing_urls_and_tokens_remain_unchanged(self):
        for url in ("https://old.example/sub/existing-token", "/sub/existing-token"):
            with self.subTest(url=url), patch.object(self.users, "create_subscription_token") as mint:
                user = self.users.UserResponse.model_validate(dict(self.payload, subscription_url=url))
                self.assertEqual(user.subscription_url, url)
                self.assertEqual(self.users.UserResponse.model_validate(user).subscription_url, url)
                mint.assert_not_called()

    def test_nodes_still_accept_ips_and_domains(self):
        node_model = importlib.import_module("app.models.node").NodeCreate
        for address in ("192.0.2.10", "2001:db8::10", "node.example.com"):
            with self.subTest(address=address):
                self.assertEqual(node_model(name="node", address=address).address, address)


if __name__ == "__main__":
    unittest.main()
