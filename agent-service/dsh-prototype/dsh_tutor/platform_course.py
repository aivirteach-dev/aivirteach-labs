# 课程资料补充：逐字复制自 agent-service/aivirteach_agent/course_repository.py，只改了导入路径。

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .platform_models import CourseContext, CourseExcerpt, DiagnoseRequest, LessonContext


MAX_COURSE_FILE_BYTES = 2_000_000


class StoredLesson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: LessonContext
    course_step: str = Field(min_length=1, max_length=32)
    aliases: list[str] = Field(default_factory=list, max_length=16)
    checkpoint_ids: list[str] = Field(default_factory=list, max_length=12)
    keywords: list[str] = Field(default_factory=list, max_length=64)
    relevant_excerpts: list[CourseExcerpt] = Field(default_factory=list, max_length=5)
    source: dict[str, str | int] = Field(default_factory=dict)


class StoredCourse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    course: CourseContext
    aliases: list[str] = Field(default_factory=list, max_length=16)
    source: dict[str, str] = Field(default_factory=dict)
    lessons: list[StoredLesson] = Field(min_length=1, max_length=1_000)


class CourseRepository:
    """Read-only lookup for preprocessed course material."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self._courses: list[StoredCourse] = []
        self.errors: list[str] = []
        self.reload()

    def reload(self) -> None:
        self._courses = []
        self.errors = []
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob("*.agent.json")):
            try:
                if path.stat().st_size > MAX_COURSE_FILE_BYTES:
                    raise ValueError("course file exceeds size limit")
                payload = json.loads(path.read_text(encoding="utf-8"))
                self._courses.append(StoredCourse.model_validate(payload))
            except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
                self.errors.append(f"{path.name}: {type(exc).__name__}")

    @property
    def course_count(self) -> int:
        return len(self._courses)

    def enrich(self, request: DiagnoseRequest) -> DiagnoseRequest:
        stored = self._find_course(request.course.course_id)
        if stored is None:
            return request
        lesson = self._find_lesson(stored, request.current_step)
        if lesson is None:
            return request

        excerpts = _unique_excerpts(
            [
                *lesson.relevant_excerpts,
                *stored.course.relevant_excerpts,
                *request.course.relevant_excerpts,
            ]
        )[:5]
        course = stored.course.model_copy(update={"relevant_excerpts": excerpts})
        step = _merge_lesson(lesson.context, request.current_step)
        return request.model_copy(update={"course": course, "current_step": step})

    def _find_course(self, course_id: str) -> StoredCourse | None:
        requested = course_id.casefold()
        for stored in self._courses:
            identifiers = [stored.course.course_id, *stored.aliases]
            if requested in {item.casefold() for item in identifiers}:
                return stored
        return None

    @staticmethod
    def _find_lesson(
        stored: StoredCourse, requested: LessonContext
    ) -> StoredLesson | None:
        lesson_id = requested.lesson_id.casefold()
        for lesson in stored.lessons:
            identifiers = [lesson.context.lesson_id, *lesson.aliases]
            if lesson_id in {item.casefold() for item in identifiers}:
                return lesson
        title = requested.title.casefold()
        for lesson in stored.lessons:
            if lesson.context.title.casefold() == title:
                return lesson
        return None


def _merge_lesson(stored: LessonContext, supplied: LessonContext) -> LessonContext:
    instructions = list(dict.fromkeys([*stored.instructions, *supplied.instructions]))[:20]
    success_criteria = list(
        dict.fromkeys([*stored.success_criteria, *supplied.success_criteria])
    )[:20]
    failures = list(
        {item.code: item for item in [*stored.common_failures, *supplied.common_failures]}.values()
    )[:12]
    return stored.model_copy(
        update={
            "instructions": instructions,
            "expected_result": stored.expected_result or supplied.expected_result,
            "success_criteria": success_criteria,
            "common_failures": failures,
        }
    )


def _unique_excerpts(excerpts: list[CourseExcerpt]) -> list[CourseExcerpt]:
    unique: list[CourseExcerpt] = []
    seen: set[tuple[str, str]] = set()
    for excerpt in excerpts:
        key = (excerpt.title, excerpt.content)
        if key not in seen:
            seen.add(key)
            unique.append(excerpt)
    return unique
