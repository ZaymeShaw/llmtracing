"""Recover observed conversations from protocol history, without runner artifacts."""
from __future__ import annotations

from eval_harness.protocol_trace import text_content


def observed_turns(calls):
    """Select the most-observed conversation; retain all calls in the report.

    Prefixes preserve repeated prompts at different positions. Tool-result messages
    are continuations, not user turns. Responses' previous_response_id links extend
    history when the client only sends the new input. Separate history roots (e.g.
    a title generator) must not become turns of the main conversation.
    """
    conversations = []
    responses = {}
    for call in calls:
        users = []
        for msg in call.get("messages") or []:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", msg.get("text", ""))
            if isinstance(content, list):
                content = [b for b in content if not isinstance(b, dict) or b.get("type") != "tool_result"]
                if not content:
                    continue
            text = text_content(content)
            users.append((text, text))
        parent = responses.get(call.get("previous_response_id"))
        if parent is not None:
            group, history = parent
            users = history + users
        else:
            group = next((g for g in conversations if users and g["history"] and
                          (users[:len(g["history"])] == g["history"] or
                           g["history"][:len(users)] == users)), None)
            if group is None:
                group = {"history": [], "turns": [], "calls": []}
                conversations.append(group)
        group["calls"].append(call)
        if len(users) > len(group["history"]):
            group["history"] = users
            for _, prompt in users[len(group["turns"]):]:
                group["turns"].append({"index": len(group["turns"]) + 1,
                                       "prompt": prompt, "final_text": ""})
        # A failed/pending call must not erase the previous observed response.
        if users and call.get("assistant"):
            group["turns"][len(users) - 1]["final_text"] = call["assistant"]
        if call.get("response_id"):
            responses[call["response_id"]] = (group, users)
    if not conversations:
        return [], [], 0
    main = max(conversations, key=lambda g: (len(g["calls"]), len(g["history"])))
    return main["turns"], main["calls"], len(conversations) - 1
