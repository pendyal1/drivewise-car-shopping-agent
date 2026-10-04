import tools
import pytest

CANDIDATES = [
 {'year':2022,'make':'Honda','model':'Accord Hybrid','vehicle_id':1,'listing_price':24000},
 {'year':2022,'make':'Toyota','model':'Camry Hybrid','vehicle_id':2,'listing_price':26000},
 {'year':2021,'make':'Subaru','model':'Legacy','vehicle_id':3,'listing_price':23000},
]
LIFESTYLE = {'maximum_purchase_price':25000,'maximum_monthly_ownership_cost':750,'down_payment':5000,'apr_percent':6.5,
             'loan_months':60,'annual_miles':15000,'city_driving_share':.6,'regular_passenger_count':4,
             'cargo_need':'medium','parking_constraint':'tight','weather_conditions':['heavy_rain','light_snow','ice'],
             'road_conditions':['mostly_city','potholes'],'charging_access':'none','expected_ownership_years':5}


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
    monkeypatch.setattr(tools, '_get_vehicle_efficiency', lambda **kw: {'error':'timeout'})
    monkeypatch.setattr(tools, '_get_vehicle_safety_profile', lambda **kw: {'overall_rating':None,'missing_data':[]})
    monkeypatch.setattr(tools, '_check_vehicle_recalls', lambda **kw: {'recalls':[],'sources':[]})
    result = tools.rank_vehicles_for_lifestyle(candidates=[{k:v for k,v in c.items() if k in ('year','make','model')} for c in CANDIDATES[:2]])
    for row in result['ranked_vehicles']:
        assert row['total_fit_score'] == 50 and row['data_completeness'] == 0
        assert row['component_scores'] == {k:v/2 for k,v in tools.BASE_WEIGHTS.items()}
        assert row['ownership_cost'] is None
    assert [r['original_order'] for r in result['ranked_vehicles']] == [0,1]


def test_tie_completeness_then_original_order(monkeypatch):
    monkeypatch.setattr(tools, '_get_vehicle_efficiency', lambda **kw: {'emissions_score':5} if kw['make']=='Toyota' else {})
    monkeypatch.setattr(tools, '_get_vehicle_safety_profile', lambda **kw: {})
    monkeypatch.setattr(tools, '_check_vehicle_recalls', lambda **kw: {'recalls':[],'sources':[]})
    result = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES)
    assert [r['original_order'] for r in result['ranked_vehicles']] == [1,0,2]
    assert all(r['total_fit_score']==50 for r in result['ranked_vehicles'])


def test_priorities_adjust_weights(government):
    result = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES, lifestyle={'safety_priority':'high','environmental_priority':'low'})
    assert result['weights']['safety'] > 15 and result['weights']['environment'] < 10
    assert sum(result['weights'].values()) == pytest.approx(100)


def test_recall_count_not_safety_penalty(monkeypatch, government):
    before = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES, lifestyle=LIFESTYLE)
    original = tools._get_vehicle_safety_profile
    monkeypatch.setattr(tools, '_get_vehicle_safety_profile', lambda **kw: {**original(**kw),'recall_count':99999})
    after = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES, lifestyle=LIFESTYLE)
    assert [r['total_fit_score'] for r in before['ranked_vehicles']] == [r['total_fit_score'] for r in after['ranked_vehicles']]


def test_ev_charging_hard_conflict(government):
    government[0][1].update(atvType='EV', fuelType1='Electricity', comb08=110, combE=30)
    result = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES[:2], lifestyle={**LIFESTYLE,'charging_access':'none','hard_constraints':['charging_access']})
    ev = next(r for r in result['ranked_vehicles'] if r['original_order']==0)
    assert ev['component_scores']['energy'] < 10
    assert any('charging-access hard constraint' in x for x in ev['hard_constraint_failures'])
    assert ev['ownership_cost']['estimated_electricity_monthly'] == 60


def test_configuration_ambiguity_is_disclosed_not_zeroed(government):
    result = tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES[:2], lifestyle=LIFESTYLE)
    for row in result['ranked_vehicles']:
        assert row['configuration_resolution']['mode'] in ('exact','matched','representative','ambiguous')
        assert row['automatic_selection_confidence'] in ('high','medium','low')


@pytest.mark.parametrize('candidates', [[], CANDIDATES[:1], CANDIDATES*2, [{**CANDIDATES[0],'vin':'invalid'},CANDIDATES[1]], [{**CANDIDATES[0],'mileage':-1},CANDIDATES[1]]])
def test_candidate_limits(candidates):
    assert tools.rank_vehicles_for_lifestyle(candidates=candidates)['error']=='invalid_input'


def test_vin_is_never_requested(government):
    result = tools.rank_vehicles_for_lifestyle(candidates=[{**CANDIDATES[0],'vin':'1HGCM82633A004352'},CANDIDATES[1]])
    assert not any('vpic' in url.lower() or 'vin' in url.lower() for url, kw in government[1])
    assert any('context only' in t for t in result['ranked_vehicles'][0]['tradeoffs']+result['ranked_vehicles'][1]['tradeoffs'])


def test_fallback_rating_is_neutral(monkeypatch, government):
    original=tools._get_vehicle_safety_profile
    monkeypatch.setattr(tools,'_get_vehicle_safety_profile',lambda **kw: {**original(**kw),'rating_scope_matches_candidate':False})
    result=tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES[:2],lifestyle=LIFESTYLE)
    assert all(row['component_scores']['safety']==7.5 for row in result['ranked_vehicles'])


def test_internal_menu_cache(government):
    candidates=[{k:v for k,v in c.items() if k!='vehicle_id'} for c in CANDIDATES]
    tools.rank_vehicles_for_lifestyle(candidates=candidates)
    assert sum(url.endswith('/menu/year') for url,kw in government[1])==1


def test_normalized_candidate_research_cache(government):
    candidates=[{'year':2022,'make':'Honda','model':'Accord Hybrid'},
                {'year':2022,'make':'honda','model':'accord hybrid'}]
    result=tools.rank_vehicles_for_lifestyle(candidates=candidates)
    assert len(result['ranked_vehicles'])==2
    assert sum('/menu/options' in url for url,kw in government[1])==1
    assert sum('/SafetyRatings/modelyear/' in url for url,kw in government[1])==1


def test_blended_phev_not_estimated(government):
    government[0][1].update(atvType='Plug-in Hybrid',combE=30,combA08=100,phevBlended='true')
    result=tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES[:2],lifestyle={**LIFESTYLE,'electric_mile_share':.6})
    row=next(r for r in result['ranked_vehicles'] if r['original_order']==0)
    assert row['ownership_cost'] is None
    assert any('PHEV' in str(x) for x in row['missing_or_ambiguous_data'])


def test_weather_arrays_and_evidence_aware_output(government):
    result=tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES[:2],lifestyle={
        'weather_conditions':['heavy_snow','ice','extreme_heat','coastal_salt','heavy_snow'],
        'road_conditions':['potholes','gravel'],'charging_access':'public_only'})
    assert result['lifestyle_inputs']['weather_conditions']==['heavy_snow','ice','extreme_heat','coastal_salt']
    for row in result['ranked_vehicles']:
        climate=row['climate_weather_fit']
        assert climate['component_score']>=0 and climate['applicable_conditions']
        assert 'ground clearance' in climate['unavailable_information']
        assert any('corrosion' in action for action in climate['user_actions'])


def test_compound_ranking_returns_all_research(government):
    result=tools.rank_vehicles_for_lifestyle(candidates=CANDIDATES[:2],lifestyle=LIFESTYLE)
    assert len(result['ranked_vehicles'])==2
    for row in result['ranked_vehicles']:
        assert row['efficiency']['vehicle_id']
        assert 'variants' in row['safety']
        assert row['recalls']['lookup_scope']=='model_level'


def test_configuration_ambiguity_can_change_affordability(monkeypatch):
    efficiency={'fuel_type':'gasoline','combined_mpg':30,'city_mpg':30,'highway_mpg':30,'sources':[],
                'configuration_resolution':{'mode':'ambiguous','confidence':'low','selected_description':'Representative gasoline',
                    'requires_clarification':True,'alternatives':[{'fuel_type':'hybrid'}],
                    'possible_result_range':{'annual_fuel_cost_usd':{'minimum':600,'maximum':3000}}}}
    monkeypatch.setattr(tools,'_get_vehicle_efficiency',lambda **kw: efficiency)
    monkeypatch.setattr(tools,'_get_vehicle_safety_profile',lambda **kw: {'missing_data':[],'sources':[]})
    monkeypatch.setattr(tools,'_check_vehicle_recalls',lambda **kw: {'recalls':[],'sources':[]})
    lifestyle={'down_payment':5000,'apr_percent':0,'loan_months':60,'maximum_monthly_ownership_cost':600}
    result=tools.rank_vehicles_for_lifestyle(candidates=[{**CANDIDATES[0],'listing_price':20000},{**CANDIDATES[1],'listing_price':20000}],lifestyle=lifestyle)
    assert all(any('across the supplied monthly-cost target' in note for note in row['sensitivity_notes']) for row in result['ranked_vehicles'])


def test_unavailable_candidate_does_not_discard_valid_candidate(monkeypatch):
    def efficiency(**identity):
        if identity['year']==2099:
            return {'error':'no_matching_data','message':'No EPA record.'}
        return {'fuel_type':'gasoline','combined_mpg':32,'sources':[],
                'configuration_resolution':{'mode':'representative','confidence':'low','selected_description':'Representative',
                                             'alternatives':[],'possible_result_range':{},'requires_clarification':False}}
    monkeypatch.setattr(tools,'_get_vehicle_efficiency',efficiency)
    monkeypatch.setattr(tools,'_get_vehicle_safety_profile',lambda **kw: {'missing_data':[],'sources':[]})
    monkeypatch.setattr(tools,'_check_vehicle_recalls',lambda **kw: {'recalls':[],'sources':[]})
    result=tools.rank_vehicles_for_lifestyle(candidates=[{'year':2099,'make':'Honda','model':'Accord'},
                                                          {'year':2022,'make':'Toyota','model':'Camry'}])
    assert len(result['ranked_vehicles'])==2
    valid=next(row for row in result['ranked_vehicles'] if row['candidate']['year']==2022)
    invalid=next(row for row in result['ranked_vehicles'] if row['candidate']['year']==2099)
    assert valid['efficiency']['combined_mpg']==32
    assert invalid['efficiency']['error']=='no_matching_data'
