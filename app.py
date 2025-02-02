import os
import sys
import json
import threading
import time
import datetime
import asyncio
import sqlite3
import subprocess
import logging

from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename

# Telethon
from telethon.sync import TelegramClient
from telethon import tl, errors
from telethon.tl.types import (
    UserStatusEmpty, UserStatusLastMonth, UserStatusLastWeek,
    UserStatusRecently, UserStatusOffline, UserStatusOnline
)
from telethon.tl.functions.channels import (
    InviteToChannelRequest, GetParticipantRequest, JoinChannelRequest
)
from telethon.errors import (
    rpcerrorlist,
    FloodWaitError,
    PeerFloodError,
    UsernameNotOccupiedError,
    RPCError
)

import openpyxl  # Per la gestione dei file Excel

# ---------------------- LOGGING -----------------------
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)s %(threadName)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("app.log", mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# ---------------------- CONFIG ------------------------
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

PHONES_FILE = 'phones.json'
SESSIONS_FOLDER = 'sessions'
LOG_STATUS_FILE = 'add_session.json'
os.makedirs(SESSIONS_FOLDER, exist_ok=True)

OTP_DICT = {}
ADD_SESSION = {}  # Stato globale dell’operazione di aggiunta
LOCK = threading.Lock()

# ---------------------- TELETHON SESSION -------------
from telethon.sessions import SQLiteSession

class CustomSQLiteSession(SQLiteSession):
    def _connect(self):
        self._conn = sqlite3.connect(self.session_name, timeout=120, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()
        logger.debug(f"SQLiteSession per {self.session_name} impostata in modalità WAL.")

def create_telegram_client(phone_entry):
    session_file = os.path.join(SESSIONS_FOLDER, f"{phone_entry['phone']}.session")
    api_id = int(phone_entry['api_id'])
    api_hash = phone_entry['api_hash']
    custom_session = CustomSQLiteSession(session_file)
    logger.debug(f"Creazione del client Telethon per {phone_entry['phone']}")
    return TelegramClient(custom_session, api_id, api_hash)

# ---------------------- FUNZIONI DI SUPPORTO ---------

def load_phones():
    """Carica i dati dei telefoni da phones.json, sblocca quelli con pausa scaduta."""
    with LOCK:
        if not os.path.exists(PHONES_FILE):
            return []
        try:
            with open(PHONES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for p in data:
                p.setdefault('total_added', 0)
                p.setdefault('added_today', 0)
                p.setdefault('last_reset_date', datetime.date.today().isoformat())
                p.setdefault('paused_until', None)
                p.setdefault('paused', False)
                p.setdefault('non_result_errors', 0)
                p.setdefault('pause_reason', '')

                # Sblocca se la pausa è scaduta
                if p['paused_until']:
                    paused_dt = datetime.datetime.fromisoformat(p['paused_until'])
                    if datetime.datetime.now() >= paused_dt:
                        p['paused'] = False
                        p['paused_until'] = None
                        p['pause_reason'] = ''
            return data
        except Exception as e:
            logger.error("Errore in load_phones: %s", e)
            return []

def save_phones(phones):
    """Salva i dati dei telefoni su phones.json."""
    with LOCK:
        with open(PHONES_FILE, 'w', encoding='utf-8') as f:
            json.dump(phones, f, indent=2, ensure_ascii=False)
        logger.debug("Salvati dati telefoni su file.")

def reset_daily_counters_if_needed(phone_entry):
    """Resetta added_today se la data odierna non coincide con last_reset_date."""
    today_str = datetime.date.today().isoformat()
    if phone_entry.get('last_reset_date') != today_str:
        phone_entry['added_today'] = 0
        phone_entry['last_reset_date'] = today_str

def load_add_session():
    """Carica i dati dell’operazione di aggiunta da add_session.json."""
    with LOCK:
        if not os.path.exists(LOG_STATUS_FILE):
            return {}
        try:
            with open(LOG_STATUS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error("Errore in load_add_session: %s", e)
            return {}

def save_add_session():
    """Salva i dati dell’operazione di aggiunta su add_session.json."""
    with LOCK:
        with open(LOG_STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(ADD_SESSION, f, indent=2, ensure_ascii=False)
        logger.debug("Salvati dati sessione aggiunta.")

def safe_telethon_call(func, *args, max_retries=10, **kwargs):
    """Tenta di chiamare una funzione Telethon fino a max_retries in caso di database locked."""
    for attempt in range(max_retries):
        try:
            result = func(*args, **kwargs)
            logger.debug("safe_telethon_call riuscita al tentativo %d", attempt+1)
            return result
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower():
                logger.warning("Database locked in safe_telethon_call, tentativo %d: %s", attempt+1, e)
                time.sleep(5)
            else:
                logger.error("Errore in safe_telethon_call: %s", e)
                raise
    logger.error("safe_telethon_call: tutti i tentativi falliti, riprovo comunque")
    return func(*args, **kwargs)

def safe_invoke_request(client, request_cls, *args, max_retries=10, **kwargs):
    """Come sopra ma per funzioni request (GetParticipantRequest, JoinChannelRequest, ecc)."""
    for attempt in range(max_retries):
        try:
            result = client(request_cls(*args, **kwargs))
            logger.debug("safe_invoke_request riuscita al tentativo %d", attempt+1)
            return result
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower():
                logger.warning("Database locked in safe_invoke_request, tentativo %d: %s", attempt+1, e)
                time.sleep(5)
            else:
                logger.error("Errore in safe_invoke_request: %s", e)
                raise
    logger.error("safe_invoke_request: tutti i tentativi falliti, riprovo comunque")
    return client(request_cls(*args, **kwargs))

def count_available_phones(phones):
    """
    Conta i telefoni disponibili (non in pausa, sessione esistente, 
    added_today < 45, e pausa scaduta).
    """
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
                    p['pause_reason'] = ''
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
    logger.debug("Numeri disponibili: %d", available)
    return available

def suspend_until_enough_phones(min_phones, phones, username):
    """
    Se non ci sono abbastanza telefoni disponibili, attende 
    finché non si libera almeno min_phones.
    """
    global ADD_SESSION
    msg = f"Nessun phone disponibile per {username}, attendo almeno {min_phones} numeri liberi."
    logger.info(msg)
    ADD_SESSION['log'].append(msg)
    save_add_session()
    while True:
        if not ADD_SESSION.get('running', False):
            return
        if count_available_phones(phones) >= min_phones:
            msg2 = f"Numeri sufficienti disponibili per {username}, riprendo."
            logger.info(msg2)
            ADD_SESSION['log'].append(msg2)
            save_add_session()
            return
        time.sleep(20)

def update_phone_stats(phone_number, added=0, total=0, non_result_err_inc=0):
    """Aggiorna le statistiche (added_today, total_added, errori) di un numero."""
    phones = load_phones()
    for p in phones:
        if p['phone'] == phone_number:
            reset_daily_counters_if_needed(p)
            p['added_today'] += added
            p['total_added'] += total
            if non_result_err_inc:
                p['non_result_errors'] += non_result_err_inc
            save_phones(phones)
            logger.debug("Aggiornate statistiche %s: +%d, total +%d, err +%d",
                         phone_number, added, total, non_result_err_inc)
            return

def set_phone_pause(phone_number, paused=True, seconds=0, days=0, reason=''):
    """
    Mette in pausa o riattiva un numero, specificando anche un motivo (pause_reason).
    Se i secondi scadono, si sblocca da solo la prossima volta che carichiamo i phones.
    """
    phones = load_phones()
    for p in phones:
        if p['phone'] == phone_number:
            p['paused'] = paused
            if paused:
                p['pause_reason'] = reason if reason else 'Manual'
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
                p['pause_reason'] = ''
            save_phones(phones)
            logger.info("[set_phone_pause] %s: pause=%s (secs=%d, days=%d, reason=%s)",
                        phone_number, paused, seconds, days, p['pause_reason'])
            return

def should_skip_user_by_last_seen(user_entity, skip_config):
    """Valuta se saltare l’utente in base allo stato last_seen."""
    from telethon.tl.types import User
    if not isinstance(user_entity, User):
        return True
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
    """
    Connette il client con possibili retry in caso di "database locked".
    """
    for attempt in range(max_retries):
        try:
            client.connect()
            logger.debug("Client connesso al tentativo %d", attempt+1)
            return True
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower():
                logger.warning("Connessione locked, tentativo %d: %s", attempt+1, e)
                time.sleep(2)
            else:
                logger.error("Errore di connessione: %s", e)
                raise
    logger.error("Impossibile connettersi al client dopo %d tentativi", max_retries)
    return False

# ---------------------- ROUTE TEMPLATES --------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/log')
def log_page():
    return render_template('log.html')

# ---------------------- API GESTIONE NUMERI ----------
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
        'pause_reason': '',
        'added_today': 0,
        'last_reset_date': datetime.date.today().isoformat(),
        'total_added': 0,
        'non_result_errors': 0
    }
    phones.append(new_phone_entry)
    save_phones(phones)
    logger.info("Aggiunto nuovo telefono: %s", phone)
    return jsonify({'success': True})

@app.route('/api/phones/<phone>', methods=['DELETE'])
def api_remove_phone(phone):
    phones = load_phones()
    phones = [p for p in phones if p['phone'] != phone]
    save_phones(phones)
    logger.info("Rimosso telefono: %s", phone)
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
                p['pause_reason'] = ''
            save_phones(phones)
            logger.info("Impostato pause=%s per il telefono %s", pause_state, phone)
            return jsonify({'success': True})
    return jsonify({'error': 'Phone not found'}), 404

# ---------------------- API LOGIN TELEGRAM -----------
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
        logger.info("Codice inviato al telefono %s", phone)
        return jsonify({'success': True})
    except errors.PhoneNumberBannedError:
        logger.error("Telefono %s bannato da Telegram.", phone)
        return jsonify({'error': 'Numero bannato da Telegram.'}), 400
    except Exception as e:
        logger.exception("Errore in send_code per %s", phone)
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
        logger.info("OTP validato per il telefono %s", phone)
        return jsonify({'success': True})
    except errors.SessionPasswordNeededError:
        OTP_DICT[phone]['2fa_needed'] = True
        save_add_session()
        logger.warning("2FA richiesto per il telefono %s", phone)
        return jsonify({'error': 'SESSION_PASSWORD_NEEDED'}), 400
    except errors.PhoneCodeInvalidError:
        logger.warning("Codice OTP non valido per %s", phone)
        return jsonify({'error': 'Codice OTP non valido.'}), 400
    except errors.PhoneCodeExpiredError:
        logger.warning("Codice OTP scaduto per %s", phone)
        return jsonify({'error': 'Codice OTP scaduto.'}), 400
    except errors.PhoneNumberUnoccupiedError:
        logger.warning("Telefono %s non associato a un account.", phone)
        return jsonify({'error': 'Numero non associato a un account.'}), 400
    except Exception as e:
        logger.exception("Errore in validate_code per %s", phone)
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
        logger.info("Password 2FA validata per %s", phone)
        return jsonify({'success': True})
    except errors.PasswordHashInvalidError:
        logger.warning("Password 2FA non valida per %s", phone)
        return jsonify({'error': 'Password 2FA non valida.'}), 400
    except Exception as e:
        logger.exception("Errore in validate_password per %s", phone)
        return jsonify({'error': str(e)}), 400
    finally:
        client.disconnect()

# ---------------------- THREAD DI AGGIUNTA UTENTI ----
def add_users_to_group_thread(group_username, users_list):
    """
    Thread che aggiunge utenti al gruppo.
    Se otteniamo errori di FloodWait generici (es. "A wait of 3090 seconds is required"),
    parsiamo la stringa e mettiamo in pausa di conseguenza.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger.debug("Creato event loop nel thread add_users_to_group_thread().")

    global ADD_SESSION
    ADD_SESSION['running'] = True
    ADD_SESSION['group'] = group_username
    ADD_SESSION['total_added'] = 0
    ADD_SESSION['log'] = []
    ADD_SESSION['start_time'] = time.time()
    if 'last_user_index' not in ADD_SESSION:
        ADD_SESSION['last_user_index'] = 0
    ADD_SESSION['paused_due_to_limit'] = False
    ADD_SESSION['paused_until'] = None
    save_add_session()

    min_phones_available = ADD_SESSION.get('min_phones_available', 1)
    max_non_result_errors = ADD_SESSION.get('max_non_result_errors', 3)
    days_pause_non_result_errors = ADD_SESSION.get('days_pause_non_result_errors', 2)
    sleep_seconds = ADD_SESSION.get('sleep_seconds', 10)

    skip_config = {
        'last_seen_gt_1_day': ADD_SESSION.get('last_seen_gt_1_day', False),
        'last_seen_gt_7_days': ADD_SESSION.get('last_seen_gt_7_days', False),
        'last_seen_gt_30_days': ADD_SESSION.get('last_seen_gt_30_days', False),
        'last_seen_gt_60_days': ADD_SESSION.get('last_seen_gt_60_days', False),
        'user_status_empty': ADD_SESSION.get('user_status_empty', False)
    }

    def log(msg):
        ADD_SESSION['log'].append(msg)
        save_add_session()
        logger.info(msg)

    # Carica phone e crea client
    phones = load_phones()
    phone_clients = {}
    group_entities = {}

    # 1) Connetti i phone validi
    for p in phones:
        reset_daily_counters_if_needed(p)
        session_path = os.path.join(SESSIONS_FOLDER, f"{p['phone']}.session")
        if os.path.isfile(session_path) and not p['paused']:
            c = create_telegram_client(p)
            if not safe_telethon_connect(c):
                log(f"[{p['phone']}] Errore di connessione (db locked). Pausa 120s.")
                set_phone_pause(p['phone'], paused=True, seconds=120,
                                reason='Connessione fallita (db locked)')
                continue
            phone_clients[p['phone']] = c

    if not phone_clients:
        log("Nessun numero disponibile. Interruzione.")
        ADD_SESSION['running'] = False
        save_add_session()
        return

    # 2) Risolvi l'entity del gruppo
    for phone, client in list(phone_clients.items()):
        try:
            grp_ent = safe_telethon_call(client.get_entity, group_username)
            if isinstance(grp_ent, (tl.types.Channel, tl.types.Chat)):
                group_entities[phone] = grp_ent
                log(f"[{phone}] Gruppo '{group_username}' risolto correttamente.")
            else:
                log(f"[{phone}] '{group_username}' non è un canale/supergruppo. Pausa.")
                set_phone_pause(phone, True, reason='Group non valido')
        except FloodWaitError as e:
            # Se la get_entity lancia un "vero" FloodWaitError
            log(f"[{phone}] FloodWaitError (get_entity): pausa {e.seconds}s.")
            set_phone_pause(phone, True, seconds=e.seconds, reason=f'FloodWait {e.seconds}s')
        except Exception as e:
            # Se è un "A wait of XXX seconds is required" nel messaggio,
            # lo interpretiamo come FloodWait personalizzato
            msg_str = str(e).lower()
            if "a wait of" in msg_str and " seconds is required" in msg_str:
                # Estrai i secondi
                sec = parse_wait_seconds(msg_str)
                if sec > 0:
                    log(f"[{phone}] get_entity: FloodWait di {sec}s da messaggio eccezione. Pausa.")
                    set_phone_pause(phone, True, seconds=sec, reason=f'FloodWait {sec}s')
                else:
                    log(f"[{phone}] Errore get_entity('{group_username}'): {e}. Pausa.")
                    set_phone_pause(phone, True, reason='get_entity error')
            else:
                log(f"[{phone}] Errore get_entity('{group_username}'): {e}. Pausa.")
                set_phone_pause(phone, True, reason='get_entity error')

    # Rimuovi client se non abbiamo un group_entity
    for phone in list(phone_clients.keys()):
        if phone not in group_entities:
            phone_clients[phone].disconnect()
            phone_clients.pop(phone)

    if not phone_clients:
        log("Nessun client può accedere al gruppo. Interruzione.")
        ADD_SESSION['running'] = False
        save_add_session()
        return

    # 3) Controlla se ciascun phone è già nel gruppo; se non c'è, prova a unirsi
    for phone, client in phone_clients.items():
        try:
            me = client.get_me()
            safe_invoke_request(client, GetParticipantRequest, group_entities[phone], me)
            log(f"[{phone}] Già nel gruppo.")
        except rpcerrorlist.UserNotParticipantError:
            try:
                safe_invoke_request(client, JoinChannelRequest, group_entities[phone])
                safe_invoke_request(client, GetParticipantRequest, group_entities[phone], me)
                log(f"[{phone}] Unito al gruppo con successo.")
            except FloodWaitError as e:
                log(f"[{phone}] FloodWaitError join: pausa {e.seconds}s.")
                set_phone_pause(phone, True, seconds=e.seconds, reason=f'FloodWait {e.seconds}s')
            except Exception as e:
                # Check se "A wait of"
                msg_str = str(e).lower()
                if "a wait of" in msg_str and "seconds is required" in msg_str:
                    sec = parse_wait_seconds(msg_str)
                    if sec > 0:
                        log(f"[{phone}] get_entity/join: FloodWait di {sec}s da messaggio eccezione. Pausa.")
                        set_phone_pause(phone, True, seconds=sec, reason=f'FloodWait {sec}s')
                    else:
                        log(f"[{phone}] Errore join gruppo: {e}. Pausa.")
                        set_phone_pause(phone, True, reason='JoinChannelRequest error')
                else:
                    log(f"[{phone}] Errore join gruppo: {e}. Pausa.")
                    set_phone_pause(phone, True, reason='JoinChannelRequest error')
        except FloodWaitError as e:
            log(f"[{phone}] FloodWaitError get_participation: {e.seconds}s.")
            set_phone_pause(phone, True, seconds=e.seconds, reason=f'FloodWait {e.seconds}s')
        except Exception as e:
            msg_str = str(e).lower()
            if "a wait of" in msg_str and "seconds is required" in msg_str:
                sec = parse_wait_seconds(msg_str)
                if sec > 0:
                    log(f"[{phone}] get_participation: FloodWait {sec}s. Pausa.")
                    set_phone_pause(phone, True, seconds=sec, reason=f'FloodWait {sec}s')
                else:
                    log(f"[{phone}] Errore partecipazione: {e}. Pausa.")
                    set_phone_pause(phone, True, reason='Partecipazione check error')
            else:
                log(f"[{phone}] Errore controllo partecipazione: {e}. Pausa.")
                set_phone_pause(phone, True, reason='Partecipazione check error')

    # Ricarica i phone e rimuovi quelli in pausa
    for phone in list(phone_clients.keys()):
        current_phones = load_phones()
        for px in current_phones:
            if px['phone'] == phone and px['paused']:
                phone_clients[phone].disconnect()
                phone_clients.pop(phone)
                break

    if not phone_clients:
        log("Nessun client disponibile dopo il join. Interruzione.")
        ADD_SESSION['running'] = False
        save_add_session()
        return

    # 4) Itera sugli utenti
    phone_list = sorted(list(phone_clients.keys()))
    phone_index = 0
    i = ADD_SESSION.get('last_user_index', 0)

    while i < len(users_list):
        if not ADD_SESSION.get('running', False):
            break

        username = users_list[i].strip()
        if not username:
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue

        # Trova un phone libero
        attempts = 0
        selected_phone = None
        while attempts < len(phone_list):
            curr_phone = phone_list[phone_index]
            phone_index = (phone_index + 1) % len(phone_list)
            attempts += 1

            current_phones = load_phones()
            p_entry = next((px for px in current_phones if px['phone'] == curr_phone), None)
            if not p_entry or p_entry['paused'] or p_entry['added_today'] >= 45:
                continue

            # Controlla se è in flood
            flood_time = 0
            if p_entry['paused_until']:
                paused_dt = datetime.datetime.fromisoformat(p_entry['paused_until'])
                flood_time = max(int((paused_dt - datetime.datetime.now()).total_seconds()), 0)
            if flood_time > 0:
                continue

            # Trovato phone libero
            selected_phone = curr_phone
            break

        if not selected_phone:
            # Non ci sono numeri disponibili
            suspend_until_enough_phones(min_phones_available, load_phones(), username)
            if not ADD_SESSION.get('running', False):
                break
            continue

        client = phone_clients[selected_phone]
        grp = group_entities[selected_phone]

        # Risolvi l'entità dell'utente
        try:
            user_entity = safe_telethon_call(client.get_entity, username)
        except UsernameNotOccupiedError:
            log(f"[{selected_phone}] {username}: non esiste. Skip.")
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue
        except FloodWaitError as e:
            log(f"[{selected_phone}] FloodWaitError get_entity {username}: {e.seconds}s.")
            set_phone_pause(selected_phone, True, seconds=e.seconds, reason=f'FloodWait {e.seconds}s')
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue
        except Exception as e:
            # Check "A wait of X seconds is required"
            msg_str = str(e).lower()
            if "a wait of" in msg_str and "seconds is required" in msg_str:
                sec = parse_wait_seconds(msg_str)
                if sec > 0:
                    log(f"[{selected_phone}] get_entity: FloodWait {sec}s per {username}. Pausa.")
                    set_phone_pause(selected_phone, True, seconds=sec, reason=f'FloodWait {sec}s')
                else:
                    log(f"[{selected_phone}] Errore get_entity('{username}'): {e}. Skip.")
            else:
                log(f"[{selected_phone}] Errore get_entity('{username}'): {e}. Skip.")
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue

        # Controllo "ultimo accesso"
        if should_skip_user_by_last_seen(user_entity, skip_config):
            log(f"[{selected_phone}] {username}: skip per last seen.")
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue

        # Verifica se è già nel gruppo
        try:
            safe_invoke_request(client, GetParticipantRequest, grp, user_entity)
            log(f"[{selected_phone}] {username}: già nel gruppo. Skip.")
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue
        except rpcerrorlist.UserNotParticipantError:
            pass
        except Exception as e:
            log(f"[{selected_phone}] Errore controllo partecipazione di {username}: {e}. Skip.")
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue

        # Prova a invitare
        try:
            safe_invoke_request(client, InviteToChannelRequest, grp, [user_entity])
            log(f"[{selected_phone}] Invitato -> {username}")

            # Verifica se è davvero dentro
            try:
                safe_invoke_request(client, GetParticipantRequest, grp, user_entity)
                log(f"[{selected_phone}] {username} confermato nel gruppo.")
                update_phone_stats(selected_phone, added=1, total=1)
                ADD_SESSION['total_added'] += 1
            except:
                log(f"[{selected_phone}] ERRORE: {username} non confermato dopo invito.")
                update_phone_stats(selected_phone, non_result_err_inc=1)
                current_p = load_phones()
                for xx in current_p:
                    if xx['phone'] == selected_phone and xx['non_result_errors'] >= max_non_result_errors:
                        set_phone_pause(selected_phone, True, days=days_pause_non_result_errors,
                                        reason='Troppi errori non_result')
                        xx['non_result_errors'] = 0
                        save_phones(current_p)
                        log(f"[{selected_phone}] Pausa {days_pause_non_result_errors} giorni per errori.")
        except FloodWaitError as e:
            log(f"[{selected_phone}] FloodWaitError: pausa {e.seconds}s.")
            set_phone_pause(selected_phone, True, seconds=e.seconds, reason=f'FloodWait {e.seconds}s')
        except PeerFloodError:
            log(f"[{selected_phone}] PeerFloodError: pausa 120s.")
            set_phone_pause(selected_phone, True, seconds=120, reason='PeerFloodError')
        except errors.UserPrivacyRestrictedError:
            log(f"[{selected_phone}] {username}: privacy restrittiva, skip.")
        except errors.UserNotMutualContactError:
            log(f"[{selected_phone}] {username}: non è contatto reciproco, skip.")
        except Exception as ex:
            msg_str = str(ex).lower()
            if "a wait of" in msg_str and "seconds is required" in msg_str:
                sec = parse_wait_seconds(msg_str)
                if sec > 0:
                    log(f"[{selected_phone}] FloodWait {sec}s per {username}. Pausa.")
                    set_phone_pause(selected_phone, True, seconds=sec, reason=f'FloodWait {sec}s')
                else:
                    log(f"[{selected_phone}] Errore sconosciuto con {username}: {ex}. Skip.")
            else:
                log(f"[{selected_phone}] Errore sconosciuto con {username}: {ex}. Skip.")

        i += 1
        ADD_SESSION['last_user_index'] = i
        save_add_session()

    # Fine lista
    if i >= len(users_list):
        ADD_SESSION['last_user_index'] = 0
        log("Lista utenti terminata. Reset index a 0.")

    for c in phone_clients.values():
        c.disconnect()

    ADD_SESSION['running'] = False
    log("Operazione di aggiunta terminata.")
    save_add_session()

def parse_wait_seconds(msg_str):
    """
    Cerca la sottostringa "a wait of NNNN seconds is required"
    e restituisce l'intero NNNN. Se non trova nulla, restituisce 0.
    """
    # Esempio di msg: "A wait of 3090 seconds is required (caused by ResolveUsernameRequest)"
    # Converti tutto in minuscolo e cerca
    import re
    pattern = r"a wait of\s+(\d+)\s+seconds is required"
    match = re.search(pattern, msg_str)
    if match:
        return int(match.group(1))
    return 0

# ---------------------- API AVVIO/STOP AGGIUNTA ------
@app.route('/api/start_adding', methods=['POST'])
def api_start_adding():
    global ADD_SESSION
    if ADD_SESSION.get('running', False):
        return jsonify({'error': "Un'operazione di aggiunta è già in corso."}), 400

    data = request.json
    group_username = data.get('group_username', '').strip()
    user_list_raw = data.get('user_list', '').strip()
    users_list = [u.strip() for u in user_list_raw.splitlines() if u.strip()]

    # Parametri
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

    if not group_username or not users_list:
        return jsonify({'error': 'Dati insufficienti (gruppo o lista utenti vuota).'}), 400

    ADD_SESSION['running'] = True
    ADD_SESSION['group'] = group_username
    ADD_SESSION['total_added'] = 0
    ADD_SESSION['log'] = []
    ADD_SESSION['start_time'] = time.time()
    if 'last_user_index' not in ADD_SESSION:
        ADD_SESSION['last_user_index'] = 0
    save_add_session()

    t = threading.Thread(target=add_users_to_group_thread, args=(group_username, users_list), daemon=True)
    t.start()
    logger.info("Avviata operazione di aggiunta per il gruppo %s", group_username)
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
        logger.info("Operazione fermata manualmente dall'utente.")
        return jsonify({"success": True, "message": "Operazione fermata."})
    else:
        return jsonify({"success": False, "message": "Nessuna operazione in corso."}), 400

# ---------------------- API RIEPILOGO -----------------
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
            'total_added': p['total_added'],
            'pause_reason': p.get('pause_reason', '')
        })
    session_total = ADD_SESSION.get('total_added', 0)
    return jsonify({
        'session_added_total': session_total,
        'phones': summary_list
    })

# ---------------------- API UPLOAD EXCEL -------------
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
        logger.exception("Errore lettura Excel")
        return jsonify({'error': f'Errore lettura Excel: {str(e)}'}), 400
    logger.info("Excel caricato correttamente, %d usernames trovati.", len(usernames))
    return jsonify({'user_list': '\n'.join(usernames)})

# ---------------------- RIAVVIO TMUX -----------------
def restart_tmux_thread(app_path):
    time.sleep(2)
    subprocess.run(["tmux", "kill-session", "-t", "mioadder"], check=False)
    cmd_new = ["tmux", "new-session", "-d", "-s", "mioadder", sys.executable, app_path]
    subprocess.run(cmd_new, check=True)
    logger.info("Sessione tmux 'mioadder' riavviata.")

@app.route('/api/restart_tmux', methods=['POST'])
def api_restart_tmux():
    try:
        app_path = os.path.abspath(sys.argv[0])
        threading.Thread(target=restart_tmux_thread, args=(app_path,), daemon=True).start()
        logger.info("Richiesta di riavvio tmux ricevuta.")
        return jsonify({"success": True, "message": "Riavvio della sessione TMUX 'mioadder' avviato."})
    except Exception as e:
        logger.exception("Errore nel riavvio tmux")
        return jsonify({"success": False, "message": str(e)}), 500

# ---------------------- MAIN -------------------------
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5050, debug=True, threaded=False, use_reloader=False)
