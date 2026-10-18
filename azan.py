#!/usr/bin/env python3
"""Spotify Azan pauser. Run `python azan.py auth` once, then `python azan.py run`."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo('Africa/Cairo')
PRAYERS = ('Fajr', 'Dhuhr', 'Asr', 'Maghrib', 'Isha')
ROOT = Path(os.environ.get('AZAN_STATE_DIR', str(Path(__file__).resolve().parent)))
TOKEN_FILE = ROOT / 'tokens.json'
STATE_FILE = ROOT / 'state.json'
CACHE_FILE = ROOT / 'prayers.json'
REDIRECT = 'http://127.0.0.1:8888/callback'
LOG = logging.getLogger('spotify-azan')
SESSION = requests.Session()


def atomic_json(path, data):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    temp = ROOT / ('.' + path.name + '.' + secrets.token_hex(5))
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def load(path, default=None):
    try:
        with path.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def client_id():
    value = os.environ.get('SPOTIFY_CLIENT_ID', '').strip()
    if not value:
        raise RuntimeError('Set SPOTIFY_CLIENT_ID')
    return value


def token_request(data):
    resp = SESSION.post('https://accounts.spotify.com/api/token', data=data, timeout=20)
    resp.raise_for_status()
    try:
        return resp.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError(f'Spotify token endpoint returned invalid JSON (HTTP {resp.status_code}): {resp.text[:200]!r}') from exc


def save_tokens(new, previous=None):
    previous = previous or {}
    if not new.get('refresh_token'):
        new['refresh_token'] = previous.get('refresh_token')
    if not new.get('refresh_token'):
        raise RuntimeError('Spotify did not provide a refresh token')
    new['expires_at'] = time.time() + int(new.get('expires_in', 3600)) - 90
    atomic_json(TOKEN_FILE, new)
    return new


def access_token():
    tok = load(TOKEN_FILE)
    if not tok:
        raise RuntimeError('No token file. Run auth first.')
    if time.time() >= tok.get('expires_at', 0):
        new = token_request({'grant_type': 'refresh_token', 'refresh_token': tok['refresh_token'], 'client_id': client_id()})
        tok = save_tokens(new, tok)
    return tok['access_token']


def spotify(method, path):
    for attempt in range(2):
        resp = SESSION.request(method, 'https://api.spotify.com/v1/' + path, headers={'Authorization': 'Bearer ' + access_token()}, timeout=20)
        if resp.status_code == 401 and attempt == 0:
            tok = load(TOKEN_FILE)
            new = token_request({'grant_type': 'refresh_token', 'refresh_token': tok['refresh_token'], 'client_id': client_id()})
            save_tokens(new, tok)
            continue
        if resp.status_code == 429:
            raise RuntimeError('Spotify rate limit: retry later')
        if resp.status_code not in (200, 204):
            raise RuntimeError(f'Spotify HTTP {resp.status_code}: {resp.text[:200]}')
        # Playback commands can succeed with an empty or non-JSON response body.
        # Only the GET playback-state endpoint needs a JSON response.
        if method.upper() != 'GET' or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise RuntimeError(
                f'Spotify returned invalid JSON for {method} {path} '
                f'(HTTP {resp.status_code}, content-type {resp.headers.get("Content-Type", "unknown")}): '
                f'{resp.text[:200]!r}'
            ) from exc


def auth():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(24)
    params = {'response_type': 'code', 'client_id': client_id(), 'redirect_uri': REDIRECT,
              'scope': 'user-read-playback-state user-modify-playback-state',
              'code_challenge_method': 'S256', 'code_challenge': challenge, 'state': state}
    url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode(params)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != '/callback' or query.get('state', [''])[0] != state:
                self.send_error(400, 'Invalid OAuth callback')
                return
            result['code'] = query.get('code', [None])[0]
            result['error'] = query.get('error', [None])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'Authorization received. Return to your terminal.')
        def log_message(self, *_):
            pass

    print('Open this URL in a browser on the machine running auth:\n' + url, flush=True)
    server = HTTPServer(('127.0.0.1', 8888), Callback)
    server.timeout = 180
    server.handle_request()
    server.server_close()
    if not result.get('code'):
        raise RuntimeError('Authorization failed or timed out: ' + str(result.get('error')))
    tokens = token_request({'grant_type': 'authorization_code', 'code': result['code'],
                            'redirect_uri': REDIRECT, 'client_id': client_id(), 'code_verifier': verifier})
    save_tokens(tokens)
    print('Authorized successfully; tokens saved with mode 0600.')


def prayer_times(day):
    key = day.isoformat()
    cache = load(CACHE_FILE, {})
    if key in cache:
        return cache[key]
    url = 'https://api.aladhan.com/v1/timingsByCity/' + day.strftime('%d-%m-%Y')
    response = SESSION.get(url, params={'city': 'Cairo', 'country': 'Egypt', 'method': 5,
                                        'timezonestring': 'Africa/Cairo'}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != 200:
        raise RuntimeError('Prayer API returned failure')
    timings = payload['data']['timings']
    result = {}
    for prayer in PRAYERS:
        match = re.match(r'^(\d{2}):(\d{2})', timings[prayer])
        if not match:
            raise ValueError('Unexpected prayer time for ' + prayer)
        hh, mm = map(int, match.groups())
        dt.time(hh, mm)
        result[prayer] = f'{hh:02d}:{mm:02d}'
    # Keep recent days for offline restart / transient API failures.
    cache = {k: v for k, v in cache.items() if k >= (day - dt.timedelta(days=2)).isoformat()}
    cache[key] = result
    atomic_json(CACHE_FILE, cache)
    return result


def pause_if_playing():
    player = spotify('GET', 'me/player')
    if not player or not player.get('is_playing'):
        LOG.info('Spotify is not playing; no action needed')
        return False
    spotify('PUT', 'me/player/pause')
    LOG.info('Spotify pause requested successfully')
    return True


def send_whatsapp_message(message):
    recipient = os.environ.get('WHATSAPP_TO', '').strip()
    api_key = os.environ.get('WHATSAPP_API_KEY', '').strip()
    password = os.environ.get('WHATSAPP_PASSWORD', '')

    if not recipient or not api_key:
        LOG.warning('WhatsApp notification skipped; set WHATSAPP_API_KEY and WHATSAPP_TO to enable')
        return
    try:
        response = SESSION.post(
            os.environ.get('WHATSAPP_API_URL', 'https://whatsapp.spysnet.com/sendText'),
            headers={
                'accept-language': 'en-US,en;q=0.9',
                'content-type': 'application/json',
                'x-api-key': api_key,
            },
            json={'to': recipient, 'content': message, 'passcode': password},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException:
        LOG.exception('WhatsApp notification failed')


def test_pause():
    if pause_if_playing():
        send_whatsapp_message('Spotify paused for azan "Test"')
        LOG.info('Resuming Spotify in 5 seconds')
        time.sleep(5)
        spotify('PUT', 'me/player/play')
        LOG.info('Spotify resume requested successfully')
        send_whatsapp_message('Spotify resumed')


def run():
    LOG.info('Scheduler started: Cairo, Egyptian Survey method 5, pause 1 minute before prayer and resume 2 minutes after')
    while True:
        now = dt.datetime.now(TZ)
        day = now.date()
        try:
            timings = prayer_times(day)
            state = load(STATE_FILE, {'completed': [], 'paused': []})
            completed = set(state.get('completed', []))
            paused = set(state.get('paused', []))
            state_changed = False
            for name, clock in timings.items():
                key = f'{day.isoformat()}:{name}'
                scheduled = dt.datetime.combine(day, dt.time.fromisoformat(clock), TZ)
                pause_at = scheduled - dt.timedelta(minutes=1)
                resume_at = scheduled + dt.timedelta(minutes=2)

                # Do not pause late if the service missed the one-minute lead-in.
                if key not in completed and key not in paused and pause_at <= now < scheduled:
                    try:
                        did_pause = pause_if_playing()
                    except Exception:
                        LOG.exception('Pause attempt failed for %s; retrying before prayer time', name)
                        continue
                    if did_pause:
                        paused.add(key)
                        state_changed = True
                        LOG.info('Paused for %s; scheduled prayer time is %s Cairo time', name, clock)
                        send_whatsapp_message(f'Spotify paused for azan "{name}"')
                    else:
                        completed.add(key)
                        state_changed = True

                if key in paused and now >= resume_at:
                    try:
                        spotify('PUT', 'me/player/play')
                    except Exception:
                        LOG.exception('Resume attempt failed for %s; will retry', name)
                        continue
                    paused.remove(key)
                    completed.add(key)
                    state_changed = True
                    LOG.info('Resumed after %s', name)
                    send_whatsapp_message('Spotify resumed')
                elif key not in completed and key not in paused and now >= resume_at:
                    # The service missed the pause window, or Spotify was idle.
                    completed.add(key)
                    state_changed = True

            if state_changed:
                atomic_json(STATE_FILE, {
                    'completed': sorted(k for k in completed if k[:10] >= (day - dt.timedelta(days=1)).isoformat()),
                    'paused': sorted(k for k in paused if k[:10] >= (day - dt.timedelta(days=1)).isoformat()),
                })
        except Exception:
            LOG.exception('Scheduler iteration failed')
        time.sleep(5)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('auth', 'run', 'times', 'test-pause'))
    args = parser.parse_args()
    if args.command == 'auth':
        auth()
    elif args.command == 'run':
        run()
    elif args.command == 'times':
        print(json.dumps(prayer_times(dt.datetime.now(TZ).date()), indent=2))
    elif args.command == 'test-pause':
        test_pause()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, requests.RequestException) as exc:
        LOG.error('%s', exc)
        sys.exit(1)
