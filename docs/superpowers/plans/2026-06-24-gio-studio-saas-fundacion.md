# Gio Studio SaaS — Fundación multi-tenant (imagen, beta privada) — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recomendado) o superpowers:executing-plans para implementar este plan tarea por tarea. Los pasos usan checkbox (`- [ ]`).

**Goal:** Convertir Gio Studio (app local de imagen con gpt-image-2) en un SaaS multiusuario en la nube, beta privada, con cuentas, datos aislados por usuario, galería en la nube y créditos con margen asignados a mano.

**Architecture:** Backend nuevo FastAPI que porta la lógica de generación de `server.py` y guarda la llave de OpenAI como secreto del servidor; Supabase como Auth + Postgres + Storage; el front actual se reutiliza como cliente estático conectado a la API. RLS por `user_id` aísla los datos. Créditos en una transacción hold→liquidación.

**Tech Stack:** Python 3.12 · FastAPI · Uvicorn · supabase-py · httpx · pytest · Supabase (Auth/Postgres/Storage) · OpenAI Images API (gpt-image-2).

## Global Constraints

- v1 = SOLO imagen (gpt-image-2). Nada de audio/video en este plan.
- Cobro = créditos con margen; en la beta el saldo se recarga A MANO (sin Stripe). Margen vía env `MARKUP` (ej. `1.5`).
- La llave de OpenAI vive SOLO en el servidor (env `OPENAI_API_KEY`), nunca se expone al navegador.
- Tarifas verbatim del local: `PRICE_OUT=30.0`, `PRICE_IN=5.0`, `PRICE_IN_IMG=8.0`, `PRICE_IN_IMG_CACHED=2.0` (USD por 1M tokens).
- Modelo fijo: `gpt-image-2`. Límites OpenAI: lado 512–3840, ÷16, 0.65–8.29 MP, ratio ≤3:1.
- Toda tabla con RLS por `auth.uid() = user_id`. Storage bucket privado, acceso por URL firmada.
- Registro CERRADO (beta): solo entra quien tiene invitación.
- **⚠️ GATE (instrucción de Gio):** las tareas de la Fase 8 (extraer/clonar el front desde `server.py`) NO se ejecutan hasta que Gio confirme explícitamente "ya hice mis ajustes, puedes clonar". Ver Tarea 8.0.
- Almacenamiento: Historial siempre en la nube; "Mis imágenes" en Free = carpeta local (Chrome/Edge escritorio); Premium = todo a la nube. `profiles.plan` ∈ {free, premium}.
- TDD estricto, commits frecuentes, sin placeholders.

---

## Estructura de archivos

```
gio-studio-cloud/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── config.py            # settings desde env (Settings)
│   │   ├── main.py              # FastAPI app + routers
│   │   ├── supa.py             # cliente Supabase (admin + per-request)
│   │   ├── auth.py             # dependencia current_user (valida JWT)
│   │   ├── pricing.py          # cálculo de costo desde usage (puro)
│   │   ├── credits.py          # hold / settle / saldo (transaccional)
│   │   ├── openai_images.py    # llamadas a gpt-image-2 (port de server.py)
│   │   ├── storage.py          # subida a Supabase Storage + URL firmada
│   │   └── routers/
│   │       ├── generate.py     # POST /api/gen, /api/edit
│   │       ├── library.py      # GET /api/history, /api/projects, /api/credits
│   │       └── admin.py        # invites + recarga de créditos (rol admin)
│   ├── tests/
│   │   ├── test_pricing.py
│   │   ├── test_credits.py
│   │   ├── test_auth.py
│   │   ├── test_generate.py
│   │   └── conftest.py
│   ├── requirements.txt
│   └── .env.example
├── supabase/
│   └── migrations/
│       ├── 0001_schema.sql
│       ├── 0002_rls.sql
│       └── 0003_invites_trigger.sql
├── web/                        # front extraído (Fase 8, GATED)
│   └── saas-adapter.js
├── SYNC.md                     # hash de última sync + procedimiento
└── README.md
```

---

## Fase 0 — Scaffold

### Task 0.1: Repo y dependencias del backend

**Files:**
- Create: `backend/requirements.txt`, `backend/.env.example`, `backend/app/__init__.py`, `backend/app/config.py`
- Test: `backend/tests/test_config.py`, `backend/tests/conftest.py`

**Interfaces:**
- Produces: `app.config.Settings` con atributos `openai_api_key: str`, `supabase_url: str`, `supabase_service_key: str`, `supabase_jwt_secret: str`, `markup: float`, `storage_bucket: str`; instancia `get_settings()` cacheada.

- [ ] **Step 1: Crear el repo y venv**

```bash
mkdir -p ~/gio-studio-cloud && cd ~/gio-studio-cloud && git init
python3.12 -m venv backend/.venv && source backend/.venv/bin/activate
```

- [ ] **Step 2: requirements.txt**

```
fastapi==0.115.*
uvicorn[standard]==0.32.*
supabase==2.9.*
httpx==0.27.*
pyjwt==2.9.*
python-multipart==0.0.*
pytest==8.*
pytest-asyncio==0.24.*
```

- [ ] **Step 3: Instalar**

Run: `pip install -r backend/requirements.txt`
Expected: instala sin error.

- [ ] **Step 4: .env.example**

```
OPENAI_API_KEY=sk-...
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...
SUPABASE_JWT_SECRET=super-secret-jwt
MARKUP=1.5
STORAGE_BUCKET=images
```

- [ ] **Step 5: Escribir el test de config (falla)**

```python
# backend/tests/test_config.py
import os
from app.config import get_settings

def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "svc")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "secret")
    monkeypatch.setenv("MARKUP", "1.5")
    get_settings.cache_clear()
    s = get_settings()
    assert s.openai_api_key == "sk-test"
    assert s.markup == 1.5
    assert s.storage_bucket == "images"  # default
```

- [ ] **Step 6: Run → FAIL** (`Run: cd backend && python -m pytest tests/test_config.py -v` → ModuleNotFoundError app.config)

- [ ] **Step 7: Implementar config.py**

```python
# backend/app/config.py
import os
from functools import lru_cache
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    supabase_url: str
    supabase_service_key: str
    supabase_jwt_secret: str
    markup: float
    storage_bucket: str

@lru_cache
def get_settings() -> Settings:
    return Settings(
        openai_api_key=os.environ["OPENAI_API_KEY"],
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_service_key=os.environ["SUPABASE_SERVICE_KEY"],
        supabase_jwt_secret=os.environ["SUPABASE_JWT_SECRET"],
        markup=float(os.environ.get("MARKUP", "1.5")),
        storage_bucket=os.environ.get("STORAGE_BUCKET", "images"),
    )
```

- [ ] **Step 8: conftest.py (pytest path + env por defecto)**

```python
# backend/tests/conftest.py
import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "svc")
os.environ.setdefault("SUPABASE_JWT_SECRET", "secret")
os.environ.setdefault("MARKUP", "1.5")
```

- [ ] **Step 9: Run → PASS**

- [ ] **Step 10: Commit**

```bash
git add backend/ && git commit -m "chore: scaffold backend FastAPI + config"
```

---

## Fase 1 — Esquema Supabase + RLS

### Task 1.1: Migración de esquema

**Files:**
- Create: `supabase/migrations/0001_schema.sql`

**Interfaces:**
- Produces: tablas `profiles`, `credits`, `credit_ledger`, `projects`, `generations`, `invites` (campos según spec).

- [ ] **Step 1: Escribir 0001_schema.sql**

```sql
-- 0001_schema.sql
create table profiles (
  id uuid primary key references auth.users on delete cascade,
  email text,
  display_name text,
  role text not null default 'user',           -- 'user' | 'admin'
  plan text not null default 'free',            -- 'free' | 'premium'
  created_at timestamptz not null default now()
);
create table credits (
  user_id uuid primary key references profiles(id) on delete cascade,
  balance_usd numeric(12,5) not null default 0,
  updated_at timestamptz not null default now()
);
create table credit_ledger (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  delta_usd numeric(12,5) not null,
  reason text not null,                          -- 'hold' | 'settle' | 'topup' | 'release'
  generation_id uuid,
  created_at timestamptz not null default now()
);
create table projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  name text not null,
  memory_text text default '',
  created_at timestamptz not null default now()
);
create table generations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  project_id uuid references projects(id) on delete set null,
  kind text not null,                           -- 'gen' | 'edit'
  prompt text not null default '',
  model text not null default 'gpt-image-2',
  size text, quality text, n int default 1,
  output_tokens int default 0, input_img_tokens int default 0,
  cost_usd numeric(12,5) not null default 0,
  storage_path text, thumb_path text,
  params jsonb default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create table invites (
  code text primary key,
  email text,
  created_by uuid references profiles(id),
  redeemed_by uuid references profiles(id),
  created_at timestamptz not null default now()
);
create index on generations(user_id, created_at desc);
create index on credit_ledger(user_id, created_at desc);
```

- [ ] **Step 2: Aplicar** (`Run: supabase db push` o pegar en el SQL editor del proyecto)
Expected: 6 tablas creadas, sin error.

- [ ] **Step 3: Commit** (`git add supabase && git commit -m "feat: esquema Postgres SaaS"`)

### Task 1.2: RLS

**Files:** Create: `supabase/migrations/0002_rls.sql`

- [ ] **Step 1: Escribir 0002_rls.sql**

```sql
-- 0002_rls.sql
alter table profiles enable row level security;
alter table credits enable row level security;
alter table credit_ledger enable row level security;
alter table projects enable row level security;
alter table generations enable row level security;
alter table invites enable row level security;

create policy own_profile on profiles for select using (auth.uid() = id);
create policy own_credits on credits for select using (auth.uid() = user_id);
create policy own_ledger on credit_ledger for select using (auth.uid() = user_id);
create policy own_projects on projects for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy own_generations on generations for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
-- credits / ledger los ESCRIBE solo el backend con service key (bypassa RLS); el cliente solo lee.
```

- [ ] **Step 2: Aplicar y verificar** — con un JWT de usuario A, `select * from generations` no devuelve filas de B. (Run en SQL editor con `set role`/token de prueba.)

- [ ] **Step 3: Commit**

### Task 1.3: Trigger de alta + invitaciones

**Files:** Create: `supabase/migrations/0003_invites_trigger.sql`

- [ ] **Step 1: Escribir trigger** (al crear un auth.user, exige invitación válida; crea profile + credits)

```sql
-- 0003_invites_trigger.sql
create or replace function handle_new_user() returns trigger
language plpgsql security definer as $$
declare inv invites%rowtype;
begin
  select * into inv from invites
    where (email = new.email or email is null) and redeemed_by is null
    order by created_at limit 1;
  if not found then
    raise exception 'No invitation for %', new.email;
  end if;
  insert into profiles(id, email) values (new.id, new.email);
  insert into credits(user_id, balance_usd) values (new.id, 0);
  update invites set redeemed_by = new.id where code = inv.code;
  return new;
end; $$;
create trigger on_auth_user_created
  after insert on auth.users for each row execute function handle_new_user();
```

- [ ] **Step 2: Probar** — insertar invite, registrar usuario con ese email → crea profile; registrar sin invite → falla. (manual en staging)

- [ ] **Step 3: Commit**

---

## Fase 2 — Cimientos del backend (Supabase + Auth)

### Task 2.1: Cliente Supabase

**Files:** Create: `app/supa.py`; Test: `tests/test_supa.py`

**Interfaces:**
- Produces: `admin_client()` → cliente con service key (bypassa RLS); `user_client(jwt: str)` → cliente con el token del usuario (respeta RLS).

- [ ] **Step 1: Test (falla)**

```python
# tests/test_supa.py
from app import supa
def test_admin_client_is_singleton():
    a, b = supa.admin_client(), supa.admin_client()
    assert a is b
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implementar**

```python
# app/supa.py
from functools import lru_cache
from supabase import create_client, Client
from .config import get_settings

@lru_cache
def admin_client() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_service_key)

def user_client(jwt: str) -> Client:
    s = get_settings()
    c = create_client(s.supabase_url, s.supabase_service_key)
    c.postgrest.auth(jwt)
    return c
```

- [ ] **Step 4: Run → PASS** · **Step 5: Commit**

### Task 2.2: Dependencia current_user (valida JWT)

**Files:** Create: `app/auth.py`; Test: `tests/test_auth.py`

**Interfaces:**
- Produces: `current_user(authorization: str) -> User` (dataclass con `id: str`, `email: str`); lanza `HTTPException(401)` si el token falta/inválido. `require_admin(user)` → 403 si no admin.

- [ ] **Step 1: Test (falla)**

```python
# tests/test_auth.py
import jwt, pytest
from fastapi import HTTPException
from app.auth import decode_token
from app.config import get_settings

def _tok(sub="u1", email="a@b.c"):
    return jwt.encode({"sub": sub, "email": email}, get_settings().supabase_jwt_secret, algorithm="HS256")

def test_decode_valid():
    u = decode_token(_tok())
    assert u.id == "u1" and u.email == "a@b.c"

def test_decode_bad():
    with pytest.raises(HTTPException):
        decode_token("garbage")
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implementar**

```python
# app/auth.py
from dataclasses import dataclass
import jwt
from fastapi import HTTPException, Header
from .config import get_settings

@dataclass
class User:
    id: str
    email: str

def decode_token(token: str) -> User:
    try:
        p = jwt.decode(token, get_settings().supabase_jwt_secret,
                       algorithms=["HS256"], audience="authenticated")
    except Exception:
        raise HTTPException(401, "Token inválido")
    return User(id=p["sub"], email=p.get("email", ""))

def current_user(authorization: str = Header(default="")) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Falta sesión")
    return decode_token(authorization[7:])

def require_admin(user: User) -> None:
    from .supa import admin_client
    r = admin_client().table("profiles").select("role").eq("id", user.id).single().execute()
    if (r.data or {}).get("role") != "admin":
        raise HTTPException(403, "Solo admin")
```

> Nota: el test usa HS256; si el proyecto Supabase emite tokens con otro algoritmo/clave, ajustar `algorithms` y `audience` aquí. Verificar contra un token real en staging antes de la Fase 9.

- [ ] **Step 4: Run → PASS** · **Step 5: Commit**

---

## Fase 3 — Cálculo de costo (puro, TDD)

### Task 3.1: pricing.py

**Files:** Create: `app/pricing.py`; Test: `tests/test_pricing.py`

**Interfaces:**
- Produces: `cost_from_usage(usage: dict, markup: float) -> dict` → `{"base_usd", "billed_usd", "output_tokens", "input_img_tokens"}`. Lee `output_tokens`, `input_tokens_details.image_tokens`, `input_tokens` (texto) del bloque `usage` que devuelve gpt-image-2.

- [ ] **Step 1: Test (falla)**

```python
# tests/test_pricing.py
from app.pricing import cost_from_usage

def test_cost_output_only():
    u = {"output_tokens": 1_000_000, "input_tokens": 0,
         "input_tokens_details": {"image_tokens": 0, "text_tokens": 0}}
    c = cost_from_usage(u, markup=1.0)
    assert c["base_usd"] == 30.0          # 1M * $30/1M
    assert c["billed_usd"] == 30.0

def test_cost_with_refs_and_markup():
    u = {"output_tokens": 100_000, "input_tokens": 50_000,
         "input_tokens_details": {"image_tokens": 50_000, "text_tokens": 0}}
    c = cost_from_usage(u, markup=1.5)
    # base = 0.1*30 + 0.05*8 = 3.0 + 0.4 = 3.4 ; billed = 5.1
    assert round(c["base_usd"], 5) == 3.4
    assert round(c["billed_usd"], 5) == 5.1
    assert c["input_img_tokens"] == 50_000
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implementar**

```python
# app/pricing.py
PRICE_OUT = 30.0           # USD / 1M output tokens
PRICE_IN = 5.0             # USD / 1M text input tokens
PRICE_IN_IMG = 8.0         # USD / 1M image input tokens (refs)

def cost_from_usage(usage: dict, markup: float) -> dict:
    out = int(usage.get("output_tokens", 0) or 0)
    det = usage.get("input_tokens_details", {}) or {}
    img = int(det.get("image_tokens", 0) or 0)
    txt = int(det.get("text_tokens", usage.get("input_tokens", 0)) or 0)
    base = out * PRICE_OUT / 1e6 + img * PRICE_IN_IMG / 1e6 + txt * PRICE_IN / 1e6
    return {"base_usd": round(base, 5), "billed_usd": round(base * markup, 5),
            "output_tokens": out, "input_img_tokens": img}
```

- [ ] **Step 4: Run → PASS** · **Step 5: Commit**

---

## Fase 4 — Motor de créditos (transaccional, TDD)

> Para evitar carreras y "cobrar sin entregar", el saldo se maneja con una función SQL `apply_credit(p_user, p_delta, p_reason, p_gen)` que actualiza `credits.balance_usd` e inserta en `credit_ledger` atómicamente, y un chequeo de saldo. El backend solo la invoca.

### Task 4.1: Función SQL apply_credit + check_balance

**Files:** Create: `supabase/migrations/0004_credit_fns.sql`

- [ ] **Step 1: Escribir**

```sql
-- 0004_credit_fns.sql
create or replace function apply_credit(p_user uuid, p_delta numeric, p_reason text, p_gen uuid default null)
returns numeric language plpgsql security definer as $$
declare new_bal numeric;
begin
  update credits set balance_usd = balance_usd + p_delta, updated_at = now()
    where user_id = p_user returning balance_usd into new_bal;
  insert into credit_ledger(user_id, delta_usd, reason, generation_id)
    values (p_user, p_delta, p_reason, p_gen);
  return new_bal;
end; $$;
```

- [ ] **Step 2: Aplicar** · **Step 3: Commit**

### Task 4.2: credits.py (hold / settle / release)

**Files:** Create: `app/credits.py`; Test: `tests/test_credits.py`

**Interfaces:**
- Consumes: `app.supa.admin_client`.
- Produces: `get_balance(user_id) -> float`; `ensure_funds(user_id, estimate)` (lanza `HTTPException(402)` si saldo < estimate); `hold(user_id, amount) -> None` (RPC apply_credit -amount reason='hold'); `release(user_id, amount)`; `settle(user_id, hold_amount, real_amount, gen_id)` (devuelve hold y aplica costo real: net delta = hold_amount - real_amount).

- [ ] **Step 1: Test (falla, con admin_client mockeado)**

```python
# tests/test_credits.py
from unittest.mock import MagicMock
import pytest
from fastapi import HTTPException
from app import credits

def _fake_client(balance):
    c = MagicMock()
    c.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {"balance_usd": balance}
    c.rpc.return_value.execute.return_value.data = balance
    return c

def test_ensure_funds_ok(monkeypatch):
    monkeypatch.setattr(credits, "admin_client", lambda: _fake_client(10.0))
    credits.ensure_funds("u1", 5.0)   # no lanza

def test_ensure_funds_insufficient(monkeypatch):
    monkeypatch.setattr(credits, "admin_client", lambda: _fake_client(1.0))
    with pytest.raises(HTTPException) as e:
        credits.ensure_funds("u1", 5.0)
    assert e.value.status_code == 402

def test_settle_nets_hold_minus_real(monkeypatch):
    c = _fake_client(0.0)
    monkeypatch.setattr(credits, "admin_client", lambda: c)
    credits.settle("u1", hold_amount=5.0, real_amount=3.4, gen_id="g1")
    # debe devolver 5.0 y cobrar 3.4 → delta neto +1.6
    args = c.rpc.call_args
    assert args[0][0] == "apply_credit"
    assert round(args[0][1]["p_delta"], 5) == 1.6
    assert args[0][1]["p_reason"] == "settle"
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implementar**

```python
# app/credits.py
from fastapi import HTTPException
from .supa import admin_client

def get_balance(user_id: str) -> float:
    r = admin_client().table("credits").select("balance_usd").eq("user_id", user_id).single().execute()
    return float((r.data or {}).get("balance_usd", 0) or 0)

def ensure_funds(user_id: str, estimate: float) -> None:
    if get_balance(user_id) < estimate:
        raise HTTPException(402, "Saldo insuficiente")

def _apply(user_id, delta, reason, gen=None):
    admin_client().rpc("apply_credit",
        {"p_user": user_id, "p_delta": delta, "p_reason": reason, "p_gen": gen}).execute()

def hold(user_id: str, amount: float) -> None:
    _apply(user_id, -abs(amount), "hold")

def release(user_id: str, amount: float) -> None:
    _apply(user_id, abs(amount), "release")

def settle(user_id: str, hold_amount: float, real_amount: float, gen_id: str) -> None:
    # devuelve el hold y aplica el costo real en un solo delta neto
    _apply(user_id, abs(hold_amount) - abs(real_amount), "settle", gen_id)

def topup(user_id: str, amount: float) -> None:
    _apply(user_id, abs(amount), "topup")
```

- [ ] **Step 4: Run → PASS** · **Step 5: Commit**

---

## Fase 5 — Generación de imagen (port + storage)

### Task 5.1: storage.py — subir a Supabase Storage + URL firmada

**Files:** Create: `app/storage.py`; Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `upload_png(user_id, gen_id, png: bytes) -> str` (devuelve storage_path `{user}/generations/{gen}.png`); `upload_thumb(user_id, gen_id, webp: bytes) -> str`; `signed_url(path: str, ttl=3600) -> str`; `make_thumb(png: bytes) -> bytes` (256px webp).

- [ ] **Step 1: Test del path (falla)**

```python
# tests/test_storage.py
from app import storage
from unittest.mock import MagicMock
def test_upload_png_path(monkeypatch):
    cli = MagicMock()
    monkeypatch.setattr(storage, "admin_client", lambda: cli)
    p = storage.upload_png("u1", "g1", b"\x89PNG...")
    assert p == "u1/generations/g1.png"
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implementar** (usa `Pillow` para la miniatura → añadir `pillow` a requirements y reinstalar)

```python
# app/storage.py
import io
from PIL import Image
from .config import get_settings
from .supa import admin_client

def _bucket():
    return admin_client().storage.from_(get_settings().storage_bucket)

def upload_png(user_id, gen_id, png: bytes) -> str:
    path = f"{user_id}/generations/{gen_id}.png"
    _bucket().upload(path, png, {"content-type": "image/png", "upsert": "true"})
    return path

def upload_thumb(user_id, gen_id, webp: bytes) -> str:
    path = f"{user_id}/thumbs/{gen_id}.webp"
    _bucket().upload(path, webp, {"content-type": "image/webp", "upsert": "true"})
    return path

def signed_url(path: str, ttl: int = 3600) -> str:
    return _bucket().create_signed_url(path, ttl)["signedURL"]

def make_thumb(png: bytes, size: int = 256) -> bytes:
    im = Image.open(io.BytesIO(png)).convert("RGB")
    im.thumbnail((size, size))
    out = io.BytesIO(); im.save(out, "WEBP", quality=80)
    return out.getvalue()
```

- [ ] **Step 4: Run → PASS** · **Step 5: Commit**

### Task 5.2: openai_images.py — llamada a gpt-image-2 (port de h_generate)

**Files:** Create: `app/openai_images.py`; Test: `tests/test_openai_images.py`

**Interfaces:**
- Produces: `generate(prompt, size, quality, fmt, n, moderation) -> dict` (devuelve `{"b64_images": [bytes...], "usage": {...}}`); `edit(prompt, size, quality, fmt, images: list[bytes], mask: bytes|None, moderation) -> dict`. Usa `OPENAI_API_KEY` del settings. Sin streaming (n se gestiona normal; el streaming del local era para mantener viva la conexión — en FastAPI usamos timeout alto y respuesta normal).

- [ ] **Step 1: Test con httpx mockeado (falla)**

```python
# tests/test_openai_images.py
import base64, json
from app import openai_images

class _Resp:
    status_code = 200
    def __init__(self, payload): self._p = payload
    def json(self): return self._p
    def raise_for_status(self): pass

def test_generate_parses_b64_and_usage(monkeypatch):
    png = b"\x89PNGdata"
    payload = {"data": [{"b64_json": base64.b64encode(png).decode()}],
               "usage": {"output_tokens": 1000, "input_tokens": 10,
                         "input_tokens_details": {"image_tokens": 0, "text_tokens": 10}}}
    monkeypatch.setattr(openai_images.httpx, "post", lambda *a, **k: _Resp(payload))
    r = openai_images.generate("gato", "1024x1024", "auto", "png", 1, "low")
    assert r["b64_images"][0] == png
    assert r["usage"]["output_tokens"] == 1000
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implementar** (port fiel del payload de `h_generate`/`h_edit`)

```python
# app/openai_images.py
import base64, httpx
from .config import get_settings

API_GEN = "https://api.openai.com/v1/images/generations"
API_EDIT = "https://api.openai.com/v1/images/edits"
MODEL = "gpt-image-2"

def _hdr():
    return {"Authorization": f"Bearer {get_settings().openai_api_key}"}

def _parse(data: dict) -> dict:
    imgs = [base64.b64decode(d["b64_json"]) for d in data.get("data", [])]
    return {"b64_images": imgs, "usage": data.get("usage", {})}

def generate(prompt, size, quality, fmt, n, moderation) -> dict:
    payload = {"model": MODEL, "prompt": prompt, "size": size, "quality": quality,
               "n": n, "output_format": fmt, "moderation": moderation}
    r = httpx.post(API_GEN, json=payload, headers={**_hdr(), "Content-Type": "application/json"}, timeout=300)
    r.raise_for_status()
    return _parse(r.json())

def edit(prompt, size, quality, fmt, images, mask, moderation) -> dict:
    files = [("image[]", (f"ref{i}.png", img, "image/png")) for i, img in enumerate(images)]
    if mask:
        files.append(("mask", ("mask.png", mask, "image/png")))
    data = {"model": MODEL, "prompt": prompt, "size": size, "quality": quality,
            "output_format": fmt, "moderation": moderation, "n": "1"}
    r = httpx.post(API_EDIT, data=data, files=files, headers=_hdr(), timeout=300)
    r.raise_for_status()
    return _parse(r.json())
```

- [ ] **Step 4: Run → PASS** · **Step 5: Commit**

### Task 5.3: Router /api/gen y /api/edit (orquesta hold→OpenAI→storage→settle)

**Files:** Create: `app/routers/generate.py`; Test: `tests/test_generate.py`

**Interfaces:**
- Consumes: `current_user`, `credits`, `openai_images`, `storage`, `pricing`, `admin_client`, `get_settings`.
- Produces: `POST /api/gen` body `{prompt,size,quality,output_format,n,moderation,project_id?}` → `{images:[{url,thumb_url}], cost_usd, balance_usd, generation_id}`. `POST /api/edit` body añade `images:[b64...]`, `mask?:b64`.

- [ ] **Step 1: Test de flujo feliz (falla, todo mockeado)**

```python
# tests/test_generate.py
import base64
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
import app.routers.generate as gen
from app.main import app
from app.auth import current_user, User

def _override():
    app.dependency_overrides[current_user] = lambda: User(id="u1", email="a@b.c")

def test_gen_happy(monkeypatch):
    _override()
    monkeypatch.setattr(gen.credits, "ensure_funds", lambda u, e: None)
    monkeypatch.setattr(gen.credits, "hold", lambda u, a: None)
    monkeypatch.setattr(gen.credits, "settle", lambda **k: None)
    monkeypatch.setattr(gen.credits, "get_balance", lambda u: 7.0)
    monkeypatch.setattr(gen.openai_images, "generate",
        lambda **k: {"b64_images": [b"PNG"], "usage": {"output_tokens": 1000, "input_tokens": 5,
                     "input_tokens_details": {"image_tokens": 0, "text_tokens": 5}}})
    monkeypatch.setattr(gen.storage, "make_thumb", lambda p: b"WEBP")
    monkeypatch.setattr(gen.storage, "upload_png", lambda u, g, p: f"{u}/generations/{g}.png")
    monkeypatch.setattr(gen.storage, "upload_thumb", lambda u, g, p: f"{u}/thumbs/{g}.webp")
    monkeypatch.setattr(gen.storage, "signed_url", lambda p, ttl=3600: "https://signed/" + p)
    monkeypatch.setattr(gen, "_insert_generation", lambda **k: "g1")
    c = TestClient(app)
    r = c.post("/api/gen", json={"prompt": "gato", "size": "1024x1024",
               "quality": "auto", "output_format": "png", "n": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["images"][0]["url"].startswith("https://signed/")
    assert body["cost_usd"] > 0
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implementar**

```python
# app/routers/generate.py
import uuid
from fastapi import APIRouter, Depends, HTTPException
import httpx
from ..auth import current_user, User
from ..config import get_settings
from ..supa import admin_client
from .. import credits, openai_images, storage, pricing

router = APIRouter(prefix="/api")

def _estimate(size: str, quality: str, n: int) -> float:
    # estimación previa (aprox.) en línea con el local: tokens ~ por MP/quality.
    w, h = (int(x) for x in size.split("x"))
    mp = w * h / 1e6
    q = {"low": 0.4, "medium": 1.0, "high": 2.0, "auto": 1.0}.get(quality, 1.0)
    out_tokens = int(mp * 1000 * q) * n
    base = out_tokens * pricing.PRICE_OUT / 1e6
    return round(base * get_settings().markup, 5)

def _insert_generation(**kw) -> str:
    gid = str(uuid.uuid4())
    admin_client().table("generations").insert({"id": gid, **kw}).execute()
    return gid

def _finish(user: User, kind, prompt, size, quality, n, project_id, result):
    cost = pricing.cost_from_usage(result["usage"], get_settings().markup)
    out_urls = []
    gid = None
    for png in result["b64_images"]:
        gid = str(uuid.uuid4())
        path = storage.upload_png(user.id, gid, png)
        tpath = storage.upload_thumb(user.id, gid, storage.make_thumb(png))
        _insert_generation(id=gid, user_id=user.id, project_id=project_id, kind=kind,
                           prompt=prompt, size=size, quality=quality, n=n,
                           output_tokens=cost["output_tokens"], input_img_tokens=cost["input_img_tokens"],
                           cost_usd=cost["billed_usd"], storage_path=path, thumb_path=tpath)
        out_urls.append({"url": storage.signed_url(path), "thumb_url": storage.signed_url(tpath)})
    return cost, out_urls, gid

@router.post("/gen")
def gen_endpoint(body: dict, user: User = Depends(current_user)):
    size = body.get("size", "1536x1024"); quality = body.get("quality", "auto")
    fmt = body.get("output_format", "png"); n = int(body.get("n", 1))
    est = _estimate(size, quality, n)
    credits.ensure_funds(user.id, est)
    credits.hold(user.id, est)
    try:
        result = openai_images.generate(prompt=body.get("prompt", ""), size=size,
                    quality=quality, fmt=fmt, n=n, moderation=body.get("moderation", "low"))
    except httpx.HTTPStatusError as e:
        credits.release(user.id, est)
        raise HTTPException(502, f"OpenAI: {e.response.text[:200]}")
    except Exception:
        credits.release(user.id, est)
        raise HTTPException(502, "Fallo al generar")
    cost, urls, gid = _finish(user, "gen", body.get("prompt", ""), size, quality, n, body.get("project_id"), result)
    credits.settle(user_id=user.id, hold_amount=est, real_amount=cost["billed_usd"], gen_id=gid)
    return {"images": urls, "cost_usd": cost["billed_usd"], "balance_usd": credits.get_balance(user.id), "generation_id": gid}

@router.post("/edit")
def edit_endpoint(body: dict, user: User = Depends(current_user)):
    import base64
    size = body.get("size", "1024x1024"); quality = body.get("quality", "auto")
    fmt = body.get("output_format", "png")
    imgs = [base64.b64decode(i["b64"]) for i in body.get("images", [])]
    if not imgs:
        raise HTTPException(400, "No hay imágenes de referencia.")
    mask = base64.b64decode(body["mask"]["b64"]) if body.get("mask") else None
    est = _estimate(size, quality, 1)
    credits.ensure_funds(user.id, est)
    credits.hold(user.id, est)
    try:
        result = openai_images.edit(prompt=body.get("prompt", ""), size=size, quality=quality,
                    fmt=fmt, images=imgs, mask=mask, moderation=body.get("moderation", "low"))
    except httpx.HTTPStatusError as e:
        credits.release(user.id, est)
        raise HTTPException(502, f"OpenAI: {e.response.text[:200]}")
    except Exception:
        credits.release(user.id, est)
        raise HTTPException(502, "Fallo al editar")
    cost, urls, gid = _finish(user, "edit", body.get("prompt", ""), size, quality, 1, body.get("project_id"), result)
    credits.settle(user_id=user.id, hold_amount=est, real_amount=cost["billed_usd"], gen_id=gid)
    return {"images": urls, "cost_usd": cost["billed_usd"], "balance_usd": credits.get_balance(user.id), "generation_id": gid}
```

- [ ] **Step 4: Crear `app/main.py` mínimo que monta el router**

```python
# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routers import generate
app = FastAPI(title="Gio Studio SaaS")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(generate.router)

@app.get("/health")
def health(): return {"ok": True}
```

- [ ] **Step 5: Run → PASS** (`python -m pytest tests/test_generate.py -v`) · **Step 6: Commit**

---

## Fase 6 — Biblioteca (historial / proyectos / saldo)

### Task 6.1: Router /api/history, /api/projects, /api/credits

**Files:** Create: `app/routers/library.py`; Test: `tests/test_library.py`

**Interfaces:**
- Produces: `GET /api/history?project_id=` → `{items:[{id,prompt,size,cost_usd,created_at,thumb_url}]}` (URLs firmadas); `GET /api/credits` → `{balance_usd}`; `POST /api/projects` `{name}` y `GET /api/projects`.

- [ ] **Step 1: Test (falla)**

```python
# tests/test_library.py
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
import app.routers.library as lib
from app.main import app
from app.auth import current_user, User

def test_history_returns_signed(monkeypatch):
    app.dependency_overrides[current_user] = lambda: User(id="u1", email="a@b.c")
    cli = MagicMock()
    cli.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = \
        [{"id": "g1", "prompt": "x", "size": "1024x1024", "cost_usd": 0.1,
          "created_at": "2026-06-24", "thumb_path": "u1/thumbs/g1.webp"}]
    monkeypatch.setattr(lib, "admin_client", lambda: cli)
    monkeypatch.setattr(lib.storage, "signed_url", lambda p, ttl=3600: "https://s/" + p)
    r = TestClient(app).get("/api/history")
    assert r.json()["items"][0]["thumb_url"] == "https://s/u1/thumbs/g1.webp"
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implementar**

```python
# app/routers/library.py
from fastapi import APIRouter, Depends
from ..auth import current_user, User
from ..supa import admin_client
from .. import credits, storage

router = APIRouter(prefix="/api")

@router.get("/credits")
def get_credits(user: User = Depends(current_user)):
    return {"balance_usd": credits.get_balance(user.id)}

@router.get("/history")
def history(project_id: str | None = None, user: User = Depends(current_user)):
    q = admin_client().table("generations").select(
        "id,prompt,size,quality,cost_usd,created_at,storage_path,thumb_path").eq("user_id", user.id)
    if project_id:
        q = q.eq("project_id", project_id)
    rows = q.order("created_at", desc=True).limit(200).execute().data or []
    for r in rows:
        if r.get("thumb_path"): r["thumb_url"] = storage.signed_url(r["thumb_path"])
        if r.get("storage_path"): r["url"] = storage.signed_url(r["storage_path"])
    return {"items": rows}

@router.get("/projects")
def list_projects(user: User = Depends(current_user)):
    rows = admin_client().table("projects").select("*").eq("user_id", user.id).order("created_at").execute().data or []
    return {"items": rows}

@router.post("/projects")
def create_project(body: dict, user: User = Depends(current_user)):
    r = admin_client().table("projects").insert(
        {"user_id": user.id, "name": body.get("name", "Sin título"), "memory_text": body.get("memory_text", "")}).execute()
    return {"project": (r.data or [None])[0]}
```

- [ ] **Step 4: Montar router en main.py** (`app.include_router(library.router)`) · **Step 5: Run → PASS** · **Step 6: Commit**

---

## Fase 7 — Admin de la beta (invites + recarga)

### Task 7.1: Router admin

**Files:** Create: `app/routers/admin.py`; Test: `tests/test_admin.py`

**Interfaces:**
- Produces: `POST /api/admin/invite` `{email?}` → `{code}`; `POST /api/admin/topup` `{user_id, amount_usd}` → `{balance_usd}`; `GET /api/admin/users` → lista (id,email,plan,balance). Todas exigen `require_admin`.

- [ ] **Step 1: Test require_admin bloquea no-admin (falla)**

```python
# tests/test_admin.py
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
import app.routers.admin as adm
from app.main import app
from app.auth import current_user, User

def test_topup_forbidden_for_user(monkeypatch):
    app.dependency_overrides[current_user] = lambda: User(id="u1", email="a@b.c")
    cli = MagicMock()
    cli.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {"role": "user"}
    monkeypatch.setattr(adm, "admin_client", lambda: cli)
    r = TestClient(app).post("/api/admin/topup", json={"user_id": "u2", "amount_usd": 5})
    assert r.status_code == 403
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implementar**

```python
# app/routers/admin.py
import uuid
from fastapi import APIRouter, Depends
from ..auth import current_user, require_admin, User
from ..supa import admin_client
from .. import credits

router = APIRouter(prefix="/api/admin")

@router.post("/invite")
def invite(body: dict, user: User = Depends(current_user)):
    require_admin(user)
    code = uuid.uuid4().hex[:10]
    admin_client().table("invites").insert({"code": code, "email": body.get("email"), "created_by": user.id}).execute()
    return {"code": code}

@router.post("/topup")
def topup(body: dict, user: User = Depends(current_user)):
    require_admin(user)
    credits.topup(body["user_id"], float(body["amount_usd"]))
    return {"balance_usd": credits.get_balance(body["user_id"])}

@router.get("/users")
def users(user: User = Depends(current_user)):
    require_admin(user)
    profs = admin_client().table("profiles").select("id,email,plan,role").execute().data or []
    creds = {c["user_id"]: c["balance_usd"] for c in (admin_client().table("credits").select("user_id,balance_usd").execute().data or [])}
    for p in profs: p["balance_usd"] = creds.get(p["id"], 0)
    return {"items": profs}
```

- [ ] **Step 4: Montar router · Step 5: Run → PASS · Step 6: Commit**

---

## Fase 8 — Front (⚠️ GATED) + adaptadores + login

### Task 8.0: GATE — confirmar con Gio antes de clonar

- [ ] **Step 1: DETENERSE.** Preguntar a Gio: "Voy a extraer/clonar el front de `server.py`. ¿Ya hiciste tus últimos ajustes en el Gio Studio local?" NO continuar sin un "sí" explícito.

### Task 8.1: Script de extracción repetible

**Files:** Create: `extract_web.py`, `SYNC.md`

- [ ] **Step 1:** Escribir `extract_web.py` que lea `~/GptPlatform/server.py`, extraiga el bloque HTML servido por la ruta `/` (la cadena del template principal) y lo escriba a `web/index.html`, separando `<style>`→`web/app.css` y `<script>`→`web/app.js` si conviene. (Determinista; re-ejecutable.)
- [ ] **Step 2:** Ejecutar y verificar que `web/index.html` abre visualmente igual que el local (sin backend aún, solo el chrome).
- [ ] **Step 3:** Crear `SYNC.md` con: hash actual de `server.py` (`git -C ~/GptPlatform rev-parse HEAD`), fecha, y el procedimiento de re-sync del spec.
- [ ] **Step 4: Commit**

### Task 8.2: Capa de adaptadores (login, quitar modal key, costo→saldo, fetch→API)

**Files:** Create: `web/saas-adapter.js`; Modify: `web/index.html` (cargar Supabase JS + adapter)

- [ ] **Step 1:** Añadir al `<head>` de `web/index.html` el SDK de Supabase y `saas-adapter.js`.
- [ ] **Step 2:** En `saas-adapter.js`: inicializar Supabase con URL+anon key; si no hay sesión → mostrar pantalla de login magic-link y ocultar la app; al haber sesión → guardar el access_token.
- [ ] **Step 3:** Sobre-escribir `window.fetch` (o el wrapper de red del front) para: (a) anteponer la base del backend a las rutas `/generate`,`/edit`,`/history`,`/projects`,`/config`,`/galeria`,etc. mapeándolas a `/api/...`; (b) añadir `Authorization: Bearer <token>`; (c) ocultar el modal de API key (la llave es del servidor).
- [ ] **Step 4:** Reemplazar la etiqueta "costo de sesión" por "saldo" leyendo `GET /api/credits` al cargar y tras cada generación (la respuesta de `/api/gen` ya trae `balance_usd`).
- [ ] **Step 5:** Verificación visual: idéntico al local salvo login + etiqueta de saldo. (Usar el navegador/Playwright.)
- [ ] **Step 6: Commit**

> Regla de oro: TODA diferencia del SaaS vive en `saas-adapter.js`. No editar a mano el HTML/JS extraído salvo el `<head>` (paso 1). Así re-ejecutar `extract_web.py` no pierde las adaptaciones.

---

## Fase 9 — Despliegue

### Task 9.1: Backend en Render/Railway

- [ ] **Step 1:** `backend/Procfile` o config: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- [ ] **Step 2:** Crear servicio en Render/Railway apuntando a `backend/`; cargar env (OPENAI_API_KEY, SUPABASE_*, MARKUP).
- [ ] **Step 3:** Verificar `GET /health` responde `{"ok": true}` en la URL pública.
- [ ] **Step 4:** Ajustar CORS en `main.py` al dominio real del front (sustituir `allow_origins=["*"]`).
- [ ] **Step 5: Commit**

### Task 9.2: Front estático + dominio

- [ ] **Step 1:** Desplegar `web/` en Vercel (estático). Configurar la base del backend en `saas-adapter.js` (env o constante).
- [ ] **Step 2:** Subdominio `studio.<dominio>`; probar login + una generación end-to-end con saldo recargado a mano.
- [ ] **Step 3: Commit + tag `beta-1`**

---

## Fase 10 — Cierre

### Task 10.1: Test de humo end-to-end (staging)

- [ ] **Step 1:** Con un usuario invitado real: login → generar 1 imagen → aparece en historial (nube) → saldo baja por el costo real → cerrar sesión y volver: el historial persiste. Documentar en `SYNC.md`/README.
- [ ] **Step 2:** Verificar aislamiento: un segundo usuario NO ve el historial del primero.
- [ ] **Step 3: Commit**

---

## Self-Review (cobertura del spec)

- Auth + invitaciones → Tasks 1.3, 2.2, 7.1 ✅
- Datos por usuario + RLS → Tasks 1.1, 1.2 ✅
- Historial en object storage → Tasks 5.1, 5.3, 6.1 ✅
- Generación portada (/gen, /edit) + costo desde usage → Tasks 3.1, 5.2, 5.3 ✅
- Créditos hold/settle/release + margen → Tasks 4.1, 4.2, 5.3 ✅
- Plan free/premium (campo) → Task 1.1 ✅ (lógica de "Mis imágenes" local vs nube: Fase 8 adapter + sub-proyecto posterior para el detalle de carpeta local — se deja anotado)
- Integración front (login, quitar modal key, costo→saldo) → Tasks 8.1, 8.2 ✅
- Despliegue → Fase 9 ✅
- Manejo de errores (saldo, OpenAI, release del hold) → Task 5.3 ✅
- GATE de no-clonar-front → Task 8.0 ✅
- Sincronización local→SaaS → Tasks 8.1 (extract_web.py + SYNC.md) ✅

**Gap anotado:** la mecánica fina de "Mis imágenes" como carpeta local (File System Access API en Chrome/Edge) se implementa en la Fase 8 como parte del adapter solo a nivel de gate/aviso; el lector de carpeta local completo y el modo Premium-cloud-shelf se detallan mejor como su propio mini-spec antes de construirlos. Para la beta basta historial en la nube. Confirmar con Gio si quiere la carpeta local ya en la beta o como fast-follow.
```
