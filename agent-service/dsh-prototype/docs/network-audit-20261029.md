# Ubuntu 网络基线核对 · 2026-10-29

本次只读取用户提供的 nft 规则、UFW 状态、监听地址和 Ollama 版本，没有修改网络、防火墙、Docker 或SSH配置。

- UFW状态为inactive；不代表所有容器防火墙规则不存在。
- nft快照包含Docker的NAT和转发规则。5678/5679监听127.0.0.1，规则中也存在本机地址对应的转发；不能据此证明其他Ubuntu账号无法访问。
- 未看到针对 dsh-runner 的独立输出过滤规则；目前不能声称其仅能连接Ollama。
- SSH连接已用于本次检查；Ubuntu可访问Mac Ollama版本接口。模型回答和账号检查见先前报告。
- 按用户明确选择保留复制粘贴和拖放；截图显示共享文件夹已禁用。VM并非完全隔离。

第一次MCP工具仅处理虚构数据，不开放任意命令、文件或网络工具。需要真实联网工具或生产部署前，应制定与测试账号网络边界，并逐一核对SSH、Docker和模型连接，不能直接假定UFW会覆盖Docker网络。

机器可读摘要见 development/network-audit-20261029.json；原始规则快照位于 development/network-rules-20261029.txt。
