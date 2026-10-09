#!/usr/bin/env python3
"""Read-only product investigation: isolated transport A/B and callback observation."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from run_pilot import ROOT, PYTHON, FAKE_KEY, gateway, dump, http
from run_baseline import BaselineUpstream
from confirm_stream_boundary import consume


def instrument(gateway_dir):
    # Wrap only the copied callback in the isolated test gateway, saving narrow
    # synthetic-error fields; delegate to the unchanged callback implementation.
    with (gateway_dir/'custom_callbacks.py').open('a') as f:
        f.write('''
import json as _probe_json
from pathlib import Path as _ProbePath
_probe_original = proxy_handler_instance.async_log_failure_event
async def _probe_failure(kwargs, response_obj, start_time, end_time):
    exc = kwargs.get("exception")
    record = {"call_id": kwargs.get("_eval_trace_call_id") or kwargs.get("litellm_call_id"),
              "response_obj_is_none": response_obj is None,
              "exception_type": type(exc).__name__ if exc is not None else None,
              "exception_message": str(exc) if exc is not None else None,
              "exception_status_code": getattr(exc, "status_code", None),
              "start_time": str(start_time), "end_time": str(end_time)}
    with (_ProbePath(__file__).parent / "failure-hook-observations.jsonl").open("a") as out:
        out.write(_probe_json.dumps(record) + "\\n")
    return await _probe_original(kwargs, response_obj, start_time, end_time)
proxy_handler_instance.async_log_failure_event = _probe_failure
''')


def run_variant(out, disable):
    out.mkdir(parents=True,exist_ok=False)
    with gateway(out, upstream_type=BaselineUpstream,
                 extra_env={'DISABLE_AIOHTTP_TRANSPORT': 'True' if disable else 'False'},
                 runtime_setup=instrument) as (url,log,upstream):
        headers={'Authorization':'Bearer '+FAKE_KEY,'X-Eval-Run-Id':out.name,
                 'X-Eval-Case-Id':'STREAM_CONTROL','X-Eval-Execution-Id':'STREAM-CONTROL-1'}
        broken=consume(url+'/v1/chat/completions',headers)
        dump(out/'broken-client.json',broken)
        # Separate healthy-stream positive control; no tracing helper change.
        import httpx
        normal={'lines':[],'error':None}
        try:
            with httpx.Client(trust_env=False,timeout=15) as client:
                with client.stream('POST',url+'/v1/chat/completions',headers={**headers,'X-Eval-Execution-Id':'NORMAL'},
                     json={'model':'trace-fixture','stream':True,'messages':[{'role':'user','content':'STREAM_OK'}]}) as response:
                    normal['status']=response.status_code
                    normal['lines']=[l for l in response.iter_lines() if l]
        except Exception as exc:normal['error']=str(exc)
        dump(out/'normal-client.json',normal)
        upstream.release_late.set()
        status,body=http(url+'/v1/chat/completions',{'model':'trace-fixture','messages':[{'role':'user','content':'LATE_ERROR'}]},
                         {**headers,'X-Eval-Execution-Id':'HTTP400'})
        dump(out/'http400-client.json',{'status':status,'body':body})
        for _ in range(30):
            proc=subprocess.run([str(PYTHON),str(ROOT/'llmtrace'),'query','calls','--log',str(log),'--run-id',out.name,
                                 '--format','json','--events'],capture_output=True,text=True,timeout=15)
            q=json.loads(proc.stdout)
            if q['counts']['total']==3 and not q['pending_call_ids']:break
            time.sleep(.1)
        dump(out/'query.json',q)
        # Generate actual HTML from unmodified importer/renderer for each transport.
        proc=subprocess.run([str(PYTHON),str(ROOT/'llmtrace'),'report','html','--log',str(log),'--run-id',out.name,'--out',str(out/'report')],capture_output=True,text=True,timeout=20)
        dump(out/'export-command.json',{'returncode':proc.returncode,'stdout':proc.stdout,'stderr':proc.stderr})
        rows={c['execution_id']:{k:c.get(k) for k in ('call_id','state','status_code','error','ended_at')} for c in q['calls']}
        hooks=[json.loads(l) for l in (out/'_runtime/gateway/failure-hook-observations.jsonl').read_text().splitlines()]
        summary={'disable_aiohttp':disable,'broken_client':broken,'normal_client':normal,'query':rows,'failure_hook':hooks}
        dump(out/'summary.json',summary)
        return {'disable_aiohttp':disable,'broken_has_done':any('[DONE]' in l for l in broken['lines']),
                'broken_has_error_event':any('"error"' in l for l in broken['lines']),
                'normal_has_done':any('[DONE]' in l for l in normal['lines']), 'query':rows,'failure_hook':hooks}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True)
    root=parser.parse_args().out.resolve();root.mkdir(parents=True,exist_ok=False)
    result=[run_variant(root/'aiohttp-default',False),run_variant(root/'httpx-option',True)]
    dump(root/'comparison.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
