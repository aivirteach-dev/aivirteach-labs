"""串行运行三个模拟课程问题，将回答保存给人工复核。"""
import json
from datetime import datetime
from pathlib import Path
from dsh_tutor.contracts import CourseQuestion, CourseSnapshot
from dsh_tutor.course import answer_course

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main():
    course = CourseSnapshot.model_validate_json((HERE / "fixtures/course.json").read_text(encoding="utf-8-sig"))
    questions = json.loads((HERE / "fixtures/questions.json").read_text(encoding="utf-8-sig"))
    report = {"note": "人工复核语义；completed 仅表示模型生成完成。", "cases": []}
    output_dir = HERE / "results/runner-check"
    report_path = output_dir / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-course.json")
    for case in questions:
        print(f"正在测试 {case['id']}：{case['question']}", flush=True)
        request = CourseQuestion(request_id=case["id"], course=course, question=case["question"])
        try:
            result = answer_course(request, config_path=ROOT / "config.json", state_dir=output_dir / "runtime")
            row = {**case, "result": result, "manual_review": "待人工复核"}
            print(result["answer"], flush=True)
        except Exception as exc:
            row = {**case, "error": str(exc), "manual_review": "运行失败"}
            print("测试失败：", exc, flush=True)
        report["cases"].append(row)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("结果文件：", report_path)
    if any("error" in row for row in report["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
