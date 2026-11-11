# Server 未进入第一课时仍可询问 Agent：实施交接说明

> 将本文直接交给负责 `aivirteach-server` 的 Agent 执行。目标是：用户已经报名并且 Workspace/VM 正常运行时，即使尚未进入第一课时、没有 Progress 记录或课时指针为空，也必须正常调用 Labs Agent 回答问题。

## 1. 先确认操作的是正确代码

- 目标仓库：`AIVirTeach/aivirteach-server`
- 目标分支：远端最新 `main`
- 当前线上代码是 monorepo，目标文件为：
  - `apps/api/src/chat/chat.service.ts`
  - `apps/api/src/chat/chat.service.spec.ts`
- 不要修改旧版 `src/chat/chat.service.ts`。旧 `local-demo` checkout 与当前 Vercel 线上结构不同。
- 开始前执行 `git fetch origin main`，基于最新 `origin/main` 建分支；不要覆盖已有未提交改动。

## 2. 根因

问题不在 Agent Service。

当前 Server 的 `ChatService.resolveDiagnoseRequest()` 在查询 Progress 后，存在类似逻辑：

```ts
if (!progress?.currentLessonContentId) {
  return {
    ok: false,
    fallbackMessage: '还没有开始学习课程内容，请先进入第一课时。',
  };
}
```

同步 `sendMessage()` 和流式 `streamMessage()` 都先调用 `resolveDiagnoseRequest()`。当它返回 `ok: false` 时，Server 会直接落库并返回这句 fallback，`AgentClient.diagnose()` / `diagnoseStream()` 根本不会被调用。因此修改 Agent prompt 或重启 Agent 都无法解决。

## 3. 必须实现的修改

### 3.1 缺少进度指针时继续构建诊断上下文

删除“没有 `currentLessonContentId` 就直接拒绝”的分支，始终调用 `buildDiagnoseContext()`：

```ts
const progress = await this.prisma.progress.findUnique({
  where: { enrollmentId },
});

const context = await this.buildDiagnoseContext(
  courseId,
  progress?.currentLessonContentId ?? null,
);
```

### 3.2 让上下文构建函数接受空指针

把签名改为：

```ts
private async buildDiagnoseContext(
  courseId: string,
  currentLessonContentId: string | null,
)
```

课程和最新发布版本加载完成后，按模块/课时的 `position` 顺序拉平课程。`LATEST_PUBLISHED_VERSION` 已经按位置排序，无需另写排序逻辑。

选择课时的规则：

```ts
const flattened = version.modules.flatMap((courseModule) =>
  courseModule.lessons.map((lesson) => ({ lesson, courseModule })),
);

if (!flattened.length) return null;

const index = currentLessonContentId
  ? flattened.findIndex(
      ({ lesson }) => lesson.contentId === currentLessonContentId,
    )
  : 0;

if (index < 0) return null;
```

含义：

- 有有效课时指针：仍使用当前课时。
- 没有 Progress 记录或指针为 `null`：使用最新发布课程的第一课作为默认上下文，`sequence = 1`。
- 第一课只是 Agent 回答所需的课程背景，不代表用户已经开始学习，也不要创建或推进 Progress。
- 课程没有已发布版本、没有课时、指针失效或课时数据库行缺失：返回 `null`，不要抛出未处理异常。

### 3.3 调整真正的内容不可用 fallback

如果 `buildDiagnoseContext()` 返回 `null`，不要再使用“尚未开始课程”的文案。改为准确描述系统状态，例如：

```ts
return {
  ok: false,
  fallbackMessage: '课程内容暂时不可用，请稍后再试。',
};
```

### 3.4 保留现有 VM 门槛

以下逻辑不属于本次修改范围，必须保留：

```ts
if (!workspace?.labId || workspace.status !== WorkspaceStatus.RUNNING) {
  return { ok: false, fallbackMessage: '请先启动虚拟机后再提问。' };
}
```

本需求只解除“必须先进入第一课时”的门槛，不解除 Workspace/VM 运行要求。

## 4. 必须补充或调整的测试

目标测试文件：`apps/api/src/chat/chat.service.spec.ts`。

### 4.1 普通聊天路径

分别覆盖：

1. `prisma.progress.findUnique()` 返回 `null`。
2. 返回 `{ currentLessonContentId: null }`。

两个场景都应断言：

- `agentClient.diagnose()` 被调用。
- 请求中的 `current_step` 是最新发布课程的第一课。
- `current_step.sequence === 1`。
- `lesson_id`、`module_id` 和标题来自第一课。
- 最终 Tutor 消息使用 Agent 返回的答案。
- 不再返回“还没有开始学习课程内容，请先进入第一课时。”。

### 4.2 流式聊天路径

至少增加一个尚无 Progress 的场景，断言：

- `agentClient.diagnoseStream()` 被调用。
- 请求使用第一课上下文且 `sequence === 1`。
- 收到 Agent 的 `result` 后正常落库并产生 `complete` 事件。

### 4.3 边界场景

保留或新增以下断言：

- 最新发布课程没有任何课时：不抛异常，返回“课程内容暂时不可用”。
- 非空指针在最新发布版本中不存在：返回“课程内容暂时不可用”，不调用 Agent。
- 找到课程树中的课时，但查询不到对应 `CourseLesson` 行：返回“课程内容暂时不可用”。
- Workspace 不存在或状态不是 `RUNNING`：仍返回 VM fallback，不调用 Agent。

旧测试中“无 Progress 时不调用 Agent”的断言必须删除或改写，因为它正是本次需要修复的错误行为。

## 5. 验收标准

- Server 源码和测试中不再包含固定文案：

  ```text
  还没有开始学习课程内容，请先进入第一课时。
  ```

- 未开始课程但 Workspace 正常运行时，同步和流式接口都会到达 Agent Client。
- Server 不创建、不修改学习 Progress；只使用第一课作为临时问答上下文。
- 已经开始学习的用户仍使用真实 `currentLessonContentId`。
- 无课程内容时不会因访问 `flattened[0]` 而抛异常。
- 所有现有测试和新增测试通过。

## 6. 建议验证命令

在 `aivirteach-server` monorepo 根目录执行：

```bash
npm ci
npm test -w api -- --runInBand chat/chat.service.spec.ts
npm test -w api -- --runInBand
npm run build -w api
npx prettier --check \
  apps/api/src/chat/chat.service.ts \
  apps/api/src/chat/chat.service.spec.ts
git diff --check
```

参考验收结果：

- ChatService 专项：28 个测试通过。
- API 全量：66 个测试套件、622 个测试通过。
- API build 和 Prettier 通过。

如果 ESLint 9 提示找不到 `eslint.config.*`，这是当前仓库配置问题，不要为本次修复顺带大范围迁移 ESLint；记录该限制即可。

## 7. 部署注意事项

- 修改的是 `aivirteach-server`，不是 Labs Agent、Client、VM Manager 或 Diagnostic Gateway。
- 合并并推送 Server 后，需要触发一次 Vercel 部署；无需手动重启 Agent Service。
- 仅重启现有线上 Server、但不部署新代码，不会生效。
- 数据库中已经保存的旧 Tutor 消息不会自动改变；部署后用一条新问题验证。
- 部署后验证：使用已报名、Workspace 为 `RUNNING`、但尚无 Progress/课时指针的账号发送问题，应看到 Agent 正常回答，并确认 Server 日志中发生了 Agent 调用。

## 8. 当前已有实现（可选参考）

本机已基于线上 `origin/main` 创建独立工作树和分支，避免覆盖旧 `local-demo` 工作区中的未提交改动：

```text
工作树：/home/arclab/Desktop/aivirteach/aivirteach-server-main-fix
分支：fix/prelesson-agent
```

该工作树中的实现和测试已经达到上面的参考验收结果，但尚未提交、推送或部署。
