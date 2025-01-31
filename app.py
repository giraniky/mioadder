import os
import json
import threading
import time
import datetime
import asyncio
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

# Stato dell'operazione di aggiunta (caricato da file a inizio)
ADD_SESSION = {}

LOCK = threading.Lock()


# ---------------------------------------------------------------------
# FUNZIONI DI SUPPORTO
# ---------------------------------------------------------------------

def load_phones():
    with LOCK:
        if not os.path.exists(PHONES_FILE):
            return []
        try:
            with open(PHONES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # Aggiorniamo i campi e verifichiamo pause scadute
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

                # Se la pausa è scaduta, sblocchiamo
                if p['paused_until']:
                    paused_until_dt = datetime.datetime.fromisoformat(p['paused_until'])
                    if datetime.datetime.now() >= paused_until_dt:
                        p['paused'] = False
                        p['paused_until'] = None
            return data
        except:
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
                data = json.load(f)
            return data
        except:
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

# Carichiamo lo stato ADD_SESSION
ADD_SESSION = load_add_session()

def count_available_phones(phones):
    now = datetime.datetime.now()
    available = 0
    for p in phones:
        reset_daily_counters_if_needed(p)
        if p['paused']:
            if p['paused_until']:
                paused_until_dt = datetime.datetime.fromisoformat(p['paused_until'])
                if now >= paused_until_dt:
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
            paused_until_dt = datetime.datetime.fromisoformat(p['paused_until'])
            delta = (paused_until_dt - now).total_seconds()
            flood_time = max(int(delta), 0)
        if flood_time > 0:
            continue

        available += 1
    return available

def suspend_until_enough_phones(min_phones, phones, username):
    global ADD_SESSION
    if ADD_SESSION.get('running', False):
        log_msg = f"Nessun phone disponibile per {username}, sospendo finché non tornano almeno {min_phones} numeri disponibili."
        ADD_SESSION['log'].append(log_msg)
        save_add_session()

    while True:
        if not ADD_SESSION.get('running', False):
            return
        current_avail = count_available_phones(phones)
        if current_avail >= min_phones:
            resume_msg = f"Sono nuovamente disponibili {current_avail} numeri: riprendo l'aggiunta di {username}."
            ADD_SESSION['log'].append(resume_msg)
            save_add_session()
            return
        time.sleep(60)

def update_phone_stats(phone_number, added=0, total=0, non_result_err_inc=0):
    phones = load_phones()
    for p in phones:
        if p['phone'] == phone_number:
            reset_daily_counters_if_needed(p)
            if added != 0:
                p['added_today'] += added
            if total != 0:
                p['total_added'] += total
            if non_result_err_inc != 0:
                p['non_result_errors'] += non_result_err_inc
            save_phones(phones)
            return

def set_phone_pause(phone_number, paused=True, seconds=0, days=0):
    phones = load_phones()
    for p in phones:
        if p['phone'] == phone_number:
            p['paused'] = paused
            if paused:
                if seconds > 0:
                    dt = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
                    p['paused_until'] = dt.isoformat()
                elif days > 0:
                    dt = datetime.datetime.now() + datetime.timedelta(days=days)
                    p['paused_until'] = dt.isoformat()
                else:
                    p['paused_until'] = None
            else:
                p['paused_until'] = None
            save_phones(phones)
            return

# Funzione di utilità per controllare se bisogna saltare l'utente
# in base al suo "last seen" e alle impostazioni scelte in interfaccia
def should_skip_user_by_last_seen(user_entity, skip_config):
    """
    skip_config contiene le seguenti chiavi:
      - last_seen_gt_1_day
      - last_seen_gt_7_days
      - last_seen_gt_30_days
      - last_seen_gt_60_days
      - user_status_empty
    """
    status = user_entity.status
    now = datetime.datetime.now()

    if not status:
        # Trattiamo come UserStatusEmpty
        if skip_config.get('user_status_empty'):
            return True
        return False

    # Gestione stati di ultimo accesso visibile
    if isinstance(status, UserStatusOffline):
        last_seen = status.was_online.replace(tzinfo=None)
        delta = now - last_seen
        days_since = delta.days

        if skip_config.get('last_seen_gt_1_day') and days_since > 1:
            return True
        if skip_config.get('last_seen_gt_7_days') and days_since > 7:
            return True
        if skip_config.get('last_seen_gt_30_days') and days_since > 30:
            return True
        if skip_config.get('last_seen_gt_60_days') and days_since > 60:
            return True

    # Gestione altri stati
    if isinstance(status, UserStatusEmpty):
        if skip_config.get('user_status_empty'):
            return True
    elif isinstance(status, UserStatusLastMonth):
        if skip_config.get('last_seen_gt_30_days'):
            return True
    elif isinstance(status, UserStatusLastWeek):
        if skip_config.get('last_seen_gt_7_days'):
            return True
    elif isinstance(status, UserStatusRecently):
        if skip_config.get('last_seen_gt_1_day'):
            return True

    return False

# ---------------------------------------------------------------------
# ROTTE FRONTEND
# ---------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

# SEZIONE 1: GESTIONE NUMERI
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

# SEZIONE 2: LOGIN E GESTIONE OTP
@app.route('/api/send_code', methods=['POST'])
def api_send_code():
    data = request.json
    phone = data['phone']

    phones = load_phones()
    phone_entry = None
    for p in phones:
        if p['phone'] == phone:
            phone_entry = p
            break
    if not phone_entry:
        return jsonify({'error': 'Phone not found'}), 404

    client = create_telegram_client(phone_entry)
    try:
        client.connect()
        sent = client.send_code_request(phone, force_sms=True)
        OTP_DICT[phone] = {
            'phone_code_hash': sent.phone_code_hash,
            '2fa_needed': False
        }
        return jsonify({'success': True})
    except errors.PhoneNumberBannedError:
        return jsonify({'error': 'This phone number is banned by Telegram.'}), 400
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
        return jsonify({'error': 'No OTP session found for this phone'}), 400

    phones = load_phones()
    phone_entry = None
    for p in phones:
        if p['phone'] == phone:
            phone_entry = p
            break
    if not phone_entry:
        return jsonify({'error': 'Phone not found'}), 404

    phone_code_hash = OTP_DICT[phone]['phone_code_hash']
    client = create_telegram_client(phone_entry)
    try:
        client.connect()
        client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        del OTP_DICT[phone]
        return jsonify({'success': True})
    except errors.SessionPasswordNeededError:
        OTP_DICT[phone]['2fa_needed'] = True
        save_add_session()
        return jsonify({'error': 'SESSION_PASSWORD_NEEDED'}), 400
    except errors.PhoneCodeInvalidError:
        return jsonify({'error': 'Invalid code provided.'}), 400
    except errors.PhoneCodeExpiredError:
        return jsonify({'error': 'The code has expired. Please request a new one.'}), 400
    except errors.PhoneNumberUnoccupiedError:
        return jsonify({'error': 'The phone number is not associated with any account.'}), 400
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
        return jsonify({'error': 'No OTP session found for this phone'}), 400
    if not OTP_DICT[phone].get('2fa_needed'):
        return jsonify({'error': '2FA was not requested for this phone'}), 400

    phones = load_phones()
    phone_entry = None
    for p in phones:
        if p['phone'] == phone:
            phone_entry = p
            break
    if not phone_entry:
        return jsonify({'error': 'Phone not found'}), 404

    client = create_telegram_client(phone_entry)
    try:
        client.connect()
        client.sign_in(password=password)
        del OTP_DICT[phone]
        return jsonify({'success': True})
    except errors.PasswordHashInvalidError:
        return jsonify({'error': 'Invalid 2FA password provided.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        client.disconnect()

# SEZIONE 3: AGGIUNTA UTENTI AL GRUPPO
def add_users_to_group_thread(group_username, users_list):
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

    # Parametri
    min_phones_available = ADD_SESSION.get('min_phones_available', 1)
    max_non_result_errors = ADD_SESSION.get('max_non_result_errors', 3)
    days_pause_non_result_errors = ADD_SESSION.get('days_pause_non_result_errors', 2)
    sleep_seconds = ADD_SESSION.get('sleep_seconds', 10)

    # Nuovi parametri di skip
    skip_config = {
        'last_seen_gt_1_day': ADD_SESSION.get('last_seen_gt_1_day', False),
        'last_seen_gt_7_days': ADD_SESSION.get('last_seen_gt_7_days', False),
        'last_seen_gt_30_days': ADD_SESSION.get('last_seen_gt_30_days', False),
        'last_seen_gt_60_days': ADD_SESSION.get('last_seen_gt_60_days', False),
        'user_status_empty': ADD_SESSION.get('user_status_empty', False)
    }

    # Event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Carichiamo i phone
    phones = load_phones()
    phone_clients = {}

    def log(msg):
        ADD_SESSION['log'].append(msg)
        save_add_session()
        print(msg)

    # Connessione
    for p in phones:
        reset_daily_counters_if_needed(p)
        session_path = os.path.join(SESSIONS_FOLDER, f"{p['phone']}.session")
        if os.path.isfile(session_path) and not p['paused']:
            try:
                c = create_telegram_client(p)
                c.connect()
                phone_clients[p['phone']] = c
            except Exception as e:
                log(f"[{p['phone']}] Errore di connessione: {e}")

    if not phone_clients:
        log("Nessun numero disponibile con sessione valida. Interruzione.")
        ADD_SESSION['running'] = False
        save_add_session()
        return

    # Recupero entità gruppo
    group_entities = {}
    for phone, client in list(phone_clients.items()):
        try:
            grp_ent = client.get_entity(group_username)
            if isinstance(grp_ent, (tl.types.Channel, tl.types.Chat)):
                group_entities[phone] = grp_ent
                log(f"[{phone}] Gruppo '{group_username}' risolto correttamente.")
            else:
                log(f"[{phone}] Gruppo '{group_username}' non è un canale/supergruppo. Pausa del numero.")
                set_phone_pause(phone, paused=True)
        except errors.UsernameNotOccupiedError:
            log(f"[{phone}] Gruppo '{group_username}' non esiste. Pausa del numero.")
            set_phone_pause(phone, paused=True)
        except errors.ChannelPrivateError:
            log(f"[{phone}] Gruppo '{group_username}' è privato e non accessibile. Pausa del numero.")
            set_phone_pause(phone, paused=True)
        except Exception as e:
            log(f"[{phone}] Impossibile recuperare il gruppo '{group_username}': {e}. Pausa del numero.")
            set_phone_pause(phone, paused=True)

    # Rimuoviamo i client che non possono gestire il gruppo
    for phone in list(phone_clients.keys()):
        if phone not in group_entities:
            phone_clients[phone].disconnect()
            phone_clients.pop(phone)

    if not group_entities:
        log("Nessun client può accedere al gruppo. Interruzione.")
        ADD_SESSION['running'] = False
        for c in phone_clients.values():
            c.disconnect()
        save_add_session()
        return

    # Aggiunta di un controllo: assicurarsi che ogni phone sia già nel gruppo
    for phone, client in phone_clients.items():
        try:
            me = client.get_me()
            client(GetParticipantRequest(group_entities[phone], me))
            log(f"[{phone}] Il numero è già nel gruppo.")
        except rpcerrorlist.UserNotParticipantError:
            try:
                client(JoinChannelRequest(group_entities[phone]))
                # Verifica se è riuscito ad unirsi
                client(GetParticipantRequest(group_entities[phone], me))
                log(f"[{phone}] Unito al gruppo con successo.")
            except Exception as e:
                log(f"[{phone}] Errore unendosi al gruppo: {e}. Pausa del numero.")
                set_phone_pause(phone, paused=True)
        except Exception as e:
            log(f"[{phone}] Errore controllando la partecipazione al gruppo: {e}. Pausa del numero.")
            set_phone_pause(phone, paused=True)

    # Rimuoviamo i client che non sono riusciti ad unirsi al gruppo
    for phone in list(phone_clients.keys()):
        phones_updated = load_phones()
        for p in phones_updated:
            if p['phone'] == phone and p['paused']:
                phone_clients[phone].disconnect()
                phone_clients.pop(phone)
                break

    if not phone_clients:
        log("Nessun client disponibile dopo il controllo di partecipazione al gruppo. Interruzione.")
        ADD_SESSION['running'] = False
        save_add_session()
        return

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

        # Cerca un phone disponibile
        attempts = 0
        selected_phone = None
        while attempts < len(phone_list):
            cur_phone = phone_list[phone_index]
            phone_index = (phone_index + 1) % len(phone_list)
            attempts += 1

            if cur_phone not in phone_clients:
                continue

            current_phones = load_phones()
            p_entry = None
            for px in current_phones:
                if px['phone'] == cur_phone:
                    p_entry = px
                    reset_daily_counters_if_needed(px)
                    break

            if not p_entry:
                continue
            if p_entry['paused']:
                continue
            if p_entry['added_today'] >= 45:
                continue

            flood_time = 0
            if p_entry['paused_until']:
                paused_until_dt = datetime.datetime.fromisoformat(p_entry['paused_until'])
                delta = (paused_until_dt - datetime.datetime.now()).total_seconds()
                flood_time = max(int(delta), 0)
            if flood_time > 0:
                continue

            selected_phone = cur_phone
            break

        if not selected_phone:
            suspend_until_enough_phones(min_phones_available, load_phones(), username)
            if not ADD_SESSION.get('running', False):
                break
            continue

        client = phone_clients[selected_phone]
        group_entity = group_entities[selected_phone]

        # Risolviamo l'utente
        try:
            user_entity = client.get_entity(username)
        except errors.UsernameNotOccupiedError:
            log(f"[{selected_phone}] {username}: l'username non esiste. Skipping.")
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue
        except Exception as ex:
            err_msg = str(ex)
            log(f"[{selected_phone}] Errore risoluzione {username}: {err_msg}. Skipping.")
            if 'A wait of' in err_msg and 'seconds is required' in err_msg:
                try:
                    parts = err_msg.split('A wait of')[1].split('seconds is required')[0].strip()
                    wait_seconds = int(parts)
                    set_phone_pause(selected_phone, paused=True, seconds=wait_seconds)
                    log(f"[{selected_phone}] Pausa per {wait_seconds} secondi (FloodWait).")
                except:
                    pass
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue

        # --- NUOVO CONTROLLO: skip in base a last_seen ---
        if should_skip_user_by_last_seen(user_entity, skip_config):
            log(f"[{selected_phone}] {username}: skip per impostazioni ultimo accesso (status={user_entity.status}).")
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue

        # Verifichiamo se è già nel gruppo
        try:
            client(GetParticipantRequest(group_entity, user_entity))
            log(f"[{selected_phone}] {username}: già nel gruppo. Skipping.")
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue
        except rpcerrorlist.UserNotParticipantError:
            pass
        except Exception as e:
            log(f"[{selected_phone}] Errore controllando partecipazione di {username}: {e}. Skipping.")
            i += 1
            ADD_SESSION['last_user_index'] = i
            save_add_session()
            continue

        # Tentiamo l'aggiunta
        try:
            client(InviteToChannelRequest(group_entity, [user_entity]))
            # Se non c'è eccezione, consideriamo aggiunto
            update_phone_stats(selected_phone, added=1, total=1)
            ADD_SESSION['total_added'] += 1
            log(f"[{selected_phone}] Aggiunto con successo -> {username}")

            # Verifica se risulta nel gruppo
            try:
                client(GetParticipantRequest(group_entity, user_entity))
                log(f"[{selected_phone}] {username}: inserito e confermato nel gruppo.")
            except:
                # Non risulta dopo l'aggiunta
                log(f"[{selected_phone}] ERRORE: {username} non risulta nel gruppo dopo l'aggiunta.")
                update_phone_stats(selected_phone, non_result_err_inc=1)
                # Carichiamo phone di nuovo per verificare contatore
                p_after = load_phones()
                for xx in p_after:
                    if xx['phone'] == selected_phone:
                        if xx['non_result_errors'] >= max_non_result_errors:
                            set_phone_pause(selected_phone, paused=True, days=days_pause_non_result_errors)
                            xx['non_result_errors'] = 0
                            save_phones(p_after)
                            log(f"[{selected_phone}] Superata soglia errori: pausa di {days_pause_non_result_errors} giorni.")

            time.sleep(sleep_seconds)

        except errors.FloodWaitError as e:
            wait_seconds = e.seconds
            log(f"[{selected_phone}] FloodWaitError. Pausa {wait_seconds}s.")
            set_phone_pause(selected_phone, paused=True, seconds=wait_seconds)
            time.sleep(wait_seconds)
        except errors.PeerFloodError:
            log(f"[{selected_phone}] PeerFloodError: spam rilevato. Pausa 2 min.")
            set_phone_pause(selected_phone, paused=True, seconds=120)
        except errors.UserPrivacyRestrictedError:
            log(f"[{selected_phone}] {username}: Restrizione privacy. Non è possibile invitarlo direttamente, si può invitare tramite link.")
        except errors.UserNotMutualContactError:
            log(f"[{selected_phone}] {username}: non è contatto reciproco. Skipping.")
        except Exception as ex:
            err_msg = str(ex)
            log(f"[{selected_phone}] Errore sconosciuto con {username}: {err_msg}. Skipping.")
            if 'A wait of' in err_msg and 'seconds is required' in err_msg:
                try:
                    parts = err_msg.split('A wait of')[1].split('seconds is required')[0].strip()
                    wait_seconds = int(parts)
                    set_phone_pause(selected_phone, paused=True, seconds=wait_seconds)
                    log(f"[{selected_phone}] Messo in pausa per {wait_seconds} secondi (FloodWait).")
                except:
                    pass

        i += 1
        ADD_SESSION['last_user_index'] = i
        save_add_session()

    if i >= len(users_list):
        ADD_SESSION['last_user_index'] = 0
        log("Lista utenti terminata. last_user_index riportato a 0.")

    for c in phone_clients.values():
        c.disconnect()

    ADD_SESSION['running'] = False
    log("Operazione di aggiunta terminata.")
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

    ADD_SESSION['min_phones_available'] = data.get('min_phones_available', 1)
    ADD_SESSION['max_non_result_errors'] = data.get('max_non_result_errors', 3)
    ADD_SESSION['days_pause_non_result_errors'] = data.get('days_pause_non_result_errors', 2)
    ADD_SESSION['sleep_seconds'] = data.get('sleep_seconds', 10)

    # Nuovi parametri di skip
    skip_options = data.get('skip_options', [])
    ADD_SESSION['last_seen_gt_1_day'] = 'last_seen_gt_1_day' in skip_options
    ADD_SESSION['last_seen_gt_7_days'] = 'last_seen_gt_7_days' in skip_options
    ADD_SESSION['last_seen_gt_30_days'] = 'last_seen_gt_30_days' in skip_options
    ADD_SESSION['last_seen_gt_60_days'] = 'last_seen_gt_60_days' in skip_options
    ADD_SESSION['user_status_empty'] = 'user_status_empty' in skip_options

    if not group_username or not users_list:
        return jsonify({'error': 'Dati insufficienti (gruppo o lista utenti vuota).'}), 400

    # Reset session
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
        return jsonify({"success": False, "message": "Nessuna operazione in corso"}), 400

# SEZIONE 4: RIEPILOGO
@app.route('/api/summary', methods=['GET'])
def api_summary():
    phones = load_phones()
    now = datetime.datetime.now()
    summary_list = []
    for p in phones:
        flood_time = 0
        if p['paused_until']:
            paused_until_dt = datetime.datetime.fromisoformat(p['paused_until'])
            delta = (paused_until_dt - now).total_seconds()
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

# SEZIONE 5: CARICAMENTO FILE EXCEL
@app.route('/api/upload_excel', methods=['POST'])
def upload_excel():
    if 'excel_file' not in request.files:
        return jsonify({'error': 'Nessun file Excel caricato.'}), 400

    file = request.files['excel_file']
    if file.filename == '':
        return jsonify({'error': 'Nessun file selezionato.'}), 400

    # Controlliamo l'estensione
    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()
    valid_exts = ['.xlsx', '.xlsm', '.xltx', '.xltm']
    if ext not in valid_exts:
        return jsonify({'error': f'Formato non supportato. Estensioni valide: {valid_exts}'}), 400

    # Salvataggio temporaneo
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        wb = openpyxl.load_workbook(filepath, read_only=True)
        sheet = wb.active
        usernames = []
        for row in sheet.iter_rows(min_row=1, min_col=1, max_col=1, values_only=True):
            cell_value = row[0]
            if cell_value:
                cell_str = str(cell_value).strip()
                if not cell_str.startswith('@'):
                    cell_str = '@' + cell_str
                usernames.append(cell_str)
        wb.close()
        os.remove(filepath)
    except Exception as e:
        return jsonify({'error': f'Errore lettura Excel: {str(e)}'}), 400

    return jsonify({'user_list': '\n'.join(usernames)})


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5050, debug=True, threaded=False, use_reloader=False)


