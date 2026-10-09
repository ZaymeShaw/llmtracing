#!/usr/bin/env python3
"""Known-good and deliberately corrupted artifacts, separate from product scores."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from run_pilot import FAKE_KEY, grade, dump


def known_good():
    identity = {'run_id': 'oracle-run', 'case_id': 'oracle-case', 'harness': 'build-eval-pilot'}
    expected = {'identity': identity, 'executions': {'E1': 1, 'E2': 1},
                'answers': ['ANSWER:PUBLIC_MARKER'], 'answers_by_execution': {'E1': ['ANSWER:PUBLIC_MARKER'], 'E2': []},
                'failure_contract': {'status_codes': [400], 'reason': 'CONTROLLED_UPSTREAM_ERROR'},
                'states': {'succeeded': 1, 'failed': 1, 'pending': 0}, 'secret': True}
    observation = {'envelope': {'calls': [
        {**identity, 'call_id': 'C1', 'execution_id': 'E1', 'state': 'succeeded',
         'usage': {'prompt_tokens': 11, 'completion_tokens': 7}},
        {**identity, 'call_id': 'C2', 'execution_id': 'E2', 'state': 'failed', 'usage': None,
         'status_code': 400, 'error': 'CONTROLLED_UPSTREAM_ERROR'}]},
        'payload': {'cases': [
            {'identity': {**identity, 'execution_id': eid}, 'n_calls': 1,
             'calls': [{'call_id': cid, 'assistant': answer, 'model': 'trace-fixture', 'system': 'SYSTEM',
                        'ts_end': '2026-10-01T00:00:01Z',
                        'error': 'CONTROLLED_UPSTREAM_ERROR' if eid == 'E2' else None}],
             'overview': {'success': None, 'turns': [{'final_text': answer}],
                          'provenance': {'final_text': 'inferred'}}}
            for eid, cid, answer in [('E1', 'C1', 'ANSWER:PUBLIC_MARKER'), ('E2', 'C2', '')]]},
        'pairs': [], 'html': 'PUBLIC_MARKER', 'direct_html': 'PUBLIC_MARKER', 'gateway_log': 'PUBLIC_MARKER',
        'ui_review': {'failure_visible': True}}
    return expected, observation


def calibrate():
    expected, original = known_good()
    mutations = [
        ('positive', '正确记录应通过', lambda o: None, 1),
        ('drop_call', '丢掉一条调用', lambda o: o['envelope']['calls'].pop(), 0),
        ('swap_execution', '查询把一条调用串到别的 execution',
         lambda o: o['envelope']['calls'][0].update(execution_id='E2'), 0),
        ('erase_error', '错误误标成成功', lambda o: o['envelope']['calls'][1].update(state='succeeded'), 0),
        ('invent_success', 'wire-only 伪造业务成功',
         lambda o: o['payload']['cases'][0]['overview'].update(success=True), 0),
        ('empty_output', '答复被清空', lambda o: o['payload']['cases'][0]['calls'][0].update(assistant=''), 0),
        ('leak_credential', '报告泄露假密钥', lambda o: o.update(direct_html='PUBLIC_MARKER ' + FAKE_KEY), 0),
        ('wrong_usage', 'usage 被替换为零', lambda o: o['envelope']['calls'][0]['usage'].update(prompt_tokens=0), 0),
        ('wrong_model', '报告模型身份错误', lambda o: o['payload']['cases'][0]['calls'][0].update(model='wrong-model'), 0),
        ('wrong_http_status', 'HTTP 400 被伪造成 500', lambda o: o['envelope']['calls'][1].update(status_code=500), 0),
        ('erase_error_reason', '保留 failed 但丢掉错误原因', lambda o: o['envelope']['calls'][1].update(error=None), 0),
        ('erase_terminal_time', '失败终止时间丢失', lambda o: o['payload']['cases'][1]['calls'][0].update(ts_end=None), 0),
        ('hide_failure_in_ui', '页面仅显示空回复', lambda o: o['ui_review'].update(failure_visible=False), 0),
        ('null', '什么也没采集', lambda o: (o['envelope'].update(calls=[]), o['payload'].update(cases=[])), 0),
    ]
    rows = []
    for ident, title, mutate, desired in mutations:
        observation = copy.deepcopy(original)
        mutate(observation)
        result = grade(expected, observation)
        actual = result['grade']['all_checks_pass']
        rows.append({'id': ident, 'title': title, 'expected_accept': desired, 'actual_accept': actual,
                     'calibration_passed': actual == desired, 'checks': result['checks']})
    return {'scope': 'grader calibration, not llmtracing product performance',
            'passed': sum(r['calibration_passed'] for r in rows), 'total': len(rows), 'rows': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    report = calibrate()
    dump(args.out, report)
    print(json.dumps({'passed': report['passed'], 'total': report['total']}))
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
