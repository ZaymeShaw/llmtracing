# insurance_eval_branch.py (shim)

Ownership moved to the insurance side:

- Umbrella (agent + MCP): `/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/scripts/insurance_upstream_align.py`
- Config: `/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/scripts/insurance_upstream_align.json`
- Eval-branch tool: `/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/scripts/insurance_eval_branch.py`
- Full README: `/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/scripts/insurance_eval_branch.README.md`

This `mock_system/scripts/` copy is a thin shim that `runpy`s the insurance-side script so existing paths keep working.
