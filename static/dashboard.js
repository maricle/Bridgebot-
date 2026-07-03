let chartDia = null, chartProd = null;
let _modoHistorial = 'tel';
let _currentUserId = null;
let _archivosData  = [];

// ── UTILS ─────────────────────────────────────────────────────────────────────
function hoy() { return new Date().toISOString().split('T')[0]; }
function haceDias(n) {
  const d = new Date(); d.setDate(d.getDate() - n);
  return d.toISOString().split('T')[0];
}
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}
function canalBadge(canal) {
  if (canal === 'whatsapp')  return '<span class="badge wa">WA</span>';
  if (canal === 'instagram') return '<span class="badge ig">IG</span>';
  return '';
}
function esWhatsApp(canal, userId) {
  return canal === 'whatsapp' || (!canal && /^\d{10,15}$/.test(userId));
}
function renderConversacion(historial) {
  if (!historial.length) return '<span class="empty">Sin mensajes registrados.</span>';
  return historial.map(m => `
    <div class="msg ${m.rol}">
      ${escHtml(m.contenido)}
      <div class="msg-meta">${m.creado_en || ''}</div>
    </div>`).join('');
}
function renderClienteInfo(c, userId) {
  return `
    <div class="cliente-info">
      <div class="ci-item"><div class="ci-label">Nombre</div><div class="ci-val">${escHtml(c.nombre || '—')}</div></div>
      <div class="ci-item"><div class="ci-label">Teléfono</div><div class="ci-val">${escHtml(c.telefono || userId)}</div></div>
      <div class="ci-item"><div class="ci-label">Email</div><div class="ci-val">${escHtml(c.email || '—')}</div></div>
      <div class="ci-item"><div class="ci-label">Canal</div><div class="ci-val">${escHtml(c.canal || '—')}</div></div>
    </div>`;
}
function mostrarConversacion(d) {
  _currentUserId = d.user_id;
  const c = d.cliente;
  document.getElementById('resultado-historial').innerHTML =
    renderClienteInfo(c, d.user_id) +
    `<div class="conversacion" id="conv-box">${renderConversacion(d.historial)}</div>`;
  const box = document.getElementById('conv-box');
  if (box) box.scrollTop = box.scrollHeight;
  document.getElementById('reply-box').style.display =
    esWhatsApp(c.canal, d.user_id) ? 'flex' : 'none';
  document.getElementById('reply-texto').value = '';
}

// ── TABS ──────────────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + tab).classList.add('active');
  event.target.classList.add('active');
  document.getElementById('filtro-analytics').style.display = tab === 'analytics' ? 'flex' : 'none';
  if (tab === 'archivos') cargarArchivos();
}

// ── ANALYTICS ─────────────────────────────────────────────────────────────────
async function cargarAnalytics() {
  const desde = document.getElementById('desde').value;
  const hasta  = document.getElementById('hasta').value;
  document.getElementById('estado-analytics').textContent = 'Cargando...';
  try {
    const res = await fetch(`/analytics?desde=${desde}&hasta=${hasta}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();

    document.getElementById('estado-analytics').textContent = `Período: ${desde} → ${hasta}`;
    document.getElementById('c-total').textContent     = d.total_clientes;
    document.getElementById('c-pct').textContent       = d.precio.porcentaje + '%';
    document.getElementById('c-precio-n').textContent  = d.precio.cantidad + ' clientes';
    document.getElementById('c-leads').textContent     = d.total_leads;
    const conv = d.total_clientes
      ? Math.round(d.total_leads / d.total_clientes * 100) + '%' : '—';
    document.getElementById('c-conv').textContent = conv + ' de conversión';

    if (chartDia) chartDia.destroy();
    chartDia = new Chart(document.getElementById('chartDia'), {
      type: 'bar',
      data: {
        labels: d.clientes_por_dia.map(x => x.dia),
        datasets: [{ label: 'Clientes', data: d.clientes_por_dia.map(x => x.total),
          backgroundColor: 'rgba(79,70,229,.25)', borderColor: '#4f46e5',
          borderWidth: 2, borderRadius: 5 }]
      },
      options: { responsive: true, plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { stepSize: 1, precision: 0 } },
                  x: { ticks: { maxRotation: 45 } } } }
    });

    const prods = d.productos.slice(0, 12);
    if (chartProd) chartProd.destroy();
    chartProd = new Chart(document.getElementById('chartProd'), {
      type: 'bar',
      data: {
        labels: prods.map(x => x.producto),
        datasets: [{ label: 'Clientes', data: prods.map(x => x.clientes),
          backgroundColor: 'rgba(16,185,129,.25)', borderColor: '#10b981',
          borderWidth: 2, borderRadius: 5 }]
      },
      options: { indexAxis: 'y', responsive: true, plugins: { legend: { display: false } },
        scales: { x: { beginAtZero: true, ticks: { stepSize: 1, precision: 0 } } } }
    });
  } catch (e) {
    document.getElementById('estado-analytics').textContent = 'Error: ' + e.message;
  }
}

// ── HISTORIAL ─────────────────────────────────────────────────────────────────
function setMode(modo) {
  _modoHistorial = modo;
  document.getElementById('mode-tel').classList.toggle('active', modo === 'tel');
  document.getElementById('mode-txt').classList.toggle('active', modo === 'txt');
  document.getElementById('input-telefono').style.display  = modo === 'tel' ? '' : 'none';
  document.getElementById('input-contenido').style.display = modo === 'txt' ? '' : 'none';
  document.getElementById('estado-historial').textContent = '';
  document.getElementById('lista-resultados').style.display = 'none';
  document.getElementById('lista-resultados').innerHTML = '';
  document.getElementById('resultado-historial').innerHTML = '';
  document.getElementById('reply-box').style.display = 'none';
}

function ejecutarBusqueda() {
  if (_modoHistorial === 'tel') buscarHistorial();
  else buscarContenido();
}

async function buscarHistorial() {
  const tel = document.getElementById('input-telefono').value.trim();
  if (!tel) return;
  const estado = document.getElementById('estado-historial');
  estado.textContent = 'Buscando...';
  document.getElementById('resultado-historial').innerHTML = '';
  try {
    const res = await fetch(`/buscar?telefono=${encodeURIComponent(tel)}`);
    const d = await res.json();
    if (!d.encontrado) {
      estado.textContent = `No se encontró ningún usuario con el teléfono "${tel}".`;
      return;
    }
    estado.textContent = '';
    mostrarConversacion(d);
  } catch (e) {
    estado.textContent = 'Error: ' + e.message;
  }
}

async function buscarContenido() {
  const q = document.getElementById('input-contenido').value.trim();
  if (!q) return;
  const estado = document.getElementById('estado-historial');
  const lista  = document.getElementById('lista-resultados');
  estado.textContent = 'Buscando...';
  lista.style.display = 'none';
  document.getElementById('resultado-historial').innerHTML = '';
  document.getElementById('reply-box').style.display = 'none';
  try {
    const res = await fetch(`/buscar-contenido?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    if (!data.length) {
      estado.textContent = `Sin conversaciones con "${q}".`;
      return;
    }
    estado.textContent = `${data.length} conversación(es) con "${q}". Hacé clic para ver.`;
    lista.style.display = 'block';
    lista.innerHTML = '<div class="resultados-lista">' + data.map(r => `
      <div class="resultado-card" onclick='cargarConversacionDirecta(${JSON.stringify(r.ig_user_id)})'>
        ${canalBadge(r.canal)}
        <div>
          <div class="rc-nombre">${escHtml(r.nombre || '—')}</div>
          <div class="rc-tel">${escHtml(r.telefono || r.ig_user_id || '—')}</div>
        </div>
        <div class="rc-fecha">${(r.ultimo_mensaje || '').substring(0, 16).replace('T', ' ')}</div>
      </div>`).join('') + '</div>';
  } catch (e) {
    estado.textContent = 'Error: ' + e.message;
  }
}

async function cargarConversacionDirecta(userId) {
  const estado = document.getElementById('estado-historial');
  estado.textContent = 'Cargando conversación...';
  try {
    const res = await fetch(`/conversacion/${encodeURIComponent(userId)}`);
    const d = await res.json();
    estado.textContent = '';
    mostrarConversacion(d);
  } catch (e) {
    estado.textContent = 'Error: ' + e.message;
  }
}

async function enviarRespuesta() {
  const texto = document.getElementById('reply-texto').value.trim();
  if (!texto || !_currentUserId) return;
  const btn = document.getElementById('btn-send');
  btn.disabled = true;
  btn.textContent = 'Enviando...';
  try {
    const res = await fetch('/responder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: _currentUserId, mensaje: texto }),
    });
    if (!res.ok) {
      const err = await res.json();
      alert('Error: ' + (err.detail || res.status));
      return;
    }
    const box = document.getElementById('conv-box');
    if (box) {
      const div = document.createElement('div');
      div.className = 'msg assistant';
      div.innerHTML = escHtml(texto) + '<div class="msg-meta">ahora</div>';
      box.appendChild(div);
      box.scrollTop = box.scrollHeight;
    }
    document.getElementById('reply-texto').value = '';
    document.getElementById('reply-texto').style.height = 'auto';
  } catch (e) {
    alert('Error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Enviar';
  }
}

// ── ARCHIVOS ──────────────────────────────────────────────────────────────────
async function cargarArchivos() {
  const estado = document.getElementById('estado-archivos');
  estado.textContent = 'Cargando archivos...';
  try {
    const res = await fetch('/archivos');
    _archivosData = await res.json();
    filtrarArchivos();
  } catch (e) {
    estado.textContent = 'Error: ' + e.message;
    document.getElementById('tabla-archivos').innerHTML =
      '<tr><td colspan="6" class="empty">Error cargando datos.</td></tr>';
  }
}

function filtrarArchivos() {
  const filtro = (document.getElementById('filtro-tel-archivos').value || '').trim();
  const estado = document.getElementById('estado-archivos');
  const tbody  = document.getElementById('tabla-archivos');

  const datos = filtro
    ? _archivosData.filter(a => (a.telefono || a.ig_user_id || '').includes(filtro))
    : _archivosData;

  if (!datos.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">${
      filtro ? 'Sin archivos para ese teléfono.' : 'No hay archivos recibidos.'
    }</td></tr>`;
    estado.textContent = filtro ? `0 resultados para "${filtro}"` : '';
    return;
  }

  estado.textContent = `${datos.length} archivo(s)${filtro ? ` para "${filtro}"` : ''}.`;
  tbody.innerHTML = datos.map(a => {
    const tipoBadge = /image|foto|imagen/.test(a.tipo) ? `<span class="badge img">${a.tipo}</span>`
                    : /audio/.test(a.tipo)             ? `<span class="badge audio">${a.tipo}</span>`
                    :                                    `<span class="badge doc">${a.tipo}</span>`;
    const accion = (a.media_id || a.url)
      ? `<a class="dl-btn" href="/archivos/${a.id}/descargar">Descargar</a>`
      : `<span class="no-url">—</span>`;
    return `<tr>
      <td>${(a.creado_en || '').replace('T', ' ').substring(0, 16)}</td>
      <td>${canalBadge(a.canal)}</td>
      <td>${tipoBadge}</td>
      <td>${escHtml(a.nombre || '—')}</td>
      <td style="font-size:.82rem;color:var(--muted)">${escHtml(a.telefono || a.ig_user_id || '—')}</td>
      <td>${accion}</td>
    </tr>`;
  }).join('');
}

// ── INIT ──────────────────────────────────────────────────────────────────────
document.getElementById('desde').value = haceDias(30);
document.getElementById('hasta').value = hoy();
cargarAnalytics();
