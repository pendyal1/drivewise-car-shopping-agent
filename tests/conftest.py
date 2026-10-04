"""Every external request and model invocation is blocked unless explicitly mocked."""
import json
import pytest
import requests


class Response:
    def __init__(self, data=None, status=200, raw=None):
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps(data).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size):
        yield from (self.raw[i:i+size] for i in range(0, len(self.raw), size))


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Live calls forbidden in tests')
    monkeypatch.setattr(requests, 'get', blocked)
    import app
    monkeypatch.setattr(app.litellm, 'completion', blocked)
    monkeypatch.setattr(app, 'sessions', app.SessionStore())


@pytest.fixture
def government(monkeypatch):
    import tools
    calls = []
    records = {
        1: {'id': 1, 'year': 2022, 'make': 'Honda', 'model': 'Accord Hybrid', 'atvType': 'Hybrid',
            'fuelType': 'Regular Gasoline', 'fuelType1': 'Regular Gasoline', 'city08': 48, 'highway08': 47, 'comb08': 48,
            'trany': 'Automatic', 'drive': 'Front-Wheel Drive', 'VClass': 'Large Cars', 'pv4': 105, 'lv4': 17,
            'feScore': 9, 'ghgScore': 9, 'cylinders': 4, 'displ': 2, 'fuelCost08': 1100},
        2: {'id': 2, 'year': 2022, 'make': 'Toyota', 'model': 'Camry Hybrid', 'atvType': 'Hybrid',
            'fuelType1': 'Regular Gasoline', 'city08': 51, 'highway08': 53, 'comb08': 52,
            'drive': 'Front-Wheel Drive', 'VClass': 'Mid-Size Cars', 'pv4': 100, 'lv4': 15, 'ghgScore': 9},
        3: {'id': 3, 'year': 2021, 'make': 'Subaru', 'model': 'Legacy', 'fuelType1': 'Regular Gasoline',
            'city08': 27, 'highway08': 35, 'comb08': 30, 'drive': 'All-Wheel Drive', 'VClass': 'Large Cars', 'ghgScore': 6},
    }
    def get(url, **kwargs):
        calls.append((url, kwargs))
        assert kwargs['timeout'] == (3, 6)
        assert kwargs['headers']['Accept'] == 'application/json'
        assert kwargs['headers']['User-Agent'].startswith('DriveWise')
        assert kwargs['stream'] is True and kwargs['allow_redirects'] is False
        params = kwargs.get('params') or {}
        if '/menu/year' in url:
            return Response({'menuItem': [{'text': str(y), 'value': str(y)} for y in (2021, 2022)]})
        if '/menu/make' in url:
            return Response({'menuItem': [{'text': m, 'value': m} for m in ('Honda', 'Toyota', 'Subaru')]})
        if '/menu/model' in url:
            return Response({'menuItem': [{'text': r['model'], 'value': r['model']} for r in records.values() if r['make'] == params['make']]})
        if '/menu/options' in url:
            return Response({'menuItem': [{'text': '2.0L automatic', 'value': str(k)} for k, r in records.items() if r['model'] == params['model']]})
        if '/vehicle/' in url:
            return Response(records[int(url.rsplit('/', 1)[1])])
        if '/SafetyRatings/modelyear/' in url:
            return Response({'Results': [{'VehicleId': 100, 'VehicleDescription': '4-door FWD'}]})
        if '/SafetyRatings/VehicleId/' in url:
            return Response({'Results': [{'OverallRating': '5', 'OverallFrontCrashRating': '4', 'OverallSideCrashRating': '5', 'RolloverRating': '4'}]})
        if '/recalls/' in url:
            return Response({'results': [{'NHTSACampaignNumber': '22V001000', 'Component': 'BRAKES', 'Summary': 'Test summary', 'Consequence': 'Test consequence', 'Remedy': 'Dealer repair', 'ReportReceivedDate': '01/01/2022'}]})
        if '/complaints/' in url:
            return Response({'results': [{'components': 'BRAKES', 'summary': 'Sensitive consumer report excluded'}]})
        raise AssertionError('Unexpected endpoint')
    monkeypatch.setattr(tools.requests, 'get', get)
    return records, calls
