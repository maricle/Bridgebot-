let chartDia = null, chartProd = null;
let _modoHistorial = 'tel';
let _currentUserId = null;
let _currentPausado = false;
let _archivosData  = [];
let _recientesOffset = 0;
const _RECIENTES_LIMITE = 20;

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
const _ARCHIVO_RE = /^\[Archivo recibido: (\w+)\] (\/archivos\/\d+\/descargar)$/;

function renderContenidoMensaje(contenido) {
  const m = _ARCHIVO_RE.exec(contenido);
  if (!m) return escHtml(contenido);
  const [, tipo, url] = m;
  return `📎 Archivo recibido (${escHtml(tipo)}) — <a class="dl-btn" href="${url}" target="_blank">Descargar</a>`;
}

function renderConversacion(historial) {
  if (!historial.length) return '<span class="empty">Sin mensajes registrados.</span>';
  return historial.map(m => `
    <div class="msg ${m.rol}">
      ${renderContenidoMensaje(m.contenido)}
      <div class="msg-meta">${m.creado_en || ''}${m.rol === 'assistant' ? ' <span class="check-sent">✓</span>' : ''}</div>
    </div>`).join('');
}
function renderClienteInfo(c, userId, pausado) {
  return `
    <div class="cliente-info">
      <div class="ci-avatar"><i class="fas fa-user"></i></div>
      <div class="ci-item"><div class="ci-label">Nombre</div><div class="ci-val">${escHtml(c.nombre || '—')}</div></div>
      <div class="ci-item"><div class="ci-label">Teléfono</div><div class="ci-val">${escHtml(c.telefono || userId)}</div></div>
      <div class="ci-item"><div class="ci-label">Email</div><div class="ci-val">${escHtml(c.email || '—')}</div></div>
      <div class="ci-item"><div class="ci-label">Canal</div><div class="ci-val">${escHtml(c.canal || '—')}</div></div>
      <button class="btn-toggle-pausa ${pausado ? 'pausado' : 'activo'}" id="btn-pausa" onclick="togglePausa()">
        ${pausado ? '▶ Reanudar bot' : '⏸ Pausar bot'}
      </button>
    </div>`;
}
function mostrarConversacion(d) {
  _currentUserId = d.user_id;
  _currentPausado = !!d.pausado;
  const c = d.cliente;
  document.getElementById('resultado-historial').innerHTML =
    renderClienteInfo(c, d.user_id, _currentPausado) +
    `<div class="conversacion" id="conv-box">${renderConversacion(d.historial)}</div>`;
  const box = document.getElementById('conv-box');
  if (box) box.scrollTop = box.scrollHeight;
  document.getElementById('reply-box').style.display =
    esWhatsApp(c.canal, d.user_id) ? 'flex' : 'none';
  document.getElementById('reply-texto').value = '';
}
async function togglePausa() {
  if (!_currentUserId) return;
  const accion = _currentPausado ? 'reanudar' : 'pausar';
  try {
    const res = await fetch(`/usuario/${encodeURIComponent(_currentUserId)}/${accion}`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _currentPausado = !_currentPausado;
    const btn = document.getElementById('btn-pausa');
    if (btn) {
      btn.className = `btn-toggle-pausa ${_currentPausado ? 'pausado' : 'activo'}`;
      btn.textContent = _currentPausado ? '▶ Reanudar bot' : '⏸ Pausar bot';
    }
  } catch (e) {
    alert('No se pudo cambiar el estado del bot: ' + e.message);
  }
}

// ── TABS ──────────────────────────────────────────────────────────────────────
const _TAB_TITULOS = { analytics: 'Analytics', historial: 'Conversaciones', archivos: 'Archivos', config: 'Configuración', webhooks: 'Webhooks' };

function switchTab(tab) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('#accordionSidebar .nav-item').forEach(li => li.classList.remove('active'));
  document.getElementById('panel-' + tab).classList.add('active');
  document.getElementById('navitem-' + tab).classList.add('active');
  document.getElementById('page-heading').textContent = _TAB_TITULOS[tab] || '';
  document.getElementById('filtro-analytics').style.display = tab === 'analytics' ? 'flex' : 'none';
  if (tab === 'archivos') cargarArchivos();
  if (tab === 'historial') cargarHistorialReciente(true);
  if (tab === 'config') cargarConfiguracionTab();
  if (tab === 'webhooks') pintarUrlsWebhooks();
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
  document.getElementById('lista-recientes').innerHTML = '';
  document.getElementById('btn-cargar-mas').style.display = 'none';
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

function renderResultadoCard(r) {
  const preview = renderContenidoMensaje(r.ultimo_texto || '').replace(/<[^>]+>/g, '').trim();
  const check = r.ultimo_rol === 'assistant' ? '<span class="check-sent">✓</span> ' : '';
  return `
    <div class="resultado-card" onclick='cargarConversacionDirecta(${JSON.stringify(r.ig_user_id)})'>
      <div class="rc-avatar"><i class="fas fa-user"></i></div>
      <div class="rc-body">
        <div class="rc-top">
          <span class="rc-nombre">${escHtml(r.nombre || r.telefono || r.ig_user_id || '—')}</span>
          ${canalBadge(r.canal)}
          <span class="rc-fecha">${(r.ultimo_mensaje || '').substring(0, 16).replace('T', ' ')}</span>
        </div>
        <div class="rc-preview">${check}${escHtml(preview || 'Sin mensajes')}</div>
      </div>
    </div>`;
}

async function buscarContenido() {
  const q = document.getElementById('input-contenido').value.trim();
  if (!q) return;
  const estado = document.getElementById('estado-historial');
  const lista  = document.getElementById('lista-resultados');
  estado.textContent = 'Buscando...';
  lista.style.display = 'none';
  document.getElementById('lista-recientes').innerHTML = '';
  document.getElementById('btn-cargar-mas').style.display = 'none';
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
    lista.innerHTML = '<div class="resultados-lista">' + data.map(renderResultadoCard).join('') + '</div>';
  } catch (e) {
    estado.textContent = 'Error: ' + e.message;
  }
}

async function cargarHistorialReciente(reset) {
  if (reset) {
    _recientesOffset = 0;
    document.getElementById('estado-historial').textContent = '';
    document.getElementById('lista-resultados').style.display = 'none';
    document.getElementById('lista-resultados').innerHTML = '';
    document.getElementById('resultado-historial').innerHTML = '';
    document.getElementById('reply-box').style.display = 'none';
    document.getElementById('lista-recientes').innerHTML = '<div class="resultados-lista" id="recientes-grid"></div>';
  }
  const btnMas = document.getElementById('btn-cargar-mas');
  try {
    const res = await fetch(`/historial-reciente?limite=${_RECIENTES_LIMITE}&offset=${_recientesOffset}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (reset && !data.length) {
      document.getElementById('lista-recientes').innerHTML = '<span class="empty">Todavía no hay conversaciones.</span>';
      btnMas.style.display = 'none';
      return;
    }
    document.getElementById('recientes-grid').insertAdjacentHTML('beforeend', data.map(renderResultadoCard).join(''));
    _recientesOffset += data.length;
    btnMas.style.display = data.length === _RECIENTES_LIMITE ? 'block' : 'none';
  } catch (e) {
    document.getElementById('estado-historial').textContent = 'Error: ' + e.message;
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

// ── BRANDING ──────────────────────────────────────────────────────────────────
async function cargarBranding() {
  try {
    const res = await fetch('/dashboard-config');
    if (!res.ok) return;
    const d = await res.json();
    if (d.nombre) {
      document.getElementById('page-title').textContent = `BridgeBot Dashboard — ${d.nombre}`;
      document.getElementById('app-title').textContent = `BridgeBot — ${d.nombre}`;
    }
    if (d.color) {
      document.documentElement.style.setProperty('--accent', d.color);
    }
  } catch (e) { /* mantiene los valores por defecto */ }
}

// ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────
let _knowledgeCache = {};

function _pedirApiKey() {
  const key = prompt('Ingresá la API key (BRIDGE_API_KEY) para guardar cambios de configuración:');
  if (key) localStorage.setItem('bridgebot_api_key', key);
  return key;
}

async function _postConfig(url, body) {
  let key = localStorage.getItem('bridgebot_api_key');
  if (!key) key = _pedirApiKey();
  if (!key) throw new Error('Se necesita la API key para guardar');

  const intentar = (k) => fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Api-Key': k },
    body: JSON.stringify(body),
  });

  let res = await intentar(key);
  if (res.status === 401) {
    localStorage.removeItem('bridgebot_api_key');
    key = _pedirApiKey();
    if (!key) throw new Error('Se necesita la API key para guardar');
    res = await intentar(key);
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function cargarConfiguracionTab() {
  const estadoVars = document.getElementById('estado-config-vars');
  estadoVars.textContent = '';
  try {
    const res = await fetch('/config');
    const d = await res.json();
    document.getElementById('cfg-nombre-negocio').value = d.NOMBRE_NEGOCIO || '';
    document.getElementById('cfg-saludo').value = d.SALUDO_BIENVENIDA || '';
    document.getElementById('cfg-alias').value = d.ALIAS_TRANSFERENCIA || '';
    document.getElementById('cfg-color').value = d.DASHBOARD_COLOR || '#4f46e5';
    document.getElementById('cfg-auto-respuesta').checked = !!d.AUTO_RESPUESTA;
  } catch (e) {
    estadoVars.textContent = 'Error cargando: ' + e.message;
  }

  const estadoKnowledge = document.getElementById('estado-config-knowledge');
  document.getElementById('cfg-knowledge-texto').value = 'Cargando...';
  try {
    const res = await fetch('/config/knowledge');
    _knowledgeCache = await res.json();
    cambiarArchivoKnowledge();
  } catch (e) {
    estadoKnowledge.textContent = 'Error cargando: ' + e.message;
  }
}

function cambiarArchivoKnowledge() {
  const archivo = document.getElementById('cfg-archivo').value;
  document.getElementById('cfg-knowledge-texto').value = _knowledgeCache[archivo] || '';
  document.getElementById('estado-config-knowledge').textContent = '';
}

async function guardarConfiguracion() {
  const estado = document.getElementById('estado-config-vars');
  estado.textContent = 'Guardando...';
  try {
    await _postConfig('/config', {
      NOMBRE_NEGOCIO: document.getElementById('cfg-nombre-negocio').value.trim(),
      SALUDO_BIENVENIDA: document.getElementById('cfg-saludo').value.trim(),
      ALIAS_TRANSFERENCIA: document.getElementById('cfg-alias').value.trim(),
      DASHBOARD_COLOR: document.getElementById('cfg-color').value,
      AUTO_RESPUESTA: document.getElementById('cfg-auto-respuesta').checked,
    });
    estado.textContent = 'Guardado ✓';
    cargarBranding();
  } catch (e) {
    estado.textContent = 'Error: ' + e.message;
  }
}

async function guardarKnowledge() {
  const archivo = document.getElementById('cfg-archivo').value;
  const contenido = document.getElementById('cfg-knowledge-texto').value;
  const estado = document.getElementById('estado-config-knowledge');
  estado.textContent = 'Guardando...';
  try {
    await _postConfig(`/config/knowledge/${encodeURIComponent(archivo)}`, { contenido });
    _knowledgeCache[archivo] = contenido;
    estado.textContent = 'Guardado ✓';
  } catch (e) {
    estado.textContent = 'Error: ' + e.message;
  }
}

// ── WEBHOOKS ───────────────────────────────────────────────────────────────────
function pintarUrlsWebhooks() {
  document.querySelectorAll('#tabla-webhooks .wh-url').forEach(td => {
    td.textContent = location.origin + td.dataset.path;
  });
}

function _mostrarResultadoWebhooks(texto) {
  const pre = document.getElementById('webhooks-resultado');
  pre.style.display = 'block';
  pre.textContent = texto;
}

async function sincronizarClientes() {
  _mostrarResultadoWebhooks('Sincronizando clientes...');
  try {
    const res = await fetch('/sync-clientes');
    const d = await res.json();
    _mostrarResultadoWebhooks(`Clientes sincronizados: ${d.clientes_sincronizados}`);
  } catch (e) {
    _mostrarResultadoWebhooks('Error: ' + e.message);
  }
}

async function sincronizarTareas() {
  _mostrarResultadoWebhooks('Sincronizando tareas...');
  try {
    const res = await fetch('/sync-tareas');
    const d = await res.json();
    _mostrarResultadoWebhooks(`Tareas sincronizadas: ${d.tareas_sincronizadas}`);
  } catch (e) {
    _mostrarResultadoWebhooks('Error: ' + e.message);
  }
}

async function actualizarPreciosManual() {
  _mostrarResultadoWebhooks('Actualizando precios...');
  try {
    const res = await fetch('/actualizar-precios');
    const d = await res.json();
    _mostrarResultadoWebhooks(JSON.stringify(d, null, 2));
  } catch (e) {
    _mostrarResultadoWebhooks('Error: ' + e.message);
  }
}

async function verPlantillasWA() {
  let key = localStorage.getItem('bridgebot_api_key');
  if (!key) key = _pedirApiKey();
  if (!key) return;
  _mostrarResultadoWebhooks('Consultando plantillas...');
  try {
    const res = await fetch(`/whatsapp/plantillas?key=${encodeURIComponent(key)}`);
    const d = await res.json();
    if (res.status === 401) {
      localStorage.removeItem('bridgebot_api_key');
      _mostrarResultadoWebhooks('API key inválida — probá de nuevo.');
      return;
    }
    _mostrarResultadoWebhooks(JSON.stringify(d, null, 2));
  } catch (e) {
    _mostrarResultadoWebhooks('Error: ' + e.message);
  }
}

// ── INIT ──────────────────────────────────────────────────────────────────────
cargarBranding();
document.getElementById('desde').value = haceDias(30);
document.getElementById('hasta').value = hoy();
cargarAnalytics();
