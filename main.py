"""Kia shortcut API; one explicit vehicle and primary account per alias."""
import hmac
import logging
import os
import threading
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from hyundai_kia_connect_api import ClimateRequestOptions, VehicleManager

load_dotenv()
# Upstream libraries can log credentials or vehicle state at verbose levels.
logging.getLogger('hyundai_kia_connect_api').setLevel(logging.CRITICAL)

@dataclass
class Account:
    manager: object
    vin: str
    lock: object = field(default_factory=threading.Lock)
    last_command: float = float('-inf')


def create_app(env=None, manager_factory=VehicleManager):
    env = os.environ if env is None else env
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 1024
    secret = env.get('SECRET_KEY', '')
    if len(secret) < 32:
        raise ValueError('SECRET_KEY must have at least 32 characters')
    accounts = {}
    for alias in ('telluride', 'ev9'):
        prefix = alias.upper()
        fields = {key: env.get(f'{prefix}_{key}', '').strip() for key in ('USERNAME', 'PASSWORD', 'PIN', 'VIN')}
        if not all(fields.values()):
            raise ValueError(f'Missing required {prefix} account settings')
        if len(fields['VIN']) != 17:
            raise ValueError(f'{prefix}_VIN must contain 17 characters')
        accounts[alias] = Account(manager_factory(region=3, brand=1,
            username=fields['USERNAME'], password=env[f'{prefix}_PASSWORD'], pin=fields['PIN']), fields['VIN'].upper())

    @app.before_request
    def authorize():
        if request.path == '/healthz':
            return None
        supplied = request.headers.get('Authorization', '')
        if not hmac.compare_digest(supplied.encode(), f'Bearer {secret}'.encode()):
            return jsonify(error='Unauthorized'), 401

    @app.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/healthz')
    def health():
        return jsonify(status='ok')  # No Kia login or vehicle wake-up.

    def select(account):
        account.manager.check_and_refresh_token()
        matches = [v for v in account.manager.vehicles.values() if (v.VIN or '').upper() == account.vin]
        if len(matches) != 1:
            raise LookupError('Configured VIN is not uniquely available on this account')
        return matches[0]

    @app.get('/vehicles/<alias>')
    def vehicle(alias):
        account = accounts.get(alias)
        if account is None:
            return jsonify(error='Unknown vehicle'), 404
        if not account.lock.acquire(blocking=False):
            return jsonify(error='Account busy'), 409
        try:
            v = select(account)
            return jsonify(alias=alias, id=v.id, vin=v.VIN, name=v.name, model=v.model)
        except LookupError:
            return jsonify(error='Configured VIN not found uniquely on this account'), 422
        except Exception:
            return jsonify(error='Kia authentication or vehicle discovery failed; check credentials and any Kia MFA requirement'), 502
        finally:
            account.lock.release()

    actions = {'lock_car': 'lock', 'unlock_car': 'unlock', 'start_climate': 'start_climate', 'stop_climate': 'stop_climate'}

    @app.post('/vehicles/<alias>/<action>')
    def command(alias, action):
        account = accounts.get(alias)
        if account is None or action not in actions:
            return jsonify(error='Unknown vehicle or action'), 404
        if request.get_data():
            return jsonify(error='This endpoint does not accept a request body'), 400
        if not account.lock.acquire(blocking=False):
            return jsonify(error='Account busy; command not sent'), 409
        try:
            if time.monotonic() - account.last_command < 10:
                return jsonify(error='Wait 10 seconds between commands'), 429
            v = select(account)
            account.last_command = time.monotonic()
            fn = getattr(account.manager, actions[action])
            if action == 'start_climate':
                fn(v.id, ClimateRequestOptions(set_temp=72, duration=10))
            else:
                fn(v.id)
            return jsonify(status='submitted', vehicle=alias, action=action), 202
        except LookupError:
            return jsonify(error='Configured VIN not found uniquely; command not sent'), 422
        except Exception:
            return jsonify(error='Kia request failed; completion is unknown. Check the Kia app before retrying.'), 502
        finally:
            account.lock.release()

    return app
