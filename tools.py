"""Five bounded government-data tools. Scores and arithmetic run in Python.

Schemas are also enforced at the dispatch boundary; no arbitrary URLs or VIN
requests are accepted. All tool results are JSON strings for the class harness.
"""
import json
import math
import re
import statistics
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
        with requests.get(url, params=params, headers={'Accept': 'application/json', 'User-Agent': 'DriveWise/1.0'},
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


def _terms(value):
    """Normalize common EPA configuration language into stable matching tokens."""
    text_value = re.sub(r'[^a-z0-9]+', ' ', str(value or '').casefold()).strip()
    replacements = {
        r'\ball wheel drive\b|\ballwheel drive\b|\bawd\b': ' awd ',
        r'\bfour wheel drive\b|\b4 wheel drive\b|\b4wd\b': ' 4wd ',
        r'\bfront wheel drive\b|\bfwd\b': ' fwd ',
        r'\brear wheel drive\b|\brwd\b': ' rwd ',
        r'\bcontinuously variable transmission\b|\bcvt\b|\be cvt\b': ' cvt automatic ',
        r'\bautomatic transmission\b|\bauto\b': ' automatic ',
        r'\bmanual transmission\b': ' manual ',
        r'\bplug in hybrid\b|\bphev\b': ' plug_in_hybrid hybrid ',
        r'\belectric vehicle\b|\bev\b': ' electric ',
    }
    for pattern, replacement in replacements.items():
        text_value = re.sub(pattern, replacement, text_value)
    return tuple(dict.fromkeys(text_value.split()))


def _hint_match(hint, value):
    wanted, available = set(_terms(hint)), set(_terms(value))
    if not wanted or not available:
        return 0
    if wanted <= available:
        return 1
    return len(wanted & available) / len(wanted)


def _model_compatible(requested, actual):
    requested_terms, actual_terms = _terms(requested), _terms(actual)
    return bool(requested_terms) and actual_terms[:len(requested_terms)] == requested_terms


def _powertrain_label(raw):
    return _normalize_vehicle(raw).get('fuel_type')


def _range(records):
    fields = ('combined_mpg', 'combined_mpge', 'annual_fuel_cost_usd', 'ev_range_miles')
    normalized = [_normalize_vehicle(raw) for _, raw in records]
    result = {}
    for field in fields:
        values = [row[field] for row in normalized if row.get(field) is not None]
        if values:
            result[field] = {'minimum': min(values), 'maximum': max(values)}
    return result


def _configuration_records(year, make, model):
    years = _menu('year')
    if isinstance(years, dict):
        return years
    if not _match(years, year):
        return error('no_matching_data', 'That model year is not in the EPA menu.')
    makes = _menu('make', {'year': year})
    if isinstance(makes, dict):
        return makes
    make_match = _match(makes, make)
    if not make_match:
        return error('no_matching_data', 'That make is not in the EPA menu for the year.')
    canonical_make = make_match['value']
    models = _menu('model', {'year': year, 'make': canonical_make})
    if isinstance(models, dict):
        return models
    model_matches = [row for row in models if _model_compatible(model, row['value'])]
    if not model_matches:
        return {**error('no_matching_data', 'Use an exact EPA model name, including Hybrid when applicable.'),
                'model_options': models[:12], 'sources': [FE_SOURCE]}
    records = []
    for model_match in model_matches:
        canonical_model = model_match['value']
        options = _menu('options', {'year': year, 'make': canonical_make, 'model': canonical_model})
        if isinstance(options, dict):
            return options
        for option in options:
            if not str(option.get('value', '')).isdigit():
                return error('malformed_response', 'EPA configuration data contained an invalid vehicle ID.')
            data = safe_get(FE + '/vehicle/' + str(option['value']))
            if 'error' in data:
                continue
            raw = data.get('vehicle', data)
            if not isinstance(raw, dict):
                continue
            if (_number(raw.get('year')) == year and str(raw.get('make', '')).casefold() == str(canonical_make).casefold()
                    and str(raw.get('model', '')).casefold() == str(canonical_model).casefold()):
                described = dict(option)
                described['text'] = f'{canonical_model} · {option.get("text")}'
                records.append((described, raw))
    if not records:
        return error('malformed_response', 'EPA configuration records were missing or malformed.')
    return {'make': canonical_make, 'model': model, 'records': records}


def _representative(records):
    normalized = [(option, raw, _normalize_vehicle(raw, option.get('text'))) for option, raw in records]
    efficiency_values = [row.get('combined_mpge') or row.get('combined_mpg') for _, _, row in normalized]
    cost_values = [row.get('annual_fuel_cost_usd') for _, _, row in normalized]
    eff_median = statistics.median(v for v in efficiency_values if v is not None) if any(v is not None for v in efficiency_values) else 0
    cost_median = statistics.median(v for v in cost_values if v is not None) if any(v is not None for v in cost_values) else 0
    special = re.compile(r'\b(performance|competition|track|launch|limited edition|special edition|police|taxi)\b', re.I)
    ordinary = [row for row in normalized if not special.search(str(row[0].get('text', '')))] or normalized
    def key(item):
        option, _, row = item
        efficiency = row.get('combined_mpge') or row.get('combined_mpg')
        cost = row.get('annual_fuel_cost_usd')
        distance = (abs(efficiency - eff_median) / max(1, eff_median) if efficiency is not None else 1)
        distance += (abs(cost - cost_median) / max(1, cost_median) if cost is not None else 1)
        return (distance, int(option['value']))
    return min(ordinary, key=key)


def _resolve_configuration(year, make, model, configuration_id=None, vehicle_id=None, configuration=None,
                           trim=None, fuel_type=None, drivetrain=None, transmission=None, engine=None, body_style=None):
    requested_id = configuration_id if configuration_id is not None else vehicle_id
    if requested_id is not None:
        data = safe_get(FE + '/vehicle/' + str(requested_id))
        if 'error' in data:
            return data
        raw = data.get('vehicle', data)
        if not isinstance(raw, dict):
            return error('malformed_response', 'EPA vehicle record was malformed.')
        if (_number(raw.get('year')) != year or str(raw.get('make', '')).casefold() != make.casefold()
                or not _model_compatible(model, raw.get('model', ''))):
            return error('no_matching_data', 'The EPA record did not match the requested vehicle identity.', 'Check the EPA model name and configuration ID.')
        return raw, {'mode': 'exact', 'selected_id': str(requested_id),
                     'selected_description': _text(raw.get('eng_dscr')) or _text(raw.get('trany')),
                     'confidence': 'high', 'matched_hints': ['configuration_id'], 'alternatives': [],
                     'possible_result_range': {}, 'requires_clarification': False}

    menu = _configuration_records(year, make, model)
    if 'error' in menu:
        return menu
    records = menu['records']
    hints = {'trim': trim or configuration, 'fuel_type': fuel_type, 'drivetrain': drivetrain,
             'transmission': transmission, 'engine': engine, 'body_style': body_style}
    weights = {'trim': 40, 'fuel_type': 25, 'drivetrain': 15, 'engine': 10, 'transmission': 7, 'body_style': 3}
    active = {key: value for key, value in hints.items() if value}
    scored = []
    for option, raw in records:
        candidate_values = {
            'trim': ' '.join(filter(None, (_text(option.get('text')), _text(raw.get('eng_dscr'))))),
            'fuel_type': ' '.join(filter(None, (_powertrain_label(raw), _text(raw.get('fuelType')), _text(raw.get('atvType'))))),
            'drivetrain': raw.get('drive'), 'transmission': raw.get('trany'),
            'engine': ' '.join(filter(None, (_text(raw.get('eng_dscr')), _text(raw.get('displ')), _text(raw.get('cylinders'))))),
            'body_style': raw.get('VClass'),
        }
        matches = {key: _hint_match(value, candidate_values.get(key)) for key, value in active.items()}
        denominator = sum(weights[key] for key in active) or 1
        score = sum(weights[key] * matches[key] for key in active) / denominator
        scored.append((score, option, raw, [key for key, value in matches.items() if value >= .75]))
    scored.sort(key=lambda item: (-item[0], int(item[1]['value'])))
    powertrains = {label for _, raw in records if (label := _powertrain_label(raw))}
    possible_range = _range(records)
    if active:
        best = scored[0]
        margin = best[0] - (scored[1][0] if len(scored) > 1 else 0)
        adequate = best[0] >= .6 and (margin >= .08 or best[0] >= .95)
        if adequate:
            selected = (best[1], best[2], _normalize_vehicle(best[2], best[1].get('text')))
            mode, confidence = 'matched', 'high' if best[0] >= .9 and margin >= .15 else 'medium'
        else:
            selected = _representative(records)
            mode, confidence = 'ambiguous', 'low'
    else:
        selected = _representative(records)
        mode, confidence = ('ambiguous', 'low') if len(powertrains) > 1 else ('representative', 'low')
        best = next(item for item in scored if item[1]['value'] == selected[0]['value'])
    option, raw, _ = selected
    alternatives = [{'id': str(item[1]['value']), 'description': item[1].get('text'),
                     'fuel_type': _powertrain_label(item[2]), 'match_score': round(item[0], 3)}
                    for item in scored if item[1]['value'] != option['value']][:4]
    return raw, {'mode': mode, 'selected_id': str(option['value']), 'selected_description': option.get('text'),
                 'confidence': confidence, 'matched_hints': best[3], 'alternatives': alternatives,
                 'possible_result_range': possible_range,
                 'requires_clarification': len(powertrains) > 1,
                 'note': ('Multiple powertrain types are present; selection is provisional and the powertrain difference may change the result.'
                          if len(powertrains) > 1 else 'The exact trim can change efficiency and cost estimates.')}


def _get_vehicle_efficiency_uncached(year, make, model, **hints):
    resolved = _resolve_configuration(year, make, model, **hints)
    if isinstance(resolved, dict):
        return resolved
    raw, resolution = resolved
    result = _normalize_vehicle(raw, resolution['selected_description'])
    result['configuration_resolution'] = resolution
    return result


def _get_vehicle_efficiency(year, make, model, **hints):
    context = _request_context.get()
    key = ('efficiency', int(year), str(make).casefold().strip(), str(model).casefold().strip(),
           tuple(sorted((name, str(value).casefold().strip()) for name, value in hints.items())))
    research = context.setdefault('research_cache', {}) if context is not None else {}
    if key not in research:
        research[key] = _get_vehicle_efficiency_uncached(year, make, model, **hints)
    return research[key]


def get_vehicle_efficiency(year, make, model, **hints):
    return _get_vehicle_efficiency(year, make, model, **hints)


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


_calculate_ownership_cost = calculate_ownership_cost


def _check_vehicle_recalls_uncached(year, make, model):
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


def _check_vehicle_recalls(year, make, model):
    context = _request_context.get()
    key = ('recalls', int(year), str(make).casefold().strip(), str(model).casefold().strip())
    research = context.setdefault('research_cache', {}) if context is not None else {}
    if key not in research:
        research[key] = _check_vehicle_recalls_uncached(year, make, model)
    return research[key]


def check_vehicle_recalls(year, make, model):
    return _check_vehicle_recalls(year, make, model)


def _get_vehicle_safety_profile_uncached(year, make, model):
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
    recalls = _check_vehicle_recalls(year=year, make=make, model=lookup_model)
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


def _get_vehicle_safety_profile(year, make, model):
    context = _request_context.get()
    key = ('safety', int(year), str(make).casefold().strip(), str(model).casefold().strip())
    research = context.setdefault('research_cache', {}) if context is not None else {}
    if key not in research:
        research[key] = _get_vehicle_safety_profile_uncached(year, make, model)
    return research[key]


def get_vehicle_safety_profile(year, make, model):
    return _get_vehicle_safety_profile(year, make, model)


BASE_WEIGHTS = {'affordability': 30, 'energy': 20, 'space': 15, 'weather': 10, 'safety': 15, 'environment': 10}


def _clip(value):
    return max(0, min(1, value))


def rank_vehicles_for_lifestyle(candidates, lifestyle=None):
    lifestyle = dict(lifestyle or {})
    for field in ('weather_conditions', 'road_conditions', 'preferred_fuel_types', 'hard_constraints'):
        if field in lifestyle:
            lifestyle[field] = list(dict.fromkeys(lifestyle[field]))
    weights = dict(BASE_WEIGHTS)
    for field, component in [('fuel_efficiency_priority', 'energy'), ('safety_priority', 'safety'), ('environmental_priority', 'environment')]:
        if field in lifestyle:
            weights[component] *= {'low': .5, 'medium': 1, 'high': 1.5}[lifestyle[field]]
    total_weight = sum(weights.values())
    weights = {key: value * 100 / total_weight for key, value in weights.items()}
    ranked = []
    weather_conditions = lifestyle.get('weather_conditions', [])
    road_conditions = lifestyle.get('road_conditions', [])
    hard_fields = set(lifestyle.get('hard_constraints', []))

    for order, candidate in enumerate(candidates):
        identity = {key: candidate[key] for key in ('year', 'make', 'model')}
        hint_fields = ('configuration', 'configuration_id', 'vehicle_id', 'trim', 'fuel_type', 'drivetrain',
                       'transmission', 'engine', 'body_style')
        efficiency = _get_vehicle_efficiency(**identity, **{key: candidate[key] for key in hint_fields if key in candidate})
        safety = _get_vehicle_safety_profile(**identity)
        recalls = _check_vehicle_recalls(**identity)
        scores = {key: .5 for key in weights}
        known, reasons, tradeoffs, failures, missing, sensitivity = set(), [], [], [], [], []
        v = {} if 'error' in efficiency else efficiency
        if not v:
            missing.append({'field': 'efficiency', 'detail': efficiency})
        resolution = v.get('configuration_resolution') or {'mode': 'ambiguous', 'confidence': 'low', 'alternatives': []}
        if resolution.get('requires_clarification'):
            sensitivity.append('Available EPA configurations span different powertrain types; confirming the powertrain could change energy, cost, and ranking results.')
        fuel_type = v.get('fuel_type')
        mpg = v.get('combined_mpg')
        if v.get('city_mpg') and v.get('highway_mpg') and 'city_driving_share' in lifestyle:
            share = lifestyle['city_driving_share']
            mpg = 1 / (share / v['city_mpg'] + (1 - share) / v['highway_mpg'])

        price, cost, affordability_parts = candidate.get('listing_price'), None, []
        if price is not None:
            if lifestyle.get('maximum_purchase_price'):
                cap = lifestyle['maximum_purchase_price']
                affordability_parts.append(_clip(1 - price / cap / 2))
                if price > cap:
                    failures.append('Listing price exceeds the explicitly declared firm purchase budget.')
            finance_fields = ('down_payment', 'apr_percent', 'loan_months')
            if all(field in lifestyle for field in finance_fields) and fuel_type:
                args = {'vehicle_price': price, **{field: lifestyle[field] for field in finance_fields},
                        'fuel_type': fuel_type, 'energy_data_source': 'government_epa',
                        **{field: lifestyle[field] for field in COST_DEFAULTS if field in lifestyle and field != 'fuel_type'}}
                if mpg:
                    args['mpg'] = mpg
                if fuel_type in ('electric', 'plug_in_hybrid') and v.get('kwh_per_100_miles'):
                    args['kwh_per_100_miles'] = v['kwh_per_100_miles']
                if fuel_type == 'plug_in_hybrid' and 'electric_mile_share' in lifestyle:
                    args['electric_mile_share'] = lifestyle['electric_mile_share']
                if fuel_type == 'plug_in_hybrid' and (v.get('phev_blended') or 'electric_mile_share' not in args):
                    missing.append({'field': 'ownership_cost', 'detail': 'PHEV cost needs a non-blended EPA record and an electric-mile share.'})
                else:
                    estimate = _calculate_ownership_cost(**args)
                    if 'error' not in estimate:
                        cost = estimate
                        government_keys = [key for key in ('mpg', 'kwh_per_100_miles') if key in args]
                        cost['government_source_data'] = {key: args[key] for key in government_keys}
                        cost['government_sources'] = v.get('sources', [])
                        for key in government_keys:
                            cost['user_provided_inputs'].pop(key, None)
                        if lifestyle.get('maximum_monthly_ownership_cost'):
                            cap = lifestyle['maximum_monthly_ownership_cost']
                            affordability_parts.append(_clip(1 - cost['estimated_total_monthly'] / cap / 2))
                            if cost['estimated_total_monthly'] > cap:
                                message = 'Estimated monthly cost exceeds the supplied monthly target.'
                                if 'maximum_monthly_ownership_cost' in hard_fields:
                                    failures.append(message)
                                else:
                                    tradeoffs.append(message)
                    else:
                        missing.append({'field': 'ownership_cost', 'detail': estimate})
        if affordability_parts:
            scores['affordability'] = sum(affordability_parts) / len(affordability_parts)
            known.add('affordability')
        if cost is None:
            missing.append({'field': 'ownership_cost', 'detail': 'Need listing price, financing terms, and adequate EPA energy data.'})

        energy_measure = v.get('combined_mpge') if fuel_type == 'electric' else mpg
        if energy_measure:
            scores['energy'] = _clip((energy_measure - 15) / (125 if fuel_type == 'electric' else 45))
            known.add('energy')
        preferences = lifestyle.get('preferred_fuel_types')
        if fuel_type and preferences:
            scores['energy'] = (scores['energy'] + (1 if fuel_type in preferences else .35)) / 2
            known.add('energy')
            if fuel_type not in preferences:
                tradeoffs.append('Powertrain is outside the preferred fuel types; treated as a preference rather than a disqualification.')
        charging = lifestyle.get('charging_access')
        if fuel_type in ('electric', 'plug_in_hybrid') and charging:
            charging_fit = {'none': .15, 'public_only': .35, 'home_120v': .65, 'workplace': .7,
                            'home_240v': 1, 'home_and_public': 1}[charging]
            scores['energy'] = (scores['energy'] + charging_fit) / 2
            known.add('energy')
            if charging in ('none', 'public_only'):
                tradeoffs.append('Limited charging access reduces EV/PHEV convenience; verify a practical charging routine.')
                if 'charging_access' in hard_fields and fuel_type == 'electric':
                    failures.append('Pure EV conflicts with the explicitly declared charging-access hard constraint.')
        if fuel_type in ('electric', 'plug_in_hybrid') and lifestyle.get('willing_to_charge_ev') is False:
            scores['energy'] = min(scores['energy'], .2 if fuel_type == 'electric' else .35)
            known.add('energy')
            tradeoffs.append('The stated unwillingness to charge materially reduces electric-powertrain fit, but it is not treated as a hard constraint unless charging_access is explicitly declared hard.')

        space_parts, vehicle_class = [], str(v.get('vehicle_class') or '').lower()
        if 'regular_passenger_count' in lifestyle and v.get('passenger_volume_cu_ft'):
            space_parts.append(_clip(v['passenger_volume_cu_ft'] / (lifestyle['regular_passenger_count'] * 25)))
            tradeoffs.append('Passenger volume is a comfort proxy; verify seat count and child-seat fit.')
        if lifestyle.get('cargo_need') and v.get('luggage_volume_cu_ft'):
            target = {'low': 10, 'medium': 18, 'high': 30}[lifestyle['cargo_need']]
            space_parts.append(_clip(v['luggage_volume_cu_ft'] / target))
        if lifestyle.get('parking_constraint') in ('moderate', 'tight') and vehicle_class:
            compact = any(label in vehicle_class for label in ('compact', 'subcompact', 'two-seater'))
            large = any(label in vehicle_class for label in ('large', 'standard', 'van', 'pickup'))
            space_parts.append(1 if compact else .3 if large else .6)
            tradeoffs.append('EPA class is a parking proxy; exterior dimensions and turning radius need verification.')
        if space_parts:
            scores['space'] = sum(space_parts) / len(space_parts)
            known.add('space')

        climate = {'component_score': 50, 'evidence_used': [],
                   'applicable_conditions': weather_conditions + road_conditions,
                   'positive_fit_factors': [], 'cautions': [], 'unavailable_information': [], 'user_actions': []}
        drive = str(v.get('drivetrain') or '').lower()
        winter = any(condition in weather_conditions for condition in ('light_snow', 'heavy_snow', 'ice', 'mountain_weather'))
        if winter and drive:
            climate['evidence_used'].append('EPA drivetrain: ' + v['drivetrain'])
            if 'all-wheel' in drive or '4-wheel' in drive:
                scores['weather'] = .75
                climate['positive_fit_factors'].append('Explicit AWD/4WD receives a modest snow/ice fit benefit.')
            elif 'front' in drive:
                scores['weather'] = .6
                climate['positive_fit_factors'].append('FWD remains a viable winter layout and is not treated as a failure.')
            elif 'rear' in drive:
                scores['weather'] = .35
                climate['cautions'].append('RWD receives a modest fit reduction for important snow/ice use.')
            known.add('weather')
            climate['user_actions'].append('Check tire condition and obtain condition-appropriate tires; drivetrain does not guarantee winter safety.')
        if any(condition in road_conditions for condition in ('potholes', 'gravel', 'unpaved', 'off_road')):
            climate['unavailable_information'] += ['ground clearance', 'tire specification', 'suspension durability', 'off-road capability']
            climate['cautions'].append('EPA vehicle class is only a limited rough-road proxy; no capability claim is made.')
            climate['user_actions'].append('Verify ground clearance, tires, and underbody condition for the intended roads.')
        if 'extreme_cold' in weather_conditions and fuel_type in ('electric', 'plug_in_hybrid'):
            climate['cautions'].append('Extreme cold can reduce usable electric range and increase cabin-heating energy use; no exact loss is estimated.')
        if 'extreme_heat' in weather_conditions and fuel_type in ('electric', 'plug_in_hybrid'):
            climate['cautions'].append('Extreme heat can increase climate-control energy use and affect battery performance; no degradation is predicted.')
        if 'coastal_salt' in weather_conditions:
            climate['user_actions'].append('Inspect a used vehicle for corrosion and salt exposure; no vehicle-specific corrosion penalty is applied.')
        if 'flood_prone' in weather_conditions:
            climate['user_actions'].append('Check title/history and obtain a pre-purchase inspection for flood exposure; no flood capability is inferred.')
        if any(condition in weather_conditions for condition in ('heavy_rain', 'high_winds')):
            climate['user_actions'].append('Verify tires, wipers, lights, and condition-specific handling during an inspection or test drive.')
        climate['component_score'] = round(scores['weather'] * 100, 1)

        if safety.get('overall_rating') and safety.get('rating_scope_matches_candidate', True):
            scores['safety'] = (safety['overall_rating'] - 1) / 4
            known.add('safety')
        if v.get('emissions_score') is not None:
            scores['environment'] = v['emissions_score'] / 10
            known.add('environment')
        for component in scores:
            if component not in known:
                missing.append({'field': component, 'detail': 'Missing evidence or unspecified preference; neutral midpoint applied.'})
        if safety.get('rating_scope_matches_candidate') is False:
            tradeoffs.append('Base-model safety data may not apply to the exact configuration; safety remains neutral.')
        if candidate.get('mileage') is not None:
            tradeoffs.append('Odometer mileage is recorded but is not used to predict repairs or reliability.')
        if candidate.get('vin'):
            tradeoffs.append('User-supplied VIN is context only; no bulk or automatic VIN lookup occurs.')
        missing.extend(safety.get('missing_data', []))
        if 'error' in recalls:
            missing.append({'field': 'recalls', 'detail': recalls})

        evidence = {
            'affordability': f'Listing price {price}; only supplied budgets and financing terms are evaluated.',
            'energy': f'{fuel_type or "Unknown powertrain"}; {energy_measure} {"MPGe" if fuel_type == "electric" else "MPG"}.',
            'space': f'EPA class {v.get("vehicle_class")}; passenger volume {v.get("passenger_volume_cu_ft")}; luggage volume {v.get("luggage_volume_cu_ft")}.',
            'weather': '; '.join(climate['evidence_used']) or 'No score-changing weather evidence available.',
            'safety': f'Applicable common NHTSA overall rating: {safety.get("overall_rating")} of 5.',
            'environment': f'EPA GHG score: {v.get("emissions_score")} of 10; not lifecycle emissions.',
        }
        ordered = sorted(known, key=lambda key: (-scores[key], list(weights).index(key)))
        reasons = [f'{key}: {scores[key] * 100:.0f}% fit. {evidence[key]}' for key in ordered[:3]]
        if resolution.get('possible_result_range'):
            sensitivity.append('Other EPA configurations may change the result within: ' + json.dumps(resolution['possible_result_range'], sort_keys=True))
            annual_range = resolution['possible_result_range'].get('annual_fuel_cost_usd')
            monthly_cap = lifestyle.get('maximum_monthly_ownership_cost')
            if cost and annual_range and monthly_cap:
                selected_energy = cost['estimated_fuel_monthly'] + cost['estimated_electricity_monthly']
                fixed_cost = cost['estimated_total_monthly'] - selected_energy
                possible_monthly = (fixed_cost + annual_range['minimum'] / 12, fixed_cost + annual_range['maximum'] / 12)
                if min(possible_monthly) <= monthly_cap <= max(possible_monthly):
                    sensitivity.append('Configuration ambiguity can move the ownership estimate across the supplied monthly-cost target; clarify powertrain/trim before treating affordability as settled.')
        component_points = {key: round(scores[key] * weights[key], 1) for key in weights}
        total_score = round(sum(component_points.values()), 1)
        hard_results = [{'constraint': 'maximum_purchase_price', 'passed': price <= lifestyle['maximum_purchase_price'],
                         'detail': f'Listing price {price} versus firm cap {lifestyle["maximum_purchase_price"]}.'}
                        for _ in [0] if price is not None and lifestyle.get('maximum_purchase_price')]
        sources = list(dict.fromkeys(v.get('sources', []) + safety.get('sources', []) + recalls.get('sources', [])))
        ranked.append({'candidate': candidate, 'original_order': order, 'overall_score': total_score,
                       'total_fit_score': total_score, 'component_scores': component_points,
                       'component_fit_percent': {key: round(value * 100, 1) for key, value in scores.items()},
                       'weights': weights, 'selected_configuration': resolution.get('selected_description'),
                       'automatic_selection_confidence': resolution.get('confidence'),
                       'configuration_resolution': resolution, 'assumptions': ['Missing score inputs receive a neutral midpoint.'],
                       'strongest_fit_reasons': reasons or ['Insufficient evidence; neutral scores applied.'],
                       'important_tradeoffs': tradeoffs, 'tradeoffs': tradeoffs,
                       'hard_constraint_results': hard_results, 'hard_constraint_failures': failures,
                       'sensitivity_notes': sensitivity, 'missing_or_ambiguous_data': missing,
                       'unknown_or_needs_verification': list(dict.fromkeys(climate['unavailable_information'])),
                       'climate_weather_fit': climate, 'data_completeness': round(len(known) / len(weights), 3),
                       'data_completeness_score': round(len(known) / len(weights) * 100),
                       'ownership_cost': cost, 'efficiency': efficiency, 'safety': safety, 'recalls': recalls,
                       'government_sources_used': sources})
    ranked.sort(key=lambda row: (-row['total_fit_score'], -row['data_completeness'], row['original_order']))
    close = None
    if len(ranked) > 1 and ranked[0]['total_fit_score'] - ranked[1]['total_fit_score'] <= 2:
        close = {'effectively_tied': True,
                 'candidates': [ranked[0]['candidate'], ranked[1]['candidate']],
                 'score_difference': round(ranked[0]['total_fit_score'] - ranked[1]['total_fit_score'], 1),
                 'separator': 'The user preference with the most leverage is shown in each candidate sensitivity note; confirm configuration and hard constraints before choosing.'}
    return {'ranked_vehicles': ranked, 'weights': weights, 'lifestyle_inputs': lifestyle,
            'close_comparison': close,
            'method': '100-point deterministic fit. Missing evidence receives a neutral midpoint. Hard constraints are reported separately and only explicitly hard fields can disqualify. Tie order: score, completeness, original order.',
            'ownership_duration_years': lifestyle.get('expected_ownership_years'),
            'duration_note': 'Duration is context; no depreciation, resale, reliability, or future repair forecast is modeled.',
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


IDENTITY = {'year': num('Four-digit model year, e.g. 2022. Future/nonexistent years remain valid syntax so the tool can preserve other candidates and return a no-match result.', 1981, 2100, True),
            'make': text('Manufacturer name as listed in government menus, e.g. Honda.'),
            'model': text('Exact government model name, e.g. Accord Hybrid for EPA; NHTSA may use Accord.')}
FUEL_TYPES = ['gasoline', 'diesel', 'hybrid', 'plug_in_hybrid', 'electric']
CONFIGURATION_HINTS = {
    'configuration': text('Optional free-text EPA configuration hint; matching is deterministic and case-insensitive.', max_length=160),
    'configuration_id': num('Optional exact FuelEconomy.gov vehicle/configuration ID; must match year, make, and model.', 1, 999999, True),
    'vehicle_id': num('Legacy alias for configuration_id; supply only one ID.', 1, 999999, True),
    'trim': text('Optional trim name such as EX-L or XLE; highest-weight matching hint.', max_length=80),
    'fuel_type': choice('Optional requested powertrain hint; used to avoid gasoline/hybrid/EV ambiguity.', FUEL_TYPES),
    'drivetrain': text('Optional drivetrain hint such as AWD, all-wheel drive, FWD, or 4WD.', max_length=60),
    'transmission': text('Optional transmission hint such as automatic, CVT, or 6-speed manual.', max_length=80),
    'engine': text('Optional engine hint such as 2.0L 4-cylinder.', max_length=80),
    'body_style': text('Optional body or vehicle-class hint such as hatchback or compact SUV.', max_length=80),
}
EFFICIENCY = obj({**IDENTITY, **CONFIGURATION_HINTS}, IDENTITY)
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
CANDIDATE = obj({**IDENTITY, **CONFIGURATION_HINTS, 'vin': text('Optional 17-character VIN explicitly provided by the user; context only, never decoded or looked up.', r'^[A-HJ-NPR-Z0-9]{17}$', 17),
                 'listing_price': num('Optional asking price from a user-found listing in USD; not market value.', .01),
                 'mileage': num('Optional odometer miles, recorded without reliability scoring.', 0, 2_000_000, True)}, IDENTITY)
WEATHER_CONDITIONS = ['mild', 'mixed_seasons', 'light_snow', 'heavy_snow', 'ice', 'heavy_rain', 'flood_prone',
                      'extreme_heat', 'extreme_cold', 'high_winds', 'coastal_salt', 'mountain_weather']
ROAD_CONDITIONS = ['mostly_city', 'mostly_highway', 'steep_grades', 'potholes', 'gravel', 'unpaved', 'off_road']
LIFESTYLE = obj({
    **{k: v for k, v in COST_PROPERTIES.items() if k not in ('vehicle_price', 'fuel_type', 'mpg', 'mpge', 'kwh_per_100_miles', 'energy_data_source')},
    'maximum_purchase_price': num('Firm maximum purchase price in USD; violations reported separately.', .01),
    'maximum_monthly_ownership_cost': num('Desired monthly ownership USD target; a soft target, not guarantee.', .01, 100000),
    'city_driving_share': num('Fraction of driving in city, 0 to 1; remaining fraction is highway.', 0, 1),
    'highway_driving_share': num('Optional fraction of driving on highways, 0 to 1; when city share is supplied it remains authoritative.', 0, 1),
    'daily_commute_miles': num('Round-trip daily commute distance in miles.', 0, 1000),
    'regular_passenger_count': num('Regular occupants including driver; volume proxy cannot establish seat count.', 1, 12, True),
    'cargo_need': choice('Cargo need category; maps to 10/18/30 cubic foot proxies.', ['low', 'medium', 'high']),
    'parking_constraint': choice('Parking space constraint; EPA class is a rough size proxy.', ['none', 'moderate', 'tight']),
    'weather_conditions': {'type': 'array', 'description': 'One or more climate or weather conditions; duplicates are removed by the tool.',
                           'items': choice('Supported weather condition.', WEATHER_CONDITIONS), 'minItems': 1, 'maxItems': 12},
    'road_conditions': {'type': 'array', 'description': 'One or more recurring road/driving conditions; duplicates are removed by the tool.',
                        'items': choice('Supported road condition.', ROAD_CONDITIONS), 'minItems': 1, 'maxItems': 7},
    'charging_access': choice('Available EV/PHEV charging access.', ['none', 'home_120v', 'home_240v', 'workplace', 'public_only', 'home_and_public']),
    **{k: choice('Relative scoring priority; medium leaves base weight, low halves and high multiplies by 1.5.', ['low', 'medium', 'high']) for k in ('fuel_efficiency_priority', 'safety_priority', 'environmental_priority')},
    'preferred_fuel_types': {'type': 'array', 'description': 'Optional preferred powertrain categories.', 'items': choice('Powertrain category.', FUEL_TYPES), 'minItems': 1, 'maxItems': 5, 'uniqueItems': True},
    'willing_to_charge_ev': {'type': 'boolean', 'description': 'Whether the user is willing to charge; false strongly reduces EV/PHEV fit but is not automatically disqualifying.'},
    'expected_ownership_years': num('Expected ownership duration in years, context only; no depreciation forecast.', .5, 30),
    'hard_constraints': {'type': 'array', 'description': 'Only fields explicitly listed here are treated as disqualifying, except maximum_purchase_price which is declared firm by its field definition.',
                         'items': choice('Explicit hard constraint field.', ['maximum_purchase_price', 'maximum_monthly_ownership_cost', 'charging_access']),
                         'minItems': 1, 'maxItems': 3},
})
SCHEMAS = {'get_vehicle_efficiency': EFFICIENCY, 'calculate_ownership_cost': COST_SCHEMA,
           'check_vehicle_recalls': obj(IDENTITY, IDENTITY), 'get_vehicle_safety_profile': obj(IDENTITY, IDENTITY),
           'rank_vehicles_for_lifestyle': obj({'candidates': {'type': 'array', 'description': 'Two to five vehicles supplied by user; no inventory search.', 'items': CANDIDATE, 'minItems': 2, 'maxItems': 5},
                                             'lifestyle': {**LIFESTYLE, 'description': 'Optional preferences; omitted preferences are neutral, never invented.'}}, ('candidates',))}
DESCRIPTIONS = {
    'get_vehicle_efficiency': 'Use for government efficiency/specifications for one known vehicle; completes EPA menu flow and deterministic configuration resolution internally. Do not use for live inventory, market value, or every candidate before a ranking.',
    'calculate_ownership_cost': 'Use for financing and energy budgets with purchase price, financing terms, and adequate energy assumptions. Do not use to predict repairs or local market value. Estimates exclude taxes, fees and depreciation.',
    'check_vehicle_recalls': 'Use for model-level NHTSA recall campaigns for a known vehicle. Do not use to prove a VIN is affected or a repair is open/completed.',
    'get_vehicle_safety_profile': 'Use for NHTSA ratings, recall count, and bounded complaint context. Completes variant and ID requests internally. Do not use complaint or recall counts as reliability scores.',
    'rank_vehicles_for_lifestyle': 'Use once to compare two to five user-supplied candidates. Internally resolves configurations, fetches EPA efficiency plus NHTSA safety/complaints/recalls for all candidates, calculates cost, and ranks weather/road/lifestyle fit. Do not call lower-level research tools around it, search inventory, or certify a winner; omit unknown preferences.',
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
        token = _request_context.set({'deadline': time.monotonic() + 45, 'cache': {}, 'research_cache': {}})
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
