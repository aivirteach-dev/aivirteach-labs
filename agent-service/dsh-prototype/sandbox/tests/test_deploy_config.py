"""部署文件里的安全设置不能被无意改掉：Phoenix 只在本机、要登录、不外发、固定版本；Agent 默认不发监控。"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class MonitoringComposeTests(unittest.TestCase):
    text = (ROOT / "compose.monitoring.yaml").read_text(encoding="utf-8")

    def setting(self, name):
        match = re.search(rf'^\s+{name}: "?([^"\n]*)"?$', self.text, re.M)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_phoenix_is_published_on_localhost_only(self):
        ports = re.findall(r'^\s+- "([^"]*:6006|[^"]*:4317)"$', self.text, re.M)
        self.assertEqual(ports, ["127.0.0.1:${PHOENIX_UI_PORT:-6006}:6006"])

    def test_auth_retention_and_outbound_features(self):
        expected = {
            "PHOENIX_ENABLE_AUTH": "true", "PHOENIX_ENABLE_STRONG_PASSWORD_POLICY": "true",
            "PHOENIX_TELEMETRY_ENABLED": "false", "PHOENIX_ALLOW_EXTERNAL_RESOURCES": "false",
            "PHOENIX_DISABLE_AGENT_ASSISTANT": "true", "PHOENIX_AGENTS_DISABLE_BASH": "true",
            "PHOENIX_AGENTS_DISABLE_WEB_ACCESS": "true", "PHOENIX_AGENTS_DISABLE_GITHUB": "true",
            "PHOENIX_ENABLE_MCP_SERVER": "false", "PHOENIX_ALLOWED_SANDBOX_PROVIDERS": "NONE",
            "PHOENIX_DEFAULT_RETENTION_POLICY_DAYS": "${PHOENIX_RETENTION_DAYS:-30}",
        }
        for name, value in expected.items():
            self.assertEqual(self.setting(name), value, name)
        self.assertIn(":?", self.setting("PHOENIX_SECRET"))  # 没设密钥就拒绝启动
        self.assertIn(":?", self.setting("PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD"))

    def test_container_hardening_and_pinned_image(self):
        self.assertRegex(self.setting("image"), r"^arizephoenix/phoenix:version-[\d.]+-nonroot@sha256:[0-9a-f]{64}$")
        for line in ('user: "65532:65532"', "read_only: true", "cap_drop: [ALL]", "no-new-privileges:true"):
            self.assertIn(line, self.text)

    def test_agent_sends_to_phoenix_only_in_the_monitoring_overlay(self):
        self.assertEqual(self.setting("OTEL_EXPORTER_OTLP_ENDPOINT"), "http://phoenix:6006")
        platform = (ROOT / "compose.platform.yaml").read_text(encoding="utf-8")
        self.assertIn('OTEL_EXPORTER_OTLP_ENDPOINT: "${OTEL_EXPORTER_OTLP_ENDPOINT:-}"', platform)
        self.assertIn('AIVIRTEACH_OTEL_CAPTURE_CONTENT: "${AIVIRTEACH_OTEL_CAPTURE_CONTENT:-false}"', platform)

    def test_secret_file_is_ignored_by_git(self):
        self.assertIn(".env.*", (ROOT / ".gitignore").read_text(encoding="utf-8").split())
        example = (ROOT / "deploy" / "monitoring.env.example").read_text(encoding="utf-8")
        self.assertTrue(all(not line.split("=", 1)[1] for line in example.splitlines()
                            if line and not line.startswith("#")), "example must not contain values")


if __name__ == "__main__":
    unittest.main()
