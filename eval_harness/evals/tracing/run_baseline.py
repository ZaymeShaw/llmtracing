#!/usr/bin/env python3
"""Approved 15-family tracing baseline. Local real proxy + public-CLI artifact replay.

Reuses the reviewed five-family frozen pilot after verifying its product hashes.
Runs new HTTP cases and two more concurrent schedules; no provider credentials.
Infrastructure exceptions are status=error, never product grade=0.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
import traceback
import urllib.request

from run_pilot import ROOT, PYTHON, FAKE_KEY, TOOL, Upstream, gateway, dump, http, check, METRICS


def read(path):
    return json.loads(Path(path).read_text())


def payload(path):
    text = Path(path).read_text()
    return json.loads(re.search(r'<script id="DATA" type="application/json">(.*?)</script>', text, re.S).group(1))


def normalized(p):
    return [c for case in p['cases'] for c in case['calls']]


class BaselineUpstream(Upstream):
    def __init__(self, path):
        super().__init__(path)
        self.late_seen, self.release_late = threading.Event(), threading.Event()

    def handler(self):
        upstream = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                items = body.get('messages', body.get('input', []))
                if isinstance(items, str):
                    marker = items
                else:
                    value = items[-1].get('content', '') if items else ''
                    marker = value if isinstance(value, str) else ''.join(b.get('text', '') for b in value if isinstance(b, dict))
                upstream.append({'event': 'received', 'path': self.path, 'body': body, 'marker': marker, 'monotonic': time.monotonic()})
                if marker == 'LATE_ERROR':
                    upstream.late_seen.set()
                    upstream.release_late.wait(15)
                if marker.startswith('CONCURRENT:'):
                    time.sleep((3-int(marker.split(':')[1])) * .04)
                if body.get('stream'):
                    self.stream(marker)
                    return
                status = 400 if marker == 'LATE_ERROR' else 200
                use = {'id': 'tool-1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"q":"Q1"}'}}
                if status == 400:
                    response = {'error': {'message': 'CONTROLLED_UPSTREAM_ERROR', 'type': 'invalid_request_error', 'code': 'invalid_request_error'}}
                elif self.path.endswith('/responses'):
                    output = ([{'type':'function_call','id':'fc-1','call_id':'tool-1','name':'lookup','arguments':'{"q":"Q1"}','status':'completed'}]
                              if marker == 'Q1' else [{'type':'message','id':'msg-1','role':'assistant','status':'completed','content':[{'type':'output_text','text':'ANSWER:'+marker,'annotations':[]}]}])
                    response = {'id':'resp_'+hashlib.sha256(marker.encode()).hexdigest()[:12], 'object':'response','created_at':1700000000,
                                'status':'completed','error':None,'incomplete_details':None,'instructions':'SYSTEM','model':'trace-fixture',
                                'output':output,'parallel_tool_calls':True,'tools':body.get('tools',[]),'tool_choice':'auto',
                                'usage':{'input_tokens':11,'output_tokens':7,'total_tokens':18},'temperature':1,'top_p':1,
                                'metadata':{},'max_output_tokens':100,'previous_response_id':None,'reasoning':None,
                                'text':{'format':{'type':'text'}},'truncation':'disabled','user':None}
                    from litellm.types.llms.openai import ResponsesAPIResponse
                    ResponsesAPIResponse(**response)  # Validate the synthetic provider contract before sending it.
                else:
                    message = {'role':'assistant','content':'ANSWER:'+marker}
                    if marker == 'Q1': message.update(content=None, tool_calls=[use])
                    response = {'id':'fixture-'+hashlib.sha256(marker.encode()).hexdigest()[:12], 'object':'chat.completion','created':1700000000,
                                'model':'trace-fixture','choices':[{'index':0,'message':message,'finish_reason':'tool_calls' if marker=='Q1' else 'stop'}],
                                'usage':{'prompt_tokens':11,'completion_tokens':7,'total_tokens':18}}
                upstream.append({'event':'sent','marker':marker,'status':status,'body':response,'monotonic':time.monotonic()})
                raw=json.dumps(response).encode()
                self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw))); self.end_headers()
                self.wfile.write(raw)

            def stream(self, marker):
                self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Transfer-Encoding','chunked'); self.end_headers()
                def emit(delta, finish=None):
                    chunk={'id':'stream-fixture','object':'chat.completion.chunk','created':1700000000,'model':'trace-fixture',
                           'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
                    upstream.append({'event':'chunk','marker':marker,'body':chunk,'monotonic':time.monotonic()})
                    raw=('data: '+json.dumps(chunk)+'\n\n').encode()
                    self.wfile.write(f'{len(raw):X}\r\n'.encode()+raw+b'\r\n'); self.wfile.flush()
                emit({'role':'assistant','content':''})
                emit({'tool_calls':[{'index':0,'id':'stream-tool','type':'function','function':{'name':'lookup','arguments':'{}'}}]})
                time.sleep(.35)
                emit({'content':'PARTIAL_TEXT' if marker=='STREAM_BREAK' else 'STREAM_TEXT'})
                if marker=='STREAM_BREAK':
                    upstream.append({'event':'disconnect','marker':marker,'monotonic':time.monotonic()})
                    self.close_connection=True
                    self.connection.shutdown(socket.SHUT_WR)
                    return
                emit({}, 'stop')
                raw=b'data: [DONE]\n\n'
                self.wfile.write(f'{len(raw):X}\r\n'.encode()+raw+b'\r\n0\r\n\r\n'); self.wfile.flush()
                upstream.append({'event':'done','marker':marker,'monotonic':time.monotonic()})
        return Handler


class Suite:
    def __init__(self, out):
        self.out=out; self.base=out/'baseline'; self.rows=[]; self.checks=[]; self.case=''; self.lock=threading.Lock()
        self.run_id=out.name
    def eq(self, metric, label, expected, actual):
        check(self.checks,metric,label,expected,actual)
    def cli(self, *args):
        started=time.monotonic()
        proc=subprocess.run([str(PYTHON),str(ROOT/'llmtrace'),*map(str,args)],cwd=ROOT,text=True,capture_output=True,timeout=30)
        entry={'args':list(map(str,args)), 'returncode':proc.returncode,'stderr':proc.stderr,'wall_s':time.monotonic()-started}
        with self.lock:
            with (self.path/'commands.jsonl').open('a') as f: f.write(json.dumps(entry)+'\n')
        if proc.returncode not in (0,1,3): raise RuntimeError(f'CLI failed {entry}')
        return proc.stdout
    def query(self, log=None, selector=None, name='query', export=False, rotated=False):
        args=['query','calls','--log',log or self.log,*(selector or ['--run-id',self.run_id,'--case-id',self.case]),'--format','json','--events']
        if rotated: args += ['--include-rotated']
        if export:
            export_args = [a for a in args if a != '--events']
            export_args[export_args.index('--format')+1] = 'pairs'
            self.cli(*export_args, '--html', self.path/name)
        result=json.loads(self.cli(*args)); dump(self.path/(name+'.json'),result)
        return result
    def snap(self, count, *, selector=None, name='final', log=None, rotated=False):
        deadline=time.monotonic()+6
        while True:
            env=self.query(log,selector,name,rotated=rotated)
            if env['counts']['total']>=count and not env['pending_call_ids']: break
            if time.monotonic()>=deadline: break
            time.sleep(.1)
        env=self.query(log,selector,name,True,rotated)
        return env,payload(self.path/name/'llm_trace.html')
    def request(self, marker, *, execution=None, headers=None, body=None, endpoint='/v1/chat/completions', stream=False):
        h={'Authorization':'Bearer '+FAKE_KEY,'X-Eval-Run-Id':self.run_id,'X-Eval-Case-Id':self.case,
           'X-Eval-Execution-Id':execution or self.case+'-E1','X-Eval-Harness':'build-eval-baseline'}
        h.update(headers or {}); h={k:v for k,v in h.items() if v is not None}
        body=body or {'model':'trace-fixture','messages':[{'role':'system','content':'SYSTEM'},{'role':'user','content':marker}],'max_tokens':100}
        started=time.monotonic(); error=None
        if stream:
            body['stream']=True
            request=urllib.request.Request(self.url+endpoint,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**h})
            response=[]; status=None
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=20) as r:
                    status=r.status
                    for line in r:
                        text=line.decode().strip()
                        if text.startswith('data:'): response.append({'data':text[5:].strip(),'monotonic':time.monotonic()})
            except Exception as exc: error=type(exc).__name__+': '+str(exc)
        else: status,response=http(self.url+endpoint,body,h)
        row={'marker':marker,'headers':h,'body':body,'endpoint':endpoint,'status':status,'response':response,'error':error,'wall_s':time.monotonic()-started}
        with self.lock:
            with (self.path/'client.jsonl').open('a') as f: f.write(json.dumps(row)+'\n')
        return row
    def basic(self,env,p,count, executions=None):
        self.eq('capture_complete','查询调用总数',count,len(env['calls']))
        self.eq('capture_complete','报告调用总数',count,len(normalized(p)))
        self.eq('capture_complete','调用不重复',len(env['calls']),len({c['call_id'] for c in env['calls']}))
        if executions is not None:
            actual={}
            for c in env['calls']: actual[c.get('execution_id')]=actual.get(c.get('execution_id'),0)+1
            self.eq('attribution_exact','逐 execution 调用数',executions,actual)
    def family(self,case,title,fn,boundary):
        self.case=case; self.path=self.base/'artifacts'/case; self.path.mkdir(parents=True,exist_ok=True)
        self.checks=[]; before=len(self.upstream.rows) if hasattr(self,'upstream') else 0; start=time.monotonic()
        try:
            fn()
            if not self.checks: raise RuntimeError('No checks were emitted')
            if hasattr(self,'upstream'): dump(self.path/'upstream.json',self.upstream.rows[before:])
            grade={m:int(all(c['passed'] for c in self.checks if c['metric']==m)) for m in METRICS if any(c['metric']==m for c in self.checks)}
            grade['all_checks_pass']=int(all(c['passed'] for c in self.checks))
            row={'prompt_id':case,'prompt':title,'rep':0,'status':'ok','grade':grade,'tags':[boundary],'model':'controlled fixture; no paid model',
                 'wall_s':time.monotonic()-start,'trace':f'traces/{case}_rep0.json','explanation':{'checks':self.checks}}
            dump(self.path/'grading.json',row['explanation'])
            turns=[{'role':'user','content':title+'\nBoundary: '+boundary}]
            clients=self.path/'client.jsonl'
            if clients.exists():
                for line in clients.read_text().splitlines():
                    client=json.loads(line)
                    turns += [{'role':'tool_call','name':'HTTP client -> LiteLLM','content':json.dumps({k:client[k] for k in ('endpoint','body','headers')},ensure_ascii=False)},
                              {'role':'tool_result','name':'LiteLLM -> HTTP client','content':json.dumps({k:client[k] for k in ('status','response','error')},ensure_ascii=False)}]
            if (self.path/'final.json').exists():
                turns.append({'role':'tool_result','name':'llmtrace query','content':(self.path/'final.json').read_text()})
            turns.append({'role':'assistant','content':json.dumps(row['explanation'],ensure_ascii=False,indent=2)})
            if (self.path/'final/llm_trace.html').exists():
                turns[-1]['attachments']=[{'kind':'html','ref':f'baseline/artifacts/{case}/final/llm_trace.html','alt':'产品实际导出'}]
            dump(self.base/row['trace'],turns)
        except Exception as exc:
            row={'prompt_id':case,'prompt':title,'rep':0,'status':'error','tags':[boundary],'error':str(exc),'wall_s':time.monotonic()-start}
            dump(self.path/'infrastructure-error.json',{'error':str(exc),'traceback':traceback.format_exc()})
        self.rows=[r for r in self.rows if r['prompt_id']!=case]+[row]
        self.save(); print(case,row['status'],row.get('grade'),row.get('error',''),flush=True)
    def save(self):
        (self.base/'results.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in sorted(self.rows,key=lambda r:r['prompt_id'])))


def protocol(s,anthropic):
    if anthropic:
        endpoint='/v1/messages'
        body={'model':'trace-fixture','system':'SYSTEM','max_tokens':100,'messages':[{'role':'user','content':'Q1'}],
              'tools':[{'name':'lookup','description':'Synthetic lookup','input_schema':{'type':'object','properties':{'q':{'type':'string'}}}}]}
        first=s.request('Q1',body=body,endpoint=endpoint,headers={'anthropic-version':'2023-06-01'})
        s.eq('content_faithful','首个请求 HTTP 200',200,first['status'])
        if first['status']!=200: return
        use=first['response'].get('content',[])
        body['messages'] += [{'role':'assistant','content':use},{'role':'user','content':[{'type':'tool_result','tool_use_id':'tool-1','content':'TOOL_RESULT_1'},{'type':'text','text':'Q2'}]}]
    else:
        endpoint='/v1/responses'
        body={'model':'trace-fixture','instructions':'SYSTEM','input':'Q1', 'max_output_tokens':100,
              'tools':[{'type':'function',**TOOL['function']}]}
        first=s.request('Q1',body=body,endpoint=endpoint)
        s.eq('content_faithful','首个请求 HTTP 200',200,first['status'])
        if first['status']!=200: return
        body['input']=[{'role':'user','content':'Q1'},*first['response'].get('output',[]),
                       {'type':'function_call_output','call_id':'tool-1','output':'TOOL_RESULT_1'},{'role':'user','content':'Q2'}]
    last=s.request('Q2',body=body,endpoint=endpoint)
    s.eq('content_faithful','第二个请求 HTTP 200',200,last['status'])
    env,p=s.snap(2); s.basic(env,p,2,{s.case+'-E1':2}); calls=normalized(p)
    s.eq('content_faithful','最终文本',True,any(c.get('assistant')=='ANSWER:Q2' for c in calls))
    s.eq('content_faithful','system 保真',['SYSTEM','SYSTEM'],[c.get('system') for c in calls])
    s.eq('content_faithful','工具关联 ID 与名称',True,any(t.get('id')=='tool-1' and t.get('name')=='lookup' for c in calls for t in c.get('tool_calls',[])))
    s.eq('content_faithful','工具结果保留',True,any('TOOL_RESULT_1' in json.dumps(c.get('messages',[])) for c in calls))
    s.eq('content_faithful','工具参数保真',[{'q':'Q1'}],[json.loads(t['arguments']) for c in calls for t in c.get('tool_calls',[])])
    s.eq('content_faithful','模型身份保真',['trace-fixture','trace-fixture'],[c.get('model') for c in calls])
    s.eq('content_faithful','usage 保真',[[11,7],[11,7]],[[c.get('usage',{}).get('prompt_tokens'),c.get('usage',{}).get('completion_tokens')] for c in calls])
    s.eq('provenance_truthful','工具往返不增加用户轮次',[2],[c['overview']['metrics'].get('num_turns') for c in p['cases']])
    # Compare actual stop values, not merely the existence of a named field.
    stop_values=[c.get('finish_reason') or c.get('stop_reason') or c.get('response_status') for c in calls]
    s.eq('content_faithful','停止原因可还原',True,
         stop_values in (['tool_calls','stop'],['tool_use','end_turn']) if anthropic else stop_values==['completed','completed'])


def concurrency(s):
    prior=read(s.path/'grading.json')['checks']
    s.checks.extend(prior)
    for rep in (1,2):
        def each(i):
            for j in range(3):
                r=s.request(f'CONCURRENT:{i}:{j}',execution=f'T04-R{rep}-E{i}')
                if r['status']!=200: raise RuntimeError('concurrency transport fixture failed')
        with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(each,range(4)))
        env,p=s.snap(rep*12,name=f'repeat-{rep}')
        for i in range(4):
            eid=f'T04-R{rep}-E{i}'
            got=[c for c in p['cases'] if c['identity'].get('execution_id')==eid]
            s.eq('attribution_exact',f'{eid} 调用及内容',[f'ANSWER:CONCURRENT:{i}:{j}' for j in range(3)],sorted(x['assistant'] for c in got for x in c['calls']))
    s.eq('capture_complete','另两次并发调度共 24 调用',24,len(env['calls']))


def retry(s):
    with ThreadPoolExecutor(max_workers=1) as pool:
        old=pool.submit(s.request,'LATE_ERROR',execution='T05-E1')
        if not s.upstream.late_seen.wait(8): raise RuntimeError('late event fixture not reached')
        try: new=s.request('NEW_EXECUTION',execution='T05-E2')
        finally: s.upstream.release_late.set()
        old=old.result(timeout=20)
    s.eq('state_truthful','受控失败与新执行 HTTP 状态',[400,200],[old['status'],new['status']])
    env,p=s.snap(2); s.basic(env,p,2,{'T05-E1':1,'T05-E2':1})
    s.eq('state_truthful','各次尝试状态',{'T05-E1':'failed','T05-E2':'succeeded'},{c['execution_id']:c['state'] for c in env['calls']})
    s.eq('attribution_exact','延迟旧事件不替代新执行',['T05-E2'],[c['identity']['execution_id'] for c in p['cases'] if c['latest']])
    s.eq('capture_complete','两次尝试在报告中保留',2,len(p['cases']))


def isolation(s):
    for run,harness in ((s.run_id,'HA'),(s.run_id,'HB'),(s.run_id+'-OTHER','HA')):
        s.request(run+':'+harness,execution=run+'-'+harness,headers={'X-Eval-Run-Id':run,'X-Eval-Harness':harness})
    env,p=s.snap(3,selector=['--case-id','T06'])
    s.basic(env,p,3)
    expected=sorted([(s.run_id,'HA'),(s.run_id,'HB'),(s.run_id+'-OTHER','HA')])
    s.eq('attribution_exact','相同 case 在不同 run/harness 分开',expected,sorted((c['identity'].get('run_id'),c['identity'].get('harness')) for c in p['cases']))
    filtered=s.query(selector=['--run-id',s.run_id,'--case-id','T06'],name='run-filter')
    s.eq('attribution_exact','run 过滤排除其他批次',[s.run_id,s.run_id],sorted(c.get('run_id') for c in filtered['calls']))


def lease(s,active,case='LANE_CASE',execution='LANE_EXECUTION'):
    receipt={'lane_id':'eval_lane','state':'active' if active else 'idle','run_id':s.run_id,'case_id':case,'execution_id':execution,
             'owner_token':'synthetic-eval-owner-token'}
    dump(s.registry/'eval_lane.json',receipt)
    if active: dump(s.path/'active-lane-input.json',receipt)


def lane_override(s):
    lease(s,True)
    try: s.request('EXPLICIT_WINS')
    finally: lease(s,False)
    env,p=s.snap(1);s.basic(env,p,1,{'T07-E1':1})
    s.eq('attribution_exact','显式双 ID 胜过 active lane',['T07'],[c.get('case_id') for c in env['calls']])
    raw=[r for r in s.upstream.rows if r.get('marker')=='EXPLICIT_WINS']
    s.eq('capture_complete','请求实际到达上游',True,bool(raw))
    events=[e for c in env['calls'] for e in c.get('events',[]) if e['event']=='pre_api_call']
    s.eq('attribution_exact','归属来源 explicit',True,bool(events) and all(e.get('attribution_status')=='explicit' for e in events))


def w3c(s):
    trace='a'*32
    h={'baggage':'eval.case_id=BAG_CASE,eval.execution_id=BAG_EXEC,eval.run_id=BAG_RUN', 'traceparent':f'00-{trace}-00f067aa0ba902b7-01'}
    s.request('W3C_CONFLICT',headers=h)
    env,p=s.snap(1);s.basic(env,p,1,{'T08-E1':1})
    s.eq('attribution_exact','显式 case 胜出',['T08'],[c['case_id'] for c in env['calls']])
    events=[e for c in env['calls'] for e in c.get('events',[]) if e['event']=='pre_api_call']
    text=json.dumps(events)
    s.eq('provenance_truthful','冲突来源可查',True,'conflict' in text and 'BAG_EXEC' in text and trace in text)
    s.eq('attribution_exact','W3C trace_id 保留',[trace],[c.get('trace_id') for c in env['calls']])


def missing(s):
    lease(s,True,case='T09',execution='T09-LANE')
    try: s.request('WITH_LANE',headers={'X-Eval-Execution-Id':None})
    finally: lease(s,False)
    s.request('NO_ACTIVE_LANE',headers={'X-Eval-Execution-Id':None})
    # Select by call_id for unattributed events; a case filter must not invent an owner.
    time.sleep(.3)
    records=[json.loads(l) for l in s.log.read_text().splitlines()]
    pre=[r for r in records if r.get('event')=='pre_api_call' and any(m.get('content') in ('WITH_LANE','NO_ACTIVE_LANE') for m in r.get('messages',[]))]
    self_ids=','.join(r['call_id'] for r in pre)
    if not self_ids: raise RuntimeError('missing-id fixture produced no pre-call records')
    env,p=s.snap(2,selector=['--call-id',self_ids]); s.basic(env,p,2)
    s.eq('attribution_exact','只有 active lane 归属',1,sum(c.get('execution_id')=='T09-LANE' for c in env['calls']))
    s.eq('attribution_exact','无 active lane 保持未归属',1,sum(not c.get('execution_id') and not c.get('case_id') for c in env['calls']))
    s.eq('provenance_truthful','未归属有可诊断状态',True,any(r.get('attribution_status')=='idle' and not r.get('execution_id') for r in pre))


def streaming(s):
    normal=s.request('STREAM_OK',execution='T11-OK',stream=True)
    broken=s.request('STREAM_BREAK',execution='T11-BREAK',stream=True)
    s.eq('content_faithful','正常流 HTTP 200',200,normal['status'])
    chunks=[]
    for row in normal['response']:
        if row['data']!='[DONE]': chunks.append(json.loads(row['data']))
    deltas=[c['choices'][0]['delta'] for c in chunks if c.get('choices')]
    s.eq('content_faithful','客户端收到原始文本','STREAM_TEXT',''.join(d.get('content') or '' for d in deltas))
    s.eq('content_faithful','工具帧不被 tracing 丢掉',True,any(t.get('id')=='stream-tool' for d in deltas for t in d.get('tool_calls',[])))
    s.eq('state_truthful','正常流明确结束',True,any(r['data']=='[DONE]' for r in normal['response']))
    env,p=s.snap(2);s.basic(env,p,2,{'T11-OK':1,'T11-BREAK':1})
    s.eq('state_truthful','正常与断流终态',{'T11-OK':'succeeded','T11-BREAK':'failed'},{c['execution_id']:c['state'] for c in env['calls']})
    for c in env['calls']:
        frames=[e for e in c.get('events',[]) if e.get('event')=='first_frame']
        s.eq('capture_complete',c['execution_id']+' 首字事件唯一',1,len(frames))
        if frames:
            s.eq('provenance_truthful',c['execution_id']+' 空帧/工具帧不算首字',True,250 <= (frames[0].get('first_frame_ms') or 0) < 5000)
    failed=[c for c in env['calls'] if c['execution_id']=='T11-BREAK']
    s.eq('content_faithful','断流有错误诊断',True,bool(failed) and bool(failed[0].get('error')))
    s.eq('content_faithful','断流已收到的文本可查',True,any(c.get('assistant')=='PARTIAL_TEXT' for c in normalized(p)))


def frozen_events(s):
    raw=[json.loads(l) for l in (s.out/'_frozen-pilot/logs/llm_calls.jsonl').read_text().splitlines()]
    cid=next(r['call_id'] for r in raw if r.get('case_id')=='T14' and r.get('event')=='pre_api_call')
    records=[r for r in raw if r.get('call_id')==cid]
    pre=copy.deepcopy(next(r for r in records if r['event']=='pre_api_call'))
    ok=copy.deepcopy(next(r for r in records if r['event']=='success'))
    for r in (pre,ok): r.update(run_id=s.run_id,case_id=s.case,execution_id=s.case+'-E1')
    return pre,ok


def rotation(s):
    pre,ok=frozen_events(s); logs=s.path/'logs';logs.mkdir()
    with gzip.open(logs/'llm_calls_20261001_000001.jsonl.gz','wt') as f: f.write(json.dumps(pre)+'\n')
    live=logs/'llm_calls.jsonl'; data=json.dumps(ok); live.write_text(data[:len(data)//2])
    during=s.query(logs,['--call-id',pre['call_id']],name='partial',rotated=True)
    s.eq('state_truthful','轮转加半行写入时保留 pending',['pending'],[c['state'] for c in during['calls']])
    with live.open('a') as f:f.write(data[len(data)//2:]+'\n')
    env,p=s.snap(1,selector=['--call-id',pre['call_id']],log=logs,rotated=True)
    s.basic(env,p,1); s.eq('state_truthful','补齐半行后跨文件配对',['succeeded'],[c['state'] for c in env['calls']])
    again=s.query(logs,['--call-id',pre['call_id']],name='repeat',rotated=True)
    s.eq('capture_complete','重复查询不重复调用',env['calls'],again['calls'])


def idempotence(s):
    pre,ok=frozen_events(s); log=s.path/'log.jsonl';log.write_text(json.dumps(pre)+'\n'+json.dumps(ok)+'\n')
    env,p=s.snap(1,log=log); target=s.path/'final'
    s.cli('report','html','--log',log,'--out',target,'--run-id',s.run_id)
    again=payload(target/'llm_trace.html')
    s.eq('capture_complete','同一日志导出幂等',p['cases'],again['cases'])
    stale=s.path/'stale.jsonl';stale.write_text(json.dumps(pre)+'\n')
    s.cli('report','html','--log',stale,'--out',target,'--run-id',s.run_id)
    last=payload(target/'llm_trace.html');dump(s.path/'after-stale.json',last)
    s.eq('state_truthful','旧请求快照不抹掉终态答复',[c['assistant'] for c in normalized(p)],[c['assistant'] for c in normalized(last)])
    s.eq('capture_complete','旧快照不重复调用',1,len(normalized(last)))
    s.eq('state_truthful','旧快照不抹掉终止时间',[c['ts_end'] for c in normalized(p)],[c['ts_end'] for c in normalized(last)])


def provenance(s):
    s.checks.extend(read(s.path/'grading.json')['checks'])
    original=s.path/'final'; wire=payload(original/'llm_trace.html')
    for mode in ('runner-wire','runner-only'):
        directory=s.path/mode
        if mode=='runner-wire':
            shutil.copytree(original,directory)
            case=next((directory/'cases').iterdir())
        else:
            case=directory/'cases/T14';case.mkdir(parents=True)
        dump(case/'trace.json',{'case_id':'T14','success':True,'exit_code':0,'metrics':{'wall_ms':9000,'first_frame_ms':125,'first_frame_kind':'text'},
             'turns':[{'index':1,'prompt':'MODEL_DRAFT','final_text':'BUSINESS_FINAL'}],
             'events':[{'kind':'text','payload':{'text':'BUSINESS_FINAL'}}]})
        s.cli('report','html',directory)
        p=payload(directory/'llm_trace.html'); ov=p['cases'][0]['overview']
        s.eq('content_faithful',mode+' 业务最终答案',['BUSINESS_FINAL'],[t['final_text'] for t in ov['turns']])
        s.eq('provenance_truthful',mode+' 来源标签',{'final_text':'harness','first_frame_ms':'harness','wall_ms':'harness','success':'harness'},ov['provenance'])
        s.eq('state_truthful',mode+' 业务成功',True,ov['success'])
        s.eq('capture_complete',mode+' 模型调用数',1 if mode=='runner-wire' else 0,p['cases'][0]['n_calls'])
        if mode=='runner-wire':s.eq('content_faithful','业务答案不改写模型草稿','ANSWER:MODEL_DRAFT',p['cases'][0]['calls'][0]['assistant'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--pilot',type=Path,required=True)
    parser.add_argument('--skip-ui-review', action='store_true',
                        help='Run machine checks only; record the missing browser review explicitly.')
    args=parser.parse_args(); out=args.out.resolve(); pilot=args.pilot.resolve()
    manifest=read(pilot/'_manifest.json')
    for name,sha in manifest['source_sha256'].items():
        if name.startswith('eval_harness/evals/'):continue
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha:
            raise RuntimeError('Product changed since reviewed pilot: '+name)
    out.mkdir(parents=True,exist_ok=False)
    shutil.copytree(pilot/'baseline',out/'baseline')
    frozen=out/'_frozen-pilot';frozen.mkdir()
    shutil.copytree(pilot/'_runtime/logs',frozen/'logs')
    evidence = ['_manifest.json','_grader-calibration-v2.json']
    if not args.skip_ui_review:
        evidence += ['_ui-review.json','_failure-ui.png','_failure-ui.txt']
    for name in evidence:
        shutil.copy2(pilot/name,frozen/name)
    dump(out/'_state.json',{'phase':'baseline','inputs_approved':True,'grader_approved':True,'approval':'User confirmed reviewed grading on 2026-10-01',
         'metrics':[{'id':'all_checks_pass','kind':'binary','label':'全部适用检查通过'}],'perf_fields':[],'train_ids':[f'T{i:02}' for i in range(1,16)]})
    s=Suite(out);s.rows=[json.loads(l) for l in (s.base/'results.jsonl').read_text().splitlines()]
    for r in s.rows:
        r['tags']=['frozen-http-pilot' if args.skip_ui_review else 'frozen-reviewed-http-pilot']
        r['source_flow']=str(pilot)
    registry=out/'_lane-registry';registry.mkdir();s.registry=registry;lease(s,False)
    start=time.monotonic()
    with gateway(out,upstream_type=BaselineUpstream,extra_env={'LANE_POOL':'eval','LLM_GATEWAY_NAME':'eval_lane','LANE_REGISTRY_DIR':str(registry)}) as (url,log,upstream):
        s.url,s.log,s.upstream=url,log,upstream
        for case,title,fn in [
            ('T02','Anthropic Messages 真实 HTTP 工具往返',lambda:protocol(s,True)),
            ('T03','Responses 真实 HTTP 工具往返',lambda:protocol(s,False)),
            ('T04','三次并发调度：4 execution × 3 calls，逐执行核对',lambda:concurrency(s)),
            ('T05','旧执行失败事件晚到；新执行与历史尝试隔离',lambda:retry(s)),
            ('T06','同 case 在不同 run 与 harness 中隔离；每次执行 ID 唯一',lambda:isolation(s)),
            ('T07','显式双 ID 优先于冲突的 active lane',lambda:lane_override(s)),
            ('T08','W3C baggage/traceparent 与显式 ID 冲突',lambda:w3c(s)),
            ('T09','缺 execution ID：active lane 归属、idle lane 保持未归属',lambda:missing(s)),
            ('T11','真实 SSE：空帧、工具帧、首字与中途断流',lambda:streaming(s))]:
            s.family(case,title,fn,'real-http-proxy')
        upstream.release_late.set()
    for case,title,fn in [('T12','跨 gzip 轮转与半行写入，通过公开 CLI 查询',lambda:rotation(s)),
                          ('T13','重复导出与旧请求快照覆盖，通过公开 CLI',lambda:idempotence(s)),
                          ('T14','wire-only / runner+wire / runner-only 来源与业务最终答案',lambda:provenance(s))]:
        s.family(case,title,fn,'public-cli-artifact-replay')
    files=[p for base in ('eval_harness/src/eval_harness','llm_gateway/callbacks') for p in (ROOT/base).rglob('*.py') if '__pycache__' not in p.parts]
    dump(out/'_manifest.json',{'scope':'15 approved scenario families; local controlled upstream + CLI artifact replay; no production error-rate claim',
         'ui_review': 'not_run' if args.skip_ui_review else 'reviewed',
         'unverified_checks': ['T10 browser failure visibility'] if args.skip_ui_review else [],
         'pilot_source':str(pilot),'wall_s':time.monotonic()-start,'scored':sum(r['status']=='ok' for r in s.rows),
         'passed':sum(r.get('grade',{}).get('all_checks_pass',0) for r in s.rows),'infra_errors':sum(r['status']=='error' for r in s.rows),
         'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}})
    (s.base/'change.md').write_text('# 已确认的 15 类追踪可信度基线；含冻结 pilot 与新增受控场景\n')
    print(json.dumps(read(out/'_manifest.json')|{'source_sha256':'saved'},ensure_ascii=False))


if __name__=='__main__':main()
