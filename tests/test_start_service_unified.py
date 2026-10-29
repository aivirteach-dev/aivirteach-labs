from pathlib import Path
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]


class ServiceStartTests(unittest.TestCase):
    def test_vm_manager_starts_only_vm_application_on_8760(self) -> None:
        script = (PROJECT_DIR / "vm-manager" / "start_service.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("AIVIRTEACH_API_TOKEN", script)
        self.assertNotIn("AIVIRTEACH_DIAGNOSTIC_TOKEN", script)
        self.assertIn("AIVIRTEACH_API_PORT:-8760", script)
        self.assertIn("uvicorn service:app", script)
        self.assertNotIn("docs_gateway_service:app", script)

    def test_docs_service_starts_without_service_tokens_on_8780(self) -> None:
        script = (PROJECT_DIR / "docs-service" / "start_docs_service.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("AIVIRTEACH_DOCS_PORT:-8780", script)
        self.assertIn("docs_service:app", script)
        self.assertNotIn("AIVIRTEACH_API_TOKEN", script)
        self.assertNotIn("AIVIRTEACH_DIAGNOSTIC_TOKEN", script)
        self.assertNotIn("AIVIRTEACH_AGENT_TOKEN", script)

    def test_diagnostic_gateway_runs_independently_on_8765(self) -> None:
        script = (
            PROJECT_DIR / "diagnostic-gateway" / "start_diagnostic_service.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("AIVIRTEACH_DIAGNOSTIC_TOKEN", script)
        self.assertNotIn("AIVIRTEACH_API_TOKEN", script)
        self.assertIn("AIVIRTEACH_DIAGNOSTIC_PORT:-8765", script)
        self.assertIn("gateway_service:app", script)

    def test_agent_gateway_default_points_to_8765(self) -> None:
        config = (
            PROJECT_DIR / "agent-service" / "aivirteach_agent" / "config.py"
        ).read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8765", config)

    def test_progress_worker_has_no_port_or_libvirt_privileges(self) -> None:
        script = (
            PROJECT_DIR / "progress-worker" / "start_progress_worker.sh"
        ).read_text(encoding="utf-8")
        unit = (
            PROJECT_DIR / "systemd" / "aivirteach-progress-worker.service"
        ).read_text(encoding="utf-8")

        self.assertIn("AIVIRTEACH_PROGRESS_SERVER_TOKEN", script)
        self.assertIn("AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN", script)
        self.assertNotIn("uvicorn", script)
        self.assertIn("User=aivirteach-progress", unit)
        self.assertIn("StateDirectory=aivirteach-progress", unit)
        self.assertNotIn("libvirt-sock", unit)
        self.assertNotIn("AIVIRTEACH_API_TOKEN", unit)


if __name__ == "__main__":
    unittest.main()
