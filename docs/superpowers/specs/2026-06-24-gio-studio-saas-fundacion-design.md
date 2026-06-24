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
- **Almacenamiento (modelo Free vs Premium):**
  - **Historial:** SIEMPRE en object storage del SaaS (visible en todos los dispositivos, incl. iPhone).
  - **"Mis imágenes" en Free:** carpeta **local** conectada — solo en **Chrome/Edge de escritorio**
    (File System Access API); los archivos se quedan en el disco del usuario, no se suben. En
    Safari/Firefox/iPhone/iPad NO está disponible en Free (se muestra un aviso para conectar desde
    escritorio Chromium o pasarse a Premium).
  - **Premium:** se sube TODO a la nube (historial + "Mis imágenes") → visible en todos los
    dispositivos, incl. iPhone.
  - **Opción adicional (sub-proyecto posterior):** conector OAuth a **Google Drive / Dropbox** como
    fuente alternativa de "Mis imágenes". Da una carpeta multiplataforma (funciona en Safari/iPhone/
    iPad) sin Premium y sin que Gio aloje nada (las imágenes quedan en la nube del propio usuario).
    No entra en el v1; queda como sub-proyecto opcional.
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
| `profiles` | usuario | `id` (=auth user), `email`, `display_name`, `role`, `plan`(free/premium), `created_at` |
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

**Principio de diseño clave (para poder sincronizar después):** el front del SaaS NO se edita a mano
salvo lo imprescindible. `server.py` local sigue siendo la **fuente de la verdad** del UI; las
diferencias del SaaS (login, quitar modal de key, costo→saldo, lecturas-de-archivo→API) se expresan
como una **capa fina y nombrada de adaptadores**, no como ediciones dispersas. Así una mejora local
se puede re-incorporar sin perder lo del SaaS. Ver sección "Sincronización local → SaaS".

---

## Sincronización local → SaaS (mantener el SaaS al día con la app local)

Gio seguirá mejorando la app **local** (`server.py`). Cada mejora debe poder llevarse al SaaS sin
re-clonar a ciegas ni perder las adaptaciones del SaaS. Mecanismo:

1. **Fuente de la verdad = `server.py` local.** El UI (HTML/CSS/JS) se sigue editando ahí, como hoy.

2. **Extracción repetible (no clon único).** Un script `extract_web.py` saca el HTML/CSS/JS embebido
   de `server.py` y genera `web/` de forma **determinista**. Se puede re-ejecutar tantas veces como
   haga falta; no es una copia manual de una sola vez.

3. **Capa de adaptadores SaaS (`web/saas-adapter.js`).** Todo lo específico del SaaS vive en un
   único shim pequeño y documentado que: inyecta login, oculta el modal de API key, cambia la
   etiqueta costo→saldo, y **redirige las lecturas de archivo a la API** (`/history`, `/projects`,
   `/shelf`, `/galeria`, `/config`, etc.). La regla de oro: las diferencias del SaaS se concentran
   aquí, no se esparcen por el front extraído.

4. **Marcador de sincronización.** Un archivo `SYNC.md` registra el **commit de `server.py`** desde
   el que se sincronizó el SaaS por última vez, más un mini-changelog de qué se portó.

5. **Procedimiento de actualización** (documentado en `SYNC.md`, ejecutable en cualquier sesión):
   1. Leer el último hash sincronizado.
   2. `git diff <hash>..HEAD -- server.py` → interpretar qué cambió.
   3. **Clasificar** los cambios: (a) solo-UI → se re-extraen solos; (b) comportamiento/lógica de
      generación → portar al backend FastAPI; (c) toca rutas de datos → revisar adaptadores/API.
   4. Re-ejecutar `extract_web.py`; re-aplicar adaptadores (que no necesitan cambios si la regla de
      oro se respetó).
   5. Probar (test de humo) y desplegar.
   6. Actualizar el hash y el changelog en `SYNC.md`.

> En la práctica, cuando Gio diga "actualicé la app local", la acción es: hacer el diff de
> `server.py` desde el último hash sincronizado, resumir los cambios, y portarlos por estas vías.
> Esto solo funciona bien si se respeta el principio de la capa de adaptadores (paso 3); por eso es
> un principio de diseño, no un añadido opcional.

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
- Conector Google Drive/Dropbox (OAuth) como fuente alternativa de "Mis imágenes" (lectura
  multiplataforma, incl. iPhone) y export/respaldo. Opción deseada por Gio, pero su propio
  sub-proyecto; no entra en v1.
- Audio y video.
