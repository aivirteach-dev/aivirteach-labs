"""模型提供方契约：语义输入不变，协议差异集中在此；能力未知绝不猜测。"""
import os
from urllib.parse import urlsplit
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator

class Capabilities(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    tools:Literal['supported','unsupported','unknown']='unknown'
    reasoning:Literal['supported','unsupported','unknown']='unknown'
    structured_output:Literal['supported','unsupported','unknown']='unknown'
    token_count:Literal['supported','unsupported','unknown']='unknown'
    usage:Literal['supported','unsupported','unknown']='unknown'
    remote_cancel:Literal['unsupported','unknown']='unknown'
    # 本版通用HTTP适配器没有带凭证的远端停止回执，不能声明supported。
    concurrency:int | None=Field(default=None,ge=1,le=64)

class ProviderSpec(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    id:str=Field(pattern=r'^[a-z][a-z0-9-]{0,47}$')
    kind:Literal['ollama','openai_compatible']
    base_url:str=Field(max_length=2048)
    auth_env:str | None=Field(default=None,pattern=r'^[A-Z][A-Z0-9_]{0,95}$')
    max_tokens_field:Literal['max_tokens','max_completion_tokens']='max_tokens'
    developer_role:bool=False
    capabilities:Capabilities=Field(default_factory=Capabilities)

    @model_validator(mode='after')
    def address(self):
        u=urlsplit(self.base_url)
        try:port=u.port
        except ValueError:raise ValueError('provider_address') from None
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment or u.path!='/v1':
            raise ValueError('provider_address')
        if self.kind=='ollama' and self.auth_env is not None:raise ValueError('ollama_auth_not_supported')
        if self.kind=='openai_compatible' and self.auth_env is None:raise ValueError('provider_auth_env_required')
        if self.auth_env and self.auth_env.startswith(('OLLAMA_PLACEHOLDER','DSH_')):raise ValueError('reserved_auth_env')
        return self

    def environment(self):
        """密钥只从宿主环境读取；不写进配置、patch、诊断或错误文本。"""
        if self.auth_env:
            value=os.environ.get(self.auth_env)
            if not value or len(value)>8192 or any(c in value for c in '\r\n'):raise ValueError('provider_auth_unavailable')
            return {self.auth_env:value}
        return {'OLLAMA_PLACEHOLDER_KEY':'local-ollama'}

    def public(self):
        # 管理界面无需完整地址，避免泄露内网拓扑；环境变量名不是密钥。
        return {'id':self.id,'kind':self.kind,'protocol':'openai-completions','auth_env':self.auth_env,
                'capabilities':self.capabilities.model_dump(),'validation':'configuration_only; real_backend_acceptance_required'}

def provider_for(config):
    if config.get('provider') is not None:return ProviderSpec.model_validate(config['provider'])
    return ProviderSpec(id='ollama-local',kind='ollama',base_url=config['ollama_base_url'])

def provider_patch(config):
    """只替换DSH的提供方注册，不改课程、记忆、工具和预算的业务输入。"""
    p=provider_for(config)
    model={'id':config['model'],'name':config['model'],'api':'openai-completions',
           'contextWindow':config['context_window'],'maxTokens':config['max_output_tokens'],
           'compat':{'supportsDeveloperRole':p.developer_role,'maxTokensField':p.max_tokens_field}}
    if config.get('reasoning_effort')=='off':model['reasoningEfforts']={'off':config['reasoning_off_value'],'low':'low'}
    return {'id':'llm-pi-ai','name':'@deepseek-ai/dsh-llm-pi-ai','config':{'providers':{p.id:{
        'displayName':p.id,'apiKeyEnv':p.auth_env or 'OLLAMA_PLACEHOLDER_KEY','api':'openai-completions',
        'baseURL':p.base_url,'defaultContextWindow':config['context_window'],
        'defaultMaxTokens':config['max_output_tokens'],'retryPolicy':{'mode':'normal','maxRetries':0},'models':[model]}}}}
