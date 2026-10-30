"""Offline deployment checks; requires Docker Compose, not a running daemon."""

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
# Deliberately not a usable credential; only tests literal dollar-sign handling.
TEST_USERS = "test:$2y$05$not-a-real-password-hash"


def compose(env_text="", overrides=None):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OTP_", "TRAEFIK_", "COMPOSE_"))
    }
    env.update(overrides or {})
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env") as env_file:
        env_file.write(env_text)
        env_file.flush()
        return subprocess.run(
            [
                "docker", "compose",
                "--project-directory", str(ROOT),
                "--env-file", env_file.name,
                "-f", str(ROOT / "compose.yaml"),
                "--profile", "build",
                "config", "--format", "json",
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


class DeploymentTests(unittest.TestCase):
    def config(self, extra="", overrides=None):
        result = compose(f"OTP_AUTH_USERS='{TEST_USERS}'\n{extra}", overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_auth_is_required(self):
        for text in ("", "OTP_AUTH_USERS=\n"):
            with self.subTest(empty=bool(text)):
                result = compose(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Set OTP_AUTH_USERS", result.stderr)

    def test_single_quoted_hash_is_literal(self):
        labels = self.config()["services"]["otp"]["labels"]
        # Newer Compose versions re-escape dollars when serializing config,
        # even in JSON, so the result can be safely used as Compose input.
        self.assertIn(
            labels["traefik.http.middlewares.trips-otp-auth.basicauth.users"],
            (TEST_USERS, TEST_USERS.replace("$", "$$")),
        )
        self.assertEqual(
            labels["traefik.http.middlewares.trips-otp-auth.basicauth.removeheader"],
            "true",
        )

    def test_runtime_is_hardened_and_private(self):
        config = self.config()
        runtime = config["services"]["otp"]
        self.assertEqual(runtime["image"], "opentripplanner/opentripplanner:2.10.0")
        self.assertEqual(runtime["command"], ["--load", "--serve"])
        self.assertNotIn("ports", runtime)
        self.assertTrue(runtime["read_only"])
        self.assertNotEqual(runtime["user"].split(":")[0], "0")
        self.assertIn("ALL", runtime["cap_drop"])
        self.assertEqual(runtime["restart"], "unless-stopped")
        self.assertTrue(all(volume["read_only"] for volume in runtime["volumes"]))
        graph = next(
            v for v in runtime["volumes"]
            if v["target"] == "/var/opentripplanner/graph.obj"
        )
        self.assertFalse(graph["bind"]["create_host_path"])
        self.assertEqual(set(runtime["networks"]), {"traefik_proxy"})
        self.assertTrue(config["networks"]["traefik_proxy"]["external"])
        labels = runtime["labels"]
        self.assertEqual(
            labels["traefik.http.routers.trips-otp.middlewares"],
            "trips-otp-auth,trips-otp-headers",
        )
        self.assertEqual(labels["traefik.http.routers.trips-otp.tls"], "true")
        self.assertEqual(
            labels["traefik.http.routers.trips-otp-http.service"], "noop@internal"
        )
        self.assertEqual(
            labels["traefik.http.middlewares.trips-otp-https.redirectscheme.scheme"],
            "https",
        )

    def test_builder_is_explicit_offline_and_writable(self):
        builder = self.config()["services"]["otp-build"]
        self.assertEqual(builder["profiles"], ["build"])
        self.assertEqual(builder["command"], ["--build", "--save"])
        self.assertEqual(builder["entrypoint"], ["bash", "/opt/otp/build.sh"])
        self.assertEqual(builder["network_mode"], "none")
        self.assertEqual(builder["labels"]["traefik.enable"], "false")
        data = next(
            v for v in builder["volumes"]
            if v["target"] == "/var/opentripplanner"
        )
        self.assertFalse(data.get("read_only", False))

    def test_environment_overrides(self):
        config = self.config(
            "OTP_HOST=planner.example.org\n"
            "TRAEFIK_NETWORK=custom_proxy\n"
            "TRAEFIK_HTTP_ENTRYPOINT=http\n"
            "TRAEFIK_HTTPS_ENTRYPOINT=https\n"
            "TRAEFIK_CERT_RESOLVER=\n"
            "OTP_UID=1234\nOTP_GID=5678\n"
            "OTP_MEMORY_LIMIT=8g\n"
        )
        runtime = config["services"]["otp"]
        labels = runtime["labels"]
        self.assertEqual(runtime["user"], "1234:5678")
        self.assertEqual(int(runtime["mem_limit"]), 8 * 1024**3)
        self.assertEqual(
            labels["traefik.http.routers.trips-otp.rule"], "Host(`planner.example.org`)"
        )
        self.assertEqual(labels["traefik.docker.network"], "custom_proxy")
        self.assertEqual(labels["traefik.http.routers.trips-otp.entrypoints"], "https")
        self.assertEqual(labels["traefik.http.routers.trips-otp.tls.certresolver"], "")
        self.assertEqual(config["networks"]["traefik_proxy"]["name"], "custom_proxy")

    def test_otp_features_and_python_example_syntax(self):
        config = json.loads((ROOT / "config/otp-config.json").read_text())
        self.assertTrue(config["otpFeatures"]["GtfsGraphQlApi"])
        self.assertTrue(config["otpFeatures"]["ActuatorAPI"])
        examples = re.findall(
            r"```python\n(.*?)```", (ROOT / "README.md").read_text(), re.DOTALL
        )
        self.assertTrue(examples)
        for example in examples:
            compile(example, "README.md Python example", "exec")


class HealthcheckTests(unittest.TestCase):
    def test_http_status_and_request_path(self):
        for code in (200, 404, 503):
            with self.subTest(code=code):
                requests = []
                errors = []
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    listener.listen(1)
                    listener.settimeout(5)
                    port = listener.getsockname()[1]

                    def serve():
                        try:
                            connection, _ = listener.accept()
                            with connection:
                                connection.settimeout(5)
                                request = b""
                                while b"\r\n\r\n" not in request:
                                    chunk = connection.recv(4096)
                                    if not chunk:
                                        break
                                    request += chunk
                                requests.append(request)
                                connection.sendall(
                                    f"HTTP/1.1 {code} Test\r\n"
                                    "Content-Length: 0\r\n"
                                    "Connection: close\r\n\r\n".encode()
                                )
                        except Exception as exc:
                            errors.append(exc)

                    worker = threading.Thread(target=serve, daemon=True)
                    worker.start()
                    result = subprocess.run(
                        ["bash", str(ROOT / "scripts/healthcheck.sh"), str(port)],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    worker.join(timeout=6)
                self.assertFalse(worker.is_alive())
                self.assertFalse(errors, errors)
                self.assertEqual(result.returncode == 0, code == 200)
                self.assertTrue(requests)
                self.assertTrue(
                    requests[0].startswith(
                        b"GET /otp/actuators/health HTTP/1.1\r\n"
                    )
                )


if __name__ == "__main__":
    unittest.main()
