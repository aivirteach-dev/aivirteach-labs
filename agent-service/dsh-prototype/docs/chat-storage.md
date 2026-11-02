# 第一步：可靠聊天存储与恢复

本轮新增dsh_tutor/storage.py的ChatStore行为接口、SQLiteChatStore本地适配器与可配置StorageLimits。Python标准库sqlite3，无新增依赖。原有Agent入口没有自动保存或自动恢复逻辑，第二步再接；摘要、偏好提取、队列和平台接口均未实现。

## 如何调用

```python
from dsh_tutor.storage import SQLiteChatStore

# 由可信应用准备私有目录、路径、身份和范围；不能直接采用学生提交的身份。
# create只用于不存在的新文件；日常重启用SQLiteChatStore(path)，不会悄悄新建丢失的库。
store = SQLiteChatStore.create(private_database_path)
handle = store.activate(trusted_conversation_scope)
record = store.begin(handle, request_id, question)
# 应用检查record.status：重复请求返回原记录，不应据此重复运行模型。
finished = store.finish(handle, request_id, answer=final_answer,
                        answer_status="completed", run_id=run_id)
page = store.read_page(handle, limit=20)
```

重新打开时：`store = SQLiteChatStore(path)`，再用`current(student_id=..., learning_context_id=...)`取当前ScopeHandle。独立应用层应管理数据库文件权限，不向MCP或模型开放此接口。持有整个数据库文件的程序仍能读取文件；本模块不提供登录认证、远端授权或OS沙箱。

config/storage.example.json是可信应用的配置样例，不会自行创建data/chat/chat.sqlite3；使用时传入limits和路径，父目录须由应用建立为私有目录。当前仅创建sandbox中的虚构测试数据库，未创建真实学生库。

## 范围、顺序和代次

复用ConversationScope：学生、学习记录、课程、版本、课时、实例、实例代次、操作系统。实例与代次必须同时提供或同时为null。学习记录ID不可被其他学生抢占；各读取/写入/删除操作都检查完整范围。身份本身仍须由Server核实。

ScopeHandle的revision是本存储的范围修订号，与VM的instance_generation不同：课程、课时或其他范围变化也会产生新revision。activate切换时必须传expected_revision，防止过时调用覆盖新范围；相同范围和正确修订号不会新建范围。切换原子地取消旧pending，新范围不自动带入旧消息。

旧ScopeHandle可供可信应用显式查看/清理自己范围的历史；begin/finish必须使用当前激活修订。旧请求晚到时拒绝完成写入，不将结果转移到新环境。接口不自行探测VM状态，真实环境代次依赖平台提供。

sequence由数据库分配，按接受请求的顺序排序，不靠时间字符串排序。每个学习记录最多一个pending，其他新请求明确返回request_in_progress；不是生产全局队列。同请求编号与相同内容重试返回已有记录，内容/修订冲突则拒绝。已完成或失败的终态只能幂等重放，不能覆盖。

## 保存状态和中断

- pending：问题已持久保存，尚无正式回答。
- completed：最终回答已保存；answer_status另外记录completed/partial，存储成功不等于回答全正确。
- failed：可信应用记录interrupted/model_failed/timeout/cancelled/application_failed等有限错误码，不存原始异常文本。
- cancelled：范围切换或显式删除导致取消。

程序意外退出后，已提交问题/回答可恢复；未提交事务回滚。pending保持pending，不能因某个读者重开数据库就判定其他工作进程已死。应用确认中断后调用finish(error_code='interrupted')；本轮不自动重试模型，不保存半截流式草稿。

每次操作使用短连接、参数化SQL、外键和显式事务；写操作BEGIN IMMEDIATE，限制锁等待，正常关闭连接。SQLite使用DELETE回滚日志与synchronous=FULL；此为单机原型选择，不声称适合服务器大并发或所有文件系统的断电可靠性。

## 读取与删除

read_page是查看记录的接口，包含pending/failed状态，不直接生成ConversationContext。默认每页最多20条，可配置至100；页内升序，以next_before向旧记录翻页，不无限读全文。第二步才会筛选可用的完整问答并接入模型预算。

delete_history仅由可信应用显式调用，在指定修订范围内清空消息文本并隐藏记录；保留不含正文的请求编号墓碑，防止晚到重试重新生成已删除消息。重复清理返回0。它不是安全擦盘，空闲数据库页、旧备份仍可能有旧字节；备份、物理清理、身份/学习记录整体注销及生产保留期限另定。墓碑仍计入容量，不能依靠删除绕过上限。

## 资源边界与错误

默认锁等待1000毫秒，数据库主页容量64MiB，每学习记录10000轮、1000个学习记录，每页20条。数字是原型可调初始边界，不是硬件容量测量或生产人数承诺。单条问题最多2000字符，回答最多16000字符，保留原文，不为适配历史输入而截断保存。

SQLite max_page_count限制数据库主页，临时回滚日志、目录中其他文件和备份不计入这项上限；不是整个磁盘配额。每连接约1MiB SQLite页缓存，不是进程总内存上限。达到上限明确失败，不自动删记录、摘要或调用模型。全局磁盘监测与调度另行实现。

StorageError.code提供scope_denied、stale_scope、revision_conflict、request_conflict、request_in_progress、request_not_found、terminal_conflict、invalid_input、invalid_scope、already_exists、unsupported_schema、busy、capacity、corrupt、unavailable等原因码。错误不输出SQL、路径或消息内容；应用应另外关联请求编号和阶段，不把内部异常完整发给学生。

## 验收与维护

自动测试sandbox/tests/test_storage.py使用临时虚构数据库，覆盖新进程恢复、突然退出、未提交回滚、隔离、幂等/并发、范围切换、分页、逻辑删除、只读/损坏/锁等待和容量拒绝。sandbox/storage_acceptance.py会保留一份虚构数据库和6例报告，复核记录写入development/storage-step1-20261030.json。运行不需要sudo，不启动DSH或Mac模型；它是应用层存储验收，DSH受限账号规定继续适用于模型运行。

不能把本轮完成称为学生界面或模型已经自动恢复聊天。备份不得直接复制活动数据库文件；后续提供备份功能时使用SQLite backup接口并测试恢复。删除sandbox前保留重要接口回归测试，虚构数据库和测试结果可按约定清理；真实数据未来放私有应用数据目录，不放sandbox或公开看板。

资料：[Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html)、[SQLite PRAGMA](https://www.sqlite.org/pragma.html)。

## 本轮结果

100项完整自动回归通过，其中30项存储检查；保留虚构数据库的6例验收通过，源码与测试指纹匹配。未调用Mac模型，未请求sudo，未修改AIVirTeach参考源码。第二步自动衔接问答尚未开始。

## 第二步接口补充

ChatStore新增page_capacity与accept（turn/created），begin委托同一实现保留第一步行为。只有created为true的调用者获得执行资格。新answer_chat负责自动恢复和保存，详见persistent-chat.md；上文未自动接入属于第一步历史范围。SQLite schema仍为v1。
