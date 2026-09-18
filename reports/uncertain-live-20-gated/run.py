import sys,asyncio,json,time,logging,contextvars,functools,subprocess,hashlib
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));OUT=Path(__file__).parent
logging.basicConfig(level=logging.ERROR)
from app.settings import settings
settings.deployment_mode='research';settings.live_trading=False;settings.allow_custodial_signing=False
settings.clickhouse_host=None;settings.langfuse_public_key=None;settings.langfuse_secret_key=None
# Do not alter research/model/provider configuration or force an uncertain route.
CASES=[
('Audit report on ANSEM','Clarify or explicitly identify token versus person; no unrelated public-sector audit.'),
('What is OPEN?','Disambiguate an overloaded ticker; disclose any assumption.'),
('OPEN token unlock schedule','Identify the project; distinguish past and future dates as of the run.'),
('Tell me about MOVE','Disambiguate token, index or equity; do not silently choose.'),
('SUI next cliff','Identify Sui token; dated future unlock or evidence-limited answer.'),
('Is M safe?','Ask which asset/project; single-letter name is insufficient.'),
('What happened to HYPE?','Identify the asset or explicitly ask; movement claims require dated evidence.'),
('Research ARC','Disambiguate multiple entities/tokens.'),
("What's happening with FARTCOIN?",'Identify the token; current claims need fresh sources.'),
('Who is behind VIRTUAL?','Identify Virtuals or clarify; founder claims need attributable sources.'),
('Thoughts on MON?','Disambiguate ticker/project; disclose assumption.'),
('Is RENDER audited?','Identify Render and the audited contract/system; no invented audit assurance.'),
('Mercury outlook','Ask company, token, planet or other subject; no silent selection.'),
("Tell me about ONDO's next unlock",'Identify Ondo token and a future unlock with dated evidence.'),
('What is KITE doing?','Clarify or explicitly establish the intended project/company.'),
('Apple token price','Do not silently substitute AAPL stock; establish crypto asset identity.'),
('Tell me about TRUMP','Distinguish person/token and namesake tokens.'),
('What is ZZZORBITTEST9?','Synthetic unknown: admit no reliable identification; do not invent entity.'),
('Compare OPEN and MOVE','Resolve both ambiguous subjects before presenting a definitive comparison.'),
('What about the unlock next month?','No prior conversation: ask which asset, do not invent context.')]
current=contextvars.ContextVar('live_case',default=None)
import httpx
orig_sync=httpx.Client.send;orig_async=httpx.AsyncClient.send

def request_row(req):
    r={'host':req.url.host,'method':req.method,'started':time.perf_counter()}
    try:
        d=json.loads(req.content)
        if isinstance(d,dict) and isinstance(d.get('model'),str):r['model']=d['model']
    except Exception:pass
    return r

def finish(row,start,response=None,error=None):
    row.pop('started',None);row['seconds']=round(time.perf_counter()-start,3)
    if response is not None:row['status']=response.status_code
    if error:row['error']=type(error).__name__

def syncsend(self,req,*a,**k):
    record=current.get();row=request_row(req);start=row['started']
    if record is not None:record['http'].append(row)
    try:r=orig_sync(self,req,*a,**k)
    except BaseException as e:finish(row,start,error=e);raise
    finish(row,start,r);return r
async def asyncsend(self,req,*a,**k):
    record=current.get();row=request_row(req);start=row['started']
    if record is not None:record['http'].append(row)
    try:r=await orig_async(self,req,*a,**k)
    except BaseException as e:finish(row,start,error=e);raise
    finish(row,start,r);return r
httpx.Client.send=syncsend;httpx.AsyncClient.send=asyncsend
from app.routing import subject_probe
for name in ['probe','context_search']:
    original=getattr(subject_probe,name)
    def wrap(fn,n):
        @functools.wraps(fn)
        def call(*a,**k):
            rec=current.get();start=time.perf_counter();event={'stage':n}
            if rec is not None:rec['search_stages'].append(event)
            try:
                result=fn(*a,**k);event['result']=result;return result
            finally:event['seconds']=round(time.perf_counter()-start,3)
        return call
    setattr(subject_probe,name,wrap(original,name))
from app import graph,followups,charts
from app import answer_gate
orig_check=answer_gate.check
async def gated_check(question,answer):
    rec=current.get();start=time.perf_counter()
    out=await orig_check(question,answer)
    if rec is not None:rec.setdefault('gate',[]).append({**out,'seconds':round(time.perf_counter()-start,3),'answer_head':answer[:160]})
    return out
answer_gate.check=gated_check
from app.nodes import runtime
from app.knowledge import tool as kb_tool
orig_lm=runtime._call_lm
async def lm(program,**kwargs):
    rec=current.get();event={'program':str(getattr(program,'signature',type(program))).__class__.__name__}
    try:event['program']=program.signature.__name__
    except Exception:event['program']=type(program).__name__
    start=time.perf_counter()
    if rec is not None:rec['model_hops'].append(event)
    try:return await orig_lm(program,**kwargs)
    finally:event['seconds']=round(time.perf_counter()-start,3)
runtime._call_lm=lm
# Captures the graph's own turn-budget accounting as well as actual HTTP attempts.
def budget(**kwargs):
    rec=current.get()
    if rec is not None:rec['budget']=kwargs
graph.emit_turn_budget_event=budget
import app.provider_router as pr
orig_event=pr.emit_provider_event
def provider_event(tool,provider,success,latency_ms):
    rec=current.get()
    if rec is not None:rec['provider_events'].append(dict(tool=tool,provider=provider,success=success,latency_ms=latency_ms))
pr.emit_provider_event=provider_event
# No secret values are printed. Redact configured credentials if a provider echoes one.
secrets=[str(v) for k,v in settings.model_dump().items() if v and isinstance(v,str) and any(s in k for s in ('api_key','secret','password','private_key')) and len(v)>7]
def save(path,data):
    raw=json.dumps(data,indent=2,default=str)
    for value in secrets:raw=raw.replace(value,'[REDACTED]')
    path.write_text(raw)
async def main():
    stamp=datetime.now(timezone.utc).isoformat()
    meta={'started_utc':stamp,'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'model':settings.model,'intent_model':settings.intent_model,'synthesis_model':settings.synthesis_model,'perplexity_configured':bool(settings.perplexity_api_key),'followups_enabled':settings.followups_enabled,'timeout_seconds':settings.chat_execution_timeout_seconds,'answer_gate_enabled':settings.answer_gate_enabled,'mode':'real graph in-process, no mocked provider responses; normal enrichment measured separately','cases':[{'id':i+1,'prompt':p,'expected':e} for i,(p,e) in enumerate(CASES)]}
    meta['source_hashes']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'app').rglob('*.py')}
    kb_tool.set_loop(asyncio.get_running_loop())
    try:await asyncio.wait_for(kb_tool.resolver(force=True),30);meta['knowledge_entities']=len(kb_tool.snapshot() or [])
    except Exception as e:meta['knowledge_error']=type(e).__name__
    save(OUT/'manifest.json',meta)
    print('START',stamp,'perplexity configured',bool(settings.perplexity_api_key),'knowledge',meta.get('knowledge_entities'),flush=True)
    # Sequential turns allow unambiguous timing and normal warm-cache behavior.
    only={int(a) for a in sys.argv[1:] if a.isdigit()}
    for i,(prompt,expected) in enumerate(CASES,1):
        if only and i not in only:continue
        rec={'id':i,'prompt':prompt,'expected':expected,'started_utc':datetime.now(timezone.utc).isoformat(),'http':[],'model_hops':[],'provider_events':[],'search_stages':[]};token=current.set(rec);start=time.perf_counter()
        try:
            run=await asyncio.wait_for(graph.run_agent(prompt,'','',{},None),settings.chat_execution_timeout_seconds)
            rec.update({'agent_seconds':round(time.perf_counter()-start,3),'answer':run.answer,'trajectory':run.trajectory,'intent':run.intent,'capabilities':run.capabilities,'routing_decision':run.routing_decision,'resolved_token':run.resolved_token,'pending_token':run.pending_token})
            async def chart():
                try:return await asyncio.wait_for(asyncio.to_thread(charts.chart_for,run,prompt),6)
                except Exception:return None
            extra=time.perf_counter();rec['chart'],rec['suggestions']=await asyncio.gather(chart(),followups.generate(prompt,run.answer,run.intent,run.trade_plan));rec['enrichment_seconds']=round(time.perf_counter()-extra,3)
        except Exception as e:rec['error']=type(e).__name__
        rec['total_seconds']=round(time.perf_counter()-start,3);rec['http_counts']=dict(Counter(r['host'] for r in rec['http']));save(OUT/f'{i:02d}.json',rec);current.reset(token)
        print(json.dumps({'id':i,'seconds':rec['total_seconds'],'gate':[g['verdict'] for g in rec.get('gate',[])],'route':rec.get('routing_decision',{}),'search_stages':[s['stage'] for s in rec['search_stages']],'http':rec['http_counts'],'error':rec.get('error')}),flush=True)
    print('COMPLETE',flush=True)
asyncio.run(main())
