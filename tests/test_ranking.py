import tools
import pytest

CANDIDATES = [
 {'year':2022,'make':'Honda','model':'Accord Hybrid','vehicle_id':1,'listing_price':24000},
 {'year':2022,'make':'Toyota','model':'Camry Hybrid','vehicle_id':2,'listing_price':26000},
 {'year':2021,'make':'Subaru','model':'Legacy','vehicle_id':3,'listing_price':23000},
]
LIFESTYLE = {'maximum_purchase_price':25000,'maximum_monthly_ownership_cost':750,'down_payment':5000,'apr_percent':6.5,
             'loan_months':60,'annual_miles':15000,'city_driving_share':.6,'regular_passenger_count':4,
             'cargo_need':'medium','parking_constraint':'tight','winter_weather_need':True,'expected_ownership_years':5}


def test_deterministic_ranking_and_weights(government):
    a = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES, lifestyle=LIFESTYLE)
    b = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES, lifestyle=LIFESTYLE)
    assert a == b
    assert sum(a['weights'].values()) == 100
    for row in a['ranked_vehicles']:
        assert 0 <= row['total_fit_score'] <= 100
        assert abs(sum(row['component_scores'].values()) - row['total_fit_score']) < .003
        assert row['strongest_fit_reasons'] and row['government_sources_used']
        assert row['ownership_cost']
        assert row['ownership_cost']['government_source_data']['mpg'] > 0
        assert 'mpg' not in row['ownership_cost']['user_provided_inputs']
    assert a['ranked_vehicles'][0]['candidate']['make'] == 'Toyota'


def test_hard_constraints_are_separate(government):
    result = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES, lifestyle=LIFESTYLE)
    toyota = next(r for r in result['ranked_vehicles'] if r['candidate']['make']=='Toyota')
    assert 'firm purchase budget' in toyota['hard_constraint_failures'][0]
    assert toyota['total_fit_score'] > 0


def test_missing_data_neutral(monkeypatch):
    monkeypatch.setattr(tools, 'get_vehicle_efficiency', lambda **kw: {'error':'timeout'})
    monkeypatch.setattr(tools, 'get_vehicle_safety_profile', lambda **kw: {'overall_rating':None,'missing_data':[]})
    result = tools.rank_vehicles_for_lifestyle(candidates=[{k:v for k,v in c.items() if k in ('year','make','model')} for c in CANDIDATES[:2]])
    for row in result['ranked_vehicles']:
        assert row['total_fit_score'] == 50 and row['data_completeness'] == 0
        assert row['component_scores'] == {k:v/2 for k,v in tools.BASE_WEIGHTS.items()}
        assert row['ownership_cost'] is None
    assert [r['original_order'] for r in result['ranked_vehicles']] == [0,1]


def test_tie_completeness_then_original_order(monkeypatch):
    monkeypatch.setattr(tools, 'get_vehicle_efficiency', lambda **kw: {'emissions_score':5} if kw['make']=='Toyota' else {})
    monkeypatch.setattr(tools, 'get_vehicle_safety_profile', lambda **kw: {})
    result = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES)
    assert [r['original_order'] for r in result['ranked_vehicles']] == [1,0,2]
    assert all(r['total_fit_score']==50 for r in result['ranked_vehicles'])


def test_priorities_adjust_weights(government):
    result = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES, lifestyle={'safety_priority':'high','environmental_priority':'low'})
    assert result['weights']['safety'] > 15 and result['weights']['environment'] < 10
    assert sum(result['weights'].values()) == pytest.approx(100)


def test_recall_count_not_safety_penalty(monkeypatch, government):
    before = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES, lifestyle=LIFESTYLE)
    original = tools.get_vehicle_safety_profile
    monkeypatch.setattr(tools, 'get_vehicle_safety_profile', lambda **kw: {**original(**kw),'recall_count':99999})
    after = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES, lifestyle=LIFESTYLE)
    assert [r['total_fit_score'] for r in before['ranked_vehicles']] == [r['total_fit_score'] for r in after['ranked_vehicles']]


def test_ev_charging_hard_conflict(government):
    government[0][1].update(atvType='EV', fuelType1='Electricity', comb08=110, combE=30)
    result = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES[:2], lifestyle={**LIFESTYLE,'willing_to_charge_ev':False})
    ev = next(r for r in result['ranked_vehicles'] if r['original_order']==0)
    assert ev['component_scores']['energy'] == 0
    assert any('unwillingness to charge' in x for x in ev['hard_constraint_failures'])
    assert ev['ownership_cost']['estimated_electricity_monthly'] == 60


def test_ambiguous_efficiency_stays_missing(monkeypatch, government):
    monkeypatch.setattr(tools, 'get_vehicle_efficiency', lambda **kw: {'ambiguous':True, 'options':[{'value':'1'},{'value':'2'}]})
    result = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES[:2], lifestyle=LIFESTYLE)
    for row in result['ranked_vehicles']:
        assert row['ownership_cost'] is None
        assert row['component_scores']['energy']==10
        assert row['missing_or_ambiguous_data'][0]['detail']['ambiguous']


@pytest.mark.parametrize('candidates', [[], CANDIDATES[:1], CANDIDATES*2, [{**CANDIDATES[0],'vin':'invalid'},CANDIDATES[1]], [{**CANDIDATES[0],'mileage':-1},CANDIDATES[1]]])
def test_candidate_limits(candidates):
    assert tools.rank_vehicles_for_lifestyle(candidates=candidates)['error']=='invalid_input'


def test_vin_is_never_requested(government):
    result = tools.rank_vehicles_for_lifestyle(candidates=[{**CANDIDATES[0],'vin':'1HGCM82633A004352'},CANDIDATES[1]])
    assert not any('vpic' in url.lower() or 'vin' in url.lower() for url, kw in government[1])
    assert any('context only' in t for t in result['ranked_vehicles'][0]['tradeoffs']+result['ranked_vehicles'][1]['tradeoffs'])


def test_fallback_rating_is_neutral(monkeypatch, government):
    original=tools.get_vehicle_safety_profile
    monkeypatch.setattr(tools,'get_vehicle_safety_profile',lambda **kw: {**original(**kw),'rating_scope_matches_candidate':False})
    result=tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES[:2],lifestyle=LIFESTYLE)
    assert all(row['component_scores']['safety']==7.5 for row in result['ranked_vehicles'])


def test_internal_menu_cache(government):
    candidates=[{k:v for k,v in c.items() if k!='vehicle_id'} for c in CANDIDATES]
    tools.rank_vehicles_for_lifestyle(candidates=candidates)
    assert sum(url.endswith('/menu/year') for url,kw in government[1])==1


def test_blended_phev_not_estimated(government):
    government[0][1].update(atvType='Plug-in Hybrid',combE=30,combA08=100,phevBlended='true')
    result=tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES[:2],lifestyle={**LIFESTYLE,'electric_mile_share':.6})
    row=next(r for r in result['ranked_vehicles'] if r['original_order']==0)
    assert row['ownership_cost'] is None
    assert any('Blended' in str(x) for x in row['missing_or_ambiguous_data'])
