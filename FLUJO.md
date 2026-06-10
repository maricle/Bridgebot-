# BridgeBot — Flujo de la aplicación

## Arquitectura general

```
WhatsApp / Instagram
        ↓
   Meta Webhooks
        ↓
   FastAPI (main.py)
        ↓
   Claude AI (ai.py)
        ↓
   Turso DB + Odoo CRM
```

BridgeBot recibe mensajes de WhatsApp e Instagram via webhooks de Meta, los procesa con Claude AI y crea leads en Odoo CRM.

---

## Canales y endpoints

| Endpoint | Método | Función |
|---|---|---|
| `/webhook` | GET | Verificación del webhook (Instagram + WA combinado) |
| `/webhook` | POST | Recibe mensajes de Instagram y WhatsApp Business Account |
| `/webhook/whatsapp` | GET/POST | Endpoint dedicado para WhatsApp |
| `/health` | GET | Estado de la app + chequeo del token de WhatsApp |
| `/test-claude` | GET | Verifica conectividad con Claude AI |
| `/test-odoo` | GET | Verifica conectividad con Odoo CRM |

---

## Flujo completo de un mensaje entrante

### 1. Recepción del webhook

`main.py` recibe el payload de Meta y lo despacha según el `object`:
- `"instagram"` → `procesar_instagram()`
- `"whatsapp_business_account"` → `procesar_whatsapp()`

Ambas funciones se ejecutan como **tareas asíncronas** (no bloquean el 200 OK a Meta).

---

### 2. Procesamiento WhatsApp

```
Mensaje entra
    ↓
¿Es archivo (imagen, doc, audio)?
    → Guardar en DB + responder "¡Recibimos el archivo!" → FIN
    ↓
¿Texto vacío o usuario en EXCLUIR_BOT?
    → Ignorar → FIN
    ↓
¿Conversación cerrada (cerrada=1)?
    → Resetear cerrada + enviar saludo de retorno "¡Hola [nombre]! ¿Te ayudo con tu pedido de hoy?"
    → FIN (sin Claude)
    ↓
¿Es usuario nuevo?
    → Buscar teléfono en Odoo sync DB
        → Si existe: saludo personalizado "¡Hola [nombre]!" + guardar nombre/email en DB
        → Si no existe: saludo genérico (SALUDO_BIENVENIDA)
    ↓
Llamar a generar_respuesta() → enviar respuesta
```

---

### 3. Procesamiento Instagram

Mismo flujo que WhatsApp con estas diferencias:
- No busca en Odoo sync (no se conoce el teléfono al inicio)
- El saludo genérico siempre se envía si es usuario nuevo
- El teléfono **debe** obtenerse durante la conversación
- El `sender_id` es el ID de Instagram, no el teléfono

---

### 4. Generación de respuesta (`ai.py` → `generar_respuesta`)

```
1. Obtener canonical_id (por si el usuario está vinculado cross-channel)
2. Cargar historial (últimos 10 mensajes)
3. Cargar datos del cliente (nombre, teléfono, email)
4. Si WA y no hay nombre → buscar en clientes Odoo sync por teléfono
5. Construir system prompt:
   - Instrucciones del agente (agente.txt)
   - Información de la empresa (conocimiento.txt + reglas)
   - Canal actual (WHATSAPP / INSTAGRAM)
   - Flujo específico si detecta keywords (cartelería / gráfica)
   - Precios si el mensaje los pide (lazy-load)
   - Datos conocidos del cliente (si existen)
   - Instrucción: pedir email si no está registrado
6. Llamar a Claude Haiku (con prompt caching)
7. Guardar mensaje en historial
8. Lanzar _intentar_crear_lead() en background
9. Si la respuesta contiene "ya registré tu consulta" → cerrar_conversacion()
10. Retornar respuesta
```

---

### 5. Detección de flujo y precios (lazy-load)

El system prompt se construye dinámicamente según el mensaje:

| Condición | Se agrega al prompt |
|---|---|
| Keywords de precio (precio, cuánto, vale...) | Lista de precios completa |
| Keywords de cartelería (lona, vinilo, banner...) | `02_flujo_carteleria.txt` |
| Keywords de gráfica (impresión, DTF, talonario...) | `03_flujo_grafica_impresiones.txt` |
| Ambos tipos detectados | Ambos flujos |

---

### 6. Creación del lead (`_intentar_crear_lead`)

Se ejecuta en background después de cada respuesta.

```
1. Cargar datos del cliente (nombre, teléfono conocidos)
2. Para WA: guardar el número como teléfono si aún no está
3. Llamar a Claude con EXTRACCION_PROMPT para analizar la conversación
4. Claude devuelve JSON: { tiene_lead, nombre, telefono, email, descripcion, destino }
5. Si tiene_lead=false o ya hay lead activo (últimas 2hs) → no hacer nada
6. Resolver destino (carteleria=company 4/user 8, oficina=company 5/user 10)
7. crear_lead() en Odoo CRM con nombre, teléfono, email, descripción, transcripción
8. Guardar lead en DB local
9. Guardar/actualizar datos del cliente (nombre, teléfono, email)
10. Si hay email → actualizar_partner() en Odoo (res.partner.email)
```

---

### 7. Cierre de conversación y retorno del cliente

**Cierre:**
- Se activa cuando el bot envía una respuesta que contiene `"ya registré tu consulta"`
- Se setea `cerrada=1` en la tabla `usuarios`
- El bot deja de responder

**Retorno:**
- Cliente escribe → `conversacion_cerrada()` devuelve True
- Se resetea `cerrada=0`
- Se envía saludo personalizado "¡Hola [nombre]! ¿Te ayudo con tu pedido de hoy?"
- El bot retorna sin procesar el mensaje con Claude
- El siguiente mensaje del cliente inicia una conversación nueva

---

## Base de datos (Turso / SQLite)

| Tabla | Contenido |
|---|---|
| `usuarios` | ig_user_id, canal, saludado, cerrada, nombre, telefono, email, canonical_id |
| `historial` | mensajes de la conversación (rol + contenido) |
| `leads` | leads creados con resumen y odoo_lead_id |
| `archivos` | archivos recibidos (media_id o URL) |
| `clientes_odoo` | sync nocturno de res.partner de Odoo (odoo_id, nombre, telefono, email) |

---

## Sync nocturno de clientes Odoo

Al arrancar la app y cada 24hs:
1. Autenticar en Odoo via JSON-RPC
2. Traer todos los `res.partner` activos con teléfono (`active=True`, `phone!=False`)
3. Upsert masivo en tabla `clientes_odoo`

Esto permite **identificar clientes de WhatsApp por teléfono** antes de que escriban su nombre.

---

## Routing multi-company (Grupo Ideas)

Los leads se dirigen a distintos equipos según el tipo de trabajo:

| Variable | Destino | Tipo de trabajo |
|---|---|---|
| `ODOO_DESTINO_CARTELERIA=4:8` | Company 4 / Usuario 8 | Letras corpóreas, acrílico, LED |
| `ODOO_DESTINO_OFICINA=5:10` | Company 5 / Usuario 10 | Impresiones, lonas, DTF, vinilos |

El destino lo determina Claude al analizar la conversación en el `EXTRACCION_PROMPT`.

---

## Knowledge files (por rama/cliente)

### Clever CNC (`main` / `clever`)
```
knowledge/
├── agente.txt          # Instrucciones del bot (comportamiento)
├── conocimiento.txt    # Info de la empresa Clever CNC
└── precios.txt         # Lista de precios (puede estar vacío si se carga dinámicamente)
```

### Grupo Ideas (`grupo-ideas`)
```
knowledge/
├── agente.txt                      # VictorIA — instrucciones del bot
├── conocimiento.txt                # Info de la empresa Grupo Ideas
├── 01_reglas_comerciales.txt       # Reglas, condiciones, plazos (siempre cargado)
├── 02_flujo_carteleria.txt         # Flujo + precios cartelería (lazy)
├── 03_flujo_grafica_impresiones.txt # Flujo + precios gráfica (lazy)
└── precios.txt                     # Vacío (precios embebidos en archivos de flujo)
```

---

## Variables de entorno clave

| Variable | Descripción |
|---|---|
| `ANTHROPIC_API_KEY` | API key de Claude (Anthropic) |
| `META_VERIFY_TOKEN` | Token de verificación del webhook de Meta |
| `META_APP_SECRET` | Secret para verificar firma de Instagram |
| `IG_ACCESS_TOKEN` | Token de acceso de Instagram |
| `WA_ACCESS_TOKEN` | Token de acceso de WhatsApp Business |
| `WA_PHONE_ID` | ID del número de WhatsApp registrado |
| `WA_NUMERO_SOPORTE` | Número para derivar seguimiento de pedidos (formato: 549XXXXXXXXX) |
| `SALUDO_BIENVENIDA` | Mensaje de bienvenida para nuevos usuarios |
| `ODOO_URL` | URL de la instancia Odoo |
| `ODOO_API_KEY` | API key de Odoo |
| `ODOO_DB` | Nombre de la base de datos Odoo |
| `ODOO_LOGIN` | Email del usuario Odoo |
| `ODOO_DESTINO_CARTELERIA` | Routing cartelería: `company_id:user_id` |
| `ODOO_DESTINO_OFICINA` | Routing oficina: `company_id:user_id` |
| `TURSO_URL` | URL de la base de datos Turso |
| `TURSO_TOKEN` | Token de autenticación Turso |
| `AUTO_RESPUESTA` | Si `true`, solo envía el saludo sin Claude (modo pausa) |
| `EXCLUIR_BOT` | Lista de user_ids separados por coma donde el bot no responde |
