# BridgeBot — Documentación

Bot de atención al cliente con IA para Instagram y WhatsApp. Responde mensajes automáticamente, captura leads y los crea en Odoo CRM.

---

## Arquitectura

```
Instagram DM / WhatsApp
        ↓
   Meta Webhook
        ↓
   BridgeBot (Railway)
        ↓
   Groq AI (llama-3.3-70b)
        ↓
   Respuesta al cliente
        +
   Lead → Odoo CRM
```

**Stack:**
- Python 3.13 + FastAPI + httpx
- Groq API (LLM gratuito)
- SQLite local (historial y leads)
- Railway (deploy)

---

## Estructura del proyecto

```
BridgeBot/
├── main.py           # Rutas FastAPI (webhooks, endpoints)
├── config.py         # Variables de entorno
├── db.py             # Base de datos SQLite
├── groq_ai.py        # Lógica de IA y detección de leads
├── instagram.py      # Envío/parseo Instagram
├── whatsapp.py       # Envío/parseo WhatsApp
├── odoo_crm.py       # Creación de leads en Odoo
├── knowledge/
│   ├── agente.txt    # Comportamiento del bot (flujo, reglas)
│   ├── conocimiento.txt  # Info de la empresa
│   └── precios.txt   # Lista de precios
└── requirements.txt
```

---

## Variables de entorno (Railway)

### Meta / Instagram
| Variable | Descripción | Ejemplo |
|---|---|---|
| `META_VERIFY_TOKEN` | Token que ingresás en Meta al configurar el webhook | `mi_token_secreto` |
| `APP_SECRET` | App Secret de Meta for Developers | `abc123...` |
| `IG_ACCESS_TOKEN` | Token de acceso de Instagram | `EAABw...` |
| `IG_ACCOUNT_ID` | ID de la cuenta de Instagram del negocio | `17841456843060136` |

### WhatsApp
| Variable | Descripción | Ejemplo |
|---|---|---|
| `WA_ACCESS_TOKEN` | Token de acceso de WhatsApp Business | `EAABw...` |
| `WA_PHONE_ID` | ID del número de teléfono de WhatsApp | `1090257267499224` |

### Groq AI
| Variable | Descripción | Ejemplo |
|---|---|---|
| `GROQ_API_KEY` | API key de console.groq.com (gratis) | `gsk_...` |

### Odoo CRM (opcional)
| Variable | Descripción | Ejemplo |
|---|---|---|
| `ODOO_URL` | URL de la instancia Odoo | `https://empresa.odoo.com` |
| `ODOO_DB` | Nombre de la base de datos | `empresa` |
| `ODOO_LOGIN` | Email del usuario Odoo | `admin@empresa.com` |
| `ODOO_API_KEY` | API key generada en Odoo → Perfil → Seguridad | `7ec9b...` |

### Personalización (opcionales)
| Variable | Descripción | Default |
|---|---|---|
| `SALUDO_BIENVENIDA` | Mensaje de bienvenida al primer contacto | `¡Hola! Soy el asistente virtual...` |
| `BOT_SYSTEM_PROMPT` | Override completo del prompt del agente | (se arma desde los archivos en `knowledge/`) |
| `AUTO_RESPUESTA` | `true` = solo saluda, `false` = IA activa | `false` |

---

## Endpoints disponibles

| Endpoint | Método | Descripción |
|---|---|---|
| `/webhook` | GET | Verificación del webhook de Instagram/WhatsApp |
| `/webhook` | POST | Recibe mensajes de Instagram y WhatsApp |
| `/webhook/whatsapp` | GET/POST | Webhook alternativo para WhatsApp |
| `/health` | GET | Estado del servicio y estadísticas |
| `/leads` | GET | Lista de leads capturados |
| `/usuarios` | GET | Lista de usuarios que escribieron |
| `/conversacion/{id}` | GET | Historial de conversación de un usuario |
| `/usuario/{id}` | DELETE | Resetea un usuario (borra historial y saludo) |
| `/test-odoo` | GET | Prueba la conexión con Odoo CRM |

---

## Configurar un cliente nuevo

### Paso 1 — Clonar/forkear el repo

```bash
git clone https://github.com/maricle/Bridgebot-.git cliente-nuevo
cd cliente-nuevo
git checkout groq-v2
git checkout -b cliente-nombre
```

### Paso 2 — Completar los archivos de conocimiento

Editá los tres archivos en `knowledge/`:

**`agente.txt`** — personalizá el nombre de la empresa y el flujo de atención si es necesario.

**`conocimiento.txt`** — completá con la info del cliente:
- Nombre y descripción de la empresa
- Servicios ofrecidos
- Materiales disponibles
- Plazos de entrega
- Preguntas frecuentes
- Zona de cobertura y envíos

**`precios.txt`** — lista de precios actualizada del cliente.

### Paso 3 — Crear la app en Meta for Developers

1. Ir a [developers.facebook.com](https://developers.facebook.com)
2. Crear una nueva app (tipo "Business")
3. Agregar el producto **Instagram** y/o **WhatsApp**
4. Conectar la cuenta de Instagram/WhatsApp Business del cliente
5. Anotar: `APP_SECRET`, `IG_ACCESS_TOKEN`, `IG_ACCOUNT_ID`

### Paso 4 — Crear API key de Groq

1. Ir a [console.groq.com](https://console.groq.com)
2. Crear cuenta (gratis)
3. API Keys → Create API Key
4. Guardar la clave (`gsk_...`)

### Paso 5 — Crear servicio en Railway

1. Ir a [railway.app](https://railway.app)
2. New Project → Deploy from GitHub repo → seleccionar el branch del cliente
3. En **Variables**, cargar todas las variables del cliente (ver tabla arriba)
4. Una vez deployado, copiar la URL del servicio (ej: `https://cliente.up.railway.app`)

### Paso 6 — Configurar webhook en Meta

**Instagram:**
1. Meta for Developers → tu app → Instagram → Webhooks
2. URL: `https://cliente.up.railway.app/webhook`
3. Token: el valor de `META_VERIFY_TOKEN`
4. Suscribirse al campo `messages`

**WhatsApp:**
1. Meta for Developers → tu app → WhatsApp → Configuración
2. URL: `https://cliente.up.railway.app/webhook`
3. Token: el valor de `META_VERIFY_TOKEN`
4. Suscribirse al campo `messages`

### Paso 7 — Configurar Odoo CRM (opcional)

1. En Odoo: ir al perfil del usuario → Seguridad de la cuenta → Claves API → Nueva clave
2. Guardar la clave generada
3. Cargar en Railway: `ODOO_URL`, `ODOO_DB`, `ODOO_LOGIN`, `ODOO_API_KEY`
4. Verificar: `https://cliente.up.railway.app/test-odoo`

### Paso 8 — Probar

1. Mandar un DM a la cuenta de Instagram del cliente
2. Verificar en Railway → Logs que aparezca:
   ```
   INFO: IG user=...: [mensaje]
   INFO: Groq [instagram] user=...: [respuesta]
   INFO: DM enviado a IG user=...
   ```
3. Verificar que el bot responde en el DM

---

## Cómo actualizar precios o info

1. Editar el archivo correspondiente en `knowledge/`
2. Hacer commit y push
3. Railway redespliega automáticamente

```bash
git add knowledge/precios.txt
git commit -m "Actualizar precios"
git push
```

---

## Lógica de leads

El bot captura leads automáticamente cuando detecta:
- Nombre y apellido del cliente
- Teléfono o WhatsApp
- Un pedido o consulta concreta

La detección corre cada 3 turnos de conversación. Cuando se detecta un lead completo, se crea automáticamente en Odoo CRM (si está configurado) y se guarda en la base de datos local.

Palabras que activan la detección de lead: `presupuesto`, `precio`, `cuánto`, `cuanto`, `cotización`, `medidas`, `cantidad`, `encargar`, `necesito`, `quiero`.

---

## Notas importantes

- **SQLite**: el historial se guarda localmente en Railway. Se resetea con cada redeploy. Para persistencia permanente, configurar un volumen en Railway o migrar a Turso.
- **Groq free tier**: 14.400 requests/día en el plan gratuito. Suficiente para un bot de mediano tráfico.
- **API key de Groq**: no commitear nunca en el código. Siempre via variable de entorno en Railway.
- **Tokens de Meta**: los tokens de Instagram expiran. Renovar cada 60 días o usar tokens de sistema de larga duración.
