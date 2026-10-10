"""主链路编排:Agno 原生 Turn Workflow 装配(S1 组装 → S2 Gate → S3 分支 → S4 finalize)。

自建代码只存在于官方扩展点之内(executor/evaluator)与 §3.5 审计表登记的四类位置(解耦纪律④)。
切片 3 起填充 turn_workflow.py / assembler.py / gate.py / branches.py / finalize.py。
"""
