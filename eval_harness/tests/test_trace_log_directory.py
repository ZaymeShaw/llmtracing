import errno
import json
from pathlib import Path

import pytest

from callbacks import trace_callback


def test_missing_log_directory_is_restored_with_unchanged_record(tmp_path, monkeypatch):
    log = tmp_path / 'removed' / 'gateway' / 'llm_calls.jsonl'
    monkeypatch.setattr(trace_callback, 'LOG_FILE', log)
    monkeypatch.setenv('LLM_GATEWAY_NAME', 'directory-recovery-test')
    record = {'event': 'stream', 'call_id': 'call-1', 'delta': {'text': '原始文本'}, 'path': '/v1/messages'}
    trace_callback._append(record)
    assert json.loads(log.read_text()) == {**record, 'gateway': 'directory-recovery-test'}


def test_directory_removed_after_startup_can_record_new_events(tmp_path, monkeypatch):
    log = tmp_path / 'gateway' / 'llm_calls.jsonl'
    monkeypatch.setattr(trace_callback, 'LOG_FILE', log)
    monkeypatch.delenv('LLM_GATEWAY_NAME', raising=False)
    trace_callback._append({'event': 'request', 'call_id': 'old'})
    log.unlink()
    log.parent.rmdir()
    new_record = {'event': 'response', 'call_id': 'new', 'response': {'content': '真实回复'}}
    trace_callback._append(new_record)
    assert log.read_text().splitlines() == [json.dumps(new_record, ensure_ascii=False)]


@pytest.mark.parametrize('code', [errno.EACCES, errno.ENOSPC])
def test_other_storage_errors_are_not_silenced(tmp_path, monkeypatch, code):
    monkeypatch.setattr(trace_callback, 'LOG_FILE', tmp_path / 'llm_calls.jsonl')

    def fail_open(*args, **kwargs):
        raise OSError(code, 'storage error')

    monkeypatch.setattr(Path, 'open', fail_open)
    with pytest.raises(OSError) as raised:
        trace_callback._append({'event': 'request', 'call_id': 'failed'})
    assert raised.value.errno == code
