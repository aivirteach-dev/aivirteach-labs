"""虚构环境后端；不读真实文件、不连接Docker、VM或网络。"""
import asyncio


class MockExecutor:
    def __init__(self, states: dict, *, failure=False, delay=0):
        self.states, self.failure, self.delay = states, failure, delay
        self.executions = 0

    async def __call__(self, service, policy):
        self.executions += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise RuntimeError('模拟后端故障')
        return {'state': self.states[service], 'source': 'simulation',
                'note': '虚构检查结果；未连接真实实验环境。'}
