"""验证边界和失败路径；不连接 Mac 模型。"""
import json
import os
import pwd
import time
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from pydantic import ValidationError
from dsh_tutor import runtime
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.course import build_prompt


class BasicsTests(unittest.TestCase):
    def test_patch_has_no_template_values(self):
        config = runtime.load_config(runtime.ROOT / "config.json")
        text = runtime.render_patch(config)
        self.assertNotIn("__MODEL__", text)
        self.assertIn(json.dumps(config["model"]), text)

    def test_unknown_input_fields_rejected(self):
        with self.assertRaises(ValidationError):
            CourseQuestion.model_validate({"request_id": "x", "question": "x", "course": {"course_id": "c", "version": "v", "lesson_id": "l", "title": "t", "goals": ["g"]}, "grant_all_tools": True})

    def test_prompt_size_is_bounded(self):
        course = {"course_id":"x", "version":"v1", "lesson_id":"l1", "title":"x", "goals":["x" * 9000]}
        request = CourseQuestion.model_validate({"request_id":"x", "course":course, "question":"x"})
        with self.assertRaises(ValueError):
            build_prompt(request)

    def test_connection_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            config = runtime.load_config(runtime.ROOT / "config.json")
            # 独立测试配置绑定当前测试账号，仅验证连接失败分支。
            config["execution_user"] = pwd.getpwuid(os.geteuid()).pw_name
            config["ollama_base_url"] = "http://127.0.0.1:1/v1"
            config["total_timeout_seconds"] = 15
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "DSH 运行失败"):
                runtime.run_prompt("你好", config_path=path, state_dir=directory)

    def test_wrong_identity_rejected_before_launch(self):
        """错误账号不能创建运行目录或启动模型进程。"""
        with tempfile.TemporaryDirectory() as directory:
            config = runtime.load_config(runtime.ROOT / "config.json")
            config["execution_user"] = "different-test-identity"
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            state = Path(directory) / "runs"
            with self.assertRaisesRegex(PermissionError, "DSH 必须以"):
                runtime.run_prompt("你好", config_path=path, state_dir=state)
            self.assertFalse(state.exists())

    def test_worker_rejects_wrong_identity(self):
        """直接运行工作进程也必须检查身份，避免绕过正常入口。"""
        with tempfile.TemporaryDirectory() as directory:
            config = runtime.load_config(runtime.ROOT / "config.json")
            config["execution_user"] = "different-test-identity"
            path = Path(directory) / "request.json"
            path.write_text(json.dumps({"config": config, "prompt": "test"}), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "DSH 必须以"):
                runtime.worker(path)

    def test_owned_process_termination(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            runtime.terminate_owned(proc)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    @unittest.skipUnless(os.name == "posix", "POSIX 进程组检查")
    def test_owned_group_cleanup_after_parent_exit(self):
        """组长退出后仍清理其子进程，同时不影响另一个请求的进程。"""
        sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        script = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True)"
        proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True,
                                stdout=subprocess.PIPE, text=True)
        try:
            child_pid = int(proc.stdout.readline())
            proc.wait(timeout=5)
            runtime.terminate_owned(proc, owns_group=True)
            def child_is_running():
                path = Path(f"/proc/{child_pid}/stat")
                try:
                    return path.read_text().rsplit(")", 1)[1].split()[0] != "Z"
                except FileNotFoundError:
                    return False
            deadline = time.monotonic() + 2
            while child_is_running() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(child_is_running())
            self.assertIsNone(sentinel.poll())
        finally:
            runtime.terminate_owned(proc, owns_group=True)
            proc.stdout.close()
            runtime.terminate_owned(sentinel)


if __name__ == "__main__":
    unittest.main()
