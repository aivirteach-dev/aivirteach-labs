#!/usr/bin/env python3
"""Convert a Markdown course and its recovery guide into Agent retrieval JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RAW_DIR = PROJECT_DIR / ".cache" / "course" / "AI Daily Briefing" / "raw"
DEFAULT_OUTPUT_DIR = (
    PROJECT_DIR / ".cache" / "course" / "AI Daily Briefing" / "processed"
)
LESSON_HEADING_RE = re.compile(r"^#\s+(\d+)\.(\d+)\s+(.+?)\s*$")
MODULE_HEADING_RE = re.compile(r"^#\s+(\d+)\s+([^#].*?)\s*$")
CHECKPOINT_HEADING_RE = re.compile(r"^##\s+(S\d+)\s+[—-]\s+(.+?)\s*$")
BOLD_HEADING_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
MARKDOWN_LINK_RE = re.compile(r"\[[^]]+\]\(([^)]+)\)")
MAX_EXCERPT_CHARS = 3_800
MAX_EXCERPTS = 5

# This course has two tutorial sections that share checkpoints, and its final
# lesson covers both delivery and scheduling. Keeping this mapping explicit
# avoids pretending that heading order alone is sufficient semantic parsing.
CHECKPOINTS_BY_LESSON = {
    "1.1": ["S1"],
    "1.2": ["S2"],
    "2.1": ["S3"],
    "2.2": ["S4"],
    "2.3": ["S5"],
    "2.4": ["S5"],
    "2.5": ["S6"],
    "2.6": ["S7"],
    "2.7": ["S8", "S9"],
}
LESSON_ALIASES_BY_STEP = {
    "1.2": ["install-n8n"],
    "2.1": ["add-trigger"],
    "2.2": ["retrieve-news"],
    "2.4": ["filter-rank-articles"],
    "2.5": ["summarize-with-gemini"],
    "2.6": ["generate-briefing-html"],
    "2.7": ["send-and-schedule"],
}


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    start_line: int
    end_line: int
    body: str


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "section"


def _strip_markdown(value: str) -> str:
    value = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", value)
    value = value.replace("`", "").replace("**", "").replace("__", "")
    return re.sub(r"\s+", " ", value).strip(" -\t")


def _section_body(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start + 1 : end]).strip()


def _parse_lessons(lines: list[str]) -> tuple[dict[str, str], list[Section]]:
    modules: dict[str, str] = {}
    headings: list[tuple[int, re.Match[str]]] = []
    h1_indexes = [index for index, line in enumerate(lines) if line.startswith("# ")]
    for index, line in enumerate(lines):
        module_match = MODULE_HEADING_RE.match(line)
        if module_match and "." not in module_match.group(1):
            modules[module_match.group(1)] = _strip_markdown(module_match.group(2))
        lesson_match = LESSON_HEADING_RE.match(line)
        if lesson_match:
            headings.append((index, lesson_match))

    lessons: list[Section] = []
    for start, match in headings:
        end = next((index for index in h1_indexes if index > start), len(lines))
        key = f"{match.group(1)}.{match.group(2)}"
        lessons.append(
            Section(
                key=key,
                title=_strip_markdown(match.group(3)),
                start_line=start + 1,
                end_line=end,
                body=_section_body(lines, start, end),
            )
        )
    return modules, lessons


def _parse_checkpoints(lines: list[str]) -> dict[str, Section]:
    headings: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = CHECKPOINT_HEADING_RE.match(line)
        if match:
            headings.append((index, match))
    checkpoints: dict[str, Section] = {}
    for position, (start, match) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        for candidate in range(start + 1, end):
            if lines[candidate].startswith("## Symptom-to-Checkpoint Matrix"):
                end = candidate
                break
        checkpoint_id = match.group(1)
        checkpoints[checkpoint_id] = Section(
            key=checkpoint_id,
            title=_strip_markdown(match.group(2)),
            start_line=start + 1,
            end_line=end,
            body=_section_body(lines, start, end),
        )
    return checkpoints


def _named_blocks(body: str) -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current = "overview"
    blocks[current] = []
    for line in body.splitlines():
        match = BOLD_HEADING_RE.match(line.strip())
        if match:
            current = _strip_markdown(match.group(1)).lower()
            blocks.setdefault(current, [])
            continue
        blocks[current].append(line)
    return {name: "\n".join(values).strip() for name, values in blocks.items()}


def _bullet_items(value: str) -> list[str]:
    items: list[str] = []
    for line in value.splitlines():
        match = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.+?)\s*$", line)
        if match:
            cleaned = _strip_markdown(match.group(1))
            if cleaned:
                items.append(cleaned)
    return items


def _common_failures(checkpoint_id: str, block: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for line in block.splitlines():
        match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if not match:
            continue
        raw_item = match.group(1)
        if ": " in raw_item:
            raw_symptom, raw_explanation = raw_item.rsplit(": ", 1)
        else:
            raw_symptom = raw_explanation = raw_item
        symptom = _strip_markdown(raw_symptom)
        explanation = _strip_markdown(raw_explanation)
        failures.append(
            {
                "code": f"{checkpoint_id.lower()}-{_slugify(symptom)[:60]}",
                "symptoms": [symptom],
                "likely_causes": [explanation],
                "learner_guidance": explanation,
            }
        )
    return failures[:12]


def _plain_paragraphs(markdown: str) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    in_code = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.startswith(("#", "---", "![", "|")):
            continue
        if not line:
            if current:
                paragraphs.append(_strip_markdown(" ".join(current)))
                current = []
            continue
        bullet = re.match(r"^(?:[-*]|\d+\.)\s+(.+)$", line)
        if bullet:
            if current:
                paragraphs.append(_strip_markdown(" ".join(current)))
                current = []
            paragraphs.append(_strip_markdown(bullet.group(1)))
        else:
            current.append(line)
    if current:
        paragraphs.append(_strip_markdown(" ".join(current)))
    return [item for item in paragraphs if item]


def _chunks(value: str, limit: int = MAX_EXCERPT_CHARS) -> list[str]:
    blocks: list[str] = []
    block_lines: list[str] = []
    in_code = False
    for line in value.strip("\n").splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        if not line.strip() and not in_code:
            if block_lines:
                blocks.append("\n".join(block_lines))
                block_lines = []
            continue
        block_lines.append(line)
    if block_lines:
        blocks.append("\n".join(block_lines))

    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            lines = block.splitlines()
            oversized = ""
            for line in lines:
                candidate = f"{oversized}\n{line}".lstrip("\n")
                if len(candidate) > limit and oversized:
                    chunks.append(oversized)
                    oversized = line
                else:
                    oversized = candidate
            if oversized:
                chunks.append(oversized)
            continue
        candidate = f"{current}\n\n{block}".strip("\n")
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _linked_text_excerpts(raw_dir: Path, body: str) -> list[dict[str, str]]:
    excerpts: list[dict[str, str]] = []
    for target in MARKDOWN_LINK_RE.findall(body):
        relative = target.split("#", 1)[0]
        candidate = (raw_dir / relative).resolve()
        try:
            candidate.relative_to(raw_dir.resolve())
        except ValueError:
            continue
        if candidate.suffix.lower() not in {".txt", ".md", ".json", ".yaml", ".yml"}:
            continue
        if not candidate.is_file() or candidate.stat().st_size > 128_000:
            continue
        content = candidate.read_text(encoding="utf-8", errors="replace")
        for number, chunk in enumerate(_chunks(content), start=1):
            excerpts.append(
                {
                    "title": f"Attachment: {candidate.name} ({number})",
                    "content": chunk,
                }
            )
    return excerpts


def _global_section(lines: list[str], start_heading: str, end_heading: str | None) -> str:
    try:
        start = lines.index(start_heading)
    except ValueError:
        return ""
    end = len(lines)
    if end_heading:
        try:
            end = lines.index(end_heading, start + 1)
        except ValueError:
            pass
    return _section_body(lines, start, end)


def build_processed_course(
    raw_dir: Path, markdown_path: Path, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict[str, Any]:
    source_text = markdown_path.read_text(encoding="utf-8")
    lines = source_text.splitlines()
    modules, lessons = _parse_lessons(lines)
    checkpoints = _parse_checkpoints(lines)
    if not lessons:
        raise ValueError(f"No numbered lesson headings found in {markdown_path}")

    overview = _global_section(lines, "# Overview", "# Agent Operating Protocol")
    if not overview:
        # The reviewed course-image runbook uses this heading for its overview.
        overview = _global_section(
            lines, "# Agent Runbook Scope", "# Agent Operating Protocol"
        )
    protocol = _global_section(
        lines, "# Agent Operating Protocol", "# 1 Configure the Runtime Environment"
    )
    recovery_tail = _global_section(lines, "## Unknown Error Procedure", None)
    overview_paragraphs = _plain_paragraphs(overview)
    summary = " ".join(overview_paragraphs[:2])[:2_000]
    global_excerpts: list[dict[str, str]] = []
    for title, content in (
        ("Agent operating protocol", protocol),
        ("Unknown errors and completion", recovery_tail),
    ):
        if content:
            global_excerpts.append({"title": title, "content": _chunks(content)[0]})

    processed_lessons: list[dict[str, Any]] = []
    for sequence, lesson in enumerate(lessons, start=1):
        module_number = lesson.key.split(".", 1)[0]
        module_title = modules.get(module_number, f"Module {module_number}")
        checkpoint_ids = CHECKPOINTS_BY_LESSON.get(lesson.key, [])
        checkpoint_records = [checkpoints[item] for item in checkpoint_ids if item in checkpoints]
        checkpoint_blocks = [_named_blocks(item.body) for item in checkpoint_records]

        success_criteria: list[str] = []
        failures: list[dict[str, Any]] = []
        for checkpoint, blocks in zip(checkpoint_records, checkpoint_blocks, strict=True):
            expected_result = next(
                (
                    content
                    for name, content in blocks.items()
                    if name.startswith("expected result")
                ),
                "",
            )
            success_criteria.extend(_bullet_items(expected_result))
            failures.extend(_common_failures(checkpoint.key, blocks.get("common failures", "")))
        success_criteria = list(dict.fromkeys(success_criteria))[:20]
        failures = list({item["code"]: item for item in failures}.values())[:12]

        paragraphs = _plain_paragraphs(lesson.body)
        instructions = paragraphs[:20]
        expected_result = " ".join(success_criteria)[:4_000]
        lesson_summary = (paragraphs[0] if paragraphs else expected_result or lesson.title)[:2_000]

        excerpts: list[dict[str, str]] = []
        lesson_chunks = _chunks(lesson.body)
        for number, chunk in enumerate(lesson_chunks[:3], start=1):
            excerpts.append(
                {"title": f"{lesson.key} {lesson.title} ({number})", "content": chunk}
            )
        for checkpoint in checkpoint_records:
            for number, chunk in enumerate(_chunks(checkpoint.body), start=1):
                excerpts.append(
                    {
                        "title": f"{checkpoint.key} {checkpoint.title} ({number})",
                        "content": chunk,
                    }
                )
        excerpts.extend(_linked_text_excerpts(raw_dir, lesson.body))
        excerpts = excerpts[:MAX_EXCERPTS]

        keywords = {
            *_slugify(lesson.title).split("-"),
            *_slugify(module_title).split("-"),
            *(item.lower() for item in checkpoint_ids),
        }
        processed_lessons.append(
            {
                "context": {
                    "module_id": _slugify(module_title),
                    "lesson_id": _slugify(lesson.title),
                    "sequence": sequence,
                    "title": lesson.title,
                    "summary": lesson_summary,
                    "instructions": instructions,
                    "expected_result": expected_result,
                    "success_criteria": success_criteria,
                    "common_failures": failures,
                },
                "course_step": lesson.key,
                "aliases": LESSON_ALIASES_BY_STEP.get(lesson.key, []),
                "checkpoint_ids": checkpoint_ids,
                "keywords": sorted(item for item in keywords if item),
                "relevant_excerpts": excerpts,
                "source": {
                    "path": os.path.relpath(markdown_path, output_dir),
                    "start_line": lesson.start_line,
                    "end_line": lesson.end_line,
                },
            }
        )

    return {
        "schema_version": 1,
        "course": {
            "course_id": "n8n-agent-builder",
            "version": 1,
            "title": "AI Daily Briefing",
            "summary": summary,
            "relevant_excerpts": global_excerpts[:5],
        },
        "aliases": ["ai-daily-briefing", "AI Daily Briefing"],
        "source": {
            "format": "markdown",
            "path": os.path.relpath(markdown_path, output_dir),
            "sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        },
        "lessons": processed_lessons,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    # Compose mounts this cache read-only and Agent runs as an unprivileged UID.
    # Course retrieval JSON contains no credentials and must remain readable.
    temporary.chmod(0o644)
    temporary.replace(path)


def process(raw_dir: Path, output_dir: Path) -> tuple[Path, Path]:
    markdown_files = sorted(raw_dir.glob("*.md"))
    if not markdown_files:
        raise FileNotFoundError(f"No Markdown course found in {raw_dir}")
    markdown_path = markdown_files[0]
    payload = build_processed_course(
        raw_dir.resolve(), markdown_path.resolve(), output_dir.resolve()
    )
    course_path = output_dir / "course.agent.json"
    index_path = output_dir / "index.json"
    _write_json(course_path, payload)
    _write_json(
        index_path,
        {
            "schema_version": 1,
            "courses": [
                {
                    "course_id": payload["course"]["course_id"],
                    "aliases": payload["aliases"],
                    "file": course_path.name,
                    "lesson_ids": [
                        item["context"]["lesson_id"] for item in payload["lessons"]
                    ],
                }
            ],
        },
    )
    return course_path, index_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    course_path, index_path = process(args.raw_dir.resolve(), args.output_dir.resolve())
    print(f"Wrote {course_path}")
    print(f"Wrote {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
