"""MCP stdio 桥接；不开放网络端口，不引用任何模拟后端。"""
import asyncio
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from .tools import TOOL_NAME, ToolGateway


class BridgeSpec(BaseModel):
    """由应用提供的可信启动说明，绝不从模型工具参数构造。"""
    model_config = ConfigDict(extra='forbid')
    entry_module: str = Field(pattern=r'^[a-zA-Z_][a-zA-Z0-9_.]+$')
    server_name: str = Field(default='course_tools', pattern=r'^[A-Za-z0-9_-]{1,32}$')
    context: dict[str, Any]


async def serve(gateway: ToolGateway):
    """使用官方 SDK 处理协议；所有调用在网关中先计数再校验。"""
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent, CallToolResult
    import json
    server = Server('course-tools')

    @server.list_tools()
    async def list_tools():
        gateway.audit.append({'type': 'discovery', 'tools': [TOOL_NAME]})
        return [Tool(name=TOOL_NAME,
                     description='读取本轮授权服务的检查状态。返回数据来源及证据；模拟数据不能声称真实访问。',
                     inputSchema={'type': 'object', 'properties': {'service': {'type': 'string', 'maxLength': 64}},
                                  'required': ['service'], 'additionalProperties': False})]

    # 禁止 SDK 在网关之前丢弃畸形参数，否则无效尝试无法计入统一预算。
    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        row = await gateway.call(name, arguments or {})
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(row, ensure_ascii=False))],
                              isError=row['status'] != 'ok')

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_stdio(gateway: ToolGateway):
    """专供受控进程入口调用；stdout 只用于 MCP 协议。"""
    asyncio.run(serve(gateway))
