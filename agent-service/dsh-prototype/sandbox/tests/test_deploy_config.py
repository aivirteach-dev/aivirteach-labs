"""部署文件里的安全设置不能被无意改掉；告警规则、Collector、Alertmanager 配置用官方工具检查。

promtool / amtool / otelcol-contrib 不在 PATH 时跳过对应检查（服务器上用镜像里的工具检查，见 docs/monitoring.md）。
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


class MonitoringComposeTests(unittest.TestCase):
    text = (ROOT / "compose.monitoring.yaml").read_text(encoding="utf-8")

    def setting(self, name):
        values = re.findall(rf'^\s+{re.escape(name)}: "?([^"\n]*)"?$', self.text, re.M)
        self.assertEqual(len(values), 1, name)
        return values[0]

    def test_every_published_port_is_localhost_only(self):
        ports = re.findall(r'^\s+- "((?!--)[^"]*:\d+)"$', self.text, re.M)  # 去掉 --xxx=:9090 这类命令参数
        self.assertEqual(sorted(ports), ["127.0.0.1:${ALERTMANAGER_UI_PORT:-9093}:9093",
                                         "127.0.0.1:${PHOENIX_UI_PORT:-6006}:6006",
                                         "127.0.0.1:${PROMETHEUS_UI_PORT:-9090}:9090"])

    def test_phoenix_auth_retention_and_outbound_features(self):
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

    def test_images_are_pinned_and_containers_hardened(self):
        images = re.findall(r"^\s+image: (\S+)$", self.text, re.M)
        self.assertEqual(len(images), 4)
        for image in images:
            self.assertRegex(image, r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$")
        services = re.split(r"^  (?=[\w-]+:\n)", self.text.split("\nservices:\n", 1)[1], flags=re.M)
        hardened = [s for s in services if "image:" in s]
        self.assertEqual(len(hardened), 4)
        for service in hardened:
            self.assertIn("<<: *hardening", service)
            self.assertRegex(service, r'user: "\d+:\d+"')
            self.assertNotIn('user: "0', service)
        for line in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true"):
            self.assertIn(line, self.text.split("services:", 1)[0])  # 在 x-hardening 里

    def test_alerting_details(self):
        self.assertIn('"--cluster.listen-address="', self.text)  # Alertmanager 不监听集群端口
        self.assertIn('"--enable-feature=created-timestamp-zero-ingestion"', self.text)
        self.assertIn("scrape_protocols: [PrometheusProto", (DEPLOY / "prometheus.yml").read_text(encoding="utf-8"))

    def test_agent_sends_to_the_collector_only_in_the_monitoring_overlay(self):
        self.assertEqual(self.setting("OTEL_EXPORTER_OTLP_ENDPOINT"), "http://otel-collector:4318")
        platform = (ROOT / "compose.platform.yaml").read_text(encoding="utf-8")
        self.assertIn('OTEL_EXPORTER_OTLP_ENDPOINT: "${OTEL_EXPORTER_OTLP_ENDPOINT:-}"', platform)
        self.assertIn('AIVIRTEACH_OTEL_CAPTURE_CONTENT: "${AIVIRTEACH_OTEL_CAPTURE_CONTENT:-false}"', platform)

    def test_platform_compose_forwards_every_setting_the_agent_reads(self):
        """服务器测试发现看图变量没有传进容器：Agent 读取的每个环境变量，compose 都要原样转发。"""
        platform = (ROOT / "compose.platform.yaml").read_text(encoding="utf-8")
        forwarded = set(re.findall(r"^\s+([A-Z][A-Z0-9_]+): ", platform, re.M))
        read = set()
        for source in (ROOT / "dsh_tutor").glob("*.py"):
            text = source.read_text(encoding="utf-8")
            read |= set(re.findall(r'(?:getenv|_int_env)\(\s*"((?:AIVIRTEACH|OTEL)_[A-Z0-9_]+)"', text))
            read |= set(re.findall(r'^[A-Z_]+_ENV = "((?:AIVIRTEACH)_[A-Z0-9_]+)"', text, re.M))
        # 只在宿主机启动脚本或进程内部用到、容器里由 compose 固定的变量
        not_needed = {"AIVIRTEACH_AGENT_ENV_FILE", "AIVIRTEACH_DSH_STATE_DIR", "OTEL_SDK_DISABLED",
                      "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"}
        self.assertTrue(read, "no settings found")
        self.assertEqual(sorted(read - forwarded - not_needed), [])

    def test_secret_files_are_ignored_by_git(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        self.assertIn(".env.*", ignored)
        self.assertIn(".alertmanager.yml", ignored)
        example = (DEPLOY / "monitoring.env.example").read_text(encoding="utf-8")
        self.assertTrue(all(not line.split("=", 1)[1] for line in example.splitlines()
                            if line and not line.startswith("#")), "example must not contain values")


def run(*command):
    return subprocess.run(command, cwd=DEPLOY, capture_output=True, text=True, timeout=60)


class MonitoringToolTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("promtool"), "promtool is not on PATH")
    def test_alert_rules_are_valid_and_pass_their_unit_tests(self):
        for command in (("promtool", "check", "rules", "alerts.yml"), ("promtool", "test", "rules", "alerts.test.yml")):
            result = run(*command)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("amtool"), "amtool is not on PATH")
    def test_alertmanager_configs_are_valid(self):
        for name in ("alertmanager.yml", "alertmanager.example.yml"):
            result = run("amtool", "check-config", name)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("otelcol-contrib"), "otelcol-contrib is not on PATH")
    def test_collector_config_is_valid(self):
        result = run("otelcol-contrib", "validate", "--config=otel-collector.yaml")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
