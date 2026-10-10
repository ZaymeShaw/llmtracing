"""Evidence that the experimental codec preserves data and fails closed."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from experiment_storage_lossless import decode_file, decode_line, digest, encode_file, encode_line, virtual_reader


def record():
    messages = [{"role": "user", "content": "客户保单 /Users/真实路径 </script> \\n \u2028"}]
    tools = [{"name": "mcp_policy", "schema": {"required": ["保单"], "path": "../原始路径"}}]
    return {"call_id": "test", "request": {"messages": messages, "tools": tools,
            "client_request": {"messages": messages, "path": "/v1/chat/completions"},
            "optional_params": {"tools": tools}},
            "response": {"messages": messages, "tools": tools, "response": {"content": "真实回复", "usage": 123}}}


class LosslessStorageTests(unittest.TestCase):
    def roundtrip(self, row):
        raw = json.dumps(row, ensure_ascii=False) + "\n"
        encoded, refs = encode_line(raw)
        self.assertEqual(decode_line(encoded), raw)
        self.assertEqual(json.loads(decode_line(encoded)), row)
        return refs

    def test_four_exact_copies_paths_tools_actual_reply(self):
        self.assertEqual(self.roundtrip(record()), [0, 1, 2, 3])

    def test_distinct_request_and_response_values_retained(self):
        row = copy.deepcopy(record())
        row["request"]["client_request"]["messages"] = [{"content": "不同上游输入"}]
        row["response"]["messages"] = [{"content": "不同响应"}]
        self.assertEqual(self.roundtrip(row), [1, 3])

    def test_strict_types_and_order_not_python_equality(self):
        row = record()
        row["request"]["messages"] = [True, 1, {"b": 2, "a": 1}]
        row["request"]["client_request"]["messages"] = [1, 1.0, {"a": 1, "b": 2}]
        self.assertNotIn(0, self.roundtrip(row))

    def test_absent_null_failure_partial(self):
        for row in ({"request": None, "response": {"error": "timeout"}},
                    {"request": {"messages": None}}, {"call_id": "only-pre"},
                    {"request": {"messages": []}, "response": None}):
            self.assertEqual(self.roundtrip(row), [])

    def test_retries_remain_distinct_no_cross_call_sharing(self):
        first, retry = record(), record()
        first["error"], retry["call_id"] = "failed", "retry-2"
        raw = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in (first, retry)).encode()
        encoded, _ = encode_file(raw)
        self.assertEqual(len(encoded.splitlines()), 2)
        self.assertEqual(decode_file(encoded), raw)

    def test_whitespace_order_line_endings_and_blank_lines(self):
        raw = b'  {"request": {"messages": [1,  2], "client_request": {"messages": [1,  2]}}} \r\n\n{"response":null}'
        encoded, _ = encode_file(raw)
        self.assertEqual(decode_file(encoded), raw)

    def test_real_marker_like_content_untouched(self):
        row = record()
        row["literal"] = {"version": 1, "refs": [0], "data": None, "$ref": "request.messages"}
        self.roundtrip(row)

    def test_duplicate_keys_cannot_be_used_as_reference(self):
        raw = '{"request":{"messages":[1,2],"messages":[3,4],"client_request":{"messages":[3,4]}}}\n'
        encoded, refs = encode_line(raw)
        self.assertEqual(refs, [])
        self.assertEqual(decode_line(encoded), raw)

    def test_corrupt_data_hash_or_reference_rejected(self):
        encoded, _ = encode_line(json.dumps(record(), ensure_ascii=False) + "\n")
        for corrupt in (encoded.replace("真实回复", "损坏回复"), encoded.replace('"refs":[0,1,2,3]', '"refs":[99]')):
            with self.assertRaises(ValueError):
                decode_line(corrupt)

    def test_reader_uses_persisted_file_and_does_not_hide_new_legacy_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = (json.dumps(record(), ensure_ascii=False) + "\n").encode()
            encoded, _ = encode_file(raw)
            (root / "compact").write_bytes(encoded)
            entries = {"llm_calls.jsonl": {"kind": "call_fields", "sources": ["compact"], "bytes": len(raw), "sha256": digest(raw)}}
            with virtual_reader(root, entries):
                target = root / "llm_calls.jsonl"
                self.assertTrue(target.exists())
                self.assertTrue(target.is_file())
                self.assertEqual(target.read_bytes(), raw)
                self.assertEqual(target.read_text(), raw.decode())
                target.write_text("new legacy file\n")
                self.assertEqual(target.read_text(), "new legacy file\n")
            (root / "llm_calls.jsonl").unlink()
            self.assertFalse((root / "llm_calls.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
