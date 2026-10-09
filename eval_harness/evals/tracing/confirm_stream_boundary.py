#!/usr/bin/env python3
"""Diagnostic control: consume identical truncated HTTP streams directly and via proxy."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import httpx
from run_baseline import BaselineUpstream
from run_pilot import ROOT, PYTHON, FAKE_KEY, gateway, dump


def consume(url, headers):
    result={'url':url,'status':None,'lines':[],'error':None}
    try:
        with httpx.Client(trust_env=False,timeout=15) as client:
            with client.stream('POST',url,headers=headers,json={'model':'trace-fixture','stream':True,
                 'messages':[{'role':'system','content':'SYSTEM'},{'role':'user','content':'STREAM_BREAK'}]}) as response:
                result['status']=response.status_code
                for line in response.iter_lines():
                    if line: result['lines'].append(line)
    except Exception as exc: result['error']={'type':type(exc).__name__,'message':str(exc)}
    return result


def run(out):
    out.mkdir(parents=True,exist_ok=False)
    with gateway(out,upstream_type=BaselineUpstream) as (url,log,upstream):
        config=(out/'_runtime/gateway/config.yaml').read_text()
        direct_base=re.search(r'api_base: (.+)',config)[1].strip()
        direct=consume(direct_base+'/chat/completions',{})
        proxy=consume(url+'/v1/chat/completions',{'Authorization':'Bearer '+FAKE_KEY,
             'X-Eval-Run-Id':out.name,'X-Eval-Case-Id':'STREAM_CONTROL','X-Eval-Execution-Id':'STREAM-CONTROL-1'})
        dump(out/'direct-client.json',direct);dump(out/'proxy-client.json',proxy)
        for _ in range(20):
            raw=subprocess.run([str(PYTHON),str(ROOT/'llmtrace'),'query','calls','--log',str(log),
                 '--case-id','STREAM_CONTROL','--format','json','--events'],capture_output=True,text=True,check=True)
            query=json.loads(raw.stdout)
            if query['counts']['total'] and not query['pending_call_ids']:break
            time.sleep(.1)
        dump(out/'query.json',query)
        summary={'direct_transport_error':direct['error'],'direct_done':any('[DONE]' in s for s in direct['lines']),
                 'proxy_transport_error':proxy['error'],'proxy_done':any('[DONE]' in s for s in proxy['lines']),
                 'trace_states':[c['state'] for c in query['calls']],
                 'upstream_disconnects':sum(r['event']=='disconnect' for r in upstream.rows)}
        dump(out/'comparison.json',summary);print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',required=True,type=Path)
    run(parser.parse_args().out.resolve())
