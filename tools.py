"""Five bounded government-data tools. Scores and arithmetic run in Python.

Schemas are also enforced at the dispatch boundary; no arbitrary URLs or VIN
requests are accepted. All tool results are JSON strings for the class harness.
"""
import json
import math
import time
import xml.etree.ElementTree as ET
from contextvars import ContextVar
from urllib.parse import quote, urlsplit

import requests
from jsonschema import Draft202012Validator

FE = 'https://www.fueleconomy.gov/ws/rest'
NHTSA = 'https://api.nhtsa.gov'
FE_SOURCE = 'https://www.fueleconomy.gov/feg/ws/index.shtml'
SAFETY_SOURCE = 'https://www.nhtsa.gov/nhtsa-datasets-and-apis'
RECALL_NOTE = ('Model-level campaigns do not prove a specific VIN is included, '
               'that a repair remains open, or that a repair was completed.')
COMPLAINT_NOTE = ('Complaints are reports submitted to NHTSA, not necessarily verified defects. '
                  'Counts are affected by sales volume and vehicle age; count alone is not a reliability score.')
MAX_BYTES = 1_000_000
_request_context = ContextVar('government_requests', default=None)


def error(code, message='The operation could not be completed.', suggestion='Retry later or provide explicit vehicle details.'):
    return {'error': code, 'message': message, 'suggestion': suggestion}


def _xml(node, depth=0):
    if depth > 16:
        raise ValueError('depth')
    children = list(node)
    if not children:
        return node.text or ''
    result = {}
    for child in children:
        key = child.tag.split('}')[-1]
        value = _xml(child, depth + 1)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


def safe_get(url, params=None):
    """Allowlisted HTTPS, no redirects, bounded bytes, connect/read timeouts.

    One dispatch has a 45-second request budget and a per-call cache. Errors
    contain only fixed public messages, never exception text or response bodies.
    """
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc not in {'www.fueleconomy.gov', 'api.nhtsa.gov'} or parsed.query:
        return error('invalid_input', 'Only fixed government endpoints are supported.')
    context = _request_context.get()
    key = (url, tuple(sorted((params or {}).items())))
    if context and key in context['cache']:
        return context['cache'][key]
    if context and time.monotonic() >= context['deadline']:
        return error('timeout', 'The government-data request budget was reached.', 'Retry one candidate at a time.')
    try:
        with requests.get(url, params=params, headers={'Accept': 'application/json', 'User-Agent': 'DriveWise/1.0 (IEOR4570 educational comparison)'},
                          timeout=(3, 6), stream=True, allow_redirects=False) as response:
            status = response.status_code
            if status == 429:
                return error('rate_limited', 'The government service is limiting requests.', 'Wait before retrying.')
            if status in (401, 403):
                return error('upstream_authentication', 'The government service denied access.', 'Retry later; no non-GCP key is needed.')
            if status == 404:
                return error('no_matching_data', 'No matching government record was found.', 'Check year, make, model, and configuration.')
            if 400 <= status < 500:
                return error('upstream_invalid_request', 'The government service rejected the request.', 'Check the vehicle identity and retry.')
            if status != 200:
                return error('upstream_unavailable', 'The government service is unavailable.', 'Try again later.')
            chunks, size = [], 0
            for chunk in response.iter_content(16384):
                size += len(chunk)
                if size > MAX_BYTES:
                    return error('malformed_response', 'The government response exceeded the safe size limit.', 'Try a narrower vehicle query.')
                if context and time.monotonic() >= context['deadline']:
                    return error('timeout', 'The government-data request budget was reached.')
                chunks.append(chunk)
            raw = b''.join(chunks)
            try:
                data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError):
                # ElementTree does not fetch external entities; reject declarations
                # entirely, including UTF-16 encodings, before parsing.
                xml_text = raw.decode('utf-8-sig')
                if '<!DOCTYPE' in xml_text.upper() or '<!ENTITY' in xml_text.upper():
                    raise ValueError('unsafe XML')
                root = ET.fromstring(xml_text)
                data = _xml(root)
            if not isinstance(data, dict):
                raise ValueError('object required')
            if 'error' in data or 'Error' in data:
                return error('malformed_response', 'The government service returned an unexpected error object.', 'Retry later; no upstream error details are exposed.')
    except requests.Timeout:
        return error('timeout', 'The government service timed out.', 'Retry later or compare fewer candidates.')
    except requests.RequestException:
        return error('upstream_unavailable', 'The government service could not be reached.')
    except (ValueError, UnicodeError, ET.ParseError, RecursionError):
        return error('malformed_response', 'The government service returned unreadable data.')
    if context:
        context['cache'][key] = data
    return data


def _number(value, minimum=0, maximum=1_000_000, positive=False):
    try:
        n = float(value)
        if not math.isfinite(n) or not minimum <= n <= maximum or (positive and n == 0):
            return None
        return n
    except (TypeError, ValueError):
        return None


def _text(value, limit=1200):
    return str(value)[:limit] if isinstance(value, (str, int, float)) and str(value).strip() else None


def _rows(data, key='Results'):
    rows = data.get(key, data.get(key.lower()))
    if rows is None:
        return None
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        return None
    return rows


def _menu(path, params=None):
    data = safe_get(FE + '/vehicle/menu/' + path, params)
    if 'error' in data:
        return data
    rows = _rows(data, 'menuItem')
    if rows is None:
        return error('malformed_response', 'Vehicle menu data was missing or malformed.')
    return [{'text': _text(x.get('text'), 160), 'value': _text(x.get('value'), 160)} for x in rows]


def _match(rows, value):
    return next((r for r in rows if str(value).casefold() in {str(r['text']).casefold(), str(r['value']).casefold()}), None)


def _normalize_vehicle(raw, description=None):
    kind = str(raw.get('atvType', raw.get('atvtype', ''))).lower()
    fuel = str(raw.get('fuelType1', raw.get('fuelType', '')))
    fuel_type = ('plug_in_hybrid' if 'plug' in kind else 'electric' if kind == 'ev' or fuel.lower() == 'electricity'
                 else 'hybrid' if 'hybrid' in kind else 'diesel' if 'diesel' in fuel.lower()
                 else 'gasoline' if 'gasoline' in fuel.lower() else None)
    electric = fuel_type == 'electric'
    record = {
        'vehicle_id': _number(raw.get('id'), positive=True), 'year': _number(raw.get('year'), positive=True),
        'make': _text(raw.get('make')), 'model': _text(raw.get('model')),
        'configuration': description or _text(raw.get('eng_dscr')) or _text(raw.get('trany')),
        'fuel_type': fuel_type, 'government_fuel_description': _text(raw.get('fuelType')),
        'engine': {'cylinders': _number(raw.get('cylinders'), positive=True), 'displacement_liters': _number(raw.get('displ'), positive=True), 'description': _text(raw.get('eng_dscr'))},
        'transmission': _text(raw.get('trany')), 'drivetrain': _text(raw.get('drive')), 'vehicle_class': _text(raw.get('VClass')),
        'city_mpg': None if electric else _number(raw.get('city08'), positive=True),
        'highway_mpg': None if electric else _number(raw.get('highway08'), positive=True),
        'combined_mpg': None if electric else _number(raw.get('comb08'), positive=True),
        'city_mpge': _number(raw.get('city08' if electric else 'cityA08'), positive=True) if electric or fuel_type == 'plug_in_hybrid' else None,
        'highway_mpge': _number(raw.get('highway08' if electric else 'highwayA08'), positive=True) if electric or fuel_type == 'plug_in_hybrid' else None,
        'combined_mpge': _number(raw.get('comb08' if electric else 'combA08'), positive=True) if electric or fuel_type == 'plug_in_hybrid' else None,
        'kwh_per_100_miles': _number(raw.get('combE'), positive=True),
        'annual_fuel_cost_usd': _number(raw.get('fuelCost08'), positive=True),
        'ev_range_miles': _number(raw.get('range' if electric else 'rangeA'), positive=True) if electric or fuel_type == 'plug_in_hybrid' else None,
        'charge_120v_hours': _number(raw.get('charge120'), positive=True), 'charge_240v_hours': _number(raw.get('charge240'), positive=True),
        'passenger_volume_cu_ft': next((n for k in ('pv4', 'hpv', 'pv2') if (n := _number(raw.get(k), positive=True)) is not None), None),
        'luggage_volume_cu_ft': next((n for k in ('lv4', 'hlv', 'lv2') if (n := _number(raw.get(k), positive=True)) is not None), None),
        'fuel_economy_score': _number(raw.get('feScore'), maximum=10), 'emissions_score': _number(raw.get('ghgScore'), maximum=10),
        'phev_blended': str(raw.get('phevBlended', '')).lower() == 'true' if fuel_type == 'plug_in_hybrid' else None,
    }
    record['unavailable_fields'] = [k for k, v in record.items() if v is None]
    record['sources'] = [FE_SOURCE, FE + '/vehicle/' + str(int(record['vehicle_id']))] if record['vehicle_id'] else [FE_SOURCE]
    record['note'] = 'EPA test estimates. Annual fuel cost uses government assumptions, not your driving budget. GHG score is not a full lifecycle footprint.'
    return record


def get_vehicle_efficiency(year, make, model, configuration=None, vehicle_id=None):
    description = configuration
    if vehicle_id is None:
        years = _menu('year')
        if isinstance(years, dict):
            return years
        if not _match(years, year):
            return error('no_matching_data', 'That model year is not in the EPA menu.')
        makes = _menu('make', {'year': year})
        if isinstance(makes, dict):
            return makes
        match = _match(makes, make)
        if not match:
            return error('no_matching_data', 'That make is not in the EPA menu for the year.')
        make = match['value']
        models = _menu('model', {'year': year, 'make': make})
        if isinstance(models, dict):
            return models
        match = _match(models, model)
        if not match:
            return {**error('no_matching_data', 'Use an exact EPA model name, including Hybrid when applicable.'), 'model_options': models[:12], 'sources': [FE_SOURCE]}
        model = match['value']
        options = _menu('options', {'year': year, 'make': make, 'model': model})
        if isinstance(options, dict):
            return options
        if configuration:
            options = [x for x in options if configuration.casefold() in str(x['text']).casefold()]
        if not options:
            return error('no_matching_data', 'No EPA configuration matched.', 'Provide an EPA vehicle ID or exact configuration.')
        if len(options) != 1:
            return {'ambiguous': True, 'option_count': len(options), 'options': options[:8], 'truncated': len(options) > 8,
                    'suggestion': 'Choose a configuration or EPA vehicle ID; no configuration was silently selected.', 'sources': [FE_SOURCE]}
        vehicle_id, description = options[0]['value'], options[0]['text']
        if not str(vehicle_id).isdigit():
            return error('malformed_response', 'EPA configuration ID was invalid.')
    data = safe_get(FE + '/vehicle/' + str(vehicle_id))
    if 'error' in data:
        return data
    raw = data.get('vehicle', data)
    if not isinstance(raw, dict):
        return error('malformed_response', 'EPA vehicle record was malformed.')
    # Identity fields must be present; no guessing from the request.
    if _number(raw.get('year')) != year or str(raw.get('make', '')).casefold() != make.casefold() or str(raw.get('model', '')).casefold() != model.casefold():
        return error('no_matching_data', 'The EPA record did not match the requested vehicle identity.', 'Check the EPA model name and vehicle ID.')
    return _normalize_vehicle(raw, description)


COST_DEFAULTS = {'annual_miles': 10000, 'fuel_type': 'gasoline', 'fuel_price_per_gallon': 3.50,
                 'electricity_price_per_kwh': .16, 'monthly_insurance_estimate': 175, 'monthly_maintenance_reserve': 75}


def calculate_ownership_cost(vehicle_price, down_payment, apr_percent, loan_months, **inputs):
    if down_payment > vehicle_price:
        return error('invalid_input', 'Down payment cannot exceed purchase price.', 'Reduce the down payment or correct the purchase price.')
    supplied = {'vehicle_price': vehicle_price, 'down_payment': down_payment, 'apr_percent': apr_percent, 'loan_months': loan_months, **inputs}
    values = {**COST_DEFAULTS, **inputs}
    fuel_type = values['fuel_type']
    mpg = values.get('mpg')
    energy = values.get('kwh_per_100_miles')
    if energy is None and values.get('mpge'):
        energy = 3370 / values['mpge']  # EPA equivalence: 33.7 kWh/gallon.
    if fuel_type in ('gasoline', 'diesel', 'hybrid') and (not mpg or energy is not None or 'electric_mile_share' in values):
        return error('invalid_input', 'Combustion vehicles need MPG and no electric driving assumptions.', 'Provide MPG for the selected configuration.')
    if fuel_type == 'electric' and (energy is None or mpg is not None or 'electric_mile_share' in values):
        return error('invalid_input', 'EVs need MPGe or kWh/100 miles and no gasoline MPG.', 'Provide the EV energy consumption.')
    if fuel_type == 'plug_in_hybrid' and (not mpg or energy is None or 'electric_mile_share' not in values):
        return error('invalid_input', 'PHEVs need gasoline-mode MPG, electric-mode consumption, and electric-mile share.', 'Supply all three; MPGe alone is insufficient.')
    if values.get('mpge') and values.get('kwh_per_100_miles'):
        return error('invalid_input', 'Supply one electric consumption measure, not both.')
    principal = vehicle_price - down_payment
    rate = apr_percent / 1200
    payment = principal / loan_months if rate == 0 else principal * rate / (1 - (1 + rate) ** -loan_months)
    miles = values['annual_miles'] / 12
    electric_share = 1 if fuel_type == 'electric' else values.get('electric_mile_share', 0)
    gas_cost = miles * (1 - electric_share) / mpg * values['fuel_price_per_gallon'] if mpg else 0
    electricity_cost = miles * electric_share * energy / 100 * values['electricity_price_per_kwh'] if energy else 0
    insurance, maintenance = values['monthly_insurance_estimate'], values['monthly_maintenance_reserve']
    return {'amount_financed': round(principal, 2), 'estimated_loan_payment_monthly': round(payment, 2),
            'estimated_total_interest_over_loan': round(payment * loan_months - principal, 2),
            'estimated_fuel_monthly': round(gas_cost, 2), 'estimated_electricity_monthly': round(electricity_cost, 2),
            'insurance_allowance_monthly': insurance, 'maintenance_reserve_monthly': maintenance,
            'estimated_total_monthly': round(payment + gas_cost + electricity_cost + insurance + maintenance, 2),
            'user_provided_inputs': {k: value for k, value in supplied.items() if not (values.get('energy_data_source') == 'government_epa' and k in ('mpg', 'mpge', 'kwh_per_100_miles'))},
            'government_source_data': {k: value for k, value in supplied.items() if values.get('energy_data_source') == 'government_epa' and k in ('mpg', 'mpge', 'kwh_per_100_miles')},
            'government_sources': [FE_SOURCE] if values.get('energy_data_source') == 'government_epa' else [],
            'defaults_and_assumptions': {k: v for k, v in COST_DEFAULTS.items() if k not in inputs},
            'method': 'Amortized loan; PHEV split-mile approximation assumes electric miles use no gasoline. Blended operation needs a separate estimate.',
            'excluded': ['taxes', 'registration', 'dealer fees', 'depreciation', 'repair predictions'],
            'note': 'Estimates, not lender, insurance, repair, or purchase guarantees.'}


def check_vehicle_recalls(year, make, model):
    data = safe_get(NHTSA + '/recalls/recallsByVehicle', {'make': make, 'model': model, 'modelYear': year})
    if 'error' in data:
        return data
    rows = _rows(data)
    lookup_model = model
    if rows == [] and model.lower().endswith(' hybrid'):
        lookup_model = model[:-7].strip()
        fallback = safe_get(NHTSA + '/recalls/recallsByVehicle', {'make': make, 'model': lookup_model, 'modelYear': year})
        if 'error' in fallback:
            return fallback
        rows = _rows(fallback)
    if rows is None:
        return error('malformed_response', 'NHTSA recall results were missing or malformed.')
    fields = {'campaign_number': 'NHTSACampaignNumber', 'component': 'Component', 'summary': 'Summary',
              'consequence': 'Consequence', 'remedy': 'Remedy', 'recall_date': 'ReportReceivedDate'}
    return {'vehicle': {'year': year, 'make': make, 'model': model}, 'recall_count': len(rows),
            'recalls': [{k: _text(r.get(v)) for k, v in fields.items()} for r in rows[:8]],
            'truncated': len(rows) > 8, 'lookup_scope': 'model_level',
            'lookup_vehicle': {'year': year, 'make': make, 'model': lookup_model},
            'scope_note': 'Base-model fallback; confirm applicability to the hybrid configuration.' if lookup_model != model else 'Requested model name.', 'note': RECALL_NOTE, 'sources': [SAFETY_SOURCE]}


def get_vehicle_safety_profile(year, make, model):
    path = f'/SafetyRatings/modelyear/{year}/make/{quote(make, safe="")}/model/{quote(model, safe="")}'
    data = safe_get(NHTSA + path, {'format': 'json'})
    variants, missing = [], []
    rows = _rows(data) if 'error' not in data else None
    lookup_model = model
    if rows == [] and model.lower().endswith(' hybrid'):
        lookup_model = model[:-7].strip()
        data = safe_get(NHTSA + f'/SafetyRatings/modelyear/{year}/make/{quote(make, safe="")}/model/{quote(lookup_model, safe="")}', {'format': 'json'})
        rows = _rows(data) if 'error' not in data else None
    if rows is None:
        missing.append({'field': 'ratings', 'error': data.get('error', 'malformed_response')})
    for item in (rows or [])[:4]:
        vid = _number(item.get('VehicleId'), positive=True)
        if vid is None or vid != int(vid):
            missing.append({'field': 'variant', 'error': 'malformed_response'})
            continue
        detail = safe_get(NHTSA + '/SafetyRatings/VehicleId/' + str(int(vid)), {'format': 'json'})
        records = _rows(detail) if 'error' not in detail else None
        ratings = records[0] if records else {}
        if not records:
            missing.append({'field': 'variant_ratings', 'error': detail.get('error', 'no_matching_data')})
        variants.append({'vehicle_id': int(vid), 'description': _text(item.get('VehicleDescription')),
                         **{k: _number(ratings.get(v), minimum=1, maximum=5) for k, v in
                            {'overall_rating': 'OverallRating', 'frontal_rating': 'OverallFrontCrashRating',
                             'side_rating': 'OverallSideCrashRating', 'rollover_rating': 'RolloverRating'}.items()}})
    # Common ratings only when every returned variant has the same available score.
    common = {}
    for key in ('overall_rating', 'frontal_rating', 'side_rating', 'rollover_rating'):
        values = [v[key] for v in variants]
        common[key] = values[0] if values and None not in values and len(set(values)) == 1 and len(rows or []) <= 4 else None
        if common[key] is None:
            missing.append({'field': key, 'error': 'missing_or_ambiguous'})
    recalls = check_vehicle_recalls(year=year, make=make, model=lookup_model)
    complaints = safe_get(NHTSA + '/complaints/complaintsByVehicle', {'make': make, 'model': lookup_model, 'modelYear': year})
    complaint_rows = _rows(complaints) if 'error' not in complaints else None
    if 'error' in recalls:
        missing.append({'field': 'recalls', 'error': recalls['error']})
    if complaint_rows is None:
        missing.append({'field': 'complaints', 'error': complaints.get('error', 'malformed_response')})
    return {'vehicle': {'year': year, 'make': make, 'model': model}, **common,
            'lookup_vehicle': {'year': year, 'make': make, 'model': lookup_model},
            'rating_scope_matches_candidate': lookup_model == model,
            'scope_note': 'Base-model fallback; exact hybrid rating applicability is unconfirmed.' if lookup_model != model else 'Requested model name; verify the matching variant.',
            'variants': variants, 'variant_count': len(rows or []), 'ambiguous': len(rows or []) > 1,
            'variants_truncated': len(rows or []) > 4, 'recall_count': recalls.get('recall_count'),
            'complaint_count_bounded': min(len(complaint_rows), 100) if complaint_rows is not None else None,
            'complaint_count_capped': len(complaint_rows) > 100 if complaint_rows is not None else None,
            'complaint_components_sample': list(dict.fromkeys(filter(None, (_text(r.get('components', r.get('Components')), 180) for r in (complaint_rows or [])[:20]))))[:5],
            'complaint_limitations': COMPLAINT_NOTE, 'recall_limitations': RECALL_NOTE,
            'missing_data': missing, 'sources': [SAFETY_SOURCE] if rows is not None or 'error' not in recalls or complaint_rows is not None else [],
            'note': 'Common ratings are reported only when all variants agree. No recall-count safety penalty is applied.'}


BASE_WEIGHTS = {'affordability': 30, 'energy': 20, 'space': 15, 'weather': 10, 'safety': 15, 'environment': 10}


def _clip(value):
    return max(0, min(1, value))


def rank_vehicles_for_lifestyle(candidates, lifestyle=None):
    lifestyle = lifestyle or {}
    weights = dict(BASE_WEIGHTS)
    for field, component in [('fuel_efficiency_priority', 'energy'), ('safety_priority', 'safety'), ('environmental_priority', 'environment')]:
        if field in lifestyle:
            weights[component] *= {'low': .5, 'medium': 1, 'high': 1.5}[lifestyle[field]]
    total_weight = sum(weights.values())
    weights = {k: v * 100 / total_weight for k, v in weights.items()}
    ranked = []
    for order, candidate in enumerate(candidates):
        identity = {k: candidate[k] for k in ('year', 'make', 'model')}
        efficiency = get_vehicle_efficiency(**identity, **{k: candidate[k] for k in ('configuration', 'vehicle_id') if k in candidate})
        safety = get_vehicle_safety_profile(**identity)
        scores = {k: .5 for k in weights}
        known, reasons, tradeoffs, failures, missing = set(), [], [], [], []
        if 'error' in efficiency or efficiency.get('ambiguous'):
            missing.append({'field': 'efficiency', 'detail': efficiency})
            efficiency_data = {}
        else:
            efficiency_data = efficiency
        v = efficiency_data
        fuel_type = v.get('fuel_type')
        mpg = v.get('combined_mpg')
        if v.get('city_mpg') and v.get('highway_mpg') and 'city_driving_share' in lifestyle:
            share = lifestyle['city_driving_share']
            mpg = 1 / (share / v['city_mpg'] + (1 - share) / v['highway_mpg'])
        cost = None
        price = candidate.get('listing_price')
        if price is not None:
            ratios = []
            if lifestyle.get('maximum_purchase_price'):
                cap = lifestyle['maximum_purchase_price']
                ratios.append(_clip(1 - price / cap / 2))
                if price > cap:
                    failures.append('Listing price exceeds firm purchase budget.')
            finance_fields = ('down_payment', 'apr_percent', 'loan_months')
            if all(k in lifestyle for k in finance_fields) and fuel_type:
                args = {'vehicle_price': price, **{k: lifestyle[k] for k in finance_fields}, 'fuel_type': fuel_type, 'energy_data_source': 'government_epa',
                        **{k: lifestyle[k] for k in COST_DEFAULTS if k in lifestyle and k != 'fuel_type'}}
                if mpg:
                    args['mpg'] = mpg
                if fuel_type in ('electric', 'plug_in_hybrid') and v.get('kwh_per_100_miles'):
                    args['kwh_per_100_miles'] = v['kwh_per_100_miles']
                if fuel_type == 'plug_in_hybrid':
                    if 'electric_mile_share' in lifestyle:
                        args['electric_mile_share'] = lifestyle['electric_mile_share']
                    if v.get('phev_blended'):
                        missing.append({'field': 'ownership_cost', 'detail': 'Blended PHEV operation is outside split-mile cost model.'})
                    else:
                        cost = calculate_ownership_cost(**args)
                else:
                    cost = calculate_ownership_cost(**args)
                if cost and 'error' not in cost:
                    government_keys = [k for k in ('mpg', 'kwh_per_100_miles') if k in args]
                    cost['government_source_data'] = {k: args[k] for k in government_keys}
                    cost['government_sources'] = v.get('sources', [])
                    for k in government_keys:
                        cost['user_provided_inputs'].pop(k, None)
                    if lifestyle.get('maximum_monthly_ownership_cost'):
                        cap = lifestyle['maximum_monthly_ownership_cost']
                        ratios.append(_clip(1 - cost['estimated_total_monthly'] / cap / 2))
                        if cost['estimated_total_monthly'] > cap:
                            tradeoffs.append('Estimated monthly cost exceeds desired monthly target.')
                elif cost:
                    missing.append({'field': 'ownership_cost', 'detail': cost})
                    cost = None
            if ratios:
                scores['affordability'] = sum(ratios) / len(ratios)
                known.add('affordability')
        if cost is None:
            missing.append({'field': 'ownership_cost', 'detail': 'Need listing price, financing terms, and unambiguous energy data.'})
        energy_measure = v.get('combined_mpge') if fuel_type == 'electric' else mpg
        if energy_measure:
            scores['energy'] = _clip((energy_measure - 15) / (125 if fuel_type == 'electric' else 45))
            known.add('energy')
        preferences = lifestyle.get('preferred_fuel_types')
        if fuel_type and preferences:
            scores['energy'] = (scores['energy'] + (1 if fuel_type in preferences else 0)) / 2
            known.add('energy')
        if fuel_type in ('electric', 'plug_in_hybrid') and lifestyle.get('willing_to_charge_ev') is False:
            scores['energy'] = 0 if fuel_type == 'electric' else min(scores['energy'], .25)
            known.add('energy')
            if fuel_type == 'electric':
                failures.append('Electric vehicle conflicts with unwillingness to charge.')
            tradeoffs.append('Charging access conflicts with the electric powertrain.')
        space_parts = []
        vehicle_class = str(v.get('vehicle_class') or '').lower()
        if 'regular_passenger_count' in lifestyle and v.get('passenger_volume_cu_ft'):
            # Volume is a rough comfort proxy, never a seat-count claim.
            space_parts.append(_clip(v['passenger_volume_cu_ft'] / (max(1, lifestyle['regular_passenger_count']) * 25)))
            tradeoffs.append('Passenger volume is a comfort proxy; verify seat count and child-seat fit.')
        if lifestyle.get('cargo_need') and v.get('luggage_volume_cu_ft'):
            target = {'low': 10, 'medium': 18, 'high': 30}[lifestyle['cargo_need']]
            space_parts.append(_clip(v['luggage_volume_cu_ft'] / target))
        if lifestyle.get('parking_constraint') in ('moderate', 'tight') and vehicle_class:
            compact = 'compact' in vehicle_class or 'two-seater' in vehicle_class
            large = any(x in vehicle_class for x in ('large', 'standard', 'van'))
            space_parts.append(1 if compact else .25 if large else .5)
            tradeoffs.append('EPA class is a parking proxy; dimensions and turning radius are unavailable.')
        if space_parts:
            scores['space'] = sum(space_parts) / len(space_parts)
            known.add('space')
        if lifestyle.get('winter_weather_need') is True and v.get('drivetrain'):
            drive = v['drivetrain'].lower()
            scores['weather'] = 1 if 'all-wheel' in drive or '4-wheel' in drive else .6 if 'front' in drive else .4
            known.add('weather')
            tradeoffs.append('Drivetrain does not replace winter tires or guarantee snow safety.')
        if safety.get('overall_rating') and safety.get('rating_scope_matches_candidate', True):
            scores['safety'] = (safety['overall_rating'] - 1) / 4
            known.add('safety')
        if v.get('emissions_score') is not None:
            scores['environment'] = v['emissions_score'] / 10
            known.add('environment')
        for component in scores:
            if component not in known:
                missing.append({'field': component, 'detail': 'Missing data or unspecified preference; neutral midpoint applied.'})
        ordered = sorted(known, key=lambda k: (-scores[k], list(weights).index(k)))
        evidence = {
            'affordability': f'Listing price ${price}; available purchase/monthly targets compared against supplied budgets.',
            'energy': f'{fuel_type or "Unknown powertrain"}; consumption measure {energy_measure} ' + ('MPGe' if fuel_type == 'electric' else 'MPG') + '; charging and fuel preferences applied when supplied.',
            'space': f'EPA class {v.get("vehicle_class")}; passenger volume {v.get("passenger_volume_cu_ft")} cu ft; luggage {v.get("luggage_volume_cu_ft")} cu ft. Rough proxies only.',
            'weather': f'Government drivetrain: {v.get("drivetrain")}; winter tires and conditions still matter.',
            'safety': f'Applicable common NHTSA overall rating: {safety.get("overall_rating")} of 5 stars.',
            'environment': f'EPA GHG score: {v.get("emissions_score")} of 10; not full lifecycle emissions.',
        }
        reasons += [f'{k}: {scores[k] * 100:.0f}% component fit. {evidence[k]}' for k in ordered[:3]]
        tradeoffs += [f'{k}: {scores[k] * 100:.0f}% component fit.' for k in ordered if scores[k] < .5]
        if safety.get('rating_scope_matches_candidate') is False:
            tradeoffs.append('Base-model safety data may not apply to the exact hybrid; safety score remains neutral.')
        if candidate.get('mileage') is not None:
            tradeoffs.append('Odometer mileage is recorded but is not used to predict repairs or reliability.')
        if candidate.get('vin'):
            tradeoffs.append('User-supplied VIN is retained as context only; no VIN service request is made.')
        if 'error' in safety:
            missing.append({'field': 'safety', 'detail': safety})
        missing.extend(safety.get('missing_data', []))
        component_points = {k: round(scores[k] * weights[k], 3) for k in weights}
        ranked.append({'candidate': candidate, 'original_order': order, 'total_fit_score': round(sum(scores[k] * weights[k] for k in scores), 3),
                       'component_scores': component_points, 'weights': weights, 'strongest_fit_reasons': reasons or ['Insufficient evidence; neutral scores.'],
                       'tradeoffs': tradeoffs, 'hard_constraint_failures': failures, 'missing_or_ambiguous_data': missing,
                       'data_completeness': round(len(known) / len(weights), 3), 'ownership_cost': cost,
                       'efficiency': efficiency, 'safety': safety,
                       'government_sources_used': list(dict.fromkeys(efficiency.get('sources', []) + safety.get('sources', [])))})
    ranked.sort(key=lambda r: (-r['total_fit_score'], -r['data_completeness'], r['original_order']))
    return {'ranked_vehicles': ranked, 'weights': weights, 'lifestyle_inputs': lifestyle,
            'method': '100-point deterministic fit. Missing evidence or unspecified preference receives half of component weight. Tie: score, completeness, original order. Hard constraints reported separately.',
            'ownership_duration_years': lifestyle.get('expected_ownership_years'),
            'duration_note': 'Duration is context; no depreciation, resale, or future repair forecast is modeled.',
            'note': 'Highest fit under supplied preferences is not objectively best, mechanically sound, reliable, fairly priced, or guaranteed affordable.'}


# Small schema builders keep argument units explicit without repeating boilerplate.
def num(description, minimum=0, maximum=1_000_000, integer=False):
    return {'type': 'integer' if integer else 'number', 'description': description, 'minimum': minimum, 'maximum': maximum}


def text(description, pattern=r'^[^\x00-\x1f]*\S[^\x00-\x1f]*$', max_length=100):
    return {'type': 'string', 'description': description, 'minLength': 1, 'maxLength': max_length, 'pattern': pattern}


def choice(description, values):
    return {'type': 'string', 'description': description, 'enum': values}


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


IDENTITY = {'year': num('Four-digit U.S. model year, e.g. 2022.', 1981, 2030, True),
            'make': text('Manufacturer name as listed in government menus, e.g. Honda.'),
            'model': text('Exact government model name, e.g. Accord Hybrid for EPA; NHTSA may use Accord.')}
EFFICIENCY = obj({**IDENTITY, 'configuration': text('Optional EPA configuration description substring; trim names may not match EPA configurations.', max_length=160),
                 'vehicle_id': num('Optional FuelEconomy.gov vehicle record ID; must match supplied year/make/model.', 1, 999999, True)}, IDENTITY)
FUEL_TYPES = ['gasoline', 'diesel', 'hybrid', 'plug_in_hybrid', 'electric']
COST_PROPERTIES = {
    'energy_data_source': choice('Provenance of MPG/MPGe/kWh data: user estimate or an EPA record previously retrieved in this session; never label guessed values as government data.', ['user_estimate', 'government_epa']),
    'vehicle_price': num('Purchase price in USD before taxes or fees.', .01),
    'down_payment': num('Cash down in USD, from zero up to the purchase price.'),
    'apr_percent': num('APR percentage, e.g. 6.5 means 6.5 percent; zero allowed.', 0, 40),
    'loan_months': num('Loan term in whole months.', 12, 96, True),
    'annual_miles': num('Miles driven per year; default 10000.', 0, 200000, True),
    'fuel_type': choice('Powertrain; default gasoline. Specify diesel/hybrid/PHEV/EV explicitly.', FUEL_TYPES),
    'mpg': num('Miles/gallon; required for combustion or PHEV gasoline mode. No guessed MPG default.', .1, 200),
    'mpge': num('EPA miles/gallon equivalent for electric mode, converts using 33.7 kWh/gallon. Supply this OR kWh/100 miles.', .1, 500),
    'kwh_per_100_miles': num('Electric-mode energy consumption in kWh per 100 miles; supply this OR MPGe.', .1, 300),
    'electric_mile_share': num('PHEV-only fraction of miles on electricity, 0 to 1; required for PHEVs.', 0, 1),
    'fuel_price_per_gallon': num('USD per gallon of gasoline or diesel; assumed 3.50 when omitted.', 0, 20),
    'electricity_price_per_kwh': num('USD per kWh; assumed 0.16 when omitted.', 0, 5),
    'monthly_insurance_estimate': num('USD/month allowance, not a quote; assumed 175 when omitted.', 0, 10000),
    'monthly_maintenance_reserve': num('USD/month reserve, not a repair forecast; assumed 75 when omitted.', 0, 10000),
}
COST_SCHEMA = obj(COST_PROPERTIES, ('vehicle_price', 'down_payment', 'apr_percent', 'loan_months'))
CANDIDATE = obj({**EFFICIENCY['properties'], 'vin': text('Optional 17-character VIN explicitly provided by the user; context only, never decoded or looked up.', r'^[A-HJ-NPR-Z0-9]{17}$', 17),
                 'listing_price': num('Optional asking price from a user-found listing in USD; not market value.', .01),
                 'mileage': num('Optional odometer miles, recorded without reliability scoring.', 0, 2_000_000, True)}, IDENTITY)
LIFESTYLE = obj({
    **{k: v for k, v in COST_PROPERTIES.items() if k not in ('vehicle_price', 'fuel_type', 'mpg', 'mpge', 'kwh_per_100_miles', 'energy_data_source')},
    'maximum_purchase_price': num('Firm maximum purchase price in USD; violations reported separately.', .01),
    'maximum_monthly_ownership_cost': num('Desired monthly ownership USD target; a soft target, not guarantee.', .01, 100000),
    'city_driving_share': num('Fraction of driving in city, 0 to 1; remaining fraction is highway.', 0, 1),
    'regular_passenger_count': num('Regular occupants including driver; volume proxy cannot establish seat count.', 1, 12, True),
    'cargo_need': choice('Cargo need category; maps to 10/18/30 cubic foot proxies.', ['low', 'medium', 'high']),
    'parking_constraint': choice('Parking space constraint; EPA class is a rough size proxy.', ['none', 'moderate', 'tight']),
    'winter_weather_need': {'type': 'boolean', 'description': 'True if winter traction matters; drivetrain proxy only.'},
    **{k: choice('Relative scoring priority; medium leaves base weight, low halves and high multiplies by 1.5.', ['low', 'medium', 'high']) for k in ('fuel_efficiency_priority', 'safety_priority', 'environmental_priority')},
    'preferred_fuel_types': {'type': 'array', 'description': 'Optional preferred powertrain categories.', 'items': choice('Powertrain category.', FUEL_TYPES), 'minItems': 1, 'maxItems': 5, 'uniqueItems': True},
    'willing_to_charge_ev': {'type': 'boolean', 'description': 'Whether user is willing to charge; false is a hard conflict for pure EVs.'},
    'expected_ownership_years': num('Expected ownership duration in years, context only; no depreciation forecast.', .5, 30),
})
SCHEMAS = {'get_vehicle_efficiency': EFFICIENCY, 'calculate_ownership_cost': COST_SCHEMA,
           'check_vehicle_recalls': obj(IDENTITY, IDENTITY), 'get_vehicle_safety_profile': obj(IDENTITY, IDENTITY),
           'rank_vehicles_for_lifestyle': obj({'candidates': {'type': 'array', 'description': 'Two to five vehicles supplied by user; no inventory search.', 'items': CANDIDATE, 'minItems': 2, 'maxItems': 5},
                                             'lifestyle': {**LIFESTYLE, 'description': 'Optional preferences; omitted preferences are neutral, never invented.'}}, ('candidates',))}
DESCRIPTIONS = {
    'get_vehicle_efficiency': 'Use for government efficiency/specifications for a known year/make/model; completes EPA menu flow internally. Do not use for live inventory or market value. Ambiguous configurations require clarification.',
    'calculate_ownership_cost': 'Use for financing and energy budgets with purchase price, financing terms, and adequate energy assumptions. Do not use to predict repairs or local market value. Estimates exclude taxes, fees and depreciation.',
    'check_vehicle_recalls': 'Use for model-level NHTSA recall campaigns for a known vehicle. Do not use to prove a VIN is affected or a repair is open/completed.',
    'get_vehicle_safety_profile': 'Use for NHTSA ratings, recall count, and bounded complaint context. Completes variant and ID requests internally. Do not use complaint or recall counts as reliability scores.',
    'rank_vehicles_for_lifestyle': 'Use to compare two to five user-supplied candidates with deterministic lifestyle scoring. Internally fetches EPA and NHTSA data and calculates cost when possible. Do not use to search inventory or certify a winner; omit unknown preferences.',
}
TOOLS = [{'type': 'function', 'function': {'name': name, 'description': DESCRIPTIONS[name], 'parameters': schema}} for name, schema in SCHEMAS.items()]
# Implementations return dictionaries; the public entrypoints below enforce schemas
# for both model dispatch and direct Python calls.
IMPLEMENTATIONS = {name: globals()[name] for name in SCHEMAS}


def run_tool(name, args):
    if not isinstance(name, str) or name not in IMPLEMENTATIONS:
        return json.dumps(error('unknown_tool', 'The requested tool is not available.', 'Use one of the five declared DriveWise tools.'))
    token = None
    if _request_context.get() is None:
        token = _request_context.set({'deadline': time.monotonic() + 45, 'cache': {}})
    try:
        try:
            valid = isinstance(args, dict) and len(json.dumps(args, allow_nan=False)) <= 24000 and Draft202012Validator(SCHEMAS[name]).is_valid(args)
        except (TypeError, ValueError, RecursionError):
            valid = False
        if not valid:
            return json.dumps(error('invalid_input', 'Arguments do not match the declared tool schema.', 'Check required fields, units, ranges, and candidate limits.'))
        try:
            return json.dumps(IMPLEMENTATIONS[name](**args), allow_nan=False)
        except Exception:
            return json.dumps(error('tool_failed', 'The tool failed safely.', 'Retry or provide a narrower request; no internal details are exposed.'))
    finally:
        if token is not None:
            _request_context.reset(token)


def _entrypoint(name):
    def wrapped(**args):
        return json.loads(run_tool(name, args))
    wrapped.__name__ = name
    return wrapped


for _name in SCHEMAS:
    globals()[_name] = _entrypoint(_name)
TOOL_MAP = {name: globals()[name] for name in SCHEMAS}
