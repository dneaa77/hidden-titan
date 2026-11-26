"""Single-process authoritative multiplayer server for Hidden Titan."""
import asyncio
import math
import random
import secrets
import string
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent
COLORS = ['#b34b42','#4d80a6','#d0aa54','#8972a4','#66a26b','#d18351','#c3c5bd','#45616b']
ROOMS = [(35,38),(280,38),(525,38),(35,245),(280,245),(525,245)]
rooms = {}

def near(p, q):
    return math.hypot(p['x']-q['x'], p['y']-q['y'])

def zone(p, i):
    x,y = ROOMS[i]
    return x < p['x'] < x+160 and y < p['y'] < y+125

def create():
    while True:
        code = ''.join(secrets.choice(string.ascii_uppercase) for _ in range(6))
        if code not in rooms: break
    r = dict(code=code, players={}, host=None, phase='lobby', titan=None, sabotage=0, sab_ready=0, round=0, votes={}, chat=[], deadline=0, reason='', winner=None, notice='', result='')
    rooms[code] = r
    return r

def add(r, name):
    pid = secrets.token_hex(8)
    p = dict(id=pid, name=str(name).strip()[:16] or 'Разведчик', color=next(c for c in COLORS if c not in {p['color'] for p in r['players'].values()}), x=360,y=205,alive=True,reported=False,done=set(),challenge=None,dx=0,dy=0,input_at=0,kill_ready=0,meet=True,ready=False,chat_at=0)
    r['players'][pid]=p
    if r['host'] is None: r['host']=pid
    return p

def finish(r, winner, reason):
    r.update(phase='end', winner=winner, reason=reason, sabotage=0)

def check(r):
    if r['phase'] not in ('game','meeting'): return
    ps=r['players']; t=ps.get(r['titan'])
    scouts=[p for p in ps.values() if p['id']!=r['titan']]
    if not t or not t['alive']: finish(r,'scout','Скрытый титан найден или покинул игру.')
    elif sum(p['alive'] for p in scouts)<=1: finish(r,'titan','Титан сравнялся числом с разведчиками.')
    elif scouts and all(len(p['done'])==6 for p in scouts): finish(r,'scout','Все задания выполнены.')

def start(r):
    r.update(phase='game',titan=secrets.choice(list(r['players'])),sabotage=0,sab_ready=time.monotonic()+15,round=0,votes={},chat=[],notice='Матч начался.',result='',winner=None,reason='')
    for i,p in enumerate(r['players'].values()):
        p.update(x=290+i*19,y=198+i%2*22,alive=True,reported=False,done=set(),challenge=None,dx=0,dy=0,input_at=0,kill_ready=time.monotonic()+12,meet=True)

def meeting(r, reason):
    r.update(phase='meeting',round=r['round']+1,votes={},chat=[],deadline=time.monotonic()+60,reason=reason)
    for p in r['players'].values(): p.update(reported=not p['alive'],dx=0,dy=0,challenge=None)

def resolve(r):
    counts={}
    for v in r['votes'].values(): counts[v]=counts.get(v,0)+1
    order=sorted(counts,key=counts.get,reverse=True)
    out=order[0] if order and (len(order)==1 or counts[order[0]]>counts[order[1]]) else None
    p=r['players'].get(out)
    if p:
        p.update(alive=False,reported=True)
        r['notice']=p['name']+' исключён. '+('Это был титан.' if out==r['titan'] else 'Это был разведчик.')
    else: r['notice']='Никто не исключён.'
    r.update(phase='game',votes={},result=r['notice'])
    check(r)

def action(r,p,m):
    t=m.get('type'); now=time.monotonic(); pid=p['id']; ps=r['players']; phase=r['phase']
    if t=='color' and phase=='lobby':
        c=m.get('color');
        if c in COLORS and all(q is p or q['color']!=c for q in ps.values()): p['color']=c
    elif t=='ready' and phase=='lobby': p['ready']=not p['ready']
    elif t=='start' and phase=='lobby' and pid==r['host']:
        if len(ps)<4: return 'Нужно минимум 4 игрока.'
        if not all(q['ready'] or q['id']==pid for q in ps.values()): return 'Дождитесь готовности игроков.'
        start(r)
    elif t=='again' and phase=='end' and pid==r['host']:
        r.update(phase='lobby',titan=None,notice='',winner=None,reason='',result='')
        for q in ps.values(): q['ready']=False
    elif phase=='meeting':
        if not p['alive']: return
        if t=='chat' and now-p['chat_at']>=.8:
            msg=str(m.get('text','')).strip()[:180]
            if msg: r['chat']=(r['chat']+[{'name':p['name'],'text':msg}])[-30:]; p['chat_at']=now
        elif t=='vote' and pid not in r['votes']:
            target=m.get('target')
            if target=='skip' or (isinstance(target,str) and target in ps and ps[target]['alive']): r['votes'][pid]=target
            if all(q['id'] in r['votes'] for q in ps.values() if q['alive']): resolve(r)
    elif phase=='game':
        titan=pid==r['titan']
        if t=='move':
            dx,dy=m.get('dx',0),m.get('dy',0)
            if all(type(v) in (int,float) and math.isfinite(v) for v in (dx,dy)):
                d=max(1,math.hypot(dx,dy)); p.update(dx=dx/d,dy=dy/d,input_at=now)
        elif not p['alive']: return
        elif t=='use':
            if not titan and r['sabotage'] and zone(p,5): r.update(sabotage=0,notice='Ворота восстановлены.'); return
            i=next((i for i in range(6) if zone(p,i)),None)
            if i is None: return 'Войди в здание.'
            if titan: return 'Ты имитируешь задание.'
            if i in p['done']: return 'Задание уже выполнено.'
            p.update(challenge={'room':i,'seq':[random.randint(1,9) for _ in range(4)],'step':0},dx=0,dy=0)
        elif t=='digit' and p['challenge']:
            c=p['challenge']; i=c['room']
            if not zone(p,i): p['challenge']=None; return 'Ты покинул здание.'
            if m.get('digit')==c['seq'][c['step']]:
                c['step']+=1
                if c['step']==4: p['done'].add(i); p['challenge']=None; check(r)
            else: c['step']=0
        elif t=='cancel': p['challenge']=None
        elif t=='kill' and titan and now>=p['kill_ready']:
            victims=[q for q in ps.values() if q['alive'] and q is not p and near(p,q)<45]
            if not victims: return 'Подойди ближе к разведчику.'
            q=min(victims,key=lambda q:near(p,q)); q.update(alive=False,challenge=None); p['kill_ready']=now+12; check(r)
        elif t=='sabotage' and titan and not r['sabotage'] and now>=r['sab_ready']:
            r.update(sabotage=now+30,sab_ready=now+55,notice='ТРЕВОГА! Ворота повреждены!')
        elif t=='report' and any(not q['alive'] and not q['reported'] and near(p,q)<65 for q in ps.values()): meeting(r,'Обнаружен павший разведчик')
        elif t=='meet' and p['meet'] and not r['sabotage']: p['meet']=False; meeting(r,'Экстренный совет')

def snapshot(r,p):
    now=time.monotonic(); ps=r['players']; phase=r['phase']; titan=p['id']==r['titan']; scouts=[q for q in ps.values() if q['id']!=r['titan']]
    return dict(type='state',code=r['code'],host=r['host'],phase=phase,you=p['id'],role=('titan' if titan else 'scout') if phase!='lobby' else None,players=[{k:q[k] for k in ('id','name','color','x','y','alive','reported','ready')} for q in ps.values()],done=sorted(p['done']),challenge=p['challenge'],progress=sum(len(q['done']) for q in scouts)/max(1,6*len(scouts)),cooldown=max(0,p['kill_ready']-now),sab_ready=max(0,r['sab_ready']-now),sabotage=max(0,r['sabotage']-now) if r['sabotage'] else 0,meet=p['meet'],round=r['round'],remaining=max(0,r['deadline']-now),voted=p['id'] in r['votes'],vote_count=len(r['votes']),chat=r['chat'],notice=r['notice'],reason=r['reason'],winner=r['winner'],revealed=ps.get(r['titan'],{}).get('name') if phase=='end' else None)

async def ticker():
    last=time.monotonic()
    while True:
        await asyncio.sleep(.05); now=time.monotonic(); dt=min(.1,now-last); last=now
        for r in list(rooms.values()):
            if r['phase']=='game':
                if r['sabotage'] and now>=r['sabotage']: finish(r,'titan','Ворота разрушены.'); continue
                for p in r['players'].values():
                    if now-p['input_at']<.3 and not p['challenge']:
                        p['x']=max(20,min(700,p['x']+p['dx']*105*dt)); p['y']=max(25,min(390,p['y']+p['dy']*105*dt))
            elif r['phase']=='meeting' and now>=r['deadline']: resolve(r)

@asynccontextmanager
async def lifespan(app):
    task=asyncio.create_task(ticker())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError): await task

app=FastAPI(lifespan=lifespan)
@app.get('/')
async def index(): return FileResponse(ROOT/'index.html')
@app.get('/{asset}')
async def asset(asset: str):
    if asset in ('game.js','online.js','style.css'): return FileResponse(ROOT/asset)
    from fastapi import HTTPException
    raise HTTPException(404)

@app.websocket('/ws')
async def ws(sock: WebSocket):
    await sock.accept(); r=p=None; writer=None; errors=asyncio.Queue(maxsize=10)
    try:
        m=await asyncio.wait_for(sock.receive_json(),timeout=15)
        if not isinstance(m,dict) or m.get('type') not in ('create','join'):
            await sock.send_json({'type':'error','message':'Выберите создание или вход.'}); return
        if m['type']=='create':
            if len(rooms)>=100: await sock.send_json({'type':'error','message':'Сервер заполнен.'}); return
            r=create()
        else: r=rooms.get(str(m.get('code','')).upper().strip())
        if not r or r['phase']!='lobby' or len(r['players'])>=8:
            await sock.send_json({'type':'error','message':'Комната не найдена, заполнена или матч уже идёт.'}); return
        p=add(r,m.get('name','Разведчик'))
        async def send_states():
            while True:
                while not errors.empty(): await sock.send_json({'type':'error','message':errors.get_nowait()})
                await sock.send_json(snapshot(r,p)); await asyncio.sleep(.1)
        writer=asyncio.create_task(send_states())
        while True:
            m=await sock.receive_json()
            if not isinstance(m,dict): continue
            err=action(r,p,m)
            if err and not errors.full(): errors.put_nowait(err)
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, TypeError, OverflowError): pass
    finally:
        if writer:
            writer.cancel()
            with suppress(asyncio.CancelledError,WebSocketDisconnect,RuntimeError): await writer
        if p and r:
            r['players'].pop(p['id'],None); r['votes'].pop(p['id'],None)
            if not r['players']: rooms.pop(r['code'],None)
            else:
                if r['host']==p['id']: r['host']=next(iter(r['players']))
                check(r)
        with suppress(RuntimeError,WebSocketDisconnect): await sock.close()
