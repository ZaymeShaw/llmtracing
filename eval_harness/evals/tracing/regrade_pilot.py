#!/usr/bin/env python3
"""Regrade frozen pilot evidence; preserve the original grading for comparison.

Requires a browser review receipt for T10, tied to the exported HTML hash.
No requests to LiteLLM or external models are made.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

from run_pilot import METRICS, check, dump, grade


def read(path):
    return json.loads(path.read_text())


def regrade(flow):
    baseline = flow / 'baseline'
    rows = [json.loads(line) for line in (baseline / 'results.jsonl').read_text().splitlines()]
    ui = read(flow / '_ui-review.json')
    html_path = flow / ui['html_ref']
    if hashlib.sha256(html_path.read_bytes()).hexdigest() != ui['html_sha256']:
        raise ValueError('UI review is stale: HTML changed')
    for key in ('screenshot_ref', 'accessibility_ref'):
        if not (flow / ui[key]).is_file():
            raise ValueError('UI evidence missing: ' + key)
    revised = []
    for row in rows:
        case = row['prompt_id']
        path = baseline / 'artifacts' / case
        expected = read(path / 'expected.json')
        html = (path / 'final/llm_trace.html').read_text()
        observation = {'envelope': read(path / 'final-query.json'),
                       'pairs': read(path / 'final-pairs.json'), 'html': html,
                       'payload': json.loads(re.search(
                           r'<script id="DATA" type="application/json">(.*?)</script>', html, re.S).group(1)),
                       'direct_html': (path / 'direct-report/llm_trace.html').read_text(),
                       'gateway_log': (flow / '_runtime/logs/llm_calls.jsonl').read_text()}
        if case == 'T10':
            client = [c for c in read(path / 'client.json') if c['marker'] == 'ERROR']
            upstream = [c for c in read(path / 'upstream.json')
                        if c['marker'] == 'ERROR' and c['event'] == 'sent']
            assert len(client) == len(upstream) == 1
            assert client[0]['http_status'] == upstream[0]['status'] == 400
            reason = upstream[0]['body']['error']['message']
            assert reason in json.dumps(client[0]['response'])
            expected['failure_contract'] = {'status_codes': [400], 'reason': reason}
            assert ui['call_id'] in [c['call_id'] for c in observation['envelope']['calls']
                                     if c['state'] == 'failed']
            observation['ui_review'] = ui
        graded = grade(expected, observation)
        if case == 'T10':
            check(graded['checks'], 'state_truthful', '受控上游尚未返回时必须有 pending', 1,
                  read(path / 'pending-query.json').get('counts', {}).get('pending'))
            graded['grade'] = {metric: int(all(c['passed'] for c in graded['checks'] if c['metric'] == metric))
                               for metric in METRICS if metric in graded['grade']}
            graded['grade']['all_checks_pass'] = int(all(c['passed'] for c in graded['checks']))
        trace = read(baseline / row['trace'])
        trace[0]['content'] = row['prompt'] + '\n\nExpected:\n' + json.dumps(expected, ensure_ascii=False, indent=2)
        trace[-1]['content'] = json.dumps(graded, ensure_ascii=False, indent=2)
        revised.append((row, path, expected, graded, trace))

    # Validate all evidence first; preserve v1 once, before changing any scores.
    history = flow / '_grading_history/v1'
    if not history.exists():
        history.mkdir(parents=True)
        for name in ('_manifest.json', '_state.json', 'report.html'):
            shutil.copy2(flow / name, history / name)
        shutil.copy2(baseline / 'results.jsonl', history / 'results.jsonl')
        for row, path, *_ in revised:
            dest = history / row['prompt_id']
            dest.mkdir()
            for name in ('grading.json', 'expected.json'):
                shutil.copy2(path / name, dest / name)
            shutil.copy2(baseline / row['trace'], dest / 'trace.json')
    for row, path, expected, graded, trace in revised:
        dump(path / 'expected.json', expected)
        dump(path / 'grading.json', graded)
        dump(baseline / row['trace'], trace)
        row.update(grade=graded['grade'], explanation=graded)
    (baseline / 'results.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    manifest = read(flow / '_manifest.json')
    manifest.update(passed=sum(r['grade']['all_checks_pass'] for r in rows), scored=len(rows),
                    grading_revision=2, prior_grading='_grading_history/v1',
                    regraded_from_frozen_outputs=True,
                    grader_sha256=hashlib.sha256(Path(__file__).with_name('run_pilot.py').read_bytes()).hexdigest())
    dump(flow / '_manifest.json', manifest)
    state = read(flow / '_state.json')
    state.update(grader_approved=False, grading_revision=2)
    state['metrics'][0]['label'] = '全部适用检查通过'
    dump(flow / '_state.json', state)
    print(json.dumps({'passed': manifest['passed'], 'scored': len(rows),
                      'failed_checks': [c['label'] for r in rows for c in r['explanation']['checks'] if not c['passed']]}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flow', type=Path, required=True)
    regrade(parser.parse_args().flow.resolve())
