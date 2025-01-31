// -------------------------------------------------------
// Mostra la sezione selezionata
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

    phones.forEach(phoneObj => {
      let row = document.createElement('tr');
      let pausedText = phoneObj.paused ? 'Yes' : 'No';
      let addedToday = phoneObj.added_today || 0;
      let totalAdded = phoneObj.total_added || 0;

      let floodTimeFormatted = '0';
      if (typeof phoneObj.flood_time !== 'undefined' && phoneObj.flood_time > 0) {
        floodTimeFormatted = formatSeconds(phoneObj.flood_time);
      }

      row.innerHTML = `
        <td>${phoneObj.phone}</td>
        <td>${phoneObj.api_id}</td>
        <td>${pausedText}</td>
        <td>${floodTimeFormatted}</td>
        <td>${addedToday}</td>
        <td>${totalAdded}</td>
        <td>
          <button onclick="pausePhone('${phoneObj.phone}', ${!phoneObj.paused})">
            ${phoneObj.paused ? 'Unpause' : 'Pause'}
          </button>
          <button onclick="removePhone('${phoneObj.phone}')">Remove</button>
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
      alert('Numero salvato correttamente!');
      document.getElementById('phone').value = '';
      document.getElementById('api_id').value = '';
      document.getElementById('api_hash').value = '';
      loadPhones();
    }
  } catch (err) {
    console.error('Errore salvando phone:', err);
    alert('Errore nel salvataggio del numero');
  }
}

async function removePhone(phone) {
  if (!confirm(`Sei sicuro di voler rimuovere ${phone}?`)) return;
  try {
    let res = await fetch(`/api/phones/${encodeURIComponent(phone)}`, {
      method: 'DELETE'
    });
    let data = await res.json();
    if (data.success) {
      alert('Numero rimosso correttamente!');
      loadPhones();
    } else {
      alert('Errore nella rimozione del numero');
    }
  } catch (err) {
    console.error('Errore rimuovendo phone:', err);
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
      alert('Errore nel cambio di stato del numero');
    }
  } catch (err) {
    console.error('Errore pausing/unpausing phone:', err);
  }
}

// -------------------------------------------------------
// LOG IN (OTP)
// -------------------------------------------------------
async function sendCode() {
  let phone = document.getElementById('phone-login').value.trim();
  if (!phone) {
    alert('Inserisci un numero');
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
      logDiv.innerHTML += `<p>Codice inviato con successo a ${phone}. Inseriscilo.</p>`;
      document.getElementById('otp-section').style.display = 'block';
    } else {
      logDiv.innerHTML += `<p>Errore: ${data.error}</p>`;
    }
  } catch (err) {
    console.error('Errore sendCode:', err);
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
    } else {
      if (data.error === 'SESSION_PASSWORD_NEEDED') {
        logDiv.innerHTML += `<p>Questo account ha la 2FA. Inserisci la password.</p>`;
        document.getElementById('password-section').style.display = 'block';
      } else {
        logDiv.innerHTML += `<p>Errore: ${data.error}</p>`;
      }
    }
  } catch (err) {
    console.error('Errore validateCode:', err);
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
      logDiv.innerHTML += `<p>Login completato con password 2FA per ${phone}!</p>`;
      document.getElementById('password-section').style.display = 'none';
      document.getElementById('twofa-password').value = '';
    } else {
      logDiv.innerHTML += `<p>Errore: ${data.error}</p>`;
    }
  } catch (err) {
    console.error('Errore validatePassword:', err);
    logDiv.innerHTML += `<p>Eccezione: ${err}</p>`;
  }
}

// -------------------------------------------------------
// UPLOAD EXCEL
// -------------------------------------------------------
async function uploadExcelFile() {
  let fileInput = document.getElementById('excel_file');
  if (!fileInput.files || fileInput.files.length === 0) {
    alert('Seleziona un file Excel prima di cliccare "Carica Excel"');
    return;
  }

  let file = fileInput.files[0];
  console.log('Carico il file Excel:', file.name);

  let formData = new FormData();
  formData.append('excel_file', file);

  try {
    let res = await fetch('/api/upload_excel', {
      method: 'POST',
      body: formData
    });
    let data = await res.json();
    if (data.error) {
      console.error('Errore upload_excel:', data.error);
      alert('Errore caricamento Excel: ' + data.error);
    } else {
      let user_list = data.user_list;
      let ta = document.getElementById('user_list');
      if (ta.value.trim()) {
        ta.value += '\n' + user_list;
      } else {
        ta.value = user_list;
      }
      alert('Lista utenti caricata dall\'Excel!');
    }
  } catch (err) {
    console.error('Errore caricando il file Excel:', err);
    alert('Errore caricando il file Excel.');
  }
}

// -------------------------------------------------------
// ADD USERS
// -------------------------------------------------------
async function startAdding() {
  let group_username = document.getElementById('group_username').value.trim();
  let user_list = document.getElementById('user_list').value;
  let addLogDiv = document.getElementById('add-log');

  let min_phones_available = parseInt(document.getElementById('min_phones_available').value) || 1;
  let max_non_result_errors = parseInt(document.getElementById('max_non_result_errors').value) || 3;
  let days_pause_non_result_errors = parseInt(document.getElementById('days_pause_non_result_errors').value) || 2;
  let sleep_seconds = parseInt(document.getElementById('sleep_seconds').value) || 10;

  let skipOptionsSelect = document.getElementById('skip_options');
  let selectedOptions = Array.from(skipOptionsSelect.selectedOptions).map(option => option.value);

  if (!group_username || !user_list) {
    alert('Compila il campo gruppo e la lista utenti.');
    return;
  }

  addLogDiv.innerHTML += `<p>Inizio procedura di aggiunta al gruppo ${group_username}...</p>`;

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
        skip_options: selectedOptions
      })
    });
    let data = await res.json();
    if (data.success) {
      addLogDiv.innerHTML += `<p>Thread di aggiunta avviato.</p>`;
      pollAddLog();
    } else {
      addLogDiv.innerHTML += `<p>Errore: ${data.error}</p>`;
    }
  } catch (err) {
    console.error('Errore startAdding:', err);
    addLogDiv.innerHTML += `<p>Eccezione: ${err}</p>`;
  }
}

async function stopAdding() {
  try {
    let res = await fetch('/api/stop_adding', {
      method: 'POST'
    });
    let data = await res.json();
    if (data.success) {
      alert(data.message);
    } else {
      alert(data.message);
    }
  } catch (err) {
    console.error('Errore stopAdding:', err);
    alert("Errore fermando l'operazione.");
  }
}

let pollingInterval = null;

function pollAddLog() {
  if (pollingInterval) {
    clearInterval(pollingInterval);
  }
  pollingInterval = setInterval(async () => {
    try {
      let res = await fetch('/api/log_status');
      let data = await res.json();
      let addLogDiv = document.getElementById('add-log');
      addLogDiv.innerHTML = '';
      data.log.forEach(line => {
        addLogDiv.innerHTML += `<p>${line}</p>`;
      });
      if (!data.running) {
        clearInterval(pollingInterval);
        pollingInterval = null;
        addLogDiv.innerHTML += `<p>Operazione terminata. Totale aggiunti: ${data.total_added}</p>`;
        loadPhones();
        loadSummary();
      }
    } catch (err) {
      console.error('Errore pollAddLog:', err);
    }
  }, 3000);
}

// -------------------------------------------------------
// SUMMARY
// -------------------------------------------------------
async function loadSummary() {
  try {
    let res = await fetch('/api/summary');
    let data = await res.json();
    let summaryTable = document.querySelector('#summary-table tbody');
    summaryTable.innerHTML = '';

    document.getElementById('session-added-info').innerText =
      `In questa sessione sono stati aggiunti in totale: ${data.session_added_total} utenti.`;

    data.phones.forEach(p => {
      let row = document.createElement('tr');
      let pausedText = p.paused ? 'Yes' : 'No';
      let addedToday = p.added_today || 0;
      let totalAdded = p.total_added || 0;

      let floodTimeFormatted = '0';
      if (p.flood_time > 0) {
        floodTimeFormatted = formatSeconds(p.flood_time);
      }

      row.innerHTML = `
        <td>${p.phone}</td>
        <td>${addedToday}</td>
        <td>${totalAdded}</td>
        <td>${pausedText}</td>
        <td>${floodTimeFormatted}</td>
      `;
      summaryTable.appendChild(row);
    });
  } catch (err) {
    console.error('Errore loadSummary:', err);
  }
}

function formatSeconds(seconds) {
  let hrs = Math.floor(seconds / 3600);
  let mins = Math.floor((seconds % 3600) / 60);
  let secs = seconds % 60;
  return `${pad(hrs)}:${pad(mins)}:${pad(secs)}`;
}

function pad(num) {
  return num.toString().padStart(2, '0');
}

// -------------------------------------------------------
// RIAVVIO TMUX
// -------------------------------------------------------
async function restartTmux() {
  if (!confirm("Sei sicuro di voler riavviare la sessione TMUX?")) return;
  try {
    let res = await fetch('/api/restart_tmux', { method: 'POST' });
    let data = await res.json();
    if (data.success) {
      alert(data.message || "Sessione TMUX riavviata con successo!");
    } else {
      alert("Errore nel riavviare TMUX: " + data.message);
    }
  } catch (err) {
    console.error('Errore restartTmux:', err);
    alert("Errore nel riavviare la sessione TMUX.");
  }
}

// -------------------------------------------------------
window.addEventListener('DOMContentLoaded', () => {
  showSection('manage-numbers');
  loadPhones();
  loadSummary();

  (async () => {
    try {
      let res = await fetch('/api/log_status');
      let data = await res.json();
      if (data.running) {
        pollAddLog();
      }
    } catch (err) {
      console.error('Errore checking log_status:', err);
    }
  })();
});
