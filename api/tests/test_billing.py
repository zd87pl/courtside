from types import SimpleNamespace

import pytest
from courtside_api import billing


@pytest.mark.parametrize('data', [{'limit': None, 'limit_remaining': None},
    {'limit': 0, 'limit_remaining': 0}, {'limit': 50, 'limit_remaining': 0}])
def test_provider_budget_guard_rejects_unbounded_or_exhausted_keys(monkeypatch, data):
    monkeypatch.setattr(billing, '_provider_get', lambda path: data)
    with pytest.raises(RuntimeError): billing.verify_provider_budget()


def test_provider_budget_guard_accepts_capped_key(monkeypatch):
    monkeypatch.setattr(billing, '_provider_get', lambda path: {'limit': 50, 'limit_remaining': 49})
    billing.verify_provider_budget()


def test_every_provider_attempt_is_accounted_including_schema_fallback():
    from courtside.vlm import ServerVLM
    vlm = ServerVLM.__new__(ServerVLM)
    calls, reservations, settled = [], [], []
    class Rejected(Exception): status_code = 400
    def create(**request):
        calls.append(request)
        if 'response_format' in request: raise Rejected()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'), finish_reason='stop')], usage=None)
    def begin(model):
        reservations.append(model)
        return len(reservations)
    vlm.model = 'qwen/qwen3.8-27b'
    vlm._billing = SimpleNamespace(begin=begin, finish=lambda call_id, **result: settled.append((call_id, result)))
    vlm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert vlm.generate('p', json_schema={'type': 'object'})[0] == '{}'
    assert len(reservations) == len(calls) == len(settled) == 2
    assert isinstance(settled[0][1]['error'], Rejected)
    assert 'response' in settled[1][1]
