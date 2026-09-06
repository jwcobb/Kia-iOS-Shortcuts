# Kia Shortcuts on Laravel Forge

Adapted from https://github.com/EwahOuon/Kia-iOS-Shortcuts (MIT; original LICENSE retained).
Python/Flask API for two explicit vehicle aliases, each with its own primary Kia Connect account. No Laravel application or database is required.

## Local setup

Use Python 3.11 or newer (local tests used Python 3.14).

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
chmod 600 .env
```

Set all values in `.env`; generate SECRET_KEY with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`. Do not commit credentials, VINs, or account emails. PINs are strings, retaining leading zeros. Start with each vehicle's primary account; shared-driver access through the unofficial API has not been verified.

```sh
.venv/bin/python -m pytest -q
./deploy/start.sh
```

## Forge deployment

1. Target: Forge server `laravel-vps-1` (`134.199.176.190`), hostname `kia.jcobb.org`. Create a site connected to your own repository containing this adaptation. Disable zero-downtime deployments for this initial setup so the application directory and virtual environment have stable paths. Disable Composer/NPM steps; this is Python. Use an empty `public` directory as the document root.
2. Ensure Python 3.11+ and its `venv` package are installed on the VPS. Under the site's operating-system user, run `python3 -m venv .venv` and `.venv/bin/pip install -r requirements.txt` in the checkout. Do not use the system-wide pip environment.
3. Create `.env` in the checkout with mode 600 and fill the account credentials, VINs, and SECRET_KEY. The Python service loads this file itself. A private local `.env` may already contain vehicle/account mappings, but passwords and PINs still need filling.
4. In Forge's Processes tab, add a Custom background process:
   - Command: `/home/forge/kia.jcobb.org/deploy/start.sh` (adjust for the actual site user/path).
   - Working directory: `/home/forge/kia.jcobb.org`.
   - User: the site user; processes: **1**; start seconds: 5; stop seconds: 130; stop signal: TERM.
   - The script uses one Gunicorn worker and four threads. Keep one process/worker: account locks and the 10-second command cooldown are in memory.
5. Configure HTTPS in Forge. Replace the site's existing Nginx `location /` with `deploy/nginx-location.conf`; retain Forge SSL/ACME and dotfile protections. Remove unused PHP routing. Validate with `sudo nginx -t` before reloading. The API listens only on `127.0.0.1:8081`; do not open port 8081 publicly. Confirm that port is unused first, or change it in both files.
6. If using Cloudflare, use Full (strict) TLS and make sure API requests can reach this hostname without browser challenges. Keep bearer authentication enabled in the app.
7. Verify `/healthz`, then authenticated GET `/vehicles/telluride` and `/vehicles/ev9`. Each GET authenticates to Kia and returns only the exact VIN-matched vehicle's identity. It does not send a vehicle command. Confirm returned identities before configuring shortcuts.
8. Deploy updates by pulling your chosen branch, installing `requirements.txt`, then restarting this background process through Forge. An example script is in `deploy/deploy.sh.example`; replace its path, branch, and daemon ID before using it.

Forge process reference: https://laravel.com/forge/docs/resources/background-processes

## iPhone shortcuts

Create one shortcut per car/action using Get Contents of URL:

- URL: `https://kia.jcobb.org/vehicles/telluride/lock_car` (replace alias with `ev9` for the other car).
- Method: POST; leave the request body empty.
- Header: `Authorization: Bearer YOUR_SECRET_KEY`.
- Actions: `lock_car`, `unlock_car`, `start_climate`, `stop_climate`.
- Climate defaults follow the original project: 72 F for 10 minutes. Actual vehicle support requires live testing.
- Show the response with Show Result.

A 202 response means submitted, not confirmed executed. Check the Kia app for actual state. A timeout or 502 can mean an uncertain outcome: do not automatically retry vehicle commands. A 409 means that account is busy; 429 means the 10-second cooldown is active. Missing/duplicate VIN matches fail closed. GET never operates a vehicle.

## One-time Kia verification

If `GET /vehicles/<alias>` returns `AuthenticationOTPRequired`, complete Kia verification from a Forge terminal before creating any shortcut. These setup endpoints require the same bearer secret, hold the short-lived code only in process memory, and save the resulting session token under `storage/tokens/` with owner-only permissions. Do not use them in iPhone Shortcuts.

```sh
cd /home/forge/kia.jcobb.org
SECRET_KEY=$(.venv/bin/python -c 'from dotenv import dotenv_values; print(dotenv_values(".env")["SECRET_KEY"])')
curl -sS -X POST -H "Authorization: Bearer $SECRET_KEY" -H 'Content-Type: application/json' \
  --data '{"channel":"email"}' http://127.0.0.1:8081/vehicles/telluride/otp/send
# Read the email, then enter its code in place of 123456. Do not save it in shell history.
read -rs OTP_CODE; printf '\n'
curl -sS -X POST -H "Authorization: Bearer $SECRET_KEY" -H 'Content-Type: application/json' \
  --data "{\"code\":\"$OTP_CODE\"}" http://127.0.0.1:8081/vehicles/telluride/otp/verify
unset OTP_CODE SECRET_KEY
```

Repeat for `ev9`. Use `"sms"` only if that account offers SMS verification. After each 200 response, verify it with `GET /vehicles/<alias>`. Treat `storage/tokens/*.json` as credentials: do not commit, download, or share those files.

## Known limits and validation

This is an unofficial Kia integration. Both accounts' live login, MFA, API vehicle discovery, climate support and command execution still require verification. No live car commands were sent during development. The service does not implement an OTP entry flow; if Kia demands MFA that the library cannot complete, stop and resolve authentication before using shortcuts. Opening the Kia app is not guaranteed to resolve API authentication.

Account sessions live in memory and are lost on restart. There is no automatic command retry, token persistence, scheduler or vehicle polling. The health endpoint checks only the service process. Mock tests cover account/vehicle routing and failure behavior, not Kia availability. Logs omit upstream exception messages and library debug output to avoid leaking credentials or vehicle state.
