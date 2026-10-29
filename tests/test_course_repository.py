import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aivirteach_agent.config import Settings
from aivirteach_agent.course_repository import CourseRepository, StoredCourse
from aivirteach_agent.models import DiagnoseRequest, ToolName
from scripts.process_course import process


def _stored_payload() -> dict:
    return {
        "schema_version": 1,
        "course": {
            "course_id": "n8n-agent-builder",
            "version": 1,
            "title": "AI Daily Briefing",
            "summary": "Stored course summary",
            "relevant_excerpts": [],
        },
        "aliases": ["ai-daily-briefing"],
        "source": {"format": "markdown", "path": "../raw/course.md"},
        "lessons": [
            {
                "context": {
                    "module_id": "configure-runtime",
                    "lesson_id": "install-and-start-n8n",
                    "sequence": 2,
                    "title": "Install and Start n8n",
                    "summary": "Run n8n with Docker Compose.",
                    "instructions": ["Start the n8n container."],
                    "expected_result": "n8n opens on localhost:5678.",
                    "success_criteria": ["The container is running."],
                    "common_failures": [],
                },
                "course_step": "1.2",
                "aliases": ["install-n8n"],
                "checkpoint_ids": ["S2"],
                "keywords": ["n8n", "docker"],
                "relevant_excerpts": [
                    {"title": "S2 n8n Running", "content": "Check docker compose ps."}
                ],
                "source": {"path": "../raw/course.md", "start_line": 10, "end_line": 20},
            }
        ],
    }


def _request() -> DiagnoseRequest:
    return DiagnoseRequest.model_validate(
        {
            "request_id": "a10beac8-d1db-4b1a-8df0-79aa8208e273",
            "lab_id": "lab-001",
            "question": "n8n 为什么打不开？",
            "course": {
                "course_id": "ai-daily-briefing",
                "version": 1,
                "title": "Server title",
            },
            "current_step": {
                "module_id": "runtime-environment",
                "lesson_id": "install-n8n",
                "sequence": 4,
                "title": "Install and Start n8n",
            },
            "diagnostic_scope": {"allowed_tools": ["get_vm_status"]},
        }
    )


class CourseRepositoryTests(unittest.TestCase):
    def test_repository_enriches_alias_step_without_expanding_tool_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.agent.json"
            path.write_text(json.dumps(_stored_payload()), encoding="utf-8")
            repository = CourseRepository(directory)

            original = _request()
            enriched = repository.enrich(original)

        self.assertEqual(repository.course_count, 1)
        self.assertEqual(enriched.course.title, "AI Daily Briefing")
        self.assertEqual(enriched.current_step.lesson_id, "install-and-start-n8n")
        self.assertIn("The container is running.", enriched.current_step.success_criteria)
        self.assertEqual(enriched.course.relevant_excerpts[0].title, "S2 n8n Running")
        self.assertEqual(enriched.diagnostic_scope.allowed_tools, {ToolName.GET_VM_STATUS})

    def test_unknown_step_is_not_matched_by_sequence_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.agent.json"
            path.write_text(json.dumps(_stored_payload()), encoding="utf-8")
            repository = CourseRepository(directory)
            request = _request().model_copy(
                update={
                    "current_step": _request().current_step.model_copy(
                        update={"lesson_id": "verify-virtual-machine", "title": "Verify VM"}
                    )
                }
            )

            enriched = repository.enrich(request)

        self.assertIs(enriched, request)

    def test_course_directory_is_configurable(self) -> None:
        with patch.dict(os.environ, {"AIVIRTEACH_COURSE_DIR": "/tmp/course-store"}):
            settings = Settings.from_env()
        self.assertEqual(settings.course_directory, "/tmp/course-store")

    def test_converter_creates_valid_retrieval_document(self) -> None:
        markdown = """# Example Course

# Agent Runbook Scope

An example course.

# Agent Operating Protocol

Preserve learner work.

# 1 Runtime

# 1.1 Install Docker

Install Docker using the commands below.

```yaml
services:
  app:
    image: example
```

# Agent Verification and Recovery Guide

## S1 — Docker Ready

**Expected result by node**

- Docker is active.

**Common failures**

- `inactive`: start Docker and check its status.
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            output = root / "processed"
            raw.mkdir()
            (raw / "course.md").write_text(markdown, encoding="utf-8")

            course_path, index_path = process(raw, output)
            payload = json.loads(course_path.read_text(encoding="utf-8"))
            stored = StoredCourse.model_validate(payload)
            course_mode = stat.S_IMODE(course_path.stat().st_mode)
            index_mode = stat.S_IMODE(index_path.stat().st_mode)

        self.assertEqual(index_path.name, "index.json")
        self.assertEqual(course_mode, 0o644)
        self.assertEqual(index_mode, 0o644)
        self.assertEqual(len(stored.lessons), 1)
        self.assertEqual(stored.course.summary, "An example course.")
        self.assertEqual(stored.lessons[0].checkpoint_ids, ["S1"])
        self.assertEqual(stored.lessons[0].context.success_criteria, ["Docker is active."])
        self.assertIn("  app:", stored.lessons[0].relevant_excerpts[0].content)


if __name__ == "__main__":
    unittest.main()
