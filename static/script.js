// -------------------------------------------------------
// Navigazione fra le sezioni
// -------------------------------------------------------
function showSection(id) {
  document.querySelectorAll('section').forEach(section => section.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

// -------------------------------------------------------
// GESTIONE NUMERI
// -------------------------------------------------------
async function loadPhones() {
  try {
    let res = await fetch('/api/phones');
    let phones = await res.json();
    let tbody = document.querySelector('#phones-table tbody');
    tbody.innerHTML = '';

    phones.forEach(p => {
      let pausedText = p.paused ? 'Yes' : 'No';
      let floodTimeFormatted = p.flood_time > 0 ? formatSeconds(p.flood_time) : '0';

      let row = document.createElement('tr');
      row.innerHTML = `
        <td>${p.phone}</td>
        <td>${p.api_id}</td>
        <td>${pausedText}</td>
        <td>${floodTimeFormatted}</td>
        <td>${p.added_today}</td>
        <td>${p.total_added}</td>
        <td>
          <button onclick="pausePhone('${p.phone}', ${!p.paused})">
            ${p.paused ? 'Unpause' : 'Pause'}
          </button>
          <button onclick="removePhone('${p.phone}')">Remove</button>
        </td>
      `;
      tbody.appendChild(row);
    });
  } catch (err) {
    console.error('Errore caricando phones:', err);
  }
}

async function addPhone() {
  let phone = document.getElementById('phone').value.trim();
  let api_id = document.getElementById('api_id').value.trim();
  let api_hash = document.getElementById('api_hash').value.trim();

  if (!phone || !api_id || !api_hash) {
    alert('Compila tutti i campi');
    return;
  }

  try {
    let res = await fetch('/api/phones', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone, api_id, api_hash })
    });
    let data = await res.json();
    if (data.error) {
      alert('Errore: ' + data.error);
    } else {
      alert('Numero inserito correttamente!');
      document.getElementById('phone').value = '';
      document.getElementById('api_id').value = '';
      document.getElementById('api_hash').value = '';
      loadPhones();
    }
  } catch (err) {
    console.error('Errore addPhone:', err);
  }
}

async function removePhone(phone) {
  if (!confirm(`Rimuovere il numero ${phone}?`)) return;
  try {
    let res = await fetch(`/api/phones/${encodeURIComponent(phone)}`, {
      method: 'DELETE'
    });
    let data = await res.json();
    if (data.success) {
      loadPhones();
    } else {
      alert('Errore nella rimozione del numero');
    }
  } catch (err) {
    console.error('Errore removePhone:', err);
  }
}

async function pausePhone(phone, pauseState) {
  try {
    let res = await fetch(`/api/phones/${encodeURIComponent(phone)}/pause`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paused: pauseState })
    });
    let data = await res.json();
    if (data.success) {
      loadPhones();
    } else {
      alert('Errore nel cambio di stato');
    }
  } catch (err) {
    console.error('Errore pausePhone:', err);
  }
}

// -------------------------------------------------------
// LOG IN (OTP)
// -------------------------------------------------------
async function sendCode() {
  let phone = document.getElementById('phone-login').value.trim();
  if (!phone) {
    alert('Inserisci il numero');
    return;
  }
  let logDiv = document.getElementById('login-log');
  logDiv.innerHTML += `<p>Invio codice a ${phone}...</p>`;

  try {
    let res = await fetch('/api/send_code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone })
    });
    let data = await res.json();
    if (data.success) {
      logDiv.innerHTML += `<p>Codice inviato. Inseriscilo nel box OTP.</p>`;
      document.getElementById('otp-section').style.display = 'block';
    } else {
      logDiv.innerHTML += `<p>Errore: ${data.error}</p>`;
    }
  } catch (err) {
    logDiv.innerHTML += `<p>Eccezione: ${err}</p>`;
  }
}

async function validateCode() {
  let phone = document.getElementById('phone-login').value.trim();
  let code = document.getElementById('otp-code').value.trim();
  let logDiv = document.getElementById('login-log');
  logDiv.innerHTML += `<p>Verifico codice per ${phone}...</p>`;

  try {
    let res = await fetch('/api/validate_code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone, code })
    });
    let data = await res.json();
    if (data.success) {
      logDiv.innerHTML += `<p>Login completato per ${phone}!</p>`;
      document.getElementById('otp-section').style.display = 'none';
      document.getElementById('otp-code').value = '';
    } else if (data.error === 'SESSION_PASSWORD_NEEDED') {
      logDiv.innerHTML += `<p>2FA attiva. Inserisci la password 2FA.</p>`;
      document.getElementById('password-section').style.display = 'block';
    } else {
      logDiv.innerHTML += `<p>Errore: ${data.error}</p>`;
    }
  } catch (err) {
    logDiv.innerHTML += `<p>Eccezione: ${err}</p>`;
  }
}

async function validatePassword() {
  let phone = document.getElementById('phone-login').value.trim();
  let password = document.getElementById('twofa-password').value.trim();
  let logDiv = document.getElementById('login-log');

  try {
    let res = await fetch('/api/validate_password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone, password })
    });
    let data = await res.json();
    if (data.success) {
      logDiv.innerHTML += `<p>Login completato (2FA) per ${phone}!</p>`;
      document.getElementById('password-section').style.display = 'none';
      document.getElementById('twofa-password').value = '';
    } else {
      logDiv.innerHTML += `<p>Errore: ${data.error}</p>`;
    }
  } catch (err) {
    logDiv.innerHTML += `<p>Eccezione: ${err}</p>`;
  }
}

// -------------------------------------------------------
// UPLOAD EXCEL
// -------------------------------------------------------
async function uploadExcelFile() {
  let fileInput = document.getElementById('excel_file');
  if (!fileInput.files || fileInput.files.length === 0) {
    alert('Nessun file Excel selezionato');
    return;
  }
  let file = fileInput.files[0];

  let formData = new FormData();
  formData.append('excel_file', file);

  try {
    let res = await fetch('/api/upload_excel', {
      method: 'POST',
      body: formData
    });
    let data = await res.json();
    if (data.error) {
      alert('Errore: ' + data.error);
    } else {
      let ta = document.getElementById('user_list');
      if (ta.value.trim()) {
        ta.value += '\n' + data.user_list;
      } else {
        ta.value = data.user_list;
      }
      alert('Caricata la lista utenti dall’Excel!');
    }
  } catch (err) {
    console.error('Errore uploadExcelFile:', err);
  }
}

// -------------------------------------------------------
// AGGIUNTA UTENTI
// -------------------------------------------------------
async function startAdding() {
  let group_username = document.getElementById('group_username').value.trim();
  let user_list = document.getElementById('user_list').value.trim();

  let min_phones_available = parseInt(document.getElementById('min_phones_available').value) || 1;
  let max_non_result_errors = parseInt(document.getElementById('max_non_result_errors').value) || 3;
  let days_pause_non_result_errors = parseInt(document.getElementById('days_pause_non_result_errors').value) || 2;
  let sleep_seconds = parseInt(document.getElementById('sleep_seconds').value) || 10;

  let skipSelect = document.getElementById('skip_options');
  let selected = Array.from(skipSelect.selectedOptions).map(opt => opt.value);

  if (!group_username || !user_list) {
    alert('Dati insufficienti (gruppo o lista utenti vuota).');
    return;
  }

  try {
    let res = await fetch('/api/start_adding', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        group_username,
        user_list,
        min_phones_available,
        max_non_result_errors,
        days_pause_non_result_errors,
        sleep_seconds,
        skip_options: selected
      })
    });
    let data = await res.json();
    if (data.success) {
      alert('Operazione di aggiunta avviata! Vai su /log per i dettagli.');
    } else {
      alert(`Errore: ${data.error}`);
    }
  } catch (err) {
    alert('Eccezione: ' + err);
  }
}

async function stopAdding() {
  try {
    let res = await fetch('/api/stop_adding', { method: 'POST' });
    let data = await res.json();
    if (data.success) {
      alert(data.message);
    } else {
      alert(data.message);
    }
  } catch (err) {
    console.error('Errore stopAdding:', err);
  }
}

// -------------------------------------------------------
// RIEPILOGO
// -------------------------------------------------------
async function loadSummary() {
  try {
    let res = await fetch('/api/summary');
    let data = await res.json();
    document.getElementById('session-added-info').innerText =
      `In questa sessione aggiunti: ${data.session_added_total} utenti.`;

    let tbody = document.querySelector('#summary-table tbody');
    tbody.innerHTML = '';

    data.phones.forEach(p => {
      let pausedText = p.paused ? 'Yes' : 'No';
      let floodTimeFormatted = p.flood_time > 0 ? formatSeconds(p.flood_time) : '0';

      let row = document.createElement('tr');
      row.innerHTML = `
        <td>${p.phone}</td>
        <td>${p.added_today}</td>
        <td>${p.total_added}</td>
        <td>${pausedText}</td>
        <td>${floodTimeFormatted}</td>
        <td>${p.pause_reason}</td>
      `;
      tbody.appendChild(row);
    });
  } catch (err) {
    console.error('Errore loadSummary:', err);
  }
}

// -------------------------------------------------------
// FORMATTING UTILS
// -------------------------------------------------------
function formatSeconds(sec) {
  let h = Math.floor(sec / 3600);
  let m = Math.floor((sec % 3600) / 60);
  let s = sec % 60;
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}
function pad(n) {
  return n.toString().padStart(2, '0');
}

// -------------------------------------------------------
// RIAVVIO TMUX
// -------------------------------------------------------
async function restartTmux() {
  if (!confirm('Sei sicuro di voler riavviare la sessione tmux "mioadder"?')) return;
  try {
    let res = await fetch('/api/restart_tmux', { method: 'POST' });
    let data = await res.json();
    if (data.success) {
      alert(data.message);
    } else {
      alert('Errore nel riavvio TMUX: ' + data.message);
    }
  } catch (err) {
    alert('Eccezione riavviando TMUX: ' + err);
  }
}

// -------------------------------------------------------
window.addEventListener('DOMContentLoaded', () => {
  showSection('manage-numbers');
  loadPhones();
  loadSummary();
});
