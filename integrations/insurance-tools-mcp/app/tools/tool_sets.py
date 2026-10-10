"""Shared tool domains; backend operations remain separately accounted."""
import re
RAG_TOOLS = frozenset({"product_knowledge", "benefit_info", "general_qa"})
STATE_TOOLS = frozenset({"ask_clarify", "run_policy_snapshot", "run_policy_field_lookup"})

POLICY_ESCALATION_TOOLS = frozenset({"policy_detail", "product_knowledge", "entity_lookup", "ask_clarify"})


def route_tool_subset(state: dict | None) -> frozenset[str] | None:
    """Provider projection for a resolved single-domain escalation.

    None keeps the complete registry. Composite/unknown routes never
    lose advertised tools, and this does not impose an execution admission rule.
    """
    route = (state or {}).get("route")
    if not isinstance(route, dict) or route.get("escalate") is not True:
        return None
    query = str(route.get("user_text") or "")
    if re.search(r"同时|另外|还有|分别|并且|对比|比较|区别", query):
        return None
    decision = route.get("decision")
    if not isinstance(decision, dict):
        return None
    args = decision.get("resolved_args")
    if decision.get("action") == "DIRECT_RAG" and isinstance(args, dict) and args.get("tool") in RAG_TOOLS:
        from app.core.resolve import POLICY_NO_RE, normalize_policy_text
        if POLICY_NO_RE.search(normalize_policy_text(query)) or args.get("policy_no"):
            return POLICY_ESCALATION_TOOLS | RAG_TOOLS
        context = route.get("direct_agent_context") or {}
        facts = context.get("facts") or []
        if context.get("status") == "PARTIAL" and any(
            isinstance(fact, dict) and fact.get("predicate") in {"reference_content", "qa_answer"}
            and fact.get("typed_value") for fact in facts
        ):
            return frozenset({args["tool"], "entity_lookup", "ask_clarify"})
        alternatives = RAG_TOOLS - {args["tool"]}
        from app.conf import entity_dict
        try:
            has_product = bool((state or {}).get('entities', {}).get('active_products') or
                               context.get('product_name') or entity_dict().product_name_spans(query, max_matches=1))
        except Exception:
            has_product = False
        if not has_product:
            alternatives -= {'product_knowledge'}
        return alternatives | {"entity_lookup", "ask_clarify"}
    if decision.get("action") in {"WORKFLOW_START", "WORKFLOW_CONTINUE"} and decision.get("capability_id") == "policy-field-lookup":
        return POLICY_ESCALATION_TOOLS | {"customer_search", "run_policy_field_lookup"}
    if decision.get("action") != "DIRECT_TOOL":
        return None
    args = decision.get("resolved_args")
    if not isinstance(args, dict) or args.get("tool", "policy_detail") != "policy_detail":
        return None
    if not isinstance(args.get("policy_no"), str) or not args["policy_no"].strip():
        return None
    fields = args.get("fields")
    if not isinstance(fields, list) or not fields or not all(isinstance(f, str) and f for f in fields):
        return None
    return POLICY_ESCALATION_TOOLS
