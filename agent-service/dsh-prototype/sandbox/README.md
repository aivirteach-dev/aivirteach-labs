# 可删除的模拟测试区

fixtures：虚构课程与固定测试题。
hello.py：简单连接测试，命令为 `python -m sandbox.hello`。
run_course.py：课程测试，命令为 `python -m sandbox.run_course`。
tests：自动化边界测试。
results：回答、人工复核结果、DSH 运行数据；legacy_home 是之前测试的运行数据。

所有命令应在项目根目录执行，使用 `.venv\Scripts\python.exe`。

以后可以整体删除此目录，不需要在可复用代码中移除模拟课程逻辑。删除会同时移除本地测试和测试记录，先按需要归档证据。正式服务应自行提供 CourseQuestion 和隔离运行目录，见 ../docs/integration.md。这里没有真实课程或学生数据。
