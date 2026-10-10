"""平安寿险通用问答智能体——应用工程根包。

分层(方案 v13 §19.1):schemas(契约)← core(纯函数)← tools ← runtime ← orchestration ← api,
依赖只准向下(tests/test_decouple.py 静态强制)。
conf/ 为版本化配置数据({组}/{名}_{ENV_TYPE}_args.yaml 分环境布局),按切片需要再建,不预建空目录。
"""
