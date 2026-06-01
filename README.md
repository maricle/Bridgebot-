# Instagram → Odoo Agent Bridge
**Kleba Dev — 2026**

Servidor FastAPI que conecta los DMs de Instagram con el agente de IA del Live Chat de Odoo.

## Flujo
```
Cliente escribe DM en Instagram
        ↓
   Este servidor (FastAPI)
        ↓
   Odoo Live Chat + AgenteCNC
        ↓
   Respuesta al DM
```

## Deploy en Railway

### 1. Subir el código a GitHub
```bash
git init
git add .
git commit -m "Instagram Odoo Agent Bridge"
git remote add origin https://github.com/tu-usuario/instagram-odoo-bridge
git push -u origin main
```

### 2. Crear proyecto en Railway
1. Entrar a railway.app
2. New Project → Deploy from GitHub repo
3. Seleccionar el repositorio

### 3. Configurar variables de entorno en Railway
En el panel de Railway ir a Variables y agregar:

| Variable | Valor |
|---|---|
| `META_VERIFY_TOKEN` | El token que escribiste en Meta |
| `META_APP_SECRET` | App Secret de Meta for Developers |
| `IG_ACCESS_TOKEN` | Token de acceso de Instagram |
| `ODOO_URL` | https://tu-instancia.odoo.com |
| `ODOO_API_KEY` | API Key de Odoo |
| `ODOO_DB` | Nombre de la base de datos |
| `ODOO_LIVECHAT_CHANNEL_ID` | ID del canal (número en la URL) |

### 4. Obtener la URL pública de Railway
Railway te da una URL tipo:
```
https://instagram-odoo-bridge-production.up.railway.app
```

### 5. Configurar el webhook en Meta for Developers
En Meta for Developers → API de Instagram → Configurar webhooks:
- **URL de devolución de llamada:** `https://TU-URL.railway.app/webhook`
- **Token de verificación:** el mismo que pusiste en `META_VERIFY_TOKEN`
- Hacer clic en **Verificar y guardar**

## Correr localmente
```bash
cp .env.example .env
# Editar .env con tus valores reales

pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Para exponer el servidor local a internet durante pruebas usar ngrok:
```bash
ngrok http 8000
```
La URL que da ngrok la usás como webhook en Meta.

## Endpoints
- `GET /webhook` — Verificación de Meta
- `POST /webhook` — Recibe eventos de Instagram
- `GET /health` — Health check

## Para agregar WhatsApp
El código soporta WhatsApp con un cambio mínimo:
1. Registrar un número en WhatsApp Business API
2. Configurar el webhook apuntando al mismo `/webhook`
3. El payload de WhatsApp tiene estructura diferente — agregar un parser en `procesar_evento()`
