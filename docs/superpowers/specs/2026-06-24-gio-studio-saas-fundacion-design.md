# Gio Studio SaaS — Sub-proyecto 1: Fundación multi-tenant (imagen, beta privada)

Fecha: 2026-06-24
Estado: diseño aprobado por el usuario (pendiente revisión del spec escrito)

## Contexto

Gio Studio es hoy un estudio **local y mono-usuario** de generación de imágenes con `gpt-image-2`
(OpenAI), servido por un único `server.py` (~8.260 líneas, `http.server` casero, HTML/CSS/JS
embebido). Los datos son archivos + JSON sueltos en `~/image-studio`, **todo global** (sin noción
de usuario). Una sola llave OpenAI en `~/.openai_key`.

El objetivo a largo plazo es convertirlo en un **SaaS multiusuario en la nube**. Este programa se
descompone en sub-proyectos (cada uno con su spec → plan → build). **Este documento cubre solo el
sub-proyecto 1.**

### Decisiones de producto (fijadas con el usuario)

- **Cobro:** modelo de **créditos con margen** (Gio pone la llave de OpenAI y revende por uso).
  La facturación automática (Stripe/Dodo) se difiere a un sub-proyecto posterior.
- **Almacenamiento:** Gio aloja las generaciones en object storage por defecto; el export a
  Drive/Dropbox del usuario se difiere a un sub-proyecto posterior.
- **Alcance v1:** **solo imagen** (gpt-image-2). Audio y video, después.
- **Primer hito:** **beta privada** — cuentas + datos por usuario + galería en la nube + créditos
  asignados a mano. **Sin** cobro automático todavía.

### Enfoque elegido

**Backend nuevo limpio + reutilizar el frontend pulido** (no refactorizar el monolito, no reescribir
todo). Stack: **Supabase** (Auth + Postgres + Storage) + **FastAPI** (Python, porta la lógica de
generación) + el **front actual** extraído como cliente estático.

---

## ⚠️ GATE crítico (instrucción del usuario)

> **Antes de extraer/clonar el front actual desde `server.py`, DETENERSE y avisar a Gio.**
> Gio quiere hacer unos últimos ajustes al Gio Studio local antes de que se clone el HTML/CSS/JS para
> el SaaS. La extracción del front NO debe ejecutarse sin su confirmación explícita ("ya hice mis
> ajustes, puedes clonar"). Este gate va replicado en el plan de implementación.

---

## Arquitectura

Tres piezas desplegadas:

```
gio-studio-cloud/
├── web/         front actual (HTML/CSS/JS) extraído del server.py, servido estático
├── backend/     FastAPI: generación, créditos, API multi-tenant
└── supabase/    migraciones SQL (esquema + RLS)
```

- **Supabase** es la columna vertebral: **Auth** (magic-link + Google), **Postgres** (datos),
  **Storage** (imágenes + miniaturas).
- **FastAPI** guarda la **llave de OpenAI como secreto del servidor** (nunca llega al navegador),
  llama a OpenAI, mide el costo real, descuenta créditos y guarda en Storage.
- **El front** deja de leer archivos locales: hace login y llama a la API con el token de sesión;
  las imágenes se cargan desde **URLs firmadas** de Storage.

**Flujo de una generación:** front (con sesión) → `POST /gen` → backend verifica saldo → llama a
OpenAI con la llave de Gio → lee `usage` (costo exacto) → sube PNG + miniatura a Storage del usuario
→ inserta fila en `generations` y descuenta créditos en una transacción → devuelve URL firmada → el
front la pinta.

---

## Modelo de datos (Postgres + Storage)

Todas las tablas con **RLS por `auth.uid() = user_id`** (aislamiento total entre usuarios).

| Tabla | Para qué | Campos clave |
|---|---|---|
| `profiles` | usuario | `id` (=auth user), `email`, `display_name`, `role`, `created_at` |
| `credits` | saldo | `user_id`, `balance_usd`, `updated_at` |
| `credit_ledger` | auditoría de cargos/abonos | `id`, `user_id`, `delta_usd`, `reason`, `generation_id`, `created_at` |
| `projects` | "memoria de proyecto" | `id`, `user_id`, `name`, `memory_text`, `created_at` |
| `generations` | historial | `id`, `user_id`, `project_id`, `kind`(gen/edit), `prompt`, `model`, `size`, `quality`, `n`, `output_tokens`, `input_img_tokens`, `cost_usd`, `storage_path`, `thumb_path`, `params`(jsonb), `created_at` |
| `invites` | beta privada | `code`/`email`, `created_by`, `redeemed_by`, `created_at` |

**Storage (bucket privado `images`):**

```
{userId}/generations/{generationId}.png
{userId}/thumbs/{generationId}.webp
{userId}/projects/{projectId}/refs/{n}.png
```

Acceso por URLs firmadas de corta duración; RLS en el bucket para que cada usuario solo vea lo suyo.

---

## Auth + invitaciones (beta privada)

- **Login:** magic-link de Supabase Auth + opción Google. Sin contraseñas que gestionar.
- **Puerta de la beta:** registro **cerrado**. Solo entra quien tiene invitación.
  - Gio genera un código de invitación (o invita por email) desde un panel admin mínimo.
  - En el primer login, un trigger de Postgres comprueba `invites`: si hay invitación válida, crea
    `profile` + fila `credits` (saldo 0); si no, rechaza con mensaje claro.
- **Rol admin:** el usuario de Gio tiene `role='admin'` y ve un panel sencillo: lista de usuarios,
  su saldo, botón "recargar créditos", crear/ver invitaciones.
- **Sesión:** el front guarda el token de Supabase; cada llamada al backend lo manda en
  `Authorization: Bearer`. El backend valida el JWT contra Supabase en cada request.

---

## Generación + créditos (núcleo)

Se portan `/gen` (texto→imagen) y `/edit` (referencias/máscara/inpainting) desde `server.py`, con
esta secuencia blindada:

1. **Pre-chequeo:** backend recalcula la estimación (fórmula actual: tokens estimados × $30/1M +
   refs × $8/1M) **× margen**. Si `balance < estimación` → rechaza sin llamar a OpenAI.
2. **Reserva (hold):** descuenta un hold provisional del saldo (evita sobregasto en paralelo).
3. **Llamada a OpenAI** con la llave de Gio (server-side). Máscara y referencias igual que hoy.
4. **Costo exacto:** lee `usage` de la respuesta → costo real × margen.
5. **Guardado:** sube PNG + miniatura a Storage del usuario; inserta fila en `generations`.
6. **Liquidación:** ajusta saldo al costo real (libera hold, aplica cargo definitivo), registra en
   `credit_ledger`. Todo en una transacción: si falla tras cobrar OpenAI, se registra igual para no
   perder dinero; si falla antes, se libera el hold.
7. Devuelve URL firmada + costo real; el front lo pinta y actualiza el saldo visible.

**Margen:** variable de entorno (`MARKUP`, ej. `1.5`). Se cambia sin tocar código.

---

## Integración del front

El front actual se conserva al máximo. Cambios acotados:

- **Pantalla de login** nueva (magic-link) antes de entrar al estudio.
- **Quitar el modal de "pega tu API key"** (la llave es del servidor).
- **Reemplazar lecturas de archivos locales** (`/history`, `/projects`, `/shelf`, `/galeria`,
  `/config`, etc.) por llamadas a la API con sesión; imágenes desde URLs firmadas de Storage.
- **Saldo de créditos** visible arriba (reusa el sitio del costo de sesión actual).
- Todo lo visual (6 temas, editor máscara/anotar/pins, lightbox, historial, motion, accesibilidad)
  **queda igual** — la app se ve idéntica a la local.

---

## Despliegue

- **Backend (FastAPI):** Render o Railway (24/7). Secretos: llave OpenAI, claves Supabase, `MARKUP`.
- **Front estático:** Vercel o el mismo Render.
- **Supabase:** proyecto gestionado (Auth + Postgres + Storage); migraciones versionadas en
  `supabase/`.
- **Dominio:** subdominio de Gio (ej. `studio.tudominio.com`).

---

## Manejo de errores

- Casos: saldo insuficiente, fallo de OpenAI (timeout/moderación/límite), fallo de subida a Storage.
- Mensajes claros con los toasts actuales. **Nunca** cobrar sin entregar ni entregar sin registrar.
- Reintentos con back-off en la llamada a OpenAI.

## Pruebas (TDD)

- Lógica de créditos: hold, liquidación, saldo insuficiente, concurrencia.
- Aislamiento RLS: un usuario no ve datos de otro.
- Portado de `/gen` y `/edit` con OpenAI **mockeado**.
- Trigger de invitaciones (con/sin invitación válida).
- Test de humo del flujo completo en staging antes de invitar a nadie.

---

## Fuera de alcance (sub-proyectos posteriores)

- Cobro automático y autoservicio (Stripe/Dodo, compra de créditos, registro abierto).
- Anti-abuso/hardening (rate limits, topes, moderación, detección de fraude).
- Export a Drive/Dropbox (OAuth, bring-your-own-storage).
- Audio y video.
