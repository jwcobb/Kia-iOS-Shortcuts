from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from main import create_app
from hyundai_kia_connect_api.exceptions import AuthenticationOTPRequired

@pytest.fixture
def setup():
    env = {'SECRET_KEY': 'x' * 48}
    managers = []
    for alias, vin in [('TELLURIDE', 'A' * 17), ('EV9', 'B' * 17)]:
        for key, value in [('USERNAME', alias), ('PASSWORD', 'secret'), ('PIN', '0123'), ('VIN', vin)]:
            env[f'{alias}_{key}'] = value
    def factory(**kwargs):
        m = Mock()
        m.vehicles = {k: SimpleNamespace(id=k, VIN=v, name=k, model=k) for k,v in [('wrong','C'*17), ('telluride','A'*17), ('ev9','B'*17)]}
        managers.append(m)
        return m
    return create_app(env, factory).test_client(), managers

H = {'Authorization': 'Bearer ' + 'x' * 48}

def test_health_and_auth_do_not_contact_kia(setup):
    c, ms = setup
    assert c.get('/healthz').status_code == 200
    assert c.post('/vehicles/ev9/unlock_car').status_code == 401
    for m in ms: m.check_and_refresh_token.assert_not_called()

def test_exact_vin_and_correct_primary_account(setup):
    c, ms = setup
    assert c.post('/vehicles/ev9/lock_car', headers=H).status_code == 202
    ms[1].lock.assert_called_once_with('ev9')
    ms[0].lock.assert_not_called()
    assert c.post('/vehicles/ev9/lock_car', headers=H).status_code == 429
    assert c.post('/vehicles/telluride/lock_car', headers=H).status_code == 202
    ms[0].lock.assert_called_once_with('telluride')

def test_missing_vin_fails_closed(setup):
    c, ms = setup
    ms[1].vehicles.pop('ev9')
    assert c.post('/vehicles/ev9/unlock_car', headers=H).status_code == 422
    ms[1].unlock.assert_not_called()

def test_no_get_mutation_or_ambiguous_route(setup):
    c, ms = setup
    assert c.get('/vehicles/ev9/unlock_car', headers=H).status_code == 405
    assert c.post('/unlock_car', headers=H).status_code == 404
    assert c.post('/vehicles/ev9/unlock_car', headers=H, json={'vehicle':'telluride'}).status_code == 400
    ms[1].unlock.assert_not_called()

def test_error_does_not_leak_secrets(setup):
    c, ms = setup
    ms[1].check_and_refresh_token.side_effect = RuntimeError('password=secret')
    r = c.get('/vehicles/ev9', headers=H)
    assert r.status_code == 502
    assert b'password=secret' not in r.data

def test_duplicate_vin_fails_closed(setup):
    c, ms = setup
    ms[1].vehicles['duplicate'] = SimpleNamespace(id='duplicate', VIN='B'*17)
    assert c.post('/vehicles/ev9/unlock_car', headers=H).status_code == 422
    ms[1].unlock.assert_not_called()

def test_climate_options_and_submission(setup):
    c, ms = setup
    r = c.post('/vehicles/telluride/start_climate', headers=H)
    assert r.status_code == 202
    args = ms[0].start_climate.call_args.args
    assert args[0] == 'telluride'
    assert args[1].set_temp == 72
    assert args[1].duration == 10

def test_configuration_fails_before_contacting_kia():
    with pytest.raises(ValueError, match='SECRET_KEY'):
        create_app({})
    with pytest.raises(ValueError, match='TELLURIDE'):
        create_app({'SECRET_KEY':'x'*48})

def test_otp_request_uses_the_selected_channel(setup):
    c, ms = setup
    ms[1].check_and_refresh_token.side_effect = AuthenticationOTPRequired()
    r = c.post('/vehicles/ev9/otp/send', headers=H, json={'channel': 'email'})
    assert r.status_code == 202
    assert r.json['status'] == 'otp_sent'
    assert ms[1].send_otp.call_args.args[0].value == 'EMAIL'
    assert c.post('/vehicles/ev9/otp/send', headers=H, json={'channel': 'voice'}).status_code == 400
