import os
import json
import threading
import time
import datetime
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename

# Telethon in modalità sincrona:
from telethon.sync import TelegramClient, errors
from telethon import tl
from telethon.errors import rpcerrorlist
from telethon.tl.functions.channels import InviteToChannelRequest, GetParticipantRequest, JoinChannelRequest
from telethon.tl.types import (
    UserStatusEmpty, UserStatusLastMonth, UserStatusLastWeek,
    UserStatusRecently, UserStatusOffline, UserStatusOnline
)

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

# Stato dell'operazione di aggiunta (caricato da file all'inizio)
ADD_SESSION = {}

# Lock per l’accesso concurrente a phones.json e add_session.json
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
            # Aggiorna campi e verifica pause scadute
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

                if p['paused_until']:
                    paused_until_dt = datetime.datetime.fromisoformat(p['paused_until'])
                    if datetime.datetime.now() >= paused_until_dt:
                        p['paused'] = False
                        p['paused_until'] = None
            return data
        except (json.JSONDecodeError, OSError):
            print("Errore nel leggere/decodificare phones.json. Torno a lista vuota.")
            return []
        except Exception as e:
            print(f"Errore load_phones: {e}")
            return []

def save_phones(phones):
    with LOCK:
        try:
            with open(PHONES_FILE, 'w', encoding='utf-8') as f:
                json.dump(phones, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Errore save_phones: {e}")

def load_add_session():
    with LOCK:
        if not os.path.exists(LOG_STATUS_FILE):
            return {}
        try:
            with open(LOG_STATUS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data
        except (json.JSONDecodeError, OSError):
            print("Errore nel leggere/decodificare add_session.json. Torno a {}.")
            return {}
        except Exception as e:
            print(f"Errore load_add_session: {e}")
            return {}

def save_add_session():
    with LOCK:
        try:
            with open(LOG_STATUS_FILE, 'w', encoding='utf-8') as f:
                json.dump(ADD_SESSION, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Errore save_add_session: {e}")

def create_telegram_client(phone_entry):
    session_file = os.path.join(SESSIONS_FOLDER, f"{phone_entry['phone']}.session")
    api_id = int(phone_entry['api_id'])
    api_hash = phone_entry['api_hash']
    return TelegramClient(session_file, api_id, api_hash)

# Carichiamo lo stato ADD_SESSION
ADD_SESSION = load_add_session()

def reset_daily_counters_if_needed(phone_entry):
    today_str = datetime.date.today().isoformat()
    if phone_entry.get('last_reset_date') != today_str:
        phone_entry['added_today'] = 0
        phone_entry['last_reset_date'] = today_str

def log(msg):
    with LOCK:
        ADD_SESSION['log'].append(msg)
        save_add_session()
    print(msg)

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
    with LOCK:
        if ADD_SESSION.get('running', False):
            msg = f"Nessun phone disponibile per {username}, sospendo finché non tornano almeno {min_phones} disponibili."
            log(msg)

    while True:
        with LOCK:
            if not ADD_SESSION.get('running', False):
                return
        current_avail = count_available_phones(phones)
        if current_avail >= min_phones:
            resume_msg = f"Sono nuovamente disponibili {current_avail} numeri: riprendo l'aggiunta di {username}."
            log(resume_msg)
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

def should_skip_user_by_last_seen(user_entity, skip_config):
    status = user_entity.status
    now = datetime.datetime.now()

    if not status:
        if skip_config.get('user_status_empty'):
            return True
        return False

    # Se offline con data e ora
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
# ROTTE FLASK
# ---------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

# GESTIONE NUMERI
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
    if not phone or not api_id or not api_hash:
        return jsonify({'error': 'Tutti i campi sono richiesti.'}), 400

    with LOCK:
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
    with LOCK:
        phones = load_phones()
        phones = [p for p in phones if p['phone'] != phone]
        save_phones(phones)
    return jsonify({'success': True})

@app.route('/api/phones/<phone>/pause', methods=['POST'])
def api_pause_phone(phone):
    data = request.json
    pause_state = data.get('paused', True)
    with LOCK:
        phones = load_phones()
        for p in phones:
            if p['phone'] == phone:
                p['paused'] = pause_state
                if not pause_state:
                    p['paused_until'] = None
                save_phones(phones)
                return jsonify({'success': True})
    return jsonify({'error': 'Phone not found'}), 404

# LOGIN E GESTIONE OTP
@app.route('/api/send_code', methods=['POST'])
def api_send_code():
    data = request.json
    phone = data.get('phone', '').strip()
    if not phone:
        return jsonify({'error': 'Il campo phone è richiesto.'}), 400

    with LOCK:
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
        with LOCK:
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
    phone = data.get('phone', '').strip()
    code = data.get('code', '').strip()
    if not phone or not code:
        return jsonify({'error': 'I campi phone e code sono richiesti.'}), 400

    with LOCK:
        if phone not in OTP_DICT:
            return jsonify({'error': 'No OTP session found for this phone'}), 400
        phone_code_hash = OTP_DICT[phone]['phone_code_hash']

    with LOCK:
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
        client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        with LOCK:
            del OTP_DICT[phone]
        return jsonify({'success': True})
    except errors.SessionPasswordNeededError:
        with LOCK:
            OTP_DICT[phone]['2fa_needed'] = True
            save_add_session()
        return jsonify({'error': 'SESSION_PASSWORD_NEEDED'}), 400
    except errors.PhoneCodeInvalidError:
        return jsonify({'error': 'Invalid code provided.'}), 400
    except errors.PhoneCodeExpiredError:
        return jsonify({'error': 'The code has expired.'}), 400
    except errors.PhoneNumberUnoccupiedError:
        return jsonify({'error': 'The phone number is not associated with any account.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        client.disconnect()

@app.route('/api/validate_password', methods=['POST'])
def api_validate_password():
    data = request.json
    phone = data.get('phone', '').strip()
    password = data.get('password', '').strip()

    with LOCK:
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
        with LOCK:
            del OTP_DICT[phone]
        return jsonify({'success': True})
    except errors.PasswordHashInvalidError:
        return jsonify({'error': 'Invalid 2FA password provided.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        client.disconnect()

# AGGIUNTA UTENTI AL GRUPPO (vecchia versione sincrona)
def add_users_to_group_thread(group_username, users_list):
    global ADD_SESSION
    with LOCK:
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
            'user_status_empty': ADD_SESSION.get('user_status_empty', False),
        }

    try:
        # Carichiamo i phones
        phones = load_phones()
        phone_clients = {}

        # Connettiamo i phone disponibili
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
            log("Nessun numero con sessione valida. Interruzione.")
            with LOCK:
                ADD_SESSION['running'] = False
                save_add_session()
            return

        # Recuperiamo l’entità del gruppo
        group_entities = {}
        for phone, client in list(phone_clients.items()):
            try:
                grp_ent = client.get_entity(group_username)
                if isinstance(grp_ent, (tl.types.Channel, tl.types.Chat)):
                    group_entities[phone] = grp_ent
                    log(f"[{phone}] Gruppo '{group_username}' risolto.")
                else:
                    log(f"[{phone}] '{group_username}' non è un canale/supergruppo. Pausa.")
                    set_phone_pause(phone, paused=True)
            except errors.UsernameNotOccupiedError:
                log(f"[{phone}] Gruppo '{group_username}' non esiste. Pausa.")
                set_phone_pause(phone, paused=True)
            except errors.ChannelPrivateError:
                log(f"[{phone}] Gruppo '{group_username}' è privato. Pausa.")
                set_phone_pause(phone, paused=True)
            except Exception as e:
                log(f"[{phone}] Impossibile recuperare gruppo '{group_username}': {e}. Pausa.")
                set_phone_pause(phone, paused=True)

        # Rimuoviamo i client che non hanno risolto il gruppo
        for phone in list(phone_clients.keys()):
            if phone not in group_entities:
                phone_clients[phone].disconnect()
                phone_clients.pop(phone)

        if not group_entities:
            log("Nessun client può accedere al gruppo. Interruzione.")
            with LOCK:
                ADD_SESSION['running'] = False
                save_add_session()
            for c in phone_clients.values():
                c.disconnect()
            return

        # Controlliamo se i phone sono dentro il gruppo
        for phone, client in phone_clients.items():
            try:
                me = client.get_me()
                client(GetParticipantRequest(group_entities[phone], me))
                log(f"[{phone}] Numero già nel gruppo.")
            except rpcerrorlist.UserNotParticipantError:
                try:
                    client(JoinChannelRequest(group_entities[phone]))
                    client(GetParticipantRequest(group_entities[phone], me))
                    log(f"[{phone}] Numero unito con successo al gruppo.")
                except Exception as e:
                    log(f"[{phone}] Errore join gruppo: {e}. Pausa.")
                    set_phone_pause(phone, paused=True)
            except Exception as e:
                log(f"[{phone}] Errore get_participant: {e}. Pausa.")
                set_phone_pause(phone, paused=True)

        # Rimuoviamo i phone che non si sono uniti
        for phone in list(phone_clients.keys()):
            ph_list = load_phones()
            for px in ph_list:
                if px['phone'] == phone and px['paused']:
                    phone_clients[phone].disconnect()
                    phone_clients.pop(phone)
                    break

        if not phone_clients:
            log("Nessun client disponibile dopo join. Interruzione.")
            with LOCK:
                ADD_SESSION['running'] = False
                save_add_session()
            return

        phone_list = sorted(list(phone_clients.keys()))
        phone_index = 0
        with LOCK:
            i = ADD_SESSION.get('last_user_index', 0)

        # Loop sugli utenti
        while i < len(users_list):
            with LOCK:
                if not ADD_SESSION.get('running', False):
                    break
            username = users_list[i].strip()
            if not username:
                with LOCK:
                    i += 1
                    ADD_SESSION['last_user_index'] = i
                    save_add_session()
                continue

            # Cerchiamo un phone disponibile
            attempts = 0
            selected_phone = None
            while attempts < len(phone_list):
                cur_phone = phone_list[phone_index]
                phone_index = (phone_index + 1) % len(phone_list)
                attempts += 1

                if cur_phone not in phone_clients:
                    continue

                ph_list = load_phones()
                p_entry = None
                for px in ph_list:
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
                with LOCK:
                    if not ADD_SESSION.get('running', False):
                        break
                continue

            client = phone_clients[selected_phone]
            group_entity = group_entities[selected_phone]

            # Risolviamo utente
            try:
                user_entity = client.get_entity(username)
            except errors.UsernameNotOccupiedError:
                log(f"[{selected_phone}] {username}: username non esiste. Skip.")
                with LOCK:
                    i += 1
                    ADD_SESSION['last_user_index'] = i
                    save_add_session()
                continue
            except Exception as ex:
                err_msg = str(ex)
                log(f"[{selected_phone}] Errore get_entity({username}): {err_msg}. Skip.")
                if 'A wait of' in err_msg and 'seconds is required' in err_msg:
                    try:
                        parts = err_msg.split('A wait of')[1].split('seconds is required')[0].strip()
                        wait_seconds = int(parts)
                        set_phone_pause(selected_phone, paused=True, seconds=wait_seconds)
                        log(f"[{selected_phone}] Pausa {wait_seconds}s (FloodWait).")
                    except:
                        pass
                with LOCK:
                    i += 1
                    ADD_SESSION['last_user_index'] = i
                    save_add_session()
                continue

            # Skip se last seen vietato
            if should_skip_user_by_last_seen(user_entity, skip_config):
                log(f"[{selected_phone}] {username}: skip per impostazioni last seen (status={user_entity.status}).")
                with LOCK:
                    i += 1
                    ADD_SESSION['last_user_index'] = i
                    save_add_session()
                continue

            # Verifica se utente è già nel gruppo
            try:
                client(GetParticipantRequest(group_entity, user_entity))
                log(f"[{selected_phone}] {username}: già nel gruppo. Skip.")
                with LOCK:
                    i += 1
                    ADD_SESSION['last_user_index'] = i
                    save_add_session()
                continue
            except rpcerrorlist.UserNotParticipantError:
                pass
            except Exception as e:
                log(f"[{selected_phone}] Errore get_participant {username}: {e}. Skip.")
                with LOCK:
                    i += 1
                    ADD_SESSION['last_user_index'] = i
                    save_add_session()
                continue

            # Aggiunta
            try:
                client(InviteToChannelRequest(group_entity, [user_entity]))
                update_phone_stats(selected_phone, added=1, total=1)
                with LOCK:
                    ADD_SESSION['total_added'] += 1
                log(f"[{selected_phone}] Aggiunto con successo -> {username}")

                # Check se effettivamente nel gruppo
                try:
                    client(GetParticipantRequest(group_entity, user_entity))
                    log(f"[{selected_phone}] {username}: confermato nel gruppo.")
                except:
                    log(f"[{selected_phone}] ERRORE: {username} non risulta nel gruppo dopo l'aggiunta.")
                    update_phone_stats(selected_phone, non_result_err_inc=1)
                    ph_after = load_phones()
                    for xx in ph_after:
                        if xx['phone'] == selected_phone:
                            if xx['non_result_errors'] >= max_non_result_errors:
                                set_phone_pause(selected_phone, paused=True, days=days_pause_non_result_errors)
                                xx['non_result_errors'] = 0
                                save_phones(ph_after)
                                log(f"[{selected_phone}] Raggiunta soglia errori: pausa {days_pause_non_result_errors} giorni.")

                time.sleep(sleep_seconds)

            except errors.FloodWaitError as e:
                wait_seconds = e.seconds
                log(f"[{selected_phone}] FloodWaitError, pausa {wait_seconds}s.")
                set_phone_pause(selected_phone, paused=True, seconds=wait_seconds)
                time.sleep(wait_seconds)
            except errors.PeerFloodError:
                log(f"[{selected_phone}] PeerFloodError: spam. Pausa 2min.")
                set_phone_pause(selected_phone, paused=True, seconds=120)
                time.sleep(120)
            except errors.UserPrivacyRestrictedError:
                log(f"[{selected_phone}] {username}: Privacy restricted. Skip.")
            except errors.UserNotMutualContactError:
                log(f"[{selected_phone}] {username}: non mutuo contatto. Skip.")
            except Exception as ex:
                err_msg = str(ex)
                log(f"[{selected_phone}] Errore sconosciuto su {username}: {err_msg}. Skip.")
                if 'A wait of' in err_msg and 'seconds is required' in err_msg:
                    try:
                        parts = err_msg.split('A wait of')[1].split('seconds is required')[0].strip()
                        wait_seconds = int(parts)
                        set_phone_pause(selected_phone, paused=True, seconds=wait_seconds)
                        log(f"[{selected_phone}] Pausa {wait_seconds}s (FloodWait).")
                    except:
                        pass

            with LOCK:
                i += 1
                ADD_SESSION['last_user_index'] = i
                save_add_session()

        # Fine loop
        with LOCK:
            if i >= len(users_list):
                ADD_SESSION['last_user_index'] = 0
                log("Lista utenti terminata. last_user_index -> 0.")

    finally:
        # Disconnettiamo i client
        for c in phone_clients.values():
            try:
                c.disconnect()
            except:
                pass
        with LOCK:
            ADD_SESSION['running'] = False
            log("Operazione di aggiunta terminata.")
            save_add_session()

@app.route('/api/start_adding', methods=['POST'])
def api_start_adding():
    global ADD_SESSION
    with LOCK:
        if ADD_SESSION.get('running', False):
            return jsonify({'error': "Un'operazione di aggiunta è già in corso."}), 400

    data = request.json
    group_username = data.get('group_username', '').strip()
    user_list_raw = data.get('user_list', '').strip()
    users_list = [u.strip() for u in user_list_raw.splitlines() if u.strip()]

    if not group_username or not users_list:
        return jsonify({'error': 'Dati insufficienti (gruppo o lista utenti vuota).'}), 400

    skip_options = data.get('skip_options', [])
    last_seen_gt_1_day = 'last_seen_gt_1_day' in skip_options
    last_seen_gt_7_days = 'last_seen_gt_7_days' in skip_options
    last_seen_gt_30_days = 'last_seen_gt_30_days' in skip_options
    last_seen_gt_60_days = 'last_seen_gt_60_days' in skip_options
    user_status_empty = 'user_status_empty' in skip_options

    min_phones_available = data.get('min_phones_available', 1)
    max_non_result_errors = data.get('max_non_result_errors', 3)
    days_pause_non_result_errors = data.get('days_pause_non_result_errors', 2)
    sleep_seconds = data.get('sleep_seconds', 10)

    with LOCK:
        ADD_SESSION['running'] = True
        ADD_SESSION['group'] = group_username
        ADD_SESSION['total_added'] = 0
        ADD_SESSION['log'] = []
        ADD_SESSION['start_time'] = time.time()
        ADD_SESSION['paused_due_to_limit'] = False
        ADD_SESSION['paused_until'] = None

        if 'last_user_index' not in ADD_SESSION:
            ADD_SESSION['last_user_index'] = 0

        ADD_SESSION['min_phones_available'] = min_phones_available
        ADD_SESSION['max_non_result_errors'] = max_non_result_errors
        ADD_SESSION['days_pause_non_result_errors'] = days_pause_non_result_errors
        ADD_SESSION['sleep_seconds'] = sleep_seconds
        ADD_SESSION['last_seen_gt_1_day'] = last_seen_gt_1_day
        ADD_SESSION['last_seen_gt_7_days'] = last_seen_gt_7_days
        ADD_SESSION['last_seen_gt_30_days'] = last_seen_gt_30_days
        ADD_SESSION['last_seen_gt_60_days'] = last_seen_gt_60_days
        ADD_SESSION['user_status_empty'] = user_status_empty

        save_add_session()

    t = threading.Thread(target=add_users_to_group_thread, args=(group_username, users_list))
    t.start()

    return jsonify({'success': True})

@app.route('/api/log_status', methods=['GET'])
def api_log_status():
    with LOCK:
        return jsonify({
            'running': ADD_SESSION.get('running', False),
            'group': ADD_SESSION.get('group', ''),
            'total_added': ADD_SESSION.get('total_added', 0),
            'log': ADD_SESSION.get('log', []),
        })

@app.route('/api/stop_adding', methods=['POST'])
def api_stop_adding():
    global ADD_SESSION
    with LOCK:
        if ADD_SESSION.get('running', False):
            ADD_SESSION['running'] = False
            ADD_SESSION['log'].append("Operazione fermata manualmente dall'utente.")
            save_add_session()
            return jsonify({"success": True, "message": "Operazione fermata con successo."})
        else:
            return jsonify({"success": False, "message": "Nessuna operazione in corso"}), 400

# RIEPILOGO
@app.route('/api/summary', methods=['GET'])
def api_summary():
    with LOCK:
        phones = load_phones()
        session_total = ADD_SESSION.get('total_added', 0)

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

    return jsonify({
        'session_added_total': session_total,
        'phones': summary_list
    })

# CARICAMENTO FILE EXCEL
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
        return jsonify({'error': f'Formato non supportato. Estensioni valide: {valid_exts}'}), 400

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    try:
        file.save(filepath)
    except Exception as e:
        return jsonify({'error': f'Errore salvataggio file: {e}'}), 400

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
        os.remove(filepath)
        return jsonify({'error': f'Errore lettura Excel: {e}'}), 400

    return jsonify({'user_list': '\n'.join(usernames)})

# RUN FLASK
if __name__ == '__main__':
    # Host 0.0.0.0 per accettare connessioni dall’esterno
    app.run(host='0.0.0.0', port=5050, debug=True, threaded=False, use_reloader=False)
