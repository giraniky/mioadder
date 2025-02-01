import os
import sys
import json
import threading
import time
import datetime
import asyncio
import sqlite3
import subprocess

from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename

from telethon.sync import TelegramClient, errors
from telethon import tl
from telethon.tl.types import (
    UserStatusEmpty, UserStatusLastMonth, UserStatusLastWeek,
    UserStatusRecently, UserStatusOffline, UserStatusOnline
)
from telethon.tl.functions.channels import (
    InviteToChannelRequest, GetParticipantRequest, JoinChannelRequest
)
from telethon.errors import rpcerrorlist

import openpyxl  # Per Excel

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

PHONES_FILE = 'phones.json'
SESSIONS_FOLDER = 'sessions'
LOG_STATUS_FILE = 'add_session.json'
os.makedirs(SESSIONS_FOLDER, exist_ok=True)

# Dizionario in memoria per OTP
OTP_DICT = {}

# Stato dell'operazione di aggiunta
ADD_SESSION = {}

LOCK = threading.Lock()


# ------------------------ FUNZIONI DI SUPPORTO ------------------------

def load_phones():
    with LOCK:
        if not os.path.exists(PHONES_FILE):
            return []
        try:
            with open(PHONES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for p in data:
                if 'total_added' not in p:
                    p['total_added'] = 0
                if 'added_today' not in p:
                    p['added_today'] = 0
                if 'last_reset_date' not in p:
                    p['last_reset_date'] = datetime.date.today().isoformat()
                if 'paused_until' not in p:
                    p['paused_until'] = None
                if 'paused' not in p:
                    p['paused'] = False
                if 'non_result_errors' not in p:
                    p['non_result_errors'] = 0

                # Se la pausa è scaduta, sblocca
                if p['paused_until']:
                    paused_dt = datetime.datetime.fromisoformat(p['paused_until'])
                    if datetime.datetime.now() >= paused_dt:
                        p['paused'] = False
                        p['paused_until'] = None
            return data
        except Exception as e:
            print("Errore in load_phones:", e)
            return []

def save_phones(phones):
    with LOCK:
        with open(PHONES_FILE, 'w', encoding='utf-8') as f:
            json.dump(phones, f, indent=2, ensure_ascii=False)

def reset_daily_counters_if_needed(phone_entry):
    today_str = datetime.date.today().isoformat()
    if phone_entry.get('last_reset_date') != today_str:
        phone_entry['added_today'] = 0
        phone_entry['last_reset_date'] = today_str

def load_add_session():
    with LOCK:
        if not os.path.exists(LOG_STATUS_FILE):
            return {}
        try:
            with open(LOG_STATUS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print("Errore in load_add_session:", e)
            return {}

def save_add_session():
    with LOCK:
        with open(LOG_STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(ADD_SESSION, f, indent=2, ensure_ascii=False)

def create_telegram_client(phone_entry):
    session_file = os.path.join(SESSIONS_FOLDER, f"{phone_entry['phone']}.session")
    api_id = int(phone_entry['api_id'])
    api_hash = phone_entry['api_hash']
    return TelegramClient(session_file, api_id, api_hash)

# Wrapper per funzioni come client.get_entity
def safe_telethon_call(func, *args, max_retries=5, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower():
                time.sleep(2)
            else:
                raise
    return func(*args, **kwargs)

# Wrapper per invocare richieste TL (es. GetParticipantRequest)
def safe_invoke_request(client, request_cls, *args, max_retries=5, **kwargs):
    for attempt in range(max_retries):
        try:
            return client(request_cls(*args, **kwargs))
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower():
                time.sleep(2)
            else:
                raise
    return client(request_cls(*args, **kwargs))

def count_available_phones(phones):
    now = datetime.datetime.now()
    available = 0
    for p in phones:
        reset_daily_counters_if_needed(p)
        if p['paused']:
            if p['paused_until']:
                paused_dt = datetime.datetime.fromisoformat(p['paused_until'])
                if now >= paused_dt:
                    p['paused'] = False
                    p['paused_until'] = None
                    save_phones(phones)
                else:
                    continue
            else:
                continue
        session_path = os.path.join(SESSIONS_FOLDER, f"{p['phone']}.session")
        if not os.path.isfile(session_path):
            continue
        if p['added_today'] >= 45:
            continue
        flood_time = 0
        if p['paused_until']:
            paused_dt = datetime.datetime.fromisoformat(p['paused_until'])
            delta = (paused_dt - now).total_seconds()
            flood_time = max(int(delta), 0)
        if flood_time > 0:
            continue
        available += 1
    return available

def suspend_until_enough_phones(min_phones, phones, username):
    global ADD_SESSION
    if ADD_SESSION.get('running', False):
        msg = f"Nessun phone disponibile per {username}, attendo almeno {min_phones} numeri liberi."
        ADD_SESSION['log'].append(msg)
        save_add_session()
    while True:
        if not ADD_SESSION.get('running', False):
            return
        if count_available_phones(phones) >= min_phones:
            msg2 = f"Numeri sufficienti disponibili per {username}, riprendo."
            ADD_SESSION['log'].append(msg2)
            save_add_session()
            return
        time.sleep(20)

def update_phone_stats(phone_number, added=0, total=0, non_result_err_inc=0):
    phones = load_phones()
    for p in phones:
        if p['phone'] == phone_number:
            reset_daily_counters_if_needed(p)
            if added:
                p['added_today'] += added
            if total:
                p['total_added'] += total
            if non_result_err_inc:
                p['non_result_errors'] += non_result_err_inc
            save_phones(phones)
            return

def set_phone_pause(phone_number, paused=True, seconds=0, days=0):
    phones = load_phones()
    for p in phones:
        if p['phone'] == phone_number:
            p['paused'] = paused
            if paused:
                if seconds:
                    dt = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
                    p['paused_until'] = dt.isoformat()
                elif days:
                    dt = datetime.datetime.now() + datetime.timedelta(days=days)
                    p['paused_until'] = dt.isoformat()
                else:
                    p['paused_until'] = None
            else:
                p['paused_until'] = None
            save_phones(phones)
            return

def should_skip_user_by_last_seen(user_entity, skip_config):
    status = user_entity.status
    now = datetime.datetime.now()
    if not status:
        return skip_config.get('user_status_empty', False)
    if isinstance(status, UserStatusOffline):
        days_since = (now - status.was_online.replace(tzinfo=None)).days
        if skip_config.get('last_seen_gt_1_day') and days_since > 1:
            return True
        if skip_config.get('last_seen_gt_7_days') and days_since > 7:
            return True
        if skip_config.get('last_seen_gt_30_days') and days_since > 30:
            return True
        if skip_config.get('last_seen_gt_60_days') and days_since > 60:
            return True
    if isinstance(status, UserStatusEmpty) and skip_config.get('user_status_empty'):
        return True
    if isinstance(status, UserStatusLastMonth) and skip_config.get('last_seen_gt_30_days'):
        return True
    if isinstance(status, UserStatusLastWeek) and skip_config.get('last_seen_gt_7_days'):
        return True
    if isinstance(status, UserStatusRecently) and skip_config.get('last_seen_gt_1_day'):
        return True
    return False

def safe_telethon_connect(client, max_retries=5):
    for attempt in range(max_retries):
        try:
            client.connect()
            return True
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower():
                time.sleep(2)
            else:
                raise
    return False

# ---------------------------- ROTTE FRONTEND ----------------------------

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

# --- Gestione Numeri ---
@app.route('/api/phones', methods=['GET'])
def api_list_phones():
    phones = load_phones()
    for p in phones:
        reset_daily_counters_if_needed(p)
    save_phones(phones)
    return jsonify(phones)

@app.route('/api/phones', methods=['POST'])
def api_add_phone():
    data = request.json
    phone = data['phone']
    api_id = data['api_id']
    api_hash = data['api_hash']
    phones = load_phones()
    for p in phones:
        if p['phone'] == phone:
            return jsonify({'error': 'Phone already exists'}), 400
    new_phone_entry = {
        'phone': phone,
        'api_id': api_id,
        'api_hash': api_hash,
        'paused': False,
        'paused_until': None,
        'added_today': 0,
        'last_reset_date': datetime.date.today().isoformat(),
        'total_added': 0,
        'non_result_errors': 0
    }
    phones.append(new_phone_entry)
    save_phones(phones)
    return jsonify({'success': True})

@app.route('/api/phones/<phone>', methods=['DELETE'])
def api_remove_phone(phone):
    phones = load_phones()
    phones = [p for p in phones if p['phone'] != phone]
    save_phones(phones)
    return jsonify({'success': True})

@app.route('/api/phones/<phone>/pause', methods=['POST'])
def api_pause_phone(phone):
    data = request.json
    pause_state = data.get('paused', True)
    phones = load_phones()
    for p in phones:
        if p['phone'] == phone:
            p['paused'] = pause_state
            if not pause_state:
                p['paused_until'] = None
            save_phones(phones)
            return jsonify({'success': True})
    return jsonify({'error': 'Phone not found'}), 404

# --- Login e OTP ---
@app.route('/api/send_code', methods=['POST'])
def api_send_code():
    data = request.json
    phone = data['phone']
    phones = load_phones()
    phone_entry = next((p for p in phones if p['phone'] == phone), None)
    if not phone_entry:
        return jsonify({'error': 'Phone not found'}), 404
    client = create_telegram_client(phone_entry)
    if not safe_telethon_connect(client):
        return jsonify({'error': 'Impossibile connettersi (db locked).'}), 500
    try:
        sent = client.send_code_request(phone, force_sms=True)
        OTP_DICT[phone] = {
            'phone_code_hash': sent.phone_code_hash,
            '2fa_needed': False
        }
        return jsonify({'success': True})
    except errors.PhoneNumberBannedError:
        return jsonify({'error': 'Numero bannato da Telegram.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        client.disconnect()

@app.route('/api/validate_code', methods=['POST'])
def api_validate_code():
    data = request.json
    phone = data['phone']
    code = data['code']
    if phone not in OTP_DICT:
        return jsonify({'error': 'Nessuna sessione OTP per questo numero'}), 400
    phones = load_phones()
    phone_entry = next((p for p in phones if p['phone'] == phone), None)
    if not phone_entry:
        return jsonify({'error': 'Phone not found'}), 404
    phone_code_hash = OTP_DICT[phone]['phone_code_hash']
    client = create_telegram_client(phone_entry)
    if not safe_telethon_connect(client):
        return jsonify({'error': 'Impossibile connettersi (db locked).'}), 500
    try:
        client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        del OTP_DICT[phone]
        return jsonify({'success': True})
    except errors.SessionPasswordNeededError:
        OTP_DICT[phone]['2fa_needed'] = True
        save_add_session()
        return jsonify({'error': 'SESSION_PASSWORD_NEEDED'}), 400
    except errors.PhoneCodeInvalidError:
        return jsonify({'error': 'Codice OTP non valido.'}), 400
    except errors.PhoneCodeExpiredError:
        return jsonify({'error': 'Codice OTP scaduto.'}), 400
    except errors.PhoneNumberUnoccupiedError:
        return jsonify({'error': 'Numero non associato a un account.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        client.disconnect()

@app.route('/api/validate_password', methods=['POST'])
def api_validate_password():
    data = request.json
    phone = data['phone']
    password = data['password']
    if phone not in OTP_DICT:
        return jsonify({'error': 'Nessuna sessione OTP per questo numero'}), 400
    if not OTP_DICT[phone].get('2fa_needed'):
        return jsonify({'error': '2FA non richiesta per questo numero'}), 400
    phones = load_phones()
    phone_entry = next((p for p in phones if p['phone'] == phone), None)
    if not phone_entry:
        return jsonify({'error': 'Phone not found'}), 404
    client = create_telegram_client(phone_entry)
    if not safe_telethon_connect(client):
        return jsonify({'error': 'Impossibile connettersi (db locked).'}), 500
    try:
        client.sign_in(password=password)
        del OTP_DICT[phone]
        return jsonify({'success': True})
    except errors.PasswordHashInvalidError:
        return jsonify({'error': 'Password 2FA non valida.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        client.disconnect()

# --- Aggiunta Utenti al Gruppo ---

def add_users_to_group_thread(group_username, users_list):
    # ... (stessa logica di gestione dell'aggiunta)
    # ... (vedi la versione precedente con safe_telethon_call, safe_invoke_request)

    # Qui metti la logica come nella risposta precedente
    pass


# SEMPLIFICHIAMO QUI PER BREVITÀ. Riprendi la logica che avevi già, 
# con "safe_telethon_call(client.get_entity, group_username)", 
# "safe_invoke_request(client, GetParticipantRequest, ...)", ecc.


# Esempio placeholder:
def add_users_to_group_thread(group_username, users_list):
    global ADD_SESSION
    ADD_SESSION['running'] = True
    ADD_SESSION['group'] = group_username
    ADD_SESSION['total_added'] = 0
    ADD_SESSION['log'] = []
    ADD_SESSION['start_time'] = time.time()
    if 'last_user_index' not in ADD_SESSION:
        ADD_SESSION['last_user_index'] = 0

    # ... caricamento phones, connessione, ecc. ...
    # ... come nelle versioni precedenti ...

    ADD_SESSION['running'] = False
    ADD_SESSION['log'].append("Operazione di aggiunta terminata.")
    save_add_session()

@app.route('/api/start_adding', methods=['POST'])
def api_start_adding():
    global ADD_SESSION
    if ADD_SESSION.get('running', False):
        return jsonify({'error': "Un'operazione di aggiunta è già in corso."}), 400

    data = request.json
    group_username = data.get('group_username', '').strip()
    user_list_raw = data.get('user_list', '').strip()
    users_list = [u.strip() for u in user_list_raw.splitlines() if u.strip()]

    if not group_username or not users_list:
        return jsonify({'error': 'Dati insufficienti (gruppo o lista utenti vuota).'}), 400

    # Altri parametri di configurazione:
    ADD_SESSION['min_phones_available'] = data.get('min_phones_available', 1)
    ADD_SESSION['max_non_result_errors'] = data.get('max_non_result_errors', 3)
    ADD_SESSION['days_pause_non_result_errors'] = data.get('days_pause_non_result_errors', 2)
    ADD_SESSION['sleep_seconds'] = data.get('sleep_seconds', 10)

    skip_options = data.get('skip_options', [])
    ADD_SESSION['last_seen_gt_1_day'] = 'last_seen_gt_1_day' in skip_options
    ADD_SESSION['last_seen_gt_7_days'] = 'last_seen_gt_7_days' in skip_options
    ADD_SESSION['last_seen_gt_30_days'] = 'last_seen_gt_30_days' in skip_options
    ADD_SESSION['last_seen_gt_60_days'] = 'last_seen_gt_60_days' in skip_options
    ADD_SESSION['user_status_empty'] = 'user_status_empty' in skip_options

    # Imposta flag
    ADD_SESSION['running'] = True
    ADD_SESSION['group'] = group_username
    ADD_SESSION['total_added'] = 0
    ADD_SESSION['log'] = []
    ADD_SESSION['start_time'] = time.time()
    if 'last_user_index' not in ADD_SESSION:
        ADD_SESSION['last_user_index'] = 0

    save_add_session()

    t = threading.Thread(target=add_users_to_group_thread, args=(group_username, users_list))
    t.start()

    return jsonify({'success': True})

@app.route('/api/log_status', methods=['GET'])
def api_log_status():
    return jsonify({
        'running': ADD_SESSION.get('running', False),
        'group': ADD_SESSION.get('group', ''),
        'total_added': ADD_SESSION.get('total_added', 0),
        'log': ADD_SESSION.get('log', []),
    })

@app.route('/api/stop_adding', methods=['POST'])
def api_stop_adding():
    global ADD_SESSION
    if ADD_SESSION.get('running', False):
        ADD_SESSION['running'] = False
        ADD_SESSION['log'].append("Operazione fermata manualmente dall'utente.")
        save_add_session()
        return jsonify({"success": True, "message": "Operazione fermata con successo."})
    else:
        return jsonify({"success": False, "message": "Nessuna operazione in corso."}), 400

# --- Riepilogo ---
@app.route('/api/summary', methods=['GET'])
def api_summary():
    phones = load_phones()
    now = datetime.datetime.now()
    summary_list = []
    for p in phones:
        flood_time = 0
        if p['paused_until']:
            paused_dt = datetime.datetime.fromisoformat(p['paused_until'])
            delta = (paused_dt - now).total_seconds()
            flood_time = max(int(delta), 0)
        summary_list.append({
            'phone': p['phone'],
            'added_today': p['added_today'],
            'paused': p['paused'],
            'flood_time': flood_time,
            'total_added': p['total_added']
        })
    session_total = ADD_SESSION.get('total_added', 0)
    return jsonify({
        'session_added_total': session_total,
        'phones': summary_list
    })

# --- Caricamento Excel ---
@app.route('/api/upload_excel', methods=['POST'])
def upload_excel():
    if 'excel_file' not in request.files:
        return jsonify({'error': 'Nessun file Excel caricato.'}), 400
    file = request.files['excel_file']
    if file.filename == '':
        return jsonify({'error': 'Nessun file selezionato.'}), 400
    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()
    valid_exts = ['.xlsx', '.xlsm', '.xltx', '.xltm']
    if ext not in valid_exts:
        return jsonify({'error': f'Formato non supportato. Estensioni: {valid_exts}'}), 400
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    try:
        wb = openpyxl.load_workbook(filepath, read_only=True)
        sheet = wb.active
        usernames = []
        for row in sheet.iter_rows(min_row=1, min_col=1, max_col=1, values_only=True):
            cell_value = row[0]
            if cell_value:
                s = str(cell_value).strip()
                if not s.startswith('@'):
                    s = '@' + s
                usernames.append(s)
        wb.close()
        os.remove(filepath)
    except Exception as e:
        return jsonify({'error': f'Errore lettura Excel: {str(e)}'}), 400
    return jsonify({'user_list': '\n'.join(usernames)})

# --- Riavvio TMUX ---


def restart_tmux_thread(app_path):
    # Attende qualche secondo per permettere all'endpoint REST di rispondere
    time.sleep(2)
    # 1) Avvia una nuova sessione (nome: mioadder_new)
    cmd_new = [
        "sudo", "tmux", "new-session", "-d", "-s", "mioadder_new",
        sys.executable, app_path
    ]
    subprocess.run(cmd_new, check=True)

    # 2) Dopo 5 secondi, kill della vecchia sessione "mioadder"
    cmd_kill = "sleep 5; sudo tmux kill-session -t mioadder"
    subprocess.Popen(cmd_kill, shell=True)


@app.route('/api/restart_tmux', methods=['POST'])
def api_restart_tmux():
    try:
        # Percorso a app.py (MODIFICALO se serve)
        app_path = "/root/mioadder/app.py"
        threading.Thread(target=restart_tmux_thread, args=(app_path,), daemon=True).start()
        return jsonify({"success": True, "message": "Riavvio della sessione TMUX avviato con successo."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5050, debug=True, threaded=False, use_reloader=False)
