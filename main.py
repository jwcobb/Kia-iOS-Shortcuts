"""Kia shortcut API; one explicit vehicle and primary account per alias."""
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from hyundai_kia_connect_api import ClimateRequestOptions, VehicleManager
from hyundai_kia_connect_api import Token
from hyundai_kia_connect_api.const import OTP_NOTIFY_TYPE
from hyundai_kia_connect_api.exceptions import (
    AuthenticationError,
    AuthenticationOTPRequired,
    ConsentRequiredError,
)

load_dotenv()
# Upstream libraries can log credentials or vehicle state at verbose levels.
logging.getLogger('hyundai_kia_connect_api').setLevel(logging.CRITICAL)

@dataclass
class Account:
    manager: object
    vin: str
    token_path: Path
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
    token_directory = Path(env.get('TOKEN_STORAGE_PATH', 'storage/tokens'))

    def load_token(path):
        if not path.is_file():
            return None
        try:
            return Token.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            app.logger.warning('Ignoring unreadable Kia session token at %s', path)
            return None

    def save_token(account):
        token_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        account.token_path.parent.chmod(0o700)
        temporary_path = account.token_path.with_suffix('.tmp')
        temporary_path.write_text(json.dumps(account.manager.token.to_dict()))
        temporary_path.chmod(0o600)
        os.replace(temporary_path, account.token_path)

    for alias in ('telluride', 'ev9'):
        prefix = alias.upper()
        fields = {key: env.get(f'{prefix}_{key}', '').strip() for key in ('USERNAME', 'PASSWORD', 'PIN', 'VIN')}
        if not all(fields.values()):
            raise ValueError(f'Missing required {prefix} account settings')
        if len(fields['VIN']) != 17:
            raise ValueError(f'{prefix}_VIN must contain 17 characters')
        token_path = token_directory / f'{alias}.json'
        accounts[alias] = Account(manager_factory(region=3, brand=1,
            username=fields['USERNAME'], password=env[f'{prefix}_PASSWORD'], pin=fields['PIN'], token=load_token(token_path)),
            fields['VIN'].upper(), token_path)

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
        except AuthenticationOTPRequired:
            return jsonify(error='Kia requires a one-time verification code', code='AuthenticationOTPRequired'), 401
        except ConsentRequiredError:
            return jsonify(error='Kia Connect terms or consent must be accepted for this account', code='ConsentRequiredError'), 401
        except AuthenticationError:
            return jsonify(error='Kia authentication failed; verify the account email, password, PIN, and Kia Connect access', code='AuthenticationError'), 401
        except LookupError:
            return jsonify(error='Configured VIN not found uniquely on this account'), 422
        except Exception as error:
            app.logger.warning('Vehicle discovery failed for %s: %s', alias, type(error).__name__)
            return jsonify(error='Kia vehicle discovery failed', code=type(error).__name__), 502
        finally:
            account.lock.release()

    @app.post('/vehicles/<alias>/otp/send')
    def send_otp(alias):
        account = accounts.get(alias)
        if account is None:
            return jsonify(error='Unknown vehicle'), 404
        request_data = request.get_json(silent=True) or {}
        if set(request_data) != {'channel'} or request_data['channel'] not in {'email', 'sms'}:
            return jsonify(error='Provide exactly one channel: email or sms'), 400
        if not account.lock.acquire(blocking=False):
            return jsonify(error='Account busy'), 409
        try:
            try:
                account.manager.check_and_refresh_token()
            except AuthenticationOTPRequired:
                account.manager.send_otp(OTP_NOTIFY_TYPE(request_data['channel'].upper()))
                return jsonify(status='otp_sent', vehicle=alias, channel=request_data['channel']), 202
            return jsonify(status='already_authenticated', vehicle=alias), 200
        except AuthenticationError:
            return jsonify(error='Kia authentication failed; verify the account email, password, PIN, and Kia Connect access', code='AuthenticationError'), 401
        except Exception as error:
            app.logger.warning('OTP request failed for %s: %s', alias, type(error).__name__)
            return jsonify(error='Kia OTP request failed', code=type(error).__name__), 502
        finally:
            account.lock.release()

    @app.post('/vehicles/<alias>/otp/verify')
    def verify_otp(alias):
        account = accounts.get(alias)
        if account is None:
            return jsonify(error='Unknown vehicle'), 404
        request_data = request.get_json(silent=True) or {}
        code = request_data.get('code') if set(request_data) == {'code'} else None
        if not isinstance(code, str) or not code.isdigit() or not 4 <= len(code) <= 10:
            return jsonify(error='Provide a numeric one-time code of 4 to 10 digits'), 400
        if not account.lock.acquire(blocking=False):
            return jsonify(error='Account busy'), 409
        try:
            if account.manager.otp_request is None:
                return jsonify(error='Request a new one-time code before verifying'), 409
            account.manager.verify_otp_and_complete_login(code)
            save_token(account)
            return jsonify(status='authenticated', vehicle=alias), 200
        except AuthenticationError:
            return jsonify(error='Kia rejected the one-time code', code='AuthenticationError'), 401
        except Exception as error:
            app.logger.warning('OTP verification failed for %s: %s', alias, type(error).__name__)
            return jsonify(error='Kia OTP verification failed', code=type(error).__name__), 502
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
        except AuthenticationOTPRequired:
            return jsonify(error='Kia requires a one-time verification code; command not sent', code='AuthenticationOTPRequired'), 401
        except ConsentRequiredError:
            return jsonify(error='Kia Connect terms or consent must be accepted; command not sent', code='ConsentRequiredError'), 401
        except AuthenticationError:
            return jsonify(error='Kia authentication failed; command not sent', code='AuthenticationError'), 401
        except LookupError:
            return jsonify(error='Configured VIN not found uniquely; command not sent'), 422
        except Exception as error:
            app.logger.warning('Kia command failed for %s/%s: %s', alias, action, type(error).__name__)
            return jsonify(error='Kia request failed; completion is unknown. Check the Kia app before retrying.', code=type(error).__name__), 502
        finally:
            account.lock.release()

    return app
