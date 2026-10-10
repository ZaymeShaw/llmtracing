"""工具层:call_backend 单点 RPC 计量、统一工具信封、async tool_hooks、域工具。

模型可见工具严格采用「一个工具一个同名 .py 文件」；backend/hooks
属于共享基础设施，不是模型可见工具。工具保持无状态、可单测，组合只发生在
业务 Workflow 或 Unified Agent 层。
"""
