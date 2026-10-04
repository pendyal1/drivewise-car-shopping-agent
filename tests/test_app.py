import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
import app


class Reply:
    def __init__(self, content=None, calls=None): self.content, self.tool_calls = content, calls or []
    def model_dump(self, **kwargs):
        result={'role':'assistant','content':self.content}
        if self.tool_calls:
            result['tool_calls']=[{'id':c.id,'type':'function','function':{'name':c.function.name,'arguments':c.function.arguments}} for c in self.tool_calls]
        return result


def call(name='calculate_ownership_cost', args=None, call_id='call1'):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=args if args is not None else json.dumps({'vehicle_price':24000,'down_payment':5000,'apr_percent':0,'loan_months':60,'mpg':48})))


def mock_model(monkeypatch, replies):
    replies=iter(replies)
    observed=[]
    def completion(**kw):
        observed.append([dict(x) for x in kw['messages']])
        assert kw['model']=='vertex_ai/gemini-3.5-flash-lite' and kw['vertex_location']=='global'
        if kw.get('tool_choice') == 'none':
            assert 'tools' not in kw
        else:
            assert len(kw['tools']) == 5
        return SimpleNamespace(choices=[SimpleNamespace(message=next(replies))])
    monkeypatch.setattr(app.litellm,'completion',completion)
    return observed


def test_chat_exact_shape_and_tool_history(monkeypatch):
    observed=mock_model(monkeypatch,[Reply(calls=[call()]),Reply('Monthly estimate calculated.')])
    data=TestClient(app.app).post('/chat',json={'message':'Estimate my budget.'}).json()
    assert set(data)=={'response','session_id','tool_calls'}
    assert set(data['tool_calls'][0])=={'name','args','result'}
    assert isinstance(data['tool_calls'][0]['result'],str)
    assert json.loads(data['tool_calls'][0]['result'])['amount_financed']==19000
    history=app.sessions.records[data['session_id']].messages
    assert [x['role'] for x in history]==['system','user','assistant','tool','assistant']
    assert observed[1][-1]['tool_call_id']=='call1'


def test_memory_and_separation(monkeypatch):
    observed=mock_model(monkeypatch,[Reply('Honda'),Reply('Toyota'),Reply('Same Honda')])
    client=TestClient(app.app)
    a=client.post('/chat',json={'message':'Honda Accord Hybrid'}).json()
    b=client.post('/chat',json={'message':'Toyota Camry Hybrid'}).json()
    c=client.post('/chat',json={'message':'that same vehicle','session_id':a['session_id']}).json()
    assert a['session_id']==c['session_id']!=b['session_id']
    assert any(m.get('content')=='Honda Accord Hybrid' for m in observed[2])
    assert not any(m.get('content')=='Toyota Camry Hybrid' for m in observed[2])


def test_clear_one_session(monkeypatch):
    mock_model(monkeypatch,[Reply('a'),Reply('b')])
    client=TestClient(app.app)
    a=client.post('/chat',json={'message':'a'}).json()['session_id']
    b=client.post('/chat',json={'message':'b'}).json()['session_id']
    assert client.post('/clear',json={'session_id':a}).json()=={'status':'ok'}
    assert a not in app.sessions.records and b in app.sessions.records
    assert client.post('/clear',json={}).status_code==422


def test_model_failure_safe(monkeypatch):
    def fail(**kw): raise RuntimeError('SECRET_CREDENTIAL https://private?key=VALUE')
    monkeypatch.setattr(app.litellm,'completion',fail)
    response=TestClient(app.app).post('/chat',json={'message':'hello'})
    assert response.status_code==200
    assert 'SECRET' not in response.text and 'VALUE' not in response.text
    assert response.json()['tool_calls']==[]


def test_model_failure_after_tool_preserves_results(monkeypatch):
    mock_model(monkeypatch,[Reply(calls=[call()])])
    data=TestClient(app.app).post('/chat',json={'message':'cost'}).json()
    assert len(data['tool_calls'])==1 and 'unavailable' in data['response']


@pytest.mark.parametrize('arguments',['bad SECRET','[]','null','{"mpg":'])
def test_bad_model_args(monkeypatch,arguments):
    mock_model(monkeypatch,[Reply(calls=[call(args=arguments)]),Reply('Please clarify.')])
    data=TestClient(app.app).post('/chat',json={'message':'cost'}).json()
    assert json.loads(data['tool_calls'][0]['result'])['error']=='invalid_input'
    assert data['tool_calls'][0]['args']=={}


def test_tool_round_configuration(monkeypatch):
    monkeypatch.delenv('MAX_TOOL_ROUNDS', raising=False)
    assert app.max_tool_rounds() == 10
    monkeypatch.setenv('MAX_TOOL_ROUNDS', '4')
    assert app.max_tool_rounds() == 4
    monkeypatch.setenv('MAX_TOOL_ROUNDS', '99')
    assert app.max_tool_rounds() == 12
    monkeypatch.setenv('MAX_TOOL_ROUNDS', '0')
    assert app.max_tool_rounds() == 1
    monkeypatch.setenv('MAX_TOOL_ROUNDS', 'invalid')
    assert app.max_tool_rounds() == 10


def test_multiple_calls_one_round_and_preserved(monkeypatch):
    calls=[call(args=json.dumps({'vehicle_price':24000,'down_payment':5000,'apr_percent':0,'loan_months':60,'mpg':48}),call_id='a'),
           call('check_vehicle_recalls',json.dumps({'year':2022,'make':'Honda','model':'Accord'}),call_id='b')]
    mock_model(monkeypatch,[Reply(calls=calls),Reply('Combined answer.')])
    data=TestClient(app.app).post('/chat',json={'message':'research'}).json()
    assert data['response']=='Combined answer.' and [item['name'] for item in data['tool_calls']]==['calculate_ownership_cost','check_vehicle_recalls']
    history=app.sessions.records[data['session_id']].messages
    assert sum(message['role']=='tool' for message in history)==2


def test_duplicate_tool_call_is_recorded_but_not_executed(monkeypatch):
    model_call=call('unknown',json.dumps({'x':1}))
    executed=[]
    monkeypatch.setattr(app,'run_tool',lambda name,args: executed.append((name,args)) or json.dumps({'ok':True}))
    mock_model(monkeypatch,[Reply(calls=[model_call]),Reply(calls=[model_call]),Reply('Used existing evidence.')])
    data=TestClient(app.app).post('/chat',json={'message':'research'}).json()
    assert len(executed)==1 and len(data['tool_calls'])==2
    assert json.loads(data['tool_calls'][1]['result'])['error']=='duplicate_tool_call'


def test_round_limit_forces_final_synthesis(monkeypatch):
    monkeypatch.setenv('MAX_TOOL_ROUNDS','2')
    observed=mock_model(monkeypatch,[Reply(calls=[call('unknown','{}',call_id='a')]),
                                     Reply(calls=[call('unknown','{"next":1}',call_id='b')]),
                                     Reply('Final comparison from collected evidence.')])
    data=TestClient(app.app).post('/chat',json={'message':'research'}).json()
    assert data['response']=='Final comparison from collected evidence.'
    assert len(data['tool_calls'])==2 and len(observed)==3
    assert 'Research for this user turn is complete' in observed[-1][-1]['content']
    assert all('five-round research limit' not in (message.get('content') or '') for message in app.sessions.records[data['session_id']].messages)


def test_no_infinite_loop_and_synthesis_fallback(monkeypatch):
    monkeypatch.setenv('MAX_TOOL_ROUNDS','3')
    repeated=call('unknown','{}')
    invoked=[]
    def completion(**kwargs):
        invoked.append(kwargs)
        if kwargs.get('tool_choice')=='none':
            raise RuntimeError('synthesis failed')
        return SimpleNamespace(choices=[SimpleNamespace(message=Reply(calls=[repeated]))])
    monkeypatch.setattr(app.litellm,'completion',completion)
    data=TestClient(app.app).post('/chat',json={'message':'research'}).json()
    assert len(invoked)==4 and len(data['tool_calls'])==3
    assert data['response'].startswith('I completed the available research')


@pytest.mark.parametrize('body',[{'message':''},{'message':'   '},{'message':'x'*8001},{'message':'x','session_id':'SECRET'},{'message':'x','extra':'SECRET'},{}])
def test_validation_safe(body):
    response=TestClient(app.app).post('/chat',json=body)
    assert response.status_code==422 and 'SECRET' not in response.text
    assert response.json()['error']=='invalid_input'


def test_health_root_and_headers():
    client=TestClient(app.app)
    assert client.get('/health').json()=={'status':'ok'}
    root=client.get('/')
    assert root.status_code==200 and 'text/html' in root.headers['content-type']
    assert 'DriveWise' in root.text and 'New conversation' in root.text
    assert 'sessionStorage' in root.text and 'renderMarkdown' in root.text
    assert 'heavy rain and occasional snow and ice' in root.text
    assert 'Configuration resolution &amp; confidence' in root.text or 'Configuration resolution & confidence' in root.text
    assert 'This tab restored an active server conversation.' in root.text
    assert 'innerHTML' not in root.text
    assert "safeSourceHosts = new Set(['www.fueleconomy.gov','www.nhtsa.gov','api.nhtsa.gov'])" in root.text
    assert root.headers['x-content-type-options']=='nosniff'


def test_expiration_and_capacity():
    store=app.SessionStore(max_sessions=2,ttl=10)
    with store.conversation() as (a,msg): msg.append({'role':'user','content':'a'})
    with store.conversation() as (b,msg): pass
    with store.conversation() as (c,msg): pass
    assert len(store.records)==2 and a not in store.records
    store.records[b].updated-=11
    with store.conversation(b) as (d,msg):
        assert d!=b and len(msg)==1
    assert len(store.records)==2


def test_active_session_not_evicted():
    store=app.SessionStore(max_sessions=1)
    with store.conversation():
        with pytest.raises(app.HTTPException) as exc:
            with store.conversation(): pass
        assert exc.value.status_code==503


def test_message_limit_keeps_complete_turns():
    store=app.SessionStore(max_messages=6)
    with store.conversation() as (sid, messages):
        messages += [{'role':'user','content':'old'}, {'role':'assistant','tool_calls':[{'id':'old'}]}, {'role':'tool','tool_call_id':'old'}, {'role':'assistant','content':'done'}, {'role':'user','content':'new'}, {'role':'assistant','content':'answer'}]
    messages=store.records[sid].messages
    assert len(messages)==3 and messages[1]['content']=='new'
    assert not any(m['role']=='tool' for m in messages)


def test_same_session_concurrency():
    store=app.SessionStore()
    with store.conversation() as (sid,messages): pass
    def append_turn(i):
        with store.conversation(sid) as (_,messages):
            messages.extend([{'role':'user','content':str(i)},{'role':'assistant','content':str(i)}])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append_turn,range(20)))
    messages=store.records[sid].messages
    assert len(messages)==41
    assert all(messages[i]['content']==messages[i+1]['content'] for i in range(1,40,2))
