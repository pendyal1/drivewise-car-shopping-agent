import json
import math
import pytest
import requests
from jsonschema import Draft202012Validator
import tools
from conftest import Response

BASE = {'vehicle_price': 24000, 'down_payment': 5000, 'apr_percent': 6.5, 'loan_months': 60, 'annual_miles': 15000, 'mpg': 48}
IDENTITY = {'year': 2022, 'make': 'Honda', 'model': 'Accord Hybrid'}


def test_cost_normal():
    result = tools.calculate_ownership_cost(**BASE)
    assert result['amount_financed'] == 19000
    assert result['estimated_loan_payment_monthly'] == 371.76
    assert result['estimated_total_interest_over_loan'] == 3305.41
    assert result['estimated_fuel_monthly'] == 91.15
    assert result['estimated_total_monthly'] == 712.9
    assert result['defaults_and_assumptions']['monthly_insurance_estimate'] == 175
    assert result['government_source_data'] == {}
    assert 'taxes' in result['excluded']


def test_zero_apr_and_cash():
    result = tools.calculate_ownership_cost(**{**BASE, 'apr_percent': 0})
    assert result['estimated_loan_payment_monthly'] == 316.67
    assert result['estimated_total_interest_over_loan'] == 0
    result = tools.calculate_ownership_cost(**{**BASE, 'down_payment': 24000})
    assert result['amount_financed'] == result['estimated_loan_payment_monthly'] == 0


@pytest.mark.parametrize('key,value', [('vehicle_price', 0), ('vehicle_price', -1), ('down_payment', -1), ('down_payment', 25000),
    ('apr_percent', -1), ('apr_percent', 41), ('loan_months', 0), ('loan_months', 97), ('loan_months', 60.5),
    ('annual_miles', -1), ('annual_miles', 200001), ('mpg', 0), ('mpg', -2), ('mpg', 201),
    ('monthly_insurance_estimate', -1), ('monthly_maintenance_reserve', -1), ('fuel_price_per_gallon', -1),
    ('electricity_price_per_kwh', -1), ('vehicle_price', float('nan')), ('apr_percent', float('inf')), ('mpg', '48')])
def test_invalid_cost(key, value):
    assert tools.calculate_ownership_cost(**{**BASE, key: value})['error'] == 'invalid_input'


@pytest.mark.parametrize('kind', ['gasoline', 'hybrid', 'diesel'])
def test_combustion_kinds(kind):
    result = tools.calculate_ownership_cost(**BASE, fuel_type=kind)
    assert result['estimated_fuel_monthly'] == 91.15
    assert result['estimated_electricity_monthly'] == 0


def test_ev_mpge_and_kwh():
    inputs = {k: v for k, v in BASE.items() if k != 'mpg'}
    result = tools.calculate_ownership_cost(**inputs, fuel_type='electric', kwh_per_100_miles=30)
    assert result['estimated_electricity_monthly'] == 60
    assert result['estimated_fuel_monthly'] == 0
    result = tools.calculate_ownership_cost(**inputs, fuel_type='electric', mpge=112.33333333333333)
    assert result['estimated_electricity_monthly'] == 60


def test_phev():
    result = tools.calculate_ownership_cost(**BASE, fuel_type='plug_in_hybrid', kwh_per_100_miles=30, electric_mile_share=.6)
    assert result['estimated_electricity_monthly'] == 36
    assert result['estimated_fuel_monthly'] == 36.46
    assert 'split-mile' in result['method']


@pytest.mark.parametrize('extra', [ {'fuel_type':'electric'}, {'fuel_type':'electric','mpge':100},
    {'fuel_type':'plug_in_hybrid'}, {'fuel_type':'plug_in_hybrid','kwh_per_100_miles':30},
    {'fuel_type':'plug_in_hybrid','kwh_per_100_miles':30,'electric_mile_share':1.1},
    {'kwh_per_100_miles':30}, {'electric_mile_share':.2}, {'kwh_per_100_miles':0}, {'fuel_type':'nuclear'}])
def test_invalid_energy(extra):
    assert tools.calculate_ownership_cost(**{**BASE, **extra})['error'] == 'invalid_input'


def test_conflicting_ev_units():
    inputs = {k:v for k,v in BASE.items() if k != 'mpg'}
    assert tools.calculate_ownership_cost(**inputs, fuel_type='electric', mpge=100, kwh_per_100_miles=30)['error'] == 'invalid_input'


def test_provenance():
    result = tools.calculate_ownership_cost(**BASE, energy_data_source='government_epa')
    assert result['government_source_data'] == {'mpg': 48}
    assert 'mpg' not in result['user_provided_inputs']


def test_efficiency_normalization(government):
    result = tools.get_vehicle_efficiency(**IDENTITY)
    assert result['combined_mpg'] == 48
    assert result['fuel_type'] == 'hybrid'
    assert result['vehicle_id'] == 1
    assert result['engine']['displacement_liters'] == 2
    assert result['luggage_volume_cu_ft'] == 17
    assert result['annual_fuel_cost_usd'] == 1100
    assert len(government[1]) == 5


def test_multiple_configurations(monkeypatch, government):
    original = tools.requests.get
    def get(url, **kw):
        if '/options' in url:
            return Response({'menuItem':[{'value':'1','text':'Automatic FWD'}, {'value':'10','text':'Automatic AWD'}]})
        return original(url, **kw)
    monkeypatch.setattr(tools.requests, 'get', get)
    result = tools.get_vehicle_efficiency(**IDENTITY)
    assert result['ambiguous'] and len(result['options']) == 2
    assert not any('/vehicle/1' in url for url, kw in government[1])
    assert tools.get_vehicle_efficiency(**IDENTITY, configuration='FWD')['vehicle_id'] == 1


def test_xml_fallback(monkeypatch):
    monkeypatch.setattr(tools.requests, 'get', lambda *a, **kw: Response(raw=b'<vehicle><id>1</id><year>2022</year><make>Honda</make><model>Accord Hybrid</model><comb08>48</comb08><fuelType1>Regular Gasoline</fuelType1><atvType>Hybrid</atvType></vehicle>'))
    assert tools.get_vehicle_efficiency(**IDENTITY, vehicle_id=1)['combined_mpg'] == 48


def test_xml_menu_repeated_and_single(monkeypatch):
    monkeypatch.setattr(tools.requests, 'get', lambda *a, **kw: Response(raw=b'<menuItems><menuItem><text>2022</text><value>2022</value></menuItem><menuItem><text>2021</text><value>2021</value></menuItem></menuItems>'))
    assert len(tools._menu('year')) == 2


@pytest.mark.parametrize('raw', [b'<!DOCTYPE x [<!ENTITY x "SECRET">]><x>&x;</x>', b'<bad', b'[1,2]', b'{"x": NaN}', b'not json SECRET'])
def test_malformed_and_unsafe_xml(monkeypatch, raw):
    monkeypatch.setattr(tools.requests, 'get', lambda *a, **kw: Response(raw=raw))
    result = tools.safe_get(tools.FE + '/vehicle/1')
    assert result['error'] == 'malformed_response'
    assert 'SECRET' not in json.dumps(result)


def test_missing_vehicle_fields(government):
    records, _ = government
    records[1] = {k: records[1][k] for k in ('id', 'year', 'make', 'model')}
    result = tools.get_vehicle_efficiency(**IDENTITY, vehicle_id=1)
    assert result['combined_mpg'] is None
    assert 'combined_mpg' in result['unavailable_fields']
    assert result['fuel_type'] is None


def test_ev_and_phev_normalization(government):
    records, _ = government
    records[1].update(atvType='EV', fuelType1='Electricity', comb08=110, combE=30, range=260, charge240=7)
    result = tools.get_vehicle_efficiency(**IDENTITY, vehicle_id=1)
    assert result['combined_mpg'] is None and result['combined_mpge'] == 110
    assert result['ev_range_miles'] == 260 and result['charge_240v_hours'] == 7
    records[1].update(atvType='Plug-in Hybrid', fuelType1='Regular Gasoline', comb08=48, combA08=100, rangeA=35)
    result = tools.get_vehicle_efficiency(**IDENTITY, vehicle_id=1)
    assert result['combined_mpg'] == 48 and result['combined_mpge'] == 100
    assert result['ev_range_miles'] == 35


def test_vehicle_id_identity_mismatch(government):
    assert tools.get_vehicle_efficiency(**IDENTITY, vehicle_id=2)['error'] == 'no_matching_data'


def test_missing_model_options(government):
    result = tools.get_vehicle_efficiency(**{**IDENTITY, 'model':'Accord'})
    assert result['error'] == 'no_matching_data' and result['model_options']


def test_recall_parsing(government):
    result = tools.check_vehicle_recalls(**IDENTITY)
    assert result['recall_count'] == 1
    assert result['recalls'][0]['campaign_number'] == '22V001000'
    assert result['recalls'][0]['recall_date'] == '01/01/2022'
    assert 'completed' in result['note']


def test_empty_recalls(monkeypatch):
    monkeypatch.setattr(tools.requests, 'get', lambda *a, **kw: Response({'results':[]}))
    result = tools.check_vehicle_recalls(**IDENTITY)
    assert result['recalls'] == [] and result['recall_count'] == 0
    assert result['lookup_scope'] == 'model_level'


def test_bounded_recall_output(monkeypatch):
    monkeypatch.setattr(tools.requests, 'get', lambda *a, **kw: Response({'results':[{'Summary':'x'*4000}]*30}))
    result = tools.check_vehicle_recalls(**IDENTITY)
    assert len(result['recalls']) == 8 and result['recall_count'] == 30 and result['truncated']
    assert len(result['recalls'][0]['summary']) == 1200


def test_safety_variants_and_complaint_limitations(government):
    result = tools.get_vehicle_safety_profile(**IDENTITY)
    assert result['overall_rating'] == 5 and result['frontal_rating'] == 4
    assert result['side_rating'] == 5 and result['rollover_rating'] == 4
    assert result['recall_count'] == 1 and result['complaint_count_bounded'] == 1
    assert 'not necessarily verified' in result['complaint_limitations']
    assert 'sales volume' in result['complaint_limitations']
    assert 'Sensitive consumer' not in json.dumps(result)
    assert any('/VehicleId/100' in u for u, kw in government[1])


def test_missing_safety_is_partial(monkeypatch):
    monkeypatch.setattr(tools.requests, 'get', lambda *a, **kw: Response({'Results':[]}))
    result = tools.get_vehicle_safety_profile(**IDENTITY)
    assert result['overall_rating'] is None and result['recall_count'] == 0
    assert result['missing_data']


def test_safety_conflicting_variants(monkeypatch, government):
    original = tools.requests.get
    def get(url, **kw):
        if '/modelyear/' in url: return Response({'Results':[{'VehicleId':100}, {'VehicleId':101}]})
        if '/VehicleId/101' in url: return Response({'Results':[{'OverallRating':'4','RolloverRating':'Not Rated'}]})
        return original(url, **kw)
    monkeypatch.setattr(tools.requests, 'get', get)
    result = tools.get_vehicle_safety_profile(**IDENTITY)
    assert result['overall_rating'] is None and result['ambiguous']
    assert result['variants'][1]['overall_rating'] == 4


@pytest.mark.parametrize('status,code', [(429,'rate_limited'),(401,'upstream_authentication'),(403,'upstream_authentication'),
                                       (400,'upstream_invalid_request'),(422,'upstream_invalid_request'),(404,'no_matching_data'),(500,'upstream_unavailable'),(503,'upstream_unavailable'),(302,'upstream_unavailable')])
def test_http_errors_safe(monkeypatch, status, code):
    monkeypatch.setattr(tools.requests, 'get', lambda *a, **kw: Response(status=status, raw=b'SECRET_TOKEN https://internal?key=PRIVATE'))
    result = tools.safe_get(tools.FE + '/vehicle/1')
    assert result['error'] == code
    assert 'SECRET' not in json.dumps(result) and 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('exception,code', [(requests.Timeout('PRIVATE environment token'), 'timeout'), (requests.ConnectionError('PRIVATE headers'), 'upstream_unavailable')])
def test_request_exceptions_safe(monkeypatch, exception, code):
    def fail(*a, **kw): raise exception
    monkeypatch.setattr(tools.requests, 'get', fail)
    result = tools.get_vehicle_efficiency(**IDENTITY, vehicle_id=1)
    assert result['error'] == code and 'PRIVATE' not in json.dumps(result)


def test_response_size_and_allowlist(monkeypatch):
    monkeypatch.setattr(tools.requests, 'get', lambda *a, **kw: Response(raw=b'x'*(tools.MAX_BYTES+1)))
    assert tools.safe_get(tools.FE + '/vehicle/1')['error'] == 'malformed_response'
    assert tools.safe_get('https://evil.test/')['error'] == 'invalid_input'
    assert tools.safe_get(tools.FE + '/vehicle/1?secret=value')['error'] == 'invalid_input'


def test_dispatch_unknown_and_failure(monkeypatch):
    assert json.loads(tools.run_tool('PRIVATE unknown', {}))['error'] == 'unknown_tool'
    assert 'PRIVATE' not in tools.run_tool('PRIVATE unknown', {})
    def fail(**kw): raise RuntimeError('SECRET stack env token')
    monkeypatch.setitem(tools.IMPLEMENTATIONS, 'check_vehicle_recalls', fail)
    result = tools.run_tool('check_vehicle_recalls', IDENTITY)
    assert json.loads(result)['error'] == 'tool_failed' and 'SECRET' not in result


@pytest.mark.parametrize('args', [None, [], 'SECRET', {}, {'year':2022}, {**IDENTITY,'secret':'SECRET'}, {**IDENTITY,'year':True}, {**IDENTITY,'make':''}])
def test_invalid_dispatch(args):
    result = tools.run_tool('check_vehicle_recalls', args)
    assert json.loads(result)['error'] == 'invalid_input' and 'SECRET' not in result


def test_schemas():
    assert len(tools.TOOLS) == 5
    for tool in tools.TOOLS:
        Draft202012Validator.check_schema(tool['function']['parameters'])
        assert 'Do not' in tool['function']['description']


def test_hybrid_base_model_fallback_scope(monkeypatch, government):
    original=tools.requests.get
    def get(url, **kw):
        if '/model/Accord%20Hybrid' in url: return Response({'Results':[]})
        if '/recalls/' in url and kw['params']['model']=='Accord Hybrid': return Response({'results':[]})
        return original(url, **kw)
    monkeypatch.setattr(tools.requests,'get',get)
    recall=tools.check_vehicle_recalls(**IDENTITY)
    assert recall['lookup_vehicle']['model']=='Accord' and recall['recall_count']==1
    assert 'Base-model' in recall['scope_note']
    safety=tools.get_vehicle_safety_profile(**IDENTITY)
    assert safety['lookup_vehicle']['model']=='Accord'
    assert safety['rating_scope_matches_candidate'] is False
    assert safety['overall_rating']==5


def test_bounded_complaints(monkeypatch, government):
    original=tools.requests.get
    monkeypatch.setattr(tools.requests,'get',lambda url, **kw: Response({'results':[{'components':f'TEST {i}','summary':'PRIVATE VIN'} for i in range(110)]}) if '/complaints/' in url else original(url,**kw))
    result=tools.get_vehicle_safety_profile(**IDENTITY)
    assert result['complaint_count_bounded']==100 and result['complaint_count_capped']
    assert len(result['complaint_components_sample'])==5
    assert 'PRIVATE VIN' not in json.dumps(result)


def test_safe_request_deadline():
    token=tools._request_context.set({'deadline':0,'cache':{}})
    try:
        assert tools.safe_get(tools.FE+'/vehicle/1')['error']=='timeout'
    finally:
        tools._request_context.reset(token)


def test_unavailable_ratings_still_returns_other_evidence(monkeypatch, government):
    original=tools.requests.get
    monkeypatch.setattr(tools.requests,'get',lambda url, **kw: Response(status=503) if '/SafetyRatings/' in url else original(url,**kw))
    result=tools.get_vehicle_safety_profile(**IDENTITY)
    assert result['overall_rating'] is None
    assert result['recall_count']==1 and result['complaint_count_bounded']==1
    assert any(x.get('error')=='upstream_unavailable' for x in result['missing_data'])


def test_malformed_recall_shape(monkeypatch):
    monkeypatch.setattr(tools.requests,'get',lambda *a,**kw: Response({'results':'PRIVATE invalid'}))
    result=tools.check_vehicle_recalls(**IDENTITY)
    assert result['error']=='malformed_response' and 'PRIVATE' not in json.dumps(result)


def test_upstream_error_object_cannot_leak(monkeypatch):
    monkeypatch.setattr(tools.requests,'get',lambda *a,**kw: Response({'error':'SECRET_KEY','headers':{'Authorization':'PRIVATE'},'url':'https://internal?key=SECRET'}))
    result=tools.get_vehicle_efficiency(**IDENTITY,vehicle_id=1)
    assert result['error']=='malformed_response'
    assert all(value not in json.dumps(result) for value in ('SECRET_KEY','PRIVATE','internal?'))
