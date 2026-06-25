#!/usr/bin/env python3
"""
Estudio v4 — gpt-image-2 (OpenAI) · app independiente
UI premium minimalista. Crear + Editar, referencias en ambos, memoria visual por
proyecto, historial con filtro y borrado, estimador de precio, moderación,
presets completos incl. anamórficos, editor de máscara integrado, pegado desde
portapapeles, atajos de teclado, resultados múltiples. Sin dependencias: solo Python 3.
"""
import io, json, base64, os, re, shutil, struct, subprocess, threading, time, uuid, urllib.request, urllib.error, zipfile, zlib, hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

LOCK = threading.Lock()  # serializa lecturas-escrituras de los JSON

PORT = int(os.environ.get("STUDIO_PORT", "7860"))
HOME = Path.home()
KEY_FILE = HOME / ".openai_key"
EL_KEY_FILE = HOME / ".elevenlabs_key"
EL_API = "https://api.elevenlabs.io/v1"
FAL_KEY_FILE = HOME / ".fal_key"
FAL_QUEUE = "https://queue.fal.run"
VIDEO_MODELS = {
    "seedance": {"t2v": "bytedance/seedance-2.0/text-to-video", "i2v": "bytedance/seedance-2.0/image-to-video",
                 "r2v": "bytedance/seedance-2.0/reference-to-video"},
    "seedance-fast": {"t2v": "bytedance/seedance-2.0/fast/text-to-video", "i2v": "bytedance/seedance-2.0/fast/image-to-video",
                      "r2v": "bytedance/seedance-2.0/fast/reference-to-video"},
    "kling-pro": {"t2v": "fal-ai/kling-video/v3/pro/text-to-video", "i2v": "fal-ai/kling-video/v3/pro/image-to-video"},
    "kling-std": {"t2v": "fal-ai/kling-video/v3/standard/text-to-video", "i2v": "fal-ai/kling-video/v3/standard/image-to-video"},
    "omnihuman": {"av": "fal-ai/bytedance/omnihuman/v1.5"},
    "omnihuman-v1": {"av": "fal-ai/bytedance/omnihuman"},
}
PENDING_VIDEOS = {}  # request_id -> {model_id, meta}; persistido en JOBS_JSON
ROOT = HOME / "image-studio"
HIST_DIR = ROOT / "historial"
HIST_JSON = ROOT / "historial.json"
PROJ_JSON = ROOT / "proyectos.json"
PROMPTS_JSON = ROOT / "prompts.json"   # biblioteca de prompts (categorías + items)
CONF_JSON = ROOT / "config.json"
JOBS_JSON = ROOT / "jobs.json"
PROJ_DIR = ROOT / "proyectos"
SHELF_DIR = ROOT / "estante"          # estante de imágenes propias (no van a OpenAI)
SHELF_JSON = ROOT / "estante.json"
TRASH_DIR = ROOT / ".papelera"        # soft-delete: deshacer borrados
THUMBS_DIR = ROOT / ".thumbs"         # miniaturas en caché para las cuadrículas (no full-res)
HIST_DIR.mkdir(parents=True, exist_ok=True)
PROJ_DIR.mkdir(parents=True, exist_ok=True)
SHELF_DIR.mkdir(parents=True, exist_ok=True)
TRASH_DIR.mkdir(parents=True, exist_ok=True)

PRICE_OUT = 30.0
PRICE_IN = 5.0          # USD por 1M de tokens de texto de entrada
PRICE_IN_IMG = 8.0      # USD por 1M de tokens de imagen de entrada (referencias)
PRICE_IN_IMG_CACHED = 2.0  # USD por 1M de tokens de imagen de entrada cacheados
DISTILL_MODEL = "gpt-4o-mini"
DETECT_MODEL = "gpt-4o"   # detección de personas/objetos + orientación (mejor localización que mini)
API_GEN = "https://api.openai.com/v1/images/generations"
API_EDIT = "https://api.openai.com/v1/images/edits"
API_CHAT = "https://api.openai.com/v1/chat/completions"
API_MODELS = "https://api.openai.com/v1/models"
API_SPEECH = "https://api.openai.com/v1/audio/speech"
API_TRANSC = "https://api.openai.com/v1/audio/transcriptions"
API_TRANSL = "https://api.openai.com/v1/audio/translations"
TTS_PRICE = {"tts-1": 15.0, "tts-1-hd": 30.0}  # USD por 1M de caracteres
STT_PRICE = {"whisper-1": 0.006, "gpt-4o-transcribe": 0.006, "gpt-4o-mini-transcribe": 0.003}  # USD por minuto
MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
        "gif": "image/gif",
        "mp3": "audio/mpeg", "wav": "audio/wav", "aac": "audio/aac", "flac": "audio/flac",
        "opus": "audio/ogg", "pcm": "application/octet-stream", "mp4": "video/mp4",
        "txt": "text/plain; charset=utf-8", "srt": "text/plain; charset=utf-8",
        "vtt": "text/vtt", "json": "application/json"}

# Requisitos de imagen de entrada de OpenAI (visión / referencias)
IMG_MAX_PAYLOAD = 512 * 1024 * 1024   # 512 MB total por petición
IMG_MAX_COUNT = 1500                   # imágenes por petición


def sniff_image(raw):
    """Devuelve la extensión si los bytes son un tipo aceptado por OpenAI (png/jpeg/webp/gif), o None."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def img_dims(raw):
    """(w, h) de PNG/JPEG/WebP sin dependencias, o None si no se puede leer."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return tuple(struct.unpack(">II", raw[16:24]))
        if raw[:3] == b"\xff\xd8\xff":  # JPEG: recorrer marcadores hasta el SOF
            i, n = 2, len(raw)
            while i + 9 < n:
                if raw[i] != 0xFF:
                    i += 1
                    continue
                m = raw[i + 1]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    return (w, h)
                if m == 0xD8 or m == 0xD9 or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            fmt = raw[12:16]
            if fmt == b"VP8 ":
                return (struct.unpack("<H", raw[26:28])[0] & 0x3FFF, struct.unpack("<H", raw[28:30])[0] & 0x3FFF)
            if fmt == b"VP8L":
                b0, b1, b2, b3 = raw[21], raw[22], raw[23], raw[24]
                return (1 + (((b1 & 0x3F) << 8) | b0), 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6)))
            if fmt == b"VP8X":
                return (1 + (raw[24] | (raw[25] << 8) | (raw[26] << 16)), 1 + (raw[27] | (raw[28] << 8) | (raw[29] << 16)))
    except Exception:
        pass
    return None


def upscale_size(w, h, factor=2.0):
    """Tamaño objetivo para gpt-image-2 (2× con el mismo aspecto, dentro de límites: ÷16, ≤3840, ≤8.29MP)."""
    tw, th = w * factor, h * factor
    m = max(tw, th)
    if m > 3840:
        tw, th = tw * 3840 / m, th * 3840 / m
    snap = lambda v: max(512, int(round(v / 16)) * 16)
    tw, th = snap(tw), snap(th)
    if tw * th > 8294400:
        s = (8294400 / (tw * th)) ** 0.5
        tw, th = snap(tw * s), snap(th * s)
    return f"{tw}x{th}"


def key():
    return KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""


def el_key():
    return EL_KEY_FILE.read_text().strip() if EL_KEY_FILE.exists() else ""


def fal_key():
    return FAL_KEY_FILE.read_text().strip() if FAL_KEY_FILE.exists() else ""


def load_json(p, d):
    # si el archivo está corrupto o falta, intenta el respaldo .bak
    for cand in (p, p.with_suffix(p.suffix + ".bak")):
        try:
            return json.loads(cand.read_text())
        except Exception:
            continue
    return d


def save_jobs():
    # persiste los trabajos de video en curso para sobrevivir reinicios del server
    try:
        JOBS_JSON.write_text(json.dumps(PENDING_VIDEOS, ensure_ascii=False))
    except Exception:
        pass


def save_json(p, data):
    # escritura atómica + respaldo del estado anterior
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    if p.exists():
        try:
            shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
        except Exception:
            pass
    tmp.replace(p)


ACTIVE_PROJ = ""   # proyecto activo del estudio (lo fija /setproject). "" o "general" = carpetas originales (proyecto General)
ACTIVE_SUB = ""    # subproyecto activo del estudio (lo fija /setproject). "" = raíz del proyecto


def is_general(proj):
    return (proj or "").strip().lower() in ("", "general")


def _sub_safe(sub):
    # nombre de carpeta seguro del subproyecto ("" = raíz del proyecto)
    s = (sub or "").strip()
    return safe(s) if s else ""


def psub_base(proj, sub):
    # carpeta base de un subproyecto: proyectos/<proj>/sub/<sub>/  (proj_folder cubre General→"general")
    return proj_folder(proj) / "sub" / _sub_safe(sub)


def list_subs(proj):
    # enumera subproyectos por escaneo de la carpeta sub/ (las carpetas mandan, no un JSON)
    base = proj_folder(proj) / "sub"
    out = []
    if base.is_dir():
        for d in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir():
                continue
            label = ""
            lf = d / "label.txt"   # nombre legible (los espacios/acentos se pierden en safe())
            if lf.exists():
                try:
                    label = lf.read_text().strip()
                except Exception:
                    pass
            out.append({"key": d.name, "label": label or d.name})
    # orden personalizado (arrastrable): _order.json manda; lo no listado va al final (alfabético)
    order = load_json(base / "_order.json", []) if base.is_dir() else []
    if order:
        pos = {k: i for i, k in enumerate(order)}
        out.sort(key=lambda s: (pos.get(s["key"], len(order)), s["label"].lower()))
    return out


TRASH_INDEX = TRASH_DIR / "_index.json"
TRASH_LOCK = threading.Lock()


def _trash_index_remove(token):
    with TRASH_LOCK:
        idx = load_json(TRASH_INDEX, [])
        save_json(TRASH_INDEX, [r for r in idx if r.get("token") != token])


def trash_put(src_path, kind="", project="", sub="", entry=None):
    # mueve un archivo a la papelera y lo registra en el índice; devuelve token (o "" si no existía)
    if not src_path.is_file():
        return ""
    token = uuid.uuid4().hex + "__" + src_path.name
    try:
        src_path.rename(TRASH_DIR / token)
    except Exception:
        return ""
    try:
        ent = entry or {}
        name = ent.get("name") or (str(ent.get("prompt", "")).strip()[:80]) or src_path.name
        with TRASH_LOCK:
            idx = load_json(TRASH_INDEX, [])
            idx.insert(0, {"token": token, "kind": kind, "project": project, "sub": sub,
                           "entry": ent, "name": name, "ts": time.time()})
            save_json(TRASH_INDEX, idx)
    except Exception:
        pass
    return token


def trash_restore(token, dest_path):
    # devuelve un archivo de la papelera a su sitio (con contención de ruta como defensa)
    t = TRASH_DIR / os.path.basename(token or "")
    if not token or not t.is_file():
        _trash_index_remove(token)
        return False
    try:
        if TRASH_DIR.resolve() != t.resolve().parent:
            return False
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        t.rename(dest_path)
        _trash_index_remove(token)
        return True
    except Exception:
        return False


def trash_purge(days=14):
    # vacía de verdad lo que lleve más de N días (evita que la papelera crezca sin límite)
    cutoff = time.time() - days * 86400
    idx = load_json(TRASH_INDEX, [])
    by_token = {r.get("token"): r for r in idx}
    alive = set()
    try:
        for p in TRASH_DIR.iterdir():
            if p.name == "_index.json":
                continue
            try:
                rec = by_token.get(p.name)
                ts = rec.get("ts") if rec else p.stat().st_mtime
                if ts < cutoff:
                    p.unlink() if p.is_file() else shutil.rmtree(p, ignore_errors=True)
                else:
                    alive.add(p.name)
            except Exception:
                pass
    except Exception:
        pass
    with TRASH_LOCK:
        save_json(TRASH_INDEX, [r for r in idx if r.get("token") in alive])


def phist_dir(proj, sub=""):
    if sub:
        d = psub_base(proj, sub) / "historial"
    else:
        d = HIST_DIR if is_general(proj) else PROJ_DIR / safe(proj) / "historial"
    d.mkdir(parents=True, exist_ok=True)
    return d


def phist_json(proj, sub=""):
    if sub:
        return psub_base(proj, sub) / "historial.json"
    return HIST_JSON if is_general(proj) else PROJ_DIR / safe(proj) / "historial.json"


def pshelf_dir(proj, sub=""):
    if sub:
        d = psub_base(proj, sub) / "estante"
    else:
        d = SHELF_DIR if is_general(proj) else PROJ_DIR / safe(proj) / "estante"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pshelf_json(proj, sub=""):
    if sub:
        return psub_base(proj, sub) / "estante.json"
    return SHELF_JSON if is_general(proj) else PROJ_DIR / safe(proj) / "estante.json"


def add_history(item):
    with LOCK:
        jp = phist_json(item.get("project"), item.get("sub", ""))
        h = load_json(jp, [])
        h.insert(0, item)
        save_json(jp, h)  # sin tope: la galería recuerda todo (por proyecto/subproyecto)


def safe(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:60] or "proj"


def safe_fn(name):
    # nombre de archivo seguro para cabeceras multipart (sin comillas ni saltos de línea)
    return re.sub(r'[\r\n"\\]', "", str(name))[:120] or "archivo"


def png_meta(raw, pairs):
    """Incrusta el prompt/parámetros como chunks iTXt: la imagen lleva su receta consigo."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return raw
    try:
        ihdr_len = struct.unpack(">I", raw[8:12])[0]
        pos = 8 + 12 + ihdr_len
        extra = b""
        for k, v in pairs:
            if not v:
                continue
            data = k.encode()[:79] + b"\x00\x00\x00\x00\x00" + str(v).encode()
            extra += struct.pack(">I", len(data)) + b"iTXt" + data + struct.pack(">I", zlib.crc32(b"iTXt" + data) & 0xFFFFFFFF)
        return raw[:pos] + extra + raw[pos:]
    except Exception:
        return raw


_THUMB_OK = None   # ¿está disponible 'sips'? (macOS) — se detecta una vez


def thumb_for(src_path, px=640):
    """Devuelve la ruta a una miniatura en caché (~px de lado mayor, JPEG) o None.
    Las cuadrículas piden esto en vez de la imagen full-res (3+ MB) → carga muchísimo más ligera."""
    global _THUMB_OK
    try:
        if not src_path.is_file():
            return None
        if _THUMB_OK is False:
            return None
        THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        st = src_path.stat()
        key = hashlib.md5(f"{src_path.resolve()}|{int(st.st_mtime)}|{st.st_size}|{px}".encode()).hexdigest() + ".jpg"
        tp = THUMBS_DIR / key
        if tp.is_file() and tp.stat().st_size > 0:
            return tp
        r = subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(px), str(src_path), "--out", str(tp)],
                           capture_output=True, timeout=25)
        if tp.is_file() and tp.stat().st_size > 0:
            _THUMB_OK = True
            return tp
        if r.returncode != 0 and _THUMB_OK is None:
            _THUMB_OK = False   # sips no existe/falla en este sistema → no reintentar
    except Exception:
        pass
    return None


def load_projects():
    raw = load_json(PROJ_JSON, {})
    out = {}
    for k, v in raw.items():
        if is_general(k):
            continue  # el espacio General se inyecta aparte bajo la clave ""
        d = {"style": v, "refs": []} if isinstance(v, str) else {"style": v.get("style", ""), "refs": v.get("refs", [])}
        # estilo.md en la carpeta del proyecto manda: editable a mano y a prueba de JSON corrupto
        f = PROJ_DIR / safe(k) / "estilo.md"
        if f.exists():
            try:
                d["style"] = f.read_text()
            except Exception:
                pass
        d["style_video"] = v.get("style_video", "") if isinstance(v, dict) else ""
        fv = PROJ_DIR / safe(k) / "estilo-video.md"
        if fv.exists():
            try:
                d["style_video"] = fv.read_text()
            except Exception:
                pass
        out[k] = d
    # El espacio General ("") es un proyecto de primera clase: memoria visual, estilo y destilado
    gv = raw.get("general", {})
    if isinstance(gv, str):
        gv = {"style": gv}
    gd = {"style": gv.get("style", ""), "style_video": gv.get("style_video", ""), "refs": list(gv.get("refs", []))}
    gf = PROJ_DIR / "general"
    if (gf / "estilo.md").exists():
        try:
            gd["style"] = (gf / "estilo.md").read_text()
        except Exception:
            pass
    if (gf / "estilo-video.md").exists():
        try:
            gd["style_video"] = (gf / "estilo-video.md").read_text()
        except Exception:
            pass
    out[""] = gd
    return out


def proj_folder(name):
    # el espacio General usa una carpeta estable ("general"), tal como lo mapean is_general/proj_key
    d = PROJ_DIR / ("general" if is_general(name) else safe(name))
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_key(k):
    try:
        urllib.request.urlopen(urllib.request.Request(API_MODELS, headers={"Authorization": f"Bearer {k}"}), timeout=20).read()
        return True
    except Exception:
        return False


def proj_key(proj):
    return (proj or "").strip() or "general"


def save_dir(proj=None):
    # carpeta EXTERNA de copias del historial — por proyecto (con fallback al global legado)
    if proj is None:
        proj = ACTIVE_PROJ
    conf = load_json(CONF_JSON, {})
    raw = (conf.get("save_dirs") or {}).get(proj_key(proj), "") or conf.get("save_dir") or ""
    return Path(os.path.expanduser(raw)) if raw else HOME / "Desktop"


def shelf_dir(proj=None):
    # carpeta EXTERNA de las "Mis imágenes" del proyecto; por defecto, la interna del proyecto
    if proj is None:
        proj = ACTIVE_PROJ
    conf = load_json(CONF_JSON, {})
    raw = (conf.get("shelf_dirs") or {}).get(proj_key(proj), "")
    if not raw and is_general(proj):
        raw = conf.get("shelf_dir") or ""   # global legado (solo General)
    return Path(os.path.expanduser(raw)) if raw else pshelf_dir(proj)


def save_dir_sub(proj=None, sub=""):
    # carpeta externa de copias: en un subproyecto cuelga de save_dir(proj)/<subkey>/
    base = save_dir(proj)
    return (base / _sub_safe(sub)) if sub else base


def shelf_dir_sub(proj=None, sub=""):
    base = shelf_dir(proj)
    if not sub:
        return base
    # si NO hay carpeta externa configurada, base es la interna (estante raíz);
    # devolver la interna del sub para que NO se cree una copia huérfana al "espejar"
    if base.resolve() == pshelf_dir(proj).resolve():
        return pshelf_dir(proj, sub)
    return base / _sub_safe(sub)


def icloud_dir():
    return HOME / "Library" / "Mobile Documents" / "com~apple~CloudDocs"


def backup_status():
    real = ROOT.resolve()
    icl = icloud_dir()
    n, size = 0, 0
    for p in real.rglob("*"):
        if p.is_file():
            n += 1
            try:
                size += p.stat().st_size
            except Exception:
                pass
    human = f"{size/1024/1024:.1f} MB" if size < 1024**3 else f"{size/1024**3:.2f} GB"
    return {"icloud": ROOT.is_symlink() and str(real).startswith(str(icl)),
            "icloud_available": icl.exists(), "size": human, "files": n,
            "git": (real / ".git").exists(),
            "path": str(real).replace(str(HOME), "~")}


def _zip_name(s):
    # nombre de carpeta legible y seguro para usar dentro del zip
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (s or "").strip())
    s = s.rstrip(". ")[:80]
    return s or "Sin nombre"


def _backup_skip(p):
    return (not p.is_file()) or p.name.endswith((".tmp", ".bak")) or p.name == ".DS_Store"


def _backup_add_scope(z, base, proj, sub):
    # vuelca un ámbito (raíz del proyecto o un subproyecto) ya organizado:
    #   <base>/Historial/<imagenes> + _info.json   y   <base>/Mis imágenes/<imagenes> + _info.json
    hdir, hjson = phist_dir(proj, sub), phist_json(proj, sub)
    sdir, sjson = pshelf_dir(proj, sub), pshelf_json(proj, sub)
    hist = load_json(hjson, [])
    if hdir.is_dir():
        for p in sorted(hdir.iterdir()):
            if not _backup_skip(p):
                z.write(p, base + "/Historial/" + p.name)
        rdir = hdir / "_refs"          # imágenes que se usaron como referencia en cada prompt
        if rdir.is_dir():
            for p in sorted(rdir.iterdir()):
                if not _backup_skip(p):
                    z.write(p, base + "/Historial/_refs/" + p.name)
    if hist:
        z.writestr(base + "/Historial/_info.json", json.dumps(hist, ensure_ascii=False, indent=1))
    shelf = load_json(sjson, [])
    if sdir.is_dir():
        for p in sorted(sdir.iterdir()):
            if not _backup_skip(p):
                z.write(p, base + "/Mis imágenes/" + p.name)
    if shelf:
        z.writestr(base + "/Mis imágenes/_info.json", json.dumps(shelf, ensure_ascii=False, indent=1))
    return len(hist), len(shelf)


def build_backup_zip():
    """Backup ORGANIZADO: cada proyecto es una carpeta con nombre legible
    (Historial / Mis imágenes / Subproyectos), sin papelera ni archivos .bak/.tmp."""
    raw = load_json(PROJ_JSON, {})
    conf = load_json(CONF_JSON, {})
    gl = conf.get("general_label") or "General"
    projects = [("", gl)]                       # ("" = espacio General)
    seen = {"general"}
    for k in raw.keys():
        if is_general(k):
            continue
        projects.append((k, k))                 # la clave del JSON ya es el nombre legible
        seen.add(safe(k))
    if PROJ_DIR.is_dir():                        # carpetas en disco no registradas en el JSON
        for d in sorted(PROJ_DIR.iterdir()):
            if d.is_dir() and d.name not in seen:
                projects.append((d.name, d.name))
                seen.add(d.name)
    manifest = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        used = {}
        for key, label in projects:
            L = _zip_name(label)
            used[L] = used.get(L, 0) + 1
            if used[L] > 1:
                L = f"{L} ({used[L]})"
            base = "Proyectos/" + L
            nh, ns = _backup_add_scope(z, base, key, "")
            subinfo = []
            for s in list_subs(key):
                snh, sns = _backup_add_scope(z, base + "/Subproyectos/" + _zip_name(s["label"]), key, s["key"])
                subinfo.append((s["label"], snh, sns))
            # otros archivos del proyecto (estilo, memoria visual, portada…) → _otros/
            pf = proj_folder(key)
            if pf.is_dir():
                for p in pf.rglob("*"):
                    if _backup_skip(p):
                        continue
                    rel = p.relative_to(pf)
                    if rel.parts and rel.parts[0] in ("historial", "estante", "sub"):
                        continue   # ya incluidos, organizados, arriba
                    if p.name in ("historial.json", "estante.json"):
                        continue   # ya van como _info.json dentro de Historial / Mis imágenes
                    z.write(p, base + "/_otros/" + str(rel))
            manifest.append((L, nh, ns, subinfo))
        if PROMPTS_JSON.exists():
            z.write(PROMPTS_JSON, "Biblioteca de prompts.json")
        if CONF_JSON.exists():
            z.write(CONF_JSON, "_sistema/config.json")
        if PROJ_JSON.exists():
            z.write(PROJ_JSON, "_sistema/proyectos.json")
        z.writestr("LÉEME.txt", _backup_readme(manifest))
    return buf.getvalue()


def build_clone_zip():
    """Copia EXACTA del directorio de datos (~/image-studio) para reimportar TAL CUAL.
    Incluye todo: imágenes, historial.json (prompts), estante, _refs (referencias),
    proyectos/subproyectos, estilos, memoria visual, config y biblioteca. Omite
    papelera, caché de miniaturas y temporales."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ROOT.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            if rel.parts and rel.parts[0] in (".papelera", ".thumbs", ".import"):
                continue
            if p.name.endswith((".tmp", ".bak")) or p.name == ".DS_Store":
                continue
            z.write(p, "image-studio/" + str(rel.as_posix()))
        z.writestr("CLON.txt",
                   "Copia EXACTA de Gio Studio.\n"
                   "Para restaurar: en la app, panel Backup → «Importar copia exacta» y elige este archivo.\n"
                   "O descomprime la carpeta image-studio/ dentro de ~/image-studio.\n")
    return buf.getvalue()


def _backup_readme(manifest):
    L = [
        "GIO STUDIO — COPIA DE SEGURIDAD",
        "Generada: " + time.strftime("%Y-%m-%d %H:%M"),
        "",
        "CÓMO ESTÁ ORGANIZADO",
        "  Proyectos/<Nombre del proyecto>/",
        "     Historial/        → las imágenes generadas (+ _info.json con prompts y datos)",
        "     Mis imágenes/      → tu estante de imágenes propias (+ _info.json)",
        "     Subproyectos/<Nombre>/  → cada subproyecto, con su Historial y Mis imágenes",
        "     _otros/            → estilo, memoria visual, portada, etc.",
        "  Biblioteca de prompts.json  → tu biblioteca de prompts",
        "  _sistema/            → config.json y proyectos.json (para restaurar)",
        "",
        "No se incluyen la papelera ni archivos temporales (.bak/.tmp).",
        "",
        "PROYECTOS EN ESTA COPIA",
    ]
    for name, nh, ns, subs in manifest:
        L.append(f"  • {name}  —  {nh} en Historial, {ns} en Mis imágenes")
        for sname, snh, sns in subs:
            L.append(f"       └ {sname}: {snh} en Historial, {sns} en Mis imágenes")
    return "\n".join(L) + "\n"



try:
    I18N_JSON = (Path(__file__).resolve().parent / "i18n.json").read_text(encoding="utf-8")
    # inyección segura dentro de <script>: < solo aparece dentro de strings, < es válido
    # en JSON y JS; U+2028/U+2029 rompen literales de cadena en JS
    I18N_JSON = (I18N_JSON.replace("<", "\\u003c")
                 .replace(" ", "\\u2028").replace(" ", "\\u2029"))
except Exception:
    I18N_JSON = "{}"

HTML = r"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Gio Studio</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%23e0a571'/%3E%3Cpath d='M12 5l1.6 4.7 4.7 1.2-3.8 2.7L15.8 18 12 15.3 8.2 18l1.3-4.4-3.8-2.7 4.7-1.2z' fill='%231a1206'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
 --bg:#0f0d0c;--surface:#1a1715;--surface2:#231f1c;--elev:#2c2723;
 --line:rgba(255,255,255,.06);--line2:rgba(255,255,255,.12);
 --txt:#f1ece7;--mut:#a89f97;--faint:#6f665f;
 --accent:#e0a571;--accent-dim:rgba(224,165,113,.14);--ok:#7bbf8f;--bad:#e07a6b;
 --glow:rgba(224,165,113,.06);--on-accent:#1f160d;
 --btn-bg:var(--txt);--btn-fg:#12100d;
 --ui:'Schibsted Grotesk',-apple-system,sans-serif;--mono:'Geist Mono',ui-monospace,monospace;
 --z-sticky:5;--z-modal:30;--z-lightbox:40;--z-toast:60;
}
/* ===== temas — 3 oscuros (Carbón = raíz) + 3 claros, via body[data-theme] ===== */
body[data-theme="medianoche"]{--bg:#070d18;--surface:#0d1626;--surface2:#161f31;--elev:#1b2840;
 --line:rgba(255,255,255,.06);--line2:rgba(255,255,255,.12);--txt:#eaf1f8;--mut:#9fb1c6;--faint:#647a96;
 --accent:#22d3ee;--accent-dim:rgba(34,211,238,.14);--ok:#34d399;--bad:#f87171;--glow:rgba(34,211,238,.06);--on-accent:#04141b}
body[data-theme="neon"]{--bg:#0c0716;--surface:#15102a;--surface2:#1e1838;--elev:#272047;
 --line:rgba(255,255,255,.06);--line2:rgba(255,255,255,.12);--txt:#f2eefb;--mut:#a99fc4;--faint:#6f6690;
 --accent:#ff3ea5;--accent-dim:rgba(255,62,165,.14);--ok:#3ee6a8;--bad:#ff5c72;--glow:rgba(255,62,165,.06);--on-accent:#ffffff}
body[data-theme="dia"]{--bg:#faf7f2;--surface:#fdfbf7;--surface2:#ffffff;--elev:#ffffff;
 --line:rgba(0,0,0,.07);--line2:rgba(0,0,0,.15);--txt:#211e1b;--mut:#6b655d;--faint:#9c958b;
 --accent:#b8492a;--accent-dim:rgba(184,73,42,.13);--ok:#3f7d52;--bad:#c0392b;--glow:rgba(184,73,42,.06);--btn-bg:#2a241f;--btn-fg:#faf7f2;--on-accent:#faf7f2}
body[data-theme="bruma"]{--bg:#eef1f6;--surface:#f8fafc;--surface2:#ffffff;--elev:#ffffff;
 --line:rgba(0,0,0,.07);--line2:rgba(0,0,0,.15);--txt:#1d2230;--mut:#5a6478;--faint:#9aa2b4;
 --accent:#4654c7;--accent-dim:rgba(70,84,199,.14);--ok:#1f8a5b;--bad:#c8324b;--glow:rgba(70,84,199,.06);--btn-bg:#1d2230;--btn-fg:#f8fafc;--on-accent:#f8fafc}
body[data-theme="crema"]{--bg:#f4efe3;--surface:#faf6ec;--surface2:#fffdf6;--elev:#ffffff;
 --line:rgba(0,0,0,.07);--line2:rgba(0,0,0,.15);--txt:#22201b;--mut:#6b665a;--faint:#9c9788;
 --accent:#1f6b54;--accent-dim:rgba(31,107,84,.14);--ok:#2f7a4a;--bad:#b4452f;--glow:rgba(31,107,84,.06);--btn-bg:#23211b;--btn-fg:#faf6ec;--on-accent:#fffdf6}
*{box-sizing:border-box}
::selection{background:var(--accent-dim)}
body{margin:0;font-family:var(--ui);background:var(--bg);color:var(--txt);font-size:14px;line-height:1.45;
 -webkit-font-smoothing:antialiased;
 background-image:radial-gradient(1200px 600px at 80% -10%,var(--glow),transparent 60%);transition:background-color .25s,color .25s;}
svg{width:16px;height:16px;stroke:currentColor;stroke-width:1.6;fill:none;stroke-linecap:round;stroke-linejoin:round;flex:none}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.eyebrow{font-size:10px;letter-spacing:.13em;text-transform:uppercase;color:var(--faint);font-weight:600;display:flex;align-items:center;gap:7px}
button{font-family:var(--ui)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
textarea:focus-visible,select:focus-visible,input[type=text]:focus-visible,input[type=password]:focus-visible{outline:none}
kbd{font-family:var(--mono);font-size:10px;color:var(--mut);background:var(--surface2);border:1px solid var(--line2);
 border-bottom-width:2px;border-radius:4px;padding:1px 5px}

/* top bar */
.top{display:flex;align-items:center;gap:18px;padding:15px 26px;border-bottom:1px solid var(--line);
 flex-wrap:wrap;row-gap:10px;
 position:sticky;top:0;z-index:var(--z-sticky);background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(14px)}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;letter-spacing:.02em;flex:none}
/* en el flujo (no absoluto): se centra entre la marca y la derecha y baja como bloque al achicar, sin pisarse */
.projbar{display:flex;align-items:center;gap:8px;margin-left:auto;flex:0 1 auto;min-width:0;flex-wrap:wrap;row-gap:6px;z-index:1}
.top .seg{flex:none}
.projbtn{display:flex;align-items:center;gap:9px;background:var(--surface2);border:1px solid var(--line);color:var(--txt);
 border-radius:11px;padding:8px 14px;font-size:13.5px;font-weight:500;cursor:pointer;transition:.16s;font-family:var(--ui);max-width:300px}
.projbtn:hover{border-color:var(--accent);background:var(--elev)}
.projbtn svg{width:15px;height:15px;stroke:var(--mut);fill:none;stroke-width:1.7;flex:none}
.projbtn .chev{width:13px;height:13px;margin-left:2px}
.projbtn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.subsel{background:var(--surface2);border:1px solid var(--line);color:var(--txt);border-radius:11px;padding:8px 12px;font-size:13px;font-family:var(--ui);cursor:pointer;max-width:220px}
.subsel:hover{border-color:var(--accent)}
.subchips{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 8px}
.subchip{font-family:var(--mono);font-size:11px;background:var(--surface);border:1px solid var(--line);color:var(--mut);border-radius:999px;padding:3px 10px;cursor:pointer;transition:.14s}
.subchip:hover{border-color:var(--line2);color:var(--txt)}
.subchip.on{background:var(--accent-dim);border-color:var(--accent);color:var(--accent)}
.subchip.subdrag{cursor:grab}
.subchip.chipdrag{opacity:.4}
.subchip.chipdropt{outline:2px solid var(--accent);outline-offset:1px}
.histgroup{margin:0 0 10px}
.histgrouphdr{font-family:var(--mono);font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--mut);background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:5px 10px;margin:0 0 8px}
.shelfsec.secdrop{outline:2px dashed var(--accent);outline-offset:3px;border-radius:10px;background:var(--accent-dim)}
.shelfsec.secdrop .histgrouphdr{border-color:var(--accent);color:var(--accent)}
.shelfsec .histgrouphdr{display:flex;align-items:center;gap:10px}
.shelfsec .histgrouphdr .ghname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.secacts{display:flex;align-items:center;gap:7px;flex:none}
.secdots{display:flex;gap:4px}
.secdot{width:13px;height:13px;border-radius:50%;border:0;cursor:pointer;padding:0;opacity:.8;transition:.12s}
.secdot:hover{opacity:1;transform:scale(1.15)}
.secdot.r{background:#e5484d}.secdot.y{background:#f5b400}.secdot.g{background:#46a758}.secdot.b{background:#3b82f6}
.secbtn{display:flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:6px;background:var(--surface);border:1px solid var(--line2);color:var(--mut);cursor:pointer;padding:0}
.secbtn:hover{color:var(--txt);border-color:var(--mut)}
.secbtn svg{width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:2}
.movepop{position:fixed;z-index:1300;background:var(--elev);border:1px solid var(--line2);border-radius:12px;padding:8px;box-shadow:0 18px 50px rgba(0,0,0,.5);max-height:60vh;overflow-y:auto;min-width:220px}
.movepop .mphdr{font-size:11px;color:var(--mut);padding:4px 8px 6px;text-transform:uppercase;letter-spacing:.04em}
.movepop .mpdest{display:flex;gap:3px;padding:3px;margin:0 4px 6px;background:var(--surface2);border-radius:9px}
.movepop .mpdest button{flex:1;text-align:center;font:inherit;font-size:12px;color:var(--mut);border:0;background:none;border-radius:6px;padding:5px;cursor:pointer}
.movepop .mpdest button.on{background:var(--accent);color:#fff}
.movepop button.mpopt{display:block;width:100%;text-align:left;background:transparent;border:0;color:var(--txt);font-size:13px;font-family:var(--ui);padding:7px 10px;border-radius:8px;cursor:pointer}
.movepop button.mpopt:hover{background:var(--accent-dim);color:var(--accent)}
/* modal de proyectos */
.modal.projmodal{max-width:min(1180px,96vw);width:96%}
.modal.trashmodal{max-width:min(960px,96vw);width:96%;max-height:88vh;overflow:auto}
.trashbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 14px}
.trashgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.tcard{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--surface2);display:flex;flex-direction:column}
.tcard img{width:100%;aspect-ratio:1;object-fit:cover;display:block;background:var(--elev)}
.tcard .tnm{font-size:11.5px;padding:6px 8px 0;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tcard .tpl{font-size:10.5px;color:var(--mut);padding:1px 8px 6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tcard .tacts{display:flex;gap:6px;padding:0 8px 8px;margin-top:auto;align-items:center}
.tcard .trest{flex:1;font:inherit;font-size:12px;border:1px solid var(--line2);background:var(--surface);color:var(--accent);border-radius:8px;padding:6px;cursor:pointer}
.tcard .trest:hover{border-color:var(--accent);background:var(--accent-dim)}
.tcard .tdelp{flex:none;width:30px;height:30px;border:1px solid var(--line2);background:var(--surface);color:var(--mut);border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center}
.tcard .tdelp:hover,.tcard .tdelp.arm{color:#fff;background:var(--bad);border-color:var(--bad)}
.tcard .tdelp svg{width:14px;height:14px}
.modsub{color:var(--mut);font-size:13px;margin:0 0 18px;line-height:1.5}
.projgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:14px;max-height:60vh;overflow-y:auto;padding:2px}
.projitem{display:flex;flex-direction:column;gap:8px}
.projitem.active .pname{color:var(--accent)}
.projitem.active .projcard{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.projcard{aspect-ratio:4/3;border-radius:14px;overflow:hidden;border:1px solid var(--line);background:var(--surface2);
 cursor:pointer;transition:transform .16s,box-shadow .16s,border-color .16s}
.projcard:hover{transform:translateY(-3px);box-shadow:0 12px 28px rgba(0,0,0,.18);border-color:var(--mut)}
.projcard .cov{width:100%;height:100%;background-size:cover;background-position:center;background-color:var(--elev)}
.projcard .ph{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:42px;font-weight:700;
 color:color-mix(in srgb,var(--accent) 70%,var(--mut));background:linear-gradient(145deg,var(--elev),var(--surface))}
.projfoot{display:flex;align-items:center;gap:8px;padding:0 3px}
.projfoot .mtext{flex:1;min-width:0}
.projfoot .pname{font-size:13.5px;font-weight:600;color:var(--txt);line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.projfoot .pcount{font-size:11.5px;color:var(--mut);margin-top:1px}
.projfoot .pedit,.projfoot .pdel{flex:none;width:28px;height:28px;border-radius:8px;border:1px solid var(--line2);background:transparent;color:var(--mut);
 display:flex;align-items:center;justify-content:center;cursor:pointer;opacity:0;transition:.15s}
.projitem:hover .pedit,.projitem:hover .pdel{opacity:1}
.projfoot .pedit:hover{color:var(--accent);border-color:var(--accent)}
.projfoot .pdel:hover{color:var(--bad);border-color:var(--bad)}
.projfoot .pdel.arm{color:#fff;background:var(--bad);border-color:var(--bad);opacity:1}
.subrow{display:flex;flex-wrap:wrap;gap:5px;align-items:center;padding:2px 3px 0;margin-top:1px}
.subchipp{display:inline-flex;align-items:center;gap:5px;background:var(--surface2);border:1px solid var(--line);border-radius:20px;padding:2px 4px 2px 10px;font-size:11.5px;color:var(--txt);max-width:100%;cursor:pointer}
.subchipp .scn{cursor:pointer}
.subchipp:hover{border-color:var(--accent)}
.subchipp.pdrag{opacity:.4}
.subchipp.subdropt{outline:2px solid var(--accent);outline-offset:1px}
.subchipp .scn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:120px}
.subchipp .scc{font-size:10px;color:var(--mut);background:var(--surface);border-radius:10px;padding:0 6px;font-variant-numeric:tabular-nums}
.subchipp .subren,.subchipp .subx,.subchipp .subout{flex:none;width:20px;height:20px;border:0;background:none;color:var(--faint);border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;opacity:0;transition:.14s}
.subchipp:hover .subren,.subchipp:hover .subx,.subchipp:hover .subout{opacity:1}
.subchipp .subren svg,.subchipp .subx svg,.subchipp .subout svg{width:12px;height:12px}
.subchipp .subren:hover,.subchipp .subout:hover{color:var(--accent)}
.subchipp .subx:hover{color:var(--bad)}
.subchipp .subx.arm{color:#fff;background:var(--bad);opacity:1}
.subadd{flex:none;font-size:11.5px;color:var(--accent);background:none;border:1px dashed var(--line2);border-radius:20px;padding:3px 10px;cursor:pointer;font-family:inherit}
.subadd:hover{border-color:var(--accent);background:var(--accent-dim)}
.subconv{flex:none;font-size:11px;color:var(--mut);background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:3px 6px;cursor:pointer;font-family:inherit;max-width:150px}
.projitem[draggable="true"]{cursor:grab}
.projitem.pdrag{opacity:.45}
.projitem{position:relative}
.projitem.dropt .projcard{outline:2px dashed var(--accent);outline-offset:3px}
.projitem.dropt::after{content:"➜ subproyecto de aquí";position:absolute;top:0;left:0;right:0;text-align:center;font-size:11px;font-weight:600;color:#fff;background:var(--accent);border-radius:14px 14px 0 0;padding:4px;pointer-events:none;z-index:2}
.projfoot .pedit svg,.projfoot .pdel svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:1.9}
.prename{width:100%;font-size:13px;padding:5px 8px;margin:0}
.projnewrow{display:flex;align-items:center;gap:9px;margin-top:18px;padding-top:16px;border-top:1px solid var(--line)}
.projnewrow>svg{width:17px;height:17px;stroke:var(--mut);fill:none;stroke-width:1.7;flex:none}
.projnewrow input{flex:1;margin:0}
.projnewrow .primary{flex:none;width:auto;padding:10px 18px;font-size:13.5px;border-radius:10px;white-space:nowrap}
.projnewrow .primary svg{width:15px;height:15px}
.brand .dot{width:22px;height:22px;border-radius:7px;background:linear-gradient(140deg,var(--accent),color-mix(in srgb,var(--accent) 65%,#000));
 display:flex;align-items:center;justify-content:center;color:#1a1206}
.brand .dot svg{width:14px;height:14px;fill:var(--on-accent);stroke:var(--on-accent);stroke-width:.6;stroke-linejoin:round}
.seg{display:flex;background:var(--surface2);border:1px solid var(--line);border-radius:11px;padding:3px;gap:2px}
.seg button{display:flex;align-items:center;gap:7px;background:transparent;border:0;color:var(--mut);
 padding:7px 15px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;transition:.18s}
.seg button:hover{color:var(--txt)}
.seg button.on{background:var(--elev);color:var(--txt);box-shadow:0 1px 0 rgba(255,255,255,.04) inset}
.seg button kbd{margin-left:2px}
.top .right{margin-left:auto;display:flex;align-items:center;gap:14px;flex:0 1 auto;min-width:0;flex-wrap:wrap;row-gap:6px;justify-content:flex-end}
.sess{font-size:12px;color:var(--mut)}.sess b{color:var(--txt);font-weight:500}
.sessfoot{margin-top:20px;padding-top:14px;border-top:1px solid var(--line);display:flex;gap:6px;align-items:center;justify-content:center}
.ghost{display:flex;align-items:center;gap:7px;background:transparent;border:1px solid var(--line2);color:var(--mut);
 border-radius:9px;padding:7px 12px;font-size:12px;cursor:pointer;transition:.18s}
.ghost:hover{color:var(--txt);border-color:var(--mut)}
.kdot{width:7px;height:7px;border-radius:50%;background:var(--faint);transition:.2s}
.kdot.on{background:var(--ok);box-shadow:0 0 6px rgba(123,217,154,.5)}

.wrap{display:grid;grid-template-columns:362px 1fr 328px;gap:1px;background:var(--line);
 height:calc(100vh - 59px)}
.wrap>*{background:var(--bg)}
@media(max-width:1180px){.wrap{grid-template-columns:1fr;height:auto}.wrap .col{overflow:visible}}
.col{padding:22px;overflow:auto;min-height:0}
.col.mid{padding:22px;display:flex;flex-direction:column}
.an{opacity:0;transform:translateY(8px);animation:rise .6s cubic-bezier(.2,.7,.2,1) forwards}
.col:nth-child(1){animation-delay:.02s}.col:nth-child(2){animation-delay:.09s}.col:nth-child(3){animation-delay:.16s}
@keyframes rise{to{opacity:1;transform:none}}

.field{margin-bottom:18px}
label{display:block;font-size:10px;letter-spacing:.11em;text-transform:uppercase;color:var(--faint);font-weight:600;margin-bottom:8px}
textarea,select,input[type=text],input[type=password]{width:100%;background:var(--surface);border:1px solid var(--line);
 border-radius:10px;color:var(--txt);padding:11px 12px;font-size:14px;font-family:var(--ui);transition:.16s}
textarea:focus,select:focus,input:focus{outline:none;border-color:var(--line2);background:var(--surface2)}
textarea{resize:vertical;min-height:78px}select{appearance:none;cursor:pointer;
 background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238c8c93' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
 background-repeat:no-repeat;background-position:right 11px center;padding-right:30px}

.slabel{display:flex;justify-content:space-between;align-items:center}
.vnum{width:78px;background:transparent;border:1px solid transparent;color:var(--txt);
 font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px;text-align:right;
 padding:3px 7px;border-radius:7px;transition:.15s;-moz-appearance:textfield;appearance:textfield}
.vnum::-webkit-inner-spin-button,.vnum::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.vnum:hover{border-color:var(--line2)}
.vnum:focus{outline:none;border-color:var(--accent);background:var(--surface2)}
input[type=range]{-webkit-appearance:none;width:100%;height:22px;background:transparent;cursor:pointer;margin-top:2px}
input[type=range]::-webkit-slider-runnable-track{height:3px;border-radius:3px;background:var(--line2)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:15px;height:15px;border-radius:50%;background:var(--txt);
 margin-top:-6px;border:3px solid var(--bg);box-shadow:0 0 0 1px var(--line2);transition:.15s}
input[type=range]::-webkit-slider-thumb:hover{background:var(--accent)}
.check{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--mut);cursor:pointer;user-select:none}
.check input{accent-color:var(--accent);width:14px;height:14px}
.lockbtn{display:flex;align-items:center;gap:8px;margin-top:10px;width:100%;background:var(--surface);border:1px solid var(--line);
 color:var(--mut);border-radius:9px;padding:8px 11px;font-size:12px;cursor:pointer;transition:.16s;font-family:var(--ui)}
.lockbtn:hover{color:var(--txt);border-color:var(--line2)}
.lockbtn svg{width:15px;height:15px;stroke:currentColor;flex:none}
.lockbtn .lk-closed{display:none}
.lockbtn.on{color:var(--accent);border-color:var(--accent);background:var(--accent-dim)}
.lockbtn.on .lk-open{display:none}.lockbtn.on .lk-closed{display:block}

.presets{display:flex;flex-wrap:wrap;gap:6px}
.pgroup{font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);width:100%;margin:8px 0 2px}
.pgroup:first-child{margin-top:0}
.chip{font-family:var(--mono);font-size:11px;background:var(--surface);border:1px solid var(--line);color:var(--mut);
 border-radius:7px;padding:5px 9px;cursor:pointer;transition:.15s;display:inline-flex;align-items:center;gap:7px}
.chip:hover{border-color:var(--line2);color:var(--txt)}
.chip.on{background:var(--accent-dim);border-color:var(--accent);color:var(--accent)}
/* validez para gpt-image-2: verde sutil = válido (lados ÷16); verde lleno = nativo; rojo = inválido */
.chip.gok{border-color:rgba(123,217,154,.42)}
.chip.gok:hover{border-color:rgba(123,217,154,.75)}
.chip.gnat{border-color:var(--ok);color:var(--ok);background:rgba(123,217,154,.12)}
.chip.gbad{border-color:#d9776b;color:#d9776b}
.chip.on{background:var(--accent-dim);border-color:var(--accent);color:var(--accent)}
.preslegend{width:100%;font-size:10px;color:var(--faint);display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:6px;line-height:1.5}
.preslegend .dotok,.preslegend .dotnat{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}
.preslegend .dotok{border:1px solid rgba(123,217,154,.7)}
.preslegend .dotnat{background:var(--ok)}

.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.drop{display:flex;align-items:center;justify-content:center;gap:8px;border:1px dashed var(--line2);border-radius:10px;
 padding:14px;color:var(--mut);font-size:12.5px;cursor:pointer;background:var(--surface);transition:.16s;text-align:center}
.drop:hover,.drop.hot{border-color:var(--accent);color:var(--txt);background:var(--surface2)}
.thumbs{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}
.thumb{position:relative;width:60px;height:60px;border-radius:9px;overflow:hidden;border:1px solid var(--line2);cursor:grab}
.thumb:active{cursor:grabbing}
.thumb.dragging{opacity:.4;transform:scale(.92);box-shadow:0 6px 16px rgba(0,0,0,.28);z-index:2}
.thumb img{width:100%;height:100%;object-fit:cover;cursor:zoom-in}
.thumb .x{position:absolute;top:2px;right:2px;width:17px;height:17px;border:0;border-radius:5px;background:rgba(0,0,0,.6);
 color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center}
.thumb .x svg{width:10px;height:10px;stroke-width:2.4}

details.adv{border:1px solid var(--line);border-radius:11px;background:var(--surface);overflow:hidden;margin-bottom:18px}
details.adv>summary{list-style:none;cursor:pointer;padding:12px 14px;display:flex;align-items:center;gap:9px;
 font-size:12px;color:var(--mut);font-weight:500}
details.adv>summary::-webkit-details-marker{display:none}
details.adv>summary .chev{margin-left:auto;transition:.2s}details.adv[open]>summary .chev{transform:rotate(180deg)}
details.adv[open]>summary{border-bottom:1px solid var(--line)}
.advbody{padding:14px}

.meta{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--mut);margin-bottom:12px}
.valid.ok{color:var(--ok)}.valid.bad{color:var(--bad)}
.estbar{display:flex;justify-content:space-between;align-items:center;padding:13px 15px;background:var(--surface);
 border:1px solid var(--line);border-radius:11px;margin-bottom:12px;font-size:12px;color:var(--mut)}
.estbar .num{font-family:var(--mono);color:var(--accent);font-size:15px}
.primary{width:100%;display:flex;align-items:center;justify-content:center;gap:9px;background:var(--btn-bg);color:var(--btn-fg);
 border:0;border-radius:11px;padding:14px;font-size:14px;font-weight:600;cursor:pointer;transition:.16s}
.primary:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(0,0,0,.4)}
.primary:disabled{opacity:.35;cursor:not-allowed;transform:none;box-shadow:none}
#go.busy{background:var(--surface2);color:var(--accent);cursor:progress;box-shadow:none;transform:none}
#go.busy svg{display:none}
#go.busy::before{content:"";width:15px;height:15px;border:2px solid var(--line2);border-top-color:var(--accent);border-radius:50%;animation:sp .8s linear infinite}
/* mientras genera: un haz de luz recorre el borde del recuadro (la imagen queda intacta) */
@property --beamang{syntax:"<angle>";initial-value:0deg;inherits:false}
.canvas.gen{border-color:transparent;box-shadow:0 0 24px -6px var(--accent)}
.canvas.gen::after{content:"";position:absolute;inset:0;border-radius:16px;padding:2.5px;pointer-events:none;z-index:6;
 background:conic-gradient(from var(--beamang),transparent 0deg,transparent 235deg,color-mix(in srgb,var(--accent) 55%,transparent) 300deg,#fff 345deg,color-mix(in srgb,var(--accent) 55%,transparent) 360deg);
 -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;
 mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);mask-composite:exclude;
 animation:beamrot 1.5s linear infinite}
@keyframes beamrot{to{--beamang:360deg}}
@keyframes beampulse{0%,100%{opacity:.35}50%{opacity:1}}
/* fallback si el navegador no soporta @property: un borde de acento que pulsa */
@supports not (background:conic-gradient(from var(--beamang),#000,#000)){
 .canvas.gen::after{background:none;border:2px solid var(--accent);animation:beampulse 1.1s ease-in-out infinite}}
@media (prefers-reduced-motion:reduce){.canvas.gen::after{animation:beampulse 1.6s ease-in-out infinite}}
.hint{font-size:11px;color:var(--faint);margin-top:10px;line-height:1.55}
.hint.warn{color:#e0b070;border-left:2px solid #e0b070;padding-left:9px}

/* center */
.canvas{aspect-ratio:4/3;width:100%;max-height:74vh;margin:0 auto;display:flex;align-items:center;justify-content:center;border:1px solid var(--line);border-radius:16px;
 overflow:hidden;background:var(--surface);position:relative;
 background-image:linear-gradient(45deg,rgba(255,255,255,.012) 25%,transparent 25%,transparent 75%,rgba(255,255,255,.012) 75%),linear-gradient(45deg,rgba(255,255,255,.012) 25%,transparent 25%,transparent 75%,rgba(255,255,255,.012) 75%);
 background-size:24px 24px;background-position:0 0,12px 12px}
.genchip{position:fixed;top:80px;left:50%;transform:translateX(-50%);z-index:1250;display:flex;align-items:center;gap:8px;
 background:color-mix(in srgb,var(--surface) 88%,transparent);backdrop-filter:blur(10px);border:1px solid var(--line2);border-radius:20px;
 padding:8px 16px;font-size:12.5px;font-weight:500;color:var(--txt);box-shadow:0 8px 28px rgba(0,0,0,.28)}
.genchip:not(.hide){animation:genchipin .25s ease}
@keyframes genchipin{from{opacity:0;transform:translate(-50%,-8px)}to{opacity:1;transform:translate(-50%,0)}}
.genchip .gcdot{width:8px;height:8px;border-radius:50%;background:var(--accent);animation:gcpulse 1s ease-in-out infinite}
@keyframes gcpulse{0%,100%{opacity:.35;transform:scale(.8)}50%{opacity:1;transform:scale(1.15)}}
.dzhi{outline:2px dashed var(--accent)!important;outline-offset:3px;border-radius:10px;background:var(--accent-dim)!important;transition:.12s}
.canvas img.result{max-width:100%;max-height:100%;display:block;cursor:zoom-in;border-radius:3px}
.floaters{position:absolute;top:12px;right:12px;display:flex;gap:7px;opacity:0;transform:translateY(-4px);transition:.18s;z-index:2}
.canvas:hover .floaters,.canvas:focus-within .floaters{opacity:1;transform:none}
.fbtn{width:34px;height:34px;border-radius:9px;background:color-mix(in srgb,var(--elev) 88%,transparent);backdrop-filter:blur(8px);border:1px solid var(--line2);
 color:var(--txt);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:.15s;text-decoration:none}
.fbtn:hover{background:var(--elev);border-color:var(--mut)}.fbtn svg{width:16px;height:16px}
.lightbox{position:fixed;inset:0;background:rgba(5,5,6,.93);backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;z-index:var(--z-lightbox);cursor:zoom-out;padding:30px 30px 90px}
.lightbox img{max-width:94vw;max-height:86vh;border-radius:8px;box-shadow:0 30px 90px rgba(0,0,0,.7)}
#lbImg:fullscreen{width:100vw;height:100vh;max-width:100vw;max-height:100vh;object-fit:contain;border-radius:0;box-shadow:none;background:#000}
#lbImg:-webkit-full-screen{width:100vw;height:100vh;max-width:100vw;max-height:100vh;object-fit:contain;border-radius:0;box-shadow:none;background:#000}
.lightbox .mclose{position:fixed;top:18px;right:18px;width:36px;height:36px;background:rgba(16,16,18,.85);backdrop-filter:blur(8px)}
.lbnav{position:fixed;top:50%;transform:translateY(-50%);width:44px;height:44px;display:flex;align-items:center;justify-content:center;border-radius:50%;background:rgba(16,16,18,.7);border:1px solid rgba(255,255,255,.14);color:#fff;cursor:pointer;backdrop-filter:blur(8px);transition:.15s;z-index:1}
.lbnav:hover{background:rgba(40,40,44,.92);border-color:rgba(255,255,255,.3)}
.lbnav svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2.2}
.lbnav.prev{left:20px}.lbnav.next{right:20px}
.lbnav.off{opacity:.25;pointer-events:none}
/* selector de fotogramas de video */
.vfmodal{width:min(680px,94vw)}
.vfstage{background:#000;border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;align-items:center;justify-content:center;max-height:46vh}
.vfstage video{width:100%;max-height:46vh;display:block;object-fit:contain;background:#000}
.vfseek{width:100%;margin:12px 0 6px;accent-color:var(--accent);cursor:pointer}
.vfctrls{display:flex;align-items:center;gap:8px}
.vfctrls .sm{padding:6px 10px;font-size:11px}
.vfctrls .sm svg{width:14px;height:14px}
.vfshots{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;max-height:150px;overflow-y:auto}
.vfshots:empty::after{content:'Aún no has capturado fotogramas';color:var(--faint);font-size:11.5px}
.vfshot{position:relative;width:90px;height:60px;border-radius:7px;overflow:hidden;border:1px solid var(--line2);flex:none}
.vfshot img{width:100%;height:100%;object-fit:cover;display:block}
.vfshot .x{position:absolute;top:3px;right:3px;width:18px;height:18px;border-radius:50%;background:rgba(10,10,12,.82);border:0;display:flex;align-items:center;justify-content:center;cursor:pointer;color:#fff}
.vfshot .x svg{width:10px;height:10px;stroke-width:2.6}
.vfshot .tt{position:absolute;left:0;bottom:0;right:0;font-family:var(--mono);font-size:9px;color:#fff;background:rgba(10,10,12,.7);padding:1px 4px}
.vfactions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.vfactions button{justify-content:center}
.vfactions .vfadd{flex:1;min-width:140px}
.vfactions .vfadd[disabled]{opacity:.45;pointer-events:none}
.lbbar{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);display:flex;flex-direction:column;gap:10px;
 background:rgba(16,16,18,.92);backdrop-filter:blur(10px);border:1px solid var(--line2);border-radius:12px;
 padding:12px 14px;max-width:min(760px,92vw);cursor:default}
.lbprompt{font-size:12.5px;line-height:1.5;color:rgba(255,255,255,.85);white-space:pre-wrap;word-break:break-word;max-height:26vh;overflow-y:auto;user-select:text;-webkit-user-select:text;cursor:text}
.lbmeta{display:flex;flex-wrap:wrap;gap:6px}
.lbmeta.hide{display:none}
.lbmeta span{font-family:var(--mono);font-size:10.5px;color:rgba(255,255,255,.82);background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.16);border-radius:6px;padding:2px 8px}
.lbmeta span b{color:rgba(255,255,255,.55);font-weight:400;margin-right:3px}
.lbrefs{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.lbrefs.hide{display:none}
.lbrefs .lbrefslbl{font-size:10.5px;color:rgba(255,255,255,.55);margin-right:2px;font-family:var(--mono);text-transform:uppercase;letter-spacing:.04em}
.lbrefs img{width:46px;height:46px;object-fit:cover;border-radius:6px;border:1px solid rgba(255,255,255,.22);cursor:zoom-in;transition:.14s}
.lbrefs img:hover{border-color:#fff;transform:translateY(-1px)}
.lbbtns{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.lbbar button,.lbbar a{display:flex;align-items:center;gap:6px;background:var(--surface);border:1px solid var(--line2);
 color:var(--txt);border-radius:8px;padding:7px 11px;font-size:12px;cursor:pointer;text-decoration:none;transition:.15s;flex:none}
.lbbar button:hover,.lbbar a:hover{border-color:var(--mut)}
.lbbar svg{width:13px;height:13px}
.mini{display:inline-block;border:1px solid currentColor;border-radius:1.5px;opacity:.65;flex:none}
.empty{color:var(--faint);font-size:13px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:12px;padding:20px;max-width:none}
.empty svg{width:30px;height:30px;stroke-width:1.3;opacity:.6}
.empty .kbdhint{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--faint);white-space:nowrap;flex-wrap:nowrap}
.empty .errmsg{color:var(--bad);line-height:1.5;max-width:380px;overflow-wrap:anywhere}
.retry{display:flex;align-items:center;gap:10px;background:var(--surface);border:1px solid var(--line2);color:var(--txt);
 border-radius:13px;padding:17px 36px;font-size:17px;font-weight:500;cursor:pointer;transition:.15s;margin-top:6px}
.retry:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-dim)}
.retry svg{width:19px;height:19px}
.spin{width:34px;height:34px;border:2.5px solid var(--line2);border-top-color:var(--accent);border-radius:50%;animation:sp .8s linear infinite}@keyframes sp{to{transform:rotate(360deg)}}
/* estante de imágenes propias (local) */
.shelf{margin-top:16px;border-top:1px solid var(--line);padding-top:14px}
.shelfhead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px}
.shelftitle{font-size:12.5px;color:var(--txt);font-weight:500;display:flex;align-items:center;gap:7px}
.shelftitle svg{width:15px;height:15px;stroke:var(--mut);fill:none;stroke-width:1.7}
.shelfsub{color:var(--faint);font-weight:400}
.ghost.sm{padding:5px 11px;font-size:12px}
.shelffolder{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--mut);margin-bottom:11px;flex-wrap:wrap}
.shelffolder b{color:var(--txt);font-weight:500}
.linklike{background:none;border:0;color:var(--accent);cursor:pointer;font-size:11.5px;padding:0;text-decoration:underline}
#shelfDirRow{display:flex;gap:6px;align-items:center}
#shelfDirIn{font-size:12px;padding:5px 8px;min-width:210px}
.shelfgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(116px,1fr));gap:9px}
.shelfgrid:empty{display:none}
.scard{position:relative;aspect-ratio:1;border-radius:10px;overflow:hidden;border:1px solid var(--line2);background:var(--surface)}
.scard img{width:100%;height:100%;object-fit:cover;display:block}
.sov{position:absolute;inset:0;display:flex;flex-wrap:wrap;align-content:flex-start;align-items:flex-start;gap:4px;padding:5px;opacity:0;
 background:linear-gradient(to bottom,rgba(0,0,0,.55),transparent 70%);transition:.15s}
.scard:hover .sov{opacity:1}
.shelfgrid.selmode{user-select:none}
.shelfgrid.selmode .scard{cursor:pointer}
.shelfgrid.selmode .scard .sov{display:none}
.shelfgrid.selmode .scard::after{content:'';position:absolute;top:6px;left:6px;width:20px;height:20px;border-radius:50%;border:2px solid #fff;background:rgba(12,12,14,.55);box-shadow:0 0 0 1px rgba(0,0,0,.25);z-index:2}
.shelfgrid.selmode .scard.sel::after{background:var(--accent);border-color:var(--accent);content:'✓';color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
.shelfgrid.selmode .scard.sel{outline:2px solid var(--accent);outline-offset:-2px}
.sbtn{width:24px;height:24px;border-radius:7px;border:0;background:rgba(0,0,0,.62);backdrop-filter:blur(6px);
 display:flex;align-items:center;justify-content:center;cursor:pointer;text-decoration:none}
.sbtn svg{width:13px;height:13px;stroke:#fff;fill:none;stroke-width:2}
.sbtn:hover{background:rgba(0,0,0,.85)}
.shelfempty{font-size:12px;color:var(--faint);text-align:center;padding:18px;border:1px dashed var(--line2);border-radius:10px}
.shelf.dragover{outline:2px dashed var(--accent);outline-offset:4px;border-radius:12px}
.strip{display:flex;gap:8px;margin-top:12px;justify-content:center;flex-wrap:wrap}
.strip .sth{width:62px;height:62px;border-radius:9px;overflow:hidden;border:1px solid var(--line2);cursor:pointer;
 padding:0;background:none;transition:.15s}
.strip .sth img{width:100%;height:100%;object-fit:cover;display:block}
.strip .sth:hover{border-color:var(--mut)}
.strip .sth.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.resbar{display:flex;align-items:center;gap:14px;margin-top:14px}
.costtag{font-size:12px;color:var(--mut)}.costtag b{font-family:var(--mono);color:var(--accent)}
.resbar .acts{margin-left:auto;display:flex;gap:8px}
.resbar a,.resbar .acts button{display:flex;align-items:center;gap:7px;background:var(--surface);border:1px solid var(--line2);
 color:var(--txt);border-radius:9px;padding:9px 13px;font-size:12.5px;cursor:pointer;text-decoration:none;transition:.16s}
.resbar a:hover,.resbar .acts button:hover{border-color:var(--mut)}

/* right */
.sec{margin-bottom:24px}
.sec h3{margin:0 0 12px}
.btnrow{display:flex;gap:7px;margin-top:9px}
.btnrow button{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;background:var(--surface);
 border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:8px;font-size:11.5px;cursor:pointer;transition:.16s}
.btnrow button:hover{color:var(--txt);border-color:var(--line2)}
.btnrow button.arm{color:var(--bad);border-color:var(--bad);background:rgba(229,115,115,.1)}
#style{min-height:74px;font-size:12px}
#prompt{min-height:195px}
#galFilter{font-size:12px;padding:8px 11px;margin-bottom:10px}
.gal{display:grid;grid-template-columns:1fr;gap:8px}
.gcard{position:relative;border:1px solid var(--line);border-radius:10px;overflow:hidden;cursor:zoom-in;background:var(--surface);transition:.16s}
.gcard:hover{border-color:var(--line2)}
.gcard img{width:100%;aspect-ratio:1/1;object-fit:cover;display:block}
.gcard .c{font-family:var(--mono);font-size:9.5px;color:var(--faint);padding:5px 6px;display:flex;justify-content:space-between}
.gfloat{position:absolute;top:5px;right:5px;display:flex;gap:3px;flex-wrap:wrap;max-width:84px;justify-content:flex-end;opacity:0;transform:translateY(-3px);transition:.15s}
.gcard:hover .gfloat{opacity:1;transform:none}
.gfbtn{width:25px;height:25px;border-radius:7px;background:rgba(12,12,14,.86);backdrop-filter:blur(6px);border:1px solid rgba(255,255,255,.18);
 color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;text-decoration:none;transition:.15s}
.gfbtn:hover{background:rgba(12,12,14,.96);border-color:rgba(255,255,255,.4)}.gfbtn svg{width:12px;height:12px;stroke:#fff;stroke-width:1.8}
.gfbtn.arm{border-color:var(--bad);background:rgba(229,115,115,.22)}.gfbtn.arm svg{stroke:#ff9b9b}
.gfbtn.fav{border-color:var(--accent);background:rgba(0,0,0,.6)}.gfbtn.fav svg{stroke:var(--accent)}
.gfbtn.busy{opacity:.4;pointer-events:none}
/* etiquetas de color en imágenes (Historial y Mis imágenes) */
.cdots,.cpick{position:absolute;left:50%;transform:translateX(-50%);bottom:8px;display:flex;gap:5px;z-index:3}
.gcard .cdots,.gcard .cpick{bottom:30px}
.cdots{pointer-events:none;transition:opacity .15s}
.cdot{width:10px;height:10px;border-radius:50%;box-shadow:0 0 0 1.5px rgba(0,0,0,.4)}
.cpick{z-index:5;opacity:0;pointer-events:none;transition:opacity .15s;padding:5px 7px;background:rgba(12,12,14,.5);backdrop-filter:blur(6px);border-radius:999px}
.gcard:hover .cpick,.scard:hover .cpick{opacity:1;pointer-events:auto}
.gcard:hover .cdots,.scard:hover .cdots{opacity:0}
.cpdot{width:16px;height:16px;border-radius:50%;border:2px solid rgba(255,255,255,.35);cursor:pointer;padding:0;flex:none}
.cpdot.on,.cpdot:hover{border-color:#fff}
.cdot.r,.cpdot.r,.cfdot.r,.cflash.r{background:#e5484d}.cdot.y,.cpdot.y,.cfdot.y,.cflash.y{background:#f5b400}.cdot.g,.cpdot.g,.cfdot.g,.cflash.g{background:#46a758}.cdot.b,.cpdot.b,.cfdot.b,.cflash.b{background:#3b82f6}
.cflash{position:absolute;border-radius:50%;z-index:4;pointer-events:none;opacity:.5}
.cflash.cin{animation:cflashin .5s cubic-bezier(.33,0,.2,1) forwards}
.cflash.cout{animation:cflashout .42s cubic-bezier(.5,0,.5,1) forwards}
@keyframes cflashin{0%{transform:scale(0);opacity:.6}65%{opacity:.5}100%{transform:scale(1);opacity:0}}
@keyframes cflashout{0%{transform:scale(1);opacity:.5}100%{transform:scale(0);opacity:.5}}
.cfilt{display:inline-flex;gap:4px;align-items:center;vertical-align:middle;margin:0 2px}
.cfdot{width:13px;height:13px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0;opacity:.45}
.cfdot:hover{opacity:.8}.cfdot.on{opacity:1;border-color:var(--txt)}
.gal.selmode .cdots,.shelfgrid.selmode .cdots,.gal.selmode .cpick,.shelfgrid.selmode .cpick{display:none}
.gcard.reordering,.scard.reordering{opacity:.35;transition:opacity .15s}
.sharepop{position:fixed;z-index:3000;background:var(--surface);border:1px solid var(--line2);border-radius:12px;padding:6px;box-shadow:0 16px 44px rgba(0,0,0,.32);display:flex;flex-direction:column;gap:2px;min-width:210px}
.sharepop button{display:block;width:100%;background:none;border:0;color:var(--txt);text-align:left;padding:9px 12px;border-radius:8px;font-size:13px;font-family:inherit;cursor:pointer}
.sharepop button:hover{background:var(--accent-dim);color:var(--accent)}
.bakprog{margin-top:12px}
.bakprogbar{position:relative;height:9px;background:var(--surface2);border-radius:6px;overflow:hidden;border:1px solid var(--line)}
.bakprogbar::after{content:'';position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--line);opacity:.7;z-index:2}
.bakprogfill{height:100%;width:0;background:var(--accent);border-radius:6px;transition:width .2s ease}
.bakprogfill.prep{background:#c79a4e}   /* Paso 1/2 (Preparando) en un color distinto al de descarga */
.bakprogtxt{display:block;margin-top:7px;font-size:12px;color:var(--mut);font-family:var(--mono)}
.gal.selmode .gcard{cursor:pointer}
.gal.selmode .gcard .gfloat{display:none}
.gal.selmode .gcard::after{content:'';position:absolute;top:6px;left:6px;width:20px;height:20px;border-radius:50%;border:2px solid #fff;background:rgba(12,12,14,.55);box-shadow:0 0 0 1px rgba(0,0,0,.25);z-index:2}
.gal.selmode .gcard.sel::after{background:var(--accent);border-color:var(--accent);content:'✓';color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
.gal.selmode .gcard.sel{outline:2px solid var(--accent);outline-offset:-2px}
.gal.selmode{user-select:none}
.gmarq{position:fixed;border:1.5px solid var(--accent);background:var(--accent-dim);z-index:50;pointer-events:none;border-radius:4px;display:none}
.galbulk{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:1200;display:flex;align-items:center;gap:6px;
 padding:9px 12px;background:var(--surface);border:1px solid var(--line2);border-radius:16px;
 box-shadow:0 18px 44px rgba(0,0,0,.3),0 2px 8px rgba(0,0,0,.14);font-size:12.5px;
 backdrop-filter:blur(14px) saturate(1.2);-webkit-backdrop-filter:blur(14px) saturate(1.2);max-width:94vw}
.galbulk.hide{display:none}
.galbulk .gbcount{font-weight:600;color:var(--accent);white-space:nowrap;margin:0 6px 0 6px}
.galbulk button{border:0;background:none;color:var(--txt);border-radius:10px;padding:8px 13px;font-size:12.5px;font-weight:500;cursor:pointer;font-family:inherit;display:flex;align-items:center;gap:6px;white-space:nowrap;transition:background .14s,color .14s}
.galbulk button:hover{background:var(--accent-dim);color:var(--accent)}
.galbulk button svg{width:15px;height:15px}
.galbulk button.bdel{color:var(--bad)}
.galbulk button.bdel:hover{background:rgba(180,69,47,.1);color:var(--bad)}
.galbulk button.bdel.arm{background:var(--bad);color:#fff}
#bulkExit{color:var(--mut)}
.galeye{flex-wrap:wrap;row-gap:7px}
.galeye #galCount{margin-left:6px}
.galactions{display:flex;align-items:center;gap:6px;margin-left:auto;flex-wrap:wrap;justify-content:flex-end}
.galsort{background:var(--surface);border:1px solid var(--line2);color:var(--txt);border-radius:8px;padding:5px 8px;font-size:12px;font-family:inherit;cursor:pointer;flex:none;max-width:150px}
.galsort:hover{border-color:var(--mut)}
.magic{float:right;background:none;border:0;color:var(--faint);cursor:pointer;padding:0 2px;line-height:1;transition:.15s}
.magic:hover{color:var(--accent)}
.magic svg{width:13px;height:13px}
.mpbtn{display:inline-flex;align-items:center;gap:7px;margin-top:8px;padding:8px 12px;font-size:12.5px;width:100%;justify-content:center}
.mpbtn svg{color:var(--accent)}
.mpbtn.busy{opacity:.55;pointer-events:none}
.ang3d{display:flex;flex-direction:column;gap:9px;margin-top:12px}
.ang3d.hide{display:none}
.ang3d .hide{display:none}
#ang3dCv{display:block;width:100%;height:auto;background:var(--surface2);border:1px solid var(--line);border-radius:12px;cursor:grab;touch-action:none}
#ang3dCv:active{cursor:grabbing}
.ang3dseg{padding:3px;border-radius:10px}
.ang3dseg button{flex:1;justify-content:center;font-size:12px;padding:6px 8px;border-radius:7px;white-space:nowrap}
.ang3drow{display:flex;flex-direction:column;gap:5px}
.ang3drow-h{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.ang3drow-h span:first-child{font-size:11px;color:var(--mut);font-weight:500;text-transform:uppercase;letter-spacing:.03em}
.ang3dval{font-size:11px;color:var(--accent);font-weight:600;font-family:var(--mono);white-space:nowrap}
.ang3drow input[type=range]{width:100%;margin:0;accent-color:var(--accent);height:4px;cursor:pointer}
.ang3dtxt{font-size:11.5px;color:var(--accent);font-weight:500;line-height:1.4;background:var(--accent-dim);border-radius:9px;padding:8px 11px}
.ang3dpresets{display:flex;flex-wrap:wrap;gap:5px}
.ang3dpresets button{font-size:11px;padding:5px 10px;border:1px solid var(--line);background:var(--surface);color:var(--mut);border-radius:8px;cursor:pointer;font-family:inherit;white-space:nowrap}
.ang3dpresets button:hover{border-color:var(--accent);color:var(--accent)}
.ang3dchk{font-size:11.5px;margin:2px 0 0}
.ang3duse{display:flex;gap:16px;flex-wrap:wrap}
.ang3duse .check{font-size:12px;margin:0}
.ang3dseg button.off{opacity:.4;pointer-events:none}
.exp2{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--accent);background:var(--accent-dim);padding:1px 6px;border-radius:20px;margin-left:6px}
/* modal Ángulos 3D (detección + gizmos) */
.modal.posemodal{max-width:min(1040px,96vw);width:96%;max-height:92vh;overflow:auto}
.posemodal .exp{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);background:var(--accent-dim);padding:2px 7px;border-radius:20px;vertical-align:middle;margin-left:8px}
.posewrap{display:flex;flex-direction:column;gap:14px}
.posestage{width:100%;position:relative;display:flex;align-items:center;justify-content:center;background:var(--surface2);border:1px solid var(--line);border-radius:10px;min-height:300px;max-height:58vh;overflow:hidden}
.posestage img{max-width:100%;max-height:58vh;display:block}
.poseov{position:absolute}
.posebusy{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.32);border-radius:10px}
.posebusy.hide{display:none}
.posebusy .spin{width:34px;height:34px;border:3px solid rgba(255,255,255,.25);border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite}
.posebox{position:absolute;border:2px solid var(--accent);border-radius:6px;box-shadow:0 0 0 9999px rgba(0,0,0,0);pointer-events:none}
.posebox.sel{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent)}
.posebox .plabel{position:absolute;top:-10px;left:6px;font-size:10px;font-weight:600;color:#fff;background:var(--accent);padding:1px 7px;border-radius:10px;white-space:nowrap}
.posecube{position:absolute;width:74px;height:74px;transform:translate(-50%,-50%);cursor:grab;touch-action:none;filter:drop-shadow(0 2px 4px rgba(0,0,0,.5))}
.posecube:active{cursor:grabbing}
.poseside{width:auto;display:flex;flex-direction:column;gap:10px}
.posecam{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:9px;padding:9px;background:var(--surface2)}
#poseCamCv{flex:none;width:104px;height:94px;background:var(--surface);border:1px solid var(--line);border-radius:8px;cursor:grab;touch-action:none}
#poseCamCv:active{cursor:grabbing}
.posecaminfo{flex:1;min-width:0;display:flex;flex-direction:column;gap:5px}
.posecamlbl{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--faint)}
.posecamtxt{font-size:11.5px;color:var(--accent);line-height:1.35}
.poselist{display:flex;flex-direction:column;gap:8px;overflow:auto;flex:1;max-height:56vh}
.posesub{border:1px solid var(--line);border-radius:9px;padding:9px;background:var(--surface2);cursor:pointer}
.posesub.sel{border-color:var(--accent)}
.posesub .pnm{font-size:12.5px;font-weight:600}
.posesub .pdesc{font-size:11.5px;color:var(--accent);margin:3px 0 6px;line-height:1.4}
.posesub .ppre{display:flex;flex-wrap:wrap;gap:4px}
.posesub .ppre button{font-size:10px;padding:3px 7px;border:1px solid var(--line);background:var(--surface);color:var(--mut);border-radius:6px;cursor:pointer;font-family:inherit}
.posesub .ppre button:hover{border-color:var(--accent);color:var(--accent)}
.posefoot{display:flex;flex-direction:column;gap:8px;margin-top:auto}
.posefoot button{justify-content:center}
@media(min-width:900px){.posewrap{flex-direction:row;align-items:stretch}.posestage{flex:1;min-width:0;width:auto;max-height:78vh}.posestage img{max-height:78vh}.poseside{width:300px;flex:none}}
#galSearch{font-size:12px;padding:8px 11px;margin-bottom:8px}
.galrow{display:flex;gap:7px;margin-bottom:10px;align-items:center}
.galrow select{margin:0;flex:1}
#galFavBtn{flex:none;font-family:var(--ui)}
.more{width:100%;display:flex;align-items:center;justify-content:center;gap:7px;background:var(--surface);
 border:1px solid var(--line);color:var(--mut);border-radius:9px;padding:9px;font-size:12px;cursor:pointer;margin-top:10px;transition:.16s}
.more:hover{color:var(--txt);border-color:var(--line2)}

/* modal */
.overlay{position:fixed;inset:0;background:rgba(5,5,6,.78);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;z-index:var(--z-modal)}
.cmdk{position:fixed;inset:0;background:rgba(5,5,6,.5);backdrop-filter:blur(4px);display:flex;align-items:flex-start;justify-content:center;z-index:var(--z-lightbox);padding-top:12vh}
.cmdk.hide{display:none}
.cmdkbox{background:var(--surface);border:1px solid var(--line2);border-radius:14px;width:min(620px,94vw);box-shadow:0 30px 80px rgba(0,0,0,.55);overflow:hidden;display:flex;flex-direction:column}
#cmdkq{border:0;border-bottom:1px solid var(--line);background:transparent;color:var(--txt);font-size:15px;padding:16px 18px;outline:none;font-family:inherit}
.cmdklist{max-height:48vh;overflow-y:auto;padding:6px}
.cmdkrow{padding:10px 12px;border-radius:9px;cursor:pointer;font-size:13px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cmdkrow b{color:var(--txt);font-weight:600}
.cmdkrow.sel{background:var(--accent-dim);color:var(--txt)}
.cmdkrow.sel b{color:var(--accent)}
.cmdkempty{padding:24px;text-align:center;color:var(--faint);font-size:13px}
.cmdkhint{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 14px;border-top:1px solid var(--line);font-size:11px;color:var(--faint);font-family:var(--mono)}
.cmdkhint a{color:var(--accent);text-decoration:none}
.modal{background:var(--surface);border:1px solid var(--line2);border-radius:18px;padding:30px;max-width:440px;width:92%;
 box-shadow:0 30px 80px rgba(0,0,0,.6);position:relative}
.mclose{position:absolute;top:13px;right:13px;width:30px;height:30px;border-radius:8px;background:var(--surface2);
 border:1px solid var(--line2);color:var(--mut);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:.15s;z-index:2}
.mclose:hover{color:var(--txt);border-color:var(--mut)}
.mclose svg{width:13px;height:13px;stroke-width:2}
.modal .ic{width:42px;height:42px;border-radius:12px;background:var(--accent-dim);display:flex;align-items:center;justify-content:center;color:var(--accent);margin-bottom:16px}
.modal h2{margin:0 0 7px;font-size:19px;font-weight:600}
.modal p{color:var(--mut);font-size:13px;margin:0 0 18px;line-height:1.55}.modal a{color:var(--accent)}
.setmodal{max-width:520px}
.setmodal h2{font-size:22px}
.guide{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.guide details{border:1px solid var(--line);border-radius:10px;background:var(--surface2);overflow:hidden}
.guide details[open]{border-color:var(--line2)}
.guide summary{cursor:pointer;padding:11px 13px;font-size:13.5px;font-weight:600;color:var(--txt);list-style:none;user-select:none;display:flex;align-items:center;gap:8px}
.guide summary::-webkit-details-marker{display:none}
.guide summary::after{content:'＋';margin-left:auto;color:var(--faint);font-weight:400}
.guide details[open] summary::after{content:'－'}
.guide summary:hover{color:var(--accent)}
.guide details>p{margin:0 13px 11px;font-size:12.5px;line-height:1.6;color:var(--mut)}
.guide details>p:first-of-type{margin-top:2px}
.guide details b{color:var(--txt);font-weight:600}
.guide details code{font-family:var(--mono);font-size:11.5px;background:var(--accent-dim);color:var(--accent);padding:1px 5px;border-radius:5px}
.guide kbd{font-family:var(--mono);font-size:11px;background:var(--elev);border:1px solid var(--line2);border-radius:5px;padding:1px 6px;color:var(--txt)}
.guide .gi{width:16px;height:16px;flex:none;stroke:currentColor;fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;color:var(--accent)}
.tutmodal{max-width:580px;max-height:84vh;overflow-y:auto}
.tutmodal h2{font-size:21px}
.setmodal .setlabel{font-size:12.5px}
.setmodal .setsublabel{font-size:13.5px}
.setmodal .langseg button{font-size:14.5px;padding:11px}
.setmodal .swatch{font-size:14px;padding:11px 13px}
.setmodal .swatch span{width:20px;height:20px}
.setmodal .setpath{font-size:13.5px}
.setmodal .setfolder .ghost{font-size:13px}
.setmodal .hint{font-size:13px;color:var(--mut);line-height:1.6}
.setsec{margin-top:22px}
.setlabel{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin-bottom:12px;font-weight:600}
.setsublabel{font-size:12.5px;color:var(--mut);margin:12px 0 8px;font-weight:500}
.setsublabel:first-of-type{margin-top:2px}
.setfolder{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid var(--line)}
.setfolder:first-of-type{border-top:0;padding-top:2px}
.setpath{font-size:12.5px;color:var(--txt);word-break:break-all;margin-top:3px}
.fsrow{display:flex;align-items:center;gap:12px;margin:12px 0}
.fsrow>span:first-child{font-size:13.5px;color:var(--txt);min-width:140px}
.fsrow input[type=range]{flex:1}
.fsrow .fsv{font-family:var(--mono);font-size:13px;color:var(--mut);min-width:46px;text-align:right}
.langseg{display:flex;gap:6px}
.langseg button{flex:1;padding:9px;border-radius:10px;background:var(--surface2);border:1px solid var(--line);color:var(--mut);cursor:pointer;font-size:13px;font-family:var(--ui);transition:.15s}
.langseg button:hover{color:var(--txt);border-color:var(--line2)}
.langseg button.on{background:var(--accent-dim);border-color:var(--accent);color:var(--accent)}
.themegrid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
.swatch{display:flex;align-items:center;gap:8px;padding:9px 11px;border-radius:11px;background:var(--surface2);border:1px solid var(--line);color:var(--mut);cursor:pointer;font-size:12.5px;font-family:var(--ui);transition:.15s}
.swatch:hover{color:var(--txt);border-color:var(--line2)}
.swatch.on{border-color:var(--accent);color:var(--txt)}
.swatch span{width:18px;height:18px;border-radius:50%;flex:none;background:var(--s-bg);border:1px solid rgba(128,128,128,.45);box-shadow:inset -7px -7px 0 -3px var(--s-ac)}
.modal input{margin-bottom:8px}.kmsg{font-size:12px;color:var(--mut);min-height:16px;margin-bottom:12px}

/* editor de imagen: máscara · anotar · pins */
.maskbox{background:var(--surface);border:1px solid var(--line2);border-radius:16px;padding:18px;max-width:980px;width:94%;
 box-shadow:0 30px 80px rgba(0,0,0,.6);position:relative}
.masktop{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:12px;flex-wrap:wrap;padding-right:38px}
.masktools{display:flex;align-items:center;gap:7px}
.mtool{width:32px;height:32px;border-radius:9px;background:var(--surface2);border:1px solid var(--line2);color:var(--mut);
 display:flex;align-items:center;justify-content:center;cursor:pointer;transition:.15s}
.mtool:hover{color:var(--txt);border-color:var(--mut)}
.mtool.on{background:var(--accent-dim);border-color:var(--accent);color:var(--accent)}
.mtool svg{width:14px;height:14px}
.masktools input[type=range]{width:110px;height:18px}
.edbody{display:flex;gap:12px;align-items:stretch}
.edbody .maskarea{flex:1;min-width:0}
.maskarea{display:flex;justify-content:center;background:var(--bg);border:1px solid var(--line);border-radius:12px;overflow:hidden;padding:10px}
.maskstack{position:relative;display:inline-block;line-height:0}
.maskstack img{max-width:100%;max-height:58vh;display:block;user-select:none;-webkit-user-drag:none}
.maskstack canvas{position:absolute;inset:0;width:100%;height:100%;cursor:crosshair;touch-action:none}
#maskDraw{opacity:.55}
#annoDraw{opacity:1;pointer-events:none}
#pinLayer{position:absolute;inset:0;pointer-events:none;cursor:copy}
.pin{position:absolute;transform:translate(-50%,-50%);width:22px;height:22px;border-radius:50%;background:#e5483f;
 border:2px solid #fff;color:#fff;font-family:var(--mono);font-size:11px;font-weight:700;line-height:1;
 display:flex;align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.5);cursor:pointer;pointer-events:inherit}
#annoText{position:absolute;width:210px;transform:translate(-8px,-130%);background:var(--elev);border:1px solid #e5483f;
 color:var(--txt);border-radius:8px;padding:7px 10px;font-size:13px;font-family:var(--ui);z-index:3}
#annoText:focus{outline:none}
.pinlist{width:236px;flex:none;display:flex;flex-direction:column;gap:8px;overflow:auto;max-height:62vh;padding:2px}
.pinrow{display:flex;align-items:center;gap:7px}
.pinrow .pinnum{width:22px;height:22px;border-radius:50%;background:#e5483f;color:#fff;font-family:var(--mono);
 font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex:none}
.pinrow input{font-size:12px;padding:8px 10px}
.pinrow .x{width:24px;height:24px;border:1px solid var(--line2);background:var(--surface2);border-radius:7px;
 color:var(--mut);cursor:pointer;display:flex;align-items:center;justify-content:center;flex:none;transition:.15s}
.pinrow .x:hover{color:var(--bad);border-color:var(--bad)}
.pinrow .x svg{width:10px;height:10px}
.maskfoot{display:flex;justify-content:space-between;align-items:center;gap:9px;margin-top:14px}
.maskfoot .hint{margin:0}
.maskfoot .acts{display:flex;gap:9px}
.maskfoot .primary{width:auto;padding:11px 22px}

/* audio + video */
#audioStage,#videoStage{display:flex;flex-direction:column;gap:14px;flex:1;min-height:0}
.audcard{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:18px;display:flex;flex-direction:column;gap:12px}
.audhead{display:flex;justify-content:space-between;align-items:center;font-size:13px;font-weight:600}
audio{width:100%;height:40px}
#txText{font-size:13px;line-height:1.6;background:var(--bg)}
.dim{opacity:.4;pointer-events:none}
.arow{display:flex;align-items:center;gap:8px;padding:8px 9px;border:1px solid var(--line);border-radius:10px;background:var(--surface);margin-bottom:7px;transition:.15s}
.arow:hover{border-color:var(--line2)}
.arow .ap{width:30px;height:30px;border-radius:8px;background:var(--surface2);border:1px solid var(--line2);color:var(--txt);
 display:flex;align-items:center;justify-content:center;cursor:pointer;flex:none;transition:.15s}
.arow .ap:hover{border-color:var(--accent);color:var(--accent)}
.arow .ap.playing{background:var(--accent-dim);border-color:var(--accent);color:var(--accent)}
.arow .ap svg{width:12px;height:12px}
.ameta{flex:1;min-width:0}
.at{font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.as{font-size:9.5px;color:var(--faint)}
.vsx{background:none;border:0;color:var(--faint);cursor:pointer;font-size:12px;padding:0 0 0 3px;line-height:1}
.vsx:hover{color:var(--bad)}
#vsAdd{color:var(--accent);border-style:dashed}

/* referencias seedance */
.refcard{display:flex;align-items:center;gap:7px;padding:7px;border:1px solid var(--line);border-radius:10px;background:var(--surface);margin-top:7px}
.refcard .rtag{font-family:var(--mono);font-size:9px;color:var(--accent);width:34px;flex:none;text-align:center;text-transform:uppercase}
.refcard img.rthumb{width:40px;height:40px;border-radius:8px;object-fit:cover;flex:none;border:1px solid var(--line2)}
.refcard .rkind{width:40px;height:40px;border-radius:8px;flex:none;border:1px solid var(--line2);background:var(--surface2);
 display:flex;align-items:center;justify-content:center;color:var(--mut)}
.refcard .rkind svg{width:16px;height:16px}
.refcard select{flex:1.3;font-size:11px;padding:7px 24px 7px 8px;margin:0;min-width:0}
.refcard input[type=text]{flex:1;font-size:11px;padding:7px 8px;min-width:0}
.refcard .x{width:24px;height:24px;border:1px solid var(--line2);background:var(--surface2);border-radius:7px;
 color:var(--mut);cursor:pointer;display:flex;align-items:center;justify-content:center;flex:none;transition:.15s}
.refcard .x:hover{color:var(--bad);border-color:var(--bad)}
.refcard .x svg{width:10px;height:10px}
#sdPrev{background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:9px 11px;margin-top:8px;font-style:italic;line-height:1.6}

/* toasts */
.toasts{position:fixed;top:18px;left:50%;transform:translateX(-50%);z-index:var(--z-toast);display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none}
.toast{display:flex;align-items:center;gap:9px;background:var(--elev);border:1px solid var(--line2);border-radius:10px;
 padding:10px 16px;font-size:13px;color:var(--txt);box-shadow:0 12px 40px rgba(0,0,0,.5);
 animation:toastIn .25s cubic-bezier(.2,.7,.2,1);transition:.25s;max-width:min(480px,90vw)}
.toast::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--ok);flex:none}
.toast.bad::before{background:var(--bad)}
@keyframes toastIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}

/* comparador A/B */
.cmpwrap{position:relative;max-width:90vw;max-height:78vh;cursor:default;line-height:0}
.cmpwrap img{max-width:90vw;max-height:78vh;display:block;border-radius:8px}
#cmpBwrap{position:absolute;inset:0;overflow:hidden}
#cmpBwrap img{position:absolute;left:0;top:0}
#cmpLine{position:absolute;top:0;bottom:0;width:2px;background:var(--accent);box-shadow:0 0 10px rgba(224,165,113,.6);pointer-events:none}
.cmptag{position:absolute;top:12px;font-family:var(--mono);font-size:11px;font-weight:700;color:#fff;background:rgba(12,12,14,.85);
 border:1px solid var(--line2);border-radius:6px;padding:3px 8px;pointer-events:none}
#cmpSlider{position:fixed;left:50%;bottom:30px;transform:translateX(-50%);width:min(420px,80vw)}

.hide{display:none!important}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:var(--line2);border-radius:9px;border:2px solid var(--bg)}

/* responsive · teléfono */
html,body{overflow-x:hidden}
.col{min-width:0}
.col img,.canvas img,.audcard video{max-width:100%}
@media(max-width:760px){
 .top{flex-wrap:wrap;gap:8px;padding:10px 12px}
 .projbar{position:static;transform:none;width:100%;justify-content:center;order:3}
 .projbar select{flex:1;max-width:none}
 .top .right{margin-left:auto;gap:8px}
 .sess{font-size:11px}
 .seg button kbd{display:none}
 .seg button{padding:6px 10px;font-size:12px}
 .col{padding:14px}
 .canvas{max-height:48vh}
 .maskbox{padding:12px}
 .edbody{flex-direction:column}
 .pinlist{width:100%;max-height:180px}
 .masktools input[type=range]{width:70px}
 .lbprompt{max-width:none;max-height:30vh}
 .lbbar{max-width:94vw}
 .lbbtns{justify-content:center}
 .resbar{flex-wrap:wrap;gap:8px}
 .resbar .acts{margin-left:0}
}
@media(max-width:480px){
 .seg button svg{display:none}
 .seg button{padding:6px 8px;font-size:11px}
 .brand{font-size:13px}
 .ghost{padding:6px 9px;font-size:11px}
 .grid2{grid-template-columns:1fr}
 .modal{padding:20px}
 .maskfoot{flex-wrap:wrap}
 .strip .sth{width:50px;height:50px}
 #cmpSlider{bottom:16px}
}

@media (prefers-reduced-motion: reduce){
 .an{animation:none!important;opacity:1!important;transform:none!important}
 .toast{animation:none!important}
 .floaters,.gfloat{transition:none!important}
 .primary:hover{transform:none}
 *{transition-duration:.01ms!important}
 .spin{animation-duration:1.6s!important}
}
</style></head><body><script>(function(){try{var t=localStorage.getItem('studio_theme'),done=localStorage.getItem('studio_theme_default_v2');if(!done&&(!t||t==='carbon'))t='crema';if(!t)t='crema';if(t!=='carbon')document.body.dataset.theme=t;}catch(e){document.body.dataset.theme='crema';}})();</script>

<div class="toasts" id="toasts"></div>

<div class="overlay hide" id="keyModal"><div class="modal">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <div class="ic"><svg viewBox="0 0 24 24"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg></div>
  <h2>Conecta tu API de OpenAI</h2>
  <p>Pega tu clave para empezar. Se guarda solo en tu equipo (<span class="mono">~/.openai_key</span>) y nunca sale de aquí. Consíguela en <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener noreferrer">platform.openai.com</a>.</p>
  <input type="password" id="keyInput" placeholder="sk-proj-…" autocomplete="off">
  <div class="kmsg" id="keyMsg"></div>
  <button class="primary" id="keySave">Conectar</button>
</div></div>

<div class="overlay hide" id="setModal"><div class="modal setmodal">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <h2>Ajustes</h2>
  <div class="setsec">
    <div class="setlabel">Idioma</div>
    <div class="langseg" id="langSeg">
      <button data-lang="es" class="on">Español</button>
      <button data-lang="en">English</button>
      <button data-lang="fr">Français</button>
    </div>
  </div>
  <div class="setsec" id="themeWrap">
    <div class="setlabel">Tema</div>
    <div class="setsublabel">Oscuros</div>
    <div class="themegrid">
      <button class="swatch" data-theme="carbon" style="--s-bg:#0f0d0c;--s-ac:#e0a571"><span></span>Carbón</button>
      <button class="swatch" data-theme="medianoche" style="--s-bg:#070d18;--s-ac:#22d3ee"><span></span>Medianoche</button>
      <button class="swatch" data-theme="neon" style="--s-bg:#0c0716;--s-ac:#ff3ea5"><span></span>Neón</button>
    </div>
    <div class="setsublabel">Claros</div>
    <div class="themegrid">
      <button class="swatch" data-theme="dia" style="--s-bg:#faf7f2;--s-ac:#b8492a"><span></span>Día</button>
      <button class="swatch" data-theme="bruma" style="--s-bg:#eef1f6;--s-ac:#4654c7"><span></span>Bruma</button>
      <button class="swatch" data-theme="crema" style="--s-bg:#f4efe3;--s-ac:#1f6b54"><span></span>Crema</button>
    </div>
  </div>
  <div class="setsec">
    <div class="setlabel">Tamaño del texto</div>
    <div class="fsrow"><span>General</span><input type="range" id="fsGen" min="12" max="20" step="1" value="14"><span class="fsv" id="fsGenV">14px</span></div>
    <div class="fsrow"><span>Etiquetas y ayudas</span><input type="range" id="fsSmall" min="9" max="16" step="1" value="11"><span class="fsv" id="fsSmallV">11px</span></div>
    <button class="ghost" id="fsReset" style="margin-top:2px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l3 2"/></svg>Restablecer</button>
  </div>
  <div class="setsec">
    <div class="setlabel">Carpetas · proyecto «<span id="setFolderProj" style="text-transform:none">General</span>»</div>
    <div class="setfolder">
      <div><div class="setsublabel" style="margin-top:0">Imágenes generadas (copia del historial)</div><div class="mono setpath" id="setGenPath">…</div></div>
      <button class="ghost" id="setGenPick" style="flex:none"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>Cambiar…</button>
    </div>
    <div class="setfolder">
      <div><div class="setsublabel" style="margin-top:0">Mis imágenes (siempre a la mano)</div><div class="mono setpath" id="setShelfPath">…</div></div>
      <button class="ghost" id="setShelfPick" style="flex:none"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>Cambiar…</button>
    </div>
    <p class="hint" style="margin-top:10px">Estas carpetas son <b>de este proyecto</b> (cambia de proyecto en la barra superior para configurar otro). Así cada proyecto guarda sus archivos por separado y no se mezclan. El historial siempre se guarda también dentro de la app.</p>
  </div>
  <div class="setsec">
    <div class="setlabel">Ayuda</div>
    <button class="ghost" id="tutBtn" style="justify-content:center"><svg viewBox="0 0 24 24" style="width:15px;height:15px"><circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>Ver tutorial · todas las funciones</button>
  </div>
</div></div>

<div class="overlay hide" id="tutModal"><div class="modal tutmodal">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <h2>Tutorial · Gio Studio</h2>
  <p class="modsub" style="color:var(--mut);font-size:13px;margin:-4px 0 14px">Todas las funciones de la app, sección por sección. Toca cada una para desplegarla.</p>
  <div class="guide">
    <details open><summary><svg class="gi" viewBox="0 0 24 24"><path d="M13 2L3 14h7l-1 8 10-12h-7z"/></svg>Lo básico</summary>
      <p>Arriba eliges entre <b>Imagen</b>, <b>Audio</b> y <b>Video</b> (también con las teclas <kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd>). Escribe tu idea en el cuadro de <b>prompt</b> y pulsa <kbd>Enter</kbd> para generar (<kbd>Shift</kbd>+<kbd>Enter</kbd> hace salto de línea). El <b>costo se estima antes</b> de generar y se muestra el <b>costo real</b> después; el gasto de la sesión aparece arriba a la derecha. La imagen es 100% <b>gpt-image-2</b> de OpenAI.</p>
    </details>
    <details><summary><svg class="gi" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>Imagen</summary>
      <p><b>Crear</b> = texto → imagen. <b>Editar</b> = subes una o varias imágenes de referencia + un prompt (incluso con máscara para cambiar solo una zona).</p>
      <p><b>Referencias (opcional):</b> arrastra, pega (<kbd>⌘V</kbd>) o elige imágenes; también puedes <b>soltar un video</b> y se abre un selector de <b>fotogramas</b> (mueves la línea de tiempo y capturas los que quieras).</p>
      <p><b>Máscara / Anotar / Pins:</b> pinta la zona a editar, dibuja flechas/círculos o pon pines numerados con una instrucción por punto.</p>
      <p><b>Tamaño:</b> sliders de ancho/alto + <b>candado de proporción</b>, presets por proporción (nativos de gpt-image-2 en verde) y chips de resolución 720p→4K.</p>
      <p><b>Avanzado:</b> formato (PNG/JPEG/WebP), moderación e <b>imágenes parciales</b> (preview en vivo). El botón <b>✨</b> junto al prompt lo mejora con IA. Genera varias a la vez con <b>Cantidad</b>.</p>
    </details>
    <details><summary><svg class="gi" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>Proyectos</summary>
      <p>El botón junto a «Gio Studio» abre tus <b>proyectos</b>. Cada uno tiene su <b>propia memoria, su historial y sus «Mis imágenes»</b>. Puedes crear, renombrar y borrar; la portada es la última imagen. El espacio <b>General</b> se puede renombrar y también es un proyecto completo.</p>
      <p><b>Memoria visual:</b> referencias que se <b>adjuntan solas</b> en cada generación del proyecto (para mantener estilo/personaje). Además un <b>Estilo</b> de texto que se antepone a tus prompts, y <b>Destilar</b> (la IA resume el estilo desde tus prompts).</p>
    </details>
    <details><summary><svg class="gi" viewBox="0 0 24 24"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l3 2"/></svg>Historial</summary>
      <p>Cada imagen generada queda aquí. Al pasar el cursor: <b>favorita</b>, <b>Mejorar 2×</b> (upscale), <b>Comparar A/B</b>, <b>Iterar</b>, <b>Descargar</b>, <b>Copiar prompt</b>, <b>Enviar prompt a la biblioteca</b>, <b>Usar como referencia</b> y <b>Borrar</b> (doble clic).</p>
      <p><b>Seleccionar:</b> marca varias y <b>envíalas a la biblioteca</b> o <b>bórralas en lote</b>. <b>Buscar</b> filtra por prompt; <b>Ver todo</b> abre una galería en pestaña aparte.</p>
    </details>
    <details><summary><svg class="gi" viewBox="0 0 24 24"><path d="M12 2 2 7l10 5 10-5z"/><path d="M2 17l10 5 10-5M2 12l10 5 10-5"/></svg>Mis imágenes</summary>
      <p>Tu estante <b>local</b> (no se sube a OpenAI), bajo el lienzo. Arrastra imágenes del historial o del resultado para guardarlas; puedes cambiar la carpeta. Soltar un <b>video</b> abre el selector de fotogramas. Clic en una para ampliarla.</p>
    </details>
    <details><summary><svg class="gi" viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>Prompt Library</summary>
      <p>El botón <b>«Prompt Library»</b> abre tu biblioteca en una <b>pestaña aparte</b>. Guarda prompts con <b>favorito</b> y <b>veredicto</b> (sirve / no sirve / sin probar), búscalos y fíltralos.</p>
      <p><b>Categorías en árbol:</b> crea <b>subcarpetas</b> (＋), <b>arrástralas</b> para reordenar (arriba/abajo) o anidar (en el centro), renómbralas.</p>
      <p><b>Compositores:</b> ten <b>varios a la vez</b>. Combinas prompts, los mejoras con <b>IA</b>, y los <b>envías a la interfaz principal</b> o los guardas. Las <b>plantillas</b> con <code>{variables}</code> te piden rellenar los huecos al usarlas.</p>
      <p><b>Mover prompts:</b> arrastra una tarjeta a una categoría o usa el botón <b>«Mover»</b>. Atajo <kbd>⌘</kbd>/<kbd>Ctrl</kbd>+<kbd>K</kbd> en la interfaz principal busca e inserta un prompt sin abrir la pestaña.</p>
    </details>
    <details><summary><svg class="gi" viewBox="0 0 24 24"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/></svg>Visor a pantalla completa</summary>
      <p>Clic en cualquier imagen para verla grande. Navega con las <b>flechas</b> <kbd>←</kbd> <kbd>→</kbd>. Abajo: <b>Usar prompt</b>, <b>A la biblioteca</b>, <b>Describir</b> y <b>Descargar</b>. <kbd>Esc</kbd> cierra.</p>
    </details>
    <details><summary><svg class="gi" viewBox="0 0 24 24"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M10 9l5 3-5 3z"/></svg>Audio y Video</summary>
      <p><b>Audio:</b> voz (TTS) con tono y voces, <b>Transcripción</b>, <b>Efectos de sonido</b> y <b>Música</b>. <b>Video:</b> Seedance, Kling y OmniHuman vía fal.ai. El video y parte del audio necesitan conectar su <b>clave</b> (botón API / fal).</p>
    </details>
    <details><summary><svg class="gi" viewBox="0 0 24 24"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>Personalización y respaldo</summary>
      <p><b>6 temas</b> (3 oscuros + 3 claros), <b>idioma</b> Español/English/Français, <b>tamaño del texto</b> y <b>carpetas</b> de guardado por proyecto — todo aquí en Ajustes. El botón <b>Backup</b> (arriba) descarga/sincroniza todo tu contenido.</p>
    </details>
    <details><summary><svg class="gi" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h12"/></svg>Atajos de teclado</summary>
      <p><kbd>Enter</kbd> genera · <kbd>Shift</kbd>+<kbd>Enter</kbd> salto de línea · <kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd> cambia de modo · <kbd>←</kbd>/<kbd>→</kbd> navega en el visor · <kbd>⌘</kbd>/<kbd>Ctrl</kbd>+<kbd>K</kbd> buscador de prompts · <kbd>Esc</kbd> cierra ventanas · doble clic en la papelera borra.</p>
    </details>
  </div>
</div></div>

<div class="overlay hide" id="bakModal"><div class="modal">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <div class="ic"><svg viewBox="0 0 24 24"><path d="M17.5 19a4.5 4.5 0 0 0 .4-8.98 6 6 0 0 0-11.8 1.18A4 4 0 0 0 6.5 19h11z"/><path d="M12 12v5M9.5 14.5L12 17l2.5-2.5"/></svg></div>
  <h2>Backup y sincronización</h2>
  <p id="bakInfo">Cargando…</p>
  <div class="kmsg" id="bakState"></div>
  <button class="primary" id="bakSync" style="margin-bottom:8px"><svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg><span id="bakSyncTxt">Sincronizar ahora</span></button>
  <button class="ghost" id="bakZip" style="width:100%;justify-content:center;padding:11px;margin-bottom:8px"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar respaldo .zip (organizado, legible)</button>
  <button class="ghost" id="bakClone" style="width:100%;justify-content:center;padding:11px;margin-bottom:8px"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Descargar copia exacta (para reimportar)</button>
  <button class="primary" id="bakImport" style="width:100%;justify-content:center;padding:11px"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg><span id="bakImportTxt">Importar copia exacta…</span></button>
  <input type="file" id="bakImportFile" accept=".zip,application/zip" class="hide">
  <div class="bakprog hide" id="bakProg"><div class="bakprogbar"><div class="bakprogfill" id="bakProgFill"></div></div><span class="bakprogtxt" id="bakProgTxt"></span></div>
  <p class="hint" style="margin-top:12px"><b>Respaldo organizado</b>: carpetas legibles (Historial / Mis imágenes / Subproyectos) con prompts y referencias — para revisar a mano. <b>Copia exacta</b>: clon completo de tus datos (incluye prompts e imágenes de referencia) que con <b>Importar</b> restaura todo <b>tal cual</b>. Las claves API no se incluyen.</p>
</div></div>

<div class="overlay hide" id="maskModal"><div class="maskbox">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <div class="masktop">
    <div class="seg" id="edTabs">
      <button class="on" data-tab="mask"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/></svg>Máscara</button>
      <button data-tab="anno"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M5 19L19 5"/><path d="M19 5h-8M19 5v8"/></svg>Anotar</button>
      <button data-tab="pins"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M12 21s-7-5.5-7-11a7 7 0 0 1 14 0c0 5.5-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>Pins</button>
    </div>
    <div class="masktools" id="toolsMask">
      <button class="mtool on" id="mBrush" title="Pincel"><svg viewBox="0 0 24 24"><path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/></svg></button>
      <button class="mtool" id="mErase" title="Borrador"><svg viewBox="0 0 24 24"><path d="M20 20H7L3 16c-.6-.6-.6-1.5 0-2.1L13 4c.6-.6 1.5-.6 2.1 0l5 5c.6.6.6 1.5 0 2.1L11 20"/></svg></button>
      <button class="mtool" id="mRect" title="Rectángulo"><svg viewBox="0 0 24 24"><rect x="4" y="6" width="16" height="12" rx="1"/></svg></button>
      <button class="mtool" id="mLasso" title="Lazo"><svg viewBox="0 0 24 24"><path d="M7 4.5c4-2 10-1.5 11.5 1.5s-1 6.5-5 7.5-9 .5-10-2 .5-5.5 3.5-7z"/><path d="M6 13c-1.5 2-1 5 1 6.5"/><circle cx="8.5" cy="20" r="1.5"/></svg></button>
      <input type="range" id="mSize" min="8" max="160" value="48" title="Tamaño del pincel">
      <button class="mtool" id="mClear" title="Limpiar máscara"><svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></button>
    </div>
    <div class="masktools hide" id="toolsAnno">
      <button class="mtool on" id="aArrow" title="Flecha"><svg viewBox="0 0 24 24"><path d="M5 19L19 5"/><path d="M19 5h-8M19 5v8"/></svg></button>
      <button class="mtool" id="aCircle" title="Círculo"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/></svg></button>
      <button class="mtool" id="aFree" title="Trazo libre"><svg viewBox="0 0 24 24"><path d="M3 16c3-8 6 8 9 0s6-8 9 0"/></svg></button>
      <button class="mtool" id="aText" title="Texto"><svg viewBox="0 0 24 24"><path d="M5 6h14M12 6v13"/></svg></button>
      <button class="mtool" id="aUndo" title="Deshacer"><svg viewBox="0 0 24 24"><path d="M8 5L4 9l4 4"/><path d="M4 9h11a5 5 0 0 1 0 10h-4"/></svg></button>
      <button class="mtool" id="aClear" title="Limpiar anotaciones"><svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></button>
    </div>
    <div class="masktools hide" id="toolsPins">
      <button class="mtool" id="pClear" title="Quitar todos los pins"><svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></button>
    </div>
  </div>
  <div class="edbody">
    <div class="maskarea"><div class="maskstack">
      <img id="maskBase" alt="Imagen a marcar">
      <canvas id="maskDraw"></canvas>
      <canvas id="annoDraw"></canvas>
      <div id="pinLayer"></div>
      <input id="annoText" class="hide" type="text" placeholder="Texto de la anotación…" spellcheck="false">
    </div></div>
    <div class="pinlist hide" id="pinList"></div>
  </div>
  <div class="maskfoot">
    <p class="hint" id="edHint">Pinta o selecciona lo que quieres regenerar. El resto se conserva.</p>
    <div class="acts">
      <button class="ghost" id="mCancel">Cancelar</button>
      <button class="primary" id="mApply">Aplicar</button>
    </div>
  </div>
</div></div>

<div class="overlay hide" id="projModal"><div class="modal projmodal">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <h2>Proyectos</h2>
  <p class="modsub">Cada proyecto guarda su propia memoria, su historial y sus «Mis imágenes». Elige uno para trabajar en él.</p>
  <div class="projgrid" id="projGrid"></div>
  <div class="projnewrow">
    <svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
    <input type="text" id="projNewName" placeholder="Nombre del proyecto nuevo…" spellcheck="false" maxlength="60">
    <button class="primary" id="projCreate"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>Crear</button>
  </div>
</div></div>

<div class="overlay hide" id="vfModal"><div class="modal vfmodal">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <h2>Elegir fotogramas del video</h2>
  <p class="modsub">Mueve la línea de tiempo hasta el momento que quieras y pulsa <b>Capturar</b>. Toma los que necesites; tú decides cuántos. <span id="vfName" class="mono" style="opacity:.8"></span></p>
  <div class="vfstage"><video id="vfVideo" playsinline preload="auto"></video></div>
  <input type="range" id="vfSeek" class="vfseek" min="0" max="1000" value="0" step="1">
  <div class="vfctrls">
    <button id="vfPlay" class="ghost sm" title="Reproducir / pausa (espacio)"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></button>
    <button id="vfStepB" class="ghost sm" title="Fotograma anterior">−1f</button>
    <button id="vfStepF" class="ghost sm" title="Fotograma siguiente">+1f</button>
    <span id="vfTime" class="mono" style="font-size:11px;color:var(--mut)">0:00 / 0:00</span>
    <button class="primary" id="vfCap" style="margin-left:auto"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg>Capturar fotograma</button>
  </div>
  <label style="margin-top:12px">Fotogramas capturados · <span id="vfCount" class="mono">0</span></label>
  <div class="vfshots" id="vfShots"></div>
  <div class="vfactions">
    <button id="vfCancel">Cancelar</button>
    <button id="vfAddRefs" class="vfadd" disabled><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 5v14M5 12h14"/></svg>Añadir a <span id="vfAddRefsTxt">referencias</span></button>
    <button id="vfAddShelf" class="vfadd" disabled><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 5v14M5 12h14"/></svg>Añadir a Mis imágenes</button>
    <button class="primary vfadd" id="vfAddBoth" disabled><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M20 6L9 17l-5-5"/></svg>Añadir a ambos</button>
  </div>
</div></div>

<div class="cmdk hide" id="cmdk"><div class="cmdkbox">
  <input id="cmdkq" placeholder="Buscar un prompt en tu biblioteca…" spellcheck="false" autocomplete="off">
  <div class="cmdklist" id="cmdkList"></div>
  <div class="cmdkhint"><span>↑↓ navegar · ↵ insertar · Esc cerrar</span><a href="/biblioteca" target="_blank" rel="noopener">Abrir biblioteca ↗</a></div>
</div></div>

<div class="top">
  <div class="brand"><span class="dot"><svg viewBox="0 0 24 24"><path d="M12 3l1.9 5.6L19.5 10l-4.6 3.3L16.5 19 12 15.7 7.5 19l1.6-5.7L4.5 10l5.6-1.4z"/></svg></span>Gio Studio</div>
  <div class="seg">
    <button id="mImagen" class="on"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg>Imagen<kbd>1</kbd></button>
    <button id="mAudio"><svg viewBox="0 0 24 24"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg>Audio<kbd>2</kbd></button>
    <button id="mVideo"><svg viewBox="0 0 24 24"><rect x="2" y="5" width="14" height="14" rx="3"/><path d="M16 10l6-3v10l-6-3z"/></svg>Video<kbd>3</kbd></button>
  </div>
  <div class="projbar">
    <button class="projbtn" id="projBtn" title="Proyectos — cada uno con su memoria, historial y Mis imágenes">
      <svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
      <span id="projBtnLbl">General</span>
      <svg class="chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>
    </button>
    <select id="projSel" class="hide"></select>
    <select id="subSel" class="subsel hide" title="Subproyecto activo — lo que generes va aquí"></select>
    <button class="projbtn" id="promptLibBtn" title="Biblioteca de prompts — guarda tus prompts favoritos y los que sirven, por categorías (abre en pestaña aparte)">
      <svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
      <span>Prompt Library</span>
    </button>
  </div>
  <div class="right">
    <button class="ghost" id="trashBtn" title="Papelera — restaurar imágenes borradas"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M10 11v6M14 11v6"/></svg>Papelera</button>
    <button class="ghost" id="bakBtn"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M17.5 19a4.5 4.5 0 0 0 .4-8.98 6 6 0 0 0-11.8 1.18A4 4 0 0 0 6.5 19h11z"/><path d="M12 12v5M9.5 14.5L12 17l2.5-2.5"/></svg>Backup</button>
    <button class="ghost" id="setBtn"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>Ajustes</button>
    <button class="ghost" id="cfgBtn"><span class="kdot" id="kdot"></span>API</button>
  </div>
</div>

<div class="wrap">
  <!-- IZQUIERDA -->
  <div class="col an">
   <div id="imgPanel">
    <div class="seg" id="imgSeg" style="margin-bottom:18px;width:100%">
      <button class="on" id="subCrear" style="flex:1;justify-content:center"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M12 3l1.9 5.6L19.5 10l-4.6 3.3L16.5 19 12 15.7 7.5 19l1.6-5.7L4.5 10l5.6-1.4z"/></svg>GPT 2 · Crear</button>
      <button id="subEditar" style="flex:1;justify-content:center"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 15l5-5 4 4 3-3 6 6"/><circle cx="9" cy="9" r="1.4"/></svg>Editar</button>
    </div>
    <div class="field" id="editBox">
      <label><span id="refLbl">Referencias · opcional</span></label>
      <div class="drop" id="drop"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>Arrastra, pega (⌘V) o elige</div>
      <input type="file" id="files" accept="image/png,image/jpeg,image/webp,image/gif,video/mp4,video/quicktime,video/webm,video/x-matroska,video/x-msvideo" multiple class="hide">
      <div class="thumbs" id="thumbs"></div>
      <div class="thumbs" id="maskThumb"></div>
      <div class="grid2" style="margin-top:9px;gap:7px">
        <div class="drop" id="maskPaint" style="padding:9px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/></svg>Pintar máscara</div>
        <div class="drop" id="dropMask" style="padding:9px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>Subir máscara PNG</div>
      </div>
      <input type="file" id="maskFile" accept="image/png" class="hide">
    </div>

    <div class="field">
      <label id="lblPrompt">Prompt<button class="magic" id="mpImg" title="Mejorar prompt con IA"><svg viewBox="0 0 24 24"><path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5z"/><path d="M19 14l.7 2.3L22 17l-2.3.7L19 20l-.7-2.3L16 17l2.3-.7z"/></svg></button></label>
      <textarea id="prompt" placeholder="Describe lo que imaginas…"></textarea>
      <button class="ghost mpbtn" id="mpImgBtn" title="Reescribe y enriquece tu prompt con IA"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5z"/><path d="M19 14l.7 2.3L22 17l-2.3.7L19 20l-.7-2.3L16 17l2.3-.7z"/></svg>Mejorar con IA</button>
    </div>

    <div class="field">
      <label class="check" style="margin:0"><input type="checkbox" id="ang3dOn"> Ángulo 3D · sujeto y cámara</label>
      <div id="ang3dBox" class="ang3d hide">
        <canvas id="ang3dCv" width="320" height="210" title="Arrastra para girar"></canvas>
        <div class="ang3duse">
          <label class="check"><input type="checkbox" id="ang3dUseSubj" checked> Sujeto (cabeza)</label>
          <label class="check"><input type="checkbox" id="ang3dUseCam" checked> Cámara</label>
        </div>
        <div class="seg ang3dseg" id="ang3dMode"><button data-m="subj" class="on">Sujeto</button><button data-m="cam">Cámara</button></div>
        <div class="seg ang3dseg" id="ang3dShape"><button data-sh="head" class="on">Cabeza</button><button data-sh="cube">Cubo</button><button data-sh="mann">Maniquí</button></div>
        <div class="ang3drow">
          <div class="ang3drow-h"><span id="ang3dYawLbl">Giro</span><span class="ang3dval" id="ang3dYawV">25°</span></div>
          <input type="range" id="ang3dYaw" min="-180" max="180" step="1" value="25">
        </div>
        <div class="ang3drow">
          <div class="ang3drow-h"><span id="ang3dPitchLbl">Inclinación</span><span class="ang3dval" id="ang3dPitchV">0°</span></div>
          <input type="range" id="ang3dPitch" min="-80" max="80" step="1" value="0">
        </div>
        <div class="ang3drow hide" id="ang3dDistRow">
          <div class="ang3drow-h"><span>Distancia</span><span class="ang3dval" id="ang3dDistV">Plano medio</span></div>
          <input type="range" id="ang3dDist" min="0" max="100" step="1" value="45">
        </div>
        <div class="ang3drow hide" id="ang3dLensRow">
          <div class="ang3drow-h"><span>Lente</span><span class="ang3dval" id="ang3dLensV">50 mm · normal</span></div>
          <input type="range" id="ang3dLens" min="14" max="200" step="1" value="50">
        </div>
        <div class="ang3drow hide" id="ang3dApRow">
          <div class="ang3drow-h"><span>Apertura</span><span class="ang3dval" id="ang3dApV">f/4</span></div>
          <input type="range" id="ang3dAp" min="0" max="9" step="1" value="4">
        </div>
        <div class="ang3dtxt" id="ang3dTxt"></div>
        <div class="ang3dpresets" id="ang3dPresets"></div>
        <label class="check ang3dchk"><input type="checkbox" id="ang3dRef"> Adjuntar el diagrama como referencia visual</label>
        <button class="ghost sm" id="ang3dIns">Insertar en el prompt</button>
      </div>
      <p class="hint hide" id="ang3dHint" style="margin-top:6px;font-size:11px">«Sujeto» gira al personaje/objeto; «Cámara» mueve el punto de vista. Se añade como guía al generar (gpt-image-2 lo respeta como sugerencia fuerte, no exacta).</p>
      <button class="ghost" id="poseOpenBtn" style="width:100%;justify-content:center;margin-top:8px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 2 2 7l10 5 10-5z"/><path d="M2 17l10 5 10-5M2 12l10 5 10-5"/></svg>Ángulos por elemento (detectar) <span class="exp2">experimental</span></button>
      <input type="file" id="poseFile" accept="image/png,image/jpeg,image/webp" class="hide">
      <p class="hint" style="margin-top:5px;font-size:11px">Detecta personas/objetos de tu imagen de referencia (o del resultado) y ajusta el ángulo de cada uno por separado.</p>
    </div>

    <div class="field">
      <div class="slabel"><label>Ancho</label><input class="vnum" id="wv" type="number" min="512" max="3840" step="16" value="1920"></div>
      <input type="range" id="w" min="512" max="3840" step="16" value="1920">
      <div class="slabel" style="margin-top:6px"><label>Alto</label><input class="vnum" id="hv" type="number" min="512" max="3840" step="16" value="1088"></div>
      <input type="range" id="h" min="512" max="3840" step="16" value="1088">
      <button type="button" id="lockBtn" class="lockbtn" aria-pressed="false" title="Bloquear proporción: al mover un lado, el otro se ajusta">
        <svg class="lk-open" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/></svg>
        <svg class="lk-closed" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
        <span>Mantener proporción</span></button>
      <input type="checkbox" id="lock" class="hide">
    </div>

    <div class="field">
      <label>Presets · relación de aspecto</label>
      <div class="presets" id="presets">
        <span class="pgroup">Referencia</span>
        <span class="chip" data-refar="1" title="Usa la misma proporción de la imagen de referencia">⤢ Como la referencia</span>
        <span class="pgroup">Nativas · gpt-image-2 (sin reescalado)</span>
        <span class="chip" data-w="1024" data-h="1024">1024² · 1:1</span>
        <span class="chip" data-w="1536" data-h="1024">1536×1024 · 3:2</span>
        <span class="chip" data-w="1024" data-h="1536">1024×1536 · 2:3</span>
        <span class="pgroup">Social</span>
        <span class="chip" data-w="1024" data-h="1024">1:1</span>
        <span class="chip" data-w="1024" data-h="1280">4:5</span>
        <span class="chip" data-w="1152" data-h="2048">9:16</span>
        <span class="chip" data-w="2048" data-h="1152">16:9</span>
        <span class="pgroup">Foto</span>
        <span class="chip" data-w="1024" data-h="1536">2:3</span>
        <span class="chip" data-w="1536" data-h="1024">3:2</span>
        <span class="chip" data-w="1152" data-h="1536">3:4</span>
        <span class="chip" data-w="1536" data-h="1152">4:3</span>
        <span class="chip" data-w="1280" data-h="1024">5:4</span>
        <span class="pgroup">Cine · anamórfico</span>
        <span class="chip" data-w="2016" data-h="1088">1.85:1</span>
        <span class="chip" data-w="2048" data-h="1024">2:1</span>
        <span class="chip" data-w="2560" data-h="1088">2.35:1</span>
        <span class="chip" data-w="2608" data-h="1088">2.39:1</span>
        <span class="chip" data-w="2544" data-h="1088">21:9</span>
        <span class="chip" data-w="2384" data-h="992">2.4:1</span>
        <span class="chip" data-w="3072" data-h="1024">Pano 3:1</span>
        <span class="pgroup">Resolución · escala el ratio actual (área en píxeles)</span>
        <span class="chip rchip" data-px="921600">720p</span>
        <span class="chip rchip" data-px="2073600">1080p</span>
        <span class="chip rchip" data-px="3686400">2K</span>
        <span class="chip rchip" data-px="5760000">3K</span>
        <span class="chip rchip" data-px="8294400">4K</span>
        <span class="chip rchip" data-uhd="1" title="Lado largo al máximo (3840px), topado a 8.29 MP según el aspecto">Ultra HD</span>
        <div class="preslegend"><span class="dotnat"></span> nativo gpt-image-2 (sin reescalado) · <span class="dotok"></span> tamaño válido (lados ÷16) · DCI 4K (4096px) no cabe en el límite de 3840</div>
      </div>
    </div>

    <div class="field grid2">
      <div><label>Calidad</label><select id="quality"><option value="high">High</option><option value="auto" selected>Auto</option><option value="medium">Medium</option><option value="low">Low</option></select></div>
      <div><label>Cantidad</label><select id="n"><option>1</option><option>2</option><option>3</option><option>4</option></select></div>
    </div>

    <details class="adv"><summary><svg viewBox="0 0 24 24" style="width:14px;height:14px"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>Ajustes avanzados<svg class="chev" viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M6 9l6 6 6-6"/></svg></summary>
      <div class="advbody">
        <div style="margin-bottom:12px"><label>Formato</label><select id="fmt"><option value="png">PNG</option><option value="jpeg">JPEG</option><option value="webp">WebP</option></select></div>
        <div><label>Moderación</label><select id="mod"><option value="low" selected>Low</option><option value="auto">Auto</option></select></div>
        <div style="margin-top:12px"><label>Imágenes parciales · preview en vivo</label><select id="partImg"><option value="0" selected>Ninguna</option><option value="1">1</option><option value="2">2</option><option value="3">3</option></select>
        <p class="hint" style="margin-top:6px">Muestra 1–3 vistas previas mientras la imagen se va generando (solo al generar 1 imagen). Cada parcial añade un pequeño costo de tokens.</p></div>
        <div id="compBox" class="hide" style="margin-top:12px"><div class="slabel"><label>Compresión</label><span class="v" id="compv">80%</span></div><input type="range" id="comp" min="0" max="100" step="5" value="80"></div>
        <label class="check" style="margin-top:12px"><input type="checkbox" id="saveDesk" checked> Guardar copia en una carpeta</label>
        <div id="dirBox" style="margin-top:10px">
          <label>Carpeta de guardado</label>
          <div style="display:flex;gap:7px">
            <input type="text" id="saveDir" placeholder="~/Desktop" spellcheck="false">
            <button class="ghost" id="dirPick" style="flex:none" title="Elegir carpeta…"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>Examinar</button>
            <button class="ghost" id="dirApply" style="flex:none">Aplicar</button>
          </div>
          <p class="hint" id="dirMsg" style="margin-top:6px"></p>
        </div>
        <p class="hint">Moderación <b>low</b> es el mínimo de OpenAI; no es "sin censura".</p>
      </div>
    </details>

    <details class="adv"><summary><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M4 6h16M4 12h16M4 18h16"/></svg>Lote de prompts · varios de una vez<svg class="chev" viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M6 9l6 6 6-6"/></svg></summary>
      <div class="advbody">
        <label>Un prompt por línea</label>
        <textarea id="batchTxt" style="min-height:90px;font-size:12.5px" placeholder="logo de cafetería minimalista, taza humeante&#10;banner 21:9 de granos de café sobre madera&#10;patrón seamless de hojas de café"></textarea>
        <div class="estbar" style="margin-top:10px"><span>Costo del lote</span><span class="num" id="batchEst">—</span></div>
        <button class="ghost" id="batchGo" style="width:100%;justify-content:center;margin-top:10px">Generar lote</button>
        <p class="hint">Usa la configuración actual (tamaño, calidad, proyecto, memoria) para cada línea, en fila. Cada prompt se cobra aparte; el estimado es aproximado.</p>
      </div></details>

    <div class="meta"><span class="mono" id="ratio">3:2</span><span class="valid ok" id="valid">válido</span></div>
    <div class="estbar"><span>Costo estimado</span><span class="num" id="estv">aprox. $0.00</span></div>
    <button class="primary" id="go"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg><span id="goTxt">Generar</span></button>
    <p class="hint" id="saveWhere"></p>
    <p class="hint">Lado 512–3840 · múltiplos de 16 · 0.65–8.29 MP · ratio ≤3:1. El estimado es aproximado; el costo real aparece al terminar. <kbd>↵</kbd> genera · <kbd>⇧</kbd><kbd>↵</kbd> salto de línea.</p>
   </div>

   <div id="audioPanel" class="hide">
    <div class="seg" id="audSeg" style="margin-bottom:18px;width:100%">
      <button class="on" id="audTTS" style="flex:1;justify-content:center"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>Voz</button>
      <button id="audSTT" style="flex:1;justify-content:center"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h5"/></svg>Transcribir</button>
      <button id="audSFX" style="flex:1;justify-content:center"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M11 5L6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a9 9 0 0 1 0 14"/></svg>Efectos</button>
      <button id="audMUS" style="flex:1;justify-content:center"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>Música</button>
    </div>

    <div id="ttsBox">
      <div class="field">
        <label>Texto <span class="mono" id="ttsCount" style="float:right;text-transform:none;letter-spacing:0">0 / 4096</span></label>
        <textarea id="ttsText" maxlength="4096" style="min-height:110px" placeholder="Escribe lo que quieres que diga…"></textarea>
      </div>
      <div class="seg" id="provSeg" style="margin-bottom:18px;width:100%">
        <button class="on" id="provOAI" style="flex:1;justify-content:center">OpenAI</button>
        <button id="provEL" style="flex:1;justify-content:center">ElevenLabs</button>
      </div>

      <div id="oaiOpts">
      <div class="field"><label>Modelo</label>
        <select id="ttsModel">
          <option value="gpt-4o-mini-tts" selected>gpt-4o-mini-tts · dirigible con instrucciones</option>
          <option value="tts-1-hd">tts-1-hd · alta fidelidad</option>
          <option value="tts-1">tts-1 · rápido y barato</option>
        </select>
      </div>
      <div class="field"><label>Voz</label><div class="presets" id="voices"></div></div>
      <div class="field" id="instrBox">
        <label>Instrucciones de tono · opcional</label>
        <textarea id="ttsInstr" style="min-height:58px;font-size:12.5px" placeholder="Ej: locutor de radio enérgico · susurro misterioso · narrador de documental, pausado y cálido…"></textarea>
      </div>
      <div class="field"><label>Estilos de voz guardados</label><div class="presets" id="vstyles"></div></div>
      <div class="field" id="speedBox">
        <div class="slabel"><label>Velocidad</label><span class="v mono" id="speedv">1.00×</span></div>
        <input type="range" id="ttsSpeed" min="0.25" max="4" step="0.05" value="1">
      </div>
      <div class="field grid2">
        <div><label>Formato</label><select id="ttsFmt"><option>mp3</option><option>wav</option><option>aac</option><option>flac</option><option>opus</option><option>pcm</option></select></div>
        <div><label>Probar voz</label><button class="ghost" id="voiceTest" style="width:100%;justify-content:center;padding:10px">Vista previa</button></div>
      </div>
      </div>

      <div id="elOpts" class="hide">
        <div id="elConnect" class="hide" style="margin-bottom:18px">
          <label>Clave de ElevenLabs</label>
          <div style="display:flex;gap:7px"><input type="password" id="elKeyIn" placeholder="xi-…" autocomplete="off"><button class="ghost" id="elKeySave" style="flex:none">Conectar</button></div>
          <p class="hint">Se guarda solo en tu equipo (<span class="mono">~/.elevenlabs_key</span>). Consíguela en elevenlabs.io → My Account → API Keys. Hay plan gratis de 10k caracteres/mes.</p>
        </div>
        <div id="elMain" class="hide">
          <div class="field"><label>Voz · incluye tus clonadas <button id="elRefresh" type="button" style="float:right;background:none;border:0;color:var(--faint);cursor:pointer;font-size:9px;letter-spacing:.1em;font-weight:600">REFRESCAR</button></label>
            <select id="elVoice"></select></div>
          <div class="field"><label>Modelo</label>
            <select id="elModel">
              <option value="eleven_multilingual_v2" selected>Multilingual v2 · máxima calidad</option>
              <option value="eleven_v3">Eleven v3 · el más expresivo</option>
              <option value="eleven_turbo_v2_5">Turbo v2.5 · rápido · ½ crédito</option>
              <option value="eleven_flash_v2_5">Flash v2.5 · ultrarrápido · ½ crédito</option>
            </select></div>
          <div class="field"><div class="slabel"><label>Estabilidad</label><span class="v mono" id="elStabV">0.50</span></div>
            <input type="range" id="elStab" min="0" max="1" step="0.05" value="0.5">
            <p class="hint" style="margin-top:4px">Baja = más expresiva y variable · alta = más consistente y plana.</p></div>
          <div class="field"><div class="slabel"><label>Similitud</label><span class="v mono" id="elSimV">0.75</span></div>
            <input type="range" id="elSim" min="0" max="1" step="0.05" value="0.75"></div>
          <div class="field"><div class="slabel"><label>Exageración de estilo</label><span class="v mono" id="elStyV">0.00</span></div>
            <input type="range" id="elSty" min="0" max="1" step="0.05" value="0"></div>
          <div class="field"><div class="slabel"><label>Velocidad</label><span class="v mono" id="elSpdV">1.00×</span></div>
            <input type="range" id="elSpd" min="0.7" max="1.2" step="0.05" value="1"></div>
          <label class="check"><input type="checkbox" id="elBoost" checked> Speaker boost · realza la claridad de la voz</label>
          <div class="field grid2" style="margin-top:12px">
            <div><label>Formato</label><select id="elFmt">
              <option value="mp3_44100_128" selected>MP3 128k</option>
              <option value="mp3_44100_192">MP3 192k · Creator+</option>
              <option value="mp3_22050_32">MP3 32k · ligero</option>
              <option value="opus_48000_128">Opus 48k</option>
              <option value="pcm_44100">PCM 44.1k · crudo</option>
              <option value="ulaw_8000">µ-law 8k · telefonía</option></select></div>
            <div><label>Normalización de texto</label><select id="elNorm"><option value="auto" selected>Auto</option><option value="on">Activada</option><option value="off">Apagada</option></select></div>
          </div>
          <div class="field grid2">
            <div><label>Seed · reproducible</label><input type="text" id="elSeed" class="mono" placeholder="opcional"></div>
            <div><label>Probar voz</label><button class="ghost" id="elTest" style="width:100%;justify-content:center;padding:10px">Vista previa</button></div>
          </div>
          <details class="adv"><summary><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg>Clonar una voz<svg class="chev" viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M6 9l6 6 6-6"/></svg></summary>
            <div class="advbody">
              <label>Nombre de la voz</label><input type="text" id="cloneName" placeholder="Mi voz" style="margin-bottom:10px">
              <div class="drop" id="dropClone" style="padding:10px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>Muestras de audio · ideal 1–3 min limpios</div>
              <input type="file" id="cloneFiles" accept="audio/*" multiple class="hide">
              <p class="hint" id="cloneInfo"></p>
              <button class="ghost" id="cloneGo" style="width:100%;justify-content:center;margin-top:10px">Crear voz clonada</button>
              <p class="hint">Instant Voice Cloning (requiere plan Starter o superior). La voz nueva aparece en la lista al refrescar.</p>
            </div></details>
          <p class="hint" id="elQuota" style="margin:0 0 14px"></p>
        </div>
      </div>

      <div class="estbar"><span>Costo estimado</span><span class="num" id="ttsEst">aprox. $0.0000</span></div>
      <button class="primary" id="ttsGo"><svg viewBox="0 0 24 24"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg><span id="ttsGoTxt">Generar voz</span></button>
      <p class="hint">OpenAI: máx 4096 caracteres; instrucciones de tono solo con <span class="mono">gpt-4o-mini-tts</span>. ElevenLabs cobra en créditos de tu plan. Se guarda en historial y tu carpeta. <kbd>↵</kbd> genera · <kbd>⇧</kbd><kbd>↵</kbd> salto de línea.</p>
    </div>

    <div id="sttBox" class="hide">
      <div class="field">
        <label>Audio a transcribir</label>
        <div class="drop" id="dropAud"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>Arrastra o elige audio · máx 25MB</div>
        <input type="file" id="audFile" accept="audio/*,.mp3,.mp4,.m4a,.wav,.webm,.mpga,.mpeg,.oga,.ogg,.flac" class="hide">
        <p class="hint" id="audInfo"></p>
      </div>
      <div class="field"><label>Modelo</label>
        <select id="sttModel">
          <option value="gpt-4o-transcribe">gpt-4o-transcribe · máxima precisión</option>
          <option value="gpt-4o-mini-transcribe" selected>gpt-4o-mini-transcribe · mitad de precio</option>
          <option value="whisper-1">whisper-1 · SRT, VTT y tiempos</option>
        </select>
      </div>
      <div class="field grid2">
        <div><label>Idioma</label><select id="sttLang"><option value="">Auto</option><option value="es">Español</option><option value="en">Inglés</option><option value="pt">Portugués</option><option value="fr">Francés</option><option value="de">Alemán</option><option value="it">Italiano</option><option value="ja">Japonés</option><option value="ko">Coreano</option><option value="zh">Chino</option></select></div>
        <div><label>Salida</label><select id="sttFmt"><option value="text">Texto</option><option value="srt">SRT · subtítulos</option><option value="vtt">VTT · web</option><option value="verbose_json">JSON + tiempos</option></select></div>
      </div>
      <div class="field"><label>Contexto · opcional</label><input type="text" id="sttPrompt" placeholder="Nombres propios, siglas, jerga esperada…"></div>
      <div class="field">
        <div class="slabel"><label>Temperatura</label><span class="v mono" id="sttTempv">0.0</span></div>
        <input type="range" id="sttTemp" min="0" max="1" step="0.1" value="0">
      </div>
      <label class="check"><input type="checkbox" id="sttTrad"> Traducir al inglés (whisper-1)</label>
      <div class="estbar" style="margin-top:14px"><span>Costo estimado</span><span class="num" id="sttEst">aprox. $0.006/min</span></div>
      <button class="primary" id="sttGo"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h5"/></svg><span id="sttGoTxt">Transcribir</span></button>
      <p class="hint">SRT/VTT y JSON con tiempos usan <span class="mono">whisper-1</span> (se ajusta solo). La traducción siempre sale en inglés. La transcripción se guarda como archivo en historial y tu carpeta.</p>
    </div>

    <div id="sfxBox" class="hide">
      <div class="field"><label>Describe el efecto de sonido</label>
        <textarea id="sfxText" style="min-height:84px" placeholder="Ej: pasos sobre nieve crujiente · explosión lejana con eco · ambiente de bar lleno, vasos y murmullo…"></textarea></div>
      <label class="check"><input type="checkbox" id="sfxAuto" checked> Duración automática</label>
      <div class="field dim" id="sfxDurBox" style="margin-top:12px">
        <div class="slabel"><label>Duración</label><span class="v mono" id="sfxDurV">5.0s</span></div>
        <input type="range" id="sfxDur" min="0.5" max="22" step="0.5" value="5"></div>
      <div class="field"><div class="slabel"><label>Apego al prompt</label><span class="v mono" id="sfxInfV">0.30</span></div>
        <input type="range" id="sfxInf" min="0" max="1" step="0.05" value="0.3">
        <p class="hint" style="margin-top:4px">Bajo = más creativo · alto = literal con tu descripción.</p></div>
      <button class="primary" id="sfxGo" style="margin-top:6px"><svg viewBox="0 0 24 24"><path d="M11 5L6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a9 9 0 0 1 0 14"/></svg><span id="sfxGoTxt">Generar efecto</span></button>
      <p class="hint">Efectos con ElevenLabs (usa la clave conectada en Voz → ElevenLabs). Hasta 22 segundos por efecto.</p>
    </div>

    <div id="musBox" class="hide">
      <div class="field"><label>Modelo</label>
        <select id="musModel">
          <option value="lyria2" selected>Lyria 2 (Google) · instrumental 30s, calidad estudio</option>
          <option value="minimax">MiniMax Music · canciones con letra y voz</option>
        </select></div>
      <div class="field"><label>Describe la música</label>
        <textarea id="musPrompt" style="min-height:84px" placeholder="Ej: bossa nova relajada con guitarra y percusión suave, atardecer en la playa…"></textarea></div>
      <div class="field" id="musLyrBox"><label>Letra · opcional</label>
        <textarea id="musLyrics" style="min-height:84px;font-size:12.5px" placeholder="Versos separados por líneas… (déjalo vacío y MiniMax la escribe)"></textarea>
        <label class="check" style="margin-top:8px"><input type="checkbox" id="musInstr"> Solo instrumental</label></div>
      <div class="field" id="musNegBox"><label>Prompt negativo · opcional</label>
        <input type="text" id="musNeg" placeholder="low quality"></div>
      <div class="field"><label>Seed · opcional</label><input type="text" id="musSeed" class="mono" placeholder="reproducible"></div>
      <button class="primary" id="musGo"><svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg><span id="musGoTxt">Generar música</span></button>
      <p class="hint">Vía fal.ai (la clave de la sección Video). Lyria 2 genera 30s instrumentales en WAV 48kHz; MiniMax hace canciones completas con voz. Tarda 1–3 min.</p>
    </div>
   </div>

   <div id="videoPanel" class="hide">
    <div id="falConnect" class="hide" style="margin-bottom:18px">
      <label>Clave de fal.ai</label>
      <div style="display:flex;gap:7px"><input type="password" id="falKeyIn" placeholder="key-id:secret…" autocomplete="off"><button class="ghost" id="falKeySave" style="flex:none">Conectar</button></div>
      <p class="hint">fal.ai da acceso a Seedance, Kling y OmniHuman con una sola clave (se guarda en <span class="mono">~/.fal_key</span>). Consíguela en fal.ai → Dashboard → Keys; regalan créditos al registrarse.</p>
    </div>
    <div id="vidMain" class="hide">
      <div class="field"><label>Modelo de video</label>
        <select id="vidModelSel">
          <option value="sd" selected>Seedance 2.0 · cine + referencias multimodales</option>
          <option value="kl">Kling 3.0 · multi-toma · Pro / Standard</option>
          <option value="oh">OmniHuman · avatar que habla (imagen + audio)</option>
          <option value="ls">LipSync · re-doblar un video existente</option>
        </select></div>
      <div class="field" id="vidMemRow" style="display:flex;align-items:center;gap:10px">
        <label class="check" style="flex:1;margin:0"><input type="checkbox" id="vidUseMem" checked> Usar memoria del proyecto</label>
        <button class="ghost" id="vidInsStyle" style="flex:none;padding:6px 11px;font-size:11px">Insertar estilo</button>
      </div>

      <div id="sdBox">
        <div class="field"><label>Variante</label><select id="sdTier">
          <option value="seedance" selected>Seedance 2.0 · máxima calidad</option>
          <option value="seedance-fast">Seedance 2.0 Fast · más barato y rápido</option></select></div>
        <div class="field"><label>Prompt<button class="magic" id="mpSd" title="Mejorar prompt con IA"><svg viewBox="0 0 24 24"><path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5z"/><path d="M19 14l.7 2.3L22 17l-2.3.7L19 20l-.7-2.3L16 17l2.3-.7z"/></svg></button></label>
          <textarea id="sdPrompt" style="min-height:96px" placeholder="Describe escena, acción y movimiento de cámara…"></textarea></div>
        <div class="field"><label>Referencias · cada una con su rol <span class="mono" id="sdCount" style="float:right;text-transform:none;letter-spacing:0">0 / 12</span></label>
          <div class="drop" id="dropSdRef" style="padding:13px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg>Arrastra imágenes, videos o audios · también del historial</div>
          <input type="file" id="sdRefFile" accept="image/png,image/jpeg,image/webp,video/mp4,video/webm,video/quicktime,audio/*" multiple class="hide">
          <div id="sdRefList"></div>
          <p class="hint hide" id="sdPrev"></p>
          <p class="hint">Hasta 9 imágenes (personaje, entorno, objeto, estilo, frames) + 3 videos (movimiento, cámara) + 3 audios (música, voz). El bloque de instrucciones se añade solo al prompt.</p></div>
        <div class="field grid2">
          <div><label>Resolución</label><select id="sdRes"><option>480p</option><option selected>720p</option><option>1080p</option></select></div>
          <div><label>Duración</label><select id="sdDur"><option value="auto" selected>Auto</option><option>4</option><option>5</option><option>6</option><option>7</option><option>8</option><option>9</option><option>10</option><option>11</option><option>12</option><option>13</option><option>14</option><option>15</option></select></div></div>
        <div class="field"><label>Aspecto</label>
          <select id="sdAsp"><option value="auto" selected>Auto</option><option>21:9</option><option>16:9</option><option>4:3</option><option>1:1</option><option>3:4</option><option>9:16</option></select></div>
        <label class="check"><input type="checkbox" id="sdGenAud" checked> Audio nativo del video</label>
        <div class="field" style="margin-top:12px"><label>Seed · opcional</label><input type="text" id="sdSeed" class="mono" placeholder="reproducible"></div>
        <p class="hint">Con 2+ imágenes, videos o audios guía usa el modo referencia (máx 12 archivos en total): mantiene personajes, estilo y movimiento entre tomas.</p>
      </div>

      <div id="klBox" class="hide">
        <div class="field"><label>Variante</label><select id="klTier">
          <option value="kling-pro" selected>Kling 3.0 Pro · cinemático</option>
          <option value="kling-std">Kling 3.0 Standard · ~2.6× más barato</option></select></div>
        <div class="field"><label>Prompt<button class="magic" id="mpKl" title="Mejorar prompt con IA"><svg viewBox="0 0 24 24"><path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5z"/><path d="M19 14l.7 2.3L22 17l-2.3.7L19 20l-.7-2.3L16 17l2.3-.7z"/></svg></button></label>
          <textarea id="klPrompt" style="min-height:84px" placeholder="Describe la escena y la acción…"></textarea></div>
        <details class="adv"><summary><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M4 6h16M4 12h16M4 18h10"/></svg>Multi-toma · varias escenas en un video<svg class="chev" viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M6 9l6 6 6-6"/></svg></summary>
          <div class="advbody">
            <label>Una toma por línea · formato "texto | segundos"</label>
            <textarea id="klMulti" style="min-height:74px;font-size:12.5px" placeholder="un dron sobrevuela la costa al amanecer | 4&#10;primer plano de la ola rompiendo | 3"></textarea>
            <label style="margin-top:10px">Estructura de tomas</label>
            <select id="klShot"><option value="customize" selected>Customize · respeta mis tomas</option><option value="intelligent">Intelligent · el modelo decide los cortes</option></select>
            <p class="hint">Si escribes tomas aquí, sustituyen al prompt único.</p>
          </div></details>
        <div class="field grid2" style="margin-top:14px">
          <div><label>Imagen inicial · opcional</label>
            <div class="drop" id="dropKlImg" style="padding:9px;font-size:11px">Frame inicial</div>
            <input type="file" id="klImgFile" accept="image/png,image/jpeg,image/webp" class="hide">
            <div class="thumbs" id="klImgThumb"></div></div>
          <div><label>Imagen final · opcional</label>
            <div class="drop" id="dropKlEnd" style="padding:9px;font-size:11px">Frame final</div>
            <input type="file" id="klEndFile" accept="image/png,image/jpeg,image/webp" class="hide">
            <div class="thumbs" id="klEndThumb"></div></div></div>
        <div class="field grid2">
          <div><label>Duración</label><select id="klDur"><option>3</option><option>4</option><option selected>5</option><option>6</option><option>7</option><option>8</option><option>9</option><option>10</option><option>11</option><option>12</option><option>13</option><option>14</option><option>15</option></select></div>
          <div><label>Aspecto</label><select id="klAsp"><option selected>16:9</option><option>9:16</option><option>1:1</option></select></div></div>
        <label class="check"><input type="checkbox" id="klGenAud" checked> Audio nativo (español/inglés)</label>
        <div class="field" style="margin-top:12px"><label>Prompt negativo</label>
          <input type="text" id="klNeg" placeholder="blur, distort, and low quality"></div>
        <div class="field"><div class="slabel"><label>Fidelidad al prompt (CFG)</label><span class="v mono" id="klCfgV">0.50</span></div>
          <input type="range" id="klCfg" min="0" max="1" step="0.05" value="0.5"></div>
      </div>

      <div id="ohBox" class="hide">
        <div class="field"><label>Versión</label><select id="ohVer">
          <option value="omnihuman" selected>OmniHuman 1.5 · prompt, turbo y resolución</option>
          <option value="omnihuman-v1">OmniHuman 1.0 · clásico</option></select></div>
        <div class="field"><label>Imagen de la persona · requerida</label>
          <div class="drop" id="dropOhImg" style="padding:10px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><circle cx="12" cy="8" r="4"/><path d="M4 21v-1a8 8 0 0 1 16 0v1"/></svg>Arrastra o elige la foto</div>
          <input type="file" id="ohImgFile" accept="image/png,image/jpeg,image/webp" class="hide">
          <div class="thumbs" id="ohImgThumb"></div></div>
        <div class="field"><label>Audio que hablará · requerido</label>
          <select id="ohAudSel" style="margin-bottom:8px"><option value="">— elegir del historial de audio —</option></select>
          <div class="drop" id="dropOhAud" style="padding:10px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>…o sube un audio</div>
          <input type="file" id="ohAudFile" accept="audio/*" class="hide">
          <p class="hint" id="ohAudInfo"></p></div>
        <div class="field" id="ohPromptBox"><label>Indicaciones · opcional</label>
          <textarea id="ohPrompt" style="min-height:58px;font-size:12.5px" placeholder="gestos, emoción, encuadre…"></textarea></div>
        <div class="field grid2" id="ohResRow">
          <div><label>Resolución</label><select id="ohRes"><option>720p</option><option selected>1080p</option></select></div>
          <div style="display:flex;align-items:flex-end;padding-bottom:4px"><label class="check"><input type="checkbox" id="ohTurbo"> Turbo · más rápido</label></div></div>
        <p class="hint">Audio ≤30s en 1080p · ≤60s en 720p. Cobra $0.14 por segundo de video.</p>
      </div>

      <div id="lsBox" class="hide">
        <div class="field"><label>Video a sincronizar · requerido</label>
          <select id="lsVidSel" style="margin-bottom:8px"><option value="">— elegir del historial de video —</option></select>
          <div class="drop" id="dropLsVid" style="padding:10px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><rect x="2" y="5" width="14" height="14" rx="3"/><path d="M16 10l6-3v10l-6-3z"/></svg>…o sube un video MP4</div>
          <input type="file" id="lsVidFile" accept="video/mp4,video/webm,video/quicktime" class="hide">
          <p class="hint" id="lsVidInfo"></p></div>
        <div class="field"><label>Audio nuevo · requerido</label>
          <select id="lsAudSel" style="margin-bottom:8px"><option value="">— elegir del historial de audio —</option></select>
          <div class="drop" id="dropLsAud" style="padding:10px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>…o sube un audio</div>
          <input type="file" id="lsAudFile" accept="audio/*" class="hide">
          <p class="hint" id="lsAudInfo"></p></div>
        <div class="field grid2">
          <div><label>Si el audio es más largo</label><select id="lsLoop"><option value="">Cortar</option><option value="loop">Repetir video</option><option value="pingpong">Ida y vuelta</option></select></div>
          <div><label>Seed · opcional</label><input type="text" id="lsSeed" class="mono" placeholder="reproducible"></div></div>
        <div class="field"><div class="slabel"><label>Guidance</label><span class="v mono" id="lsGuidV">1.0</span></div>
          <input type="range" id="lsGuid" min="0.5" max="3" step="0.1" value="1"></div>
        <p class="hint">LatentSync mueve los labios del video para que digan el audio nuevo. Combo: genera una voz en Audio y aplícala aquí.</p>
      </div>

      <div class="estbar"><span>Costo estimado</span><span class="num" id="vidEst">aprox. $—</span></div>
      <button class="primary" id="vidGo"><svg viewBox="0 0 24 24"><rect x="2" y="5" width="14" height="14" rx="3"/><path d="M16 10l6-3v10l-6-3z"/></svg><span id="vidGoTxt">Generar video</span></button>
      <p class="hint">El video se genera en la nube de fal y tarda 1–5 min; puedes seguir usando la app mientras. Se guarda en historial y tu carpeta. <kbd>↵</kbd> genera · <kbd>⇧</kbd><kbd>↵</kbd> salto de línea.</p>
    </div>
   </div>
   <div class="sess sessfoot" id="sessTot"><span>Sesión</span> <b class="mono" id="sessCostV">aprox. $0.0000</b> · <b class="mono" id="sessNV">0</b> <span>gen</span></div>
  </div>

  <!-- CENTRO -->
  <div class="col mid an">
   <div id="imgStage" style="display:flex;flex-direction:column;flex:none">
    <div class="canvas" id="canvas">
      <div class="genchip hide" id="genChip"><span class="gcdot"></span><span id="genChipTxt">Generando…</span></div>
      <div class="empty" id="emptyState"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg><div>Tu imagen aparecerá aquí</div><div class="kbdhint"><kbd>⌘</kbd><kbd>↵</kbd> generar · <kbd>1</kbd> Imagen <kbd>2</kbd> Audio <kbd>3</kbd> Video · <kbd>⌘</kbd><kbd>V</kbd> pegar · puedes lanzar varias a la vez</div></div>
      <div class="spin hide" id="spinner"></div>
      <img class="result hide" id="resultImg" alt="Resultado" draggable="true">
      <div class="floaters hide" id="floaters">
        <button class="fbtn" id="fCopy" title="Copiar prompt + referencias usadas"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
        <button class="fbtn" id="fAdd" title="Usar como referencia"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button>
        <button class="fbtn" id="fIter" title="Iterar: editar este resultado con un cambio"><svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg></button>
        <a class="fbtn" id="fDl" title="Descargar" download><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg></a>
      </div>
    </div>
    <div class="strip hide" id="strip"></div>
    <div class="resbar hide" id="resbar">
      <span class="costtag" id="cost"></span>
      <div class="acts"><a id="dl" download="imagen.png"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar</a>
      <button id="again"><svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>Otra</button></div>
    </div>
    <div class="shelf" id="shelf">
      <div class="shelfhead">
        <span class="shelftitle"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg>Mis imágenes <span class="shelfsub">· siempre a la mano, en tu equipo</span></span>
        <div style="display:flex;gap:7px;flex:none;align-items:center">
        <span class="cfilt" id="shelfColFilt" title="Filtrar por color"><button class="cfdot r" data-col="r" title="Rojo"></button><button class="cfdot y" data-col="y" title="Amarillo"></button><button class="cfdot g" data-col="g" title="Verde"></button><button class="cfdot b" data-col="b" title="Azul"></button></span>
        <button class="ghost sm" id="shelfSelBtn" title="Seleccionar varias (arrastra un recuadro o haz clic)"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>Seleccionar</button>
        <button class="ghost sm" id="shelfAll" title="Ver todas en una ventana"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>Ver todo</button>
        <button class="ghost sm" id="shelfAddBtn"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 5v14M5 12h14"/></svg>Cargar</button>
        </div>
      </div>
      <div class="shelffolder">
        <span>Carpeta: <b class="mono" id="shelfDirLbl">…</b></span>
        <button class="linklike" id="shelfDirEdit">cambiar</button>
        <span id="shelfDirRow" class="hide"><input type="text" id="shelfDirIn" placeholder="~/Pictures/MiEstante" spellcheck="false"><button class="ghost sm" id="shelfDirSave">Guardar</button></span>
      </div>
      <input type="file" id="shelfFile" accept="image/png,image/jpeg,image/webp,image/gif,video/mp4,video/quicktime,video/webm,video/x-matroska,video/x-msvideo" multiple hidden>
      <div class="subchips" id="shelfSubChips"></div>
      <div class="shelfgrid" id="shelfGrid"></div>
      <div class="galbulk hide" id="shelfBulk"></div>
      <div class="shelfempty" id="shelfEmpty">Arrastra imágenes aquí o pulsa «Cargar». Se guardan en tu equipo, no en OpenAI. Pasa el cursor sobre una para usarla como referencia, describirla, descargarla o quitarla.</div>
    </div>
   </div>

   <div id="videoStage" class="hide">
    <div class="audcard" id="vidEmpty" style="min-height:320px;align-items:center;justify-content:center">
      <div class="empty"><svg viewBox="0 0 24 24"><rect x="2" y="5" width="14" height="14" rx="3"/><path d="M16 10l6-3v10l-6-3z"/></svg><div>Tu video aparecerá aquí</div><div class="kbdhint"><kbd>⌘</kbd><kbd>↵</kbd> generar · 1–5 min por video</div></div>
    </div>
    <div class="audcard hide" id="vidProgress" style="min-height:320px;align-items:center;justify-content:center">
      <div class="empty"><div class="spin"></div><div id="vidProgTxt">Generando video…</div><div class="hint" id="vidProgSub"></div></div>
    </div>
    <div class="audcard hide" id="vidResult">
      <div class="audhead"><span id="vidTitle"></span><span class="costtag" id="vidCost"></span></div>
      <video id="vidPlayer" controls style="width:100%;max-height:60vh;border-radius:10px;background:#000"></video>
      <div class="resbar" style="margin-top:0"><div class="acts"><a id="vidDl" download><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar</a></div></div>
    </div>
   </div>

   <div id="audioStage" class="hide">
    <div class="audcard" id="audEmpty" style="min-height:320px;align-items:center;justify-content:center">
      <div class="empty"><svg viewBox="0 0 24 24"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg><div>Tu audio aparecerá aquí</div><div class="kbdhint"><kbd>⌘</kbd><kbd>↵</kbd> generar · arrastra un audio para transcribirlo</div></div>
    </div>
    <div class="audcard hide" id="audResult">
      <div class="audhead"><span id="audTitle"></span><span class="costtag" id="audCost"></span></div>
      <audio id="audPlayer" controls></audio>
      <div class="resbar" style="margin-top:0"><div class="acts"><a id="audDl" download><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar</a></div></div>
    </div>
    <div class="audcard hide" id="txResult">
      <div class="audhead"><span>Transcripción</span><span class="costtag" id="txCost"></span></div>
      <textarea id="txText" readonly style="min-height:240px"></textarea>
      <div class="resbar" style="margin-top:0"><div class="acts">
        <button id="txCopy"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copiar</button>
        <a id="txDl" download><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar</a></div></div>
    </div>
   </div>
  </div>

  <!-- DERECHA -->
  <div class="col an">
    <div class="sec">
      <h3 class="eyebrow"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Memoria <span class="mono" id="memProjLbl" style="margin-left:auto;font-weight:400;text-transform:none;letter-spacing:0;color:var(--mut)"></span></h3>
      <div class="btnrow">
        <button id="distill"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M12 3l1.9 5.6L19.5 10l-4.6 3.3L16.5 19 12 15.7 7.5 19l1.6-5.7L4.5 10l5.6-1.4z"/></svg>Destilar</button>
        <button id="delProj" title="Borrar proyecto (doble clic)"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>Borrar</button>
      </div>
      <div class="seg" id="styleSeg" style="margin:14px 0 8px;padding:2px;width:100%">
        <button class="on" data-st="img" style="flex:1;justify-content:center;padding:5px 0;font-size:11px">estilo.md</button>
        <button data-st="vid" style="flex:1;justify-content:center;padding:5px 0;font-size:11px">estilo-video.md</button>
      </div>
      <textarea id="style" placeholder="Estilo: técnica, paleta, luz, mood…"></textarea>
      <div class="btnrow"><button id="saveProj"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>Guardar estilo</button></div>
      <label style="margin-top:14px">Memoria visual · referencias</label>
      <div class="drop" id="dropPref" style="padding:10px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 5v14M5 12h14"/></svg>Añadir referencia</div>
      <input type="file" id="prefFile" accept="image/png,image/jpeg,image/webp,video/mp4,video/quicktime,video/webm,video/x-matroska,video/x-msvideo" multiple class="hide">
      <div class="thumbs" id="prefThumbs"></div>
      <label class="check" style="margin-top:10px"><input type="checkbox" id="useVis" checked> Usar memoria visual al generar</label>
      <p class="hint">Con esto activo, estas imágenes se adjuntan solas como referencia en cada generación del proyecto (Crear y Editar), para mantener el mismo estilo sin re-subirlas. El estilo se guarda como <span class="mono">estilo.md</span> en la carpeta del proyecto y se antepone siempre al prompt.</p>
    </div>
    <div class="sec hide" id="vidSec">
      <h3 class="eyebrow"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><rect x="2" y="5" width="14" height="14" rx="3"/><path d="M16 10l6-3v10l-6-3z"/></svg>Video</h3>
      <div id="vidList"></div>
    </div>
    <div class="sec hide" id="audSec">
      <h3 class="eyebrow"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>Audio</h3>
      <div id="audList"></div>
    </div>
    <div class="sec">
      <h3 class="eyebrow galeye"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l3 2"/></svg>Historial<span class="mono" id="galCount" style="font-weight:400"></span><span class="galactions"><button class="chip" id="galFavBtn" title="Ver solo favoritas (★)">★</button><span class="cfilt" id="galColFilt" title="Filtrar por color"><button class="cfdot r" data-col="r" title="Rojo"></button><button class="cfdot y" data-col="y" title="Amarillo"></button><button class="cfdot g" data-col="g" title="Verde"></button><button class="cfdot b" data-col="b" title="Azul"></button></span><select id="galSort" class="ghost sm galsort" title="Organizar las imágenes del historial"><option value="">Organizar ▾</option><option value="new">Fecha de creación · recientes primero</option><option value="old">Fecha de creación · antiguas primero</option><option value="name">Nombre del prompt (A→Z)</option></select><button class="ghost sm" id="galSelBtn" title="Seleccionar varias" style="text-transform:none;white-space:nowrap;flex:none"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>Seleccionar</button><button class="ghost sm" id="galAll" title="Ver todas en una ventana" style="text-transform:none;white-space:nowrap;flex:none"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>Ver todo</button></span></h3>
      <div class="subchips" id="galSubChips"></div>
      <input type="text" id="galSearch" placeholder="Buscar en prompts…" spellcheck="false">
      <div class="shelffolder" title="Carpeta externa donde se copian las imágenes generadas de este proyecto (además del historial interno)"><span>Carpeta: <b class="mono" id="histDirLbl">…</b></span><button class="linklike" id="histDirEdit">cambiar</button></div>
      <div class="galbulk hide" id="galBulk"></div>
      <div class="gal" id="gal"></div>
      <button class="more hide" id="galMore"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M6 9l6 6 6-6"/></svg>Ver más</button>
    </div>
  </div>
</div>

<div class="lightbox hide" id="cmpModal">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <div class="cmpwrap" id="cmpWrap">
    <img id="cmpA" alt="A"><div id="cmpBwrap"><img id="cmpB" alt="B"></div>
    <div class="cmptag" style="left:12px">A</div><div class="cmptag" style="right:12px">B</div>
    <div id="cmpLine"></div>
  </div>
  <input type="range" id="cmpSlider" min="0" max="100" value="50">
</div>

<div class="overlay hide" id="trashModal"><div class="modal trashmodal">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
  <h2>Papelera</h2>
  <p class="modsub">Imágenes que borraste (del historial y de Mis imágenes). Se vacía sola a los 14 días. Restáuralas a su sitio o bórralas para siempre.</p>
  <div class="trashbar"><span id="trashCount" class="mut"></span><button class="ghost sm bdel" id="trashEmpty">Vaciar papelera</button></div>
  <div class="trashgrid" id="trashGrid"></div>
</div></div>

<div class="lightbox hide" id="lightbox">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <button class="lbnav prev" id="lbPrev" title="Anterior (←)"><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg></button>
  <button class="lbnav next" id="lbNext" title="Siguiente (→)"><svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button>
  <img id="lbImg" src="" alt="Vista completa">
  <div class="lbbar" id="lbBar">
    <span class="lbprompt" id="lbPrompt"></span>
    <div class="lbrefs hide" id="lbRefs"></div>
    <div class="lbmeta hide" id="lbMeta"></div>
    <div class="lbbtns">
    <button id="lbUse"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Usar prompt</button>
    <button id="lbLib"><svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>A la biblioteca</button>
    <button id="lbDesc"><svg viewBox="0 0 24 24"><path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5z"/><path d="M19 14l.7 2.3L22 17l-2.3.7L19 20l-.7-2.3L16 17l2.3-.7z"/></svg>Describir</button>
    <button id="lbPose"><svg viewBox="0 0 24 24"><path d="M12 2 2 7l10 5 10-5z"/><path d="M2 17l10 5 10-5M2 12l10 5 10-5"/></svg>Ángulos 3D</button>
    <button id="lbFull" title="Ver la imagen en toda la pantalla (Esc para salir)"><svg viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg>Pantalla completa</button>
    <button id="lbShare" title="Compartir · WhatsApp, Telegram, redes…"><svg viewBox="0 0 24 24"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg>Compartir</button>
    <a id="lbDl" download><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar</a>
    </div>
  </div>
</div>
<div class="overlay hide" id="poseModal"><div class="modal posemodal">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <h2>Ángulos 3D <span class="exp">experimental</span></h2>
  <p class="modsub" style="color:var(--mut);font-size:12.5px;margin:-4px 0 12px">Detecta los elementos, gira su cubo al ángulo que quieras y genera. gpt-image-2 lo toma como guía fuerte (no exacta); funciona mejor con giros moderados.</p>
  <div class="posewrap">
    <div class="posestage" id="poseStage"><img id="poseImg" alt=""><div class="poseov" id="poseOv"></div><div class="posebusy hide" id="poseBusy"><div class="spin"></div></div></div>
    <div class="poseside">
      <div class="posecam">
        <canvas id="poseCamCv" width="120" height="108" title="Arrastra para orbitar la cámara"></canvas>
        <div class="posecaminfo">
          <div class="posecamlbl">Ángulo de cámara (toda la toma)</div>
          <div class="posecamtxt" id="poseCamTxt"></div>
          <div class="ppre" id="poseCamPre"><button data-y="0" data-p="0">Frontal</button><button data-y="0" data-p="35">Picado</button><button data-y="0" data-p="-28">Contrapicado</button><button data-y="0" data-p="60">Cenital</button></div>
        </div>
      </div>
      <button class="primary" id="poseDetect"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>Detectar personas / objetos</button>
      <div class="poselist" id="poseList"></div>
      <div class="posefoot">
        <button class="primary" id="poseGen" disabled><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M5 12h14M13 6l6 6-6 6"/></svg>Generar con estos ángulos</button>
        <button id="poseCancel">Cerrar</button>
      </div>
    </div>
  </div>
</div></div>
<script>
const $=id=>document.getElementById(id);
let mode='crear',refs=[],mask=null,sessCost=0,sessN=0,ratio=1920/1088,projects={};
const REF_IMG_TOKENS=500; // respaldo si aún no conocemos las dimensiones; el real lo da la API
// tokens de imagen de entrada ≈ parches de 32px (esquema de gpt-image), tope 1536
function refTokens(w,h){if(!w||!h)return REF_IMG_TOKENS;return Math.min(1536,Math.ceil(w/32)*Math.ceil(h/32));}
// rellena r.tok midiendo cada referencia que aún no se haya medido; revalida al terminar
function ensureRefTokens(){refs.filter(r=>r.tok===undefined).forEach(r=>{r.tok=null;
 const im=new Image();im.onload=()=>{r.tok=refTokens(im.naturalWidth,im.naturalHeight);validate();};
 im.onerror=()=>{r.tok=REF_IMG_TOKENS;validate();};im.src='data:image/png;base64,'+r.b64;});}
let results=[],active=0,lastResult=null,activeJobs=0,genTimer=null,genT0=0;
let hist=[],shown=30,cmpA=null;
function openCmp(fa,fb){
 $('cmpA').src='/file?name='+encodeURIComponent(fa);
 $('cmpB').src='/file?name='+encodeURIComponent(fb);
 $('cmpModal').classList.remove('hide');
 $('cmpA').onload=()=>{const w=$('cmpA').getBoundingClientRect().width;
  $('cmpB').style.width=w+'px';$('cmpSlider').value=50;cmpUpdate()}}
function cmpUpdate(){const v=+$('cmpSlider').value;
 $('cmpBwrap').style.clipPath='inset(0 0 0 '+v+'%)';
 $('cmpLine').style.left=v+'%'}

function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function toast(msg,kind){try{msg=trVal(String(msg).trim(),LANG)}catch(e){}const t=document.createElement('div');t.className='toast'+(kind==='bad'?' bad':'');
 t.textContent=msg;$('toasts').appendChild(t);
 setTimeout(()=>{t.style.opacity='0';t.style.transform='translateY(-6px)';setTimeout(()=>t.remove(),260)},2600)}

async function checkKey(){const r=await(await fetch('/keystatus')).json();$('kdot').classList.toggle('on',r.ok);
 if(r.data_ok===false){toast('⚠ El servicio no puede leer tus datos en iCloud: dale "Acceso total al disco" a Python (ver panel Backup)','bad');
  setTimeout(()=>toast('Tus datos están intactos; es solo un permiso de macOS','bad'),3200)}
 if(!r.ok)$('keyModal').classList.remove('hide');return r.ok}
// X de cierre en todas las ventanas flotantes + clic fuera para los modales
document.querySelectorAll('.mclose').forEach(b=>b.onclick=e=>{e.stopPropagation();
 b.closest('.overlay,.lightbox').classList.add('hide')});
['keyModal','bakModal'].forEach(id=>$(id).addEventListener('click',e=>{
 if(e.target===$(id))$(id).classList.add('hide')}));
$('cfgBtn').onclick=()=>$('keyModal').classList.remove('hide');
$('bakBtn').onclick=async()=>{$('bakModal').classList.remove('hide');
 const s=await(await fetch('/backupstatus')).json();
 $('bakInfo').textContent='Tus datos ('+s.size+' · '+s.files+' archivos) viven en '+s.path+' y sobreviven apagados y reinicios.';
 $('bakState').innerHTML=s.git
  ?'<span style="color:var(--ok)">●</span> Sincronización por git activa · pulsa "Sincronizar ahora" en cada equipo'
  :'<span style="color:var(--faint)">●</span> Sin sincronización configurada (usa el respaldo .zip)';
 $('bakSync').classList.toggle('hide',!s.git)};
function fmtMB(b){return (b/1048576).toFixed(b<10485760?1:0)+' MB';}
async function streamDownload(url,name,prepLabel){
 const prog=$('bakProg'),fill=$('bakProgFill'),txt=$('bakProgTxt');
 let writable=null;
 // dejar elegir DÓNDE guardar (si el navegador lo soporta); si no, descarga normal
 if(window.showSaveFilePicker){
  try{const h=await showSaveFilePicker({suggestedName:name,types:[{description:'Archivo .zip',accept:{'application/zip':['.zip']}}]});writable=await h.createWritable();}
  catch(e){if(e&&e.name==='AbortError')return; /* el usuario canceló */ writable=null;}
 }
 prog.classList.remove('hide');fill.classList.add('prep');fill.style.width='0%';
 // PASO 1/2 · Preparando: el servidor arma el zip; la barra llena la PRIMERA mitad (asíntota hacia 48%)
 const t0=performance.now();let shown=0;txt.textContent='Paso 1/2 · '+(prepLabel||'Preparando copia…');
 let prepTimer=setInterval(()=>{shown+=(48-shown)*0.06;fill.style.width=shown.toFixed(1)+'%';txt.textContent='Paso 1/2 · '+(prepLabel||'Preparando copia…')+' ('+Math.round((performance.now()-t0)/1000)+'s)';},180);
 try{
  const resp=await fetch(url);if(!resp.ok)throw new Error('HTTP '+resp.status);
  clearInterval(prepTimer);prepTimer=null;
  // PASO 2/2 · Descargando: barra de acento llenando la SEGUNDA mitad (50%→100%)
  fill.classList.remove('prep');shown=50;fill.style.width='50%';txt.textContent='Paso 2/2 · Descargando…';
  const total=+(resp.headers.get('Content-Length')||0),reader=resp.body.getReader(),chunks=[];let received=0;
  for(;;){const {done,value}=await reader.read();if(done)break;received+=value.length;
   if(writable)await writable.write(value);else chunks.push(value);
   if(total){const dl=received/total;shown=Math.max(shown,50+dl*50);fill.style.width=shown.toFixed(1)+'%';txt.textContent='Paso 2/2 · Descargando · '+Math.round(dl*100)+'% · '+fmtMB(received)+' / '+fmtMB(total);}
   else{txt.textContent='Paso 2/2 · Descargando… '+fmtMB(received);}}
  if(writable){await writable.close();}
  else{const blob=new Blob(chunks,{type:'application/zip'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),2000);}
  fill.style.width='100%';txt.textContent='Listo ✓ · '+fmtMB(received);toast('Copia guardada ✓');
  setTimeout(()=>prog.classList.add('hide'),3000);
 }catch(e){if(prepTimer)clearInterval(prepTimer);fill.classList.remove('prep');txt.textContent='Error: '+(e&&e.message||e);toast('No se pudo descargar: '+(e&&e.message||e),'bad');}
}
$('bakZip').onclick=()=>streamDownload('/backup.zip','studio-backup.zip','Preparando respaldo organizado…');
$('bakClone').onclick=()=>streamDownload('/clone.zip','gio-studio-copia-exacta.zip','Preparando copia exacta…');
$('bakImport').onclick=()=>$('bakImportFile').click();
$('bakImportFile').onchange=async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;
 if(!confirm('Importar «'+f.name+'» restaurará tus datos a como están en esa copia (se sobrescriben los archivos que coincidan). ¿Continuar?'))return;
 const btn=$('bakImport'),txt=$('bakImportTxt');btn.disabled=true;txt.textContent='Importando…';
 try{const r=await(await fetch('/import',{method:'POST',headers:{'Content-Type':'application/zip'},body:f})).json();
  if(r.error){toast(r.error,'bad');}
  else{toast(r.restored+' archivo(s) restaurados ✓ · recargando…');
   await loadProjects();renderGalChips();renderShelfChips();await loadGal();await loadShelf();
   setTimeout(()=>location.reload(),900);}
 }catch(x){toast('No se pudo importar: '+String(x&&x.message||x),'bad');}
 btn.disabled=false;txt.textContent='Importar copia exacta…';};
$('bakSync').onclick=async()=>{$('bakSync').disabled=true;$('bakSyncTxt').textContent='Sincronizando…';
 const r=await(await fetch('/datasync',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
 $('bakSync').disabled=false;$('bakSyncTxt').textContent='Sincronizar ahora';
 if(r.error){toast(r.error,'bad');return}
 toast('Datos sincronizados con la nube');loadGal()};
$('keySave').onclick=async()=>{const k=$('keyInput').value.trim();if(!k)return;$('keyMsg').textContent='Validando…';
 const r=await(await fetch('/setkey',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})})).json();
 if(r.ok){$('keyMsg').textContent='Conectada ✓';$('keyModal').classList.add('hide');$('kdot').classList.add('on');toast('API conectada')}
 else{$('keyMsg').textContent=(r.error||'clave inválida')}};

function bumpSess(c,n=1){sessCost+=c||0;sessN+=n;
 $('sessCostV').textContent='aprox. $'+sessCost.toFixed(4);$('sessNV').textContent=sessN}
let lastImgMode=localStorage.getItem('studio_imgmode')||'crear';
function setMode(m){mode=m;
 const aud=m==='audio',vid=m==='video',img=!aud&&!vid;
 $('mImagen').classList.toggle('on',img);
 $('mAudio').classList.toggle('on',aud);$('mVideo').classList.toggle('on',vid);
 $('imgPanel').classList.toggle('hide',!img);$('imgStage').classList.toggle('hide',!img);
 $('audioPanel').classList.toggle('hide',!aud);$('audioStage').classList.toggle('hide',!aud);
 $('videoPanel').classList.toggle('hide',!vid);$('videoStage').classList.toggle('hide',!vid);
 if(vid&&!falReady)falInit();
 if(img){lastImgMode=m;localStorage.setItem('studio_imgmode',m);
  $('subCrear').classList.toggle('on',m==='crear');$('subEditar').classList.toggle('on',m==='editar');
  $('lblPrompt').textContent=m==='editar'?'Instrucción de edición':'Prompt';
  $('refLbl').textContent=m==='editar'?'Imágenes a editar / combinar':'Referencias · opcional';
  $('goTxt').textContent=m==='editar'?'Editar':'Generar'}}
$('mImagen').onclick=()=>setMode(lastImgMode);
$('subCrear').onclick=()=>setMode('crear');$('subEditar').onclick=()=>setMode('editar');
$('mAudio').onclick=()=>setMode('audio');$('mVideo').onclick=()=>setMode('video');

function gcd(a,b){return b?gcd(b,a%b):a}function fr(a,b){const g=gcd(a,b);return(a/g)+':'+(b/g)}
function snap(v){return Math.round(v/16)*16}
function estTokens(){const W=+$('w').value,H=+$('h').value,MP=W*H/1e6,q=$('quality').value;
 // 'auto' factura como 'medium' (a veces sube a 'high'); validado con tokens reales de OpenAI
 let t;if(q==='low')t=129+64*MP;else if(q==='medium'||q==='auto')t=1150+577*MP;else t=4600+2308*MP;
 // gpt-image-2 cobra MENOS los no-cuadrados; factor lado corto/largo calibrado con la tabla oficial
 // (cuadrado → ×1 sin cambio; 1024×1536 high → $0.165, exacto)
 t*=Math.min(W,H)/Math.max(W,H);
 return Math.max(80,Math.round(t))}
function validate(){const W=+$('w').value,H=+$('h').value,long=Math.max(W,H),mp=W*H,rls=long/Math.min(W,H);let ok=true,msg='válido';
 // límites de gpt-image-2: lado ≤3840, 0.65–8.29 MP, ratio largo:corto ≤3:1
 if(long>3840){ok=false;msg='lado > 3840'}
 else if(mp>8294400){ok=false;msg='> 8.29 MP (máx)'}
 else if(mp<655360){ok=false;msg='< 0.65 MP (mín)'}
 else if(rls>3.0001){ok=false;msg='ratio > 3:1'}
 else if(mp>3686400){msg='válido · >2K experimental'}   // OpenAI marca >2K como experimental
 $('valid').textContent=msg;$('valid').className='valid '+(ok?'ok':'bad');$('ratio').textContent=fr(W,H);
 const n=+$('n').value;let est=estTokens()*n*30/1e6;
 // referencias = tokens de imagen de entrada (~$8/1M). Aproximado; el costo real al terminar es exacto.
 if(mode==='editar'){ensureRefTokens();let toks=0;refs.forEach(r=>toks+=(r.tok||REF_IMG_TOKENS));
  const pd=projects[$('projSel').value];
  if($('useVis').checked&&pd&&pd.refs)toks+=pd.refs.length*REF_IMG_TOKENS;
  est+=toks*8/1e6;}
 $('estv').textContent='aprox. $'+est.toFixed(est<0.1?4:3)+(n>1?' ×'+n:'');$('go').disabled=!ok;
 try{batchEst()}catch(e){}}
let selRes=0;
function clearRes(){selRes=0;document.querySelectorAll('.rchip').forEach(x=>x.classList.remove('on'))}
function applyRes(){if(!selRes)return;
 const MAXA=8294400;  // tope de gpt-image-2 (8.29 MP)
 const r=(+$('w').value)/(+$('h').value);  // ratio actual W/H
 let W,H;
 if(selRes==='uhd'){             // Ultra HD: lado largo a 3840, luego topado por área
  if(r>=1){W=3840;H=W/r;}else{H=3840;W=H*r;}
 }else{                          // por área: W·H≈selRes conservando el ratio
  H=Math.sqrt(selRes/r);W=H*r;
 }
 if(W*H>MAXA){const s=Math.sqrt(MAXA/(W*H));W*=s;H*=s;}
 W=Math.max(512,Math.min(3840,snap(W)));H=Math.max(512,Math.min(3840,snap(H)));
 // el redondeo a 16 puede empujar el ratio sobre 3:1; baja el lado largo para mantenerlo ≤3:1
 if(Math.max(W,H)/Math.min(W,H)>3){if(W>H)W=Math.floor(H*3/16)*16;else H=Math.floor(W*3/16)*16;}
 $('w').value=W;$('h').value=H;$('wv').value=W;$('hv').value=H;ratio=W/H;validate()}
$('w').oninput=()=>{if($('lock').checked){$('h').value=snap(Math.min(3840,Math.max(512,$('w').value/ratio)));$('hv').value=$('h').value}$('wv').value=$('w').value;clearRes();validate()};
$('h').oninput=()=>{if($('lock').checked){$('w').value=snap(Math.min(3840,Math.max(512,$('h').value*ratio)));$('wv').value=$('w').value}$('hv').value=$('h').value;clearRes();validate()};
$('lock').onchange=()=>ratio=$('w').value/$('h').value;
$('lockBtn').onclick=()=>{const on=!$('lock').checked;$('lock').checked=on;$('lockBtn').classList.toggle('on',on);
 $('lockBtn').setAttribute('aria-pressed',on);if(on)ratio=$('w').value/$('h').value};
function commitNum(numId,sliderId){
 let v=Math.max(512,Math.min(3840,snap(+$(numId).value||512)));
 $(numId).value=v;$(sliderId).value=v;
 if($('lock').checked){
  if(sliderId==='w'){$('h').value=snap(Math.min(3840,Math.max(512,v/ratio)));$('hv').value=$('h').value}
  else{$('w').value=snap(Math.min(3840,Math.max(512,v*ratio)));$('wv').value=$('w').value}}
 clearRes();validate()}
$('wv').addEventListener('change',()=>commitNum('wv','w'));
$('hv').addEventListener('change',()=>commitNum('hv','h'));
['wv','hv'].forEach(n=>$(n).addEventListener('keydown',e=>{
 if(e.key==='Enter'){e.preventDefault();$(n).blur()}}));
function setSizeFromRefAR(){
 if(!refs.length){toast('Carga una imagen de referencia primero','bad');return}
 const im=new Image();
 im.onload=()=>{let rw=im.naturalWidth,rh=im.naturalHeight;
  if(!rw||!rh){toast('No pude leer el tamaño de la referencia','bad');return}
  let ratioWH=rw/rh;if(ratioWH>3)ratioWH=3;else if(ratioWH<1/3)ratioWH=1/3;   // límite gpt-image-2 3:1
  const cur=(+$('w').value||0)*(+$('h').value||0);
  let area=cur>655360?cur:2073600;area=Math.min(8000000,Math.max(800000,area));   // conserva la resolución actual (o ~1080p)
  let W=Math.round(Math.sqrt(area*ratioWH)/16)*16, H=Math.round(Math.sqrt(area/ratioWH)/16)*16;
  W=Math.max(512,Math.min(3840,W));H=Math.max(512,Math.min(3840,H));
  document.querySelectorAll('.chip[data-w]').forEach(x=>x.classList.remove('on'));
  document.querySelector('.chip[data-refar]').classList.add('on');
  $('w').value=W;$('h').value=H;$('wv').value=W;$('hv').value=H;ratio=W/H;validate();
  toast('Proporción de la referencia: '+W+'×'+H);};
 im.onerror=()=>toast('No pude leer la referencia','bad');
 im.src='data:image/png;base64,'+refs[0].b64;}
$('presets').onclick=e=>{const c=e.target.closest('.chip');if(!c)return;
 if(c.dataset.refar){setSizeFromRefAR();return}
 if(c.dataset.px||c.dataset.uhd){const was=c.classList.contains('on');
  document.querySelectorAll('.rchip').forEach(x=>x.classList.remove('on'));
  if(was){selRes=0;return}
  selRes=c.dataset.uhd?'uhd':+c.dataset.px;c.classList.add('on');applyRes();return}
 document.querySelectorAll('.chip[data-w]').forEach(x=>x.classList.remove('on'));c.classList.add('on');
 $('w').value=c.dataset.w;$('h').value=c.dataset.h;$('wv').value=c.dataset.w;$('hv').value=c.dataset.h;ratio=c.dataset.w/c.dataset.h;
 if(selRes)applyRes();else validate()};
$('quality').onchange=validate;$('n').onchange=validate;
$('fmt').onchange=()=>$('compBox').classList.toggle('hide',$('fmt').value==='png');
$('comp').oninput=()=>$('compv').textContent=$('comp').value+'%';
let cfgEffective='~/Desktop';
let genLabel='General';
function renderSaveWhere(){
 $('saveWhere').innerHTML='Se guarda en <span class="mono">~/image-studio/historial</span>'
  +($('saveDesk').checked?' + copia en <span class="mono">'+esc(cfgEffective)+'</span>':' (sin copia extra)');
 $('dirMsg').textContent='Copia en: '+cfgEffective;
 $('dirBox').style.opacity=$('saveDesk').checked?'1':'.4'}
$('saveDesk').checked=localStorage.getItem('studio_desk')!=='0';
$('saveDesk').onchange=()=>{localStorage.setItem('studio_desk',$('saveDesk').checked?'1':'0');renderSaveWhere()};
async function loadConfig(){const r=await(await fetch('/config?project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(activeSub||''))).json();
 genLabel=r.general_label||'General';
 $('saveDir').value=r.save_dir||'';cfgEffective=r.effective;renderSaveWhere();
 if($('histDirLbl'))$('histDirLbl').textContent=r.effective||'(carpeta interna de la app)';
 voiceStyles=r.voice_styles||[];renderVStyles()}
$('dirApply').onclick=async()=>{
 const r=await(await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({save_dir:$('saveDir').value})})).json();
 if(r.error){toast(r.error,'bad');return}
 cfgEffective=r.effective;renderSaveWhere();toast('Las copias irán a '+r.effective)};
$('dirPick').onclick=async()=>{toast('Abriendo selector de carpeta…');
 try{const r=await(await fetch('/pickfolder')).json();
  if(r.path){$('saveDir').value=r.path;$('dirApply').click();}
  else if(r.error)toast(r.error,'bad');
 }catch(e){toast(String(e),'bad')}};

function fileToB64(f){return new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(f)})}
function xicon(){return '<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>'}
// ===== Papelera =====
async function tpost(u,b){return (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json();}
function trashClose(){$('trashModal').classList.add('hide')}
async function openTrash(){$('trashModal').classList.remove('hide');await renderTrash()}
async function renderTrash(){let r;try{r=await(await fetch('/trash')).json()}catch(e){r={items:[]}}
 const items=r.items||[];$('trashCount').textContent=items.length?(items.length+(items.length>1?' elementos':' elemento')):trVal('Vacía',LANG);
 const tMis=trVal('Mis imágenes',LANG),tHist=trVal('Historial',LANG),tRest=trVal('Restaurar',LANG),tDel=trVal('Borrar para siempre',LANG);
 $('trashGrid').innerHTML=items.map(it=>'<div class="tcard"><img src="/trashfile?token='+encodeURIComponent(it.token)+'" alt="" loading="lazy"><div class="tnm" title="'+esc(it.name||'')+'">'+esc(it.name||it.token)+'</div><div class="tpl">'+esc(it.plabel||'')+' · '+(it.kind==='shelf'?tMis:tHist)+'</div><div class="tacts"><button class="trest" data-t="'+esc(it.token)+'">'+tRest+'</button><button class="tdelp bdel" data-t="'+esc(it.token)+'" title="'+tDel+'">'+xicon()+'</button></div></div>').join('')||'<div class="hint" style="grid-column:1/-1">'+trVal('La papelera está vacía.',LANG)+'</div>';}
$('trashBtn').onclick=openTrash;
$('trashModal').querySelector('.mclose').onclick=trashClose;
$('trashModal').addEventListener('click',e=>{if(e.target===$('trashModal'))trashClose()});
$('trashGrid').onclick=async e=>{const rest=e.target.closest('.trest'),del=e.target.closest('.tdelp');
 if(rest){const t=rest.dataset.t;const r=await tpost('/trashrestore',{token:t});if(r&&r.ok){toast('Restaurada ✓');if(typeof loadGal==='function')loadGal();if(typeof loadShelf==='function')loadShelf();renderTrash()}else toast((r&&r.error)||'No se pudo restaurar','bad');return}
 if(del){const t=del.dataset.t;if(!del.classList.contains('arm')){del.classList.add('arm');toast('Clic otra vez para borrar para siempre','bad');setTimeout(()=>del.classList.remove('arm'),2500);return}await tpost('/trashdelete',{token:t});renderTrash();return}};
$('trashEmpty').onclick=async()=>{const b=$('trashEmpty');if(!b.classList.contains('arm')){b.classList.add('arm');b.textContent='¿Vaciar todo?';setTimeout(()=>{b.classList.remove('arm');b.textContent='Vaciar papelera'},2500);return}await tpost('/trashdelete',{all:true});b.classList.remove('arm');b.textContent='Vaciar papelera';renderTrash();toast('Papelera vaciada')};
let _refKeyc=0;
function renderThumbs(){refs.forEach(r=>{if(r._k===undefined)r._k=++_refKeyc});
 $('thumbs').innerHTML=refs.map((r,i)=>`<div class="thumb" draggable="true" data-i="${i}" data-k="${r._k}"><img draggable="false" src="data:image/png;base64,${r.b64}" alt="${esc(r.name)}" title="Arrastra para reordenar · clic para ampliar"><button class="x" data-i="${i}" title="Quitar">${xicon()}</button></div>`).join('')}
// formatos que acepta OpenAI para entrada de imagen
const OK_IMG_TYPES=new Set(['image/png','image/jpeg','image/webp','image/gif']);
// ===== video → fotogramas de referencia (gpt-image-2 no acepta video) =====
const VIDEO_RE=/\.(mp4|mov|m4v|webm|mkv|avi|qt)$/i;
function isVideoFile(f){return !!f&&((f.type&&f.type.startsWith('video/'))||VIDEO_RE.test(f.name||''));}
// línea de tiempo + captura por canvas (100% en el navegador, frame exacto)
let vfTarget=null,vfShotsArr=[],vfURL=null,vfBase='video';
const VF_FPS=30; // paso aproximado de "1 fotograma"
function vfFmt(t){t=Math.max(0,t||0);const m=Math.floor(t/60),s=Math.floor(t%60);return m+':'+(s<10?'0':'')+s}
function openVideoFrames(file,target){
 vfTarget=target;vfShotsArr=[];
 vfBase=(file.name||'video').replace(/\.[^.]+$/,'').replace(/[^A-Za-z0-9_-]+/g,'_')||'video';
 // si el video vino de "Memoria visual" (proyecto), el botón de refs apunta ahí; si no, a "Referencias"
 $('vfAddRefsTxt').textContent=(target==='pref')?'memoria visual':'referencias';
 if(vfURL)URL.revokeObjectURL(vfURL);
 vfURL=URL.createObjectURL(file);
 const v=$('vfVideo');v.src=vfURL;v.currentTime=0;
 $('vfName').textContent=file.name||'';
 vfRenderShots();$('vfModal').classList.remove('hide')}
function closeVF(){$('vfModal').classList.add('hide');const v=$('vfVideo');try{v.pause()}catch(e){}
 if(vfURL){URL.revokeObjectURL(vfURL);vfURL=null}v.removeAttribute('src');try{v.load()}catch(e){}
 vfTarget=null;vfShotsArr=[]}
function vfRenderShots(){
 $('vfCount').textContent=vfShotsArr.length;
 $('vfShots').innerHTML=vfShotsArr.map((s,i)=>`<div class="vfshot"><img src="data:image/png;base64,${s.b64}" alt=""><span class="tt">${s.tt}</span><button class="x" data-i="${i}" title="Quitar">${xicon()}</button></div>`).join('');
 const none=vfShotsArr.length===0;
 $('vfAddRefs').disabled=none;$('vfAddShelf').disabled=none;$('vfAddBoth').disabled=none}
function vfCapture(){
 const v=$('vfVideo');if(!v.videoWidth){toast('El video aún no carga','bad');return}
 const M=1536;let w=v.videoWidth,h=v.videoHeight;
 if(Math.max(w,h)>M){const k=M/Math.max(w,h);w=Math.round(w*k);h=Math.round(h*k)}
 const c=document.createElement('canvas');c.width=w;c.height=h;
 c.getContext('2d').drawImage(v,0,0,w,h);
 let b64;try{b64=c.toDataURL('image/png').split(',')[1]}catch(e){toast('No pude capturar el fotograma','bad');return}
 const tt=vfFmt(v.currentTime);
 vfShotsArr.push({name:vfBase+'_'+tt.replace(':','m')+'s.png',b64,tt});
 vfRenderShots();flash($('vfCap'))}
$('vfVideo').addEventListener('loadedmetadata',()=>{const v=$('vfVideo');$('vfSeek').max=String(v.duration||0);$('vfSeek').step='0.03';$('vfTime').textContent='0:00 / '+vfFmt(v.duration)});
$('vfVideo').addEventListener('timeupdate',()=>{const v=$('vfVideo');$('vfSeek').value=String(v.currentTime);$('vfTime').textContent=vfFmt(v.currentTime)+' / '+vfFmt(v.duration)});
$('vfVideo').addEventListener('play',()=>$('vfPlay').classList.add('on'));
$('vfVideo').addEventListener('pause',()=>$('vfPlay').classList.remove('on'));
$('vfSeek').addEventListener('input',()=>{$('vfVideo').currentTime=+$('vfSeek').value});
$('vfPlay').onclick=()=>{const v=$('vfVideo');if(v.paused)v.play();else v.pause()};
$('vfStepB').onclick=()=>{const v=$('vfVideo');v.pause();v.currentTime=Math.max(0,v.currentTime-1/VF_FPS)};
$('vfStepF').onclick=()=>{const v=$('vfVideo');v.pause();v.currentTime=Math.min(v.duration||1e9,v.currentTime+1/VF_FPS)};
$('vfCap').onclick=vfCapture;
$('vfShots').onclick=e=>{const b=e.target.closest('.x');if(!b)return;vfShotsArr.splice(+b.dataset.i,1);vfRenderShots()};
$('vfCancel').onclick=closeVF;
$('vfModal').querySelector('.mclose').onclick=closeVF;
$('vfModal').onclick=e=>{if(e.target===$('vfModal'))closeVF()};
async function vfCommit(toRefs,toShelf){
 if(!vfShotsArr.length)return;const shots=vfShotsArr.slice();const plural=shots.length>1;
 if(toRefs){
  if(vfTarget==='pref'){const n=$('projSel').value,lbl=n||genLabel;
   for(const s of shots)await postRef(n,s.name,s.b64);await loadProjects();
   toast(shots.length+(plural?' fotogramas añadidos':' fotograma añadido')+' a la memoria de "'+lbl+'"')}
  else{for(const s of shots)refs.push({name:s.name,b64:s.b64});renderThumbs();
   toast(shots.length+(plural?' fotogramas añadidos':' fotograma añadido')+' a referencias')}}
 if(toShelf){await shelfAddImages(shots.map(s=>({name:s.name,b64:s.b64})))}  // shelfAddImages ya hace su toast
 closeVF()}
$('vfAddRefs').onclick=()=>vfCommit(true,false);
$('vfAddShelf').onclick=()=>vfCommit(false,true);
$('vfAddBoth').onclick=()=>vfCommit(true,true);
// reparte una lista de archivos soltados/elegidos: imágenes directas + el primer video al modal de frames
async function routeRefFiles(list,target){const arr=[...list];const vid=arr.find(isVideoFile);
 const imgs=arr.filter(f=>!isVideoFile(f));
 if(imgs.length){
  if(target==='pref'){const n=$('projSel').value;let added=0;
   for(const f of imgs){if(!OK_IMG_TYPES.has(f.type))continue;await postRef(n,f.name,await fileToB64(f));added++}
   if(added){await loadProjects();toast(added+(added>1?' referencias añadidas':' referencia añadida')+' a la memoria de "'+(n||genLabel)+'"')}}
  else{const n=await addFiles(imgs);if(n)toast(n+(n>1?' imágenes añadidas':' imagen añadida')+' como referencia')}}
 if(vid)openVideoFrames(vid,target);
 if(arr.filter(isVideoFile).length>1)toast('Solo proceso un video a la vez','bad')}
async function addFiles(list){let added=0,bad=0;
 for(const f of list){
  if(!OK_IMG_TYPES.has(f.type)){bad++;continue}
  if(f.size>50*1024*1024){toast(f.name+' supera 50MB','bad');continue}
  refs.push({name:f.name,b64:await fileToB64(f)});added++}
 if(bad)toast(bad+(bad>1?' archivos ignorados':' archivo ignorado')+': solo PNG/JPEG/WebP/GIF','bad');
 if(added)renderThumbs();return added}
$('drop').onclick=()=>$('files').click();
$('files').onchange=e=>{routeRefFiles(e.target.files,'local');e.target.value=''};
$('thumbs').onclick=e=>{const b=e.target.closest('.x');
 if(b){flipRefs($('thumbs'),()=>{refs.splice(+b.dataset.i,1);renderThumbs()});return}
 const t=e.target.closest('.thumb');if(t){const r=refs[+t.dataset.i];if(r)openLb('data:image/png;base64,'+r.b64,'',null)}};
// ===== reordenar referencias arrastrándolas, con animación fluida (FLIP) =====
let refReordering=false;
// anima los vecinos de las REFERENCIAS desde su posición previa hasta la nueva (los que NO se arrastran)
// (nombre propio para no colisionar con el flipMove de 3 args de los grids de imágenes)
function flipRefs(cont,mutate){
 const before=new Map([...cont.children].map(el=>[el.dataset.k,el.getBoundingClientRect()]));
 mutate();
 [...cont.children].forEach(el=>{
  if(el.classList.contains('dragging'))return;
  const b=before.get(el.dataset.k);if(!b)return;
  const a=el.getBoundingClientRect();const dx=b.left-a.left,dy=b.top-a.top;
  if(dx||dy){el.style.transition='none';el.style.transform='translate('+dx+'px,'+dy+'px)';
   requestAnimationFrame(()=>{el.style.transition='transform .22s cubic-bezier(.2,.85,.25,1)';el.style.transform=''});
   el.addEventListener('transitionend',()=>{el.style.transition='';el.style.transform=''},{once:true})}})}
// reconstruye el array refs según el orden actual del DOM (tras mover nodos en vivo)
function syncRefsFromDOM(){const order=[...$('thumbs').children].map(el=>+el.dataset.k);
 refs.sort((x,y)=>order.indexOf(x._k)-order.indexOf(y._k));renderThumbs()}
$('thumbs').addEventListener('dragstart',e=>{const t=e.target.closest('.thumb');if(!t)return;
 refReordering=true;requestAnimationFrame(()=>t.classList.add('dragging'));
 try{e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/x-studio-reorder','1')}catch(_){}});
$('thumbs').addEventListener('dragover',e=>{if(!refReordering)return;e.preventDefault();e.stopPropagation();
 try{e.dataTransfer.dropEffect='move'}catch(_){}
 const cont=$('thumbs'),dragged=cont.querySelector('.thumb.dragging');if(!dragged)return;
 const t=e.target.closest('.thumb');if(!t||t===dragged)return;
 const r=t.getBoundingClientRect();
 const ref=(e.clientX>=r.left+r.width/2)?t.nextElementSibling:t;   // ¿antes o después del objetivo?
 if(ref===dragged||dragged.nextElementSibling===ref)return;        // ya está ahí → nada que hacer
 flipRefs(cont,()=>cont.insertBefore(dragged,ref))});              // mueve en vivo y desliza a los vecinos
$('thumbs').addEventListener('drop',e=>{if(!refReordering)return;e.preventDefault();e.stopPropagation()});
$('thumbs').addEventListener('dragend',()=>{const d=$('thumbs').querySelector('.thumb.dragging');
 if(d)d.classList.remove('dragging');
 if(refReordering){refReordering=false;syncRefsFromDOM();validate();if(typeof updGenChip==='function')updGenChip()}});
['dragover','dragenter'].forEach(ev=>$('drop').addEventListener(ev,e=>{if(refReordering)return;e.preventDefault();$('drop').classList.add('hot')}));
['dragleave','drop'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.remove('hot')}));
// arrastrar a cualquier parte de la ventana
window.addEventListener('dragover',e=>{if(refReordering)return;e.preventDefault();$('drop').classList.add('hot')});
window.addEventListener('dragleave',e=>{if(!e.relatedTarget)$('drop').classList.remove('hot')});
window.addEventListener('drop',async e=>{if(refReordering){e.preventDefault();return}e.preventDefault();$('drop').classList.remove('hot');
 const dt=e.dataTransfer;if(!dt)return;
 const audF=[...dt.files].find(f=>f.type.startsWith('audio/')||/\.(mp3|m4a|wav|webm|ogg|oga|flac|mpga)$/i.test(f.name));
 if(audF){setSttFile(audF);return}
 // arrastre interno: imagen generada (resultado), historial o estante → referencia
 if(dt.getData('text/x-studio-b64')||dt.getData('text/x-studio-file')||dt.getData('text/x-studio-shelf')){
  const imgs=await imagesFromDT(dt);
  if(imgs.length){for(const im of imgs)refs.push({name:im.name,b64:im.b64});renderThumbs();
   toast(imgs.length>1?imgs.length+' imágenes añadidas como referencia':'Añadida como referencia');}
  return}
 if(dt.files.length)routeRefFiles(dt.files,'local')});
// pegar desde el portapapeles
document.addEventListener('paste',async e=>{
 const t=document.activeElement.tagName;if(t==='TEXTAREA'||t==='INPUT')return;
 const items=[...(e.clipboardData?e.clipboardData.items:[])].filter(i=>i.type.startsWith('image/'));
 if(!items.length)return;e.preventDefault();
 for(const it of items){const f=it.getAsFile();if(f)refs.push({name:'pegada_'+Date.now()+'.png',b64:await fileToB64(f)})}
 renderThumbs();toast(items.length>1?items.length+' imágenes pegadas como referencia':'Imagen pegada como referencia')});

function renderMaskThumb(){$('maskThumb').innerHTML=mask?`<div class="thumb"><img src="data:image/png;base64,${mask.b64}" alt="Máscara"><button class="x" id="mx" title="Quitar máscara">${xicon()}</button></div>`:'';
 if(mask)$('mx').onclick=()=>{mask=null;renderMaskThumb()}}
$('dropMask').onclick=()=>$('maskFile').click();
$('maskFile').onchange=async e=>{const f=e.target.files[0];if(!f)return;mask={name:f.name,b64:await fileToB64(f)};renderMaskThumb();toast('Máscara cargada')};

// ===== editor de imagen: máscara · anotar · pins =====
const RED='#e5483f';
let edTab='mask',mTool='brush',aTool='arrow';
let mDrawing=false,mLast=null,mSnap=null,mPts=[];
let aDrawing=false,aLast=null,aSnap=null,aStart=null;
let maskOps=0,annoOps=0,annoUndo=[],pins=[];
const mCanvas=()=>$('maskDraw'),aCanvas=()=>$('annoDraw');
const aCtx=()=>aCanvas().getContext('2d');
function selTool(group,id){[...$(group).querySelectorAll('.mtool')].forEach(b=>b.classList.toggle('on',b.id===id))}
function edSetTab(t){edTab=t;
 [...$('edTabs').children].forEach(b=>b.classList.toggle('on',b.dataset.tab===t));
 $('toolsMask').classList.toggle('hide',t!=='mask');
 $('toolsAnno').classList.toggle('hide',t!=='anno');
 $('toolsPins').classList.toggle('hide',t!=='pins');
 $('pinList').classList.toggle('hide',t!=='pins'||!pins.length);
 mCanvas().style.pointerEvents=t==='mask'?'auto':'none';
 aCanvas().style.pointerEvents=t==='anno'?'auto':'none';
 $('pinLayer').style.pointerEvents=t==='pins'?'auto':'none';
 $('annoText').classList.add('hide');
 $('edHint').textContent=t==='mask'?'Pinta o selecciona lo que quieres regenerar. El resto se conserva.'
  :t==='anno'?'Dibuja instrucciones en rojo: flechas, círculos, trazos o texto. No saldrán en el resultado.'
  :'Clic sobre la imagen para soltar pins numerados y escribe la instrucción de cada uno.'}
$('edTabs').onclick=e=>{const b=e.target.closest('button');if(b)edSetTab(b.dataset.tab)};
function maskOpen(){
 if(!refs.length){toast('Sube o pega primero una imagen a editar','bad');return}
 const img=$('maskBase');
 img.onload=()=>{[mCanvas(),aCanvas()].forEach(c=>{c.width=img.naturalWidth;c.height=img.naturalHeight;
   c.getContext('2d').clearRect(0,0,c.width,c.height)});
  maskOps=0;annoOps=0;annoUndo=[];pins=[];renderPins();edSetTab('mask')};
 img.src='data:image/png;base64,'+refs[0].b64;
 mTool='brush';aTool='arrow';selTool('toolsMask','mBrush');selTool('toolsAnno','aArrow');
 $('maskModal').classList.remove('hide');
}
$('maskPaint').onclick=maskOpen;
$('mBrush').onclick=()=>{mTool='brush';selTool('toolsMask','mBrush')};
$('mErase').onclick=()=>{mTool='erase';selTool('toolsMask','mErase')};
$('mRect').onclick=()=>{mTool='rect';selTool('toolsMask','mRect')};
$('mLasso').onclick=()=>{mTool='lasso';selTool('toolsMask','mLasso')};
$('mClear').onclick=()=>{const c=mCanvas();c.getContext('2d').clearRect(0,0,c.width,c.height);maskOps=0};
$('aArrow').onclick=()=>{aTool='arrow';selTool('toolsAnno','aArrow')};
$('aCircle').onclick=()=>{aTool='circle';selTool('toolsAnno','aCircle')};
$('aFree').onclick=()=>{aTool='free';selTool('toolsAnno','aFree')};
$('aText').onclick=()=>{aTool='text';selTool('toolsAnno','aText')};
$('aUndo').onclick=()=>{if(!annoUndo.length)return;aCtx().putImageData(annoUndo.pop(),0,0);annoOps=Math.max(0,annoOps-1)};
$('aClear').onclick=()=>{const c=aCanvas();c.getContext('2d').clearRect(0,0,c.width,c.height);annoOps=0;annoUndo=[]};
$('mCancel').onclick=()=>$('maskModal').classList.add('hide');
function cPt(c,e){const r=c.getBoundingClientRect();
 return{x:(e.clientX-r.left)*c.width/r.width,y:(e.clientY-r.top)*c.height/r.height,k:c.width/r.width}}
function pushUndo(){const c=aCanvas();annoUndo.push(c.getContext('2d').getImageData(0,0,c.width,c.height));
 if(annoUndo.length>10)annoUndo.shift()}
// --- máscara: pincel, borrador, rectángulo, lazo ---
function mStroke(a,b,k){const ctx=mCanvas().getContext('2d');
 ctx.globalCompositeOperation=mTool==='erase'?'destination-out':'source-over';
 ctx.strokeStyle='#e0a571';ctx.lineWidth=+$('mSize').value*k;ctx.lineCap='round';ctx.lineJoin='round';
 ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke()}
$('maskDraw').addEventListener('pointerdown',e=>{const c=mCanvas();mDrawing=true;c.setPointerCapture(e.pointerId);
 const p=cPt(c,e);mLast=p;mPts=[p];maskOps++;
 if(mTool==='rect'||mTool==='lasso')mSnap=c.getContext('2d').getImageData(0,0,c.width,c.height);
 else mStroke(p,{x:p.x+.01,y:p.y+.01},p.k)});
$('maskDraw').addEventListener('pointermove',e=>{if(!mDrawing)return;const c=mCanvas(),p=cPt(c,e),ctx=c.getContext('2d');
 if(mTool==='brush'||mTool==='erase'){mStroke(mLast,p,p.k);mLast=p;return}
 ctx.putImageData(mSnap,0,0);ctx.globalCompositeOperation='source-over';
 ctx.strokeStyle='#e0a571';ctx.lineWidth=2*p.k;ctx.setLineDash([6*p.k,5*p.k]);
 if(mTool==='rect')ctx.strokeRect(mPts[0].x,mPts[0].y,p.x-mPts[0].x,p.y-mPts[0].y);
 else{mPts.push(p);ctx.beginPath();ctx.moveTo(mPts[0].x,mPts[0].y);mPts.forEach(q=>ctx.lineTo(q.x,q.y));ctx.stroke()}
 ctx.setLineDash([]);mLast=p});
['pointerup','pointercancel'].forEach(ev=>$('maskDraw').addEventListener(ev,()=>{
 if(!mDrawing)return;mDrawing=false;const ctx=mCanvas().getContext('2d');
 if(mSnap){ctx.putImageData(mSnap,0,0);ctx.globalCompositeOperation='source-over';ctx.fillStyle='#e0a571';
  if(mTool==='rect'&&mLast)ctx.fillRect(mPts[0].x,mPts[0].y,mLast.x-mPts[0].x,mLast.y-mPts[0].y);
  if(mTool==='lasso'&&mPts.length>2){ctx.beginPath();ctx.moveTo(mPts[0].x,mPts[0].y);
   mPts.forEach(q=>ctx.lineTo(q.x,q.y));ctx.closePath();ctx.fill()}}
 mSnap=null;mLast=null;mPts=[]}));
// --- anotaciones: flecha, círculo, trazo, texto ---
function aFreeSeg(a,b,k){const ctx=aCtx();ctx.globalCompositeOperation='source-over';
 ctx.strokeStyle=RED;ctx.lineWidth=6*k;ctx.lineCap='round';ctx.lineJoin='round';
 ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke()}
function drawArrow(ctx,a,b,k){ctx.strokeStyle=RED;ctx.fillStyle=RED;ctx.lineWidth=6*k;ctx.lineCap='round';
 ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
 const ang=Math.atan2(b.y-a.y,b.x-a.x),L=22*k;
 ctx.beginPath();ctx.moveTo(b.x,b.y);
 ctx.lineTo(b.x-L*Math.cos(ang-0.45),b.y-L*Math.sin(ang-0.45));
 ctx.lineTo(b.x-L*Math.cos(ang+0.45),b.y-L*Math.sin(ang+0.45));
 ctx.closePath();ctx.fill()}
function drawEllipse(ctx,a,b,k){ctx.strokeStyle=RED;ctx.lineWidth=6*k;
 ctx.beginPath();ctx.ellipse((a.x+b.x)/2,(a.y+b.y)/2,Math.abs(b.x-a.x)/2||1,Math.abs(b.y-a.y)/2||1,0,0,Math.PI*2);ctx.stroke()}
$('annoDraw').addEventListener('pointerdown',e=>{const c=aCanvas(),p=cPt(c,e);
 if(aTool==='text'){placeText(e,p);return}
 aDrawing=true;c.setPointerCapture(e.pointerId);pushUndo();annoOps++;aStart=p;aLast=p;
 if(aTool==='arrow'||aTool==='circle')aSnap=aCtx().getImageData(0,0,c.width,c.height);
 else aFreeSeg(p,{x:p.x+.01,y:p.y+.01},p.k)});
$('annoDraw').addEventListener('pointermove',e=>{if(!aDrawing)return;const c=aCanvas(),p=cPt(c,e);
 if(aTool==='free'){aFreeSeg(aLast,p,p.k);aLast=p;return}
 const ctx=aCtx();ctx.putImageData(aSnap,0,0);
 if(aTool==='arrow')drawArrow(ctx,aStart,p,p.k);else drawEllipse(ctx,aStart,p,p.k);aLast=p});
['pointerup','pointercancel'].forEach(ev=>$('annoDraw').addEventListener(ev,()=>{aDrawing=false;aSnap=null;aLast=null;aStart=null}));
function placeText(e,p){const inp=$('annoText'),stack=document.querySelector('.maskstack'),r=stack.getBoundingClientRect();
 inp.style.left=(e.clientX-r.left)+'px';inp.style.top=(e.clientY-r.top)+'px';
 inp.dataset.x=p.x;inp.dataset.y=p.y;inp.dataset.k=p.k;inp.value='';inp.classList.remove('hide');inp.focus()}
$('annoText').addEventListener('keydown',e=>{e.stopPropagation();
 if(e.key==='Escape'){$('annoText').classList.add('hide');return}
 if(e.key!=='Enter')return;
 const inp=$('annoText'),t=inp.value.trim();inp.classList.add('hide');if(!t)return;
 pushUndo();annoOps++;const ctx=aCtx(),k=+inp.dataset.k;
 ctx.font='600 '+Math.round(38*k)+"px 'Schibsted Grotesk',sans-serif";
 ctx.textAlign='left';ctx.textBaseline='middle';
 ctx.lineWidth=4*k;ctx.strokeStyle='rgba(0,0,0,.55)';ctx.strokeText(t,+inp.dataset.x,+inp.dataset.y);
 ctx.fillStyle=RED;ctx.fillText(t,+inp.dataset.x,+inp.dataset.y)});
// --- pins numerados ---
function renderPins(){
 $('pinLayer').innerHTML=pins.map((p,i)=>`<div class="pin" data-i="${i}" style="left:${p.fx*100}%;top:${p.fy*100}%">${i+1}</div>`).join('');
 $('pinList').innerHTML=pins.map((p,i)=>`<div class="pinrow"><span class="pinnum">${i+1}</span><input type="text" data-i="${i}" placeholder="Instrucción del punto ${i+1}…" value="${esc(p.text)}"><button class="x" data-del="${i}" title="Quitar pin">${xicon()}</button></div>`).join('');
 $('pinList').classList.toggle('hide',edTab!=='pins'||!pins.length)}
$('pinLayer').onclick=e=>{
 const pin=e.target.closest('.pin');
 if(pin){const inp=$('pinList').querySelector(`input[data-i="${pin.dataset.i}"]`);if(inp)inp.focus();return}
 const r=$('pinLayer').getBoundingClientRect();
 pins.push({fx:(e.clientX-r.left)/r.width,fy:(e.clientY-r.top)/r.height,text:''});renderPins();
 const inp=$('pinList').querySelector(`input[data-i="${pins.length-1}"]`);if(inp)inp.focus()};
$('pinList').addEventListener('input',e=>{const i=e.target.dataset.i;if(i!==undefined)pins[+i].text=e.target.value});
$('pinList').addEventListener('click',e=>{const b=e.target.closest('[data-del]');if(!b)return;pins.splice(+b.dataset.del,1);renderPins()});
$('pClear').onclick=()=>{pins=[];renderPins()};
// --- aplicar ---
$('mApply').onclick=()=>{const img=$('maskBase');let made=[];
 if(maskOps>0){const dc=mCanvas(),out=document.createElement('canvas');out.width=dc.width;out.height=dc.height;
  const ctx=out.getContext('2d');ctx.drawImage(img,0,0,out.width,out.height);
  ctx.globalCompositeOperation='destination-out';ctx.drawImage(dc,0,0);
  mask={name:'mask.png',b64:out.toDataURL('image/png').split(',')[1]};renderMaskThumb();made.push('máscara')}
 if(annoOps>0||pins.length){const ac=aCanvas(),out=document.createElement('canvas');out.width=ac.width;out.height=ac.height;
  const ctx=out.getContext('2d');ctx.drawImage(img,0,0,out.width,out.height);ctx.drawImage(ac,0,0);
  const R=Math.max(16,out.width*0.018);
  pins.forEach((p,i)=>{const x=p.fx*out.width,y=p.fy*out.height;
   ctx.beginPath();ctx.arc(x,y,R,0,Math.PI*2);ctx.fillStyle=RED;ctx.fill();
   ctx.lineWidth=R*0.18;ctx.strokeStyle='#fff';ctx.stroke();
   ctx.fillStyle='#fff';ctx.font='700 '+Math.round(R*1.15)+"px 'Geist Mono',monospace";
   ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(String(i+1),x,y)});
  refs.push({name:'instrucciones.png',b64:out.toDataURL('image/png').split(',')[1]});renderThumbs();
  let note='Sigue las instrucciones marcadas en rojo en la imagen "instrucciones" (flechas, círculos, texto y pins numerados) y aplícalas a la primera imagen. No incluyas ninguna marca roja en el resultado.';
  const lines=pins.map((p,i)=>p.text.trim()?'Punto '+(i+1)+': '+p.text.trim():'').filter(Boolean);
  if(lines.length)note+='\n'+lines.join('\n');
  $('prompt').value=($('prompt').value.trim()?$('prompt').value.trim()+'\n\n':'')+note;
  made.push(pins.length?'anotaciones y pins':'anotaciones')}
 if(!made.length){toast('No has marcado nada todavía','bad');return}
 $('maskModal').classList.add('hide');if(mode!=='editar')setMode('editar');
 toast('Listo: '+made.join(' + ')+(annoOps>0||pins.length?' · instrucciones añadidas al prompt':''))};

let LANG=localStorage.getItem('studio_lang')||'es';
let activeSub=localStorage.getItem('studio_sub')||'';
function curSubs(){return (window.SUBS&&window.SUBS[curProj()])||[]}
function pj(extra){return Object.assign({project:$('projSel').value,sub:activeSub},extra||{})}
async function loadProjects(){const _r=await(await fetch('/projects')).json();projects=_r.projects||_r;window.SUBS=_r.subs||{};const s=$('projSel');
 const cur=s.value||localStorage.getItem('studio_proj')||'';
 s.innerHTML=`<option value="">${esc(genLabel)}</option>`+Object.keys(projects).filter(n=>n).map(n=>`<option ${n===cur?'selected':''}>${esc(n)}</option>`).join('');renderSubSel();renderProj()}
function renderSubSel(){const sel=$('subSel');if(!sel)return;const subs=curSubs();
 if(!subs.some(s=>s.key===activeSub))activeSub='';
 sel.innerHTML='<option value="">Todo el proyecto</option>'+subs.map(s=>`<option value="${esc(s.key)}" ${s.key===activeSub?'selected':''}>${esc(s.label)}</option>`).join('')+'<option value="__new__">+ Crear subproyecto…</option>';
 sel.value=activeSub}
async function createSub(){const name=prompt('Nombre del subproyecto:');if(!name||!name.trim())return null;
 const r=await(await fetch('/subcreate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:$('projSel').value,name:name.trim()})})).json();
 if(r.error){toast(r.error,'bad');return null}
 await loadProjects();activeSub=r.key;localStorage.setItem('studio_sub',activeSub);renderSubSel();await switchProject();toast('Subproyecto "'+r.label+'" creado');return r.key}
async function setActiveProject(n,sub){try{await fetch('/setproject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:n,sub:sub||''})});}catch(e){}}
async function switchProject(){const n=$('projSel').value;localStorage.setItem('studio_proj',n);
 const subs=curSubs();if(!subs.some(s=>s.key===activeSub))activeSub='';
 localStorage.setItem('studio_sub',activeSub);
 await setActiveProject(n,activeSub);renderSubSel();renderProj();await loadConfig();
 galSubs=new Set(['all']);shelfSubs=new Set(['all']);
 renderGalChips();renderShelfChips();await loadGal();await loadShelf();}
let styleTab='img';
function stashStyle(){const n=$('projSel').value;if(!projects[n])return;
 projects[n][styleTab==='img'?'style':'style_video']=$('style').value}
function renderProj(){const n=$('projSel').value,p=projects[n];
 {const l=$('memProjLbl');if(l)l.textContent=n||genLabel;}
 {const b=$('projBtnLbl');if(b)b.textContent=n||genLabel;}
 $('style').value=p?(styleTab==='img'?(p.style||''):(p.style_video||'')):'';
 $('style').placeholder=styleTab==='img'?'Estilo: técnica, paleta, luz, mood…':'Estilo de video: cámara, movimiento, ritmo, grading…';
 $('prefThumbs').innerHTML=p?p.refs.map(f=>`<div class="thumb" data-f="${esc(f)}"><img src="/pfile?project=${encodeURIComponent(n)}&name=${encodeURIComponent(f)}" alt="" title="Clic para ampliar"><button class="x" data-f="${esc(f)}" title="Quitar">${xicon()}</button></div>`).join(''):''}
$('styleSeg').onclick=e=>{const btn=e.target.closest('button');if(!btn)return;
 stashStyle();styleTab=btn.dataset.st;
 [...$('styleSeg').children].forEach(x=>x.classList.toggle('on',x.dataset.st===styleTab));renderProj()};
$('style').addEventListener('input',stashStyle);
$('projSel').onchange=()=>switchProject();
$('subSel').onchange=async()=>{const v=$('subSel').value;
 if(v==='__new__'){renderSubSel();await createSub();return}
 activeSub=v;localStorage.setItem('studio_sub',activeSub);await switchProject()};
$('useVis').checked=localStorage.getItem('studio_usevis')!=='0';
$('useVis').onchange=()=>localStorage.setItem('studio_usevis',$('useVis').checked?'1':'0');
// crear / borrar proyecto — reutilizable (modal o panel)
async function createProject(n){n=(n||'').trim();if(!n)return false;
 if(projects[n]){$('projSel').value=n;await switchProject();toast('El proyecto "'+n+'" ya existía · seleccionado');return true}
 await fetch('/project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});
 await loadProjects();$('projSel').value=n;await switchProject();toast('Proyecto "'+n+'" creado · empieza vacío');return true}
async function deleteProject(n){
 const r=await(await fetch('/projectdel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})})).json();
 if(r&&r.error){toast(r.error,'bad');return}
 if(!n){ // General: se vació (no se elimina el espacio)
  if($('projSel').value==='')  {await loadGal();await loadShelf();}
  toast('«General» vaciado');return}
 const wasActive=$('projSel').value===n;
 await loadProjects();
 if(wasActive){$('projSel').value='';await switchProject();}
 toast('Proyecto "'+n+'" borrado por completo')}
$('delProj').onclick=()=>{const n=$('projSel').value;
 if(!$('delProj').classList.contains('arm')){
  $('delProj').classList.add('arm');toast((n?('Borra "'+n+'" con TODO su historial y Mis imágenes'):('Vacía «'+genLabel+'»: borra su historial y Mis imágenes'))+' · clic otra vez','bad');
  setTimeout(()=>$('delProj').classList.remove('arm'),2800);return}
 $('delProj').classList.remove('arm');deleteProject(n)};
// ===== ventana (modal) de proyectos =====
async function openProjModal(){
 try{const r=await(await fetch('/projectcards')).json();renderProjCards(r.cards||[]);}
 catch(e){$('projGrid').innerHTML='<div class="hint">No pude cargar los proyectos</div>';}
 $('projNewName').value='';$('projModal').classList.remove('hide');setTimeout(()=>$('projNewName').focus(),60);}
let lastProjCards=[];
const PEN='<svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
function renderProjCards(cards){lastProjCards=cards;const cur=$('projSel').value;
 $('projGrid').innerHTML=cards.map(c=>{const active=c.name===cur;
  const cov=c.cover?`<div class="cov" style="background-image:url('/file?name=${encodeURIComponent(c.cover)}&project=${encodeURIComponent(c.name)}')"></div>`
   :`<div class="ph">${esc((c.label||'?').slice(0,1).toUpperCase())}</div>`;
  const acts=`<button class="pedit" data-edit="1" title="Renombrar">${PEN}</button><button class="pdel" data-del="1" title="${c.name?'Borrar proyecto':'Vaciar General'}">${GTR}</button>`;
  const cnt=c.count+' '+(c.count===1?'imagen':'imágenes')+(active?' · activo':'');
  const subs=c.subs||[];
  const subrow=`<div class="subrow">`
   +subs.map(s=>`<span class="subchipp" data-sub="${esc(s.key)}" draggable="true" title="Clic para abrir · arrastra para reordenar · ${(s.count||0)} imagen(es)"><span class="scn">${esc(s.label)}</span><span class="scc">${s.count||0}</span><button class="subren" data-subren="${esc(s.key)}" data-sublabel="${esc(s.label)}" title="Renombrar subproyecto">${PEN}</button><button class="subout" data-subpromote="${esc(s.key)}" data-sublabel="${esc(s.label)}" title="Sacar: volverlo un proyecto independiente"><svg viewBox="0 0 24 24"><path d="M12 3v12M8 7l4-4 4 4M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/></svg></button><button class="subx" data-subdel="${esc(s.key)}" title="Borrar subproyecto">${GTR}</button></span>`).join('')
   +`<button class="subadd" data-subadd="1" title="Crear subproyecto">+ subproyecto</button>`
   +(c.name?`<select class="subconv" data-conv="1" title="Convertir este proyecto en subproyecto de otro"><option value="">Convertir en sub de…</option>`+cards.filter(o=>o.name!==c.name).map(o=>`<option value="${esc(o.name)}">${esc(o.label)}</option>`).join('')+`</select>`:'')
   +`</div>`;
  return `<div class="projitem${active?' active':''}" data-name="${esc(c.name)}" data-label="${esc(c.label)}" draggable="${c.name?'true':'false'}" title="${c.name?'Arrástrame para reordenar · para anidar usa «Convertir en sub de…»':''}">
   <div class="projcard">${cov}</div>
   <div class="projfoot"><div class="mtext"><div class="pname">${esc(c.label)}</div><div class="pcount">${cnt}</div></div>${acts}</div>${subrow}</div>`}).join('');}
async function renameProject(old,nw){nw=(nw||'').trim();
 if(!nw){renderProjCards(lastProjCards);return}
 const r=await(await fetch('/projectrename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old:old,new:nw})})).json();
 if(r.error){toast(r.error,'bad');renderProjCards(lastProjCards);return}
 const wasActive=$('projSel').value===old;
 await loadConfig();await loadProjects();
 if(wasActive){$('projSel').value=r.name;await switchProject()}else{renderProj()}
 toast('Renombrado a "'+nw+'"');openProjModal()}
$('projBtn').onclick=openProjModal;
$('projModal').onclick=e=>{if(e.target===$('projModal'))$('projModal').classList.add('hide')};
$('projCreate').onclick=async()=>{const ok=await createProject($('projNewName').value);if(ok)$('projModal').classList.add('hide')};
$('projNewName').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();$('projCreate').click()}});
$('projGrid').onclick=async e=>{
 const item=e.target.closest('.projitem');if(!item)return;
 const name=item.dataset.name,label=item.dataset.label;
 const ed=e.target.closest('.pedit');
 if(ed){e.stopPropagation();const mt=item.querySelector('.mtext');
  mt.innerHTML='<input class="prename" type="text" maxlength="60" title="Enter o clic afuera para guardar · Esc para cancelar">';
  const inp=mt.querySelector('.prename');inp.value=label;inp.focus();inp.select();
  let pdone=false;const psave=()=>{if(pdone)return;pdone=true;renameProject(name,inp.value)};
  inp.addEventListener('click',ev=>ev.stopPropagation());
  inp.addEventListener('keydown',ev=>{if(ev.key==='Enter'){ev.preventDefault();psave()}else if(ev.key==='Escape'){ev.preventDefault();pdone=true;renderProjCards(lastProjCards)}});
  inp.addEventListener('blur',psave);
  return}
 const del=e.target.closest('.pdel');
 if(del){e.stopPropagation();
  if(!del.classList.contains('arm')){[...$('projGrid').querySelectorAll('.pdel.arm')].forEach(x=>x.classList.remove('arm'));
   del.classList.add('arm');
   toast((name?('Borra "'+label+'" y TODO su contenido'):('Vacía «'+label+'»: borra su historial y Mis imágenes'))+' · clic otra vez','bad');
   setTimeout(()=>del.classList.remove('arm'),2800);return}
  del.classList.remove('arm');await deleteProject(name);openProjModal();return}
 const sadd=e.target.closest('[data-subadd]');
 if(sadd){e.stopPropagation();const nm=prompt('Nombre del nuevo subproyecto de "'+(label)+'":');if(nm&&nm.trim()){const r=await(await fetch('/subcreate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:name,name:nm.trim()})})).json();if(r&&r.error){toast(r.error,'bad')}else{await loadProjects();openProjModal();toast('Subproyecto "'+nm.trim()+'" creado')}}return}
 const sren=e.target.closest('[data-subren]');
 if(sren){e.stopPropagation();const key=sren.dataset.subren,old=sren.dataset.sublabel||key;const nw=prompt('Nuevo nombre del subproyecto:',old);if(nw!==null&&nw.trim()){const r=await(await fetch('/subrename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:name,key:key,new:nw.trim()})})).json();if(r&&r.error){toast(r.error,'bad')}else{await loadProjects();openProjModal();toast('Subproyecto renombrado')}}return}
 const sprom=e.target.closest('[data-subpromote]');
 if(sprom){e.stopPropagation();const key=sprom.dataset.subpromote,lbl=sprom.dataset.sublabel||key;
  if(!confirm('¿Sacar «'+lbl+'» de «'+(label)+'» y volverlo un proyecto independiente?'))return;
  const r=await(await fetch('/subpromote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:name,key:key})})).json();
  if(r&&r.error){toast(r.error,'bad')}else{await loadProjects();openProjModal();toast('«'+(r.name||lbl)+'» ahora es un proyecto')}return}
 const sdel=e.target.closest('[data-subdel]');
 if(sdel){e.stopPropagation();const key=sdel.dataset.subdel;
  if(!sdel.classList.contains('arm')){[...$('projGrid').querySelectorAll('.subx.arm')].forEach(x=>x.classList.remove('arm'));sdel.classList.add('arm');toast('Borra el subproyecto y TODO su contenido · clic otra vez','bad');setTimeout(()=>sdel.classList.remove('arm'),2800);return}
  const r=await(await fetch('/subdel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:name,key:key})})).json();if(r&&r.error){toast(r.error,'bad')}else{await loadProjects();openProjModal();toast('Subproyecto borrado')}return}
 const schip=e.target.closest('.subchipp');
 if(schip&&!e.target.closest('button')){e.stopPropagation();$('projSel').value=name;activeSub=schip.dataset.sub||'';await switchProject();$('projModal').classList.add('hide');toast('Abierto: '+(label)+' › '+(schip.querySelector('.scn')?schip.querySelector('.scn').textContent:activeSub));return}
 if(e.target.closest('.subrow'))return;
 if(e.target.closest('input'))return;
 $('projSel').value=name;activeSub='';await switchProject();$('projModal').classList.add('hide')};
$('projGrid').addEventListener('change',async e=>{const sc=e.target.closest('.subconv');if(!sc||!sc.value||!sc.value.trim())return;
 const item=sc.closest('.projitem');if(!item)return;const src=item.dataset.name,dest=sc.value;
 if(!confirm('¿Convertir el proyecto "'+item.dataset.label+'" en subproyecto de "'+(projects[dest]!==undefined?dest:dest)+'"? Se moverá su carpeta y su contenido.')){sc.value='';return}
 const r=await(await fetch('/subconvert',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({src:src,dest:dest})})).json();
 if(r&&r.error){toast(r.error,'bad');sc.value='';return}
 const wasActive=$('projSel').value===src;await loadProjects();if(wasActive){$('projSel').value=dest;activeSub='';await switchProject()}openProjModal();toast('Convertido en subproyecto de "'+dest+'" ✓')});
// arrastrar una tarjeta de proyecto sobre otra → convertir en subproyecto
// FLIP: anima a los demás elementos para que "abran espacio" al reordenar
function flipMove(container,sel,mutate){
 const els=[...container.querySelectorAll(sel)],pos=new Map();
 els.forEach(el=>pos.set(el,el.getBoundingClientRect()));
 mutate();
 els.forEach(el=>{if(el.classList.contains('pdrag')||el.classList.contains('reordering'))return;const a=pos.get(el);if(!a)return;
  const b=el.getBoundingClientRect(),dx=a.left-b.left,dy=a.top-b.top;
  if(dx||dy){el.style.transition='none';el.style.transform='translate('+dx+'px,'+dy+'px)';
   requestAnimationFrame(()=>{el.style.transition='transform .22s cubic-bezier(.2,.7,.3,1)';el.style.transform='';});}});}
// ── Reordenar imágenes arrastrando (FLIP) — compartido por Historial y Mis imágenes ──
let reorderDrag=null;
function gridReorderStart(card,grid,sel,attr,src){card.classList.add('reordering');reorderDrag={grid,card,sel,attr,src,sub:card.dataset.sub||'',moved:false,persisted:false};}
function gridReorderOver(e,grid,sel){if(!reorderDrag||reorderDrag.grid!==grid)return;
 const t=e.target.closest(sel);if(!t||t===reorderDrag.card)return;
 if((t.dataset.sub||'')!==reorderDrag.sub)return;            // solo dentro del mismo grupo/subproyecto
 const d=reorderDrag.card,cont=t.parentElement;if(cont!==d.parentElement)return;
 e.preventDefault();
 const r=t.getBoundingClientRect(),after=e.clientX>(r.left+r.width/2),ref=after?t.nextElementSibling:t;
 if(ref!==d&&d.nextElementSibling!==ref){flipMove(cont,sel,()=>{cont.insertBefore(d,ref)});reorderDrag.moved=true;}}
function persistGridOrder(rd){const cont=rd.card.parentElement;const order=[...cont.querySelectorAll(rd.sel)].map(c=>c.dataset[rd.attr]).filter(Boolean);
 fetch('/itemsorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({src:rd.src,project:curProj(),sub:rd.sub||'',order})});}
function gridReorderDrop(e,grid,alwaysBlock){if(!reorderDrag||reorderDrag.grid!==grid)return;
 if(!reorderDrag.moved&&!alwaysBlock)return;   // en el estante, sin reorder real deja pasar el movimiento entre secciones
 e.preventDefault();e.stopPropagation();
 if(reorderDrag.moved){reorderDrag.persisted=true;persistGridOrder(reorderDrag);toast('Orden actualizado');}}
function gridReorderEnd(){if(!reorderDrag)return;reorderDrag.card.classList.remove('reordering');
 if(reorderDrag.moved&&!reorderDrag.persisted){if(reorderDrag.src==='shelf')loadShelf();else loadGal();}
 reorderDrag=null;}
window.addEventListener('dragend',gridReorderEnd);
$('projGrid').addEventListener('dragstart',e=>{
 const chip=e.target.closest('.subchipp');
 if(chip&&!e.target.closest('button')){const pit=chip.closest('.projitem');e.dataTransfer.setData('text/x-sub',chip.dataset.sub||'');e.dataTransfer.effectAllowed='move';window.__subDrag={proj:pit?pit.dataset.name:'',key:chip.dataset.sub};chip.classList.add('pdrag');return}
 const it=e.target.closest('.projitem');if(!it||!it.dataset.name)return;
 if(e.target.closest('button,select,input,.subrow')){e.preventDefault();return}
 e.dataTransfer.setData('text/x-studio-proj',it.dataset.name);e.dataTransfer.effectAllowed='move';it.classList.add('pdrag');window.__projMoved=false;window.__projReordered=false});
$('projGrid').addEventListener('dragend',()=>{[...$('projGrid').querySelectorAll('.pdrag,.dropt,.subdropt')].forEach(x=>x.classList.remove('pdrag','dropt','subdropt'));window.__subDrag=null;
 if(window.__projMoved&&!window.__projReordered){loadProjects().then(openProjModal);}  // arrastre cancelado a mitad: restaurar orden real
 window.__projMoved=false;window.__projReordered=false});
$('projGrid').addEventListener('dragover',e=>{const types=[...e.dataTransfer.types];
 if(types.indexOf('text/x-sub')>=0){const chip=e.target.closest('.subchipp');if(!chip||!window.__subDrag)return;const pit=chip.closest('.projitem');if(!pit||pit.dataset.name!==window.__subDrag.proj)return;e.preventDefault();[...$('projGrid').querySelectorAll('.subdropt')].forEach(x=>x.classList.remove('subdropt'));if(chip.dataset.sub!==window.__subDrag.key)chip.classList.add('subdropt');return}
 if(types.indexOf('text/x-studio-proj')<0)return;e.preventDefault();
 const dragging=$('projGrid').querySelector('.projitem.pdrag');if(!dragging)return;
 const it=e.target.closest('.projitem');if(!it||it===dragging||!it.dataset.name)return;
 const r=it.getBoundingClientRect(),after=e.clientX>(r.left+r.width/2),ref=after?it.nextElementSibling:it;
 if(ref!==dragging&&dragging.nextElementSibling!==ref){
  flipMove($('projGrid'),'.projitem',()=>{$('projGrid').insertBefore(dragging,ref)});window.__projMoved=true;}});
$('projGrid').addEventListener('dragleave',e=>{const it=e.target.closest('.projitem');if(it&&!it.contains(e.relatedTarget))it.classList.remove('dropt');const ch=e.target.closest('.subchipp');if(ch&&!ch.contains(e.relatedTarget))ch.classList.remove('subdropt')});
$('projGrid').addEventListener('drop',async e=>{
 if([...e.dataTransfer.types].indexOf('text/x-sub')>=0&&window.__subDrag){e.preventDefault();const sd=window.__subDrag;const chip=e.target.closest('.subchipp');[...$('projGrid').querySelectorAll('.subdropt,.pdrag')].forEach(x=>x.classList.remove('subdropt','pdrag'));if(!chip)return;const pit=chip.closest('.projitem');if(!pit||pit.dataset.name!==sd.proj)return;const tgt=chip.dataset.sub;if(tgt===sd.key)return;
  const ord=[...pit.querySelectorAll('.subchipp')].map(c=>c.dataset.sub).filter(k=>k!==sd.key);const i=ord.indexOf(tgt);ord.splice(i<0?ord.length:i,0,sd.key);
  const r=await(await fetch('/suborder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:sd.proj,order:ord})})).json();
  if(r&&r.error){toast(r.error,'bad')}else{await loadProjects();openProjModal();toast('Subproyectos reordenados')}return}});
$('projGrid').addEventListener('drop',async e=>{const src=e.dataTransfer.getData('text/x-studio-proj');if(!src)return;e.preventDefault();
 [...$('projGrid').querySelectorAll('.pdrag,.dropt')].forEach(x=>x.classList.remove('pdrag','dropt'));
 window.__projReordered=true;
 const order=[...$('projGrid').querySelectorAll('.projitem')].map(x=>x.dataset.name).filter(n=>n);
 const r=await(await fetch('/projorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order})})).json();
 await loadProjects();openProjModal();
 toast((r&&r.error)?r.error:'Proyectos reordenados ✓',(r&&r.error)?'bad':'')});
$('saveProj').onclick=async()=>{const n=$('projSel').value;if(!projects[n])return;
 stashStyle();
 await fetch('/project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,style:projects[n].style||'',style_video:projects[n].style_video||''})});
 toast('Estilos guardados (imagen y video)')};
$('distill').onclick=async()=>{const n=$('projSel').value;$('distill').textContent='…';
 const r=await(await fetch('/distill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:n})})).json();$('distill').innerHTML='Destilar';
 if(r.error){toast(r.error,'bad');return}$('style').value=r.style;toast('Estilo destilado · revisa y guarda')};
async function postRef(project,name,b64){
 await fetch('/projectref',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project,image:{name,b64}})})}
// resuelve un drop a [{name,b64}] desde cualquier fuente: resultado, historial, estante o archivos del SO
async function imagesFromDT(dt){const out=[];
 const multi=dt.getData('text/x-studio-files');
 const uri=dt.getData('text/x-studio-b64'),hist=dt.getData('text/x-studio-file'),shelf=dt.getData('text/x-studio-shelf');
 const fsub=dt.getData('text/x-studio-filesub')||'';
 if(multi){try{const arr=JSON.parse(multi);for(const e of arr){const b=await(await fetch('/file?name='+encodeURIComponent(e.file)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(e.sub||''))).blob();out.push({name:e.file,b64:await blobToB64(b)})}return out}catch(_){}}
 const multiShelf=dt.getData('text/x-studio-shelfs');
 if(multiShelf){try{const arr=JSON.parse(multiShelf);for(const it of arr){const b=await(await fetch('/shelffile?name='+encodeURIComponent(it.file)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(it.sub||''))).blob();out.push({name:it.file,b64:await blobToB64(b)})}return out}catch(_){}}
 if(uri){const c=uri.indexOf(',');out.push({name:'generada.png',b64:c>=0?uri.slice(c+1):uri});}
 else if(hist){const b=await(await fetch('/file?name='+encodeURIComponent(hist)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(fsub))).blob();out.push({name:hist,b64:await blobToB64(b)});}
 else if(shelf){const ssub=dt.getData('text/x-studio-shelfsub')||'';const b=await(await fetch('/shelffile?name='+encodeURIComponent(shelf)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(ssub))).blob();out.push({name:shelf,b64:await blobToB64(b)});}
 else for(const f of dt.files){if(f.type&&f.type.startsWith('image/'))out.push({name:f.name,b64:await fileToB64(f)});}
 return out;}
$('dropPref').onclick=()=>{$('prefFile').click()};
$('prefFile').onchange=async e=>{const list=e.target.files;e.target.value='';await routeRefFiles(list,'pref')};
['dragover','dragenter'].forEach(ev=>$('dropPref').addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();$('dropPref').classList.add('hot')}));
$('dropPref').addEventListener('dragleave',e=>{e.preventDefault();$('dropPref').classList.remove('hot')});
$('dropPref').addEventListener('drop',async e=>{e.preventDefault();e.stopPropagation();$('dropPref').classList.remove('hot');$('drop').classList.remove('hot');
 const n=$('projSel').value,lbl=n||genLabel;
 const vid=[...(e.dataTransfer.files||[])].find(isVideoFile);
 if(vid){openVideoFrames(vid,'pref');return}
 const imgs=await imagesFromDT(e.dataTransfer);
 for(const im of imgs)await postRef(n,im.name,im.b64);
 await loadProjects();
 if(imgs.length)toast(imgs.length+(imgs.length>1?' referencias añadidas':' referencia añadida')+' a la memoria de "'+lbl+'"')});
$('prefThumbs').onclick=async e=>{const b=e.target.closest('.x');const n=$('projSel').value;
 if(b){await fetch('/projectrefdel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:n,file:b.dataset.f})});await loadProjects();return}
 const t=e.target.closest('.thumb');if(t&&t.dataset.f)openLb('/pfile?project='+encodeURIComponent(n)+'&name='+encodeURIComponent(t.dataset.f),'',null)};

// ===== historial =====
const GDL='<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>';
const GCP='<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const GSHARE='<svg viewBox="0 0 24 24"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg>';
const GPL='<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>';
const GTR='<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>';
const GST='<svg viewBox="0 0 24 24"><path d="M12 3l2.4 5.9 6.1.4-4.7 4 1.5 6-5.3-3.3L6.7 19.3l1.5-6-4.7-4 6.1-.4z"/></svg>';
const GCM='<svg viewBox="0 0 24 24"><rect x="3" y="5" width="8" height="14" rx="2"/><rect x="13" y="5" width="8" height="14" rx="2"/></svg>';
const GIT='<svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>';
const GLB='<svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><path d="M11 7h5"/></svg>';
// ===== etiquetas de color (multi-color) en imágenes =====
const IMGCOLS=['r','y','g','b'];
let galColFilter=new Set(),shelfColFilter=new Set();
function colDots(arr){const a=(arr||[]).filter(c=>IMGCOLS.includes(c));return a.length?'<div class="cdots">'+a.map(c=>'<span class="cdot '+c+'"></span>').join('')+'</div>':''}
function colPick(arr){const s=arr||[];return '<div class="cpick">'+IMGCOLS.map(c=>'<button class="cpdot '+c+(s.includes(c)?' on':'')+'" data-col="'+c+'" title="Etiqueta de color" tabindex="-1"></button>').join('')+'</div>'}
function toggleCol(arr,c){arr=(arr||[]).slice();const i=arr.indexOf(c);if(i>=0)arr.splice(i,1);else arr.push(c);return arr}
function updCdots(card,colors){const html=(colors||[]).filter(c=>IMGCOLS.includes(c)).map(c=>'<span class="cdot '+c+'"></span>').join('');let cd=card.querySelector('.cdots');if(html){if(!cd){cd=document.createElement('div');cd.className='cdots';card.insertBefore(cd,card.children[1]||null)}cd.innerHTML=html}else if(cd)cd.remove()}
function setImgColors(file,colors,scope,sub){fetch('/imgcolors',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:file,colors:colors,scope:scope,project:curProj(),sub:sub||''})}).catch(()=>{})}
function flashCard(card,c,srcEl,adding){if(!card||!IMGCOLS.includes(c))return;
 const cr=card.getBoundingClientRect(),sr=(srcEl||card).getBoundingClientRect();
 const cx=sr.left+sr.width/2-cr.left,cy=sr.top+sr.height/2-cr.top;
 const R=Math.hypot(Math.max(cx,cr.width-cx),Math.max(cy,cr.height-cy));
 const f=document.createElement('div');f.className='cflash '+c+' '+(adding?'cin':'cout');
 f.style.left=cx+'px';f.style.top=cy+'px';f.style.width=f.style.height=(R*2)+'px';f.style.marginLeft=f.style.marginTop=(-R)+'px';
 card.appendChild(f);const done=()=>f.remove();f.addEventListener('animationend',done);setTimeout(done,800)}
function galFiltered(){const q=$('galSearch').value.trim().toLowerCase();
 const fav=$('galFavBtn').classList.contains('on');
 let imgs=hist.filter(it=>!['tts','stt','sfx','vid'].includes(it.kind));
 if(q)imgs=imgs.filter(it=>(it.prompt||'').toLowerCase().includes(q));
 if(fav)imgs=imgs.filter(it=>it.fav);
 if(galColFilter.size)imgs=imgs.filter(it=>(it.colors||[]).some(c=>galColFilter.has(c)));
 return imgs}
$('galSearch').oninput=()=>{shown=30;renderGal()};
$('galFavBtn').onclick=()=>{$('galFavBtn').classList.toggle('on');shown=30;renderGal()};
$('galColFilt').onclick=e=>{const b=e.target.closest('.cfdot');if(!b)return;const c=b.dataset.col;if(galColFilter.has(c))galColFilter.delete(c);else galColFilter.add(c);b.classList.toggle('on');shown=30;renderGal()};
$('shelfColFilt').onclick=e=>{const b=e.target.closest('.cfdot');if(!b)return;const c=b.dataset.col;if(shelfColFilter.has(c))shelfColFilter.delete(c);else shelfColFilter.add(c);b.classList.toggle('on');renderShelf()};
function curProj(){return $('projSel')?($('projSel').value||''):''}
$('galAll').onclick=()=>{const p=encodeURIComponent(curProj()),fav=$('galFavBtn').classList.contains('on');
 const sp=galSubs.has('all')?'&subs=all':('&subs='+encodeURIComponent([...galSubs].join(',')));
 window.open('/galeria?'+(fav?'fav=1&':'')+'project='+p+sp,'_blank','noopener');};
// las ventanas "Ver todo" dejan imágenes en el servidor (/stage); el estudio las recoge (real, no depende del navegador)
async function addRefFromServer(src,file,project,sub){try{
 const pq='&project='+encodeURIComponent(project||'')+'&sub='+encodeURIComponent(sub||'');
 const url=(src==='shelf'?'/shelffile?name=':'/file?name=')+encodeURIComponent(file)+pq;
 const b=await(await fetch(url)).blob();
 refs.push({name:file,b64:await blobToB64(b)});renderThumbs();
 if(mode!=='editar')setMode('editar');validate();toast('Imagen añadida como referencia (desde Ver todo)');
}catch(e){toast('No pude añadir la referencia','bad')}}
async function pollStage(){try{const r=await(await fetch('/stage')).json();
 if(r.items&&r.items.length)for(const it of r.items)await addRefFromServer(it.src,it.file,it.project,it.sub);}catch(e){}}
// la ventana "Biblioteca de prompts" envía aquí el prompt compuesto
async function pollPromptStage(){try{const r=await(await fetch('/promptstage')).json();
 if(r.items&&r.items.length){const p=r.items[r.items.length-1].prompt||'';
  if(p){if(typeof setMode==='function')setMode(lastImgMode);$('prompt').value=p;
   $('prompt').dispatchEvent(new Event('input',{bubbles:true}));$('prompt').focus();
   toast('Prompt recibido de la biblioteca')}}}catch(e){}}
$('promptLibBtn').onclick=()=>window.open('/biblioteca','_blank','noopener');
async function sendPromptToLib(prompt){try{const r=await(await fetch('/promptinbox',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt})})).json();
 toast(r&&r.ok?'Prompt enviado a la biblioteca':((r&&r.error)||'No se pudo enviar'),r&&r.ok?'':'bad')}catch(e){toast('No se pudo enviar','bad')}}
// ===== buscador rápido de prompts de la biblioteca (Cmd/Ctrl+K) =====
let cmdkAll=[],cmdkItems=[],cmdkIdx=0;
async function openCmdk(){let d;try{d=await(await fetch('/promptlib')).json()}catch(e){d={items:[]}}
 cmdkAll=Array.isArray(d.items)?d.items:[];$('cmdk').classList.remove('hide');$('cmdkq').value='';cmdkRender('');$('cmdkq').focus()}
function closeCmdk(){$('cmdk').classList.add('hide')}
function cmdkRender(q){q=(q||'').trim().toLowerCase();
 cmdkItems=cmdkAll.filter(it=>!q||(((it.title||'')+' '+(it.text||'')).toLowerCase().includes(q))).slice(0,60);cmdkIdx=0;
 $('cmdkList').innerHTML=cmdkItems.length?cmdkItems.map((it,i)=>`<div class="cmdkrow${i===0?' sel':''}" data-i="${i}">${it.title?'<b>'+esc(it.title)+'</b> · ':''}${esc((it.text||'').slice(0,140))}</div>`).join(''):'<div class="cmdkempty">Sin resultados en tu biblioteca</div>'}
function cmdkSel(){[...document.querySelectorAll('#cmdkList .cmdkrow')].forEach((r,i)=>r.classList.toggle('sel',i===cmdkIdx));const s=document.querySelector('#cmdkList .cmdkrow.sel');if(s)s.scrollIntoView({block:'nearest'})}
function cmdkInsert(i){const it=cmdkItems[i];if(!it)return;if(typeof setMode==='function')setMode(lastImgMode);$('prompt').value=it.text||'';$('prompt').dispatchEvent(new Event('input',{bubbles:true}));$('prompt').focus();closeCmdk();toast('Prompt insertado de la biblioteca')}
$('cmdkq').addEventListener('input',e=>cmdkRender(e.target.value));
$('cmdkq').addEventListener('keydown',e=>{
 if(e.key==='ArrowDown'){e.preventDefault();cmdkIdx=Math.min(cmdkItems.length-1,cmdkIdx+1);cmdkSel()}
 else if(e.key==='ArrowUp'){e.preventDefault();cmdkIdx=Math.max(0,cmdkIdx-1);cmdkSel()}
 else if(e.key==='Enter'){e.preventDefault();cmdkInsert(cmdkIdx)}
 else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();closeCmdk()}});
$('cmdkList').onclick=e=>{const r=e.target.closest('.cmdkrow');if(r)cmdkInsert(+r.dataset.i)};
$('cmdk').addEventListener('click',e=>{if(e.target===$('cmdk'))closeCmdk()});
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&(e.key==='k'||e.key==='K')){e.preventDefault();if($('cmdk').classList.contains('hide'))openCmdk();else closeCmdk()}},true);
function pollAll(){pollStage();pollPromptStage();}
setInterval(pollAll,2500);
window.addEventListener('focus',()=>{pollAll();if(!selMode)loadGal();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){pollAll();if(!selMode)loadGal();}});
function gcardHtml(it){const fn=encodeURIComponent(it.file),p=esc(it.prompt||''),sb=esc(it._sub||'');
 const pq='&project='+encodeURIComponent(curProj())+(it._sub?'&sub='+encodeURIComponent(it._sub):'');
 const drg=(selMode&&!selFiles.has(it.file))?'false':'true';  // en selección, solo las SELECCIONADAS se arrastran (a Referencias); las demás no, para que el recuadro reciba el puntero
 return `<div class="gcard${selFiles.has(it.file)?' sel':''}" data-file="${esc(it.file)}" data-sub="${sb}" data-p="${p}" draggable="${drg}"><img src="/file?name=${fn}${pq}&thumb=1" alt="${p.slice(0,60)}" title="${p}&#10;(arrástrame a Referencias, Mis imágenes o Memoria visual)" loading="lazy" draggable="${drg}">
   ${colDots(it.colors)}${colPick(it.colors)}
   <div class="gfloat"><button class="gfbtn gstar${it.fav?' fav':''}" title="${it.fav?'Quitar de favoritas':'Favorita'}">${GST}</button>
   <button class="gfbtn gcmp" title="Comparar A/B (elige dos)">${GCM}</button>
   <button class="gfbtn giter" title="Iterar: editar con un cambio">${GIT}</button>
   <a class="gfbtn" href="/file?name=${fn}${pq}" download="${esc(it.file)}" title="Descargar">${GDL}</a>
   <button class="gfbtn gcopy" title="Copiar prompt + cargar las referencias que se usaron">${GCP}</button>
   <button class="gfbtn glib" title="Enviar prompt a la biblioteca">${GLB}</button>
   <button class="gfbtn gref" title="Usar como referencia">${GPL}</button>
   <button class="gfbtn gshare" title="Compartir · WhatsApp, Telegram, redes…">${GSHARE}</button>
   <button class="gfbtn gdel" title="Borrar (doble clic)">${GTR}</button></div>
   <div class="c"><span>$${(it.cost||0).toFixed(4)}</span><span>${esc(it.size||'')}</span></div></div>`}
function renderGal(){const items=galFiltered();
 if(histGroups.length>1){
  const subs=curSubs();
  $('gal').innerHTML=histGroups.map(g=>{
   let gi=(g.items||[]).filter(it=>!['tts','stt','sfx','vid'].includes(it.kind));
   const q=$('galSearch').value.trim().toLowerCase();const fav=$('galFavBtn').classList.contains('on');
   if(q)gi=gi.filter(it=>(it.prompt||'').toLowerCase().includes(q));
   if(fav)gi=gi.filter(it=>it.fav);
   if(galColFilter.size)gi=gi.filter(it=>(it.colors||[]).some(c=>galColFilter.has(c)));
   const lbl=g.k===''?'Raíz':((subs.find(s=>s.key===g.k)||{}).label||g.k);
   const inner=gi.map(it=>gcardHtml(Object.assign({},it,{_sub:g.k}))).join('')||'<div class="hint">Vacío</div>';
   return `<div class="histgroup"><div class="histgrouphdr">${esc(lbl)}</div>${inner}</div>`}).join('')
   ||'<div class="hint">Aún no hay imágenes</div>';
 }else{
  $('gal').innerHTML=items.map(it=>gcardHtml(it)).join('')
   ||'<div class="hint">Aún no hay imágenes en este proyecto</div>';
 }
 $('galMore').classList.add('hide');
 $('gal').classList.toggle('selmode',selMode);
 $('galCount').textContent=items.length||''}
// ===== selección múltiple del historial =====
let selMode=false;const selFiles=new Set();
let galMarqueed=false,galMarq=null,galMqStart=null,galMqMoved=false,galAnchor=-1;
function selFileSub(f){const c=$('gal').querySelector('.gcard[data-file="'+(window.CSS&&CSS.escape?CSS.escape(f):f)+'"]');return c?(c.dataset.sub||''):(activeSub||'')}
// destinos de "Mover": cada proyecto (raíz) + cada subproyecto de cada proyecto
function moveTargets(){const out=[];const list=Object.keys(window.SUBS||{});
 for(const n of list){const lbl=n||genLabel;out.push({project:n,sub:'',label:lbl});
  for(const s of (window.SUBS[n]||[]))out.push({project:n,sub:s.key,label:lbl+' › '+s.label})}
 return out}
function closeMovePop(){const e=$('movePop');if(e)e.remove()}
document.addEventListener('click',e=>{const mp=$('movePop');if(mp&&!mp.contains(e.target)&&!e.target.closest('#bulkMove'))closeMovePop()});
async function bulkMoveTo(dest,dest_sub,destSrc){destSrc=destSrc||'history';
 // agrupa la selección por sub de origen (al ver "Todos" puede haber varios)
 const proj=curProj();const byd={};for(const f of selFiles){const s=selFileSub(f);(byd[s]=byd[s]||[]).push(f)}
 let moved=0;const groups=[];
 for(const ssub of Object.keys(byd)){
  if(destSrc==='history'&&proj===dest&&ssub===dest_sub)continue;
  const r=await jpost('/moveitem',{src:'history',files:byd[ssub],project:proj,sub:ssub,dest:dest,dest_sub:dest_sub,dest_src:destSrc,mode:'move'});
  if(r&&r.error){toast(r.error,'bad');continue}moved+=byd[ssub].length;
  if(r&&r.pairs)groups.push({a:{p:proj,s:ssub,k:'history'},b:{p:dest,s:dest_sub,k:destSrc},names:r.pairs.map(x=>x.to),at:'b'})}
 closeMovePop();selMode=false;selFiles.clear();await loadGal();if(destSrc==='shelf')loadShelf();renderBulk();
 if(moved){const dst=destSrc==='shelf'?'Mis imágenes':'el proyecto';toast(moved+' movida(s) a '+dst+' · ⌘Z para deshacer');
  pushUndo({label:moved+' movida(s)',
   undo:async()=>{for(const g of groups){if(g.at!=='b')continue;const r=await jpost('/moveitem',{src:g.b.k,files:g.names,project:g.b.p,sub:g.b.s,dest:g.a.p,dest_sub:g.a.s,dest_src:g.a.k,mode:'move'});if(r&&r.pairs){g.names=r.pairs.map(x=>x.to);g.at='a'}}await loadGal();loadShelf();},
   redo:async()=>{for(const g of groups){if(g.at!=='a')continue;const r=await jpost('/moveitem',{src:g.a.k,files:g.names,project:g.a.p,sub:g.a.s,dest:g.b.p,dest_sub:g.b.s,dest_src:g.b.k,mode:'move'});if(r&&r.pairs){g.names=r.pairs.map(x=>x.to);g.at='b'}}await loadGal();loadShelf();}})}}
// copiar (duplicar) la selección del Historial a otro proyecto/sub (Historial o Mis imágenes), con deshacer
async function bulkCopyTo(dest,dest_sub,destSrc){destSrc=destSrc||'history';
 const proj=curProj();const byd={};for(const f of selFiles){const s=selFileSub(f);(byd[s]=byd[s]||[]).push(f)}
 let done=0;const groups=[];
 for(const ssub of Object.keys(byd)){
  const r=await jpost('/moveitem',{src:'history',files:byd[ssub],project:proj,sub:ssub,dest:dest,dest_sub:dest_sub,dest_src:destSrc,mode:'copy'});
  if(r&&r.error){toast(r.error,'bad');continue}
  if(r&&r.pairs){done+=r.pairs.length;groups.push({srcP:proj,srcS:ssub,srcFiles:byd[ssub],dstP:dest,dstS:dest_sub,dstK:destSrc,names:r.pairs.map(x=>x.to)});}}
 closeMovePop();selMode=false;selFiles.clear();await loadGal();if(destSrc==='shelf')loadShelf();renderBulk();
 if(!done){toast('No se pudo copiar','bad');return}
 toast(done+' copiada(s) a '+(destSrc==='shelf'?'Mis imágenes':'el proyecto')+' · ⌘Z para deshacer');
 pushUndo({label:done+' copiada(s)',
  undo:async()=>{for(const g of groups){await jpost('/deleteitems',{src:g.dstK,project:g.dstP,sub:g.dstS,files:g.names})}await loadGal();if(destSrc==='shelf')loadShelf();},
  redo:async()=>{for(const g of groups){const r=await jpost('/moveitem',{src:'history',files:g.srcFiles,project:g.srcP,sub:g.srcS,dest:g.dstP,dest_sub:g.dstS,dest_src:g.dstK,mode:'copy'});if(r&&r.pairs)g.names=r.pairs.map(x=>x.to)}await loadGal();if(destSrc==='shelf')loadShelf();}})}
function openMovePop(anchor,mode){mode=mode||'move';closeMovePop();
 const tgts=moveTargets();let pdest='history';
 const pop=document.createElement('div');pop.className='movepop';pop.id='movePop';
 pop.innerHTML='<div class="mphdr">'+trVal(mode==='copy'?'Copiar a…':'Mover a…',LANG)+'</div><div class="mpdest"><button data-d="history" class="on">'+trVal('Historial',LANG)+'</button><button data-d="shelf">'+trVal('Mis imágenes',LANG)+'</button></div>'+tgts.map((t,i)=>'<button class="mpopt" data-i="'+i+'">'+esc(t.label)+'</button>').join('');
 document.body.appendChild(pop);
 const r=anchor.getBoundingClientRect();const popH=pop.offsetHeight,popW=pop.offsetWidth;
 let top=r.bottom+6;if(top+popH>window.innerHeight-8)top=Math.max(8,r.top-popH-6);
 pop.style.left=Math.max(8,Math.min(r.left,window.innerWidth-popW-12))+'px';pop.style.top=top+'px';
 pop.onclick=e=>{const d=e.target.closest('.mpdest button');if(d){pdest=d.dataset.d;[...pop.querySelectorAll('.mpdest button')].forEach(x=>x.classList.toggle('on',x===d));return}const b=e.target.closest('.mpopt');if(!b)return;const t=tgts[+b.dataset.i];(mode==='copy'?bulkCopyTo:bulkMoveTo)(t.project,t.sub,pdest)}}
// mover UNA imagen de Mis imágenes (estante) a otro proyecto/sub (o al Historial), con deshacer
async function shelfMoveOne(file,srcSub,dest,dest_sub,destSrc){const proj=curProj();
 if(destSrc==='shelf'&&proj===dest&&srcSub===dest_sub){toast('Ya está en ese lugar');return}
 const r=await jpost('/moveitem',{src:'shelf',files:[file],project:proj,sub:srcSub,dest:dest,dest_sub:dest_sub,dest_src:destSrc,mode:'move'});
 if(r&&r.error){toast(r.error,'bad');return}
 await loadShelf();if(destSrc==='history')await loadGal();
 const g={a:{p:proj,s:srcSub,k:'shelf'},b:{p:dest,s:dest_sub,k:destSrc},names:(r.pairs||[]).map(p=>p.to),at:'b'};
 toast('Movida a '+(destSrc==='shelf'?'Mis imágenes':'Historial')+' · ⌘Z para deshacer');
 pushUndo({label:'1 movida',
  undo:async()=>{const rr=await jpost('/moveitem',{src:g.b.k,files:g.names,project:g.b.p,sub:g.b.s,dest:g.a.p,dest_sub:g.a.s,dest_src:g.a.k,mode:'move'});if(rr&&rr.pairs)g.names=rr.pairs.map(x=>x.to);await loadShelf();await loadGal();},
  redo:async()=>{const rr=await jpost('/moveitem',{src:g.a.k,files:g.names,project:g.a.p,sub:g.a.s,dest:g.b.p,dest_sub:g.b.s,dest_src:g.b.k,mode:'move'});if(rr&&rr.pairs)g.names=rr.pairs.map(x=>x.to);await loadShelf();await loadGal();}})}
async function shelfMoveMany(arr,dest_sub){const proj=curProj();
 const byd={};arr.forEach(it=>{const s=it.sub||'';if(s!==dest_sub)(byd[s]=byd[s]||[]).push(it.file)});
 const groups=[];let moved=0;
 for(const s of Object.keys(byd)){const r=await jpost('/moveitem',{src:'shelf',files:byd[s],project:proj,sub:s,dest:proj,dest_sub:dest_sub,dest_src:'shelf',mode:'move'});
  if(r&&r.error){toast(r.error,'bad');continue}
  if(r&&r.pairs){moved+=r.pairs.length;groups.push({srcSub:s,names:r.pairs.map(p=>p.to)});}}
 shelfSelMode=false;shelfSel.clear();renderShelfBulk();await loadShelf();
 if(!moved){toast('Ya estaban en ese subproyecto');return}
 const subs=curSubs(),lbl=dest_sub?((subs.find(s=>s.key===dest_sub)||{}).label||dest_sub):'Raíz';
 toast(moved+' movida(s) a '+lbl+' · ⌘Z para deshacer');
 pushUndo({label:moved+' movida(s)',
  undo:async()=>{for(const g of groups){const rr=await jpost('/moveitem',{src:'shelf',files:g.names,project:proj,sub:dest_sub,dest:proj,dest_sub:g.srcSub,dest_src:'shelf',mode:'move'});if(rr&&rr.pairs)g.names=rr.pairs.map(x=>x.to)}await loadShelf();},
  redo:async()=>{for(const g of groups){const rr=await jpost('/moveitem',{src:'shelf',files:g.names,project:proj,sub:g.srcSub,dest:proj,dest_sub:dest_sub,dest_src:'shelf',mode:'move'});if(rr&&rr.pairs)g.names=rr.pairs.map(x=>x.to)}await loadShelf();}});}
function openShelfMovePop(anchor,file,srcSub){closeMovePop();
 const tgts=moveTargets();let pdest='shelf';
 const pop=document.createElement('div');pop.className='movepop';pop.id='movePop';
 pop.innerHTML='<div class="mphdr">'+trVal('Mover a…',LANG)+'</div><div class="mpdest"><button data-d="shelf" class="on">'+trVal('Mis imágenes',LANG)+'</button><button data-d="history">'+trVal('Historial',LANG)+'</button></div>'+tgts.map((t,i)=>'<button class="mpopt" data-i="'+i+'">'+esc(t.label)+'</button>').join('');
 document.body.appendChild(pop);
 const r=anchor.getBoundingClientRect();const popH=pop.offsetHeight,popW=pop.offsetWidth;
 let top=r.bottom+6;if(top+popH>window.innerHeight-8)top=Math.max(8,r.top-popH-6);
 pop.style.left=Math.max(8,Math.min(r.left,window.innerWidth-popW-12))+'px';pop.style.top=top+'px';
 pop.onclick=e=>{const d=e.target.closest('.mpdest button');if(d){pdest=d.dataset.d;[...pop.querySelectorAll('.mpdest button')].forEach(x=>x.classList.toggle('on',x===d));return}const b=e.target.closest('.mpopt');if(!b)return;const t=tgts[+b.dataset.i];closeMovePop();shelfMoveOne(file,srcSub,t.project,t.sub,pdest)}}
// mover en LOTE la selección de Mis imágenes a cualquier proyecto/sub (Mis imágenes o Historial)
async function bulkShelfMoveTo(dest,dest_sub,destSrc){destSrc=destSrc||'shelf';
 const proj=curProj();const byd={};for(const f of shelfSel){const s=shelfFileSub(f);(byd[s]=byd[s]||[]).push(f)}
 let moved=0;const groups=[];
 for(const ssub of Object.keys(byd)){
  if(destSrc==='shelf'&&proj===dest&&ssub===dest_sub)continue;
  const r=await jpost('/moveitem',{src:'shelf',files:byd[ssub],project:proj,sub:ssub,dest:dest,dest_sub:dest_sub,dest_src:destSrc,mode:'move'});
  if(r&&r.error){toast(r.error,'bad');continue}moved+=byd[ssub].length;
  if(r&&r.pairs)groups.push({a:{p:proj,s:ssub,k:'shelf'},b:{p:dest,s:dest_sub,k:destSrc},names:r.pairs.map(x=>x.to),at:'b'})}
 closeMovePop();shelfSelMode=false;shelfSel.clear();$('shelfSelBtn').classList.remove('on');await loadShelf();if(destSrc==='history')await loadGal();renderShelfBulk();
 if(!moved){toast('Ya estaban en ese lugar');return}
 toast(moved+' movida(s) · ⌘Z para deshacer');
 pushUndo({label:moved+' movida(s)',
  undo:async()=>{for(const g of groups){if(g.at!=='b')continue;const r=await jpost('/moveitem',{src:g.b.k,files:g.names,project:g.b.p,sub:g.b.s,dest:g.a.p,dest_sub:g.a.s,dest_src:g.a.k,mode:'move'});if(r&&r.pairs){g.names=r.pairs.map(x=>x.to);g.at='a'}}await loadShelf();loadGal();},
  redo:async()=>{for(const g of groups){if(g.at!=='a')continue;const r=await jpost('/moveitem',{src:g.a.k,files:g.names,project:g.a.p,sub:g.a.s,dest:g.b.p,dest_sub:g.b.s,dest_src:g.b.k,mode:'move'});if(r&&r.pairs){g.names=r.pairs.map(x=>x.to);g.at='b'}}await loadShelf();loadGal();}})}
// copiar (duplicar) la selección de Mis imágenes a otro proyecto/sub (Mis imágenes o Historial), con deshacer
async function bulkShelfCopyTo(dest,dest_sub,destSrc){destSrc=destSrc||'shelf';
 const proj=curProj();const byd={};for(const f of shelfSel){const s=shelfFileSub(f);(byd[s]=byd[s]||[]).push(f)}
 let done=0;const groups=[];
 for(const ssub of Object.keys(byd)){
  const r=await jpost('/moveitem',{src:'shelf',files:byd[ssub],project:proj,sub:ssub,dest:dest,dest_sub:dest_sub,dest_src:destSrc,mode:'copy'});
  if(r&&r.error){toast(r.error,'bad');continue}
  if(r&&r.pairs){done+=r.pairs.length;groups.push({srcP:proj,srcS:ssub,srcFiles:byd[ssub],dstP:dest,dstS:dest_sub,dstK:destSrc,names:r.pairs.map(x=>x.to)});}}
 closeMovePop();shelfSelMode=false;shelfSel.clear();$('shelfSelBtn').classList.remove('on');await loadShelf();if(destSrc==='history')await loadGal();renderShelfBulk();
 if(!done){toast('No se pudo copiar','bad');return}
 toast(done+' copiada(s) a '+(destSrc==='history'?'Historial':'Mis imágenes')+' · ⌘Z para deshacer');
 pushUndo({label:done+' copiada(s)',
  undo:async()=>{for(const g of groups){await jpost('/deleteitems',{src:g.dstK,project:g.dstP,sub:g.dstS,files:g.names})}await loadShelf();if(destSrc==='history')loadGal();},
  redo:async()=>{for(const g of groups){const r=await jpost('/moveitem',{src:'shelf',files:g.srcFiles,project:g.srcP,sub:g.srcS,dest:g.dstP,dest_sub:g.dstS,dest_src:g.dstK,mode:'copy'});if(r&&r.pairs)g.names=r.pairs.map(x=>x.to)}await loadShelf();if(destSrc==='history')loadGal();}})}
function openShelfBulkMovePop(anchor,mode){mode=mode||'move';closeMovePop();
 const tgts=moveTargets();let pdest='shelf';
 const pop=document.createElement('div');pop.className='movepop';pop.id='movePop';
 pop.innerHTML='<div class="mphdr">'+trVal(mode==='copy'?'Copiar a…':'Mover a…',LANG)+'</div><div class="mpdest"><button data-d="shelf" class="on">'+trVal('Mis imágenes',LANG)+'</button><button data-d="history">'+trVal('Historial',LANG)+'</button></div>'+tgts.map((t,i)=>'<button class="mpopt" data-i="'+i+'">'+esc(t.label)+'</button>').join('');
 document.body.appendChild(pop);
 const r=anchor.getBoundingClientRect();const popH=pop.offsetHeight,popW=pop.offsetWidth;
 let top=r.bottom+6;if(top+popH>window.innerHeight-8)top=Math.max(8,r.top-popH-6);
 pop.style.left=Math.max(8,Math.min(r.left,window.innerWidth-popW-12))+'px';pop.style.top=top+'px';
 pop.onclick=e=>{const d=e.target.closest('.mpdest button');if(d){pdest=d.dataset.d;[...pop.querySelectorAll('.mpdest button')].forEach(x=>x.classList.toggle('on',x===d));return}const b=e.target.closest('.mpopt');if(!b)return;const t=tgts[+b.dataset.i];closeMovePop();(mode==='copy'?bulkShelfCopyTo:bulkShelfMoveTo)(t.project,t.sub,pdest)}}
function renderBulk(){const bar=$('galBulk');if(!selMode){bar.classList.add('hide');closeMovePop();return}
 if(bar.parentNode!==document.body)document.body.appendChild(bar);  // fixed relativo al viewport (un ancestro con transform lo descentraba)
 bar.classList.remove('hide');
 bar.innerHTML='<span class="gbcount">'+selFiles.size+' seleccionada'+(selFiles.size===1?'':'s')+'</span>'
  +'<button id="bulkAll" title="Seleccionar todas"><svg viewBox="0 0 24 24" style="width:15px;height:15px"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="M8 12l2.8 2.8L16.5 9"/></svg>Todo</button>'
  +'<button id="bulkNone" title="Deseleccionar todas"><svg viewBox="0 0 24 24" style="width:15px;height:15px"><rect x="3" y="3" width="18" height="18" rx="4"/></svg>Ninguna</button>'
  +'<button id="bulkLib">'+GLB+'A la biblioteca</button>'
  +'<button id="bulkMove">'+GCM+'Mover</button>'
  +'<button id="bulkCopy">'+GCP+'Copiar</button>'
  +'<button id="bulkDel" class="bdel">'+GTR+'Borrar</button>'
  +'<button id="bulkExit">Salir</button>';
 $('bulkAll').onclick=()=>{[...document.querySelectorAll('#gal .gcard')].forEach(c=>selFiles.add(c.dataset.file));renderGal();renderBulk()};
 $('bulkNone').onclick=()=>{selFiles.clear();renderGal();renderBulk()};
 $('bulkMove').onclick=e=>{e.stopPropagation();if(!selFiles.size){toast('Selecciona imágenes primero','bad');return}if($('movePop')){closeMovePop();return}openMovePop(e.currentTarget,'move')};
 $('bulkCopy').onclick=e=>{e.stopPropagation();if(!selFiles.size){toast('Selecciona imágenes primero','bad');return}if($('movePop')){closeMovePop();return}openMovePop(e.currentTarget,'copy')};
 $('bulkExit').onclick=()=>{selMode=false;selFiles.clear();renderGal();renderBulk()};
 $('bulkLib').onclick=async()=>{if(!selFiles.size){toast('Selecciona imágenes primero','bad');return}
  let n=0;for(const f of selFiles){const it=hist.find(x=>x.file===f);if(it&&(it.prompt||'').trim()){try{await fetch('/promptinbox',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:it.prompt})});n++}catch(e){}}}
  toast(n?(n+' prompt(s) enviados a la biblioteca'):'Ninguna tenía prompt',n?'':'bad');selMode=false;selFiles.clear();renderGal();renderBulk()};
 $('bulkDel').onclick=async(e)=>{const b=e.currentTarget;if(!selFiles.size){toast('Selecciona imágenes primero','bad');return}
  if(!b.classList.contains('arm')){b.classList.add('arm');b.lastChild.textContent='¿Borrar '+selFiles.size+'?';setTimeout(()=>{b.classList.remove('arm');renderBulk()},2600);return}
  const proj=curProj();const byd={};for(const f of selFiles){const s=selFileSub(f);(byd[s]=byd[s]||[]).push(f)}
  const groups=[];for(const s of Object.keys(byd)){const r=await jpost('/deleteitems',{src:'history',files:byd[s],project:proj,sub:s});if(r&&r.undo)groups.push({sub:s,items:r.undo})}
  const k=selFiles.size;selMode=false;selFiles.clear();await loadGal();renderBulk();toast(k+' borrada(s) · ⌘Z para deshacer');
  pushUndo({label:k+' borrada(s)',
   undo:async()=>{for(const g of groups){await jpost('/restoreitems',{src:'history',project:proj,sub:g.sub,items:g.items})}await loadGal();},
   redo:async()=>{for(const g of groups){const r=await jpost('/deleteitems',{src:'history',files:g.items.map(u=>(u.entry||{}).file).filter(Boolean),project:proj,sub:g.sub});if(r&&r.undo)g.items=r.undo}await loadGal();}})}}
$('galSelBtn').onclick=()=>{selMode=!selMode;selFiles.clear();
 if(selMode&&typeof shelfSelMode!=='undefined'&&shelfSelMode){shelfSelMode=false;shelfSel.clear();$('shelfSelBtn').classList.remove('on');renderShelf();renderShelfBulk();}
 renderGal();renderBulk()};
// ===== Deshacer / Rehacer (⌘Z / ⇧⌘Z) =====
let undoStack=[],redoStack=[];
async function jpost(u,b){return (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json();}
function pushUndo(op){undoStack.push(op);if(undoStack.length>40)undoStack.shift();redoStack.length=0;}
async function doUndo(){const op=undoStack.pop();if(!op){toast('Nada que deshacer');return}try{await op.undo();redoStack.push(op);toast('Deshecho: '+op.label);}catch(e){toast('No se pudo deshacer','bad');}}
async function doRedo(){const op=redoStack.pop();if(!op){toast('Nada que rehacer');return}try{await op.redo();undoStack.push(op);toast('Rehecho: '+op.label);}catch(e){toast('No se pudo rehacer','bad');}}
document.addEventListener('keydown',e=>{if(!(e.metaKey||e.ctrlKey))return;const t=(document.activeElement||{}).tagName;if(t==='INPUT'||t==='TEXTAREA'||(document.activeElement||{}).isContentEditable)return;const k=(e.key||'').toLowerCase();
 if(k!=='z'&&k!=='y')return;
 // no deshacer con el visor o un modal abierto (evita ⌘Z accidental mientras se mira algo)
 if($('lightbox')&&!$('lightbox').classList.contains('hide'))return;
 if(document.querySelector('.overlay:not(.hide)'))return;
 if(k==='z'){e.preventDefault();e.shiftKey?doRedo():doUndo();}else if(k==='y'){e.preventDefault();doRedo();}});
let galSubs=new Set(['all']),histGroups=[];
function renderGalChips(){const c=$('galSubChips');if(!c)return;const subs=curSubs();
 if(!subs.length){c.innerHTML='';return}
 const chip=(k,lbl,dr)=>`<button class="subchip${galSubs.has(k)?' on':''}${dr?' subdrag':''}" data-k="${esc(k)}"${dr?' draggable="true"':''}>${esc(lbl)}</button>`;
 c.innerHTML=chip('all',trVal('Todos',LANG))+chip('',trVal('Raíz',LANG))+subs.map(s=>chip(s.key,s.label,true)).join('')}
$('galSubChips').onclick=e=>{const b=e.target.closest('.subchip');if(!b)return;const k=b.dataset.k;
 if(k==='all'){galSubs=new Set(['all'])}else{galSubs.delete('all');if(galSubs.has(k))galSubs.delete(k);else galSubs.add(k);if(!galSubs.size)galSubs.add('')}
 renderGalChips();loadGal()};
async function loadGal(){const subs=curSubs();
 let groups;
 if(!subs.length){groups=[{k:activeSub||''}]}
 else if(galSubs.has('all')){groups=[{k:''}].concat(subs.map(s=>({k:s.key})))}
 else{groups=[...galSubs].map(k=>({k}))}
 if(!groups.length)groups=[{k:''}];
 histGroups=[];
 for(const g of groups){const items=await(await fetch('/history?project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(g.k))).json();
  (items||[]).forEach(it=>it._sub=g.k);histGroups.push({k:g.k,items:items||[]})}
 hist=histGroups.length===1?histGroups[0].items:[].concat(...histGroups.map(g=>g.items));
 renderGal();renderAud()}
$('galMore').onclick=()=>{shown+=30;renderGal()};
function _sortKeyCreation(it){const m=(it.file||'').match(/(\d{8})_(\d{6})/);return m?(m[1]+m[2]):((it.ts||'').replace(/\D/g,''));}
async function organizeHist(mode){if(!mode)return;
 for(const g of histGroups){const items=[...g.items];
  if(mode==='new')items.sort((a,b)=>_sortKeyCreation(b).localeCompare(_sortKeyCreation(a)));
  else if(mode==='old')items.sort((a,b)=>_sortKeyCreation(a).localeCompare(_sortKeyCreation(b)));
  else if(mode==='name')items.sort((a,b)=>String(a.prompt||'').toLowerCase().localeCompare(String(b.prompt||'').toLowerCase()));
  const order=items.map(it=>it.file);
  await fetch('/itemsorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({src:'history',project:curProj(),sub:g.k,order})});}
 await loadGal();
 toast(mode==='new'?'Ordenado por fecha · recientes primero':mode==='old'?'Ordenado por fecha · antiguas primero':'Ordenado por nombre del prompt');}
$('galSort').onchange=e=>{const v=e.target.value;e.target.value='';organizeHist(v);};
function blobToB64(b){return new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(b)})}
function markDropZones(on){['drop','dropPref','shelf'].forEach(id=>{const el=$(id);if(el)el.classList.toggle('dzhi',on)})}
window.addEventListener('dragend',()=>markDropZones(false));
window.addEventListener('drop',()=>markDropZones(false));
$('gal').addEventListener('dragstart',e=>{const card=e.target.closest('.gcard');if(!card)return;
 if(selMode&&selFiles.size>1&&selFiles.has(card.dataset.file)){
  const arr=[...$('gal').querySelectorAll('.gcard')].filter(c=>selFiles.has(c.dataset.file)).map(c=>({file:c.dataset.file,sub:c.dataset.sub||''}));
  e.dataTransfer.setData('text/x-studio-files',JSON.stringify(arr));}
 e.dataTransfer.setData('text/x-studio-file',card.dataset.file);
 if(card.dataset.sub)e.dataTransfer.setData('text/x-studio-filesub',card.dataset.sub);
 e.dataTransfer.effectAllowed='copy';markDropZones(true);
 if(!selMode)gridReorderStart(card,$('gal'),'.gcard','file','history');});
$('gal').addEventListener('dragover',e=>gridReorderOver(e,$('gal'),'.gcard'));
$('gal').addEventListener('drop',e=>gridReorderDrop(e,$('gal'),true));
// Marquee: arrastrar un recuadro con el mouse para seleccionar varias (solo en modo selección)
$('gal').addEventListener('pointerdown',e=>{
 if(!selMode||e.button!==0)return;
 if(e.target.closest('a,button'))return;
 const card=e.target.closest('.gcard');
 if(card&&selFiles.has(card.dataset.file))return;  // arrastrar una YA seleccionada = sacar la selección a Referencias (drag nativo)
 e.preventDefault();galMqStart={x:e.clientX,y:e.clientY};galMqMoved=false;});
window.addEventListener('pointermove',e=>{
 if(!selMode||!galMqStart)return;
 const dx=e.clientX-galMqStart.x,dy=e.clientY-galMqStart.y;
 if(!galMqMoved&&Math.abs(dx)+Math.abs(dy)<6)return;
 galMqMoved=true;e.preventDefault();
 if(!galMarq){galMarq=document.createElement('div');galMarq.className='gmarq';document.body.appendChild(galMarq);}
 const x1=Math.min(e.clientX,galMqStart.x),y1=Math.min(e.clientY,galMqStart.y),x2=Math.max(e.clientX,galMqStart.x),y2=Math.max(e.clientY,galMqStart.y);
 galMarq.style.cssText='position:fixed;left:'+x1+'px;top:'+y1+'px;width:'+(x2-x1)+'px;height:'+(y2-y1)+'px;display:block';
 $('gal').querySelectorAll('.gcard').forEach(c=>{const r=c.getBoundingClientRect();
  if(!(r.right<x1||r.left>x2||r.bottom<y1||r.top>y2)&&!selFiles.has(c.dataset.file)){selFiles.add(c.dataset.file);c.classList.add('sel');c.draggable=true;}});
 renderBulk();});
function endGalMarq(){if(!galMqStart)return;if(galMarq){galMarq.remove();galMarq=null;}if(galMqMoved)galMarqueed=true;galMqStart=null;galMqMoved=false;}
window.addEventListener('pointerup',endGalMarq);
window.addEventListener('pointercancel',endGalMarq);
$('gal').onclick=async e=>{
 if(selMode){if(galMarqueed){galMarqueed=false;return}const card=e.target.closest('.gcard');if(card){
   const cards=[...$('gal').querySelectorAll('.gcard')],idx=cards.indexOf(card);
   if(e.shiftKey&&galAnchor>=0&&galAnchor<cards.length){  // Shift: seleccionar el rango de punta a punta
    const lo=Math.min(galAnchor,idx),hi=Math.max(galAnchor,idx);
    for(let i=lo;i<=hi;i++){selFiles.add(cards[i].dataset.file);cards[i].classList.add('sel');cards[i].draggable=true;}
   }else{const f=card.dataset.file;const now=!selFiles.has(f);if(now)selFiles.add(f);else selFiles.delete(f);card.classList.toggle('sel',now);card.draggable=now;galAnchor=idx;}
   renderBulk()}return}
 const gsh=e.target.closest('.gshare');
 if(gsh){e.stopPropagation();const c=e.target.closest('.gcard');if(c){openSharePop(gsh,'/file?name='+encodeURIComponent(c.dataset.file)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(c.dataset.sub||''),c.dataset.file);}return;}
 if(e.target.closest('a'))return;
 const cp=e.target.closest('.gcopy'),rf=e.target.closest('.gref'),del=e.target.closest('.gdel'),
  star=e.target.closest('.gstar'),lib=e.target.closest('.glib'),
  cmp=e.target.closest('.gcmp'),iter=e.target.closest('.giter'),card=e.target.closest('.gcard');
 if(lib){const p=(hist.find(x=>x.file===card.dataset.file)||{}).prompt||card.dataset.p||'';
  if(!p.trim()){toast('Esta imagen no tiene prompt','bad');return}
  sendPromptToLib(p);flash(lib);return}
 if(cmp){if(!cmpA){cmpA=card.dataset.file;cmp.classList.add('fav');toast('A elegida · ahora pulsa comparar en otra imagen')}
  else if(cmpA===card.dataset.file){cmpA=null;cmp.classList.remove('fav');toast('Comparación cancelada')}
  else{openCmp(cmpA,card.dataset.file);cmpA=null;renderGal()}
  return}
 const cardSub=card?(card.dataset.sub||''):'';const fileQ='&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(cardSub);
 if(iter){const b=await(await fetch('/file?name='+encodeURIComponent(card.dataset.file)+fileQ)).blob();
  refs=[{name:card.dataset.file,b64:await blobToB64(b)}];mask=null;renderThumbs();renderMaskThumb();
  setMode('editar');$('prompt').value='';$('prompt').placeholder='Describe solo el cambio: "ahora de noche", "quita el texto", "hazlo acuarela"…';
  $('prompt').focus();toast('Iterando sobre esa imagen · describe el cambio');return}
 if(star){const it=hist.find(x=>x.file===card.dataset.file);if(!it)return;
  it.fav=!it.fav;star.classList.toggle('fav',it.fav);
  fetch('/histfav',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:it.file,fav:it.fav,project:curProj(),sub:cardSub})});
  if($('galFavBtn').classList.contains('on'))renderGal();return}
 const cpd=e.target.closest('.cpdot');
 if(cpd){const active=[...card.querySelectorAll('.cpdot.on')].map(x=>x.dataset.col);const next=toggleCol(active,cpd.dataset.col);
  cpd.classList.toggle('on');updCdots(card,next);flashCard(card,cpd.dataset.col,cpd,next.includes(cpd.dataset.col));
  const it=hist.find(x=>x.file===card.dataset.file);if(it)it.colors=next;
  setImgColors(card.dataset.file,next,'hist',cardSub);if(galColFilter.size)setTimeout(renderGal,460);return}
 if(cp){const it=hist.find(x=>x.file===card.dataset.file)||{};$('prompt').value=card.dataset.p;try{navigator.clipboard.writeText(card.dataset.p)}catch(x){}flash(cp);const n=await useHistRefs(it);toast(n?('Prompt + '+n+' referencia(s) cargadas'):'Prompt copiado');return}
 if(rf){const b=await(await fetch('/file?name='+encodeURIComponent(card.dataset.file)+fileQ)).blob();refs.push({name:card.dataset.file,b64:await blobToB64(b)});renderThumbs();flash(rf);toast('Añadida como referencia');return}
 if(del){
  if(del.classList.contains('arm')){
   await fetch('/historydel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:card.dataset.file,project:curProj(),sub:cardSub})});
   hist=hist.filter(it=>it.file!==card.dataset.file);if(histGroups.length){const g=histGroups.find(x=>x.k===cardSub);if(g)g.items=g.items.filter(it=>it.file!==card.dataset.file)}renderGal();toast('Imagen eliminada')}
  else{del.classList.add('arm');setTimeout(()=>del.classList.remove('arm'),1800)}
  return}
 if(card){openLb('/file?name='+encodeURIComponent(card.dataset.file)+fileQ,card.dataset.p,card.dataset.file);lbScope='gal';lbCurFile=card.dataset.file;lbSyncNav()}};

// ===== lightbox =====
let lbScope=null,lbCurFile=null;   // contexto de navegación con flechas (galería / estante)
function lbNavigate(dir){
 if($('lightbox').classList.contains('hide')||!lbScope||!lbCurFile)return;
 const sel=lbScope==='gal'?'#gal .gcard':'#shelfGrid .scard';
 const attr=lbScope==='gal'?'file':'shelf';
 const cards=[...document.querySelectorAll(sel)];
 const idx=cards.findIndex(c=>c.dataset[attr]===lbCurFile);
 if(idx<0)return;
 const ni=idx+dir;if(ni<0||ni>=cards.length)return;   // sin dar la vuelta
 const c=cards[ni];
 if(lbScope==='gal'){openLb('/file?name='+encodeURIComponent(c.dataset.file)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(c.dataset.sub||''),c.dataset.p||'',c.dataset.file);lbScope='gal';lbCurFile=c.dataset.file;lbSyncNav()}
 else{openLb('/shelffile?name='+encodeURIComponent(c.dataset.shelf)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(c.dataset.sub||''),'','');lbScope='shelf';lbCurFile=c.dataset.shelf;lbSyncNav()}}
async function useHistRefs(it){   // carga en el panel las referencias guardadas con esa imagen del historial
 if(!it||!Array.isArray(it.refs)||!it.refs.length)return 0;
 const sb=it._sub||it.sub||'';const out=[];
 for(const r of it.refs){try{const bl=await(await fetch('/reffile?name='+encodeURIComponent(r.file)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(sb))).blob();out.push({name:r.name||r.file,b64:await blobToB64(bl)})}catch(e){}}
 if(out.length){refs=out;mask=null;renderThumbs();if(typeof renderMaskThumb==='function')renderMaskThumb();if(mode!=='editar')setMode('editar');if(typeof validate==='function')validate();}
 return out.length;}
function openLb(src,p,file){lbScope=null;lbCurFile=null;$('lbImg').src=src;$('lbPrompt').textContent=p||'';
 $('lightbox').dataset.file=file||'';$('lbDesc').style.display=file?'':'none';
 $('lbPrompt').classList.toggle('hide',!p);
 $('lbDl').href=src;$('lbDl').setAttribute('download',file||'imagen.png');
 $('lbUse').style.display=p?'':'none';
 $('lbUse').onclick=async ev=>{ev.stopPropagation();$('prompt').value=p||'';const f=$('lightbox').dataset.file;const it=(typeof hist!=='undefined'&&hist)?hist.find(x=>x.file===f):null;const n=await useHistRefs(it);toast(n?('Prompt y '+n+' referencia(s) cargadas'):'Prompt cargado');};
 $('lbLib').style.display=p?'':'none';
 $('lbLib').onclick=ev=>{ev.stopPropagation();sendPromptToLib(p||'')};
 // ficha técnica (resolución, calidad, modo, costo, tokens, fecha) si es del historial
 const m=$('lbMeta');const it=file?hist.find(x=>x.file===file):null;
 if(it){const QL={high:'Alta',medium:'Media',low:'Baja',auto:'Auto'};const tags=[];
  if(it.size)tags.push('<span><b>Resolución</b>'+esc(it.size)+'</span>');
  if(it.quality)tags.push('<span><b>Calidad</b>'+esc(QL[it.quality]||it.quality)+'</span>');
  if(it.mode)tags.push('<span><b>Modo</b>'+(it.mode==='editar'?'Editar':'Crear')+'</span>');
  if(typeof it.cost==='number')tags.push('<span><b>Costo</b>aprox. $'+(it.cost||0).toFixed(4)+'</span>');
  if(it.output_tokens)tags.push('<span><b>Tokens</b>'+it.output_tokens+'</span>');
  if(it.ts)tags.push('<span>'+esc(it.ts)+'</span>');
  m.innerHTML=tags.join('');m.classList.toggle('hide',!tags.length)}
 else m.classList.add('hide');
 // imágenes que se usaron como referencia para generar esta del historial
 const rf=$('lbRefs');
 if(it&&Array.isArray(it.refs)&&it.refs.length){const sb=it._sub||it.sub||'';
  rf.innerHTML='<span class="lbrefslbl">Referencias usadas</span>'+it.refs.map(r=>{
   const u='/reffile?name='+encodeURIComponent(r.file)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(sb);
   return '<img src="'+u+'" data-refsrc="'+esc(u)+'" title="'+esc(r.name||r.file)+' · clic para ampliar" alt="" loading="lazy">';}).join('');
  rf.classList.remove('hide');}
 else{rf.innerHTML='';rf.classList.add('hide');}
 // resolución real del archivo (útil sobre todo en Mis imágenes, que no tiene ficha de historial)
 const showRes=()=>{const im=$('lbImg'),w=im.naturalWidth,h=im.naturalHeight;if(!w||!h||it)return;
  m.innerHTML='<span><b>Resolución</b>'+w+'×'+h+' px</span>';m.classList.remove('hide');};
 $('lbImg').onload=showRes;
 if($('lbImg').complete)showRes();
 $('lightbox').classList.remove('hide');lbSyncNav()}
$('lbRefs').onclick=e=>{const im=e.target.closest('img[data-refsrc]');if(!im)return;e.stopPropagation();openLb(im.dataset.refsrc,'',null)};
function lbSyncNav(){const pv=$('lbPrev'),nx=$('lbNext');
 if(!lbScope||!lbCurFile){pv.classList.add('off');nx.classList.add('off');return}
 const sel=lbScope==='gal'?'#gal .gcard':'#shelfGrid .scard',attr=lbScope==='gal'?'file':'shelf';
 const cards=[...document.querySelectorAll(sel)],idx=cards.findIndex(c=>c.dataset[attr]===lbCurFile);
 pv.classList.toggle('off',idx<=0);nx.classList.toggle('off',idx<0||idx>=cards.length-1)}
$('lightbox').onclick=()=>{const s=window.getSelection&&window.getSelection();if(s&&String(s).length)return;$('lightbox').classList.add('hide')};  // no cerrar si acabas de seleccionar texto
$('lbBar').onclick=e=>e.stopPropagation();
$('lbPrev').onclick=e=>{e.stopPropagation();lbNavigate(-1)};
$('lbNext').onclick=e=>{e.stopPropagation();lbNavigate(1)};
function toggleFull(el){const d=document;
 if(d.fullscreenElement||d.webkitFullscreenElement){(d.exitFullscreen||d.webkitExitFullscreen||function(){}).call(d);return}
 const fn=el.requestFullscreen||el.webkitRequestFullscreen;if(fn)fn.call(el);}
$('lbFull').onclick=e=>{e.stopPropagation();toggleFull($('lbImg'))};
$('lbShare').onclick=e=>{e.stopPropagation();openSharePop(e.currentTarget,$('lbImg').src,$('lightbox').dataset.file||'imagen.png')};
// ── Compartir imagen: sistema (Web Share) / WhatsApp / Telegram / X / copiar / descargar ──
async function _imgBlob(u){if(u.startsWith('data:')){return await(await fetch(u)).blob();}return await(await fetch(u)).blob();}
async function _copyImg(u){try{let b=await _imgBlob(u);
 if(b.type!=='image/png'){const bm=await createImageBitmap(b);const c=document.createElement('canvas');c.width=bm.width;c.height=bm.height;c.getContext('2d').drawImage(bm,0,0);b=await new Promise(r=>c.toBlob(r,'image/png'));}
 await navigator.clipboard.write([new ClipboardItem({'image/png':b})]);return true;}catch(e){return false;}}
async function _nativeShare(u,fn){try{const b=await _imgBlob(u);const f=new File([b],fn||'imagen.png',{type:b.type||'image/png'});
 if(navigator.canShare&&navigator.canShare({files:[f]})){await navigator.share({files:[f],title:'Imagen · Gio Studio'});return true;}}catch(e){if(e&&e.name==='AbortError')return true;}return false;}
function closeSharePop(){const p=$('sharePop');if(p)p.remove();document.removeEventListener('click',_shareOutside,true)}
function _shareOutside(e){if(!e.target.closest('#sharePop')&&!e.target.closest('.gshare')&&!e.target.closest('.sshare'))closeSharePop()}
function openSharePop(anchor,url,filename){closeSharePop();
 const pop=document.createElement('div');pop.className='sharepop';pop.id='sharePop';
 const opts=[['sys','Compartir (apps del sistema)…'],['wa','WhatsApp'],['tg','Telegram'],['ig','Instagram'],['fb','Facebook'],['x','X (Twitter)'],['copy','Copiar imagen'],['dl','Descargar']];
 pop.innerHTML=opts.map(o=>'<button data-k="'+o[0]+'">'+o[1]+'</button>').join('');
 document.body.appendChild(pop);
 const r=anchor.getBoundingClientRect();
 pop.style.left=Math.max(8,Math.min(r.right-pop.offsetWidth,window.innerWidth-pop.offsetWidth-8))+'px';
 let top=r.bottom+6;if(top+pop.offsetHeight>window.innerHeight-8)top=Math.max(8,r.top-pop.offsetHeight-6);pop.style.top=top+'px';
 pop.onclick=async e=>{const b=e.target.closest('button');if(!b)return;e.stopPropagation();const k=b.dataset.k;closeSharePop();
  if(k==='sys'){if(!await _nativeShare(url,filename))toast('Tu navegador no permite compartir archivos aquí; usa «Copiar imagen»','bad');return;}
  if(k==='dl'){const a=document.createElement('a');a.href=url;a.download=filename||'imagen.png';document.body.appendChild(a);a.click();a.remove();return;}
  if(k==='copy'){const ok=await _copyImg(url);toast(ok?'Imagen copiada ✓ · pégala donde quieras':'No se pudo copiar la imagen',ok?'':'bad');return;}
  const ok=await _copyImg(url),links={wa:'https://web.whatsapp.com/',tg:'https://web.telegram.org/',ig:'https://www.instagram.com/',fb:'https://www.facebook.com/',x:'https://twitter.com/intent/tweet'},nm={wa:'WhatsApp',tg:'Telegram',ig:'Instagram',fb:'Facebook',x:'X'}[k];
  window.open(links[k],'_blank','noopener');
  toast(ok?('Imagen copiada — pégala con ⌘V en '+nm):('Abre '+nm+' y adjunta la imagen'));};
 setTimeout(()=>document.addEventListener('click',_shareOutside,true),0);}
async function _nativeShareMany(items){try{const files=[];for(const it of items){const b=await _imgBlob(it.url);files.push(new File([b],it.filename||'imagen.png',{type:b.type||'image/png'}));}
 if(navigator.canShare&&navigator.canShare({files})){await navigator.share({files,title:'Imágenes · Gio Studio'});return true;}}catch(e){if(e&&e.name==='AbortError')return true;}return false;}
// compartir una selección (varias imágenes): sistema = todas; redes/copiar = la primera (las redes aceptan una a la vez)
function openSharePopMulti(anchor,items){closeSharePop();
 if(items.length<=1){return openSharePop(anchor,items[0].url,items[0].filename);}
 const pop=document.createElement('div');pop.className='sharepop';pop.id='sharePop';
 const opts=[['sys','Compartir todas (apps del sistema)…'],['wa','WhatsApp'],['tg','Telegram'],['ig','Instagram'],['fb','Facebook'],['x','X (Twitter)'],['copy','Copiar la primera'],['dl','Descargar todas']];
 pop.innerHTML=opts.map(o=>'<button data-k="'+o[0]+'">'+o[1]+'</button>').join('');
 document.body.appendChild(pop);
 const r=anchor.getBoundingClientRect();
 pop.style.left=Math.max(8,Math.min(r.right-pop.offsetWidth,window.innerWidth-pop.offsetWidth-8))+'px';
 let top=r.bottom+6;if(top+pop.offsetHeight>window.innerHeight-8)top=Math.max(8,r.top-pop.offsetHeight-6);pop.style.top=top+'px';
 pop.onclick=async e=>{const b=e.target.closest('button');if(!b)return;e.stopPropagation();const k=b.dataset.k;closeSharePop();
  if(k==='sys'){if(!await _nativeShareMany(items))toast('Tu navegador no permite compartir varios archivos aquí; usa «Descargar todas»','bad');return;}
  if(k==='dl'){for(const it of items){const a=document.createElement('a');a.href=it.url;a.download=it.filename||'imagen.png';document.body.appendChild(a);a.click();a.remove();await new Promise(r=>setTimeout(r,120));}return;}
  if(k==='copy'){const ok=await _copyImg(items[0].url);toast(ok?'Primera imagen copiada ✓ · pégala donde quieras':'No se pudo copiar',ok?'':'bad');return;}
  const ok=await _copyImg(items[0].url),links={wa:'https://web.whatsapp.com/',tg:'https://web.telegram.org/',ig:'https://www.instagram.com/',fb:'https://www.facebook.com/',x:'https://twitter.com/intent/tweet'},nm={wa:'WhatsApp',tg:'Telegram',ig:'Instagram',fb:'Facebook',x:'X'}[k];
  window.open(links[k],'_blank','noopener');
  toast(ok?('Copié la primera — pégala con ⌘V en '+nm+' (las redes aceptan una a la vez)'):('Abre '+nm+' y adjunta las imágenes'));};
 setTimeout(()=>document.addEventListener('click',_shareOutside,true),0);}
$('resultImg').onclick=()=>{if(results.length)openLb(results[active].image,lastResult?lastResult.prompt:'',null)};
$('resultImg').addEventListener('dragstart',e=>{if(!results.length){e.preventDefault();return}
 e.dataTransfer.setData('text/x-studio-b64',results[active].image);e.dataTransfer.effectAllowed='copy';markDropZones(true)});

// ===== resultado(s) =====
function showState(s){$('emptyState').classList.toggle('hide',s!=='empty');$('spinner').classList.toggle('hide',s!=='spin');
 $('resultImg').classList.toggle('hide',s!=='result');$('floaters').classList.toggle('hide',s!=='result')}
function err(m){let msg=typeof m==='string'?m:(m&&m.message)||'Error inesperado';
 $('emptyState').innerHTML='<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>'
  +'<div class="errmsg">'+esc(msg)+'</div><button class="retry" id="retryBtn"><svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>Reintentar</button>';
 showState('empty');$('retryBtn').onclick=run}
function fnFor(i){const base=((lastResult&&lastResult.prompt)||'imagen').slice(0,24).replace(/\s+/g,'_')||'imagen';
 return base+(results.length>1?'_'+(i+1):'')+'.'+(lastResult?lastResult.fmt:'png')}
function showResult(i){active=i;const r=results[i];
 $('resultImg').src=r.image;showState('result');
 const fn=fnFor(i);
 $('fDl').href=r.image;$('fDl').setAttribute('download',fn);
 $('dl').href=r.image;$('dl').setAttribute('download',fn);
 [...document.querySelectorAll('.strip .sth')].forEach((x,j)=>x.classList.toggle('on',j===i))}
function renderStrip(){const s=$('strip');
 if(results.length<2){s.classList.add('hide');s.innerHTML='';return}
 s.innerHTML=results.map((r,i)=>`<button class="sth${i===active?' on':''}" data-i="${i}" title="Resultado ${i+1}"><img src="${r.image}" alt="Resultado ${i+1}"></button>`).join('');
 s.classList.remove('hide')}
$('strip').onclick=e=>{const b=e.target.closest('.sth');if(b)showResult(+b.dataset.i)};

function updGenChip(){const n=activeJobs;const gchip=$('genChip');
 if(n>0&&gchip.parentNode!==document.body)document.body.appendChild(gchip);   // fixed real al viewport (la columna .an tiene transform y rompía el fixed)
 gchip.classList.toggle('hide',n<=0);
 const lbl=()=>{const s=Math.round((Date.now()-genT0)/1000);return(activeJobs>1?('Generando '+activeJobs+'… '):'Generando… ')+'('+s+'s)';};
 if(n>0){if(!genTimer){genT0=Date.now();genTimer=setInterval(()=>{if(activeJobs>0)$('genChipTxt').textContent=lbl();},500);}$('genChipTxt').textContent=n>1?('Generando '+n+'…'):'Generando…';}
 else if(genTimer){clearInterval(genTimer);genTimer=null;}
 // feedback inmediato en el propio botón (aunque ya haya un resultado en el lienzo)
 const go=$('go'),gt=$('goTxt');
 if(go){go.classList.toggle('busy',n>0);if(gt)gt.textContent=n>0?(n>1?('Generando '+n+'…'):'Generando…'):'Generar';}
 // mientras se genera: la imagen actual queda TAL CUAL (sin velo) y el recuadro muestra
 // un haz de luz recorriendo el borde
 if($('canvas'))$('canvas').classList.toggle('gen',n>0)}
// el chip "Generando" debe vivir en <body>: su columna tiene transform y eso rompe el position:fixed
try{document.body.appendChild($('genChip'))}catch(e){}
async function run(){
 const prompt=$('prompt').value.trim();if(!prompt){toast('Escribe el prompt','bad');$('prompt').focus();return}
 const proj=$('projSel').value,pdata=projects[proj];
 const useVisual=$('useVis').checked&&pdata&&pdata.refs.length>0;
 if(mode==='editar'&&refs.length===0&&!useVisual){toast('Sube una imagen (o activa memoria visual)','bad');return}
 if(mask&&refs.length===0&&useVisual)toast('Ojo: la máscara se aplicará a la primera referencia del proyecto');
 const fmt=$('fmt').value;
 const body={prompt,size:$('w').value+'x'+$('h').value,quality:$('quality').value,n:+$('n').value,
  output_format:fmt,moderation:$('mod').value,
  partial_images:+($('partImg').value||0),project:proj,sub:activeSub,
  save_desktop:$('saveDesk').checked};
 if(fmt!=='png')body.output_compression=+$('comp').value;
 let url='/generate';const willEdit=mode==='editar'||useVisual||refs.length>0;
 const refsUsed=refs.map(r=>({name:r.name,b64:r.b64}));
 if(willEdit){url='/edit';body.images=refsUsed;if(mask)body.mask=mask;body.use_project_refs=useVisual}
 // ángulo 3D: añade la guía de cámara al prompt (y opcionalmente el cubo como referencia)
 if($('ang3dOn')&&$('ang3dOn').checked){const d=ang3dDesc();body.prompt=(body.prompt+' '+d.prompt).trim();
  if($('ang3dRef').checked){const ab=ang3dSnap();if(ab){if(url!=='/edit'){url='/edit';body.images=refsUsed.slice()}body.images=(body.images||[]).concat([{name:'angulo3d.png',b64:ab}])}}}
 // stream de previews solo si es el único trabajo en curso (evita que peleen por el lienzo)
 const willStream=(+($('partImg').value||0))>0 && +$('n').value===1 && activeJobs===0;
 // muestra el spinner en el lienzo solo si no hay nada que mostrar y es el primer trabajo
 const showSpin = activeJobs===0 && $('resultImg').classList.contains('hide');
 if(showSpin){$('resbar').classList.add('hide');$('strip').classList.add('hide');showState('spin');}
 activeJobs++;updGenChip();
 const ac=new AbortController(),killer=setTimeout(()=>{try{ac.abort()}catch(_){}} ,360000);  // red de seguridad: nunca quedarse pegado
 try{
  let d;
  const resp=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:ac.signal});
  if(willStream&&resp.body&&(resp.headers.get('content-type')||'').includes('ndjson')){
   const reader=resp.body.getReader(),dec=new TextDecoder();let buf='';
   const ext=fmt==='jpeg'?'jpeg':fmt;
   for(;;){const{value,done}=await reader.read();if(done)break;
    buf+=dec.decode(value,{stream:true});let nl;
    while((nl=buf.indexOf('\n'))>=0){const ln=buf.slice(0,nl).trim();buf=buf.slice(nl+1);if(!ln)continue;
     let ev;try{ev=JSON.parse(ln)}catch(_){continue}
     if(ev.type==='partial'){showState('result');$('floaters').classList.add('hide');$('resultImg').src='data:image/'+ext+';base64,'+ev.b64;}
     else if(ev.type==='done')d=ev.result;
     else if(ev.type==='error')d={error:ev.error};}}
   if(!d)d={error:'El preview no devolvió resultado.'};
  }else{d=await resp.json();}
  if(d.error){toast(d.error,'bad');if(showSpin&&activeJobs===1)err(d.error)}
  else{
   results=d.images&&d.images.length?d.images:[{image:d.image}];
   lastResult={prompt,refsUsed,fmt};
   renderStrip();showResult(0);
   $('resbar').classList.remove('hide');
   bumpSess(d.cost||0,results.length);
   let ctxt='<b>aprox. $'+(d.cost||0).toFixed(4)+'</b>';
   // desglose salida (imagen) vs entrada (texto + referencias) cuando hubo imágenes de entrada
   if((d.in_img_tokens||0)>0)ctxt+=' <span style="color:var(--mut)">(salida $'+(d.out_cost||0).toFixed(4)
     +' + entrada $'+(d.in_cost||0).toFixed(4)+')</span>';
   ctxt+=' · '+(d.output_tokens||0)+' tok salida'+((d.in_img_tokens||0)>0?' · '+d.in_img_tokens+' tok ref':'');
   $('cost').innerHTML=ctxt
    +(results.length>1?' · '+results.length+' imágenes':'')
    +(d.via_visual?' · memoria visual':'');
   if(activeJobs>1)toast('Imagen lista');
   loadGal()}
 }catch(e){
  if(e&&e.name==='AbortError'){toast('La generación tardó demasiado y se canceló. Revisa tu conexión e intenta de nuevo.','bad');if(showSpin&&activeJobs===1)err('Tardó demasiado · cancelado')}
  else{toast(String(e&&e.message||e||'Error'),'bad');if(showSpin&&activeJobs===1)err(e)}}
 clearTimeout(killer);
 activeJobs--;updGenChip();
 // si ya no queda nada generando y el lienzo quedó en spinner sin resultado, vuelve a vacío
 if(activeJobs===0&&!$('spinner').classList.contains('hide')&&!$('resultImg').src)showState('empty');
 validate();
}
// lanza un trabajo /edit no bloqueante (usado por Ángulos 3D) reutilizando el contador y el lienzo
async function fireGenJob(body){
 const showSpin=activeJobs===0 && $('resultImg').classList.contains('hide');
 if(showSpin){$('resbar').classList.add('hide');$('strip').classList.add('hide');showState('spin')}
 activeJobs++;updGenChip();
 try{const d=await(await fetch('/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.error){toast(d.error,'bad');if(showSpin&&activeJobs===1)err(d.error)}
  else{results=d.images&&d.images.length?d.images:[{image:d.image}];lastResult={prompt:body.prompt,refsUsed:[],fmt:'png'};
   renderStrip();showResult(0);$('resbar').classList.remove('hide');bumpSess(d.cost||0,results.length);
   $('cost').innerHTML='<b>$'+(d.cost||0).toFixed(4)+'</b>'+(d.via_visual?' · memoria visual':'');
   loadGal();if(activeJobs>1)toast('Imagen lista')}
 }catch(e){toast(String(e&&e.message||e||'Error'),'bad');if(showSpin&&activeJobs===1)err(e)}
 activeJobs--;updGenChip();
 if(activeJobs===0&&!$('spinner').classList.contains('hide')&&!$('resultImg').src)showState('empty');
}
$('go').onclick=run;$('again').onclick=run;
// Enter genera (Shift+Enter = salto de línea) en los campos de prompt principales.
// Los campos multilínea por naturaleza (lote de prompts, letra de canción) se
// quedan con Enter = salto de línea para no romper su función.
function enterGen(taId,btnId,fn){
 const el=$(taId); if(!el)return;
 el.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){
   e.preventDefault();
   if(!btnId||!$(btnId)||!$(btnId).disabled)fn();}});
}
enterGen('prompt','go',run);          // imagen
enterGen('sdPrompt','vidGo',runVID);  // video · Seedance
enterGen('klPrompt','vidGo',runVID);  // video · Kling
enterGen('ttsText','ttsGo',runTTS);   // voz
enterGen('sfxText','sfxGo',runSFX);   // efectos
enterGen('musPrompt','musGo',runMUS); // música
enterGen('sttPrompt','sttGo',runSTT); // transcribir (contexto opcional)
function batchLines(){return $('batchTxt').value.split('\n').map(l=>l.trim()).filter(Boolean);}
function batchEst(){const lines=batchLines().length,n=+$('n').value,imgs=lines*n,tot=imgs*estTokens()*30/1e6;
 $('batchEst').textContent=lines?imgs+(imgs>1?' imágenes':' imagen')+' ('+lines+'×'+n+') · aprox. $'+tot.toFixed(tot<0.1?4:2):'—';}
$('batchTxt').addEventListener('input',batchEst);
$('batchGo').onclick=async()=>{
 const lines=batchLines();
 if(!lines.length){toast('Escribe al menos un prompt (uno por línea)','bad');return}
 const n=+$('n').value,imgs=lines.length*n,tot=imgs*estTokens()*30/1e6;
 if(imgs>3&&!confirm('Vas a generar '+imgs+' imágenes (~$'+tot.toFixed(2)+'). ¿Continuar?'))return;
 $('batchGo').disabled=true;
 for(let i=0;i<lines.length;i++){
  $('batchGo').textContent='Lote '+(i+1)+' / '+lines.length+'…';
  $('prompt').value=lines[i];
  await run()}
 $('batchGo').disabled=false;$('batchGo').textContent='Generar lote';
 toast('Lote terminado: '+lines.length+' prompts')};

function flash(el){const c=el.style.color;el.style.color='var(--accent)';setTimeout(()=>el.style.color=c,650)}
$('fCopy').onclick=()=>{if(!lastResult)return;$('prompt').value=lastResult.prompt;refs=lastResult.refsUsed.map(r=>({name:r.name,b64:r.b64}));renderThumbs();try{navigator.clipboard.writeText(lastResult.prompt)}catch(e){}flash($('fCopy'));toast('Prompt y referencias restauradas')};
$('fAdd').onclick=()=>{if(!results.length)return;refs.push({name:'generada.png',b64:results[active].image.split(',')[1]});renderThumbs();flash($('fAdd'));toast('Añadida como referencia')};
$('fIter').onclick=()=>{if(!results.length)return;
 refs=[{name:'iteracion.png',b64:results[active].image.split(',')[1]}];mask=null;
 renderThumbs();renderMaskThumb();setMode('editar');
 $('prompt').value='';$('prompt').placeholder='Describe solo el cambio: "ahora de noche", "quita el texto", "hazlo acuarela"…';
 $('prompt').focus();toast('Iterando sobre el resultado · describe el cambio')};
$('cmpSlider').oninput=cmpUpdate;
$('cmpModal').onclick=e=>{if(e.target===$('cmpModal'))$('cmpModal').classList.add('hide')};

// ===== audio: voz (TTS) y transcripción =====
const VOICES=['alloy','ash','ballad','coral','echo','fable','onyx','nova','sage','shimmer','verse'];
let selVoice=localStorage.getItem('studio_voice')||'nova';
if(!VOICES.includes(selVoice))selVoice='nova';
$('voices').innerHTML=VOICES.map(v=>`<span class="chip vchip${v===selVoice?' on':''}" data-v="${v}">${v}</span>`).join('');
$('voices').onclick=e=>{const c=e.target.closest('.vchip');if(!c)return;
 selVoice=c.dataset.v;localStorage.setItem('studio_voice',selVoice);
 [...$('voices').children].forEach(x=>x.classList.toggle('on',x.dataset.v===selVoice))};
function audTab(t){['audTTS','audSTT','audSFX','audMUS'].forEach(id=>$(id).classList.toggle('on',id===t));
 $('ttsBox').classList.toggle('hide',t!=='audTTS');
 $('sttBox').classList.toggle('hide',t!=='audSTT');
 $('sfxBox').classList.toggle('hide',t!=='audSFX');
 $('musBox').classList.toggle('hide',t!=='audMUS')}
$('audTTS').onclick=()=>audTab('audTTS');
$('audSTT').onclick=()=>audTab('audSTT');
$('audSFX').onclick=()=>audTab('audSFX');
$('audMUS').onclick=()=>audTab('audMUS');
// --- música ---
$('musModel').onchange=()=>{const mm=$('musModel').value==='minimax';
 $('musLyrBox').classList.toggle('dim',!mm);$('musNegBox').classList.toggle('dim',mm)};
$('musModel').onchange();
async function runMUS(){const p=$('musPrompt').value.trim();
 if(p.length<10){toast('Describe la música con al menos 10 caracteres','bad');$('musPrompt').focus();return}
 $('musGo').disabled=true;$('musGoTxt').textContent='Generando · 1–3 min…';
 const body={model:$('musModel').value,prompt:p,lyrics:$('musLyrics').value,
  instrumental:$('musInstr').checked,negative:$('musNeg').value,seed:$('musSeed').value.trim(),
  project:$('projSel').value,sub:activeSub,save_desktop:$('saveDesk').checked};
 try{const d=await(await fetch('/music',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.error)toast(d.error,'bad');
  else{$('audPlayer').src='/file?name='+encodeURIComponent(d.file);
   $('audTitle').textContent='Música · '+($('musModel').value==='minimax'?'MiniMax':'Lyria 2');
   $('audCost').innerHTML='<b>fal</b>';
   $('audDl').href='/file?name='+encodeURIComponent(d.file);$('audDl').setAttribute('download',d.file);
   $('audEmpty').classList.add('hide');$('txResult').classList.add('hide');$('audResult').classList.remove('hide');
   $('audPlayer').play().catch(()=>{});
   bumpSess(0);loadGal();toast('Música lista')}
 }catch(e){toast(String(e),'bad')}
 $('musGo').disabled=false;$('musGoTxt').textContent='Generar música'}
$('musGo').onclick=runMUS;
// --- proveedor: OpenAI / ElevenLabs ---
let prov=localStorage.getItem('studio_prov')||'oai',elReady=false,elVoices=[];
function setProv(p){prov=p;localStorage.setItem('studio_prov',p);
 $('provOAI').classList.toggle('on',p==='oai');$('provEL').classList.toggle('on',p==='el');
 $('oaiOpts').classList.toggle('hide',p!=='oai');$('elOpts').classList.toggle('hide',p!=='el');
 if(p==='el'&&!elReady)elInit();
 ttsEstCalc()}
$('provOAI').onclick=()=>setProv('oai');$('provEL').onclick=()=>setProv('el');
async function elInit(){const s=await(await fetch('/elstatus')).json();
 elReady=s.ok;
 $('elConnect').classList.toggle('hide',s.ok);$('elMain').classList.toggle('hide',!s.ok);
 if(s.ok){renderElQuota(s);await loadElVoices()}}
function renderElQuota(s){const left=(s.limit||0)-(s.used||0);
 $('elQuota').textContent='Plan '+(s.tier||'free')+' · usados '+(s.used||0).toLocaleString()+' de '+(s.limit||0).toLocaleString()+' créditos · quedan '+left.toLocaleString()}
async function loadElVoices(){const r=await(await fetch('/elvoices')).json();
 elVoices=r.voices||[];
 const cats={cloned:'Clonadas',generated:'Generadas',professional:'Profesionales',premade:'Predefinidas'};
 const cur=localStorage.getItem('studio_elvoice')||'';
 let html='';
 for(const[c,label]of Object.entries(cats)){
  const vs=elVoices.filter(v=>v.category===c);if(!vs.length)continue;
  html+='<optgroup label="'+label+'">'+vs.map(v=>`<option value="${esc(v.id)}" ${v.id===cur?'selected':''}>${esc(v.name)}</option>`).join('')+'</optgroup>'}
 const rest=elVoices.filter(v=>!cats[v.category]);
 if(rest.length)html+='<optgroup label="Otras">'+rest.map(v=>`<option value="${esc(v.id)}" ${v.id===cur?'selected':''}>${esc(v.name)}</option>`).join('')+'</optgroup>';
 $('elVoice').innerHTML=html||'<option value="">Sin voces · revisa tu cuenta</option>'}
$('elVoice').onchange=()=>localStorage.setItem('studio_elvoice',$('elVoice').value);
$('elRefresh').onclick=e=>{e.preventDefault();loadElVoices();toast('Voces actualizadas')};
$('elKeySave').onclick=async()=>{const k=$('elKeyIn').value.trim();if(!k)return;
 $('elKeySave').textContent='…';
 const r=await(await fetch('/elkey',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})})).json();
 $('elKeySave').textContent='Conectar';
 if(!r.ok){toast(r.error||'Clave inválida','bad');return}
 toast('ElevenLabs conectado');elInit()};
[['elStab','elStabV',v=>(+v).toFixed(2)],['elSim','elSimV',v=>(+v).toFixed(2)],
 ['elSty','elStyV',v=>(+v).toFixed(2)],['elSpd','elSpdV',v=>(+v).toFixed(2)+'×']].forEach(([i,o,f])=>
 $(i).oninput=()=>$(o).textContent=f($(i).value));
$('elModel').onchange=()=>ttsEstCalc();
function ttsEstCalc(){const n=$('ttsText').value.length;$('ttsCount').textContent=n+' / 4096';
 if(prov==='el'){const m=$('elModel').value;
  const cr=Math.round(n*((m.includes('flash')||m.includes('turbo'))?0.5:1));
  $('ttsEst').textContent='≈'+cr+' créditos';return}
 const m=$('ttsModel').value;
 const est=m==='tts-1'?n*15/1e6:m==='tts-1-hd'?n*30/1e6:n/950*0.015;
 $('ttsEst').textContent='aprox. $'+est.toFixed(4)}
$('ttsText').oninput=ttsEstCalc;
$('ttsModel').onchange=()=>{const mini=$('ttsModel').value==='gpt-4o-mini-tts';
 $('instrBox').classList.toggle('dim',!mini);$('speedBox').classList.toggle('dim',mini);ttsEstCalc()};
$('ttsModel').onchange();
$('ttsSpeed').oninput=()=>$('speedv').textContent=(+$('ttsSpeed').value).toFixed(2)+'×';
function showAudResult(d,title){$('audPlayer').src=d.audio;
 $('audTitle').textContent=title;
 $('audCost').innerHTML=d.credits!==undefined?'<b>'+d.credits+' cr</b>':'<b>aprox. $'+(d.cost||0).toFixed(4)+'</b>';
 $('audDl').href=d.audio;$('audDl').setAttribute('download',d.file||'audio.mp3');
 $('audEmpty').classList.add('hide');$('audResult').classList.remove('hide');
 $('audPlayer').play().catch(()=>{})}
async function runTTS(){const text=$('ttsText').value.trim();
 if(!text){toast('Escribe el texto para la voz','bad');$('ttsText').focus();return}
 $('ttsGo').disabled=true;$('ttsGoTxt').textContent='Generando…';
 try{
  let d;
  if(prov==='el'){
   if(!elReady){toast('Conecta tu clave de ElevenLabs','bad');throw 0}
   const sel=$('elVoice');
   const body={input:text,voice_id:sel.value,voice_name:sel.options[sel.selectedIndex]?.text||'',
    model_id:$('elModel').value,stability:+$('elStab').value,similarity:+$('elSim').value,
    style:+$('elSty').value,speed:+$('elSpd').value,boost:$('elBoost').checked,
    seed:$('elSeed').value.trim(),normalization:$('elNorm').value,format:$('elFmt').value,
    project:$('projSel').value,sub:activeSub,save_desktop:$('saveDesk').checked};
   d=await(await fetch('/elspeech',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
   if(d.error)toast(d.error,'bad');
   else{showAudResult(d,(body.voice_name||'ElevenLabs')+' · '+$('elModel').value.replace('eleven_',''));
    bumpSess(0);loadGal();fetch('/elstatus').then(x=>x.json()).then(s=>{if(s.ok)renderElQuota(s)});
    toast('Voz generada · '+d.credits+' créditos')}
  }else{
   const m=$('ttsModel').value;
   const body={input:text,model:m,voice:selVoice,format:$('ttsFmt').value,
    project:$('projSel').value,sub:activeSub,save_desktop:$('saveDesk').checked};
   if(m==='gpt-4o-mini-tts')body.instructions=$('ttsInstr').value;else body.speed=+$('ttsSpeed').value;
   d=await(await fetch('/speech',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
   if(d.error)toast(d.error,'bad');
   else{showAudResult(d,selVoice+' · '+m);bumpSess(d.cost);loadGal();toast('Voz generada')}
  }
 }catch(e){if(e)toast(String(e),'bad')}
 $('ttsGo').disabled=false;$('ttsGoTxt').textContent='Generar voz'}
$('ttsGo').onclick=runTTS;
$('voiceTest').onclick=async()=>{
 $('voiceTest').disabled=true;$('voiceTest').textContent='…';
 const body={preview:true,input:'Hola, soy la voz '+selVoice+'. Así puedo sonar en tu proyecto.',
  model:$('ttsModel').value,voice:selVoice,format:'mp3',instructions:$('ttsInstr').value};
 try{const d=await(await fetch('/speech',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.error)toast(d.error,'bad');
  else{audEl.src=d.audio;audEl.play();bumpSess(d.cost);toast('Vista previa de '+selVoice+' · $'+(d.cost||0).toFixed(4))}
 }catch(e){toast(String(e),'bad')}
 $('voiceTest').disabled=false;$('voiceTest').textContent='Vista previa'};
$('elTest').onclick=async()=>{
 if(!elReady){toast('Conecta tu clave de ElevenLabs','bad');return}
 $('elTest').disabled=true;$('elTest').textContent='…';
 const sel=$('elVoice');
 const body={preview:true,input:'Hola, así sueno yo. Esta es una prueba corta de voz.',voice_id:sel.value,
  model_id:$('elModel').value,stability:+$('elStab').value,similarity:+$('elSim').value,
  style:+$('elSty').value,speed:+$('elSpd').value,boost:$('elBoost').checked,format:'mp3_44100_128'};
 try{const d=await(await fetch('/elspeech',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.error)toast(d.error,'bad');
  else{audEl.src=d.audio;audEl.play();toast('Vista previa · '+d.credits+' créditos')}
 }catch(e){toast(String(e),'bad')}
 $('elTest').disabled=false;$('elTest').textContent='Vista previa'};
// --- estilos de voz guardados (OpenAI) ---
let voiceStyles=[];
function renderVStyles(){
 $('vstyles').innerHTML=voiceStyles.map((s,i)=>
  `<span class="chip vstyle" data-i="${i}" title="${esc(s.voice)} · ${esc(s.instructions||'sin instrucciones')}">${esc(s.name)}<button class="vsx" data-del="${i}" title="Borrar estilo">×</button></span>`).join('')
  +'<span class="chip" id="vsAdd">+ Guardar actual</span>';
 $('vsAdd').onclick=async()=>{
  const name=(prompt('Nombre del estilo (voz: '+selVoice+'):')||'').trim();if(!name)return;
  voiceStyles.push({name,voice:selVoice,instructions:$('ttsInstr').value});
  await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice_styles:voiceStyles})});
  renderVStyles();toast('Estilo "'+name+'" guardado')}}
$('vstyles').onclick=async e=>{
 const del=e.target.closest('.vsx');
 if(del){voiceStyles.splice(+del.dataset.del,1);
  await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice_styles:voiceStyles})});
  renderVStyles();return}
 const c=e.target.closest('.vstyle');if(!c)return;
 const s=voiceStyles[+c.dataset.i];if(!s)return;
 selVoice=s.voice;localStorage.setItem('studio_voice',selVoice);
 [...$('voices').children].forEach(x=>x.classList.toggle('on',x.dataset.v===selVoice));
 $('ttsInstr').value=s.instructions||'';
 if($('ttsModel').value!=='gpt-4o-mini-tts'&&s.instructions){$('ttsModel').value='gpt-4o-mini-tts';$('ttsModel').onchange()}
 toast('Estilo "'+s.name+'" aplicado')};
// --- efectos de sonido (ElevenLabs) ---
$('sfxAuto').onchange=()=>$('sfxDurBox').classList.toggle('dim',$('sfxAuto').checked);
$('sfxDur').oninput=()=>$('sfxDurV').textContent=(+$('sfxDur').value).toFixed(1)+'s';
$('sfxInf').oninput=()=>$('sfxInfV').textContent=(+$('sfxInf').value).toFixed(2);
async function runSFX(){const text=$('sfxText').value.trim();
 if(!text){toast('Describe el efecto de sonido','bad');$('sfxText').focus();return}
 $('sfxGo').disabled=true;$('sfxGoTxt').textContent='Generando…';
 const body={input:text,influence:+$('sfxInf').value,project:$('projSel').value,sub:activeSub,save_desktop:$('saveDesk').checked};
 if(!$('sfxAuto').checked)body.duration=+$('sfxDur').value;
 try{const d=await(await fetch('/elsfx',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.error)toast(d.error,'bad');
  else{showAudResult(d,'Efecto · '+text.slice(0,40));$('audCost').innerHTML='<b>SFX</b>';
   bumpSess(0);loadGal();toast('Efecto generado')}
 }catch(e){toast(String(e),'bad')}
 $('sfxGo').disabled=false;$('sfxGoTxt').textContent='Generar efecto'}
$('sfxGo').onclick=runSFX;
// --- clonación de voz ---
let cloneSamples=[];
$('dropClone').onclick=()=>$('cloneFiles').click();
$('cloneFiles').onchange=async e=>{
 for(const f of e.target.files){if(f.size>10*1024*1024){toast(f.name+' supera 10MB','bad');continue}
  cloneSamples.push({name:f.name,b64:await fileToB64(f)})}
 e.target.value='';
 $('cloneInfo').textContent=cloneSamples.length?cloneSamples.length+' muestra(s): '+cloneSamples.map(s=>s.name).join(', '):''};
$('cloneGo').onclick=async()=>{
 const name=$('cloneName').value.trim();
 if(!name){toast('Ponle un nombre a la voz','bad');return}
 if(!cloneSamples.length){toast('Sube al menos una muestra de audio','bad');return}
 $('cloneGo').disabled=true;$('cloneGo').textContent='Clonando…';
 try{const d=await(await fetch('/elclone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,files:cloneSamples})})).json();
  if(d.error)toast(d.error,'bad');
  else{toast('Voz "'+name+'" creada · refrescando lista');cloneSamples=[];$('cloneInfo').textContent='';$('cloneName').value='';
   await loadElVoices();$('elVoice').value=d.voice_id;localStorage.setItem('studio_elvoice',d.voice_id)}
 }catch(e){toast(String(e),'bad')}
 $('cloneGo').disabled=false;$('cloneGo').textContent='Crear voz clonada'};
// --- transcripción ---
let sttFile=null,sttDur=0;
function sttEstCalc(){const p={'whisper-1':0.006,'gpt-4o-transcribe':0.006,'gpt-4o-mini-transcribe':0.003}[$('sttModel').value]||0.006;
 $('sttEst').textContent=sttDur?'aprox. $'+(sttDur*p).toFixed(4):'aprox. $'+p.toFixed(3)+'/min'}
async function setSttFile(f){
 if(f.size>25*1024*1024){toast(f.name+' supera 25MB (límite de OpenAI)','bad');return}
 sttFile={name:f.name,b64:await fileToB64(f)};sttDur=0;
 const u=URL.createObjectURL(f),a=new Audio();
 a.onloadedmetadata=()=>{sttDur=a.duration/60;URL.revokeObjectURL(u);
  const s=Math.round(a.duration);
  $('audInfo').textContent=f.name+' · '+(s>=60?Math.floor(s/60)+'m '+(s%60)+'s':s+'s');sttEstCalc()};
 a.onerror=()=>{$('audInfo').textContent=f.name;sttEstCalc()};
 a.src=u;
 if(mode!=='audio')setMode('audio');
 if($('sttBox').classList.contains('hide'))$('audSTT').click();
 sttEstCalc();toast('Audio cargado: '+f.name)}
$('dropAud').onclick=()=>$('audFile').click();
$('audFile').onchange=e=>{if(e.target.files[0])setSttFile(e.target.files[0]);e.target.value=''};
['dragover','dragenter'].forEach(ev=>$('dropAud').addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();$('dropAud').classList.add('hot')}));
$('dropAud').addEventListener('dragleave',e=>{e.preventDefault();$('dropAud').classList.remove('hot')});
$('dropAud').addEventListener('drop',e=>{e.preventDefault();e.stopPropagation();$('dropAud').classList.remove('hot');$('drop').classList.remove('hot');
 if(e.dataTransfer.files[0])setSttFile(e.dataTransfer.files[0])});
$('sttModel').onchange=()=>{if($('sttModel').value!=='whisper-1'&&['srt','vtt','verbose_json'].includes($('sttFmt').value))$('sttFmt').value='text';sttEstCalc()};
$('sttFmt').onchange=()=>{if(['srt','vtt','verbose_json'].includes($('sttFmt').value)&&$('sttModel').value!=='whisper-1'){
 $('sttModel').value='whisper-1';toast('SRT/VTT y tiempos usan whisper-1');sttEstCalc()}};
$('sttTrad').onchange=()=>{if($('sttTrad').checked){$('sttModel').value='whisper-1';sttEstCalc()}};
$('sttTemp').oninput=()=>$('sttTempv').textContent=(+$('sttTemp').value).toFixed(1);
async function runSTT(){
 if(!sttFile){toast('Sube o arrastra un audio primero','bad');return}
 $('sttGo').disabled=true;$('sttGoTxt').textContent='Transcribiendo…';
 const body={name:sttFile.name,b64:sttFile.b64,model:$('sttModel').value,language:$('sttLang').value,
  prompt:$('sttPrompt').value,response_format:$('sttFmt').value,translate:$('sttTrad').checked,
  temperature:+$('sttTemp').value,duration:sttDur,project:$('projSel').value,sub:activeSub,save_desktop:$('saveDesk').checked};
 try{const d=await(await fetch('/transcribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.error)toast(d.error,'bad');
  else{$('txText').value=d.text;
   $('txCost').innerHTML='<b>$'+(d.cost||0).toFixed(4)+'</b> · '+esc(d.model_used||'');
   $('txDl').href='/file?name='+encodeURIComponent(d.file);$('txDl').setAttribute('download',d.file);
   $('audEmpty').classList.add('hide');$('txResult').classList.remove('hide');
   bumpSess(d.cost);loadGal();toast('Transcripción lista')}
 }catch(e){toast(String(e),'bad')}
 $('sttGo').disabled=false;$('sttGoTxt').textContent='Transcribir'}
$('sttGo').onclick=runSTT;
$('txCopy').onclick=()=>{try{navigator.clipboard.writeText($('txText').value);toast('Transcripción copiada')}catch(e){}};
// --- historial de audio ---
const APLAY='<svg viewBox="0 0 24 24"><path d="M7 4l13 8-13 8z"/></svg>';
const APAUSE='<svg viewBox="0 0 24 24"><path d="M7 4h4v16H7zM13 4h4v16h-4z"/></svg>';
const ADOC='<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';
let audEl=new Audio(),playingFile=null;
audEl.onended=()=>{playingFile=null;renderAud()};
function renderAud(){renderVid();fillOhAudSel();
 const items=hist.filter(it=>['tts','stt','sfx','music'].includes(it.kind));
 $('audSec').classList.toggle('hide',!items.length);
 $('audList').innerHTML=items.slice(0,15).map(it=>{
  const playable=it.kind!=='stt',playing=playingFile===it.file;
  const sub=it.kind==='stt'?esc(it.model||''):esc(it.voice||'');
  const price=it.credits?it.credits+' cr':'$'+(it.cost||0).toFixed(4);
  const aq='&project='+encodeURIComponent(curProj())+(it._sub?'&sub='+encodeURIComponent(it._sub):'');
  return `<div class="arow" data-file="${esc(it.file)}" data-sub="${esc(it._sub||'')}">
   <button class="ap${playing?' playing':''}" title="${playable?(playing?'Pausar':'Reproducir'):'Ver transcripción'}">${playable?(playing?APAUSE:APLAY):ADOC}</button>
   <div class="ameta"><div class="at" title="${esc(it.prompt||'')}">${esc(it.prompt||it.file)}</div>
    <div class="as mono">${sub} · ${price}</div></div>
   <a class="gfbtn" href="/file?name=${encodeURIComponent(it.file)}${aq}" download="${esc(it.file)}" title="Descargar">${GDL}</a>
   <button class="gfbtn adel" title="Borrar (doble clic)">${GTR}</button></div>`}).join('')}
$('audList').onclick=async e=>{
 const row=e.target.closest('.arow');if(!row||e.target.closest('a'))return;
 const del=e.target.closest('.adel');
 const aqd=row?('&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(row.dataset.sub||'')):'';
 if(del){if(del.classList.contains('arm')){
   await fetch('/historydel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:row.dataset.file,project:curProj(),sub:row.dataset.sub||''})});
   if(playingFile===row.dataset.file){audEl.pause();playingFile=null}
   hist=hist.filter(it=>it.file!==row.dataset.file);if(histGroups.length){const g=histGroups.find(x=>x.k===(row.dataset.sub||''));if(g)g.items=g.items.filter(it=>it.file!==row.dataset.file)}renderAud();toast('Eliminado')}
  else{del.classList.add('arm');setTimeout(()=>del.classList.remove('arm'),1800)}
  return}
 if(!e.target.closest('.ap'))return;
 const it=hist.find(x=>x.file===row.dataset.file);if(!it)return;
 if(it.kind==='stt'){
  const t=await(await fetch('/file?name='+encodeURIComponent(it.file)+aqd)).text();
  $('txText').value=t;$('txCost').innerHTML='<b>$'+(it.cost||0).toFixed(4)+'</b> · '+esc(it.model||'');
  $('txDl').href='/file?name='+encodeURIComponent(it.file)+aqd;$('txDl').setAttribute('download',it.file);
  if(mode!=='audio')setMode('audio');
  $('audEmpty').classList.add('hide');$('txResult').classList.remove('hide');return}
 if(playingFile===it.file&&!audEl.paused){audEl.pause();playingFile=null}
 else{audEl.src='/file?name='+encodeURIComponent(it.file)+aqd;audEl.play();playingFile=it.file}
 renderAud()};

// ===== video: Seedance · Kling · OmniHuman (vía fal.ai) =====
let falReady=false,vidTab='sd',vidPoll=null;
let sdImgs=[],sdEnd=null,sdAuds=[],sdVids=[],klImg=null,klEnd=null,ohImg=null,ohAud=null,ohAudDur=0;
const VID_RATES={seedance:{'480p':0.15,'720p':0.30,'1080p':0.68},
 'seedance-fast':{'480p':0.12,'720p':0.24,'1080p':0.50},
 'kling-pro':{on:0.336,off:0.224},'kling-std':{on:0.126,off:0.084},omnihuman:0.14};
async function falInit(){const s=await(await fetch('/falstatus')).json();
 falReady=s.ok;
 $('falConnect').classList.toggle('hide',s.ok);
 $('vidMain').classList.remove('hide');
 vidEstCalc()}
$('falKeySave').onclick=async()=>{const k=$('falKeyIn').value.trim();if(!k)return;
 $('falKeySave').textContent='…';
 const r=await(await fetch('/falkey',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})})).json();
 $('falKeySave').textContent='Conectar';
 if(!r.ok){toast(r.error||'Clave inválida','bad');return}
 toast('fal.ai conectado');falInit()};
function vidSetTab(t){vidTab=t;
 if($('vidModelSel').value!==t)$('vidModelSel').value=t;
 $('sdBox').classList.toggle('hide',t!=='sd');
 $('klBox').classList.toggle('hide',t!=='kl');
 $('ohBox').classList.toggle('hide',t!=='oh');
 $('lsBox').classList.toggle('hide',t!=='ls');
 $('vidMemRow').classList.toggle('dim',t==='oh'||t==='ls');
 if(t==='oh')fillOhAudSel();
 if(t==='ls'){fillLsSels()}
 vidEstCalc()}
$('vidModelSel').onchange=()=>vidSetTab($('vidModelSel').value);
$('vidUseMem').checked=localStorage.getItem('studio_vidmem')!=='0';
$('vidUseMem').onchange=()=>localStorage.setItem('studio_vidmem',$('vidUseMem').checked?'1':'0');
$('vidInsStyle').onclick=()=>{const n=$('projSel').value,p=projects[n];
 if(!p){toast('Elige un proyecto con estilo guardado','bad');return}
 const st=((p.style_video||'').trim()||(p.style||'').trim());
 if(!st){toast('Este proyecto no tiene estilo guardado','bad');return}
 const ta=vidTab==='kl'?$('klPrompt'):$('sdPrompt');
 ta.value=st+(ta.value.trim()?'\n\n'+ta.value.trim():'');ta.focus();
 toast('Estilo insertado en el prompt')};
function vidEstCalc(){let est=null,note='';
 if(vidTab==='ls'){note='según segundos procesados (fal)'}
 else if(vidTab==='oh'){if(ohAudDur)est=ohAudDur*60*VID_RATES.omnihuman;else note='$0.14/seg de audio'}
 else if(vidTab==='kl'){const secs=+$('klDur').value;
  est=secs*($('klGenAud').checked?VID_RATES[$('klTier').value].on:VID_RATES[$('klTier').value].off)}
 else{const auto=$('sdDur').value==='auto',secs=auto?8:+$('sdDur').value;
  est=secs*(VID_RATES[$('sdTier').value][$('sdRes').value]||0.3);
  if(auto)note=' (auto ≈8s)'}
 $('vidEst').textContent=est!==null?'aprox. $'+est.toFixed(2)+note:note}
['sdTier','sdRes','sdDur','klTier','klDur','klGenAud','ohVer'].forEach(id=>$(id).onchange=vidEstCalc);
$('klCfg').oninput=()=>$('klCfgV').textContent=(+$('klCfg').value).toFixed(2);
$('ohVer').addEventListener('change',()=>{const v15=$('ohVer').value==='omnihuman';
 $('ohPromptBox').classList.toggle('dim',!v15);$('ohResRow').classList.toggle('dim',!v15)});
// cableado genérico de dropzones (clic, arrastre, tarjetas del historial)
function wireDrop(dropId,fileId,handler){
 $(dropId).onclick=()=>$(fileId).click();
 $(fileId).onchange=async e=>{await handler([...e.target.files]);e.target.value=''};
 ['dragover','dragenter'].forEach(ev=>$(dropId).addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();$(dropId).classList.add('hot')}));
 $(dropId).addEventListener('dragleave',e=>{e.preventDefault();$(dropId).classList.remove('hot')});
 $(dropId).addEventListener('drop',async e=>{e.preventDefault();e.stopPropagation();
  $(dropId).classList.remove('hot');$('drop').classList.remove('hot');
  const sf=e.dataTransfer.getData('text/x-studio-file');
  if(sf){const b=await(await fetch('/file?name='+encodeURIComponent(sf))).blob();
   const ext=sf.split('.').pop().toLowerCase();
   const mime=ext==='mp4'?'video/mp4':['mp3','wav','aac','flac','opus'].includes(ext)?'audio/mpeg':'image/png';
   await handler([new File([b],sf,{type:mime})]);return}
  await handler([...e.dataTransfer.files])})}
function thumbHTML(b64,xid,i){return `<div class="thumb"><img src="data:image/png;base64,${b64}" alt=""><button class="x" data-${xid}="${i}" title="Quitar">${xicon()}</button></div>`}
const SD_ROLES={
 img:[['personaje','Personaje / elemento'],['entorno','Entorno / escenario'],['objeto','Objeto / producto'],
      ['estilo','Estilo visual'],['composicion','Composición / encuadre'],['frame_ini','Frame inicial'],['frame_fin','Frame final']],
 vid:[['movimiento','Movimiento del sujeto'],['camara','Lenguaje de cámara'],['efectos','Efectos visuales'],['estilo_v','Estilo del video']],
 aud:[['musica','Música / banda sonora'],['voz','Voz · timbre del personaje'],['sfx','Efecto de sonido']]};
const RKIND_ICON={vid:'<svg viewBox="0 0 24 24"><rect x="2" y="5" width="14" height="14" rx="3"/><path d="M16 10l6-3v10l-6-3z"/></svg>',
 aud:'<svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>'};
let sdRefs=[];
function sdNums(){let i=0,v=0,a=0;return sdRefs.map(r=>r.kind==='img'?{n:++i,t:'img'}:r.kind==='vid'?{n:++v,t:'vid'}:{n:++a,t:'aud'})}
function buildSdBlock(){
 const nums=sdNums(),out=[];
 sdRefs.forEach((r,i)=>{const n=nums[i].n,L=(r.label||'').trim();
  const x={personaje:`Usa la imagen ${n} como referencia del personaje${L?' ('+L+')':''}: mantén su identidad y apariencia.`,
   entorno:`Usa la imagen ${n} como el entorno donde ocurre la escena${L?' ('+L+')':''}.`,
   objeto:`La imagen ${n} es ${L||'el objeto'} que debe aparecer en el video.`,
   estilo:`Adopta el estilo visual y la paleta de la imagen ${n}.`,
   composicion:`Sigue la composición y el encuadre de la imagen ${n}.`,
   frame_ini:`Usa la imagen ${n} como el primer frame del video.`,
   frame_fin:`Usa la imagen ${n} como el frame final del video.`,
   movimiento:`Replica el movimiento del sujeto del video ${n}.`,
   camara:`Usa el lenguaje y movimiento de cámara del video ${n}.`,
   efectos:`Replica los efectos visuales del video ${n}.`,
   estilo_v:`Adopta el estilo visual del video ${n}.`,
   musica:`Usa el audio ${n} como banda sonora y ajusta el ritmo del video a su música.`,
   voz:`Usa la voz del audio ${n} como el timbre de voz del personaje.`,
   sfx:`Incorpora el audio ${n} como efecto de sonido en el momento adecuado.`}[r.role];
  if(x)out.push(x)});
 return out.join(' ')}
function sdIsPureFrames(){
 const imgs=sdRefs.filter(r=>r.kind==='img');
 return sdRefs.length===imgs.length&&imgs.length>=1&&imgs.length<=2
  &&imgs.every(r=>['frame_ini','frame_fin'].includes(r.role))
  &&imgs.filter(r=>r.role==='frame_ini').length<=1
  &&imgs.filter(r=>r.role==='frame_fin').length<=1
  &&(imgs.length===1||imgs.some(r=>r.role==='frame_ini'))}
function renderSdRefs(){
 const nums=sdNums();
 $('sdRefList').innerHTML=sdRefs.map((r,i)=>{
  const opts=SD_ROLES[r.kind].map(([v,t])=>`<option value="${v}" ${r.role===v?'selected':''}>${t}</option>`).join('');
  const thumb=r.kind==='img'?`<img class="rthumb" src="data:image/png;base64,${r.b64}" alt="">`:`<span class="rkind">${RKIND_ICON[r.kind]}</span>`;
  return `<div class="refcard" data-i="${i}"><span class="rtag">${nums[i].t} ${nums[i].n}</span>${thumb}
   <select data-role="${i}">${opts}</select>
   <input type="text" data-label="${i}" placeholder="nombre · opcional" value="${esc(r.label||'')}">
   <button class="x" data-del="${i}" title="Quitar">${xicon()}</button></div>`}).join('');
 $('sdCount').textContent=sdRefs.length+' / 12';
 const block=sdRefs.length&&!sdIsPureFrames()?buildSdBlock():'';
 $('sdPrev').textContent=block?'Se añadirá al prompt: '+block:'';
 $('sdPrev').classList.toggle('hide',!block)}
$('sdRefList').addEventListener('change',e=>{
 if(e.target.dataset.role!==undefined){sdRefs[+e.target.dataset.role].role=e.target.value;renderSdRefs()}});
$('sdRefList').addEventListener('input',e=>{
 if(e.target.dataset.label!==undefined){sdRefs[+e.target.dataset.label].label=e.target.value;
  const block=sdRefs.length&&!sdIsPureFrames()?buildSdBlock():'';
  $('sdPrev').textContent=block?'Se añadirá al prompt: '+block:''}});
$('sdRefList').addEventListener('click',e=>{
 const b=e.target.closest('[data-del]');if(!b)return;sdRefs.splice(+b.dataset.del,1);renderSdRefs()});
wireDrop('dropSdRef','sdRefFile',async fs=>{
 for(const f of fs){
  const kind=f.type.startsWith('video/')?'vid':f.type.startsWith('audio/')?'aud':f.type.startsWith('image/')?'img':null;
  if(!kind)continue;
  const limits={img:9,vid:3,aud:3};
  if(sdRefs.filter(r=>r.kind===kind).length>=limits[kind]){toast('Máximo '+limits[kind]+' de ese tipo','bad');continue}
  if(sdRefs.length>=12){toast('Máximo 12 archivos en total','bad');break}
  if(kind==='vid'&&f.size>50*1024*1024){toast(f.name+' supera 50MB','bad');continue}
  if(kind==='aud'&&f.size>15*1024*1024){toast(f.name+' supera 15MB','bad');continue}
  sdRefs.push({kind,name:f.name,b64:await fileToB64(f),
   role:kind==='img'?(sdRefs.some(r=>r.role==='personaje')?'entorno':'personaje'):kind==='vid'?'movimiento':'musica',label:''})}
 renderSdRefs()});
function renderKl(){
 $('klImgThumb').innerHTML=klImg?thumbHTML(klImg.b64,'kli',0):'';
 $('klEndThumb').innerHTML=klEnd?thumbHTML(klEnd.b64,'kle',0):''}
$('klImgThumb').onclick=e=>{if(e.target.closest('.x')){klImg=null;renderKl()}};
$('klEndThumb').onclick=e=>{if(e.target.closest('.x')){klEnd=null;renderKl()}};
wireDrop('dropKlImg','klImgFile',async fs=>{const f=fs.find(x=>x.type.startsWith('image/'));
 if(f)klImg={name:f.name,b64:await fileToB64(f)};renderKl()});
wireDrop('dropKlEnd','klEndFile',async fs=>{const f=fs.find(x=>x.type.startsWith('image/'));
 if(f)klEnd={name:f.name,b64:await fileToB64(f)};renderKl()});
function renderOh(){$('ohImgThumb').innerHTML=ohImg?thumbHTML(ohImg.b64,'ohi',0):''}
$('ohImgThumb').onclick=e=>{if(e.target.closest('.x')){ohImg=null;renderOh()}};
wireDrop('dropOhImg','ohImgFile',async fs=>{const f=fs.find(x=>x.type.startsWith('image/'));
 if(f)ohImg={name:f.name,b64:await fileToB64(f)};renderOh()});
function fillOhAudSel(){const items=hist.filter(it=>['tts','sfx','music'].includes(it.kind));
 const cur=$('ohAudSel').value;
 $('ohAudSel').innerHTML='<option value="">— elegir del historial de audio —</option>'
  +items.slice(0,20).map(it=>`<option value="${esc(it.file)}" ${it.file===cur?'selected':''}>${esc((it.prompt||it.file).slice(0,50))}</option>`).join('')}
$('ohAudSel').onchange=()=>{const f=$('ohAudSel').value;
 if(!f){ohAud=null;ohAudDur=0;$('ohAudInfo').textContent='';vidEstCalc();return}
 ohAud={hist_file:f};
 const a=new Audio('/file?name='+encodeURIComponent(f));
 a.onloadedmetadata=()=>{ohAudDur=a.duration/60;
  $('ohAudInfo').textContent='Del historial · '+Math.round(a.duration)+'s';vidEstCalc()}};
wireDrop('dropOhAud','ohAudFile',async fs=>{const f=fs.find(x=>x.type.startsWith('audio/'))||fs[0];if(!f)return;
 ohAud={b64:await fileToB64(f)};$('ohAudSel').value='';
 const u=URL.createObjectURL(f),a=new Audio();
 a.onloadedmetadata=()=>{ohAudDur=a.duration/60;URL.revokeObjectURL(u);
  $('ohAudInfo').textContent=f.name+' · '+Math.round(a.duration)+'s';vidEstCalc()};
 a.src=u});
let lsVid=null,lsAud=null;
function fillLsSels(){
 const vids=hist.filter(it=>it.kind==='vid');
 const auds=hist.filter(it=>['tts','sfx','music'].includes(it.kind));
 $('lsVidSel').innerHTML='<option value="">— elegir del historial de video —</option>'
  +vids.slice(0,20).map(it=>`<option value="${esc(it.file)}">${esc((it.prompt||it.file).slice(0,50))}</option>`).join('');
 $('lsAudSel').innerHTML='<option value="">— elegir del historial de audio —</option>'
  +auds.slice(0,20).map(it=>`<option value="${esc(it.file)}">${esc((it.prompt||it.file).slice(0,50))}</option>`).join('')}
$('lsVidSel').onchange=()=>{const f=$('lsVidSel').value;
 lsVid=f?{hist_file:f}:null;$('lsVidInfo').textContent=f?'Del historial: '+f:''};
$('lsAudSel').onchange=()=>{const f=$('lsAudSel').value;
 lsAud=f?{hist_file:f}:null;$('lsAudInfo').textContent=f?'Del historial: '+f:''};
wireDrop('dropLsVid','lsVidFile',async fs=>{const f=fs.find(x=>x.type.startsWith('video/'))||fs[0];if(!f)return;
 if(f.size>80*1024*1024){toast(f.name+' supera 80MB','bad');return}
 lsVid={b64:await fileToB64(f)};$('lsVidSel').value='';$('lsVidInfo').textContent=f.name});
wireDrop('dropLsAud','lsAudFile',async fs=>{const f=fs.find(x=>x.type.startsWith('audio/'))||fs[0];if(!f)return;
 lsAud={b64:await fileToB64(f)};$('lsAudSel').value='';$('lsAudInfo').textContent=f.name});
$('lsGuid').oninput=()=>$('lsGuidV').textContent=(+$('lsGuid').value).toFixed(1);
function klMultiList(){return $('klMulti').value.split('\n').map(l=>l.trim()).filter(Boolean).map(l=>{
 const m=l.split('|');const o={prompt:m[0].trim()};
 if(m[1]&&(+m[1].trim())>0)o.duration=String(Math.round(+m[1].trim()));return o})}
async function runVID(){
 if(!falReady){toast('Conecta tu clave de fal.ai','bad');return}
 const est=parseFloat(($('vidEst').textContent.match(/\d[\d.]*/)||[0])[0])||0;
 let body={cost_est:est,project:$('projSel').value,sub:activeSub,save_desktop:$('saveDesk').checked,
  use_memory:$('vidUseMem').checked},title='';
 if(vidTab==='sd'){
  let prompt=$('sdPrompt').value.trim();
  if(!prompt){toast('Escribe el prompt del video','bad');$('sdPrompt').focus();return}
  const pick=k=>sdRefs.filter(r=>r.kind===k).map(r=>({name:r.name,b64:r.b64}));
  let images=pick('img'),end_image=null,force_ref=false;
  if(sdIsPureFrames()){
   const imgs=sdRefs.filter(r=>r.kind==='img');
   const ini=imgs.find(r=>r.role==='frame_ini')||imgs[0],fin=imgs.find(r=>r.role==='frame_fin');
   images=[{name:ini.name,b64:ini.b64}];
   if(fin&&fin!==ini)end_image={name:fin.name,b64:fin.b64};
  }else if(sdRefs.length){
   prompt+='\n\n'+buildSdBlock();force_ref=true}
  Object.assign(body,{model:$('sdTier').value,prompt,images,videos:pick('vid'),audios:pick('aud'),
   force_ref,resolution:$('sdRes').value,duration:$('sdDur').value,aspect:$('sdAsp').value,
   gen_audio:$('sdGenAud').checked,seed:$('sdSeed').value.trim()});
  if(end_image)body.end_image=end_image;title=$('sdPrompt').value.trim()}
 else if(vidTab==='kl'){
  const prompt=$('klPrompt').value.trim(),multi=klMultiList();
  if(!prompt&&!multi.length){toast('Escribe el prompt (o tomas multi-toma)','bad');$('klPrompt').focus();return}
  Object.assign(body,{model:$('klTier').value,prompt,multi_prompt:multi,shot_type:$('klShot').value,
   duration:$('klDur').value,aspect:$('klAsp').value,gen_audio:$('klGenAud').checked,
   negative:$('klNeg').value,cfg:+$('klCfg').value});
  if(klImg){body.image=klImg;if(klEnd)body.end_image=klEnd}
  title=prompt||multi.map(m=>m.prompt).join(' · ')}
 else if(vidTab==='ls'){
  if(!lsVid){toast('Elige o sube el video a sincronizar','bad');return}
  if(!lsAud){toast('Elige o sube el audio nuevo','bad');return}
  const body2={video:lsVid,audio:lsAud,guidance:+$('lsGuid').value,loop_mode:$('lsLoop').value,
   seed:$('lsSeed').value.trim(),label:$('lsAudSel').value||'audio subido',
   project:$('projSel').value,sub:activeSub,save_desktop:$('saveDesk').checked};
  $('vidGo').disabled=true;$('vidGoTxt').textContent='Enviando…';
  $('vidEmpty').classList.add('hide');$('vidResult').classList.add('hide');$('vidProgress').classList.remove('hide');
  $('vidProgTxt').textContent='Sincronizando labios…';$('vidProgSub').textContent='Enviando a fal.ai';
  try{const d=await(await fetch('/lipsync',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body2)})).json();
   if(d.error){vidFail(d.error);return}
   pollVid(d.id,'Lip-sync')}catch(e){vidFail(String(e))}
  return}
 else{
  if(!ohImg){toast('OmniHuman necesita la imagen de la persona','bad');return}
  if(!ohAud){toast('Elige o sube el audio que hablará','bad');return}
  Object.assign(body,{model:$('ohVer').value,prompt:$('ohPrompt').value.trim(),image:ohImg,audio:ohAud,
   resolution:$('ohRes').value,turbo:$('ohTurbo').checked});
  title='Avatar · OmniHuman'}
 $('vidGo').disabled=true;$('vidGoTxt').textContent='Enviando…';
 $('vidEmpty').classList.add('hide');$('vidResult').classList.add('hide');$('vidProgress').classList.remove('hide');
 $('vidProgTxt').textContent='Generando video…';$('vidProgSub').textContent='Enviando trabajo a fal.ai';
 try{const d=await(await fetch('/video',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.error){vidFail(d.error);return}
  pollVid(d.id,title);
 }catch(e){vidFail(String(e))}}
function pollVid(id,title){
 const t0=Date.now();
 if(vidPoll)clearInterval(vidPoll);
 vidPoll=setInterval(async()=>{
  try{const s=await(await fetch('/videostatus?id='+encodeURIComponent(id))).json();
   const mins=Math.floor((Date.now()-t0)/60000),secs=Math.floor(((Date.now()-t0)%60000)/1000);
   if(s.error){clearInterval(vidPoll);vidPoll=null;vidFail(s.error);return}
   if(!s.done){$('vidProgSub').textContent=(s.status==='IN_QUEUE'?'En cola'+(s.queue!=null?' · posición '+s.queue:''):'Procesando')+' · '+mins+'m '+secs+'s';return}
   clearInterval(vidPoll);vidPoll=null;
   $('vidProgress').classList.add('hide');$('vidResult').classList.remove('hide');
   $('vidPlayer').src=s.url;$('vidTitle').textContent=title.slice(0,60);
   $('vidCost').innerHTML='<b>$'+(s.cost||0).toFixed(2)+'</b>';
   $('vidDl').href=s.url;$('vidDl').setAttribute('download',s.file);
   $('vidPlayer').play().catch(()=>{});
   bumpSess(s.cost||0);loadGal();toast('Video listo');
   $('vidGo').disabled=false;$('vidGoTxt').textContent='Generar video';
  }catch(e){}},5000)}
function vidFail(msg){toast(msg,'bad');
 $('vidProgress').classList.add('hide');$('vidEmpty').classList.remove('hide');
 $('vidGo').disabled=false;$('vidGoTxt').textContent='Generar video'}
$('vidGo').onclick=runVID;
function renderVid(){const items=hist.filter(it=>it.kind==='vid');
 $('vidSec').classList.toggle('hide',!items.length);
 $('vidList').innerHTML=items.slice(0,10).map(it=>
  `<div class="arow" data-file="${esc(it.file)}" data-sub="${esc(it._sub||'')}">
   <button class="ap" title="Ver video"><svg viewBox="0 0 24 24"><path d="M7 4l13 8-13 8z"/></svg></button>
   <div class="ameta"><div class="at" title="${esc(it.prompt||'')}">${esc(it.prompt||it.file)}</div>
    <div class="as mono">${esc(it.model||'')} · $${(it.cost||0).toFixed(2)}</div></div>
   <a class="gfbtn" href="/file?name=${encodeURIComponent(it.file)}&project=${encodeURIComponent(curProj())}${it._sub?'&sub='+encodeURIComponent(it._sub):''}" download="${esc(it.file)}" title="Descargar">${GDL}</a>
   <button class="gfbtn vdel" title="Borrar (doble clic)">${GTR}</button></div>`).join('')}
$('vidList').onclick=async e=>{
 const row=e.target.closest('.arow');if(!row||e.target.closest('a'))return;
 const del=e.target.closest('.vdel');const vq=row?('&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(row.dataset.sub||'')):'';
 if(del){if(del.classList.contains('arm')){
   await fetch('/historydel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:row.dataset.file,project:curProj(),sub:row.dataset.sub||''})});
   hist=hist.filter(it=>it.file!==row.dataset.file);if(histGroups.length){const g=histGroups.find(x=>x.k===(row.dataset.sub||''));if(g)g.items=g.items.filter(it=>it.file!==row.dataset.file)}renderVid();toast('Video eliminado')}
  else{del.classList.add('arm');setTimeout(()=>del.classList.remove('arm'),1800)}
  return}
 if(!e.target.closest('.ap'))return;
 const it=hist.find(x=>x.file===row.dataset.file);if(!it)return;
 if(mode!=='video')setMode('video');
 $('vidEmpty').classList.add('hide');$('vidProgress').classList.add('hide');$('vidResult').classList.remove('hide');
 $('vidPlayer').src='/file?name='+encodeURIComponent(it.file)+vq;
 $('vidTitle').textContent=(it.prompt||'').slice(0,60);
 $('vidCost').innerHTML='<b>$'+(it.cost||0).toFixed(2)+'</b>';
 $('vidDl').href='/file?name='+encodeURIComponent(it.file)+vq;$('vidDl').setAttribute('download',it.file)};

// ===== magic prompt + describir =====
async function improvePrompt(btn,taId,mode){const ta=$(taId),p=ta.value.trim();
 if(!p){toast('Escribe primero un prompt','bad');ta.focus();return}
 btn.classList.add('busy');
 try{const d=await(await fetch('/magicprompt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p,mode})})).json();
  if(d.error)toast(d.error,'bad');
  else{ta.value=d.prompt;toast('Prompt mejorado · ⌘Z para volver al tuyo')}
 }catch(e){toast(String(e),'bad')}
 btn.classList.remove('busy')}
$('mpImg').onclick=e=>{e.preventDefault();improvePrompt($('mpImg'),'prompt','imagen')};
$('mpImgBtn').onclick=e=>{e.preventDefault();improvePrompt($('mpImgBtn'),'prompt','imagen')};
// ===== Ángulo 3D (gizmo de cámara/orientación) =====
let ang3dSubj={yaw:25,pitch:0}, ang3dCam={yaw:0,pitch:0,dist:45,lens:50,fstop:4}, ang3dMode='subj', ang3dShape='head', ang3dUse={subj:true,cam:true};
const ANG_FSTOPS=[1.2,1.4,2,2.8,4,5.6,8,11,16,22];
function lensType(mm){mm=+mm;if(mm<=16)return 'ultra gran angular';if(mm<=28)return 'gran angular';if(mm<=45)return 'casi normal';if(mm<=60)return 'normal';if(mm<=105)return 'retrato (tele corto)';if(mm<=180)return 'teleobjetivo';return 'teleobjetivo largo';}
function fNum(f){f=+f;return 'f/'+(f<10?f.toFixed(f%1?1:0):Math.round(f));}
function dofText(f){f=+f;if(f<=2)return 'muy poca profundidad de campo, fondo muy desenfocado, bokeh marcado';if(f<=4)return 'poca profundidad de campo, fondo desenfocado';if(f<=8)return 'profundidad de campo media';return 'gran profundidad de campo, casi todo enfocado';}
function ang3dCap(s){return s.charAt(0).toUpperCase()+s.slice(1)}
function ang3dActive(){return ang3dMode==='cam'?ang3dCam:ang3dSubj}
function ang3dSubjText(){const a2=(((ang3dSubj.yaw+180)%360+360)%360)-180,a=Math.abs(a2);
 const dir=a2>0?'hacia la derecha de la imagen':'hacia la izquierda de la imagen';let h;
 if(a<=15)h='de frente a la cámara';
 else if(a<=65)h='en vista 3/4 mirando '+dir;
 else if(a<=115)h='de perfil, mirando '+dir;
 else if(a<=160)h='casi de espaldas ('+dir+')';
 else h='de espaldas a la cámara';
 let v='';if(ang3dSubj.pitch>=18)v=', con la vista hacia arriba';else if(ang3dSubj.pitch<=-18)v=', con la vista hacia abajo';
 return h+v;}
function distLabel(d){d=+d;
 if(d<14)return 'en primer plano (close-up del rostro)';
 if(d<32)return 'en plano medio corto (busto)';
 if(d<52)return 'en plano medio (de la cintura hacia arriba)';
 if(d<70)return 'en plano entero (cuerpo completo)';
 if(d<86)return 'en plano general (figura dentro del entorno)';
 return 'en gran plano general (sujeto pequeño, mucho entorno)';}
function distShort(d){d=+d;
 if(d<14)return 'Primer plano';
 if(d<32)return 'Medio corto';
 if(d<52)return 'Plano medio';
 if(d<70)return 'Plano entero';
 if(d<86)return 'Plano general';
 return 'Gran plano gral.';}
function camTextFor(yaw,pitch,dist,lens,fstop){const a2=(((yaw+180)%360+360)%360)-180,a=Math.abs(a2);
 const dir=a2>0?'la derecha':'la izquierda';let h;
 if(a<=20)h='desde el frente';
 else if(a<=70)h='desde un ángulo a '+dir;
 else if(a<=115)h='desde el lado ('+dir+')';
 else if(a<=160)h='desde atrás en ángulo';
 else h='desde atrás';
 let v;
 if(pitch>=45)v='en vista cenital (muy desde arriba)';
 else if(pitch>=18)v='en picado (desde arriba)';
 else if(pitch<=-18)v='en contrapicado (desde abajo)';
 else v='a la altura de los ojos';
 let out=h+', '+v+(dist!=null?', '+distLabel(dist):'');
 if(lens!=null)out+=', con lente de '+Math.round(lens)+'mm ('+lensType(lens)+')';
 if(fstop!=null)out+=', apertura '+fNum(fstop)+' ('+dofText(fstop)+')';
 return out;}
function ang3dCamText(){return camTextFor(ang3dCam.yaw,ang3dCam.pitch,ang3dCam.dist,ang3dCam.lens,ang3dCam.fstop)}
function ang3dDesc(){const us=ang3dUse.subj,uc=ang3dUse.cam;const s=ang3dSubjText(),c=ang3dCamText();
 const short=[],pp=[];
 if(us){short.push('Sujeto: '+ang3dCap(s));pp.push('muestra al sujeto '+s);}
 if(uc){short.push('Cámara: '+ang3dCap(c));pp.push('con la cámara '+c);}
 let prompt='';
 if(pp.length){prompt='Encuadre y ángulo: '+pp.join(', ')+'.';if(us)prompt+=' Conserva su identidad y proporciones.';}
 return {short:short.join('  ·  ')||'Marca «Sujeto» o «Cámara» para usar el ángulo', prompt};}
function ang3dPresetList(){return ang3dMode==='cam'
 ?[['Frontal',0,0],['Lado izq',-90,0],['Lado der',90,0],['Detrás',180,0],['Picado',0,35],['Contrapicado',0,-28],['Cenital',0,60],['Nivel',0,0]]
 :[['Frente',0,0],['3/4 izq',-35,0],['3/4 der',35,0],['Perfil izq',-90,0],['Perfil der',90,0],['Espalda',180,0],['Vista arriba',0,25],['Vista abajo',0,-25]];}
function ang3dRenderPresets(){$('ang3dPresets').innerHTML=ang3dPresetList().map(p=>'<button data-y="'+p[1]+'" data-p="'+p[2]+'">'+p[0]+'</button>').join('')}
function drawCubeCv(cv,yaw,pitch){if(!cv)return;const ctx=cv.getContext('2d');
 const W=cv.width,H=cv.height;ctx.clearRect(0,0,W,H);
 const cs=getComputedStyle(document.body);
 const acc=(cs.getPropertyValue('--accent').trim()||'#1f6b54');
 const ln=(cs.getPropertyValue('--mut').trim()||'#888');
 const cx=W/2,cy=H/2,s=Math.min(W,H)*0.27;
 const ry=yaw*Math.PI/180,rx=pitch*Math.PI/180;
 function rot(p){let x=p[0],y=p[1],z=p[2];
  let x1=x*Math.cos(ry)+z*Math.sin(ry),z1=-x*Math.sin(ry)+z*Math.cos(ry);
  let y2=y*Math.cos(rx)-z1*Math.sin(rx),z2=y*Math.sin(rx)+z1*Math.cos(rx);
  return[x1,y2,z2];}
 const V=[[-1,-1,-1],[1,-1,-1],[1,1,-1],[-1,1,-1],[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]];
 const P=V.map(v=>{const r=rot(v);return{x:cx+r[0]*s,y:cy+r[1]*s,z:r[2]};});
 const faces=[{i:[4,5,6,7],front:1},{i:[0,1,2,3]},{i:[0,4,7,3]},{i:[1,5,6,2]},{i:[0,1,5,4]},{i:[3,2,6,7]}];
 faces.forEach(f=>f.z=(P[f.i[0]].z+P[f.i[1]].z+P[f.i[2]].z+P[f.i[3]].z)/4);
 faces.sort((a,b)=>a.z-b.z);
 faces.forEach(f=>{ctx.beginPath();f.i.forEach((vi,k)=>{const pt=P[vi];k?ctx.lineTo(pt.x,pt.y):ctx.moveTo(pt.x,pt.y)});ctx.closePath();
  if(f.front){ctx.fillStyle=acc+'cc';ctx.fill()}
  ctx.globalAlpha=f.front?1:.5;ctx.strokeStyle=f.front?acc:ln;ctx.lineWidth=f.front?2:1;ctx.stroke();ctx.globalAlpha=1});
 const c0=rot([0,0,0]),c1=rot([0,0,1.7]);
 ctx.beginPath();ctx.moveTo(cx+c0[0]*s,cy+c0[1]*s);ctx.lineTo(cx+c1[0]*s,cy+c1[1]*s);ctx.strokeStyle=acc;ctx.lineWidth=2.5;ctx.stroke();
 ctx.beginPath();ctx.arc(cx+c1[0]*s,cy+c1[1]*s,4,0,7);ctx.fillStyle=acc;ctx.fill();}
// vista superior (escenario): sujeto al centro con su flecha + cámara orbitando. Más intuitivo que el cubo.
function sceneCols(){const cs=getComputedStyle(document.body);return{acc:(cs.getPropertyValue('--accent').trim()||'#1f6b54'),mut:(cs.getPropertyValue('--mut').trim()||'#888'),txt:(cs.getPropertyValue('--txt').trim()||'#222')}}
function drawScene(cv,subjYaw,camYaw,camPitch,active){if(!cv)return;const ctx=cv.getContext('2d'),W=cv.width,H=cv.height;ctx.clearRect(0,0,W,H);
 const c=sceneCols(),D=Math.PI/180,cx=W/2,cy=H/2,R=Math.min(W,H)*0.36;
 // piso
 ctx.beginPath();ctx.arc(cx,cy,R,0,7);ctx.setLineDash([4,4]);ctx.globalAlpha=.45;ctx.strokeStyle=c.mut;ctx.lineWidth=1;ctx.stroke();ctx.setLineDash([]);ctx.globalAlpha=1;
 // cámara: glifo en la órbita + línea de visión al sujeto
 const ca=camYaw*D,px=cx+Math.sin(ca)*R,py=cy+Math.cos(ca)*R,camOn=active==='cam';
 ctx.beginPath();ctx.moveTo(px,py);ctx.lineTo(cx,cy);ctx.globalAlpha=camOn?.7:.35;ctx.strokeStyle=camOn?c.acc:c.mut;ctx.lineWidth=1.5;ctx.stroke();ctx.globalAlpha=1;
 ctx.save();ctx.translate(px,py);ctx.rotate(Math.atan2(cy-py,cx-px));ctx.fillStyle=camOn?c.acc:c.mut;
 ctx.fillRect(-5,-5,9,10);ctx.beginPath();ctx.moveTo(4,-4);ctx.lineTo(11,-7);ctx.lineTo(11,7);ctx.lineTo(4,4);ctx.closePath();ctx.fill();ctx.restore();
 // sujeto: punto + flecha de orientación
 const subjOn=active!=='cam',sa=subjYaw*D,ax=cx+Math.sin(sa)*(R*0.55),ay=cy+Math.cos(sa)*(R*0.55);
 ctx.beginPath();ctx.moveTo(cx,cy);ctx.lineTo(ax,ay);ctx.strokeStyle=subjOn?c.acc:c.mut;ctx.lineWidth=3;ctx.stroke();
 ctx.beginPath();ctx.arc(ax,ay,4,0,7);ctx.fillStyle=subjOn?c.acc:c.mut;ctx.fill();
 ctx.beginPath();ctx.arc(cx,cy,6,0,7);ctx.fillStyle=subjOn?c.acc:c.mut;ctx.fill();
 // etiquetas
 ctx.font='9px sans-serif';ctx.textAlign='center';ctx.globalAlpha=.85;ctx.fillStyle=c.txt;
 ctx.fillText('sujeto',cx,cy+R+10);
 const hb=camPitch>=45?'cenital':camPitch>=18?'picado':camPitch<=-18?'contrapicado':'nivel';
 ctx.fillText('cámara · '+hb,px,py-9);ctx.globalAlpha=1;ctx.textAlign='start';}
function drawDial(cv,yaw,pitch){if(!cv)return;const ctx=cv.getContext('2d'),W=cv.width,H=cv.height;ctx.clearRect(0,0,W,H);
 const c=sceneCols(),D=Math.PI/180,cx=W/2,cy=H/2,R=Math.min(W,H)*0.36;
 ctx.beginPath();ctx.arc(cx,cy,R,0,7);ctx.globalAlpha=.5;ctx.strokeStyle=c.mut;ctx.lineWidth=1.5;ctx.stroke();ctx.globalAlpha=1;
 const a=yaw*D,ax=cx+Math.sin(a)*R,ay=cy+Math.cos(a)*R;
 ctx.beginPath();ctx.moveTo(cx,cy);ctx.lineTo(ax,ay);ctx.strokeStyle=c.acc;ctx.lineWidth=3;ctx.stroke();
 ctx.beginPath();ctx.arc(ax,ay,4,0,7);ctx.fillStyle=c.acc;ctx.fill();
 ctx.beginPath();ctx.arc(cx,cy,4,0,7);ctx.fillStyle=c.acc;ctx.fill();
 if(Math.abs(pitch)>=18){ctx.fillStyle=c.acc;ctx.font='bold 11px sans-serif';ctx.textAlign='center';ctx.fillText(pitch>0?'▲':'▼',cx,cy-R-1);ctx.textAlign='start';}}
function sceneAngle(cv,e){const r=cv.getBoundingClientRect();const dx=e.clientX-(r.left+r.width/2),dy=e.clientY-(r.top+r.height/2);return Math.round(Math.atan2(dx,dy)*180/Math.PI);}
// maniquí 3D girable (alternativa al cubo para el SUJETO): figura articulada
function drawMannequin(cv,yaw,pitch){if(!cv)return;const ctx=cv.getContext('2d'),W=cv.width,H=cv.height;ctx.clearRect(0,0,W,H);
 const cs=getComputedStyle(document.body),acc=(cs.getPropertyValue('--accent').trim()||'#1f6b54'),mut=(cs.getPropertyValue('--mut').trim()||'#888');
 const cx=W/2,cy=H/2+6,s=Math.min(W,H)*0.16,ry=yaw*Math.PI/180,rx=pitch*Math.PI/180;
 function rot(p){var x=p[0],y=p[1],z=p[2];var x1=x*Math.cos(ry)+z*Math.sin(ry),z1=-x*Math.sin(ry)+z*Math.cos(ry);var y2=y*Math.cos(rx)-z1*Math.sin(rx),z2=y*Math.sin(rx)+z1*Math.cos(rx);return[x1,y2,z2];}
 function P(p){var r=rot(p);return[cx+r[0]*s,cy+r[1]*s];}
 const neck=[0,-1.0,0],hip=[0,0.7,0],lsh=[-0.55,-0.95,0],rsh=[0.55,-0.95,0],lha=[-0.85,0.25,0.05],rha=[0.85,0.25,0.05],lhp=[-0.32,0.7,0],rhp=[0.32,0.7,0],lf=[-0.42,2.0,0],rf=[0.42,2.0,0],head=[0,-1.5,0];
 ctx.strokeStyle=acc;ctx.lineWidth=2.6;ctx.lineJoin='round';ctx.lineCap='round';
 function seg(a,b){var A=P(a),B=P(b);ctx.beginPath();ctx.moveTo(A[0],A[1]);ctx.lineTo(B[0],B[1]);ctx.stroke();}
 seg(neck,hip);seg(lsh,rsh);seg(lsh,lha);seg(rsh,rha);seg(hip,lhp);seg(hip,rhp);seg(lhp,lf);seg(rhp,rf);
 var hd=P(head);ctx.beginPath();ctx.arc(hd[0],hd[1],s*0.42,0,7);ctx.fillStyle=acc+'cc';ctx.fill();ctx.strokeStyle=acc;ctx.lineWidth=2;ctx.stroke();
 var nz=P([0,-1.5,0.95]);ctx.beginPath();ctx.moveTo(hd[0],hd[1]);ctx.lineTo(nz[0],nz[1]);ctx.lineWidth=2.4;ctx.strokeStyle=acc;ctx.stroke();ctx.beginPath();ctx.arc(nz[0],nz[1],3.2,0,7);ctx.fillStyle=acc;ctx.fill();}
// CABEZA humana 3D girable: muestra hacia dónde mira (ojos + nariz que sobresale + flecha de mirada)
function drawHead(cv,yaw,pitch){if(!cv)return;const ctx=cv.getContext('2d'),W=cv.width,H=cv.height;ctx.clearRect(0,0,W,H);
 const cs=getComputedStyle(document.body),acc=(cs.getPropertyValue('--accent').trim()||'#1f6b54'),mut=(cs.getPropertyValue('--mut').trim()||'#9a9a9a');
 const cx=W/2,cy=H/2,R=Math.min(W,H)*0.30,ry=yaw*Math.PI/180,rx=pitch*Math.PI/180;
 function rot(p){var x=p[0],y=p[1],z=p[2];var x1=x*Math.cos(ry)+z*Math.sin(ry),z1=-x*Math.sin(ry)+z*Math.cos(ry);var y2=y*Math.cos(rx)-z1*Math.sin(rx),z2=y*Math.sin(rx)+z1*Math.cos(rx);return[x1,y2,z2];}
 function P(p){var r=rot(p);return[cx+r[0]*R,cy+r[1]*R,r[2]];}
 // cráneo (esfera → círculo) con sombreado suave
 var grd=ctx.createRadialGradient(cx-R*0.3,cy-R*0.3,R*0.2,cx,cy,R);grd.addColorStop(0,acc+'33');grd.addColorStop(1,acc+'14');
 ctx.beginPath();ctx.arc(cx,cy,R,0,7);ctx.fillStyle=grd;ctx.fill();ctx.strokeStyle=acc;ctx.lineWidth=2;ctx.stroke();
 // wireframe sutil (meridiano de la cara + línea de los ojos) para dar volumen y leer la rotación
 ctx.strokeStyle=acc+'4d';ctx.lineWidth=1;
 function ring(fn){ctx.beginPath();for(var t=0;t<=Math.PI*2+0.01;t+=Math.PI/24){var q=P(fn(t));if(t===0)ctx.moveTo(q[0],q[1]);else ctx.lineTo(q[0],q[1]);}ctx.stroke();}
 ring(function(t){return [0,Math.sin(t),Math.cos(t)];});            // meridiano vertical (centro de la cara)
 ring(function(t){return [Math.sin(t),-0.12,Math.cos(t)];});        // línea horizontal de los ojos
 // rasgos: solo visibles si están en el hemisferio que mira al espectador (z>0)
 function vis(p){return rot(p)[2]>0.02;}
 // orejas (a los lados; se ven de perfil)
 [[-1.0,-0.05,0],[1.0,-0.05,0]].forEach(function(e){if(vis(e)){var q=P(e);ctx.beginPath();ctx.ellipse(q[0],q[1],R*0.07,R*0.13,0,0,7);ctx.fillStyle=acc+'cc';ctx.fill();ctx.strokeStyle=acc;ctx.lineWidth=1.2;ctx.stroke();}});
 // cejas
 ctx.strokeStyle=acc;ctx.lineWidth=2;
 [[[-0.46,-0.28,0.86],[-0.16,-0.30,0.92]],[[0.16,-0.30,0.92],[0.46,-0.28,0.86]]].forEach(function(br){if(vis(br[0])||vis(br[1])){var a=P(br[0]),b=P(br[1]);ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke();}});
 // ojos con pupila (mirando al frente)
 [[-0.31,-0.12,0.90],[0.31,-0.12,0.90]].forEach(function(e){if(vis(e)){var q=P(e);ctx.beginPath();ctx.ellipse(q[0],q[1],R*0.11,R*0.075,0,0,7);ctx.fillStyle='#fff';ctx.fill();ctx.strokeStyle=acc;ctx.lineWidth=1.3;ctx.stroke();var pu=P([e[0]*1.02,e[1],e[2]+0.06]);ctx.beginPath();ctx.arc(pu[0],pu[1],R*0.038,0,7);ctx.fillStyle=acc;ctx.fill();}});
 // nariz que SOBRESALE (clave para leer la dirección)
 var nb=P([0,0.06,0.95]),nt=P([0,0.18,1.34]);if(nt[2]>-0.2){ctx.strokeStyle=acc;ctx.lineWidth=2.6;ctx.lineCap='round';ctx.beginPath();ctx.moveTo(nb[0],nb[1]);ctx.lineTo(nt[0],nt[1]);ctx.stroke();ctx.beginPath();ctx.arc(nt[0],nt[1],2.6,0,7);ctx.fillStyle=acc;ctx.fill();}
 // boca
 var m0=[-0.2,0.5,0.86],m1=[0.2,0.5,0.86];if(vis(m0)||vis(m1)){var a=P(m0),b=P(m1);ctx.strokeStyle=acc+'cc';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke();}
 // FLECHA DE MIRADA: desde el entrecejo hacia delante, sobresale del cráneo
 var g0=P([0,-0.1,1.0]),g1=P([0,-0.1,2.15]);var ahead=g1[2]>g0[2]||g1[2]>-0.3;
 ctx.strokeStyle=acc;ctx.lineWidth=2.4;ctx.setLineDash(ahead?[]:[3,3]);
 ctx.beginPath();ctx.moveTo(g0[0],g0[1]);ctx.lineTo(g1[0],g1[1]);ctx.stroke();ctx.setLineDash([]);
 var ang=Math.atan2(g1[1]-g0[1],g1[0]-g0[0]),ah=8;
 ctx.beginPath();ctx.moveTo(g1[0],g1[1]);ctx.lineTo(g1[0]-ah*Math.cos(ang-0.5),g1[1]-ah*Math.sin(ang-0.5));ctx.moveTo(g1[0],g1[1]);ctx.lineTo(g1[0]-ah*Math.cos(ang+0.5),g1[1]-ah*Math.sin(ang+0.5));ctx.stroke();
 ctx.fillStyle=mut;ctx.font='600 9px sans-serif';ctx.textAlign='center';ctx.fillText(trVal('mirada',LANG),g1[0],g1[1]+(g1[1]>cy?12:-7));ctx.textAlign='start';}
// esfera de órbita 3D para la CÁMARA: sujeto al centro, cámara orbitando; arrastra para mover en 3D
function drawCamSphere(cv,camYaw,camPitch,subjYaw,dist){if(!cv)return;const ctx=cv.getContext('2d'),W=cv.width,H=cv.height;ctx.clearRect(0,0,W,H);
 const c=sceneCols(),D=Math.PI/180,cx=W/2,cy=H/2,R=Math.min(W,H)*0.38,vt=20*D;
 function proj(px,py,pz){const y2=py*Math.cos(vt)-pz*Math.sin(vt),z2=py*Math.sin(vt)+pz*Math.cos(vt);return{x:cx+px*R,y:cy-y2*R,z:z2};}
 // alambre de la esfera: ecuador + meridiano frontal
 function ring(fn,front){ctx.beginPath();for(let t=0;t<=360;t+=8){const p=fn(t*D),s=proj(p[0],p[1],p[2]);t?ctx.lineTo(s.x,s.y):ctx.moveTo(s.x,s.y);}ctx.closePath();ctx.globalAlpha=front?.5:.5;ctx.strokeStyle=c.mut;ctx.lineWidth=1;ctx.stroke();ctx.globalAlpha=1;}
 ctx.beginPath();ctx.arc(cx,cy,R,0,7);ctx.globalAlpha=.25;ctx.strokeStyle=c.mut;ctx.lineWidth=1;ctx.stroke();ctx.globalAlpha=1;
 ring(t=>[Math.sin(t),0,Math.cos(t)]); // ecuador
 ring(t=>[0,Math.sin(t),Math.cos(t)]); // meridiano
 // sujeto al centro (dot + flecha de orientación, proyectada en el ecuador)
 const sp=proj(0,0,0);
 const sa=subjYaw*D,se=proj(Math.sin(sa)*0.42,0,Math.cos(sa)*0.42);
 ctx.beginPath();ctx.moveTo(sp.x,sp.y);ctx.lineTo(se.x,se.y);ctx.strokeStyle=c.mut;ctx.lineWidth=2;ctx.stroke();
 ctx.beginPath();ctx.arc(sp.x,sp.y,5,0,7);ctx.fillStyle=c.mut;ctx.fill();
 // cámara en la esfera (az=yaw, el=pitch); el radio refleja la DISTANCIA
 const az=camYaw*D,el=camPitch*D,rf=(dist==null?1:(0.5+(+dist)/100*0.75));
 const P=proj(Math.sin(az)*Math.cos(el)*rf, Math.sin(el)*rf, Math.cos(az)*Math.cos(el)*rf);
 ctx.beginPath();ctx.moveTo(sp.x,sp.y);ctx.lineTo(P.x,P.y);ctx.strokeStyle=c.acc;ctx.globalAlpha=.6;ctx.lineWidth=1.5;ctx.stroke();ctx.globalAlpha=1;
 const sz=4+(P.z+1)*2.2; // más grande si está más cerca
 ctx.save();ctx.translate(P.x,P.y);ctx.rotate(Math.atan2(sp.y-P.y,sp.x-P.x));ctx.fillStyle=c.acc;
 ctx.fillRect(-sz*0.7,-sz*0.7,sz*1.1,sz*1.4);ctx.beginPath();ctx.moveTo(sz*0.4,-sz*0.6);ctx.lineTo(sz*1.5,-sz);ctx.lineTo(sz*1.5,sz);ctx.lineTo(sz*0.4,sz*0.6);ctx.closePath();ctx.fill();ctx.restore();
 // etiquetas
 ctx.font='9px sans-serif';ctx.textAlign='center';ctx.globalAlpha=.85;ctx.fillStyle=c.txt;
 const hb=camPitch>=45?'cenital':camPitch>=18?'picado':camPitch<=-18?'contrapicado':'nivel';
 ctx.fillText('cámara · '+hb+(dist!=null?' · '+distShort(dist).toLowerCase():''), cx, H-5);ctx.globalAlpha=1;ctx.textAlign='start';}
function ang3dDraw(){if(ang3dMode==='cam')drawCamSphere($('ang3dCv'),ang3dCam.yaw,ang3dCam.pitch,ang3dSubj.yaw,ang3dCam.dist);else if(ang3dShape==='mann')drawMannequin($('ang3dCv'),ang3dSubj.yaw,ang3dSubj.pitch);else if(ang3dShape==='cube')drawCubeCv($('ang3dCv'),ang3dSubj.yaw,ang3dSubj.pitch);else drawHead($('ang3dCv'),ang3dSubj.yaw,ang3dSubj.pitch)}
function ang3dSyncSliders(){const o=ang3dActive(),y=$('ang3dYaw'),p=$('ang3dPitch');if(!y||!p)return;
 const a2=(((o.yaw+180)%360+360)%360)-180;   // normaliza el giro a -180..180
 y.value=Math.round(a2);p.value=Math.round(o.pitch);
 const cam=ang3dMode==='cam';
 $('ang3dYawLbl').textContent=trVal(cam?'Órbita':'Giro',LANG);
 $('ang3dPitchLbl').textContent=trVal(cam?'Altura':'Inclinación',LANG);
 $('ang3dYawV').textContent=Math.round(a2)+'°';
 $('ang3dPitchV').textContent=Math.round(o.pitch)+'°';
 $('ang3dDistRow').classList.toggle('hide',!cam);
 $('ang3dLensRow').classList.toggle('hide',!cam);
 $('ang3dApRow').classList.toggle('hide',!cam);
 if(cam){$('ang3dDist').value=Math.round(ang3dCam.dist);$('ang3dDistV').textContent=trVal(distShort(ang3dCam.dist),LANG);
  $('ang3dLens').value=Math.round(ang3dCam.lens);$('ang3dLensV').textContent=Math.round(ang3dCam.lens)+' mm · '+trVal(lensType(ang3dCam.lens),LANG);
  let ai=ANG_FSTOPS.indexOf(ang3dCam.fstop);if(ai<0)ai=ANG_FSTOPS.reduce((b,f,i)=>Math.abs(f-ang3dCam.fstop)<Math.abs(ANG_FSTOPS[b]-ang3dCam.fstop)?i:b,0);
  $('ang3dAp').value=ai;$('ang3dApV').textContent=fNum(ang3dCam.fstop);}}
function ang3dUpd(){ang3dDraw();ang3dSyncSliders();const t=$('ang3dTxt');if(t)t.textContent=ang3dDesc().short;}
function ang3dSnap(){const cv=$('ang3dCv');try{return cv.toDataURL('image/png').split(',')[1]}catch(e){return null}}
$('ang3dOn').onchange=()=>{const on=$('ang3dOn').checked;$('ang3dBox').classList.toggle('hide',!on);$('ang3dHint').classList.toggle('hide',!on);if(on){ang3dRenderPresets();ang3dUpd()}};
function ang3dSetMode(m){ang3dMode=m;[...$('ang3dMode').children].forEach(x=>x.classList.toggle('on',x.dataset.m===m));$('ang3dShape').classList.toggle('hide',m!=='subj');ang3dRenderPresets();ang3dUpd();}
$('ang3dMode').onclick=e=>{const b=e.target.closest('button');if(!b||b.classList.contains('off'))return;ang3dSetMode(b.dataset.m)};
function ang3dApplyUse(){const mb=$('ang3dMode');
 mb.querySelector('[data-m="subj"]').classList.toggle('off',!ang3dUse.subj);
 mb.querySelector('[data-m="cam"]').classList.toggle('off',!ang3dUse.cam);
 if(ang3dMode==='subj'&&!ang3dUse.subj&&ang3dUse.cam)ang3dSetMode('cam');
 else if(ang3dMode==='cam'&&!ang3dUse.cam&&ang3dUse.subj)ang3dSetMode('subj');}
$('ang3dUseSubj').onchange=()=>{ang3dUse.subj=$('ang3dUseSubj').checked;ang3dApplyUse();ang3dUpd();};
$('ang3dUseCam').onchange=()=>{ang3dUse.cam=$('ang3dUseCam').checked;ang3dApplyUse();ang3dUpd();};
$('ang3dShape').onclick=e=>{const b=e.target.closest('button');if(!b)return;ang3dShape=b.dataset.sh;[...$('ang3dShape').children].forEach(x=>x.classList.toggle('on',x.dataset.sh===ang3dShape));ang3dUpd()};
$('ang3dPresets').onclick=e=>{const b=e.target.closest('button');if(!b)return;const o=ang3dActive();o.yaw=+b.dataset.y;o.pitch=+b.dataset.p;ang3dUpd()};
$('ang3dIns').onclick=()=>{const d=ang3dDesc();if(!d.prompt){toast('Marca «Sujeto» o «Cámara» primero','bad');return}const ta=$('prompt');ta.value=(ta.value.trim()?ta.value.trim()+' ':'')+d.prompt;ta.dispatchEvent(new Event('input',{bubbles:true}));toast('Ángulo añadido al prompt')};
(function(){const cv=$('ang3dCv');if(!cv)return;let drag=false,lx=0,ly=0;
 cv.addEventListener('pointerdown',e=>{drag=true;lx=e.clientX;ly=e.clientY;try{cv.setPointerCapture(e.pointerId)}catch(_){}});
 cv.addEventListener('pointermove',e=>{if(!drag)return;const dx=e.clientX-lx,dy=e.clientY-ly;lx=e.clientX;ly=e.clientY;
  if(ang3dMode==='cam'){ang3dCam.yaw+=dx*0.8;ang3dCam.pitch=Math.max(-80,Math.min(80,ang3dCam.pitch-dy*0.7))}
  else{ang3dSubj.yaw+=dx*0.8;ang3dSubj.pitch=Math.max(-60,Math.min(60,ang3dSubj.pitch+dy*0.6))}
  ang3dUpd()});
 cv.addEventListener('pointerup',()=>{drag=false});cv.addEventListener('pointerleave',()=>{drag=false});})();
// dos ejes INDEPENDIENTES: un deslizador para el giro horizontal y otro para la inclinación vertical
(function(){const y=$('ang3dYaw'),p=$('ang3dPitch'),d=$('ang3dDist');if(!y||!p)return;
 const txt=()=>{const t=$('ang3dTxt');if(t)t.textContent=ang3dDesc().short;};
 y.addEventListener('input',()=>{ang3dActive().yaw=+y.value;$('ang3dYawV').textContent=y.value+'°';ang3dDraw();txt();});
 p.addEventListener('input',()=>{ang3dActive().pitch=+p.value;$('ang3dPitchV').textContent=p.value+'°';ang3dDraw();txt();});
 if(d)d.addEventListener('input',()=>{ang3dCam.dist=+d.value;$('ang3dDistV').textContent=trVal(distShort(d.value),LANG);ang3dDraw();txt();});
 const ln=$('ang3dLens'),ap=$('ang3dAp');
 if(ln)ln.addEventListener('input',()=>{ang3dCam.lens=+ln.value;$('ang3dLensV').textContent=Math.round(ang3dCam.lens)+' mm · '+trVal(lensType(ang3dCam.lens),LANG);txt();});
 if(ap)ap.addEventListener('input',()=>{ang3dCam.fstop=ANG_FSTOPS[+ap.value]||4;$('ang3dApV').textContent=fNum(ang3dCam.fstop);txt();});})();
// ===== Ángulos 3D: detección de sujetos + gizmos (experimental) =====
let poseSubs=[],poseImg={src:'',full:'',w:0,h:0},poseSel=-1,poseCam={yaw:0,pitch:0};
function poseCamUpd(){drawCamSphere($('poseCamCv'),poseCam.yaw,poseCam.pitch,0);const t=$('poseCamTxt');if(t)t.textContent=ang3dCap(camTextFor(poseCam.yaw,poseCam.pitch))}
(function(){const cv=$('poseCamCv');if(!cv)return;let drag=false,lx=0,ly=0;
 cv.addEventListener('pointerdown',e=>{drag=true;lx=e.clientX;ly=e.clientY;try{cv.setPointerCapture(e.pointerId)}catch(_){}});
 cv.addEventListener('pointermove',e=>{if(!drag)return;const dx=e.clientX-lx,dy=e.clientY-ly;lx=e.clientX;ly=e.clientY;poseCam.yaw+=dx*0.8;poseCam.pitch=Math.max(-80,Math.min(80,poseCam.pitch-dy*0.7));poseCamUpd()});
 cv.addEventListener('pointerup',()=>{drag=false});cv.addEventListener('pointerleave',()=>{drag=false});})();
(function(){const pre=$('poseCamPre');if(pre)pre.onclick=e=>{const b=e.target.closest('button');if(!b)return;poseCam.yaw=+b.dataset.y;poseCam.pitch=+b.dataset.p;poseCamUpd()}})();
function poseDownscale(img,max){const nw=img.naturalWidth,nh=img.naturalHeight;let w=nw,h=nh;
 if(Math.max(w,h)>max){const k=max/Math.max(w,h);w=Math.round(w*k);h=Math.round(h*k)}
 const c=document.createElement('canvas');c.width=w;c.height=h;c.getContext('2d').drawImage(img,0,0,w,h);
 try{return c.toDataURL('image/png').split(',')[1]}catch(e){return ''}}
function poseFacingFromDet(f){let yaw=0,pitch=0;
 if(f){if(typeof f.yaw_deg==='number')yaw=f.yaw_deg;else{const m={front:0,front_left:-35,front_right:35,left_profile:-90,right_profile:90,back_left:-145,back_right:145,back:180,unknown:0};yaw=m[f.yaw_label]||0}
  if(typeof f.pitch_deg==='number')pitch=f.pitch_deg;else{pitch=f.pitch_label==='looking_up'?22:f.pitch_label==='looking_down'?-22:0}}
 return{yaw:Math.max(-180,Math.min(180,yaw)),pitch:Math.max(-60,Math.min(60,pitch))}}
function poseFacingText(yaw,pitch){let y=(((yaw+180)%360+360)%360)-180;const a=Math.abs(y);
 const dir=y>0?'hacia la derecha de la imagen':'hacia la izquierda de la imagen';let h;
 if(a<=15)h='de frente a la cámara';
 else if(a<=65)h='en vista 3/4 mirando '+dir;
 else if(a<=115)h='de perfil, mirando '+dir;
 else if(a<=160)h='casi de espaldas ('+dir+')';
 else h='de espaldas a la cámara';
 let v='';if(pitch>=18)v=', con la vista hacia arriba';else if(pitch<=-18)v=', con la vista hacia abajo';
 return h+v}
function posePos(b){const cx=(b[0]+b[2])/2,cy=(b[1]+b[3])/2;
 const hx=cx<0.34?'a la izquierda':cx>0.66?'a la derecha':'al centro';
 const hy=cy<0.34?', arriba':cy>0.66?', abajo':'';return hx+hy}
function poseLayout(){const img=$('poseImg'),stage=$('poseStage'),ov=$('poseOv');if(!img||!img.clientWidth)return;
 const sr=stage.getBoundingClientRect(),ir=img.getBoundingClientRect();
 ov.style.left=(ir.left-sr.left)+'px';ov.style.top=(ir.top-sr.top)+'px';ov.style.width=ir.width+'px';ov.style.height=ir.height+'px';
 poseRenderCubes()}
function poseRenderCubes(){const ov=$('poseOv');if(!ov)return;
 ov.innerHTML=poseSubs.map((s,i)=>{const x=s.box[0]*100,y=s.box[1]*100,w=(s.box[2]-s.box[0])*100,h=(s.box[3]-s.box[1])*100;
  return '<div class="posebox'+(i===poseSel?' sel':'')+'" style="left:'+x+'%;top:'+y+'%;width:'+w+'%;height:'+h+'%"><span class="plabel">'+esc(s.label)+'</span></div>'
   +'<canvas class="posecube" data-i="'+i+'" width="74" height="74" style="left:'+(x+w/2)+'%;top:'+(y+h/2)+'%"></canvas>';}).join('');
 poseSubs.forEach((s,i)=>{const cv=ov.querySelector('.posecube[data-i="'+i+'"]');if(cv)drawCubeCv(cv,s.yaw,s.pitch)});
 ov.querySelectorAll('.posecube').forEach(cv=>{let drag=false,lx=0,ly=0;const i=+cv.dataset.i;
  cv.addEventListener('pointerdown',e=>{drag=true;lx=e.clientX;ly=e.clientY;poseSelect(i);try{cv.setPointerCapture(e.pointerId)}catch(_){}; e.stopPropagation()});
  cv.addEventListener('pointermove',e=>{if(!drag)return;e.stopPropagation();const s=poseSubs[i],dx=e.clientX-lx,dy=e.clientY-ly;lx=e.clientX;ly=e.clientY;s.yaw=(((s.yaw+dx*0.9)+540)%360)-180;s.pitch=Math.max(-60,Math.min(60,s.pitch+dy*0.6));drawCubeCv(cv,s.yaw,s.pitch);poseUpdDesc(i)});
  cv.addEventListener('pointerup',()=>{drag=false});cv.addEventListener('pointerleave',()=>{drag=false})})}
function poseRenderList(){$('poseList').innerHTML=poseSubs.length?poseSubs.map((s,i)=>'<div class="posesub'+(i===poseSel?' sel':'')+'" data-i="'+i+'"><div class="pnm">'+esc(s.label)+'</div><div class="pdesc" data-d="'+i+'">'+esc(poseFacingText(s.yaw,s.pitch))+'</div><div class="ppre"><button data-y="0" data-p="0">Frente</button><button data-y="-35" data-p="0">3/4 izq</button><button data-y="35" data-p="0">3/4 der</button><button data-y="-90" data-p="0">Perfil izq</button><button data-y="90" data-p="0">Perfil der</button><button data-y="180" data-p="0">Espalda</button><button data-y="0" data-p="28">Arriba</button><button data-y="0" data-p="-28">Abajo</button></div></div>').join(''):'<div class="hint" style="font-size:12px">Pulsa «Detectar» para encontrar los elementos de la imagen.</div>'}
function poseUpdDesc(i){const el=$('poseList').querySelector('.pdesc[data-d="'+i+'"]');if(el)el.textContent=poseFacingText(poseSubs[i].yaw,poseSubs[i].pitch)}
function poseSelect(i){poseSel=i;[...$('poseList').querySelectorAll('.posesub')].forEach((b,j)=>b.classList.toggle('sel',j===i));[...$('poseOv').querySelectorAll('.posebox')].forEach((b,j)=>b.classList.toggle('sel',j===i))}
$('poseList').addEventListener('click',e=>{const sub=e.target.closest('.posesub');if(!sub)return;const i=+sub.dataset.i;const pre=e.target.closest('.ppre button');
 if(pre){poseSubs[i].yaw=+pre.dataset.y;poseSubs[i].pitch=+pre.dataset.p;poseUpdDesc(i);const cv=$('poseOv').querySelector('.posecube[data-i="'+i+'"]');if(cv)drawCubeCv(cv,poseSubs[i].yaw,poseSubs[i].pitch);return}
 poseSelect(i)});
function poseOpen(src){if(!src){toast('Abre una imagen primero','bad');return}
 poseSubs=[];poseSel=-1;poseImg={src:src,full:'',w:0,h:0};$('poseList').innerHTML='';$('poseOv').innerHTML='';$('poseGen').disabled=true;
 const img=$('poseImg');img.onload=()=>{poseImg.w=img.naturalWidth;poseImg.h=img.naturalHeight;poseImg.full=poseDownscale(img,2048);poseLayout()};
 img.src=src;poseRenderList();poseCam={yaw:0,pitch:0};poseCamUpd();$('poseModal').classList.remove('hide');setTimeout(poseLayout,60)}
$('lbPose').onclick=ev=>{ev.stopPropagation();const src=$('lbImg').src;$('lightbox').classList.add('hide');poseOpen(src)};
$('poseDetect').onclick=async()=>{const img=$('poseImg');if(!img||!img.naturalWidth){toast('La imagen no cargó','bad');return}
 const max=1024;let dw=img.naturalWidth,dh=img.naturalHeight;if(Math.max(dw,dh)>max){const k=max/Math.max(dw,dh);dw=Math.round(dw*k);dh=Math.round(dh*k)}
 const det=poseDownscale(img,max);if(!det){toast('No pude leer la imagen','bad');return}
 $('poseBusy').classList.remove('hide');$('poseDetect').disabled=true;
 try{const r=await(await fetch('/detectsubjects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({b64:det,w:dw,h:dh})})).json();
  if(r.error)toast(r.error,'bad');
  else{const ds=(r.detections||[]).filter(d=>Array.isArray(d.box)&&d.box.length===4);
   poseSubs=ds.map(d=>{const f=poseFacingFromDet(d.facing);return{label:d.label||d.category||'elemento',cat:d.category,box:d.box.map(Number),yaw:f.yaw,pitch:f.pitch}});
   poseSel=poseSubs.length?0:-1;poseRenderList();poseLayout();$('poseGen').disabled=!poseSubs.length;
   toast(poseSubs.length?(poseSubs.length+(poseSubs.length>1?' elementos detectados':' elemento detectado')):'No detecté elementos',poseSubs.length?'':'bad')}
 }catch(e){toast(String(e),'bad')}
 $('poseBusy').classList.add('hide');$('poseDetect').disabled=false}
async function poseGenerate(){if(!poseSubs.length){toast('Detecta primero los elementos','bad');return}
 if(!poseImg.full){toast('La imagen aún se está cargando','bad');return}
 const lines=poseSubs.map(s=>'- '+s.label+' ('+posePos(s.box)+'): '+poseFacingText(s.yaw,s.pitch)+'.').join('\n');
 const camLine=(poseCam.yaw||poseCam.pitch)?('\nÁngulo de cámara de toda la toma: '+camTextFor(poseCam.yaw,poseCam.pitch)+'.'):'';
 const instr='Edita esta imagen cambiando ÚNICAMENTE la orientación/ángulo de los siguientes elementos. Conserva su identidad, rasgos faciales, ropa, colores, proporciones, el estilo artístico, la iluminación y todo el resto de la escena exactamente igual:\n'+lines+camLine+'\nNo dibujes cubos, cajas, flechas ni guías de ningún tipo. Mantén el fondo y los demás elementos sin cambios.';
 const ar=poseImg.w>=poseImg.h*1.15?'1536x1024':poseImg.h>=poseImg.w*1.15?'1024x1536':'1024x1024';
 const body={prompt:instr,images:[{name:'base.png',b64:poseImg.full}],size:ar,quality:$('quality').value,n:1,output_format:'png',moderation:$('mod').value,project:$('projSel').value,sub:activeSub,save_desktop:$('saveDesk').checked};
 $('poseModal').classList.add('hide');toast('Generando con los ángulos…');fireGenJob(body)}
$('poseGen').onclick=poseGenerate;
$('poseCancel').onclick=()=>$('poseModal').classList.add('hide');
$('poseModal').querySelector('.mclose').onclick=()=>$('poseModal').classList.add('hide');
$('poseModal').addEventListener('click',e=>{if(e.target===$('poseModal'))$('poseModal').classList.add('hide')});
// abrir "Ángulos por elemento" desde la columna principal (usa la referencia, el resultado o pide un archivo)
function poseOpenFromMain(){
 if(refs.length){poseOpen('data:image/png;base64,'+refs[0].b64);return}
 if(results.length&&!$('resultImg').classList.contains('hide')){poseOpen(results[active].image);return}
 toast('Elige una imagen para detectar sus ángulos','');$('poseFile').click();
}
$('poseOpenBtn').onclick=poseOpenFromMain;
$('poseFile').onchange=e=>{const f=e.target.files[0];e.target.value='';if(!f)return;
 const fr=new FileReader();fr.onload=()=>poseOpen(fr.result);fr.readAsDataURL(f)};
$('mpSd').onclick=e=>{e.preventDefault();improvePrompt($('mpSd'),'sdPrompt','video')};
$('mpKl').onclick=e=>{e.preventDefault();improvePrompt($('mpKl'),'klPrompt','video')};
$('lbDesc').onclick=async e=>{e.stopPropagation();
 const f=$('lightbox').dataset.file;if(!f){toast('Solo disponible para imágenes del historial','bad');return}
 $('lbDesc').classList.add('busy');toast('Describiendo imagen…');
 try{const d=await(await fetch('/describe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:f})})).json();
  if(d.error)toast(d.error,'bad');
  else{$('prompt').value=d.prompt;toast('Prompt de la imagen copiado al panel Crear')}
 }catch(x){toast(String(x),'bad')}
 $('lbDesc').classList.remove('busy')};

// ===== atajos de teclado =====
document.addEventListener('keydown',e=>{
 if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();
  if(mode==='video'){runVID()}
  else if(mode==='audio'){
   if(!$('sttBox').classList.contains('hide'))runSTT();
   else if(!$('sfxBox').classList.contains('hide'))runSFX();
   else if(!$('musBox').classList.contains('hide'))runMUS();
   else runTTS()}
  else if(!$('go').disabled)run();return}
 if((e.key==='ArrowRight'||e.key==='ArrowLeft')&&!$('lightbox').classList.contains('hide')){
  e.preventDefault();lbNavigate(e.key==='ArrowRight'?1:-1);return}
 if(e.key==='Escape'){
  if(!$('vfModal').classList.contains('hide')){closeVF();return}
  if(!$('cmpModal').classList.contains('hide')){$('cmpModal').classList.add('hide');return}
  if(!$('lightbox').classList.contains('hide')){$('lightbox').classList.add('hide');return}
  if(!$('maskModal').classList.contains('hide')){$('maskModal').classList.add('hide');return}
  if(!$('keyModal').classList.contains('hide')){$('keyModal').classList.add('hide');return}
  if(!$('poseModal').classList.contains('hide')){$('poseModal').classList.add('hide');return}
  if(!$('trashModal').classList.contains('hide')){$('trashModal').classList.add('hide');return}
  if(!$('tutModal').classList.contains('hide')){$('tutModal').classList.add('hide');return}
  if(!$('setModal').classList.contains('hide')){$('setModal').classList.add('hide');return}
  if(!$('projModal').classList.contains('hide')){$('projModal').classList.add('hide');return}
  if(!$('bakModal').classList.contains('hide')){$('bakModal').classList.add('hide');return}}
 const tag=document.activeElement.tagName;
 if(tag==='TEXTAREA'||tag==='INPUT'||tag==='SELECT')return;
 if(e.key==='1')setMode(lastImgMode);
 if(e.key==='2')setMode('audio');
 if(e.key==='3')setMode('video')});

// miniaturas de proporción en los presets
function buildMinis(){document.querySelectorAll('.chip[data-w]').forEach(c=>{const W=+c.dataset.w,H=+c.dataset.h,m=14;
 let bw,bh;if(W>=H){bw=m;bh=Math.max(3,Math.round(m*H/W))}else{bh=m;bw=Math.max(3,Math.round(m*W/H))}
 const s=document.createElement('span');s.className='mini';s.style.width=bw+'px';s.style.height=bh+'px';c.insertBefore(s,c.firstChild)})}
// ===== Estante de imágenes propias (local, en tu equipo · no en OpenAI) =====
let shelfItems=[],shelfSubs=new Set(['all']),shelfGroups=[];
function shelfFileSub(f){const c=$('shelfGrid').querySelector('.scard[data-shelf="'+(window.CSS&&CSS.escape?CSS.escape(f):f)+'"]');return c?(c.dataset.sub||''):(activeSub||'')}
function renderShelfChips(){const c=$('shelfSubChips');if(!c)return;const subs=curSubs();
 if(!subs.length){c.innerHTML='';return}
 const chip=(k,lbl,dr)=>`<button class="subchip${shelfSubs.has(k)?' on':''}${dr?' subdrag':''}" data-k="${esc(k)}"${dr?' draggable="true"':''}>${esc(lbl)}</button>`;
 c.innerHTML=chip('all',trVal('Todos',LANG))+chip('',trVal('Raíz',LANG))+subs.map(s=>chip(s.key,s.label,true)).join('')}
function wireSubReorder(cid){const c=$(cid);if(!c)return;
 c.addEventListener('dragstart',e=>{const b=e.target.closest('.subdrag');if(!b)return;e.dataTransfer.setData('text/x-subk',b.dataset.k);e.dataTransfer.effectAllowed='move';window.__subck=b.dataset.k;b.classList.add('chipdrag')});
 c.addEventListener('dragend',()=>{[...c.querySelectorAll('.subchip')].forEach(x=>x.classList.remove('chipdrag','chipdropt'))});
 c.addEventListener('dragover',e=>{if([...e.dataTransfer.types].indexOf('text/x-subk')<0)return;e.preventDefault();const b=e.target.closest('.subdrag');[...c.querySelectorAll('.subchip')].forEach(x=>x.classList.remove('chipdropt'));if(b&&b.dataset.k!==window.__subck)b.classList.add('chipdropt')});
 c.addEventListener('drop',async e=>{if([...e.dataTransfer.types].indexOf('text/x-subk')<0)return;e.preventDefault();const b=e.target.closest('.subdrag');[...c.querySelectorAll('.subchip')].forEach(x=>x.classList.remove('chipdropt','chipdrag'));const src=window.__subck;if(!b||b.dataset.k===src)return;
  const ord=[...c.querySelectorAll('.subdrag')].map(x=>x.dataset.k).filter(k=>k!==src);const i=ord.indexOf(b.dataset.k);ord.splice(i<0?ord.length:i,0,src);
  const r=await jpost('/suborder',{project:curProj(),order:ord});if(r&&r.error){toast(r.error,'bad');return}
  await loadProjects();renderGalChips();renderShelfChips();toast('Subproyectos reordenados')})}
wireSubReorder('galSubChips');wireSubReorder('shelfSubChips');
$('shelfSubChips').onclick=e=>{const b=e.target.closest('.subchip');if(!b)return;const k=b.dataset.k;
 if(k==='all'){shelfSubs=new Set(['all'])}else{shelfSubs.delete('all');if(shelfSubs.has(k))shelfSubs.delete(k);else shelfSubs.add(k);if(!shelfSubs.size)shelfSubs.add('')}
 renderShelfChips();loadShelf()};
async function loadShelf(){const subs=curSubs();
 let groups;
 if(!subs.length){groups=[{k:activeSub||''}]}
 else if(shelfSubs.has('all')){groups=[{k:''}].concat(subs.map(s=>({k:s.key})))}
 else{groups=[...shelfSubs].map(k=>({k}))}
 if(!groups.length)groups=[{k:''}];
 shelfGroups=[];let dir='';
 for(const g of groups){try{const r=await(await fetch('/shelf?project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(g.k))).json();
  const items=r.items||[];items.forEach(it=>it._sub=g.k);shelfGroups.push({k:g.k,items});if(r.dir&&g.k===(activeSub||''))dir=r.dir;if(r.dir&&!dir)dir=r.dir}catch(e){shelfGroups.push({k:g.k,items:[]})}}
 shelfItems=shelfGroups.length===1?shelfGroups[0].items:[].concat(...shelfGroups.map(g=>g.items));
 if(dir)$('shelfDirLbl').textContent=dir;renderShelf()}
let shelfSelMode=false;const shelfSel=new Set();
let shMarqueed=false,shMarq=null,shMqStart=null,shMqMoved=false,shAnchor=-1;
function scardHtml(it){const u='/shelffile?name='+encodeURIComponent(it.file)+'&project='+encodeURIComponent(curProj())+(it._sub?'&sub='+encodeURIComponent(it._sub):'');const sb=esc(it._sub||'');
 const drg=(shelfSelMode&&!shelfSel.has(it.file))?'false':'true';  // en selección, solo las SELECCIONADAS se arrastran (a Referencias); las demás no, para que el recuadro reciba el puntero
 return `<div class="scard${shelfSel.has(it.file)?' sel':''}" title="${esc(it.name||'')}" draggable="${drg}" data-shelf="${esc(it.file)}" data-sub="${sb}"><img src="${u}&thumb=1" alt="${esc(it.name||'')}" loading="lazy" draggable="${drg}">
  ${colDots(it.colors)}${colPick(it.colors)}
  <div class="sov"><button class="sbtn use" data-file="${esc(it.file)}" title="Usar como referencia"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button>
  <a class="sbtn" href="${u}" download="${esc(it.name||it.file)}" title="Descargar"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg></a>
  <button class="sbtn desc" data-file="${esc(it.file)}" title="Describir → prompt (visión)"><svg viewBox="0 0 24 24"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg></button>
  <button class="sbtn smove" data-file="${esc(it.file)}" title="Mover a otro proyecto o subproyecto"><svg viewBox="0 0 24 24"><path d="M14 5l7 7-7 7M21 12H3"/></svg></button>
  <button class="sbtn sshare" data-file="${esc(it.file)}" title="Compartir · WhatsApp, Telegram, redes…">${GSHARE}</button>
  <button class="sbtn del" data-file="${esc(it.file)}" title="Desechar (quitar de Mis imágenes)">${GTR}</button></div></div>`}
function renderShelf(){
 $('shelfEmpty').classList.toggle('hide',shelfItems.length>0);
 $('shelfGrid').classList.toggle('selmode',shelfSelMode);
 const cf=shelfColFilter,passC=it=>!cf.size||((it.colors||[]).some(c=>cf.has(c)));
 if(shelfGroups.length>1){const subs=curSubs();
  const cn={r:'rojas',y:'amarillas',g:'verdes',b:'azules'};
  const secActs='<span class="secacts"><span class="secdots">'+IMGCOLS.map(c=>'<button class="secdot '+c+'" data-col="'+c+'" title="Seleccionar las imágenes '+cn[c]+' de este proyecto"></button>').join('')+'</span><button class="secbtn secall" title="Seleccionar todo lo de este proyecto"><svg viewBox="0 0 24 24"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></button><button class="secbtn secshare" title="Compartir las imágenes de este proyecto">'+GSHARE+'</button></span>';
  $('shelfGrid').innerHTML=shelfGroups.map(g=>{const lbl=g.k===''?'Raíz':((subs.find(s=>s.key===g.k)||{}).label||g.k);
   const inner=(g.items||[]).filter(passC).map(it=>scardHtml(Object.assign({},it,{_sub:g.k}))).join('')||'<div class="hint">Vacío</div>';
   return `<div class="histgroup shelfsec" data-sub="${esc(g.k)}" style="grid-column:1/-1"><div class="histgrouphdr"><span class="ghname">${esc(lbl)}</span>${secActs}</div><div class="shelfgrid">${inner}</div></div>`}).join('');
 }else{
  $('shelfGrid').innerHTML=shelfItems.filter(passC).map(it=>scardHtml(it)).join('');
 }}
async function shelfAddImages(imgs){return shelfAddTo(imgs,activeSub)}
async function shelfAddTo(imgs,sub){if(!imgs.length)return;
 const r=await(await fetch('/shelfadd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({images:imgs,project:curProj(),sub:sub||''})})).json();
 if(r.error){toast(r.error,'bad');return;}
 if(r.skipped&&r.skipped.length)toast(r.skipped.length+' descartada(s) por formato no válido','bad');
 await loadShelf();
 const n=imgs.length-(r.skipped?r.skipped.length:0);
 if(n>0){const subs=curSubs();const lbl=sub?((subs.find(s=>s.key===sub)||{}).label||sub):'Raíz';toast(n+(n>1?' imágenes guardadas':' imagen guardada')+' en Mis imágenes · '+lbl);}}
async function shelfAddFiles(files,sub){const imgs=[];let bad=0;
 for(const f of files){if(!OK_IMG_TYPES.has(f.type)){bad++;continue}imgs.push({name:f.name,b64:await fileToB64(f)});}
 if(bad)toast(bad+(bad>1?' archivos ignorados':' archivo ignorado')+': solo PNG/JPEG/WebP/GIF','bad');
 await shelfAddTo(imgs,sub===undefined?activeSub:sub);}
$('shelfAddBtn').onclick=()=>$('shelfFile').click();
$('shelfAll').onclick=()=>{const sp=shelfSubs.has('all')?'&subs=all':('&subs='+encodeURIComponent([...shelfSubs].join(',')));window.open('/galeria?src=shelf&project='+encodeURIComponent(curProj())+sp,'_blank','noopener')};
$('shelfFile').onchange=e=>{const arr=[...e.target.files];e.target.value='';const vid=arr.find(isVideoFile);
 if(vid)openVideoFrames(vid,'shelf');const imgs=arr.filter(f=>!isVideoFile(f));if(imgs.length)shelfAddFiles(imgs);};
$('shelfGrid').onclick=async e=>{
 if(shelfSelMode){if(shMarqueed){shMarqueed=false;return}const card=e.target.closest('.scard');if(card){
   const cards=[...$('shelfGrid').querySelectorAll('.scard')],idx=cards.indexOf(card);
   if(e.shiftKey&&shAnchor>=0&&shAnchor<cards.length){  // Shift: seleccionar el rango de punta a punta
    const lo=Math.min(shAnchor,idx),hi=Math.max(shAnchor,idx);
    for(let i=lo;i<=hi;i++){shelfSel.add(cards[i].dataset.shelf);cards[i].classList.add('sel');cards[i].draggable=true;}
   }else{const f=card.dataset.shelf;const now=!shelfSel.has(f);if(now)shelfSel.add(f);else shelfSel.delete(f);card.classList.toggle('sel',now);card.draggable=now;shAnchor=idx;}
   renderShelfBulk()}return}
 const cpd=e.target.closest('.cpdot');
 if(cpd){e.stopPropagation();const card=e.target.closest('.scard');const f=card.dataset.shelf;
  const active=[...card.querySelectorAll('.cpdot.on')].map(x=>x.dataset.col);const next=toggleCol(active,cpd.dataset.col);
  cpd.classList.toggle('on');updCdots(card,next);flashCard(card,cpd.dataset.col,cpd,next.includes(cpd.dataset.col));
  const it=shelfItems.find(x=>x.file===f);if(it)it.colors=next;
  setImgColors(f,next,'shelf',shelfFileSub(f));if(shelfColFilter.size)setTimeout(renderShelf,460);return;}
 const ssh=e.target.closest('.sshare');
 if(ssh){e.stopPropagation();const c=e.target.closest('.scard');if(c){const ssub=c.dataset.sub||'';openSharePop(ssh,'/shelffile?name='+encodeURIComponent(c.dataset.shelf)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(ssub),c.dataset.shelf);}return;}
 const use=e.target.closest('.use'),del=e.target.closest('.del'),desc=e.target.closest('.desc');
 const smv=e.target.closest('.smove');
 if(smv){e.stopPropagation();const f=smv.dataset.file;openShelfMovePop(smv,f,shelfFileSub(f));return;}
 if(use){const it=shelfItems.find(x=>x.file===use.dataset.file);if(!it)return;const ssub=shelfFileSub(it.file);
  const b=await(await fetch('/shelffile?name='+encodeURIComponent(it.file)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(ssub))).blob();
  refs.push({name:it.name||it.file,b64:await blobToB64(b)});renderThumbs();
  if(mode!=='editar')setMode('editar');validate();toast('Añadida como referencia');return;}
 if(desc){const it=shelfItems.find(x=>x.file===desc.dataset.file);if(!it)return;const ssub=shelfFileSub(it.file);
  desc.classList.add('busy');toast('Describiendo imagen…');
  try{const d=await(await fetch('/describe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:it.file,shelf:true,project:curProj(),sub:ssub})})).json();
   if(d.error)toast(d.error,'bad');
   else{setMode('crear');$('prompt').value=d.prompt;validate();$('prompt').focus();toast('Prompt de la imagen copiado al panel Crear');}
  }catch(x){toast(String(x),'bad')}
  desc.classList.remove('busy');return;}
 if(del){const f=del.dataset.file;const ssub=shelfFileSub(f);
  await fetch('/shelfdel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:f,project:curProj(),sub:ssub})});
  await loadShelf();return;}
 if(e.target.closest('a,.sbtn'))return;     // descargar u otro botón → su acción nativa
 const card=e.target.closest('.scard');     // clic en la imagen → ampliar en lightbox flotante
 if(card){const it=shelfItems.find(x=>x.file===card.dataset.shelf);const ssub=card.dataset.sub||'';
  if(it){openLb('/shelffile?name='+encodeURIComponent(it.file)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(ssub),'','');lbScope='shelf';lbCurFile=it.file;lbSyncNav()}}};
// arrastrar una imagen DEL estante hacia otra zona (p.ej. memoria visual o referencias)
$('shelfGrid').addEventListener('dragstart',e=>{const card=e.target.closest('.scard');if(!card)return;
 if(shelfSelMode&&shelfSel.size>1&&shelfSel.has(card.dataset.shelf)){
  const arr=[...$('shelfGrid').querySelectorAll('.scard')].filter(c=>shelfSel.has(c.dataset.shelf)).map(c=>({file:c.dataset.shelf,sub:c.dataset.sub||''}));
  e.dataTransfer.setData('text/x-studio-shelfs',JSON.stringify(arr));}
 e.dataTransfer.setData('text/x-studio-shelf',card.dataset.shelf);e.dataTransfer.setData('text/x-studio-shelfsub',card.dataset.sub||'');e.dataTransfer.effectAllowed='copyMove';markDropZones(true);
 if(!shelfSelMode)gridReorderStart(card,$('shelfGrid'),'.scard','shelf','shelf');});
// soltar sobre una sección de subproyecto: del estante = mover ahí; del historial = copiar ahí
const ANGSEC_TYPES=['text/x-studio-shelf','text/x-studio-file','text/x-studio-files'];
$('shelfGrid').addEventListener('dragover',e=>{
 if(reorderDrag&&reorderDrag.grid===$('shelfGrid')){gridReorderOver(e,$('shelfGrid'),'.scard');if(e.defaultPrevented){[...$('shelfGrid').querySelectorAll('.shelfsec.secdrop')].forEach(x=>x.classList.remove('secdrop'));return;}}
 const t=[...e.dataTransfer.types];if(t.indexOf('Files')<0&&!ANGSEC_TYPES.some(x=>t.indexOf(x)>=0))return;const sec=e.target.closest('.shelfsec');if(!sec)return;e.preventDefault();e.stopPropagation();[...$('shelfGrid').querySelectorAll('.shelfsec.secdrop')].forEach(x=>x.classList.remove('secdrop'));sec.classList.add('secdrop')});
$('shelfGrid').addEventListener('dragleave',e=>{const sec=e.target.closest('.shelfsec');if(sec&&!sec.contains(e.relatedTarget))sec.classList.remove('secdrop')});
$('shelfGrid').addEventListener('drop',async e=>{
 if(reorderDrag&&reorderDrag.grid===$('shelfGrid')){
  const sc=e.target.closest('.shelfsec'),tsub=sc?(sc.dataset.sub||''):'';
  if(reorderDrag.moved||!sc||tsub===reorderDrag.sub){   // reorder, o soltar en el mismo grupo: bloquear (no ir a Referencias)
   e.preventDefault();e.stopPropagation();
   if(reorderDrag.moved){reorderDrag.persisted=true;persistGridOrder(reorderDrag);toast('Orden actualizado');}
   [...$('shelfGrid').querySelectorAll('.secdrop')].forEach(x=>x.classList.remove('secdrop'));return;}
  // si no, es soltar en OTRA sección → cae al movimiento de abajo
 }
 const sec=e.target.closest('.shelfsec');if(!sec)return;
 const shelfFile=e.dataTransfer.getData('text/x-studio-shelf'),histFile=e.dataTransfer.getData('text/x-studio-file'),histMulti=e.dataTransfer.getData('text/x-studio-files');
 const extFiles=(e.dataTransfer.files&&e.dataTransfer.files.length)?[...e.dataTransfer.files]:[];
 if(!shelfFile&&!histFile&&!histMulti&&!extFiles.length)return;
 e.preventDefault();e.stopPropagation();
 [...$('shelfGrid').querySelectorAll('.secdrop')].forEach(x=>x.classList.remove('secdrop'));const sh=$('shelf');if(sh)sh.classList.remove('dragover');
 const tgtSub=sec.dataset.sub||'';
 if(extFiles.length){const vid=extFiles.find(isVideoFile);   // archivos del SO → directo al subproyecto de esta sección
  if(vid){openVideoFrames(vid,'shelf');const ims=extFiles.filter(f=>!isVideoFile(f));if(ims.length)await shelfAddFiles(ims,tgtSub);if(extFiles.filter(isVideoFile).length>1)toast('Solo proceso un video a la vez','bad');return;}
  await shelfAddFiles(extFiles,tgtSub);return;}
 const multiShelf=e.dataTransfer.getData('text/x-studio-shelfs');   // selección múltiple: mover TODAS
 if(multiShelf){try{const arr=JSON.parse(multiShelf);if(arr.length>1){await shelfMoveMany(arr,tgtSub);return;}}catch(_){}}
 if(shelfFile){const srcSub=e.dataTransfer.getData('text/x-studio-shelfsub')||'';if(srcSub===tgtSub)return;await shelfMoveOne(shelfFile,srcSub,curProj(),tgtSub,'shelf');return;}
 const imgs=await imagesFromDT(e.dataTransfer);   // imagen(es) del historial → copiar a Mis imágenes de esa sección
 if(imgs.length)await shelfAddTo(imgs,tgtSub);});
$('shelfGrid').addEventListener('dragend',()=>{[...$('shelfGrid').querySelectorAll('.secdrop')].forEach(x=>x.classList.remove('secdrop'))});
// controles en la cabecera de cada sección (proyecto/subproyecto): por color, todo, compartir — solo lo de ESA sección
function enterShelfSel(){if(!shelfSelMode){shelfSelMode=true;if(selMode){selMode=false;selFiles.clear();renderGal();renderBulk();}$('shelfSelBtn').classList.add('on');}}
$('shelfGrid').addEventListener('click',e=>{
 const sd=e.target.closest('.secdot'),sa=e.target.closest('.secall'),ss=e.target.closest('.secshare');
 if(!sd&&!sa&&!ss)return;
 e.preventDefault();e.stopPropagation();
 const sec=e.target.closest('.shelfsec');if(!sec)return;
 const sub=sec.dataset.sub||'',cards=[...sec.querySelectorAll('.scard')];
 if(ss){const items=cards.map(cd=>({url:'/shelffile?name='+encodeURIComponent(cd.dataset.shelf)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(sub),filename:cd.dataset.shelf}));
  if(!items.length){toast('No hay imágenes en este proyecto','bad');return}openSharePopMulti(ss,items);return;}
 const cardColors=cd=>[...cd.querySelectorAll('.cdots .cdot')].map(d=>[...d.classList].find(x=>x!=='cdot'));
 let target=cards;
 if(sd){const c=sd.dataset.col;target=cards.filter(cd=>cardColors(cd).indexOf(c)>=0);
  if(!target.length){toast('No hay imágenes de ese color en este proyecto','bad');return}}
 enterShelfSel();
 const allSel=target.length&&target.every(cd=>shelfSel.has(cd.dataset.shelf));
 target.forEach(cd=>{if(allSel)shelfSel.delete(cd.dataset.shelf);else shelfSel.add(cd.dataset.shelf)});
 renderShelf();renderShelfBulk();
},true);
// salir del modo selección de Mis imágenes al hacer clic fuera (no en el estante, la barra de acciones o un popup)
function exitShelfSel(){if(!shelfSelMode)return;shelfSelMode=false;shelfSel.clear();$('shelfSelBtn').classList.remove('on');renderShelf();renderShelfBulk();}
document.addEventListener('click',e=>{
 if(!shelfSelMode||shMarqueed)return;
 // mantener la selección solo si se interactúa con una tarjeta, los controles de sección, la barra de acciones, el botón Seleccionar o un popup; cualquier otro clic (zona vacía o fuera) sale
 if(e.target.closest('.scard')||e.target.closest('.secacts')||e.target.closest('#shelfSelBtn')||e.target.closest('#shelfBulk')||e.target.closest('.movepop')||e.target.closest('.sharepop'))return;
 exitShelfSel();
});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&shelfSelMode){const t=(document.activeElement||{}).tagName;if(t==='INPUT'||t==='TEXTAREA')return;exitShelfSel();}});
// ── Mis imágenes: modo selección (clic + arrastre de recuadro) ──────────────
$('shelfSelBtn').onclick=()=>{shelfSelMode=!shelfSelMode;shelfSel.clear();
 if(shelfSelMode&&selMode){selMode=false;selFiles.clear();renderGal();renderBulk();}  // evita dos barras a la vez
 $('shelfSelBtn').classList.toggle('on',shelfSelMode);renderShelf();renderShelfBulk();};
function renderShelfBulk(){const bar=$('shelfBulk');if(!shelfSelMode){bar.classList.add('hide');return}
 if(bar.parentNode!==document.body)document.body.appendChild(bar);  // fixed relativo al viewport
 bar.classList.remove('hide');
 bar.innerHTML='<span class="gbcount">'+shelfSel.size+' seleccionada'+(shelfSel.size===1?'':'s')+'</span>'
  +'<button id="shBulkAll" title="Seleccionar todas"><svg viewBox="0 0 24 24" style="width:15px;height:15px"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="M8 12l2.8 2.8L16.5 9"/></svg>Todo</button>'
  +'<button id="shBulkNone" title="Deseleccionar todas"><svg viewBox="0 0 24 24" style="width:15px;height:15px"><rect x="3" y="3" width="18" height="18" rx="4"/></svg>Ninguna</button>'
  +'<button id="shBulkMove">'+GCM+'Mover</button>'
  +'<button id="shBulkCopy">'+GCP+'Copiar</button>'
  +'<button id="shBulkShare">'+GSHARE+'Compartir</button>'
  +'<button id="shBulkDel" class="bdel">'+GTR+'Borrar</button>'
  +'<button id="shBulkExit">Salir</button>';
 $('shBulkAll').onclick=()=>{[...document.querySelectorAll('#shelfGrid .scard')].forEach(c=>shelfSel.add(c.dataset.shelf));renderShelf();renderShelfBulk()};
 $('shBulkNone').onclick=()=>{shelfSel.clear();renderShelf();renderShelfBulk()};
 $('shBulkMove').onclick=e=>{e.stopPropagation();if(!shelfSel.size){toast('Selecciona imágenes primero','bad');return}if($('movePop')){closeMovePop();return}openShelfBulkMovePop(e.currentTarget,'move')};
 $('shBulkCopy').onclick=e=>{e.stopPropagation();if(!shelfSel.size){toast('Selecciona imágenes primero','bad');return}if($('movePop')){closeMovePop();return}openShelfBulkMovePop(e.currentTarget,'copy')};
 $('shBulkShare').onclick=e=>{e.stopPropagation();if(!shelfSel.size){toast('Selecciona imágenes primero','bad');return}
  const items=[...shelfSel].map(f=>({url:'/shelffile?name='+encodeURIComponent(f)+'&project='+encodeURIComponent(curProj())+'&sub='+encodeURIComponent(shelfFileSub(f)),filename:f}));
  openSharePopMulti(e.currentTarget,items)};
 $('shBulkExit').onclick=()=>{shelfSelMode=false;shelfSel.clear();$('shelfSelBtn').classList.remove('on');renderShelf();renderShelfBulk()};
 $('shBulkDel').onclick=async(e)=>{const b=e.currentTarget;if(!shelfSel.size){toast('Selecciona imágenes primero','bad');return}
  if(!b.classList.contains('arm')){b.classList.add('arm');b.lastChild.textContent='¿Quitar '+shelfSel.size+'?';setTimeout(()=>{b.classList.remove('arm');renderShelfBulk()},2600);return}
  const proj=curProj();const byd={};for(const f of shelfSel){const s=shelfFileSub(f);(byd[s]=byd[s]||[]).push(f)}
  const groups=[];for(const s of Object.keys(byd)){const r=await jpost('/deleteitems',{src:'shelf',files:byd[s],project:proj,sub:s});if(r&&r.undo)groups.push({sub:s,items:r.undo})}
  const k=shelfSel.size;shelfSelMode=false;shelfSel.clear();$('shelfSelBtn').classList.remove('on');await loadShelf();renderShelfBulk();toast(k+' quitada(s) de Mis imágenes · ⌘Z para deshacer');
  pushUndo({label:k+' quitada(s) de Mis imágenes',
   undo:async()=>{for(const g of groups){await jpost('/restoreitems',{src:'shelf',project:proj,sub:g.sub,items:g.items})}await loadShelf();},
   redo:async()=>{for(const g of groups){const r=await jpost('/deleteitems',{src:'shelf',files:g.items.map(u=>(u.entry||{}).file).filter(Boolean),project:proj,sub:g.sub});if(r&&r.undo)g.items=r.undo}await loadShelf();}})};}
$('shelfGrid').addEventListener('pointerdown',e=>{
 if(!shelfSelMode||e.button!==0)return;
 if(e.target.closest('a,button'))return;
 const card=e.target.closest('.scard');
 if(card&&shelfSel.has(card.dataset.shelf))return;  // arrastrar una YA seleccionada = sacar la selección a Referencias (drag nativo)
 e.preventDefault();shMqStart={x:e.clientX,y:e.clientY};shMqMoved=false;});
window.addEventListener('pointermove',e=>{
 if(!shelfSelMode||!shMqStart)return;
 const dx=e.clientX-shMqStart.x,dy=e.clientY-shMqStart.y;
 if(!shMqMoved&&Math.abs(dx)+Math.abs(dy)<6)return;
 shMqMoved=true;e.preventDefault();
 if(!shMarq){shMarq=document.createElement('div');shMarq.className='gmarq';document.body.appendChild(shMarq);}
 const x1=Math.min(e.clientX,shMqStart.x),y1=Math.min(e.clientY,shMqStart.y),x2=Math.max(e.clientX,shMqStart.x),y2=Math.max(e.clientY,shMqStart.y);
 shMarq.style.cssText='position:fixed;left:'+x1+'px;top:'+y1+'px;width:'+(x2-x1)+'px;height:'+(y2-y1)+'px;display:block';
 $('shelfGrid').querySelectorAll('.scard').forEach(c=>{const r=c.getBoundingClientRect();
  if(!(r.right<x1||r.left>x2||r.bottom<y1||r.top>y2)&&!shelfSel.has(c.dataset.shelf)){shelfSel.add(c.dataset.shelf);c.classList.add('sel');c.draggable=true;}});
 renderShelfBulk();});
function endShMarq(){if(!shMqStart)return;if(shMarq){shMarq.remove();shMarq=null;}if(shMqMoved)shMarqueed=true;shMqStart=null;shMqMoved=false;}
window.addEventListener('pointerup',endShMarq);
window.addEventListener('pointercancel',endShMarq);
// soltar una imagen (del historial o del estante) sobre un CHIP de subproyecto → archivarla ahí (siempre visible)
$('shelfSubChips').addEventListener('dragover',e=>{const t=[...e.dataTransfer.types];if(!ANGSEC_TYPES.some(x=>t.indexOf(x)>=0))return;const c=e.target.closest('.subchip');if(!c||c.dataset.k==='all')return;e.preventDefault();e.stopPropagation();[...$('shelfSubChips').querySelectorAll('.chipdropt')].forEach(x=>x.classList.remove('chipdropt'));c.classList.add('chipdropt')});
$('shelfSubChips').addEventListener('dragleave',e=>{const c=e.target.closest('.subchip');if(c&&!c.contains(e.relatedTarget))c.classList.remove('chipdropt')});
$('shelfSubChips').addEventListener('drop',async e=>{const c=e.target.closest('.subchip');if(!c||c.dataset.k==='all')return;
 const shelfFile=e.dataTransfer.getData('text/x-studio-shelf'),histFile=e.dataTransfer.getData('text/x-studio-file'),histMulti=e.dataTransfer.getData('text/x-studio-files');
 if(!shelfFile&&!histFile&&!histMulti)return;
 e.preventDefault();e.stopPropagation();[...$('shelfSubChips').querySelectorAll('.chipdropt')].forEach(x=>x.classList.remove('chipdropt'));
 const tgtSub=c.dataset.k||'';
 if(shelfFile){const srcSub=e.dataTransfer.getData('text/x-studio-shelfsub')||'';if(srcSub===tgtSub)return;await shelfMoveOne(shelfFile,srcSub,curProj(),tgtSub,'shelf');return;}
 const imgs=await imagesFromDT(e.dataTransfer);if(imgs.length)await shelfAddTo(imgs,tgtSub);});
$('shelfSubChips').addEventListener('dragend',()=>{[...$('shelfSubChips').querySelectorAll('.chipdropt')].forEach(x=>x.classList.remove('chipdropt'))});
// arrastrar imágenes sobre el estante
const shEl=$('shelf');
shEl.addEventListener('dragover',e=>{e.preventDefault();shEl.classList.add('dragover');});
shEl.addEventListener('dragleave',e=>{if(e.target===shEl)shEl.classList.remove('dragover');});
shEl.addEventListener('drop',async e=>{e.preventDefault();e.stopPropagation();shEl.classList.remove('dragover');
 if(e.dataTransfer.files.length){const arr=[...e.dataTransfer.files];const vid=arr.find(isVideoFile);
  if(vid){openVideoFrames(vid,'shelf');const imgs=arr.filter(f=>!isVideoFile(f));if(imgs.length)shelfAddFiles(imgs);
   if(arr.filter(isVideoFile).length>1)toast('Solo proceso un video a la vez','bad');return;}
  shelfAddFiles(arr);return;}
 if(e.dataTransfer.getData('text/x-studio-shelf'))return; // ya está en el estante
 const imgs=await imagesFromDT(e.dataTransfer);   // historial o resultado → estante
 if(imgs.length)await shelfAddImages(imgs);});
// carpeta de guardado configurable
async function saveShelfDir(p){if(!p)return;
 const r=await(await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shelf_dir:p,project:curProj(),sub:activeSub})})).json();
 if(r.error){toast(r.error,'bad');return;}
 if(r.shelf_effective)$('shelfDirLbl').textContent=r.shelf_effective;
 $('shelfDirRow').classList.add('hide');toast('«Mis imágenes» de este proyecto se guardarán en '+(r.shelf_effective||'tu carpeta'));loadShelf();}
$('shelfDirEdit').onclick=async()=>{toast('Abriendo selector de carpeta…');
 try{const r=await(await fetch('/pickfolder')).json();
  if(r.path)saveShelfDir(r.path);else if(r.error)toast(r.error,'bad');
 }catch(e){toast(String(e),'bad')}};
$('shelfDirSave').onclick=()=>saveShelfDir($('shelfDirIn').value);
$('shelfDirIn').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();$('shelfDirSave').click();}});
// carpeta del HISTORIAL (copia externa de las imágenes generadas)
async function saveHistDir(p){if(!p)return;
 const c=await(await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({save_dir:p,project:curProj(),sub:activeSub})})).json();
 if(c.error){toast(c.error,'bad');return;}
 cfgEffective=c.effective||cfgEffective;
 if(!$('saveDesk').checked){$('saveDesk').checked=true;localStorage.setItem('studio_desk','1');}
 renderSaveWhere();
 if($('histDirLbl'))$('histDirLbl').textContent=c.effective||'(carpeta interna de la app)';
 if($('setGenPath'))$('setGenPath').textContent=c.effective;
 toast('Las imágenes de este proyecto se copiarán en '+c.effective);}
$('histDirEdit').onclick=async()=>{toast('Abriendo selector de carpeta…');
 try{const r=await(await fetch('/pickfolder')).json();
  if(r.path)saveHistDir(r.path);else if(r.error)toast(r.error,'bad');
 }catch(e){toast(String(e),'bad')}};
// ===== marcado de validez de presets para gpt-image-2 =====
const NATIVE_SIZES=new Set(['1024x1024','1536x1024','1024x1536']);
function chipValid(w,h){return w%16===0&&h%16===0&&w>=512&&h>=512&&Math.max(w,h)<=3840
 &&Math.max(w,h)/Math.min(w,h)<=3.0001&&w*h>=655360&&w*h<=8294400;}
function markValidChips(){
 document.querySelectorAll('.chip[data-w]').forEach(c=>{
  const w=+c.dataset.w,h=+c.dataset.h,nat=NATIVE_SIZES.has(w+'x'+h),v=chipValid(w,h);
  c.classList.toggle('gnat',nat);c.classList.toggle('gok',v&&!nat);c.classList.toggle('gbad',!v);
  c.title=v?`${w}×${h} · ${nat?'nativo gpt-image-2':'válido (lados ÷16)'}`:`${w}×${h} · NO válido para gpt-image-2`;});
 // los chips de resolución ajustan el área a un tamaño válido, así que siempre funcionan
 document.querySelectorAll('.rchip').forEach(c=>c.classList.add('gok'));
}
// ===== Temas e idioma =====
const THEMES=['carbon','medianoche','neon','dia','bruma','crema'];
function applyTheme(t,save){if(!THEMES.includes(t))t='crema';
 if(t==='carbon')document.body.removeAttribute('data-theme');else document.body.dataset.theme=t;
 if(save)localStorage.setItem('studio_theme',t);
 document.querySelectorAll('.swatch').forEach(s=>s.classList.toggle('on',s.dataset.theme===t));}
let _i18nTxt=[],_i18nAttr=[],_i18nHtml=[];
function trVal(key,lang){const e=(window.I18N||{})[key];return lang==='es'||!e?key:(e[lang]||key);}
function i18nSnapshot(){const I=window.I18N||{};
 _i18nHtml=[];const htmlEls=new Set();
 document.querySelectorAll('p,span,label,button,option,h1,h2,h3,a,div').forEach(el=>{
  const h=el.innerHTML.trim();if(h&&I[h]&&/[<&]/.test(h)){_i18nHtml.push({el,orig:el.innerHTML});htmlEls.add(el);}});
 _i18nTxt=[];const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let n;
 while(n=w.nextNode()){const p=n.parentElement;if(!p||p.tagName==='SCRIPT'||p.tagName==='STYLE')continue;
  let a=p,skip=false;while(a){if(htmlEls.has(a)){skip=true;break}a=a.parentElement}if(skip)continue;
  const t=n.nodeValue.trim();if(t&&I[t])_i18nTxt.push({node:n,orig:n.nodeValue});}
 _i18nAttr=[];
 document.querySelectorAll('[placeholder],[title],[alt]').forEach(el=>{
  ['placeholder','title','alt'].forEach(at=>{const v=el.getAttribute(at);if(v){const k=v.trim();if(I[k])_i18nAttr.push({el,attr:at,orig:v});}});});}
function applyLang(lang){LANG=lang;localStorage.setItem('studio_lang',lang);document.documentElement.lang=lang;
 _i18nHtml.forEach(o=>{o.el.innerHTML=lang==='es'?o.orig:trVal(o.orig.trim(),lang);});
 _i18nTxt.forEach(o=>{const k=o.orig.trim();o.node.nodeValue=lang==='es'?o.orig:o.orig.replace(k,()=>trVal(k,lang));});
 _i18nAttr.forEach(o=>{o.el.setAttribute(o.attr,lang==='es'?o.orig:trVal(o.orig.trim(),lang));});
 document.querySelectorAll('#langSeg button').forEach(b=>b.classList.toggle('on',b.dataset.lang===lang));
 try{if(typeof renderGalChips==='function')renderGalChips();if(typeof renderShelfChips==='function')renderShelfChips();if($('trashModal')&&!$('trashModal').classList.contains('hide'))renderTrash();}catch(e){}}
// === tamaño de texto ajustable (reescala las tipografías del estudio) ===
const FS_GEN=[['body',14],['textarea,select,input[type=text],input[type=password]',14],['.btnrow button',11.5],['.resbar a,.resbar .acts button',12.5],['.lbprompt',12.5]];
const FS_SMALL=[['label',10],['.hint',11],['.eyebrow',10],['.chip',11],['.pgroup',9],['.preslegend',10]];
function applyFs(){
 const g=+($('fsGen').value||14),s=+($('fsSmall').value||11),sg=g/14,ss=s/11;
 let css='';
 FS_GEN.forEach(x=>css+=x[0]+'{font-size:'+(Math.round(x[1]*sg*10)/10)+'px}');
 FS_SMALL.forEach(x=>css+=x[0]+'{font-size:'+(Math.round(x[1]*ss*10)/10)+'px}');
 let el=$('fsCustom');if(!el){el=document.createElement('style');el.id='fsCustom';document.head.appendChild(el);}
 el.textContent=css;
 $('fsGenV').textContent=g+'px';$('fsSmallV').textContent=s+'px';
 localStorage.setItem('studio_fs_g',g);localStorage.setItem('studio_fs_s',s);}
$('fsGen').oninput=applyFs;$('fsSmall').oninput=applyFs;
$('fsReset').onclick=()=>{$('fsGen').value=14;$('fsSmall').value=11;applyFs();toast('Tamaño de texto restablecido');};
$('fsGen').value=localStorage.getItem('studio_fs_g')||14;$('fsSmall').value=localStorage.getItem('studio_fs_s')||11;applyFs();
async function refreshSetFolders(){try{const c=await(await fetch('/config')).json();
 $('setGenPath').textContent=c.effective||'(por defecto)';
 $('setShelfPath').textContent=c.shelf_effective||'(por defecto)';
 const l=$('setFolderProj');if(l)l.textContent=c.project_label||'General';
}catch(e){}}
$('setBtn').onclick=()=>{$('setModal').classList.remove('hide');refreshSetFolders();};
$('tutBtn').onclick=()=>{$('tutModal').classList.remove('hide')};
$('tutModal').onclick=e=>{if(e.target===$('tutModal'))$('tutModal').classList.add('hide')};
$('setGenPick').onclick=async()=>{toast('Abriendo selector de carpeta…');
 try{const r=await(await fetch('/pickfolder')).json();
  if(r.path){const c=await(await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({save_dir:r.path,project:curProj(),sub:activeSub})})).json();
   if(c.error){toast(c.error,'bad');return;}
   cfgEffective=c.effective||cfgEffective;if(!$('saveDesk').checked){$('saveDesk').checked=true;localStorage.setItem('studio_desk','1');}renderSaveWhere();$('setGenPath').textContent=c.effective;
   toast('Las imágenes generadas de este proyecto se copiarán en '+c.effective);}
  else if(r.error)toast(r.error,'bad');
 }catch(e){toast(String(e),'bad')}};
$('setShelfPick').onclick=async()=>{toast('Abriendo selector de carpeta…');
 try{const r=await(await fetch('/pickfolder')).json();
  if(r.path){await saveShelfDir(r.path);$('setShelfPath').textContent=$('shelfDirLbl').textContent;}
  else if(r.error)toast(r.error,'bad');
 }catch(e){toast(String(e),'bad')}};
$('themeWrap').onclick=e=>{const s=e.target.closest('.swatch');if(s)applyTheme(s.dataset.theme,true);};
$('langSeg').onclick=e=>{const b=e.target.closest('button');if(b)applyLang(b.dataset.lang);};
buildMinis();validate();checkKey();setProv(prov);markValidChips();
// config (etiqueta de General) → proyecto activo → historial + Mis imágenes de ese proyecto
(async()=>{await loadProjects();activeSub=localStorage.getItem('studio_sub')||'';
 const subs0=curSubs();if(!subs0.some(s=>s.key===activeSub))activeSub='';
 renderSubSel();galSubs=new Set(['all']);shelfSubs=new Set(['all']);
 await setActiveProject($('projSel').value,activeSub);await loadConfig();
 renderGalChips();renderShelfChips();await loadGal();await loadShelf();})();
(function(){let saved=localStorage.getItem('studio_theme');
 if(!localStorage.getItem('studio_theme_default_v2')){localStorage.setItem('studio_theme_default_v2','1');
  if(!saved||saved==='carbon'){localStorage.removeItem('studio_theme');saved=null;}}
 applyTheme(saved||'crema',false);})();
i18nSnapshot();applyLang(LANG);
</script></body></html>"""


ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", "localhost", "127.0.0.1"}
CSP = ("default-src 'self'; script-src 'unsafe-inline'; "
       "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data: blob:; media-src 'self' data: blob:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

# bandeja en memoria: las ventanas "Ver todo" dejan aquí imágenes para que el estudio las recoja
STAGE = []
STAGE_LOCK = threading.Lock()
# bandeja para prompts: la ventana "Biblioteca de prompts" envía un prompt compuesto al estudio
PROMPT_STAGE = []
PROMPT_STAGE_LOCK = threading.Lock()
# bandeja inversa: el historial del estudio envía prompts a la biblioteca para apilarlos
PROMPT_INBOX = []
PROMPT_INBOX_LOCK = threading.Lock()


def load_promptlib():
    d = load_json(PROMPTS_JSON, {}) or {}
    cats = d.get("categories") if isinstance(d.get("categories"), list) else []
    items = d.get("items") if isinstance(d.get("items"), list) else []
    # categorías pueden ser strings (formato viejo) u objetos {id,name,parent} (árbol) — se pasan tal cual
    return {"categories": list(cats), "items": [x for x in items if isinstance(x, dict)]}

GALERIA_CSS = """
*{box-sizing:border-box;margin:0}
svg{fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
body{background:#f4efe3;color:#2a2620;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:0 0 64px;-webkit-font-smoothing:antialiased}
header{position:sticky;top:0;z-index:5;display:flex;align-items:baseline;gap:14px;padding:18px 26px;
 background:rgba(244,239,227,.86);backdrop-filter:blur(12px);border-bottom:1px solid #e3dccb}
h1{font-size:18px;font-weight:600;letter-spacing:-.01em}
.count{font-size:13px;color:#8a8170}
.hint{margin-left:auto;font-size:12px;color:#8a8170}
.favtog{margin-left:14px;font-size:12.5px;color:#8a8170;text-decoration:none;border:1px solid #e3dccb;border-radius:9px;padding:6px 12px;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.favtog:hover{border-color:#cfc4ac;color:#2a2620}
.favtog.on{color:#e6b35c;border-color:#e6b35c;background:rgba(230,179,92,.1)}
.favtog svg{width:14px;height:14px}
.gfolder{font-size:12px;color:#8a8170;display:inline-flex;align-items:center;gap:6px;border:1px solid #e3dccb;border-radius:9px;padding:5px 10px;white-space:nowrap;align-self:center}
.gfolder b{font-weight:600;color:#2a2620;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;max-width:300px;overflow:hidden;text-overflow:ellipsis}
.gfolder svg{width:14px;height:14px;flex:none}
.gfbtn{font:inherit;font-size:11.5px;color:#1f6b54;background:none;border:0;cursor:pointer;text-decoration:underline;padding:0 0 0 3px}
.gfbtn:hover{opacity:.7}
.gmvov{position:fixed;inset:0;z-index:90;display:none;align-items:center;justify-content:center;padding:20px;background:rgba(10,10,12,.5);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}
.gmvcard{position:relative;width:min(420px,94vw);max-height:82vh;overflow:auto;background:#faf6ec;border:1px solid #e3dccb;border-radius:20px;box-shadow:0 28px 70px rgba(0,0,0,.38);padding:24px;animation:gmvin .2s cubic-bezier(.2,.85,.3,1)}
@keyframes gmvin{from{opacity:0;transform:translateY(14px) scale(.97)}to{opacity:1;transform:none}}
.gmvclose{position:absolute;top:13px;right:13px;width:32px;height:32px;border:0;background:none;color:#8a8170;border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center}
.gmvclose:hover{background:rgba(0,0,0,.06);color:#2a2620}
.gmvclose svg{width:17px;height:17px}
.gmvtitle{font-size:17px;font-weight:650;color:#2a2620;margin-bottom:16px}
.gmvtabs{display:flex;gap:4px;padding:3px;margin-bottom:16px;background:rgba(0,0,0,.05);border-radius:11px}
.gmvtabs button{flex:1;text-align:center;padding:9px 8px;border-radius:8px;font:inherit;font-size:13.5px;font-weight:500;color:#8a8170;border:0;background:none;cursor:pointer;transition:.14s}
.gmvtabs button:hover{color:#2a2620}
.gmvtabs button.on{background:#1f6b54;color:#fff}
.gmvh{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:#8a8170;margin-bottom:9px}
.gmvlist{display:flex;flex-direction:column;gap:4px}
.gmvp{display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:none;border:1px solid transparent;padding:12px 14px;border-radius:11px;font:inherit;font-size:14px;color:#2a2620;cursor:pointer;transition:.14s}
.gmvp:hover{background:rgba(31,107,84,.1);color:#1f6b54;border-color:rgba(31,107,84,.22)}
.gmvp svg{width:18px;height:18px;flex:none;opacity:.8}
.tile.gsel{outline:3px solid #1f6b54;outline-offset:-3px}
.tile.gsel::after{content:"✓";position:absolute;top:8px;left:8px;width:24px;height:24px;display:flex;align-items:center;justify-content:center;background:#1f6b54;color:#fff;border-radius:50%;font-size:14px;font-weight:700;z-index:3}
body.selmode{user-select:none;-webkit-user-select:none}
body.selmode .tile{cursor:pointer}
body.selmode .tile>img{-webkit-user-drag:none;user-drag:none;pointer-events:none}
body.selmode .acts{display:none!important}
.gmarq{position:fixed;border:1.5px solid #1f6b54;background:rgba(31,107,84,.14);z-index:50;pointer-events:none;border-radius:4px}
body.gdrop::after{content:"Suelta para añadir a Mis imágenes";position:fixed;inset:14px;z-index:70;display:flex;align-items:center;justify-content:center;background:rgba(31,107,84,.1);border:3px dashed #1f6b54;border-radius:18px;font-size:19px;font-weight:650;color:#1f6b54;pointer-events:none;backdrop-filter:blur(2px)}
.gselbar{position:fixed;left:50%;bottom:24px;z-index:40;display:none;align-items:center;gap:6px;
 background:rgba(250,246,236,.9);backdrop-filter:blur(16px) saturate(1.3);-webkit-backdrop-filter:blur(16px) saturate(1.3);
 border:1px solid #e3dccb;border-radius:18px;box-shadow:0 18px 44px rgba(0,0,0,.22),0 2px 8px rgba(0,0,0,.07);
 padding:8px 10px;opacity:0;transform:translateX(-50%) translateY(24px) scale(.97)}
.gselbar.show{display:flex;animation:gselin .24s cubic-bezier(.2,.85,.3,1) forwards}
@keyframes gselin{to{opacity:1;transform:translateX(-50%) translateY(0) scale(1)}}
.gselcount{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:#6b665a;font-weight:500;padding:0 8px 0 6px}
.gselcount b{display:inline-flex;align-items:center;justify-content:center;min-width:24px;height:24px;padding:0 7px;background:#1f6b54;color:#fff;border-radius:12px;font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}
.gseldiv{width:1px;height:26px;background:#e3dccb;margin:0 2px}
.gselact{display:inline-flex;align-items:center;gap:7px;font:inherit;font-size:13px;font-weight:500;border:0;background:none;color:#2a2620;border-radius:11px;padding:8px 13px;cursor:pointer;transition:background .14s,color .14s}
.gselact:hover{background:rgba(31,107,84,.12);color:#1f6b54}
.gselact svg{width:16px;height:16px;stroke-width:1.9}
.gselbar .gseldel{color:#b4452f}
.gselbar .gseldel:hover{background:rgba(180,69,47,.1);color:#b4452f}
.gselx{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border:0;background:none;color:#8a8170;border-radius:50%;cursor:pointer;transition:.14s}
.gselx:hover{background:rgba(0,0,0,.06);color:#2a2620}
.gselx svg{width:16px;height:16px}
.gchips{display:flex;flex-wrap:wrap;gap:7px;padding:16px 26px 0}
.gchip{font-size:12.5px;color:#8a8170;text-decoration:none;border:1px solid #e3dccb;border-radius:999px;padding:6px 13px;background:#fffdf6;white-space:nowrap;transition:.14s}
.gchip:hover{border-color:#cfc4ac;color:#2a2620}
.gchip.on{color:#fff;background:#1f6b54;border-color:#1f6b54}
.groups{padding:8px 0 0}
.ggroup{padding:0}
.ggroup .grid{padding:8px 26px 22px}
.ggrouphdr{position:sticky;top:62px;z-index:3;display:flex;align-items:center;gap:10px;margin:18px 26px 0;padding:9px 14px;
 background:rgba(255,253,246,.92);backdrop-filter:blur(8px);border:1px solid #e3dccb;border-radius:12px}
.ggroupname{font-size:13.5px;font-weight:650;color:#2a2620;letter-spacing:-.01em}
.ggroupcount{font-size:11.5px;color:#8a8170;background:rgba(0,0,0,.05);border-radius:999px;padding:2px 9px;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;padding:22px 26px}
.tile{position:relative;border-radius:14px;overflow:hidden;border:1px solid #e3dccb;background:#fffdf6;
 box-shadow:0 1px 2px rgba(0,0,0,.05);transition:transform .18s,box-shadow .18s,border-color .18s}
.tile.gdragging{opacity:.35}
.tile:hover{transform:translateY(-3px);box-shadow:0 10px 26px rgba(0,0,0,.13);border-color:#cfc4ac}
.tile>img{width:100%;aspect-ratio:1;object-fit:cover;display:block}
.acts{position:absolute;top:8px;right:8px;display:flex;gap:5px;flex-wrap:wrap;max-width:108px;justify-content:flex-end;opacity:0;transition:opacity .15s}
.tile:hover .acts{opacity:1}
.cdots,.cpick{position:absolute;left:50%;transform:translateX(-50%);bottom:10px;display:flex;gap:5px;z-index:3}
.cdots{pointer-events:none;transition:opacity .15s}
.cdot{width:11px;height:11px;border-radius:50%;box-shadow:0 0 0 1.6px rgba(0,0,0,.4)}
.cpick{z-index:5;opacity:0;pointer-events:none;transition:opacity .15s;padding:5px 7px;background:rgba(12,12,14,.5);backdrop-filter:blur(6px);border-radius:999px}
.tile:hover .cpick{opacity:1;pointer-events:auto}
.tile:hover .cdots{opacity:0}
.cpdot{width:16px;height:16px;border-radius:50%;border:2px solid rgba(255,255,255,.35);cursor:pointer;padding:0}
.cpdot.on,.cpdot:hover{border-color:#fff}
.cdot.r,.cpdot.r,.cfdot.r,.cflash.r{background:#e5484d}.cdot.y,.cpdot.y,.cfdot.y,.cflash.y{background:#f5b400}.cdot.g,.cpdot.g,.cfdot.g,.cflash.g{background:#46a758}.cdot.b,.cpdot.b,.cfdot.b,.cflash.b{background:#3b82f6}
.cflash{position:absolute;border-radius:50%;z-index:4;pointer-events:none;opacity:.5}
.cflash.cin{animation:cflashin .5s cubic-bezier(.33,0,.2,1) forwards}
.cflash.cout{animation:cflashout .42s cubic-bezier(.5,0,.5,1) forwards}
@keyframes cflashin{0%{transform:scale(0);opacity:.6}65%{opacity:.5}100%{transform:scale(1);opacity:0}}
@keyframes cflashout{0%{transform:scale(1);opacity:.5}100%{transform:scale(0);opacity:.5}}
body.selmode .cpick{display:none}
.cfilt{margin-left:10px;display:inline-flex;gap:5px;align-items:center}
.cfdot{width:15px;height:15px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0;opacity:.5}
.cfdot:hover{opacity:.85}.cfdot.on{opacity:1;border-color:#2a2620}
body.selmode .cdots{display:none}
.gb{width:30px;height:30px;border-radius:8px;background:rgba(12,12,14,.86);backdrop-filter:blur(6px);border:1px solid rgba(255,255,255,.18);
 color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;text-decoration:none}
.gb:hover{background:rgba(12,12,14,.96);border-color:rgba(255,255,255,.45)}
.gb svg{width:15px;height:15px;stroke:#fff;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.gb.star.on svg{stroke:#e6b35c;fill:#e6b35c}
.cap{position:absolute;inset:auto 0 0 0;padding:22px 11px 9px;font-size:11px;line-height:1.32;color:#fff;
 background:linear-gradient(to top,rgba(0,0,0,.7),transparent);opacity:0;transition:opacity .18s;pointer-events:none;
 display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.tile:hover .cap{opacity:1}
.empty{padding:64px 26px;color:#8a8170;font-size:14px}
.glb{position:fixed;inset:0;background:rgba(5,5,6,.94);backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;z-index:80;padding:30px 30px 100px;cursor:zoom-out}
.glb.show{display:flex}
.glb>img{max-width:94vw;max-height:84vh;border-radius:10px;box-shadow:0 30px 90px rgba(0,0,0,.7)}
#glbImg:fullscreen{width:100vw;height:100vh;max-width:100vw;max-height:100vh;object-fit:contain;border-radius:0;box-shadow:none;background:#000}
#glbImg:-webkit-full-screen{width:100vw;height:100vh;max-width:100vw;max-height:100vh;object-fit:contain;border-radius:0;box-shadow:none;background:#000}
.glbx{position:fixed;top:18px;right:18px;width:38px;height:38px;border-radius:10px;border:1px solid rgba(255,255,255,.2);
 background:rgba(16,16,18,.85);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer}
.glbx svg{width:18px;height:18px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round}
.glbbar{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);display:flex;flex-direction:column;gap:10px;cursor:default;
 background:rgba(16,16,18,.92);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,.14);border-radius:12px;padding:12px 14px;max-width:min(760px,92vw)}
.glbp{font-size:12.5px;line-height:1.5;color:rgba(255,255,255,.85);white-space:pre-wrap;word-break:break-word;max-height:24vh;overflow-y:auto;user-select:text;-webkit-user-select:text;cursor:text}
.glbres{font-family:ui-monospace,monospace;font-size:11px;color:rgba(255,255,255,.82);background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.16);border-radius:6px;padding:2px 8px;align-self:flex-start}
.glbres:empty{display:none}
.glbp:empty{display:none}
.glbbtns{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.gbtn{display:flex;align-items:center;gap:6px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.18);color:#fff;
 border-radius:8px;padding:8px 12px;font-size:12.5px;cursor:pointer;text-decoration:none}
.gbtn:hover{background:rgba(255,255,255,.16)}
.gbtn svg{width:14px;height:14px;stroke:#fff;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.gtoast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(10px);background:#2a2620;color:#fff;
 padding:10px 18px;border-radius:11px;font-size:13px;opacity:0;transition:opacity .2s,transform .2s;pointer-events:none;z-index:95;box-shadow:0 8px 30px rgba(0,0,0,.25)}
.gtoast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media(prefers-color-scheme:dark){
 body{background:#14110c;color:#ece6d8}
 header{background:rgba(20,17,12,.86);border-color:#2a2418}
 .count,.empty,.hint{color:#9a8f78}
 .tile{background:#1c1812;border-color:#2a2418}
 .tile:hover{border-color:#3a3322}
 .gtoast{background:#ece6d8;color:#1c1812}
 .gchip{color:#9a8f78;border-color:#2a2418;background:#1c1812}
 .gchip:hover{border-color:#3a3322;color:#ece6d8}
 .gchip.on{color:#1c1812;background:#e0a571;border-color:#e0a571}
 .ggrouphdr{background:rgba(28,24,18,.92);border-color:#2a2418}
 .ggroupname{color:#ece6d8}
 .ggroupcount{color:#9a8f78;background:rgba(255,255,255,.06)}
 .gfolder{color:#9a8f78;border-color:#2a2418}
 .gfolder b{color:#ece6d8}
 .gfbtn{color:#e0a571}
 .gmvcard{background:#1c1812;border-color:#3a3322;box-shadow:0 28px 70px rgba(0,0,0,.62)}
 .gmvclose{color:#9a8f78}
 .gmvclose:hover{background:rgba(255,255,255,.08);color:#ece6d8}
 .gmvtitle{color:#ece6d8}
 .gmvtabs{background:rgba(255,255,255,.06)}
 .gmvtabs button{color:#9a8f78}
 .gmvtabs button:hover{color:#ece6d8}
 .gmvtabs button.on{background:#e0a571;color:#1c1812}
 .gmvh{color:#9a8f78}
 .gmvp{color:#ece6d8}
 .gmvp:hover{background:rgba(224,165,113,.16);color:#e0a571;border-color:rgba(224,165,113,.3)}
 .tile.gsel{outline-color:#e0a571}
 .tile.gsel::after{background:#e0a571;color:#1c1812}
 .gmarq{border-color:#e0a571;background:rgba(224,165,113,.16)}
 body.gdrop::after{background:rgba(224,165,113,.12);border-color:#e0a571;color:#e0a571}
 .gselbar{background:rgba(28,24,18,.9);border-color:#3a3322;box-shadow:0 18px 44px rgba(0,0,0,.55)}
 .gselcount{color:#9a8f78}
 .gselcount b{background:#e0a571;color:#1c1812}
 .gseldiv{background:#3a3322}
 .gselact{color:#ece6d8}
 .gselact:hover{background:rgba(224,165,113,.16);color:#e0a571}
 .gselbar .gseldel{color:#e07a6b}
 .gselbar .gseldel:hover{background:rgba(224,122,107,.16);color:#e07a6b}
 .gselx{color:#9a8f78}
 .gselx:hover{background:rgba(255,255,255,.08);color:#ece6d8}
}
"""

def gallery_html(src, fav=False, proj="", sub="", subs_filter=""):
    import html as _h, json as _json
    from urllib.parse import quote as _q
    is_shelf = (src == "shelf")
    folder = str((shelf_dir_sub(proj, sub) if is_shelf else save_dir_sub(proj, sub))).replace(str(HOME), "~")
    _glabel = load_json(CONF_JSON, {}).get("general_label") or "General"
    # destinos de "mover": cada proyecto (raíz) + cada subproyecto, excepto el grupo de origen
    move_targets = []
    for p in [{"name": "", "label": _glabel}] + [{"name": k, "label": k} for k in load_projects().keys() if k]:
        if not (proj_key(p["name"]) == proj_key(proj) and not sub):
            move_targets.append({"name": p["name"], "sub": "", "label": p["label"]})
        for s in list_subs(p["name"]):
            if proj_key(p["name"]) == proj_key(proj) and _sub_safe(s["key"]) == _sub_safe(sub):
                continue
            move_targets.append({"name": p["name"], "sub": s["key"],
                                 "label": p["label"] + " › " + s["label"]})
    _all_subs = list_subs(proj)
    _subkeys = [s["key"] for s in _all_subs]
    _sub_label_of = {s["key"]: s["label"] for s in _all_subs}
    _sublabel = ""
    if sub:
        _sublabel = _sub_label_of.get(_sub_safe(sub), sub)
    # --- resolver qué grupos (raíz / subproyectos) se muestran ---
    # subs_filter: "all" = raíz + todos; "k1,k2" = los listados (cadena vacía = raíz);
    #              "" + sub = solo ese sub; "" sin sub = solo raíz (igual que hoy)
    sf = (subs_filter or "").strip()
    if sf == "all":
        group_keys = [""] + _subkeys
    elif sf:
        group_keys = []
        for k in sf.split(","):
            kk = _sub_safe(k)
            if kk == "" and "" not in group_keys:
                group_keys.append("")
            elif kk in _subkeys and kk not in group_keys:
                group_keys.append(kk)
        if not group_keys:
            group_keys = [_sub_safe(sub)]
    else:
        group_keys = [_sub_safe(sub)]
    multi = len(group_keys) > 1
    plabel = (" · " + proj) if (proj and not is_general(proj)) else ""
    if not multi and sub:
        plabel += " › " + _sublabel
    if is_shelf:
        title, base = "Mis imágenes" + plabel, "/shelffile?name="
    else:
        title, base = "Historial" + plabel, "/file?name="
        if fav:
            title = "Historial · favoritas" + plabel
    if multi:
        title = ("Mis imágenes" if is_shelf else ("Historial · favoritas" if fav else "Historial")) \
            + ((" · " + proj) if (proj and not is_general(proj)) else "")
    GPL = '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>'
    GCP = '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
    GST = '<svg viewBox="0 0 24 24"><path d="M12 3l2.4 5.9 6.1.4-4.7 4 1.5 6-5.3-3.3L6.7 19.3l1.5-6-4.7-4 6.1-.4z"/></svg>'
    GDL = '<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>'
    GOP = '<svg viewBox="0 0 24 24"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/></svg>'
    GLB = '<svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>'
    GMV = '<svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><path d="M12 11v6M9.5 13.5 12 11l2.5 2.5"/></svg>'
    def _tile_html(it, gk):
        f = it.get("file", "")
        if not f:
            return ""
        gpq = (("&project=" + _q(proj)) if proj else "") + (("&sub=" + _q(gk)) if gk else "")
        u = base + _q(f) + gpq
        fa = _h.escape(f)
        prompt = "" if is_shelf else str(it.get("prompt", "") or "")
        pa = _h.escape(prompt)
        favon = (not is_shelf) and bool(it.get("fav"))
        btns = ['<button class="gb" data-act="ref" title="Usar como referencia (subir al prompt)">' + GPL + '</button>']
        if not is_shelf and prompt:
            btns.append('<button class="gb" data-act="prompt" title="Copiar prompt">' + GCP + '</button>')
            btns.append('<button class="gb" data-act="lib" title="Enviar prompt a la biblioteca">' + GLB + '</button>')
        if not is_shelf:
            btns.append('<button class="gb star' + (' on' if favon else '') + '" data-act="fav" title="Favorita">' + GST + '</button>')
        btns.append('<a class="gb" href="' + u + '" download="' + fa + '" title="Descargar">' + GDL + '</a>')
        if move_targets:
            btns.append('<button class="gb" data-act="move" title="Mover a otro proyecto">' + GMV + '</button>')
        btns.append('<button class="gb" data-act="open" title="Abrir en grande (flotante)">' + GOP + '</button>')
        cols = [c for c in (it.get("colors") or []) if c in ("r", "y", "g", "b")]
        cpick = ('<div class="cpick">' + "".join(
            '<button class="cpdot ' + c + (' on' if c in cols else '') + '" data-act="col" data-col="' + c + '" title="Etiqueta de color"></button>'
            for c in ("r", "y", "g", "b")) + '</div>')
        cdots = ('<div class="cdots">' + "".join('<span class="cdot ' + c + '"></span>' for c in cols) + '</div>') if cols else ''
        capt = pa if (not is_shelf) else _h.escape(str(it.get("name", "") or ""))
        cap = ('<span class="cap">' + capt + '</span>') if capt else ""
        return ('<figure class="tile" draggable="true" data-file="' + fa + '" data-sub="' + _h.escape(gk) + '" data-fav="'
                + ('1' if favon else '0') + '" data-colors="' + ",".join(cols) + '" data-prompt="' + pa + '">'
                '<img src="' + u + '&thumb=1" loading="lazy" alt="">'
                + cdots + cpick +
                '<div class="acts">' + "".join(btns) + '</div>' + cap + '</figure>')

    # cargar cada grupo (raíz + subproyectos seleccionados) y construir su grilla
    groups = []          # [(key, label, [tile_html...])]
    total_tiles = 0
    for gk in group_keys:
        if is_shelf:
            g_items = load_json(pshelf_json(proj, gk), [])
        else:
            g_items = load_json(phist_json(proj, gk), [])
            if fav:
                g_items = [it for it in g_items if it.get("fav")]
        g_tiles = [t for t in (_tile_html(it, gk) for it in g_items) if t]
        total_tiles += len(g_tiles)
        glabel = ("Raíz del proyecto" if (proj and not is_general(proj)) else "General") if gk == "" else _sub_label_of.get(gk, gk)
        groups.append((gk, glabel, g_tiles))
    if multi:
        # Historial/Mis imágenes separado por recuadro + encabezado del subproyecto
        secs = []
        for gk, glabel, g_tiles in groups:
            inner = "".join(g_tiles) if g_tiles else '<div class="empty">Sin imágenes en este grupo.</div>'
            secs.append('<section class="ggroup">'
                        '<div class="ggrouphdr"><span class="ggroupname">' + _h.escape(glabel) + '</span>'
                        '<span class="ggroupcount">' + str(len(g_tiles)) + '</span></div>'
                        '<div class="grid">' + inner + '</div></section>')
        grid = "".join(secs)
    else:
        only = groups[0][2] if groups else []
        grid = "".join(only) if only else '<div class="empty">Aún no hay imágenes.</div>'
    # chips para elegir qué subproyectos ver (solo si el proyecto tiene subproyectos)
    chips_html = ""
    if _all_subs:
        sel_root = ("" in group_keys)
        sel_all = (sf == "all")
        def _chip(key, lbl, on):
            url = "/galeria?" + (("fav=1&") if (fav and not is_shelf) else "") + (("src=shelf&") if is_shelf else "")
            url += ("project=" + _q(proj)) if proj else ""
            url += "&subs=" + _q(key)
            return ('<a class="gchip' + (' on' if on else '') + '" href="' + url + '">' + _h.escape(lbl) + '</a>')
        cps = [_chip("", "Raíz", sel_root and not sel_all)]
        for s in _all_subs:
            cps.append(_chip(s["key"], s["label"], (s["key"] in group_keys) and not sel_all))
        cps.append(_chip("all", "Todos", sel_all))
        chips_html = '<div class="gchips">' + "".join(cps) + '</div>'
    _subq = ("&sub=" + _q(sub)) if sub else ""
    pqg = ("?project=" + _q(proj) + _subq) if (proj or sub) else ("?sub=" + _q(sub)) if sub else ""
    if is_shelf:
        favlink = ""
    elif fav:
        favlink = '<a class="favtog on" href="/galeria' + pqg + '" title="Ver todas las imágenes">' + GST + 'Todas</a>'
    else:
        favlink = '<a class="favtog" href="/galeria?fav=1' + (("&project=" + _q(proj)) if proj else "") + _subq + '" title="Ver solo las favoritas">' + GST + 'Solo favoritas</a>'
    js = ("const SRC=" + _json.dumps("shelf" if is_shelf else "history") + ";"
          "const PROJ=" + _json.dumps(proj or "") + ";"
          "const SUB=" + _json.dumps(sub or "") + ";"
          "const MOVES=" + _json.dumps(move_targets) + ";"
          "var BASE=(SRC==='shelf')?'/shelffile?name=':'/file?name=';"
          # PQ depende del subproyecto del tile (puede haber varios grupos en pantalla)
          "function pqOf(s){return (PROJ?('&project='+encodeURIComponent(PROJ)):'')+(s?('&sub='+encodeURIComponent(s)):'');}"
          "function subOf(file){var t=null;try{t=document.querySelector('.tile[data-file=\"'+(window.CSS&&CSS.escape?CSS.escape(file):file)+'\"]');}catch(_){}return (t&&t.dataset.sub)||SUB||'';}"
          "const tEl=document.getElementById('gtoast');"
          "function gt(m){tEl.textContent=m;tEl.classList.add('show');clearTimeout(tEl._t);tEl._t=setTimeout(function(){tEl.classList.remove('show')},1800);}"
          "async function stageRef(file){try{var r=await fetch('/stage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({src:SRC,file:file,project:PROJ,sub:subOf(file)})});var j=await r.json();gt(j&&j.ok?'Enviada como referencia al estudio ✓':(j&&j.error?j.error:'No se pudo enviar'));}catch(x){gt('No se pudo enviar');}}"
          "async function copyP(p){try{await navigator.clipboard.writeText(p||'');gt('Prompt copiado');}catch(x){gt('No se pudo copiar');}}"
          "async function stageP(p){if(!(p||'').trim()){gt('Esta imagen no tiene prompt');return;}try{var r=await fetch('/promptinbox',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p})});var j=await r.json();gt(j&&j.ok?'Prompt enviado a la biblioteca ✓':(j&&j.error?j.error:'No se pudo enviar'));}catch(x){gt('No se pudo enviar');}}"
          "var glb=document.getElementById('glb'),glbImg=document.getElementById('glbImg'),glbP=document.getElementById('glbP'),glbDl=document.getElementById('glbDl'),glbCopy=document.getElementById('glbCopy');"
          "var curFile='',curPrompt='',curSub='';"
          "function openLb(file,prompt,s){curFile=file;curPrompt=prompt||'';curSub=(s!==undefined&&s!==null)?s:subOf(file);var u=BASE+encodeURIComponent(file)+pqOf(curSub);"
          "var glbRes=document.getElementById('glbRes');if(glbRes){glbRes.textContent='';glbImg.onload=function(){var w=glbImg.naturalWidth,h=glbImg.naturalHeight;glbRes.textContent=(w&&h)?(w+'\\u00d7'+h+' px'):'';};}"
          "glbImg.src=u;glbP.textContent=curPrompt;glbDl.href=u;glbDl.setAttribute('download',file);glbCopy.style.display=curPrompt?'':'none';glb.classList.add('show');}"
          "function closeLb(){glb.classList.remove('show');glbImg.src='';}"
          "var GRIDS=[].slice.call(document.querySelectorAll('.grid'));"
          "function allTiles(){return [].slice.call(document.querySelectorAll('.tile'));}"
          "GRIDS.forEach(function(g){g.addEventListener('click',async function(e){"
          "if(selMode){if(window.__marqueed){window.__marqueed=false;return;}var st=e.target.closest('.tile');if(st){e.preventDefault();var ts=[].slice.call(g.querySelectorAll('.tile')),idx=ts.indexOf(st);"
          "if(e.shiftKey&&lastSelIdx!==null&&lastSelIdx<ts.length){var a=Math.min(lastSelIdx,idx),z=Math.max(lastSelIdx,idx);for(var i=a;i<=z;i++){var t=ts[i];if(t&&!selSet[t.dataset.file]){selSet[t.dataset.file]=t;t.classList.add('gsel');}}updSel();}"
          "else{toggleSel(st);lastSelIdx=idx;}}return;}"
          "var b=e.target.closest('[data-act]');"
          "if(b){e.preventDefault();var tile=b.closest('.tile'),file=tile.dataset.file,act=b.dataset.act;"
          "if(act==='ref'){stageRef(file);}"
          "else if(act==='open'){openLb(file,tile.dataset.prompt,tile.dataset.sub);}"
          "else if(act==='prompt'){copyP(tile.dataset.prompt);}"
          "else if(act==='move'){setMode('move');openMenu(b,[file],[tile]);}"
          "else if(act==='lib'){stageP(tile.dataset.prompt);}"
          "else if(act==='fav'){var on=tile.dataset.fav!=='1';tile.dataset.fav=on?'1':'0';b.classList.toggle('on',on);"
          "try{await fetch('/histfav',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:file,fav:on,project:PROJ,sub:tile.dataset.sub||''})});}catch(x){}"
          "gt(on?'Marcada como favorita':'Quitada de favoritas');}"
          "else if(act==='col'){var on2=!b.classList.contains('on');b.classList.toggle('on',on2);"
          "(function(){var tr=tile.getBoundingClientRect(),br=b.getBoundingClientRect();var cx=br.left+br.width/2-tr.left,cy=br.top+br.height/2-tr.top;var R=Math.hypot(Math.max(cx,tr.width-cx),Math.max(cy,tr.height-cy));var fc=document.createElement('div');fc.className='cflash '+b.dataset.col+' '+(on2?'cin':'cout');fc.style.left=cx+'px';fc.style.top=cy+'px';fc.style.width=fc.style.height=(R*2)+'px';fc.style.marginLeft=fc.style.marginTop=(-R)+'px';tile.appendChild(fc);var d=function(){fc.remove();};fc.addEventListener('animationend',d);setTimeout(d,800);})();"
          "var cols=[].slice.call(tile.querySelectorAll('.cpdot.on')).map(function(x){return x.dataset.col});tile.dataset.colors=cols.join(',');"
          "var cd=tile.querySelector('.cdots');if(cols.length){if(!cd){cd=document.createElement('div');cd.className='cdots';tile.insertBefore(cd,tile.children[1]||null);}cd.innerHTML=cols.map(function(c){return '<span class=\"cdot '+c+'\"></span>'}).join('');}else if(cd){cd.remove();}"
          "try{await fetch('/imgcolors',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:file,colors:cols,scope:(SRC==='shelf'?'shelf':'hist'),project:PROJ,sub:tile.dataset.sub||''})});}catch(x){}"
          "setTimeout(applyColFilter,420);gt(cols.length?'Etiqueta de color actualizada':'Etiquetas quitadas');}return;}"
          "if(e.target.closest('a'))return;"
          "var tile=e.target.closest('.tile');if(tile)openLb(tile.dataset.file,tile.dataset.prompt,tile.dataset.sub);"
          "});});"
          "glb.addEventListener('click',function(e){var s=window.getSelection&&window.getSelection();if(s&&String(s).length)return;if(e.target===glb||e.target.closest('#glbClose'))closeLb();});"
          "document.addEventListener('keydown',function(e){if(e.key==='Escape'){if(document.fullscreenElement||document.webkitFullscreenElement){return;}if(mv&&mv.style.display!=='none'){closeMv();}else if(selMode){exitSel();}else{closeLb();}}});"
          "document.getElementById('glbRef').onclick=function(){stageRef(curFile);};"
          "glbCopy.onclick=function(){copyP(curPrompt);};"
          "document.getElementById('glbFull').onclick=function(){var d=document,el=glbImg;if(d.fullscreenElement||d.webkitFullscreenElement){(d.exitFullscreen||d.webkitExitFullscreen||function(){}).call(d);return;}var fn=el.requestFullscreen||el.webkitRequestFullscreen;if(fn)fn.call(el);};"
          "var gfdir=document.getElementById('gfdir'),gfpick=document.getElementById('gfpick');"
          "if(gfpick)gfpick.onclick=async function(){gt('Abriendo selector de carpeta…');"
          "try{var r=await(await fetch('/pickfolder')).json();"
          "if(r.path){var fld=(SRC==='shelf')?'shelf_dir':'save_dir';var body={project:PROJ,sub:SUB};body[fld]=r.path;"
          "var c=await(await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();"
          "if(c.error){gt(c.error);return;}var eff=(SRC==='shelf')?c.shelf_effective:c.effective;"
          "if(gfdir)gfdir.textContent=eff;gt('Carpeta cambiada ✓');}"
          "else if(r.error)gt(r.error);}catch(x){gt('No se pudo abrir el selector');}};"
          # --- mover / copiar imágenes a otro proyecto (individual o en lote) ---
          "var mvFiles=[],mvTiles=[],mvMode='move',mvDest='history';"
          "var mv=document.createElement('div');mv.className='gmvov';mv.style.display='none';"
          "mv.innerHTML='<div class=\"gmvcard\"><button class=\"gmvclose\" title=\"Cerrar (Esc)\"><svg viewBox=\"0 0 24 24\"><path d=\"M18 6 6 18M6 6l12 12\"/></svg></button><div class=\"gmvtitle\">Enviar a proyecto</div><div class=\"gmvtabs\"><button data-m=\"move\" class=\"on\">Mover</button><button data-m=\"copy\">Copiar</button></div><div class=\"gmvtabs gmvdest\"><button data-d=\"history\" class=\"on\">a Historial</button><button data-d=\"shelf\">a Mis imágenes</button></div><div class=\"gmvh\">Elige el proyecto destino</div><div class=\"gmvlist\">'+MOVES.map(function(p,i){var nm=String(p.label).replace(/[<>&]/g,'');return '<button class=\"gmvp\" data-i=\"'+i+'\"><svg viewBox=\"0 0 24 24\"><path d=\"M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z\"/></svg><span>'+nm+'</span></button>';}).join('')+'</div></div>';"
          "document.body.appendChild(mv);"
          "function setMode(m){mvMode=m;mv.querySelectorAll('.gmvtabs:not(.gmvdest) button').forEach(function(x){x.classList.toggle('on',x.dataset.m===m);});}"
          "function setDest(d){mvDest=d;mv.querySelectorAll('.gmvdest button').forEach(function(x){x.classList.toggle('on',x.dataset.d===d);});}"
          "mv.querySelectorAll('.gmvtabs:not(.gmvdest) button').forEach(function(t){t.onclick=function(ev){ev.stopPropagation();setMode(t.dataset.m);};});"
          "mv.querySelectorAll('.gmvdest button').forEach(function(t){t.onclick=function(ev){ev.stopPropagation();setDest(t.dataset.d);};});"
          "function openMenu(anchor,files,tiles){mvFiles=files;mvTiles=tiles||[];setDest('history');mv.style.display='flex';}"
          "function closeMv(){mv.style.display='none';}"
          "mv.querySelector('.gmvclose').onclick=closeMv;"
          "mv.addEventListener('click',function(e){if(e.target===mv)closeMv();});"
          "function updCount(){var c=document.querySelector('.count');if(c)c.textContent=document.querySelectorAll('.tile').length+' imágenes';"
          "[].slice.call(document.querySelectorAll('.ggroup')).forEach(function(s){var n=s.querySelectorAll('.tile').length,cc=s.querySelector('.ggroupcount');if(cc)cc.textContent=n;});}"
          "mv.addEventListener('click',async function(e){var b=e.target.closest('button.gmvp');if(!b)return;var tgt=MOVES[+b.getAttribute('data-i')]||{name:'',sub:''};var dest=tgt.name,destSub=tgt.sub||'';var mode=mvMode,files=mvFiles.slice(),tiles=mvTiles.slice();closeMv();if(!files.length)return;"
          # agrupar por subproyecto de origen (en multi-grupo la selección puede mezclar subs)
          "var byS={};for(var i=0;i<files.length;i++){var s=(tiles[i]&&tiles[i].dataset?tiles[i].dataset.sub:subOf(files[i]))||'';(byS[s]=byS[s]||[]).push(files[i]);}"
          "var done=0,err='';"
          "var ug=[];for(var sk in byS){try{var r=await(await fetch('/moveitem',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({src:SRC,files:byS[sk],project:PROJ,sub:sk,dest:dest,dest_sub:destSub,dest_src:mvDest,mode:mode})})).json();if(r&&r.ok){done+=(r.done||byS[sk].length);if(r.pairs)ug.push({s:sk,pairs:r.pairs});}else if(r&&r.error){err=r.error;}}catch(x){err='No se pudo';}}"
          "if(done){if(mode==='move'){tiles.forEach(function(t){if(t&&t.remove)t.remove();});closeLb();updCount();}var verb=mode==='copy'?'copiada':'movida';var dst=(mvDest==='shelf'?'Mis imágenes':(b.textContent||'').trim());"
          "var _mode=mode,_dest=dest,_dsub=destSub,_dk=mvDest;gLastUndo=async function(){for(var i=0;i<ug.length;i++){var names=ug[i].pairs.map(function(p){return p.to});if(_mode==='copy'){await ghpost('/deleteitems',{src:_dk,project:_dest,sub:_dsub,files:names});}else{await ghpost('/moveitem',{src:_dk,files:names,project:_dest,sub:_dsub,dest:PROJ,dest_sub:ug[i].s,dest_src:SRC,mode:'move'});}}gt('Deshecho');setTimeout(function(){location.reload();},400);};"
          "gt((done>1?done+' '+verb+'s':'1 '+verb)+' a '+dst+' ✓ · ⌘Z para deshacer');if(selMode)exitSel();}else gt(err||'No se pudo');});"
          "var glbMove=document.getElementById('glbMove');"
          "if(glbMove)glbMove.onclick=function(){setMode('move');var t=null;try{t=document.querySelector('.tile[data-file=\"'+(window.CSS&&CSS.escape?CSS.escape(curFile):curFile)+'\"]');}catch(_){}openMenu(glbMove,[curFile],t?[t]:[]);};"
          # --- selección en lote ---
          "var selMode=false,selSet={},lastSelIdx=null;"
          # deshacer (1 nivel) en la ventana Ver todo: revierte la última acción y recarga
          "var gLastUndo=null;"
          "async function ghpost(u,b){return (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json();}"
          "document.addEventListener('keydown',function(e){if(!(e.metaKey||e.ctrlKey))return;var t=(document.activeElement||{}).tagName;if(t==='INPUT'||t==='TEXTAREA')return;if(mv&&mv.style.display!=='none')return;if(glb&&glb.classList.contains('show'))return;if((e.key||'').toLowerCase()!=='z'||e.shiftKey)return;e.preventDefault();if(!gLastUndo){gt('Nada que deshacer');return;}var op=gLastUndo;gLastUndo=null;op();});"
          # marquee: arrastrar un recuadro para seleccionar varias
          "var marq=null,mqStart=null,mqMoved=false;"
          "GRIDS.forEach(function(g){"
          "g.addEventListener('pointerdown',function(e){if(!selMode)return;if(e.button!==0)return;if(e.target.closest('a,button'))return;e.preventDefault();mqStart={x:e.clientX,y:e.clientY};mqMoved=false;});"
          "g.addEventListener('pointermove',function(e){if(!selMode||!mqStart)return;var dx=e.clientX-mqStart.x,dy=e.clientY-mqStart.y;if(!mqMoved&&Math.abs(dx)+Math.abs(dy)<6)return;mqMoved=true;"
          "if(!marq){marq=document.createElement('div');marq.className='gmarq';document.body.appendChild(marq);}"
          "var x1=Math.min(e.clientX,mqStart.x),y1=Math.min(e.clientY,mqStart.y),x2=Math.max(e.clientX,mqStart.x),y2=Math.max(e.clientY,mqStart.y);"
          "marq.style.cssText='position:fixed;left:'+x1+'px;top:'+y1+'px;width:'+(x2-x1)+'px;height:'+(y2-y1)+'px;display:block';"
          "allTiles().forEach(function(t){var r=t.getBoundingClientRect();var hit=!(r.right<x1||r.left>x2||r.bottom<y1||r.top>y2);if(hit&&!selSet[t.dataset.file]){selSet[t.dataset.file]=t;t.classList.add('gsel');}});updSel();});"
          "g.addEventListener('pointerup',endMarq);g.addEventListener('pointercancel',endMarq);"
          "});"
          "function endMarq(){if(!mqStart)return;if(marq){marq.remove();marq=null;}if(mqMoved)window.__marqueed=true;mqStart=null;mqMoved=false;}"
          "var rdTile=null,rdMoved=false;"
          "function gflip(container,mutate){var els=[].slice.call(container.querySelectorAll('.tile')),pos=new Map();els.forEach(function(el){pos.set(el,el.getBoundingClientRect());});mutate();els.forEach(function(el){if(el===rdTile)return;var a=pos.get(el);if(!a)return;var b=el.getBoundingClientRect(),dx=a.left-b.left,dy=a.top-b.top;if(dx||dy){el.style.transition='none';el.style.transform='translate('+dx+'px,'+dy+'px)';requestAnimationFrame(function(){el.style.transition='transform .22s cubic-bezier(.2,.7,.3,1)';el.style.transform='';});}});}"
          "function persistTileOrder(cont){var order=[].slice.call(cont.querySelectorAll('.tile')).map(function(t){return t.dataset.file;});var ft=cont.querySelector('.tile');var sub=(ft&&ft.dataset.sub)||'';fetch('/itemsorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({src:SRC,project:PROJ,sub:sub,order:order})}).then(function(){gt('Orden actualizado');});}"
          "GRIDS.forEach(function(g){"
          "g.addEventListener('dragstart',function(e){if(selMode)return;var t=e.target.closest('.tile');if(!t)return;rdTile=t;rdMoved=false;t.classList.add('gdragging');try{e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain','');}catch(_){}});"
          "g.addEventListener('dragover',function(e){if(!rdTile)return;var t=e.target.closest('.tile');if(!t||t===rdTile)return;if(t.parentElement!==rdTile.parentElement)return;e.preventDefault();var r=t.getBoundingClientRect(),after=e.clientX>(r.left+r.width/2),ref=after?t.nextElementSibling:t;if(ref!==rdTile&&rdTile.nextElementSibling!==ref){gflip(rdTile.parentElement,function(){rdTile.parentElement.insertBefore(rdTile,ref);});rdMoved=true;}});"
          "g.addEventListener('drop',function(e){if(!rdTile)return;e.preventDefault();e.stopPropagation();if(rdMoved)persistTileOrder(rdTile.parentElement);});"
          "});"
          "document.addEventListener('dragend',function(){if(rdTile)rdTile.classList.remove('gdragging');rdTile=null;rdMoved=false;});"
          "var selbtn=document.getElementById('gselbtn'),selbar=document.getElementById('gselbar'),seln=document.getElementById('gseln');"
          "function selFiles(){return Object.keys(selSet);}"
          "function selTiles(){return selFiles().map(function(f){return selSet[f];});}"
          "function updSel(){var n=selFiles().length;if(seln)seln.textContent=n;if(selbar)selbar.classList.toggle('show',selMode);}"
          "function toggleSel(tile){var f=tile.dataset.file;if(selSet[f]){delete selSet[f];tile.classList.remove('gsel');}else{selSet[f]=tile;tile.classList.add('gsel');}updSel();}"
          "function setTilesDrag(on){allTiles().forEach(function(t){t.draggable=on;});}"
          "function exitSel(){selMode=false;lastSelIdx=null;window.__marqueed=false;document.body.classList.remove('selmode');for(var k in selSet){if(selSet[k])selSet[k].classList.remove('gsel');}selSet={};updSel();setTilesDrag(true);if(selbtn)selbtn.classList.remove('on');var _ga=document.getElementById('gselall');if(_ga&&_ga.lastChild)_ga.lastChild.textContent='Todas';}"
          "if(selbtn)selbtn.onclick=function(){selMode=!selMode;document.body.classList.toggle('selmode',selMode);selbtn.classList.toggle('on',selMode);setTilesDrag(!selMode);if(!selMode)exitSel();else updSel();};"
          "var gselall=document.getElementById('gselall');"
          "if(gselall)gselall.onclick=function(){var ts=allTiles();var all=ts.length&&ts.every(function(t){return selSet[t.dataset.file]});if(all){ts.forEach(function(t){delete selSet[t.dataset.file];t.classList.remove('gsel')})}else{ts.forEach(function(t){if(!selSet[t.dataset.file]){selSet[t.dataset.file]=t;t.classList.add('gsel')}})}lastSelIdx=null;updSel();if(gselall.lastChild)gselall.lastChild.textContent=all?'Todas':'Ninguna';};"
          "var gcopyall=document.getElementById('gcopyall');"
          "if(gcopyall)gcopyall.onclick=async function(){if(!confirm('¿Copiar todas las imágenes del historial de este proyecto (incluye subproyectos) a Mis imágenes?'))return;"
          "try{var r=await(await fetch('/shelfcopyall',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:PROJ,allsubs:true})})).json();"
          "if(r&&r.ok){gt(r.added>0?(r.added+' copiada(s) a Mis imágenes ✓'):'Ya estaban todas en Mis imágenes');}else gt((r&&r.error)||'No se pudo copiar');}catch(x){gt('No se pudo copiar');}};"
          "var gselmove=document.getElementById('gselmove'),gselcopy=document.getElementById('gselcopy'),gselcancel=document.getElementById('gselcancel');"
          "if(gselmove)gselmove.onclick=function(){if(!selFiles().length){gt('Selecciona imágenes primero');return;}setMode('move');openMenu(gselmove,selFiles(),selTiles());};"
          "if(gselcopy)gselcopy.onclick=function(){if(!selFiles().length){gt('Selecciona imágenes primero');return;}setMode('copy');openMenu(gselcopy,selFiles(),selTiles());};"
          "if(gselcancel)gselcancel.onclick=exitSel;"
          "var gseldel=document.getElementById('gseldel');"
          "if(gseldel)gseldel.onclick=async function(){var files=selFiles(),tiles=selTiles();if(!files.length){gt('Selecciona imágenes primero');return;}"
          "if(!confirm('¿Eliminar '+files.length+(files.length===1?' imagen':' imágenes')+'? Se quita la copia interna (las copias en tu carpeta se conservan).'))return;"
          # agrupar por subproyecto de origen
          "var byS={};for(var i=0;i<files.length;i++){var s=(tiles[i]&&tiles[i].dataset?tiles[i].dataset.sub:'')||'';(byS[s]=byS[s]||[]).push(files[i]);}"
          "var done=0,err='',ug=[];for(var sk in byS){try{var r=await(await fetch('/deleteitems',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({src:SRC,files:byS[sk],project:PROJ,sub:sk})})).json();if(r&&r.ok){done+=(r.done||byS[sk].length);if(r.undo)ug.push({sub:sk,items:r.undo});}else if(r&&r.error){err=r.error;}}catch(x){err='No se pudo eliminar';}}"
          "if(done){tiles.forEach(function(t){if(t&&t.remove)t.remove();});updCount();exitSel();gLastUndo=async function(){for(var i=0;i<ug.length;i++){await ghpost('/restoreitems',{src:SRC,project:PROJ,sub:ug[i].sub,items:ug[i].items});}gt('Restaurado');setTimeout(function(){location.reload();},400);};gt(done+((done===1)?' eliminada ✓':' eliminadas ✓')+' · ⌘Z para deshacer');}else gt(err||'No se pudo eliminar');};"
          # --- filtro por color (OR): muestra las imágenes que tengan cualquiera de los colores activos ---
          "var colF={};"
          "function applyColFilter(){var act=Object.keys(colF).filter(function(c){return colF[c];});"
          "allTiles().forEach(function(t){var cs=(t.dataset.colors||'').split(',').filter(Boolean);var show=!act.length||act.some(function(c){return cs.indexOf(c)>=0;});t.style.display=show?'':'none';});"
          "[].slice.call(document.querySelectorAll('.ggroup')).forEach(function(s){var vis=[].slice.call(s.querySelectorAll('.tile')).filter(function(t){return t.style.display!=='none';}).length;var cc=s.querySelector('.ggroupcount');if(cc)cc.textContent=vis;});"
          "var c2=document.querySelector('.count');if(c2)c2.textContent=allTiles().filter(function(t){return t.style.display!=='none';}).length+' imágenes';}"
          "var gcf=document.getElementById('gColFilt');"
          "if(gcf)gcf.addEventListener('click',function(e){var b=e.target.closest('.cfdot');if(!b)return;var c=b.dataset.col;colF[c]=!colF[c];b.classList.toggle('on',!!colF[c]);applyColFilter();});"
          # --- soltar imágenes externas (Finder/escritorio/otra app) en Mis imágenes ---
          "if(SRC==='shelf'){"
          "window.addEventListener('dragover',function(e){if(e.dataTransfer&&[].slice.call(e.dataTransfer.types||[]).indexOf('Files')>=0){e.preventDefault();document.body.classList.add('gdrop');}});"
          "window.addEventListener('dragleave',function(e){if(!e.relatedTarget)document.body.classList.remove('gdrop');});"
          "window.addEventListener('drop',async function(e){document.body.classList.remove('gdrop');if(selMode)return;var fs=[].slice.call((e.dataTransfer&&e.dataTransfer.files)||[]).filter(function(f){return f.type&&f.type.indexOf('image/')===0;});if(!fs.length)return;e.preventDefault();"
          "gt('Subiendo '+fs.length+' imagen'+(fs.length>1?'es':'')+'…');var imgs=[];for(var i=0;i<fs.length;i++){imgs.push({name:fs[i].name,b64:await new Promise(function(res){var rd=new FileReader();rd.onload=function(){res(String(rd.result).split(',')[1]);};rd.readAsDataURL(fs[i]);})});}"
          "try{var rr=await(await fetch('/shelfadd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({images:imgs,project:PROJ,sub:SUB})})).json();if(rr&&rr.ok){gt(fs.length+' añadida'+(fs.length>1?'s':'')+' a Mis imágenes ✓');setTimeout(function(){location.reload();},650);}else gt((rr&&rr.error)||'No se pudo subir');}catch(x){gt('No se pudo subir');}});"
          "}")
    return ('<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + _h.escape(title) + ' · Gio Studio</title><style>' + GALERIA_CSS + '</style></head><body>'
            '<header><h1>' + _h.escape(title) + '</h1><span class="count">' + str(total_tiles) + ' imágenes</span>'
            '<span class="gfolder" title="Carpeta donde se guardan estas imágenes">'
            '<svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
            'Carpeta: <b id="gfdir">' + _h.escape(folder) + '</b>'
            '<button class="gfbtn" id="gfpick">cambiar</button></span>'
            '<span class="hint">Pasa el cursor sobre una imagen para sus acciones</span>' + favlink
            + ('<button class="favtog" id="gselbtn" title="Seleccionar varias para mover, copiar o eliminar">'
               '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="M8 12l2.8 2.8L16.5 9"/></svg>Seleccionar</button>')
            + (('<button class="favtog" id="gcopyall" title="Copiar todas las imágenes de este historial a Mis imágenes">'
                '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="13" height="13" rx="2"/><path d="M8 21h11a2 2 0 0 0 2-2V8"/><path d="M11.5 8.5v4M9.5 10.5h4"/></svg>A Mis imágenes</button>') if not is_shelf else '')
            + ('<span class="cfilt" id="gColFilt" title="Filtrar por color">'
               + "".join('<button class="cfdot ' + c + '" data-col="' + c + '" title="Filtrar por color"></button>' for c in ("r", "y", "g", "b"))
               + '</span>')
            + '</header>'
            + chips_html
            + (('<main class="groups">' + grid + '</main>') if multi else ('<main class="grid">' + grid + '</main>'))
            + ('<div class="gselbar" id="gselbar">'
               '<span class="gselcount"><b id="gseln">0</b>seleccionadas</span>'
               '<span class="gseldiv"></span>'
               '<button class="gselact" id="gselall"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="M8 12l2.8 2.8L16.5 9"/></svg>Todas</button>'
               '<span class="gseldiv"></span>'
               + (('<button class="gselact" id="gselmove">' + GMV + 'Mover</button>'
                   '<button class="gselact" id="gselcopy">' + GCP + 'Copiar</button>') if move_targets else '')
               + '<button class="gselact gseldel" id="gseldel"><svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M10 11v6M14 11v6"/></svg>Eliminar</button>'
               '<span class="gseldiv"></span>'
               '<button class="gselx" id="gselcancel" title="Salir de selección (Esc)"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>')
            + '<div class="glb" id="glb"><button class="glbx" id="glbClose" title="Cerrar (Esc)"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button>'
            '<img id="glbImg" alt="">'
            '<div class="glbbar"><span class="glbp" id="glbP"></span><span class="glbres" id="glbRes"></span><div class="glbbtns">'
            '<button class="gbtn" id="glbRef">' + GPL + 'Usar como referencia</button>'
            + ('<button class="gbtn" id="glbMove">' + GMV + 'Mover a proyecto</button>' if move_targets else '') +
            '<button class="gbtn" id="glbCopy">' + GCP + 'Copiar prompt</button>'
            '<button class="gbtn" id="glbFull"><svg viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg>Pantalla completa</button>'
            '<a class="gbtn" id="glbDl" download>' + GDL + 'Descargar</a>'
            '</div></div></div>'
            '<div class="gtoast" id="gtoast"></div>'
            '<script>' + js + '</script></body></html>')


PROMPTLIB_PAGE = r"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Biblioteca de prompts · Gio Studio</title>
<style>
*{box-sizing:border-box;margin:0}
:root{--bg:#f4efe3;--surf:#faf6ec;--surf2:#fffdf6;--line:rgba(0,0,0,.10);--line2:rgba(0,0,0,.18);
 --txt:#22201b;--mut:#6b665a;--faint:#9c9788;--accent:#1f6b54;--accent-dim:rgba(31,107,84,.12);
 --ok:#2f7a4a;--bad:#b4452f;--star:#e0a93a}
@media(prefers-color-scheme:dark){:root{--bg:#0f0d0c;--surf:#1a1715;--surf2:#231f1c;--line:rgba(255,255,255,.08);--line2:rgba(255,255,255,.16);
 --txt:#f1ece7;--mut:#a89f97;--faint:#6f665f;--accent:#e0a571;--accent-dim:rgba(224,165,113,.16);--ok:#7bbf8f;--bad:#e07a6b;--star:#e0a93a}}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh}
svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;vertical-align:middle}
header{display:flex;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);position:sticky;top:0;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px);z-index:5;flex-wrap:wrap}
header h1{font-size:17px;font-weight:650;letter-spacing:.01em}
.search{flex:1;min-width:180px;max-width:420px;background:var(--surf);border:1px solid var(--line);border-radius:10px;padding:9px 12px;color:var(--txt);font-size:13px;outline:none}
.search:focus{border-color:var(--accent)}
.hint{color:var(--faint);font-size:11.5px;margin-left:auto}
.hbtn{background:var(--surf);border:1px solid var(--line);color:var(--txt);border-radius:9px;padding:8px 12px;font-size:12.5px;font-family:inherit;cursor:pointer}
.hbtn:hover{border-color:var(--accent);color:var(--accent)}
.layout{display:flex;gap:0;align-items:stretch;min-height:calc(100vh - 60px)}
.side{width:300px;flex:none;border-right:1px solid var(--line);padding:16px 14px;display:flex;flex-direction:column;gap:6px}
.filters{display:flex;flex-direction:column;gap:4px;margin-bottom:8px}
.f{display:flex;align-items:center;gap:8px;text-align:left;background:transparent;border:0;color:var(--mut);font-size:13px;padding:8px 10px;border-radius:9px;cursor:pointer;font-family:inherit}
.f:hover{background:var(--surf);color:var(--txt)}
.f.on{background:var(--accent-dim);color:var(--accent);font-weight:600}
.cathdr{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint);margin:10px 0 4px;padding:0 4px}
.cats{display:flex;flex-direction:column;gap:2px;overflow-y:auto}
.cat{display:flex;align-items:center;gap:6px;background:transparent;border:0;color:var(--mut);font-size:13px;padding:7px 10px;border-radius:9px;cursor:pointer;font-family:inherit;text-align:left;width:100%}
.cat:hover{background:var(--surf);color:var(--txt)}
.cat.on{background:var(--accent-dim);color:var(--accent);font-weight:600}
.cat{position:relative}
.cat .tw{flex:none;width:14px;text-align:center;font-size:10px;color:var(--faint);cursor:pointer}
.cat .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cat .cn{font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums}
.cat .sub{opacity:0;border:0;background:transparent;cursor:pointer;color:var(--accent);padding:2px 4px;border-radius:5px;font-size:13px;line-height:1}
.cat:hover .sub{opacity:.7}.cat .sub:hover{opacity:1;background:var(--accent-dim)}
.cat[draggable=true]{cursor:grab}
.cat.dropbefore{box-shadow:inset 0 2px 0 var(--accent)}
.cat.dropafter{box-shadow:inset 0 -2px 0 var(--accent)}
.cat.dropinside{background:var(--accent-dim);outline:1px dashed var(--accent);outline-offset:-2px}
.cat .ed,.cat .del{opacity:0;border:0;background:transparent;cursor:pointer;padding:2px 4px;border-radius:5px;font-size:12px;line-height:1}
.cat .ed{color:var(--accent)}
.cat .del{color:var(--bad)}
.cat:hover .ed,.cat:hover .del{opacity:.75}
.cat .ed:hover{opacity:1;background:var(--accent-dim)}
.cat .del:hover{opacity:1;background:color-mix(in srgb,var(--bad) 16%,transparent)}
.cat.editing{padding:4px 6px}
.catedit{width:100%;background:var(--surf2);border:1px solid var(--accent);border-radius:8px;padding:6px 9px;color:var(--txt);font-size:13px;outline:none;font-family:inherit}
.addcat{display:flex;gap:6px;margin-top:10px}
.addcat input{flex:1;background:var(--surf);border:1px solid var(--line);border-radius:8px;padding:7px 9px;color:var(--txt);font-size:12.5px;outline:none}
.addcat input:focus{border-color:var(--accent)}
.addcat button{flex:none;width:34px;border:1px solid var(--line);background:var(--surf);color:var(--txt);border-radius:8px;cursor:pointer;font-size:16px}
.addcat button:hover{border-color:var(--accent);color:var(--accent)}
.main{flex:1;min-width:0;padding:18px 22px 60px;display:flex;flex-direction:column;gap:16px}
.composer{background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:10px}
.clbl{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--faint);display:flex;align-items:center;gap:8px}
.clbl .csub{color:var(--accent);text-transform:none;letter-spacing:0;font-size:11px}
.comphead{display:flex;align-items:center;gap:10px}
.clbl2{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--faint)}
.addcomp{margin-left:auto;display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--surf);color:var(--txt);border-radius:9px;padding:7px 12px;font-size:12.5px;cursor:pointer;font-family:inherit}
.addcomp:hover{border-color:var(--accent);color:var(--accent)}
.addcomp svg{width:14px;height:14px}
#composers{display:flex;flex-direction:column;gap:12px}
.composer.editing{border-color:var(--accent)}
.cdel{margin-left:auto;border:0;background:transparent;color:var(--faint);cursor:pointer;padding:2px 4px;border-radius:6px;display:flex;align-items:center}
.cdel:hover{color:var(--bad);background:color-mix(in srgb,var(--bad) 14%,transparent)}
.cdel svg{width:14px;height:14px}
.c-text{width:100%;min-height:120px;resize:vertical;background:var(--surf2);border:1px solid var(--line);border-radius:10px;padding:12px;color:var(--txt);font-size:14px;line-height:1.5;font-family:inherit;outline:none}
.c-text:focus{border-color:var(--accent)}
.cmeta{display:flex;gap:8px;flex-wrap:wrap}
.cmeta input,.cmeta select{background:var(--surf2);border:1px solid var(--line);border-radius:9px;padding:8px 10px;color:var(--txt);font-size:12.5px;font-family:inherit;outline:none}
.cmeta input{flex:1;min-width:160px}
.cmeta input:focus,.cmeta select:focus{border-color:var(--accent)}
.vsel{display:flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
.vsel button{background:var(--surf2);border:0;border-left:1px solid var(--line);color:var(--mut);font-size:12px;padding:8px 11px;cursor:pointer;font-family:inherit}
.vsel button:first-child{border-left:0}
.vsel button.on[data-cv="works"],.vsel .vw.on{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok);font-weight:600}
.vsel button.on[data-cv="fails"]{background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad);font-weight:600}
.vsel button.on[data-cv=""]{background:var(--accent-dim);color:var(--txt);font-weight:600}
.cbtns{display:flex;gap:8px;flex-wrap:wrap}
.cbtns button{border:1px solid var(--line);background:var(--surf2);color:var(--txt);border-radius:9px;padding:9px 14px;font-size:13px;cursor:pointer;font-family:inherit;display:flex;align-items:center;gap:7px}
.cbtns button:hover{border-color:var(--line2)}
.cbtns .primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600;margin-right:auto}
.cbtns .primary:hover{filter:brightness(1.06)}
.listhdr{display:flex;align-items:baseline;gap:10px;font-size:14px;font-weight:600}
.listhdr .count{color:var(--faint);font-size:12px;font-weight:400}
.list{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.card{background:var(--surf);border:1px solid var(--line);border-radius:12px;padding:13px;display:flex;flex-direction:column;gap:9px}
.card:hover{border-color:var(--line2)}
.ctop{display:flex;align-items:center;gap:8px}
.star{border:0;background:transparent;cursor:pointer;color:var(--faint);padding:2px;font-size:17px;line-height:1}
.star.on{color:var(--star)}
.vbadge{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-left:auto}
.vbadge button{background:transparent;border:0;border-left:1px solid var(--line);color:var(--mut);font-size:11px;padding:5px 8px;cursor:pointer;font-family:inherit}
.vbadge button:first-child{border-left:0}
.vbadge button.on.vw{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok)}
.vbadge button.on.vf{background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad)}
.vbadge button.on.vn{background:var(--accent-dim);color:var(--txt)}
.card h3{font-size:13.5px;font-weight:650}
.card .text{font-size:12.5px;line-height:1.5;color:var(--mut);white-space:pre-wrap;word-break:break-word;display:-webkit-box;-webkit-line-clamp:6;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer}
.card .text.full{-webkit-line-clamp:unset}
.card .catb{font-size:10.5px;color:var(--accent);background:var(--accent-dim);padding:2px 8px;border-radius:20px;align-self:flex-start}
.newb{font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:#fff;background:var(--accent);padding:2px 7px;border-radius:20px}
.card[draggable=true]{cursor:grab}
.card.dragging{opacity:.45}
.movemenu{position:fixed;z-index:60;background:var(--surf2);border:1px solid var(--line2);border-radius:10px;box-shadow:0 14px 44px rgba(0,0,0,.28);padding:6px;min-width:200px;max-height:62vh;overflow:auto}
.movemenu .mh{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);padding:6px 10px 4px}
.movemenu button{display:block;width:100%;text-align:left;background:transparent;border:0;color:var(--txt);font-size:13px;padding:7px 10px;border-radius:7px;cursor:pointer;font-family:inherit;white-space:pre}
.movemenu button:hover{background:var(--accent-dim);color:var(--accent)}
.movemenu button.cur{color:var(--accent);font-weight:600}
.tmplb{font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--accent);background:var(--accent-dim);padding:2px 7px;border-radius:20px}
.c-magic{display:inline-flex;align-items:center;gap:6px}
.tmplov{position:fixed;inset:0;background:rgba(0,0,0,.45);backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center;z-index:70}
.tmplbox{background:var(--surf);border:1px solid var(--line2);border-radius:14px;padding:18px;width:min(440px,92vw);box-shadow:0 24px 70px rgba(0,0,0,.4);display:flex;flex-direction:column;gap:6px}
.tmplh{font-size:15px;font-weight:650}
.tmplsub{font-size:12px;color:var(--mut);margin-bottom:6px}
.tmpllbl{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;margin-top:6px}
.tmplin{background:var(--surf2);border:1px solid var(--line);border-radius:9px;padding:9px 11px;color:var(--txt);font-size:13.5px;font-family:inherit;outline:none}
.tmplin:focus{border-color:var(--accent)}
.tmplbtns{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
.tmplbtns button{border:1px solid var(--line);background:var(--surf2);color:var(--txt);border-radius:9px;padding:9px 16px;font-size:13px;cursor:pointer;font-family:inherit}
.tmplbtns .primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.cacts{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
.cacts button{border:1px solid var(--line);background:var(--surf2);color:var(--mut);border-radius:7px;padding:6px 9px;font-size:11.5px;cursor:pointer;font-family:inherit;display:flex;align-items:center;gap:5px}
.cacts button:hover{border-color:var(--line2);color:var(--txt)}
.cacts .danger:hover{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,transparent)}
.cacts .key{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 35%,transparent)}
.empty{grid-column:1/-1;color:var(--faint);text-align:center;padding:48px 0;font-size:13px}
.tt{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(12px);background:var(--txt);color:var(--bg);padding:10px 16px;border-radius:10px;font-size:13px;opacity:0;pointer-events:none;transition:.2s;z-index:50}
.tt.show{opacity:1;transform:translateX(-50%) translateY(0)}
.tt.bad{background:var(--bad);color:#fff}
@media(max-width:760px){.layout{flex-direction:column}.side{width:auto;border-right:0;border-bottom:1px solid var(--line)}.filters{flex-direction:row;flex-wrap:wrap}}
</style></head><body>
<header>
  <h1>Biblioteca de prompts</h1>
  <input id="q" class="search" placeholder="Buscar en prompts…">
  <button id="libExport" class="hbtn" title="Descargar toda la biblioteca como archivo .json">Exportar</button>
  <button id="libImport" class="hbtn" title="Importar prompts desde un .json (se fusionan)">Importar</button>
  <input type="file" id="libFile" accept="application/json,.json" style="display:none">
  <span class="hint">Compón un prompt y envíalo a la interfaz principal ▸</span>
</header>
<div class="layout">
  <aside class="side">
    <div class="filters">
      <button class="f on" data-f="all">Todos</button>
      <button class="f" data-f="fav">★ Favoritos</button>
      <button class="f" data-f="works">✓ Sirve</button>
      <button class="f" data-f="fails">✗ No sirve</button>
    </div>
    <div class="cathdr">Categorías</div>
    <div class="cats" id="cats"></div>
    <div class="addcat">
      <input id="newCat" placeholder="Nueva categoría…" maxlength="40">
      <button id="addCat" title="Crear categoría">+</button>
    </div>
  </aside>
  <main class="main">
    <div class="comphead">
      <span class="clbl2">Compositores</span>
      <button class="addcomp" id="addComp"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>Nuevo compositor</button>
    </div>
    <div id="composers"></div>
    <div class="listhdr"><span id="listTitle">Todos</span> <span class="count" id="count"></span></div>
    <div class="list" id="list"></div>
  </main>
</div>
<div class="tt" id="tt"></div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let lib={categories:[],items:[]}, filter='all', curCat=null, q='', editCat=null, dragCatId=null, collapsed=new Set(), activeComposer=null;
function anyComposerEditing(){return !!document.querySelector('#composers .composer.editing')}
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function uid(){return 'p_'+Date.now().toString(36)+Math.random().toString(36).slice(2,7)}
// === árbol de categorías: {id,name,parent}; el orden en el array define el orden entre hermanas ===
function catById(id){return lib.categories.find(c=>c.id===id)}
function catName(id){const c=catById(id);return c?c.name:''}
function catChildren(pid){return lib.categories.filter(c=>(c.parent||'')===(pid||''))}
function catPath(id){const parts=[];let c=catById(id),g=0;while(c&&g++<50){parts.unshift(c.name);c=c.parent?catById(c.parent):null}return parts.join(' / ')}
function isDesc(id,ancestorId){let c=catById(id),g=0;while(c&&c.parent&&g++<50){if(c.parent===ancestorId)return true;c=catById(c.parent)}return false}
function catDescIds(id){const out=[id];catChildren(id).forEach(ch=>out.push(...catDescIds(ch.id)));return out}
function uniqueName(base,parent){let k=1,name=base;const sib=()=>catChildren(parent).some(c=>c.name===name);while(sib()){k++;name=base+' '+k}return name}
function toast(m,bad){const t=$('#tt');t.textContent=m;t.className='tt show'+(bad?' bad':'');clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove('show'),1800)}
// guardado inmediato, serializado (sin perder cambios) + coalescing si llegan varios seguidos
let saving=false, dirty=false;
async function doSave(){if(saving){dirty=true;return}saving=true;
 try{await fetch('/promptlib',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(lib)})}
 catch(e){toast('No se pudo guardar',1)}
 saving=false;if(dirty){dirty=false;doSave()}}
function save(){doSave()}
function libSnap(){return JSON.stringify({categories:lib.categories,items:lib.items})}
let lastSnap='';
// al volver a esta pestaña, recarga lo último del servidor (evita pisar cambios de otra pestaña) salvo si estás editando
async function reloadIfIdle(){if(editCat||anyComposerEditing()||saving||dirty)return;
 try{const fresh=await(await fetch('/promptlib')).json();
  if(!fresh||!Array.isArray(fresh.categories)||!Array.isArray(fresh.items))return;
  if(JSON.stringify({categories:fresh.categories,items:fresh.items})!==libSnap()){lib=fresh;migrate();render()}}catch(e){}}
function migrate(){ // formato viejo: categories = ["Nombre",...] e items.cat = nombre → árbol con ids
 if(lib.categories.some(c=>typeof c==='string')){
  const map={};
  lib.categories=lib.categories.map(c=>{if(typeof c!=='string')return c;const id=uid();map[c]=id;return {id,name:c,parent:''}});
  lib.items.forEach(it=>{if(it.cat&&map[it.cat])it.cat=map[it.cat];else if(it.cat&&!catById(it.cat))it.cat=''});
  save();}}
async function load(){try{lib=await(await fetch('/promptlib')).json()}catch(e){lib={categories:[],items:[]}}
 if(!Array.isArray(lib.categories))lib.categories=[];if(!Array.isArray(lib.items))lib.items=[];migrate();render()}
function filtered(){const scope=(curCat!=null&&curCat!=='')?new Set(catDescIds(curCat)):null;return lib.items.filter(it=>{
 if(curCat===''){if(it.cat)return false}
 else if(scope){if(!scope.has(it.cat||''))return false}
 if(filter==='fav'&&!it.fav)return false;
 if(filter==='works'&&it.verdict!=='works')return false;
 if(filter==='fails'&&it.verdict!=='fails')return false;
 if(q){const s=((it.title||'')+' '+(it.text||'')+' '+catName(it.cat)).toLowerCase();if(!s.includes(q))return false}
 return true})}
function catOptionsHTML(){
 let opts='<option value="">Sin categoría</option>';
 (function walk(pid,depth){catChildren(pid).forEach(c=>{opts+=`<option value="${esc(c.id)}">${'  '.repeat(depth)}${depth?'└ ':''}${esc(c.name)}</option>`;walk(c.id,depth+1)})})('',0);
 return opts}
function fillCatSelect(){const html=catOptionsHTML();$$('#composers .c-cat').forEach(sel=>{const cur=sel.value;sel.innerHTML=html;if([...sel.options].some(o=>o.value===cur))sel.value=cur})}
function renderCats(){const box=$('#cats');
 const ownCnt=id=>lib.items.filter(it=>it.cat===id).length;
 const none=lib.items.filter(it=>!it.cat).length;
 let html=`<button class="cat${curCat===''?' on':''}" data-cat=""><span class="tw"></span><span class="nm">Sin categoría</span><span class="cn">${none}</span></button>`;
 const rows=[];
 (function walk(pid,depth){catChildren(pid).forEach(c=>{
  const kids=catChildren(c.id),isCol=collapsed.has(c.id),pad=8+depth*15;
  if(c.id===editCat){rows.push(`<div class="cat editing" style="padding-left:${pad}px"><input class="catedit" data-old="${esc(c.id)}" value="${esc(c.name)}" maxlength="40" placeholder="Nombre…"></div>`)}
  else{rows.push(`<button class="cat${curCat===c.id?' on':''}" data-cat="${esc(c.id)}" draggable="true" style="padding-left:${pad}px" title="Arrastra para mover/ordenar · doble clic para renombrar">`
   +`<span class="tw" data-tw="${esc(c.id)}">${kids.length?(isCol?'▸':'▾'):''}</span>`
   +`<span class="nm">${esc(c.name)}</span><span class="cn">${ownCnt(c.id)}</span>`
   +`<span class="sub" data-subcat="${esc(c.id)}" title="Nueva subcarpeta">＋</span>`
   +`<span class="ed" data-editcat="${esc(c.id)}" title="Renombrar">✎</span>`
   +`<span class="del" data-delcat="${esc(c.id)}" title="Borrar">✕</span></button>`)}
  if(kids.length&&!isCol)walk(c.id,depth+1);
 })})('',0);
 box.innerHTML=html+rows.join('');fillCatSelect();
 const ie=box.querySelector('.catedit');
 if(ie){ie.focus();ie.select();
  ie.addEventListener('click',ev=>ev.stopPropagation());
  ie.addEventListener('keydown',ev=>{if(ev.key==='Enter'){ev.preventDefault();renameCat(ie.dataset.old,ie.value)}else if(ev.key==='Escape'){ev.preventDefault();editCat=null;render()}});
  ie.addEventListener('blur',()=>renameCat(ie.dataset.old,ie.value))}}
function renderList(){const items=filtered();
 $('#count').textContent=items.length+(items.length===1?' prompt':' prompts');
 $('#listTitle').textContent=curCat!=null?(curCat?catPath(curCat):'Sin categoría'):(filter==='fav'?'★ Favoritos':filter==='works'?'✓ Sirve':filter==='fails'?'✗ No sirve':'Todos');
 if(!items.length){$('#list').innerHTML='<div class="empty">No hay prompts aquí todavía. Compón uno arriba y pulsa «Guardar en biblioteca».</div>';return}
 $('#list').innerHTML=items.map(it=>{
  const v=it.verdict||'';
  return `<article class="card" data-id="${it.id}" draggable="true" title="Arrástrame a una categoría de la izquierda">
   <div class="ctop">
    <button class="star${it.fav?' on':''}" data-act="fav" title="Favorito">${it.fav?'★':'☆'}</button>
    ${it._new?'<span class="newb">nuevo</span>':''}
    ${templateVars(it.text).length?'<span class="tmplb" title="Tiene variables {…} que se rellenan al usarla">plantilla</span>':''}
    ${it.cat&&catById(it.cat)?`<span class="catb">${esc(catPath(it.cat))}</span>`:''}
    <div class="vbadge">
     <button class="vw${v==='works'?' on':''}" data-act="vworks" title="Sirve">✓</button>
     <button class="vf${v==='fails'?' on':''}" data-act="vfails" title="No sirve">✗</button>
     <button class="vn${v===''?' on':''}" data-act="vnone" title="Sin probar">—</button>
    </div>
   </div>
   ${it.title?`<h3>${esc(it.title)}</h3>`:''}
   <p class="text" data-act="expand">${esc(it.text||'')}</p>
   <div class="cacts">
    <button class="key" data-act="add">+ Compositor</button>
    <button data-act="replace">Reemplazar</button>
    <button data-act="move">Mover</button>
    <button data-act="copy">Copiar</button>
    <button data-act="edit">Editar</button>
    <button class="danger" data-act="del">Borrar</button>
   </div>
  </article>`}).join('')}
function render(){renderCats();renderList();
 $$('.f').forEach(b=>b.classList.toggle('on',b.dataset.f===filter&&curCat==null))}
async function copyText(t){try{await navigator.clipboard.writeText(t||'');toast('Copiado')}catch(e){toast('No se pudo copiar',1)}}
// === mover un prompt a una categoría (arrastrando o con el botón "Mover") ===
let dragItemId=null;
function moveItemTo(itemId,catId){const it=lib.items.find(x=>x.id===itemId);if(!it)return;it.cat=catId||'';it._new=false;save();render();toast(catId?('Movido a "'+catName(catId)+'"'):'Movido a Sin categoría')}
function closeMoveMenu(){const m=document.querySelector('.movemenu');if(m)m.remove();document.removeEventListener('click',moveMenuOutside,true)}
function moveMenuOutside(e){if(!e.target.closest('.movemenu'))closeMoveMenu()}
function openMoveMenu(itemId,anchor){closeMoveMenu();const it=lib.items.find(x=>x.id===itemId);if(!it)return;
 const m=document.createElement('div');m.className='movemenu';
 let html='<div class="mh">Mover a</div>';
 html+=`<button data-cat="" class="${!it.cat?'cur':''}">Sin categoría</button>`;
 (function walk(pid,depth){catChildren(pid).forEach(c=>{html+=`<button data-cat="${esc(c.id)}" class="${it.cat===c.id?'cur':''}">${'　'.repeat(depth)}${depth?'└ ':''}${esc(c.name)}</button>`;walk(c.id,depth+1)})})('',0);
 m.innerHTML=html;document.body.appendChild(m);
 const r=anchor.getBoundingClientRect();let left=r.left,top=r.bottom+4;
 if(left+m.offsetWidth>innerWidth-8)left=innerWidth-8-m.offsetWidth;
 if(top+m.offsetHeight>innerHeight-8)top=Math.max(8,r.top-m.offsetHeight-4);
 m.style.left=Math.max(8,left)+'px';m.style.top=top+'px';
 m.addEventListener('click',e=>{const b=e.target.closest('button[data-cat]');if(!b)return;moveItemTo(itemId,b.dataset.cat);closeMoveMenu()});
 setTimeout(()=>document.addEventListener('click',moveMenuOutside,true),0)}
// === plantillas: prompts con {variables} que se rellenan al usarlos ===
function templateVars(text){const set=[];(String(text||'').match(/\{[^{}]+\}/g)||[]).forEach(m=>{const n=m.slice(1,-1).trim();if(n&&!set.includes(n))set.push(n)});return set}
function fillTemplate(text,cb){const vars=templateVars(text);if(!vars.length){cb(text);return}
 const ov=document.createElement('div');ov.className='tmplov';
 ov.innerHTML='<div class="tmplbox"><div class="tmplh">Rellena la plantilla</div><div class="tmplsub">Completa los huecos del prompt y pulsa Insertar.</div>'
  +vars.map(v=>`<label class="tmpllbl">${esc(v)}</label><input class="tmplin" data-v="${esc(v)}" placeholder="${esc(v)}…">`).join('')
  +'<div class="tmplbtns"><button class="tmplcancel">Cancelar</button><button class="primary tmplok">Insertar</button></div></div>';
 document.body.appendChild(ov);
 const inputs=[...ov.querySelectorAll('.tmplin')];if(inputs[0])inputs[0].focus();
 const close=()=>ov.remove();
 ov.querySelector('.tmplcancel').onclick=close;
 ov.addEventListener('click',e=>{if(e.target===ov)close()});
 ov.querySelector('.tmplok').onclick=()=>{const map={};inputs.forEach(i=>map[i.dataset.v]=i.value);
  const out=String(text).replace(/\{([^{}]+)\}/g,(m,n)=>{const k=n.trim();return (map[k]!==undefined&&map[k]!=='')?map[k]:m});
  close();cb(out)};
 inputs.forEach(i=>i.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();ov.querySelector('.tmplok').click()}else if(e.key==='Escape'){e.preventDefault();close()}}))}
// === compositores (varios a la vez) ===
const COMP_TPL=`<section class="composer" data-cv="">
 <div class="clbl">Compositor <span class="csub editflag"></span><button class="cdel" title="Quitar este compositor"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
 <textarea class="c-text" placeholder="Escribe aquí tu prompt, o añade prompts guardados con «+ Compositor» para combinarlos…"></textarea>
 <div class="cmeta">
  <input class="c-title" placeholder="Título (opcional)">
  <select class="c-cat"></select>
  <div class="vsel c-vsel">
   <button class="vw" data-cv="works">✓ Sirve</button>
   <button class="vf" data-cv="fails">✗ No sirve</button>
   <button class="vn on" data-cv="">— sin probar</button>
  </div>
 </div>
 <div class="cbtns">
  <button class="primary c-send"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>Enviar a la interfaz principal</button>
  <button class="c-save"><svg viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>Guardar en biblioteca</button>
  <button class="c-magic"><svg viewBox="0 0 24 24"><path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5z"/><path d="M19 14l.7 2.3L22 17l-2.3.7L19 20l-.7-2.3L16 17l2.3-.7z"/></svg>Mejorar con IA</button>
  <button class="c-copy">Copiar</button>
  <button class="c-clear">Limpiar</button>
 </div>
</section>`;
function compSetCV(el,v){el.dataset.cv=v;el.querySelectorAll('.c-vsel button').forEach(b=>b.classList.toggle('on',b.dataset.cv===v))}
function compClear(el){el.querySelector('.c-text').value='';el.querySelector('.c-title').value='';compSetCV(el,'');el.querySelector('.c-cat').value='';el._editingId=null;el.classList.remove('editing');el.querySelector('.editflag').textContent=''}
function compAdd(el,t,replace){const c=el.querySelector('.c-text');if(replace||!c.value.trim())c.value=t;else c.value=c.value.trim()+'\n\n'+t;c.focus()}
function activeComp(){return (activeComposer&&document.body.contains(activeComposer))?activeComposer:(document.querySelector('#composers .composer')||addComposer(false))}
function updateCdel(){const comps=$$('#composers .composer');comps.forEach(c=>{const d=c.querySelector('.cdel');if(d)d.style.display=comps.length>1?'':'none'})}
function addComposer(focus){const wrap=document.createElement('div');wrap.innerHTML=COMP_TPL;const el=wrap.firstElementChild;
 $('#composers').appendChild(el);
 el.querySelector('.c-cat').innerHTML=catOptionsHTML();
 el.addEventListener('focusin',()=>{activeComposer=el});
 el.querySelectorAll('.c-vsel button').forEach(b=>b.onclick=()=>compSetCV(el,b.dataset.cv));
 el.querySelector('.c-clear').onclick=()=>compClear(el);
 el.querySelector('.c-copy').onclick=()=>copyText(el.querySelector('.c-text').value);
 el.querySelector('.c-magic').onclick=async()=>{const ta=el.querySelector('.c-text'),p=ta.value.trim();if(!p){toast('Escribe un prompt primero',1);return}
  const btn=el.querySelector('.c-magic'),html0=btn.innerHTML;btn.disabled=true;btn.textContent='Mejorando…';
  try{const r=await(await fetch('/magicprompt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p,mode:'imagen'})})).json();
   if(r.error)toast(r.error,1);else if(r.prompt){ta.value=r.prompt;toast('Prompt mejorado con IA ✨')}}catch(e){toast('No se pudo mejorar',1)}
  btn.disabled=false;btn.innerHTML=html0};
 el.querySelector('.c-send').onclick=()=>{const raw=el.querySelector('.c-text').value.trim();if(!raw){toast('Escribe o compón un prompt',1);return}
  fillTemplate(raw,async(p)=>{try{const r=await(await fetch('/promptstage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p})})).json();
   if(r&&r.ok)toast('Enviado a la interfaz principal ✓');else toast((r&&r.error)||'No se pudo enviar',1)}catch(e){toast('No se pudo enviar',1)}})};
 el.querySelector('.c-save').onclick=()=>{const text=el.querySelector('.c-text').value.trim();if(!text){toast('Escribe un prompt primero',1);return}
  const title=el.querySelector('.c-title').value.trim(),cat=el.querySelector('.c-cat').value,verdict=el.dataset.cv||'';
  if(el._editingId){const it=lib.items.find(x=>x.id===el._editingId);if(it){it.text=text;it.title=title;it.cat=cat;it.verdict=verdict}el._editingId=null;el.classList.remove('editing');el.querySelector('.editflag').textContent='';toast('Prompt actualizado')}
  else{lib.items.unshift({id:uid(),text,title,cat,verdict,fav:false,ts:Date.now()});toast('Guardado en la biblioteca')}
  save();render()};
 el.querySelector('.cdel').onclick=()=>{const comps=$$('#composers .composer');if(comps.length<=1){compClear(el);return}const wasActive=activeComposer===el;el.remove();if(wasActive)activeComposer=document.querySelector('#composers .composer');updateCdel()};
 activeComposer=el;updateCdel();
 if(focus)el.querySelector('.c-text').focus();
 return el}
// === eventos ===
$('#q').addEventListener('input',e=>{q=e.target.value.trim().toLowerCase();renderList()});
$$('.f').forEach(b=>b.onclick=()=>{filter=b.dataset.f;curCat=null;render()});
function addCategory(parent){const name=uniqueName('Nueva categoría',parent||'');const id=uid();
 lib.categories.push({id,name,parent:parent||''});if(parent)collapsed.delete(parent);
 editCat=id;save();render();toast('Categoría creada — edita el nombre cuando quieras')}
function delCategory(id){ // borra la categoría; sus subcarpetas e items suben al padre
 const c=catById(id);if(!c)return;const par=c.parent||'';
 catChildren(id).forEach(ch=>ch.parent=par);
 lib.items.forEach(it=>{if(it.cat===id)it.cat=par});
 lib.categories=lib.categories.filter(x=>x.id!==id);
 if(curCat===id)curCat=null;save();render();toast('Categoría borrada')}
function renameCat(id,nw){if(editCat===null)return;nw=(nw||'').trim();const c=catById(id);
 if(!c||!nw||nw===c.name){editCat=null;render();return}
 if(catChildren(c.parent||'').some(x=>x.id!==id&&x.name===nw)){editCat=null;render();toast('Ya existe una categoría con ese nombre aquí',1);return}
 c.name=nw;editCat=null;save();render();toast('Categoría renombrada')}
function moveCat(dragId,targetId,mode){ // mode: before|after|inside
 if(dragId===targetId||isDesc(targetId,dragId))return;
 const d=catById(dragId);if(!d)return;
 lib.categories=lib.categories.filter(c=>c.id!==dragId);
 if(mode==='inside'){d.parent=targetId;collapsed.delete(targetId);const ti=lib.categories.findIndex(c=>c.id===targetId);lib.categories.splice(ti+1,0,d)}
 else{const t=catById(targetId);d.parent=t?(t.parent||''):'';const ti=lib.categories.findIndex(c=>c.id===targetId);lib.categories.splice(mode==='before'?ti:ti+1,0,d)}
 save();render()}
$('#cats').addEventListener('click',e=>{
 const tw=e.target.closest('[data-tw]');
 if(tw){e.stopPropagation();const id=tw.dataset.tw;if(collapsed.has(id))collapsed.delete(id);else collapsed.add(id);render();return}
 const sub=e.target.closest('[data-subcat]');
 if(sub){e.stopPropagation();addCategory(sub.dataset.subcat);return}
 const ed=e.target.closest('[data-editcat]');
 if(ed){e.stopPropagation();editCat=ed.dataset.editcat;render();return}
 const del=e.target.closest('[data-delcat]');
 if(del){e.stopPropagation();const id=del.dataset.delcat;
  if(!del.dataset.arm){$$('#cats .del').forEach(x=>delete x.dataset.arm);del.dataset.arm='1';del.textContent='✓?';setTimeout(()=>{if(del){del.textContent='✕';delete del.dataset.arm}},2200);return}
  delCategory(id);return}
 const cat=e.target.closest('.cat');if(!cat||cat.classList.contains('editing'))return;curCat=cat.dataset.cat;filter='all';render()});
$('#cats').addEventListener('dblclick',e=>{const cat=e.target.closest('.cat[data-cat]');if(!cat||!cat.dataset.cat)return;editCat=cat.dataset.cat;render()});
// arrastrar para mover/ordenar
$('#cats').addEventListener('dragstart',e=>{const cat=e.target.closest('.cat[data-cat]');if(!cat||!cat.dataset.cat){e.preventDefault();return}dragCatId=cat.dataset.cat;e.dataTransfer.effectAllowed='move';try{e.dataTransfer.setData('text/plain',dragCatId)}catch(x){}});
function clearDrop(){$$('#cats .cat').forEach(c=>c.classList.remove('dropbefore','dropafter','dropinside'))}
$('#cats').addEventListener('dragover',e=>{const cat=e.target.closest('.cat');if(!cat)return;
 if(dragItemId){e.preventDefault();e.dataTransfer.dropEffect='move';clearDrop();cat.classList.add('dropinside');return}
 if(!cat.dataset.cat||!dragCatId)return;
 e.preventDefault();e.dataTransfer.dropEffect='move';clearDrop();
 const r=cat.getBoundingClientRect(),y=e.clientY-r.top;
 const mode=y<r.height*0.28?'before':y>r.height*0.72?'after':'inside';
 cat.classList.add(mode==='before'?'dropbefore':mode==='after'?'dropafter':'dropinside')});
$('#cats').addEventListener('dragleave',e=>{if(!e.target.closest('#cats'))clearDrop()});
$('#cats').addEventListener('drop',e=>{const cat=e.target.closest('.cat');clearDrop();
 if(dragItemId){if(cat){e.preventDefault();moveItemTo(dragItemId,cat.dataset.cat||'')}dragItemId=null;return}
 if(!cat||!cat.dataset.cat||!dragCatId){dragCatId=null;return}
 e.preventDefault();const r=cat.getBoundingClientRect(),y=e.clientY-r.top;
 const mode=y<r.height*0.28?'before':y>r.height*0.72?'after':'inside';
 moveCat(dragCatId,cat.dataset.cat,mode);dragCatId=null});
$('#cats').addEventListener('dragend',()=>{clearDrop();dragCatId=null});
$('#addCat').onclick=()=>{const i=$('#newCat');const n=i.value.trim();
 if(!n){addCategory('');i.value='';return}
 lib.categories.push({id:uid(),name:n,parent:''});i.value='';save();render();toast('Categoría creada')};
$('#newCat').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();$('#addCat').click()}});
$('#libExport').onclick=()=>{const blob=new Blob([JSON.stringify(lib,null,1)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='biblioteca-prompts.json';document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},1000);toast('Biblioteca exportada')};
$('#libImport').onclick=()=>$('#libFile').click();
$('#libFile').onchange=async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;
 let data;try{data=JSON.parse(await f.text())}catch(_){toast('Archivo .json inválido','bad');return}
 const cats=Array.isArray(data.categories)?data.categories:[],its=Array.isArray(data.items)?data.items:[];
 if(!cats.length&&!its.length){toast('No encontré prompts en ese archivo','bad');return}
 const idmap={};   // id de categoría del archivo → id en esta biblioteca (fusiona por nombre+padre)
 cats.forEach(c=>{if(!c||!c.id)return;const nm=(c.name||'').trim();if(!nm)return;const ex=lib.categories.find(x=>x.name===nm&&((x.parent||'')===(c.parent||'')));if(ex){idmap[c.id]=ex.id}else{const nid=uid();idmap[c.id]=nid;lib.categories.push({id:nid,name:nm,parent:''})}});
 cats.forEach(c=>{if(c&&c.parent&&idmap[c.id]){const me=catById(idmap[c.id]);if(me)me.parent=idmap[c.parent]||''}});
 const existing=new Set(lib.items.map(x=>(x.text||'').trim()));let n=0;
 its.forEach(x=>{const t=(x.text||x.prompt||'').trim();if(!t||existing.has(t))return;existing.add(t);lib.items.unshift({id:uid(),text:t,title:(x.title||'').trim(),cat:idmap[x.cat]||'',verdict:x.verdict||'',fav:!!x.fav,_new:true,ts:Date.now()});n++});
 save();render();toast(n?(n+' prompt(s) importados'):'Nada nuevo que importar (ya estaban)')};
$('#addComp').onclick=()=>addComposer(true);
$('#list').addEventListener('dragstart',e=>{const card=e.target.closest('.card');if(!card){return}dragItemId=card.dataset.id;e.dataTransfer.effectAllowed='move';try{e.dataTransfer.setData('text/plain',card.dataset.id)}catch(x){}card.classList.add('dragging')});
$('#list').addEventListener('dragend',e=>{const card=e.target.closest('.card');if(card)card.classList.remove('dragging');dragItemId=null;clearDrop()});
$('#list').addEventListener('click',e=>{
 const card=e.target.closest('.card');if(!card)return;const id=card.dataset.id;const it=lib.items.find(x=>x.id===id);if(!it)return;
 const b=e.target.closest('[data-act]');if(!b)return;const act=b.dataset.act;
 if(act==='expand'){b.classList.toggle('full');return}
 if(act==='fav'){it.fav=!it.fav;it._new=false;save();renderList();renderCats();return}
 if(act==='vworks'){it.verdict=it.verdict==='works'?'':'works';it._new=false;save();renderList();return}
 if(act==='vfails'){it.verdict=it.verdict==='fails'?'':'fails';it._new=false;save();renderList();return}
 if(act==='vnone'){it.verdict='';it._new=false;save();renderList();return}
 if(act==='add'){compAdd(activeComp(),it.text,false);toast('Añadido al compositor');return}
 if(act==='replace'){compAdd(activeComp(),it.text,true);toast('Compositor reemplazado');return}
 if(act==='move'){openMoveMenu(id,b);return}
 if(act==='copy'){copyText(it.text);return}
 if(act==='edit'){it._new=false;const el=activeComp();el.querySelector('.c-text').value=it.text||'';el.querySelector('.c-title').value=it.title||'';el.querySelector('.c-cat').value=it.cat||'';compSetCV(el,it.verdict||'');el._editingId=id;el.classList.add('editing');el.querySelector('.editflag').textContent='· editando (Guardar actualiza)';window.scrollTo({top:0,behavior:'smooth'});el.querySelector('.c-text').focus();return}
 if(act==='del'){if(!b.dataset.arm){b.dataset.arm='1';b.textContent='¿Seguro?';setTimeout(()=>{if(b){b.textContent='Borrar';delete b.dataset.arm}},2200);return}
  lib.items=lib.items.filter(x=>x.id!==id);save();render();toast('Prompt borrado');return}});
// === recibir prompts enviados desde el historial y apilarlos ===
let inboxReady=false;
async function pollInbox(){if(!inboxReady)return;
 try{const r=await(await fetch('/promptinbox')).json();
  if(r.items&&r.items.length){
   r.items.forEach(x=>{const t=(x.prompt||'').trim();if(t)lib.items.unshift({id:uid(),text:t,title:(x.title||'').trim(),cat:'',verdict:'',fav:false,_new:true,ts:Date.now()})});
   save();render();toast(r.items.length+(r.items.length>1?' prompts recibidos del historial':' prompt recibido del historial'))}}catch(e){}}
addComposer(false); // empieza con un compositor
(async()=>{await load();inboxReady=true;pollInbox();setInterval(pollInbox,2500);})();
async function onReturn(){await reloadIfIdle();pollInbox()}
window.addEventListener('focus',onReturn);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)onReturn()});
// respaldo: si quedó algo sin guardar al cerrar/ocultar la pestaña, lo mandamos con sendBeacon
function flushBeacon(){try{navigator.sendBeacon('/promptlib',new Blob([JSON.stringify(lib)],{type:'application/json'}))}catch(e){}}
window.addEventListener('pagehide',flushBeacon);
document.addEventListener('visibilitychange',()=>{if(document.hidden&&(saving||dirty))flushBeacon()});
</script></body></html>"""


def promptlib_html():
    return PROMPTLIB_PAGE


class H(BaseHTTPRequestHandler):
    server_version = "GioStudio"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _guard(self, post=False):
        # anti DNS-rebinding: solo aceptamos peticiones dirigidas a localhost
        host = (self.headers.get("Host") or "").lower()
        if host not in ALLOWED_HOSTS:
            self._send(403, "host no permitido", "text/plain")
            return False
        # anti-CSRF: si el navegador declara un Origin, debe ser esta misma app
        if post:
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc.lower() not in ALLOWED_HOSTS:
                self._send(403, "origen no permitido", "text/plain")
                return False
        return True

    def _send(self, code, body, ctype="application/json", extra=None):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        headers = {"Content-Type": ctype, "Content-Length": str(len(b)),
                   "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                   "Cache-Control": "no-store"}  # sin esto el navegador cachea la UI vieja
        headers.update(extra or {})
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _json(self, o, code=200):
        self._send(code, json.dumps(o, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 720 * 1024 * 1024:   # ~512MB de imágenes crudas + inflación base64 (límite real de OpenAI)
            raise ValueError("La petición supera el límite de 720MB")
        return json.loads(self.rfile.read(n) or b"{}")

    def _proj(self, b=None):
        # proyecto de la petición: body.project → query ?project= (incl. vacío=General) → proyecto activo
        if b is not None and "project" in b:
            return b.get("project") or ""
        q = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("project")
        return q[0] if q is not None else ACTIVE_PROJ

    def _sub(self, b=None):
        # subproyecto de la petición: body.sub → query ?sub= (incl. vacío=raíz) → subproyecto activo
        if b is not None and "sub" in b:
            return b.get("sub") or ""
        q = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("sub")
        return q[0] if q is not None else ACTIVE_SUB

    # ---- streaming (imágenes parciales / preview en vivo) ----
    def _stream_open(self):
        self.send_response(200)
        for k, v in {"Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store",
                     "X-Content-Type-Options": "nosniff", "Connection": "close"}.items():
            self.send_header(k, v)
        self.end_headers()

    def _emit(self, obj):
        try:
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
        except Exception:
            pass

    def _conn_msg(self, e):
        # mensaje claro y amable cuando OpenAI corta la conexión por una generación lenta (>~60s),
        # en vez del críptico "Remote end closed connection without response"
        s = str(getattr(e, "reason", None) or e)
        low = s.lower()
        if ("closed connection" in low or "timed out" in low or "timeout" in low
                or "eof occurred" in low or "reset by peer" in low or isinstance(e, TimeoutError)):
            return ("La conexión se cortó a mitad de la imagen. CAUSA #1: un VPN activo (Surfshark, etc.) "
                    "suele matar las conexiones lentas a los ~60s — apágalo o excluye la app del VPN. "
                    "Si no usas VPN, OpenAI puede estar lento: reintenta o baja calidad/tamaño.")
        return f"Sin conexión con OpenAI: {s}"

    def _pump_sse(self, resp, meta, model_used="gpt-image-2"):
        # lee el SSE de OpenAI: emite cada imagen parcial (preview en vivo) y, al final, guarda y emite el resultado
        final_b64, usage = None, {}
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                ev = json.loads(chunk)
            except Exception:
                continue
            t = ev.get("type", "")
            if "partial_image" in t:
                b64 = ev.get("b64_json") or ev.get("b64") or ""
                if b64:
                    self._emit({"type": "partial", "b64": b64, "i": ev.get("partial_image_index", 0)})
            elif "completed" in t:
                final_b64 = ev.get("b64_json") or final_b64
                usage = ev.get("usage") or usage
        if not final_b64:
            self._emit({"type": "error", "error":
                        "OpenAI tardó demasiado con esta imagen y cortó la conexión. "
                        "Prueba con calidad media o un tamaño más pequeño."})
            return
        res = self._save_results({"data": [{"b64_json": final_b64}], "usage": usage}, meta, model_used=model_used)
        self._emit({"type": "done", "result": res})

    def _read_final_b64(self, resp):
        # lee el SSE de OpenAI en silencio (sin reenviar parciales) y devuelve (b64_final, usage).
        # Pedir streaming mantiene la conexión con datos fluyendo → las ediciones lentas (>60s) no
        # se cortan al límite de ~60s de OpenAI a las conexiones silenciosas.
        final_b64, usage = None, {}
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                ev = json.loads(chunk)
            except Exception:
                continue
            if "completed" in ev.get("type", ""):
                final_b64 = ev.get("b64_json") or final_b64
                usage = ev.get("usage") or usage
        return final_b64, usage

    def _stream_err(self, e):
        if isinstance(e, urllib.error.HTTPError):
            self._emit({"type": "error", "error": self._err(e)})
        else:
            self._emit({"type": "error", "error": self._conn_msg(e)})

    def do_GET(self):
        if not self._guard():
            return
        if urlparse(self.path).path in ("/", "/index.html"):
            page = HTML.replace("<script>", "<script>\nwindow.I18N=" + I18N_JSON + ";\n", 1)
            return self._send(200, page, "text/html; charset=utf-8",
                              {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"})
        if self.path == "/keystatus":
            try:
                list((ROOT.resolve()).iterdir())
                data_ok = True
            except Exception:
                data_ok = False
            return self._json({"ok": bool(key()), "data_ok": data_ok})
        if urlparse(self.path).path == "/history":
            return self._json(load_json(phist_json(self._proj(), self._sub()), []))
        if self.path == "/projects":
            data = load_projects()
            subs = {k: list_subs(k) for k in data.keys()}   # k="" es General
            return self._json({"projects": data, "subs": subs})
        if self.path == "/projectcards":
            glabel = (load_json(CONF_JSON, {}).get("general_label") or "General")

            def _count_cover(n, sk=""):
                items = load_json(phist_json(n, sk), [])
                imgs = [it for it in items if it.get("kind") not in ("tts", "stt", "sfx", "vid") and it.get("file")]
                return len(imgs), (imgs[0]["file"] if imgs else "")

            cards = []
            for n in [""] + [k for k in load_projects().keys() if k]:
                cnt, cov = _count_cover(n)
                subs = []
                for s in list_subs(n):
                    sc, scov = _count_cover(n, s["key"])
                    subs.append({"key": s["key"], "label": s["label"], "count": sc, "cover": scov})
                cards.append({"name": n, "label": (glabel if n == "" else n), "count": cnt,
                              "cover": cov, "subs": subs})
            return self._json({"cards": cards})
        if urlparse(self.path).path == "/config":
            conf = load_json(CONF_JSON, {})
            pr = self._proj()   # proyecto activo (o ?project=): sus carpetas
            sb = self._sub()    # subproyecto activo (o ?sub=)
            glabel = conf.get("general_label", "") or "General"
            return self._json({"save_dir": conf.get("save_dir", ""),
                               "effective": str(save_dir_sub(pr, sb)).replace(str(HOME), "~"),
                               "shelf_effective": str(shelf_dir_sub(pr, sb)).replace(str(HOME), "~"),
                               "project": pr, "sub": sb, "project_label": (glabel if is_general(pr) else pr),
                               "general_label": glabel,
                               "voice_styles": conf.get("voice_styles", [])})
        if self.path == "/backupstatus":
            return self._json(backup_status())
        if self.path == "/backup.zip":
            data = build_backup_zip()   # organizado por proyecto (Historial / Mis imágenes / Subproyectos)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="studio-backup-{time.strftime("%Y%m%d_%H%M")}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if self.path == "/clone.zip":
            data = build_clone_zip()    # copia EXACTA de ~/image-studio (reimportable tal cual)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="gio-studio-copia-exacta-{time.strftime("%Y%m%d_%H%M")}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if self.path == "/falstatus":
            return self._json({"ok": bool(fal_key())})
        if self.path.startswith("/videostatus?"):
            return self.h_videostatus()
        if self.path == "/elstatus":
            if not el_key():
                return self._json({"ok": False})
            try:
                with urllib.request.urlopen(urllib.request.Request(EL_API + "/user/subscription",
                        headers={"xi-api-key": el_key()}), timeout=20) as r:
                    s = json.loads(r.read())
                return self._json({"ok": True, "used": s.get("character_count", 0),
                                   "limit": s.get("character_limit", 0), "tier": s.get("tier", "")})
            except Exception:
                return self._json({"ok": False})
        if self.path == "/elvoices":
            if not el_key():
                return self._json({"voices": []})
            try:
                with urllib.request.urlopen(urllib.request.Request(EL_API + "/voices",
                        headers={"xi-api-key": el_key()}), timeout=30) as r:
                    data = json.loads(r.read())
                vs = [{"id": v["voice_id"], "name": v.get("name", "?"), "category": v.get("category", "")}
                      for v in data.get("voices", [])]
                return self._json({"voices": vs})
            except Exception as e:
                return self._json({"voices": [], "error": str(e)})
        if self.path.startswith("/file?"):
            q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            # si la petición especifica project, el sub AUSENTE significa raíz ("") — no el subproyecto activo
            pr = q.get("project", [None])[0]; sb = q.get("sub", [""])[0]
            if pr is None:
                pr, sb = ACTIVE_PROJ, ACTIVE_SUB
            fp = phist_dir(pr, sb) / os.path.basename(q.get("name", [""])[0])
            if q.get("thumb") and fp.is_file():
                tp = thumb_for(fp)
                if tp:
                    return self._send(200, tp.read_bytes(), "image/jpeg", {"Cache-Control": "private, max-age=86400"})
            ctype = MIME.get(fp.suffix.lstrip(".").lower(), "application/octet-stream")
            return self._send(200, fp.read_bytes(), ctype, {"Cache-Control": "private, max-age=86400"}) if fp.is_file() else self._send(404, "no", "text/plain")
        if self.path.startswith("/pfile?"):
            q = parse_qs(urlparse(self.path).query)
            fp = proj_folder(q.get("project", [""])[0]) / os.path.basename(q.get("name", [""])[0])
            ctype = MIME.get(fp.suffix.lstrip(".").lower(), "application/octet-stream")
            return self._send(200, fp.read_bytes(), ctype) if fp.is_file() else self._send(404, "no", "text/plain")
        if self.path.startswith("/reffile?"):
            q = parse_qs(urlparse(self.path).query)
            fp = phist_dir(q.get("project", [""])[0], q.get("sub", [""])[0]) / "_refs" / os.path.basename(q.get("name", [""])[0])
            ctype = MIME.get(fp.suffix.lstrip(".").lower(), "application/octet-stream")
            return self._send(200, fp.read_bytes(), ctype, {"Cache-Control": "private, max-age=86400"}) if fp.is_file() else self._send(404, "no", "text/plain")
        if self.path == "/trash":
            idx = load_json(TRASH_INDEX, [])
            items = [r for r in idx if (TRASH_DIR / os.path.basename(r.get("token", ""))).is_file()]
            glabel = load_json(CONF_JSON, {}).get("general_label") or "General"
            for r in items:
                p = r.get("project", "")
                r["plabel"] = (glabel if is_general(p) else p) + ((" › " + r.get("sub")) if r.get("sub") else "")
            return self._json({"items": items})
        if self.path.startswith("/trashfile?"):
            tok = os.path.basename(parse_qs(urlparse(self.path).query).get("token", [""])[0])
            fp = TRASH_DIR / tok
            if not tok or not fp.is_file() or TRASH_DIR.resolve() != fp.resolve().parent:
                return self._send(404, "no", "text/plain")
            nm = tok.split("__", 1)[1] if "__" in tok else tok
            ctype = MIME.get(os.path.splitext(nm)[1].lstrip(".").lower(), "application/octet-stream")
            return self._send(200, fp.read_bytes(), ctype, {"Cache-Control": "private, max-age=60"})
        if urlparse(self.path).path == "/shelf":
            pr = self._proj()
            sb = self._sub()
            shdir = shelf_dir_sub(pr, sb)
            return self._json({"items": load_json(pshelf_json(pr, sb), []),
                               "dir": str(shdir).replace(str(HOME), "~")})
        if self.path == "/pickfolder":
            # diálogo nativo de macOS para elegir carpeta (la app corre local)
            try:
                osa = ('tell application "System Events" to activate\n'
                       'POSIX path of (choose folder with prompt "Elige dónde guardar las imágenes")')
                r = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True, timeout=180)
                path = r.stdout.strip()
                if path:
                    return self._json({"path": path.replace(str(HOME), "~")})
                return self._json({"canceled": True})   # el usuario canceló
            except Exception as e:
                return self._json({"error": f"No pude abrir el selector: {e}"})
        if self.path.startswith("/shelffile?"):
            q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            pr = q.get("project", [None])[0]; sb = q.get("sub", [""])[0]
            if pr is None:
                pr, sb = ACTIVE_PROJ, ACTIVE_SUB
            fp = pshelf_dir(pr, sb) / os.path.basename(q.get("name", [""])[0])
            if q.get("thumb") and fp.is_file():
                tp = thumb_for(fp)
                if tp:
                    return self._send(200, tp.read_bytes(), "image/jpeg", {"Cache-Control": "private, max-age=86400"})
            ctype = MIME.get(fp.suffix.lstrip(".").lower(), "application/octet-stream")
            return self._send(200, fp.read_bytes(), ctype, {"Cache-Control": "private, max-age=86400"}) if fp.is_file() else self._send(404, "no", "text/plain")
        if self.path == "/stage":
            with STAGE_LOCK:
                items = list(STAGE)
                STAGE.clear()
            return self._json({"items": items})
        if self.path == "/promptstage":
            with PROMPT_STAGE_LOCK:
                items = list(PROMPT_STAGE)
                PROMPT_STAGE.clear()
            return self._json({"items": items})
        if self.path == "/promptinbox":
            with PROMPT_INBOX_LOCK:
                items = list(PROMPT_INBOX)
                PROMPT_INBOX.clear()
            return self._json({"items": items})
        if self.path == "/promptlib":
            return self._json(load_promptlib())
        if urlparse(self.path).path == "/biblioteca":
            return self._send(200, promptlib_html(), "text/html; charset=utf-8",
                              {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"})
        if urlparse(self.path).path == "/galeria":
            q = parse_qs(urlparse(self.path).query)
            src = q.get("src", ["history"])[0]
            fav = q.get("fav", ["0"])[0] == "1"
            proj = q.get("project", [ACTIVE_PROJ])[0]
            sub = q.get("sub", [""])[0]
            subs_filter = q.get("subs", [""])[0]
            return self._send(200, gallery_html(src, fav, proj, sub, subs_filter), "text/html; charset=utf-8",
                              {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"})
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._guard(post=True):
            return
        try:
            h = {"/setkey": self.h_setkey, "/generate": self.h_generate, "/edit": self.h_edit,
                 "/project": self.h_project, "/projectdel": self.h_projectdel, "/projectrename": self.h_projectrename, "/projectref": self.h_projectref,
                 "/projectrefdel": self.h_projectrefdel, "/distill": self.h_distill,
                 "/historydel": self.h_historydel, "/config": self.h_config,
                 "/speech": self.h_speech, "/transcribe": self.h_transcribe,
                 "/elkey": self.h_elkey, "/elspeech": self.h_elspeech,
                 "/elsfx": self.h_elsfx, "/elclone": self.h_elclone,
                 "/icloudsync": self.h_icloudsync, "/datasync": self.h_datasync,
                 "/falkey": self.h_falkey, "/video": self.h_video,
                 "/histfav": self.h_histfav, "/imgcolors": self.h_imgcolors, "/magicprompt": self.h_magicprompt,
                 "/describe": self.h_describe, "/upscale": self.h_upscale,
                 "/detectsubjects": self.h_detectsubjects,
                 "/music": self.h_music, "/lipsync": self.h_lipsync,
                 "/shelfadd": self.h_shelf_add, "/shelfdel": self.h_shelf_del,
                 "/moveitem": self.h_moveitem, "/deleteitems": self.h_deleteitems,
                 "/restoreitems": self.h_restoreitems,
                 "/trashrestore": self.h_trashrestore, "/trashdelete": self.h_trashdelete,
                 "/shelfcopyall": self.h_shelfcopyall,
                 "/promptlib": self.h_promptlib, "/promptstage": self.h_promptstage,
                 "/promptinbox": self.h_promptinbox,
                 "/stage": self.h_stage, "/setproject": self.h_setproject,
                 "/subcreate": self.h_subcreate, "/subrename": self.h_subrename,
                 "/subdel": self.h_subdel, "/subconvert": self.h_subconvert,
                 "/subpromote": self.h_subpromote, "/suborder": self.h_suborder,
                 "/projorder": self.h_projorder, "/itemsorder": self.h_itemsorder,
                 "/import": self.h_import}.get(self.path)
            if h:
                return h()
        except Exception as e:
            return self._json({"error": str(e)})
        return self._json({"error": "ruta no encontrada"}, 404)

    def h_setkey(self):
        k = (self._body().get("key") or "").strip()
        if not validate_key(k):
            return self._json({"ok": False, "error": "La clave no es válida"})
        KEY_FILE.write_text(k)
        try:
            os.chmod(KEY_FILE, 0o600)
        except Exception:
            pass
        return self._json({"ok": True})

    def h_project(self):
        b = self._body()
        name = (b.get("name") or "").strip()
        is_style_save = ("style" in b) or ("style_video" in b)
        # crear exige nombre; guardar estilo del espacio General llega con name vacío y es válido
        if not name and not is_style_save:
            return self._json({"error": "Falta el nombre del proyecto"})
        key = proj_key(name)  # "general" para el espacio General
        with LOCK:
            pr = load_json(PROJ_JSON, {})
            cur = pr.get(key)
            if not isinstance(cur, dict):
                cur = {"style": cur if isinstance(cur, str) else "", "refs": []}
            cur.setdefault("refs", [])
            if "style" in b:  # solo pisa el estilo si la petición lo trae (crear ≠ guardar)
                cur["style"] = b["style"]
                try:
                    (proj_folder(name) / "estilo.md").write_text(b["style"])
                except Exception:
                    pass
            if "style_video" in b:
                cur["style_video"] = b["style_video"]
                try:
                    (proj_folder(name) / "estilo-video.md").write_text(b["style_video"])
                except Exception:
                    pass
            pr[key] = cur
            save_json(PROJ_JSON, pr)
        return self._json({"ok": True})

    def h_projectdel(self):
        name = (self._body().get("name") or "").strip()
        if not name:
            return self._json({"error": "Falta el nombre del proyecto"})
        with LOCK:
            pr = load_json(PROJ_JSON, {})
            key = proj_key(name)
            if key in pr:
                del pr[key]
                save_json(PROJ_JSON, pr)
        if is_general(name):
            # vaciar el espacio General: borra sus imágenes (historial + estante) y resetea sus índices
            with LOCK:
                for d in (HIST_DIR, SHELF_DIR):
                    for f in d.glob("*"):
                        if f.is_file():
                            try:
                                f.unlink()
                            except Exception:
                                pass
                save_json(HIST_JSON, [])
                save_json(SHELF_JSON, [])
            return self._json({"ok": True, "cleared": True})
        try:
            shutil.rmtree(PROJ_DIR / safe(name))
        except Exception:
            pass
        return self._json({"ok": True})

    def h_projectrename(self):
        global ACTIVE_PROJ
        b = self._body()
        old = (b.get("old") or "").strip()
        new = (b.get("new") or "").strip()
        if not new:
            return self._json({"error": "Falta el nombre nuevo."})
        if is_general(old):
            # renombrar «General» = etiqueta personalizada (no mueve datos; su valor sigue siendo "")
            with LOCK:
                conf = load_json(CONF_JSON, {})
                conf["general_label"] = new
                save_json(CONF_JSON, conf)
            return self._json({"ok": True, "name": "", "label": new})
        if is_general(new):
            return self._json({"error": "Ese nombre está reservado."})
        with LOCK:
            pr = load_json(PROJ_JSON, {})
            if new in pr and new != old:
                return self._json({"error": "Ya existe un proyecto con ese nombre."})
            oldf, newf = PROJ_DIR / safe(old), PROJ_DIR / safe(new)
            if safe(old) != safe(new):
                if newf.exists():
                    return self._json({"error": "Ya existe una carpeta con ese nombre."})
                if oldf.exists():
                    oldf.rename(newf)
            if old in pr:
                pr[new] = pr.pop(old)
                save_json(PROJ_JSON, pr)
            try:  # reetiquetar el historial del proyecto (raíz + cada subproyecto)
                for sk in [""] + [s["key"] for s in list_subs(new)]:
                    jp = phist_json(new, sk)
                    h = load_json(jp, [])
                    if not h:
                        continue
                    for it in h:
                        it["project"] = new
                    save_json(jp, h)
            except Exception:
                pass
        if ACTIVE_PROJ == old:
            ACTIVE_PROJ = new
        return self._json({"ok": True, "name": new})

    def h_projectref(self):
        b = self._body()
        name, img = b["project"], b["image"]
        fn = f"ref_{uuid.uuid4().hex[:8]}_{safe(img.get('name','ref'))}"
        if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            fn += ".png"
        (proj_folder(name) / fn).write_bytes(base64.b64decode(img["b64"]))
        with LOCK:
            pr = load_json(PROJ_JSON, {})
            key = proj_key(name)
            cur = pr.get(key)
            if not isinstance(cur, dict):
                cur = {"style": cur if isinstance(cur, str) else "", "refs": []}
            cur.setdefault("refs", []).append(fn)
            pr[key] = cur
            save_json(PROJ_JSON, pr)
        return self._json({"ok": True, "file": fn})

    def h_projectrefdel(self):
        b = self._body()
        f = os.path.basename(b["file"])
        try:
            (proj_folder(b["project"]) / f).unlink()
        except Exception:
            pass
        with LOCK:
            pr = load_json(PROJ_JSON, {})
            key = proj_key(b["project"])
            if key in pr and isinstance(pr[key], dict):
                pr[key]["refs"] = [x for x in pr[key].get("refs", []) if x != f]
                save_json(PROJ_JSON, pr)
        return self._json({"ok": True})

    def h_suborder(self):
        # guarda el orden personalizado (arrastrable) de subproyectos de un proyecto
        b = self._body()
        proj = b.get("project", "") or ""
        order = [str(k) for k in (b.get("order") or []) if k]
        base = proj_folder(proj) / "sub"
        if not base.is_dir():
            return self._json({"error": "Sin subproyectos"})
        with LOCK:
            save_json(base / "_order.json", order)
        return self._json({"ok": True, "subs": list_subs(proj)})

    def h_import(self):
        # restaura una copia EXACTA (clone.zip) dentro de ~/image-studio, tal cual
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            return self._json({"error": "Archivo vacío"})
        if n > 8 * 1024 * 1024 * 1024:
            return self._json({"error": "El archivo supera 8GB"})
        tmpdir = ROOT / ".import"
        tmpdir.mkdir(parents=True, exist_ok=True)
        zp = tmpdir / ("upload_" + uuid.uuid4().hex + ".zip")
        remaining = n
        try:
            with open(zp, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(2 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            restored = 0
            try:
                zf = zipfile.ZipFile(zp)
            except Exception:
                return self._json({"error": "El archivo no es un .zip válido."})
            root_resolved = ROOT.resolve()
            with LOCK:
                with zf:
                    members = [i for i in zf.infolist() if not i.is_dir() and i.filename.startswith("image-studio/")]
                    if not members:
                        return self._json({"error": "Este zip no es una «copia exacta». Descarga «Copia exacta (para importar)» y usa ese archivo."})
                    for info in members:
                        rel = info.filename[len("image-studio/"):]
                        if not rel or rel.startswith("/") or ".." in rel.split("/"):
                            continue
                        # frontera robusta: resolve() canoniza symlinks y relative_to no se deja engañar por prefijos
                        dest = (ROOT / rel).resolve()
                        try:
                            dest.relative_to(root_resolved)
                        except ValueError:
                            continue   # la ruta real cae fuera de ~/image-studio → se descarta
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
                        restored += 1
            return self._json({"ok": True, "restored": restored})
        finally:
            try:
                zp.unlink()
            except Exception:
                pass

    def h_itemsorder(self):
        # guarda el orden personalizado (arrastrable) de las imágenes del historial o del estante
        b = self._body()
        src = b.get("src", "history")
        pr = self._proj(b)
        sb = self._sub(b)
        order = [os.path.basename(str(f)) for f in (b.get("order") or []) if f]
        jp = pshelf_json(pr, sb) if src == "shelf" else phist_json(pr, sb)
        with LOCK:
            items = load_json(jp, [])
            pos = {f: i for i, f in enumerate(order)}
            items.sort(key=lambda it: pos.get(it.get("file"), len(order) + 1))  # estable: lo no listado va al final en su orden
            save_json(jp, items)
        return self._json({"ok": True})

    def h_projorder(self):
        # guarda el orden personalizado (arrastrable) de los proyectos = orden de claves en proyectos.json
        b = self._body()
        order = [str(k) for k in (b.get("order") or []) if k and not is_general(k)]
        with LOCK:
            raw = load_json(PROJ_JSON, {})
            new = {}
            if "general" in raw:
                new["general"] = raw["general"]      # General se inyecta aparte; su posición no afecta
            for k in order:
                if k in raw and k not in new:
                    new[k] = raw[k]
            for k, v in raw.items():                  # cualquier clave no listada va al final, en su orden
                if k not in new:
                    new[k] = v
            save_json(PROJ_JSON, new)
        return self._json({"ok": True})

    def h_subcreate(self):
        b = self._body()
        proj = b.get("project", "") or ""
        name = (b.get("name") or "").strip()
        if not name:
            return self._json({"error": "Falta el nombre del subproyecto"})
        key = safe(name)
        with LOCK:
            base = psub_base(proj, key)
            if base.exists():
                return self._json({"error": "Ya existe un subproyecto con ese nombre"})
            base.mkdir(parents=True, exist_ok=True)
            try:
                (base / "label.txt").write_text(name)
            except Exception:
                pass
        return self._json({"ok": True, "key": key, "label": name})

    def h_subrename(self):
        b = self._body()
        proj = b.get("project", "") or ""
        key = _sub_safe(b.get("key", ""))
        new = (b.get("new") or "").strip()
        if not key or not new:
            return self._json({"error": "Faltan datos"})
        with LOCK:
            base = psub_base(proj, key)
            if not base.is_dir():
                return self._json({"error": "Subproyecto no encontrado"})
            newkey = safe(new)
            if newkey != key:
                nb = psub_base(proj, newkey)
                if nb.exists():
                    return self._json({"error": "Ya existe un subproyecto con ese nombre"})
                base.rename(nb)
                base = nb
            try:
                (base / "label.txt").write_text(new)
            except Exception:
                pass
            try:   # reetiquetar metadatos del historial del sub
                jp = base / "historial.json"
                h = load_json(jp, [])
                for it in h:
                    it["project"] = proj
                    it["sub"] = newkey
                save_json(jp, h)
            except Exception:
                pass
        return self._json({"ok": True, "key": newkey, "label": new})

    def h_subdel(self):
        b = self._body()
        proj = b.get("project", "") or ""
        key = _sub_safe(b.get("key", ""))
        if not key:
            return self._json({"error": "Falta el subproyecto"})
        with LOCK:
            src = psub_base(proj, key)
            try:
                if src.is_dir():   # a la papelera (recuperable), no borrado definitivo
                    dest = TRASH_DIR / (uuid.uuid4().hex + "__sub__" + safe(proj) + "__" + key)
                    src.rename(dest)
            except Exception:
                try:
                    shutil.rmtree(src)
                except Exception:
                    pass
        return self._json({"ok": True})

    def h_subconvert(self):
        # CONVERTIR un proyecto existente en subproyecto de otro (migración: mueve la carpeta)
        b = self._body()
        src = (b.get("src") or "").strip()          # proyecto a convertir (no General)
        dest = b.get("dest", "") or ""              # proyecto destino (puede ser General)
        if is_general(src):
            return self._json({"error": "No se puede convertir «General»"})
        if proj_key(src) == proj_key(dest):
            return self._json({"error": "Destino inválido"})
        key = safe(src)
        srcf = PROJ_DIR / safe(src)
        if not srcf.is_dir() and src not in load_projects():
            return self._json({"error": "Proyecto origen no encontrado"})
        subf = srcf / "sub"
        if subf.is_dir() and any(p.is_dir() for p in subf.iterdir()):
            return self._json({"error": "Ese proyecto tiene subproyectos; vacíalos antes de convertirlo"})
        with LOCK:
            target = psub_base(dest, key)
            if target.exists():
                return self._json({"error": "Ya hay un subproyecto con ese nombre en el destino"})
            target.parent.mkdir(parents=True, exist_ok=True)
            if srcf.is_dir():
                srcf.rename(target)            # proyecto con contenido: mueve la carpeta
            else:
                target.mkdir(parents=True, exist_ok=True)   # proyecto vacío: solo crea el sub
            try:
                (target / "label.txt").write_text(src)
            except Exception:
                pass
            pr = load_json(PROJ_JSON, {})   # quitar el proyecto del índice
            pr.pop(proj_key(src), None)
            save_json(PROJ_JSON, pr)
            try:   # reetiquetar su historial
                jp = target / "historial.json"
                h = load_json(jp, [])
                for it in h:
                    it["project"] = dest
                    it["sub"] = key
                save_json(jp, h)
            except Exception:
                pass
        return self._json({"ok": True, "dest": dest, "key": key, "label": src})

    def h_subpromote(self):
        # SACAR un subproyecto y volverlo proyecto de primer nivel (mueve su carpeta)
        b = self._body()
        proj = b.get("project", "") or ""
        key = _sub_safe(b.get("key", ""))
        if not key:
            return self._json({"error": "Falta el subproyecto"})
        src = psub_base(proj, key)
        if not src.is_dir():
            return self._json({"error": "Subproyecto no encontrado"})
        label = ""
        lf = src / "label.txt"
        if lf.exists():
            try:
                label = lf.read_text().strip()
            except Exception:
                pass
        label = label or key
        with LOCK:
            pr = load_json(PROJ_JSON, {})
            base_label, n = label, 2
            while (PROJ_DIR / safe(label)).exists() or proj_key(label) in pr or is_general(label):
                label = f"{base_label} {n}"
                n += 1
            dest = PROJ_DIR / safe(label)
            src.rename(dest)                       # mueve la carpeta del sub a primer nivel
            pr[label] = {"style": "", "refs": []}  # registrar como proyecto
            save_json(PROJ_JSON, pr)
            try:
                (dest / "label.txt").unlink()      # ya no es subproyecto
            except Exception:
                pass
            try:                                   # reetiquetar su historial (raíz del nuevo proyecto)
                jp = dest / "historial.json"
                h = load_json(jp, [])
                for it in h:
                    it["project"] = label
                    it["sub"] = ""
                save_json(jp, h)
            except Exception:
                pass
        return self._json({"ok": True, "name": label})

    def h_config(self):
        b = self._body()
        with LOCK:
            return self._config_locked(b)

    def _config_locked(self, b):
        conf = load_json(CONF_JSON, {})
        prn = b.get("project", ACTIVE_PROJ)   # las carpetas son por proyecto
        sbn = b.get("sub", ACTIVE_SUB)        # el subproyecto solo afecta al effective mostrado
        pk = proj_key(prn)

        def _checkdir(raw):
            p = Path(os.path.expanduser(raw))
            p.mkdir(parents=True, exist_ok=True)
            t = p / ".studio_test"
            t.write_text("")
            t.unlink()

        if "save_dir" in b:
            raw = (b.get("save_dir") or "").strip()
            if raw:
                try:
                    _checkdir(raw)
                except Exception as e:
                    return self._json({"error": f"No puedo escribir en esa carpeta: {e}"})
            m = conf.setdefault("save_dirs", {})
            (m.__setitem__(pk, raw) if raw else m.pop(pk, None))
        if "shelf_dir" in b:
            raw = (b.get("shelf_dir") or "").strip()
            if raw:
                try:
                    _checkdir(raw)
                except Exception as e:
                    return self._json({"error": f"No puedo escribir en esa carpeta: {e}"})
            m = conf.setdefault("shelf_dirs", {})
            (m.__setitem__(pk, raw) if raw else m.pop(pk, None))
        if "voice_styles" in b and isinstance(b["voice_styles"], list):
            conf["voice_styles"] = b["voice_styles"][:50]
        save_json(CONF_JSON, conf)
        return self._json({"ok": True, "effective": str(save_dir_sub(prn, sbn)).replace(str(HOME), "~"),
                           "shelf_effective": str(shelf_dir_sub(prn, sbn)).replace(str(HOME), "~")})

    def h_historydel(self):
        b = self._body()
        f = os.path.basename(b.get("file", ""))
        if not f:
            return self._json({"error": "Falta el archivo"})
        pr = self._proj(b)
        sb = self._sub(b)
        with LOCK:
            jp = phist_json(pr, sb)
            h = load_json(jp, [])
            entry = next((x for x in h if x.get("file") == f), None)
            token = trash_put(phist_dir(pr, sb) / f, "history", pr, sb, entry)   # a la papelera (deshacer), dentro del lock
            save_json(jp, [x for x in h if x.get("file") != f])
        return self._json({"ok": True, "token": token, "entry": entry})

    def h_shelf_add(self):
        b = self._body()
        imgs = b.get("images", [])
        if not imgs:
            return self._json({"error": "Sin imágenes"})
        pr = self._proj(b)
        sb = self._sub(b)
        sdir = pshelf_dir(pr, sb)
        ext_dir = shelf_dir_sub(pr, sb)   # carpeta externa configurable de este proyecto/sub (o la interna)
        mirror = ext_dir.resolve() != sdir.resolve()
        skipped = []
        with LOCK:
            sj = pshelf_json(pr, sb)
            items = load_json(sj, [])
            for im in imgs[:IMG_MAX_COUNT]:
                try:
                    raw = base64.b64decode(im.get("b64", ""))
                except Exception:
                    continue
                if not raw:
                    continue
                nm = im.get("name", "") or ""
                ext = sniff_image(raw)   # validamos por contenido, no por nombre
                if not ext:
                    skipped.append(nm or "imagen")
                    continue
                fn = f"shelf_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}.{ext}"
                (sdir / fn).write_bytes(raw)   # copia interna: el estante siempre funciona
                if mirror:
                    try:
                        ext_dir.mkdir(parents=True, exist_ok=True)
                        (ext_dir / fn).write_bytes(raw)
                    except Exception:
                        pass
                items.insert(0, {"file": fn, "name": nm or fn, "ts": time.strftime("%Y-%m-%d %H:%M")})
            save_json(sj, items)
        return self._json({"ok": True, "items": items, "skipped": skipped,
                           "dir": str(ext_dir).replace(str(HOME), "~")})

    def h_shelf_del(self):
        b = self._body()
        f = os.path.basename(b.get("file", ""))
        if not f:
            return self._json({"error": "Falta el archivo"})
        pr = self._proj(b)
        sb = self._sub(b)
        with LOCK:
            sj = pshelf_json(pr, sb)
            items = load_json(sj, [])
            entry = next((x for x in items if x.get("file") == f), None)
            token = trash_put(pshelf_dir(pr, sb) / f, "shelf", pr, sb, entry)   # papelera (deshacer), dentro del lock
            save_json(sj, [x for x in items if x.get("file") != f])
        return self._json({"ok": True, "token": token, "entry": entry})

    def h_moveitem(self):
        # mueve o COPIA una o varias imágenes (archivo interno + metadato) a otro proyecto, mismo tipo
        b = self._body()
        src = b.get("src", "history")
        files = b.get("files")
        if not files:
            one = b.get("file", "")
            files = [one] if one else []
        files = [os.path.basename(x) for x in files if x]
        srcp = b.get("project", "") or ""
        dest = b.get("dest", "") or ""
        src_sub = b.get("sub", "") or ""
        dest_sub = b.get("dest_sub", "") or ""
        mode = b.get("mode", "move")
        if mode not in ("move", "copy"):
            mode = "move"
        dest_src = b.get("dest_src") or src   # tipo de destino: 'history' o 'shelf' (por defecto, el mismo del origen)
        if dest_src not in ("history", "shelf"):
            dest_src = src
        if not files:
            return self._json({"error": "Faltan archivos"})
        if src not in ("history", "shelf"):
            return self._json({"error": "Origen inválido"})
        if src == dest_src and proj_key(srcp) == proj_key(dest) and _sub_safe(src_sub) == _sub_safe(dest_sub):
            return self._json({"ok": True, "done": 0, "mode": mode, "dest": dest, "dest_sub": dest_sub, "errors": [], "pairs": []})
        done = 0
        errors = []
        with LOCK:
            if src == "history":
                sj, sdir = phist_json(srcp, src_sub), phist_dir(srcp, src_sub)
            else:
                sj, sdir = pshelf_json(srcp, src_sub), pshelf_dir(srcp, src_sub)
            if dest_src == "history":
                dj, ddir, ext_dest = phist_json(dest, dest_sub), phist_dir(dest, dest_sub), save_dir_sub(dest, dest_sub)
            else:
                dj, ddir, ext_dest = pshelf_json(dest, dest_sub), pshelf_dir(dest, dest_sub), shelf_dir_sub(dest, dest_sub)
            items = load_json(sj, [])
            ditems = load_json(dj, [])
            moved_files = set()
            pairs = []   # origen→destino, para deshacer
            for f in files:
                entry = next((x for x in items if x.get("file") == f), None)
                if entry is None:
                    errors.append(f)
                    continue
                target = f                       # evita colisión de nombre en el destino
                if (ddir / target).exists():
                    stem, ext = os.path.splitext(f)
                    target = f"{stem}_{uuid.uuid4().hex[:6]}{ext}"
                raw = None
                srcfile = sdir / f
                try:
                    if srcfile.is_file():
                        raw = srcfile.read_bytes()
                        (ddir / target).write_bytes(raw)
                        if mode == "move":
                            srcfile.unlink()
                except Exception:
                    errors.append(f)
                    continue
                try:                              # espeja en la carpeta externa del destino
                    if raw is not None and ext_dest.resolve() != ddir.resolve():
                        ext_dest.mkdir(parents=True, exist_ok=True)
                        (ext_dest / target).write_bytes(raw)
                except Exception:
                    pass
                if dest_src == "history":
                    new_entry = dict(entry)
                    new_entry["file"] = target
                    new_entry["project"] = dest
                    new_entry["sub"] = dest_sub
                else:   # destino = Mis imágenes (estante): entrada con forma de estante
                    nm = entry.get("name") or (str(entry.get("prompt", "")).strip()[:60]) or f
                    new_entry = {"file": target, "name": nm, "ts": time.strftime("%Y-%m-%d %H:%M")}
                ditems.insert(0, new_entry)
                pairs.append({"from": f, "to": target})
                if mode == "move":
                    moved_files.add(f)
                done += 1
            if mode == "move" and moved_files:
                save_json(sj, [x for x in items if x.get("file") not in moved_files])
            if done:
                save_json(dj, ditems)
        return self._json({"ok": True, "done": done, "mode": mode, "dest": dest, "dest_sub": dest_sub, "errors": errors, "pairs": pairs})

    def h_deleteitems(self):
        # borra en lote del historial o estante (solo la copia interna; las copias externas se conservan)
        b = self._body()
        src = b.get("src", "history")
        files = b.get("files")
        if not files:
            one = b.get("file", "")
            files = [one] if one else []
        files = [os.path.basename(x) for x in files if x]
        pr = b.get("project", "") or ""
        sb = b.get("sub", "") or ""
        if not files:
            return self._json({"error": "Faltan archivos"})
        if src not in ("history", "shelf"):
            return self._json({"error": "Origen inválido"})
        undo = []   # para deshacer: token de papelera + metadato por archivo
        with LOCK:
            if src == "history":
                jp, d = phist_json(pr, sb), phist_dir(pr, sb)
            else:
                jp, d = pshelf_json(pr, sb), pshelf_dir(pr, sb)
            items = load_json(jp, [])
            fset = set(files)
            for x in items:
                if x.get("file") in fset:
                    tok = trash_put(d / x.get("file", ""), src, pr, sb, x)   # a la papelera (deshacer), dentro del lock
                    undo.append({"entry": x, "token": tok})
            save_json(jp, [x for x in items if x.get("file") not in fset])
        return self._json({"ok": True, "done": len(undo), "undo": undo})

    def h_restoreitems(self):
        # deshacer un borrado: devuelve los archivos de la papelera y reinserta los metadatos
        b = self._body()
        src = b.get("src", "history")
        pr = b.get("project", "") or ""
        sb = b.get("sub", "") or ""
        ud = b.get("items") or []
        if src not in ("history", "shelf"):
            return self._json({"error": "Origen inválido"})
        restored = 0
        with LOCK:
            if src == "history":
                jp, d = phist_json(pr, sb), phist_dir(pr, sb)
            else:
                jp, d = pshelf_json(pr, sb), pshelf_dir(pr, sb)
            items = load_json(jp, [])
            have = set(x.get("file") for x in items)
            for u in ud:
                entry = u.get("entry") or {}
                f = entry.get("file", "")
                if not f or os.path.basename(f) != f or f.startswith("."):
                    continue   # rechaza separadores de ruta y '..' (path traversal)
                trash_restore(u.get("token", ""), d / f)
                if (d / f).is_file() and f not in have:
                    items.insert(0, entry)
                    have.add(f)
                    restored += 1
            save_json(jp, items)
        return self._json({"ok": True, "restored": restored})

    def h_trashrestore(self):
        # restaura un elemento desde la Papelera (usa el índice para saber su origen)
        tok = os.path.basename(self._body().get("token") or "")
        if not tok:
            return self._json({"error": "Falta el token"})
        rec = next((r for r in load_json(TRASH_INDEX, []) if r.get("token") == tok), None)
        if not rec:
            return self._json({"error": "No está en la papelera"})
        kind = rec.get("kind", "history")
        pr = rec.get("project", "") or ""
        sb = rec.get("sub", "") or ""
        entry = rec.get("entry") or {}
        f = os.path.basename(entry.get("file", "") or (tok.split("__", 1)[1] if "__" in tok else tok))
        if not f or os.path.basename(f) != f or f.startswith("."):
            return self._json({"error": "Nombre inválido"})
        with LOCK:
            jp, d = (pshelf_json(pr, sb), pshelf_dir(pr, sb)) if kind == "shelf" else (phist_json(pr, sb), phist_dir(pr, sb))
            ok = trash_restore(tok, d / f)
            if ok:
                items = load_json(jp, [])
                if not any(x.get("file") == f for x in items):
                    items.insert(0, entry if entry.get("file") else {"file": f})
                    save_json(jp, items)
        return self._json({"ok": True}) if ok else self._json({"error": "No se pudo restaurar"})

    def h_trashdelete(self):
        # borra definitivamente uno (token) o vacía toda la papelera (all)
        b = self._body()
        if b.get("all"):
            try:
                for p in TRASH_DIR.iterdir():
                    if p.name == "_index.json":
                        continue
                    try:
                        p.unlink() if p.is_file() else shutil.rmtree(p, ignore_errors=True)
                    except Exception:
                        pass
            except Exception:
                pass
            with TRASH_LOCK:
                save_json(TRASH_INDEX, [])
            return self._json({"ok": True})
        tok = os.path.basename(b.get("token") or "")
        if not tok:
            return self._json({"error": "Falta el token"})
        fp = TRASH_DIR / tok
        if fp.exists() and TRASH_DIR.resolve() == fp.resolve().parent:
            try:
                fp.unlink() if fp.is_file() else shutil.rmtree(fp, ignore_errors=True)
            except Exception:
                pass
        _trash_index_remove(tok)
        return self._json({"ok": True})

    def h_shelfcopyall(self):
        # copia TODAS las imágenes del historial a Mis imágenes del MISMO ámbito.
        # sub -> ese subproyecto; sin sub y allsubs -> raíz + todos los subs; sin sub -> solo raíz.
        b = self._body()
        pr = b.get("project", "") or ""
        sub = b.get("sub", "") or ""
        if sub:
            scopes = [sub]
        elif b.get("allsubs"):
            scopes = [""] + [s["key"] for s in list_subs(pr)]
        else:
            scopes = [""]
        added = 0
        created = []   # {sub, file} de cada copia, para deshacer
        with LOCK:
            for sk in scopes:
                hist = load_json(phist_json(pr, sk), [])
                if not hist:
                    continue
                hdir = phist_dir(pr, sk)
                sdir = pshelf_dir(pr, sk)
                sj = pshelf_json(pr, sk)
                ext = shelf_dir_sub(pr, sk)
                mirror = ext.resolve() != sdir.resolve()
                items = load_json(sj, [])
                already = set(x.get("name", "") for x in items)   # rápido: por nombre de origen
                have_hashes = set()                                # robusto: por CONTENIDO (cubre renombrados al mover)
                for x in items:
                    p = sdir / x.get("file", "")
                    if p.is_file():
                        try:
                            have_hashes.add(hashlib.md5(p.read_bytes()).hexdigest())
                        except Exception:
                            pass
                for it in hist:
                    f = it.get("file", "")
                    if not f or it.get("kind") in ("tts", "stt", "sfx", "vid") or f in already:
                        continue
                    src = hdir / f
                    if not src.is_file():
                        continue
                    raw = src.read_bytes()
                    ie = sniff_image(raw)
                    if not ie:
                        continue
                    h = hashlib.md5(raw).hexdigest()
                    if h in have_hashes:
                        continue   # ya está en el estante aunque con otro nombre (tras moverlo)
                    have_hashes.add(h)
                    fn = f"shelf_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}.{ie}"
                    (sdir / fn).write_bytes(raw)
                    if mirror:
                        try:
                            ext.mkdir(parents=True, exist_ok=True)
                            (ext / fn).write_bytes(raw)
                        except Exception:
                            pass
                    items.insert(0, {"file": fn, "name": f, "ts": time.strftime("%Y-%m-%d %H:%M")})
                    created.append({"sub": sk, "file": fn})
                    added += 1
                save_json(sj, items)
        return self._json({"ok": True, "added": added, "created": created})

    def _style_prefix(self, project):
        if not project:
            return ""
        st = load_projects().get(project, {}).get("style", "")
        return (st.strip() + "\n\n") if st.strip() else ""

    def _persist_refs(self, proj, sub, items):
        # guarda las referencias usadas en _refs/ (deduplicadas por contenido); devuelve [{file,name}]
        if not items:
            return []
        out = []
        with LOCK:
            rdir = phist_dir(proj, sub) / "_refs"
            rdir.mkdir(parents=True, exist_ok=True)
            for name, raw in items:
                ie = sniff_image(raw)
                if not ie:
                    continue
                fn = hashlib.md5(raw).hexdigest() + "." + ie
                fp = rdir / fn
                if not fp.exists():
                    fp.write_bytes(raw)
                out.append({"file": fn, "name": name})
        return out

    def _save_results(self, data, meta, via_visual=False, model_used="gpt-image-2"):
        ext = meta.get("output_format", "png")
        mime = "image/" + ("jpeg" if ext == "jpeg" else ext)
        u = data.get("usage", {})
        out_t = u.get("output_tokens", 0) or 0
        in_t = u.get("input_tokens", 0) or 0
        # OpenAI cobra distinto cada tipo de entrada: texto $5, imagen $8, imagen
        # cacheada $2 (por 1M). El desglose viene en input_tokens_details.
        det = u.get("input_tokens_details", {}) or {}
        txt_t = det.get("text_tokens")
        img_t = det.get("image_tokens")
        cached_t = det.get("cached_tokens", 0) or 0
        if txt_t is None and img_t is None:
            txt_t, img_t = in_t, 0  # sin desglose: asumimos todo texto
            in_cost = in_t * PRICE_IN / 1e6
        else:
            txt_t = txt_t or 0
            img_t = img_t or 0
            img_cached = min(cached_t, img_t)
            img_fresh = img_t - img_cached
            in_cost = (txt_t * PRICE_IN
                       + img_fresh * PRICE_IN_IMG
                       + img_cached * PRICE_IN_IMG_CACHED) / 1e6
        out_cost = out_t * PRICE_OUT / 1e6
        total = round(out_cost + in_cost, 5)
        items = data.get("data", [])
        per = round(total / max(1, len(items)), 7)
        images = []
        for d in items:
            b64 = d["b64_json"]
            raw = base64.b64decode(b64)
            if ext == "png":
                raw = png_meta(raw, [("prompt", meta.get("prompt", "")),
                                     ("studio", json.dumps({"size": meta.get("size"), "quality": meta.get("quality"), "mode": meta.get("mode")}, ensure_ascii=False))])
                b64 = base64.b64encode(raw).decode()
            name = f"img_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.{ext}"
            (phist_dir(meta.get("project", ""), meta.get("sub", "")) / name).write_bytes(raw)
            if meta.get("save_desktop", True):
                try:
                    dst = save_dir_sub(meta.get("project", ""), meta.get("sub", ""))
                    dst.mkdir(parents=True, exist_ok=True)
                    (dst / name).write_bytes(raw)
                except Exception:
                    pass
            add_history({"file": name, "prompt": meta["prompt"], "size": meta["size"],
                         "quality": meta["quality"], "mode": meta["mode"], "cost": per,
                         "output_tokens": out_t, "ts": time.strftime("%Y-%m-%d %H:%M"),
                         "project": meta.get("project", ""), "sub": meta.get("sub", ""),
                         "refs": meta.get("ref_files", [])})
            images.append({"image": f"data:{mime};base64," + b64, "file": name, "cost": per})
        first = images[0]["image"] if images else ""
        return {"images": images, "image": first, "cost": total, "output_tokens": out_t,
                "out_cost": round(out_cost, 5), "in_cost": round(in_cost, 5),
                "in_text_tokens": txt_t, "in_img_tokens": img_t,
                "via_visual": via_visual, "model_used": model_used}

    def h_generate(self):
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API (botón API)."})
        model = "gpt-image-2"
        size = b.get("size", "1536x1024")
        fmt = b.get("output_format", "png")
        quality = b.get("quality", "auto")
        prompt = self._style_prefix(b.get("project")) + b.get("prompt", "")
        n = int(b.get("n", 1))
        partial = max(0, min(3, int(b.get("partial_images") or 0)))
        payload = {"model": model, "prompt": prompt, "size": size, "quality": quality,
                   "n": n, "output_format": fmt, "moderation": b.get("moderation", "low")}
        if b.get("output_compression") is not None and fmt != "png":
            payload["output_compression"] = b["output_compression"]
        meta = {"prompt": b.get("prompt", ""), "size": size, "quality": quality,
                "mode": "crear", "output_format": fmt, "project": b.get("project", ""),
                "sub": b.get("sub", ""), "save_desktop": b.get("save_desktop", True)}
        hdr = {"Authorization": f"Bearer {key()}", "Content-Type": "application/json"}
        if partial > 0 and n == 1:   # preview en vivo (imágenes parciales por streaming)
            payload["stream"] = True
            payload["partial_images"] = partial
            self._stream_open()
            try:
                req = urllib.request.Request(API_GEN, data=json.dumps(payload).encode(), headers={**hdr, "Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    self._pump_sse(r, meta, model)
            except Exception as e:
                self._stream_err(e)
            return
        try:
            with urllib.request.urlopen(urllib.request.Request(API_GEN, data=json.dumps(payload).encode(),
                    headers=hdr), timeout=300) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except Exception as e:
            return self._json({"error": self._conn_msg(e)})
        return self._json(self._save_results(data, meta, model_used=model))

    def h_edit(self):
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API (botón API)."})
        model = "gpt-image-2"
        size = b.get("size", "1024x1024")
        fmt = b.get("output_format", "png")
        quality = b.get("quality", "auto")
        prompt = self._style_prefix(b.get("project")) + b.get("prompt", "")
        boundary = "----studio" + uuid.uuid4().hex
        parts = []

        def field(n, v):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode())

        def filepart(n, fn, raw):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"; filename="{safe_fn(fn)}"\r\nContent-Type: image/png\r\n\r\n'.encode() + raw + b"\r\n")

        field("model", model)
        field("prompt", prompt)
        field("size", size)
        field("quality", quality)
        field("n", str(b.get("n", 1)))
        field("output_format", fmt)
        field("moderation", b.get("moderation", "low"))
        if b.get("output_compression") is not None and fmt != "png":
            field("output_compression", str(b["output_compression"]))
        nimg = 0
        total_bytes = 0
        ref_saves = []   # referencias que el usuario añadió → se guardan con la imagen
        for img in b.get("images", []):
            raw = base64.b64decode(img["b64"])
            if not sniff_image(raw):
                return self._json({"error": f"'{img.get('name','imagen')}' no es PNG/JPEG/WebP/GIF (formatos que acepta OpenAI)."})
            total_bytes += len(raw)
            filepart("image[]", img.get("name", "ref.png"), raw)
            nimg += 1
            ref_saves.append((img.get("name", "ref.png"), raw))
        via_visual = False
        if b.get("use_project_refs"):  # "" = espacio General, también válido
            proj = b.get("project") or ""
            for f in load_projects().get(proj, {}).get("refs", []):
                fp = proj_folder(proj) / f
                if fp.exists():
                    raw = fp.read_bytes()
                    total_bytes += len(raw)
                    filepart("image[]", f, raw)
                    nimg += 1
                    via_visual = True
        if nimg == 0:
            return self._json({"error": "No hay imágenes de referencia."})
        if nimg > IMG_MAX_COUNT:
            return self._json({"error": f"Demasiadas referencias ({nimg}); OpenAI permite hasta {IMG_MAX_COUNT} por petición."})
        if total_bytes > IMG_MAX_PAYLOAD:
            return self._json({"error": f"Las referencias pesan {total_bytes/1048576:.0f} MB; el máximo de OpenAI es 512 MB por petición."})
        if b.get("mask"):
            filepart("mask", b["mask"].get("name", "mask.png"), base64.b64decode(b["mask"]["b64"]))
        meta = {"prompt": b.get("prompt", ""), "size": size, "quality": b.get("quality", "auto"),
                "mode": "editar", "output_format": fmt, "project": b.get("project", ""),
                "sub": b.get("sub", ""), "save_desktop": b.get("save_desktop", True),
                "ref_files": self._persist_refs(b.get("project", ""), b.get("sub", ""), ref_saves)}
        partial = max(0, min(3, int(b.get("partial_images") or 0)))
        stream = partial > 0 and int(b.get("n", 1)) == 1
        if stream:
            field("stream", "true")
            field("partial_images", str(partial))
        parts.append(f"--{boundary}--\r\n".encode())
        hdr = {"Authorization": f"Bearer {key()}", "Content-Type": f"multipart/form-data; boundary={boundary}"}
        if stream:   # preview en vivo (imágenes parciales por streaming)
            self._stream_open()
            try:
                req = urllib.request.Request(API_EDIT, data=b"".join(parts), headers={**hdr, "Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    self._pump_sse(r, meta, model)
            except Exception as e:
                self._stream_err(e)
            return
        try:
            with urllib.request.urlopen(urllib.request.Request(API_EDIT, data=b"".join(parts), headers=hdr), timeout=300) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except Exception as e:
            return self._json({"error": self._conn_msg(e)})
        return self._json(self._save_results(data, meta, via_visual, model))

    def h_speech(self):
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API (botón API)."})
        text = (b.get("input") or "").strip()
        if not text:
            return self._json({"error": "Escribe el texto a convertir en voz."})
        model = b.get("model", "gpt-4o-mini-tts")
        fmt = b.get("format", "mp3")
        voice = b.get("voice", "alloy")
        payload = {"model": model, "input": text, "voice": voice, "response_format": fmt}
        if model == "gpt-4o-mini-tts" and (b.get("instructions") or "").strip():
            payload["instructions"] = b["instructions"].strip()
        if model in TTS_PRICE and b.get("speed") and float(b["speed"]) != 1:
            payload["speed"] = float(b["speed"])
        try:
            with urllib.request.urlopen(urllib.request.Request(API_SPEECH, data=json.dumps(payload).encode(),
                    headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"}), timeout=300) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
        chars = len(text)
        # tts-1/hd cobran por carácter; gpt-4o-mini-tts por tokens de audio (~$0.015/min, ~950 chars/min)
        cost = round(chars * TTS_PRICE[model] / 1e6, 5) if model in TTS_PRICE else round(chars / 950 * 0.015, 5)
        data_url = f"data:{MIME.get(fmt, 'audio/mpeg')};base64," + base64.b64encode(raw).decode()
        if b.get("preview"):
            return self._json({"audio": data_url, "cost": cost})
        name = f"voz_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.{fmt}"
        (phist_dir(b.get("project", ""), b.get("sub", "")) / name).write_bytes(raw)
        if b.get("save_desktop", True):
            try:
                d = save_dir_sub(b.get("project", ""), b.get("sub", ""))
                d.mkdir(parents=True, exist_ok=True)
                (d / name).write_bytes(raw)
            except Exception:
                pass
        add_history({"file": name, "kind": "tts", "prompt": text[:160], "voice": voice, "model": model,
                     "size": fmt, "quality": "", "mode": "audio", "cost": cost, "output_tokens": 0,
                     "ts": time.strftime("%Y-%m-%d %H:%M"), "project": b.get("project", ""), "sub": b.get("sub", "")})
        return self._json({"file": name, "audio": data_url, "cost": cost})

    def h_transcribe(self):
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API (botón API)."})
        if not b.get("b64"):
            return self._json({"error": "Sube o arrastra un audio primero."})
        translate = bool(b.get("translate"))
        fmt = b.get("response_format", "text")
        model = b.get("model", "gpt-4o-mini-transcribe")
        if translate or fmt in ("srt", "vtt", "verbose_json"):
            model = "whisper-1"
        boundary = "----studio" + uuid.uuid4().hex
        parts = []

        def field(n, v):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode())

        field("model", model)
        field("response_format", fmt)
        if b.get("language") and not translate:
            field("language", b["language"])
        if (b.get("prompt") or "").strip():
            field("prompt", b["prompt"].strip())
        if b.get("temperature"):
            field("temperature", str(b["temperature"]))
        if fmt == "verbose_json":
            field("timestamp_granularities[]", "segment")
        fn = safe_fn(b.get("name", "audio.mp3"))
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                     + base64.b64decode(b["b64"]) + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        url = API_TRANSL if translate else API_TRANSC
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=b"".join(parts),
                    headers={"Authorization": f"Bearer {key()}", "Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=600) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
        text = raw
        if fmt in ("json", "verbose_json"):
            try:
                text = json.loads(raw).get("text", raw)
            except Exception:
                pass
        dur = float(b.get("duration") or 0)  # minutos, medido en el cliente
        cost = round(dur * STT_PRICE.get(model, 0.006), 5)
        ext = {"text": "txt", "json": "json", "verbose_json": "json", "srt": "srt", "vtt": "vtt"}.get(fmt, "txt")
        name = f"tx_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.{ext}"
        (phist_dir(b.get("project", ""), b.get("sub", "")) / name).write_text(raw)
        if b.get("save_desktop", True):
            try:
                d = save_dir_sub(b.get("project", ""), b.get("sub", ""))
                d.mkdir(parents=True, exist_ok=True)
                (d / name).write_text(raw)
            except Exception:
                pass
        add_history({"file": name, "kind": "stt", "prompt": (text or "")[:160], "model": model,
                     "size": ext, "quality": "", "mode": "audio", "cost": cost, "output_tokens": 0,
                     "ts": time.strftime("%Y-%m-%d %H:%M"), "project": b.get("project", ""), "sub": b.get("sub", "")})
        return self._json({"text": text if fmt in ("json", "verbose_json", "text") else raw,
                           "file": name, "cost": cost, "model_used": model})

    def _el_err(self, e):
        try:
            det = json.loads(e.read()).get("detail")
            if isinstance(det, dict):
                return det.get("message") or str(det)[:200]
            return str(det)[:200] if det else f"HTTP {e.code}"
        except Exception:
            return f"HTTP {e.code}"

    def _save_audio(self, raw, prefix, ext, hist_item, save_desktop):
        name = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.{ext}"
        (phist_dir(hist_item.get("project", ""), hist_item.get("sub", "")) / name).write_bytes(raw)
        if save_desktop:
            try:
                d = save_dir_sub(hist_item.get("project", ""), hist_item.get("sub", ""))
                d.mkdir(parents=True, exist_ok=True)
                (d / name).write_bytes(raw)
            except Exception:
                pass
        hist_item["file"] = name
        add_history(hist_item)
        return name

    def h_datasync(self):
        root = ROOT.resolve()
        if not (root / ".git").exists():
            return self._json({"error": "Esta carpeta no está configurada para sincronizar por git."})

        def git(*args, timeout=180):
            return subprocess.run(["git", "-C", str(root),
                                   "-c", "user.email=gio.park.4444@gmail.com", "-c", "user.name=Gio"] + list(args),
                                  capture_output=True, text=True, timeout=timeout)
        try:
            git("add", "-A")
            git("commit", "-m", "sync " + time.strftime("%Y-%m-%d %H:%M"))  # puede no haber cambios
            pull = git("pull", "--no-rebase", "--no-edit", "origin", "main")
            if pull.returncode != 0 and "CONFLICT" in (pull.stdout + pull.stderr):
                git("merge", "--abort")
                return self._json({"error": "Conflicto de sincronización: hay cambios distintos en los dos equipos. Descarga un respaldo .zip y avísame para resolverlo a mano."})
            push = git("push", "origin", "main")
            if push.returncode != 0:
                return self._json({"error": "No pude subir: " + (push.stderr or push.stdout).strip()[:200]})
        except subprocess.TimeoutExpired:
            return self._json({"error": "La sincronización tardó demasiado."})
        except Exception as e:
            return self._json({"error": str(e)})
        return self._json({"ok": True})

    def h_icloudsync(self):
        icl = icloud_dir()
        if not icl.exists():
            return self._json({"error": "iCloud Drive no está activo en este Mac (Ajustes → Apple ID → iCloud)."})
        if ROOT.is_symlink():
            return self._json({"ok": True, "note": "Ya estaba activa."})
        target = icl / "image-studio"
        try:
            if target.exists():
                bak = HOME / f"image-studio-backup-{time.strftime('%Y%m%d_%H%M')}"
                ROOT.rename(bak)
                note = f"iCloud ya tenía datos; los locales quedaron respaldados en {bak.name}/"
            else:
                shutil.move(str(ROOT), str(target))
                note = "Datos movidos a iCloud Drive."
            ROOT.symlink_to(target)
        except Exception as e:
            return self._json({"error": f"No pude activar la sincronización: {e}"})
        return self._json({"ok": True, "note": note})

    def h_elkey(self):
        k = (self._body().get("key") or "").strip()
        try:
            urllib.request.urlopen(urllib.request.Request(EL_API + "/user",
                headers={"xi-api-key": k}), timeout=20).read()
        except Exception:
            return self._json({"ok": False, "error": "La clave de ElevenLabs no es válida"})
        EL_KEY_FILE.write_text(k)
        try:
            os.chmod(EL_KEY_FILE, 0o600)
        except Exception:
            pass
        return self._json({"ok": True})

    def h_elspeech(self):
        b = self._body()
        if not el_key():
            return self._json({"error": "Conecta tu clave de ElevenLabs primero."})
        text = (b.get("input") or "").strip()
        if not text:
            return self._json({"error": "Escribe el texto a convertir en voz."})
        vid = b.get("voice_id")
        if not vid:
            return self._json({"error": "Elige una voz de ElevenLabs."})
        model = b.get("model_id", "eleven_multilingual_v2")
        vs = {"stability": float(b.get("stability", 0.5)),
              "similarity_boost": float(b.get("similarity", 0.75)),
              "style": float(b.get("style", 0)),
              "use_speaker_boost": bool(b.get("boost", True))}
        if b.get("speed") and float(b["speed"]) != 1:
            vs["speed"] = float(b["speed"])
        payload = {"text": text, "model_id": model, "voice_settings": vs}
        if str(b.get("seed") or "").strip().isdigit():
            payload["seed"] = int(b["seed"])
        if b.get("normalization") in ("on", "off"):
            payload["apply_text_normalization"] = b["normalization"]
        fmt = b.get("format", "mp3_44100_128")
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    f"{EL_API}/text-to-speech/{vid}?output_format={fmt}",
                    data=json.dumps(payload).encode(),
                    headers={"xi-api-key": el_key(), "Content-Type": "application/json"}), timeout=300) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            return self._json({"error": self._el_err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con ElevenLabs: {e.reason}"})
        credits = round(len(text) * (0.5 if ("flash" in model or "turbo" in model) else 1))
        ext = "mp3" if fmt.startswith("mp3") else "opus" if fmt.startswith("opus") else "pcm" if fmt.startswith("pcm") else "ulaw"
        mime = "audio/mpeg" if ext == "mp3" else "audio/ogg" if ext == "opus" else "application/octet-stream"
        data_url = f"data:{mime};base64," + base64.b64encode(raw).decode()
        if b.get("preview"):
            return self._json({"audio": data_url, "credits": credits})
        name = self._save_audio(raw, "el", ext,
            {"kind": "tts", "prompt": text[:160], "voice": b.get("voice_name", ""), "model": model,
             "size": ext, "quality": "", "mode": "audio", "cost": 0, "credits": credits, "output_tokens": 0,
             "ts": time.strftime("%Y-%m-%d %H:%M"), "project": b.get("project", ""), "sub": b.get("sub", "")},
            b.get("save_desktop", True))
        return self._json({"file": name, "audio": data_url, "credits": credits})

    def h_elsfx(self):
        b = self._body()
        if not el_key():
            return self._json({"error": "Conecta tu clave de ElevenLabs (pestaña Voz → ElevenLabs)."})
        text = (b.get("input") or "").strip()
        if not text:
            return self._json({"error": "Describe el efecto de sonido."})
        payload = {"text": text}
        if b.get("duration"):
            payload["duration_seconds"] = float(b["duration"])
        if b.get("influence") is not None:
            payload["prompt_influence"] = float(b["influence"])
        try:
            with urllib.request.urlopen(urllib.request.Request(EL_API + "/sound-generation",
                    data=json.dumps(payload).encode(),
                    headers={"xi-api-key": el_key(), "Content-Type": "application/json"}), timeout=300) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            return self._json({"error": self._el_err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con ElevenLabs: {e.reason}"})
        data_url = "data:audio/mpeg;base64," + base64.b64encode(raw).decode()
        name = self._save_audio(raw, "sfx", "mp3",
            {"kind": "sfx", "prompt": text[:160], "voice": "SFX", "model": "sound-generation",
             "size": "mp3", "quality": "", "mode": "audio", "cost": 0, "output_tokens": 0,
             "ts": time.strftime("%Y-%m-%d %H:%M"), "project": b.get("project", ""), "sub": b.get("sub", "")},
            b.get("save_desktop", True))
        return self._json({"file": name, "audio": data_url})

    def h_elclone(self):
        b = self._body()
        if not el_key():
            return self._json({"error": "Conecta tu clave de ElevenLabs primero."})
        name = (b.get("name") or "").strip()
        files = b.get("files") or []
        if not name or not files:
            return self._json({"error": "Pon un nombre y al menos una muestra de audio."})
        boundary = "----studio" + uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\n{name}\r\n'.encode()]
        if (b.get("description") or "").strip():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="description"\r\n\r\n{b["description"].strip()}\r\n'.encode())
        for f in files[:10]:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{safe_fn(f.get("name","muestra.mp3"))}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                         + base64.b64decode(f["b64"]) + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        try:
            with urllib.request.urlopen(urllib.request.Request(EL_API + "/voices/add",
                    data=b"".join(parts),
                    headers={"xi-api-key": el_key(),
                             "Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=300) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._json({"error": self._el_err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con ElevenLabs: {e.reason}"})
        return self._json({"ok": True, "voice_id": data.get("voice_id", "")})

    def _fal_req(self, url, data=None, timeout=60):
        return urllib.request.urlopen(urllib.request.Request(url,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": f"Key {fal_key()}", "Content-Type": "application/json"}), timeout=timeout)

    def _fal_err(self, e):
        try:
            d = json.loads(e.read())
            det = d.get("detail") or d.get("message") or d
            if isinstance(det, list) and det:
                det = det[0].get("msg", str(det[0])) if isinstance(det[0], dict) else str(det[0])
            return str(det)[:300]
        except Exception:
            return f"HTTP {e.code}"

    def h_falkey(self):
        k = (self._body().get("key") or "").strip()
        if not k:
            return self._json({"ok": False, "error": "Pega tu clave de fal.ai"})
        # una clave inválida devuelve 401; con clave válida, un id inexistente da 404/400/422
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{FAL_QUEUE}/bytedance/seedance-2.0/text-to-video/requests/00000000-0000-0000-0000-000000000000/status",
                headers={"Authorization": f"Key {k}"}), timeout=20).read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return self._json({"ok": False, "error": "La clave de fal.ai no es válida"})
        except Exception:
            return self._json({"ok": False, "error": "No pude validar la clave (sin conexión)"})
        FAL_KEY_FILE.write_text(k)
        try:
            os.chmod(FAL_KEY_FILE, 0o600)
        except Exception:
            pass
        return self._json({"ok": True})

    def _audio_b64(self, aud):
        """{b64} directo o {hist_file} del historial → (b64, mime) o None"""
        if aud and aud.get("hist_file"):
            fp = phist_dir(ACTIVE_PROJ, ACTIVE_SUB) / os.path.basename(aud["hist_file"])
            if not fp.is_file():
                fp = phist_dir(ACTIVE_PROJ) / os.path.basename(aud["hist_file"])
            if not fp.is_file():
                fp = HIST_DIR / os.path.basename(aud["hist_file"])
            if not fp.is_file():
                return None
            ext = fp.suffix.lstrip(".").lower()
            return base64.b64encode(fp.read_bytes()).decode(), MIME.get(ext, "audio/mpeg").split(";")[0]
        if aud and aud.get("b64"):
            return aud["b64"], "audio/mpeg"
        return None

    def h_video(self):
        b = self._body()
        if not fal_key():
            return self._json({"error": "Conecta tu clave de fal.ai primero."})
        model = b.get("model", "seedance")
        if model not in VIDEO_MODELS:
            return self._json({"error": "Modelo de video desconocido"})
        prompt = (b.get("prompt") or "").strip()
        use_mem = bool(b.get("use_memory"))  # "" = espacio General, también válido
        mproj = b.get("project") or ""
        if use_mem and not model.startswith("omnihuman"):
            p = load_projects().get(mproj, {})
            st = (p.get("style_video") or "").strip() or (p.get("style") or "").strip()
            if st and not prompt.startswith(st):
                prompt = st + "\n\n" + prompt

        if model.startswith("omnihuman"):
            img = b.get("image")
            if not img:
                return self._json({"error": "OmniHuman necesita la imagen de la persona."})
            a = self._audio_b64(b.get("audio"))
            if not a:
                return self._json({"error": "OmniHuman necesita un audio (súbelo o elige uno del historial)."})
            payload = {"image_url": "data:image/png;base64," + img["b64"],
                       "audio_url": f"data:{a[1]};base64," + a[0]}
            if model == "omnihuman":  # solo la 1.5 acepta prompt/turbo/resolución
                payload["resolution"] = b.get("resolution", "1080p")
                payload["turbo_mode"] = bool(b.get("turbo"))
                if prompt:
                    payload["prompt"] = prompt
            model_id = VIDEO_MODELS[model]["av"]

        elif model.startswith("seedance"):
            if not prompt:
                return self._json({"error": "Escribe el prompt del video."})
            payload = {"prompt": prompt, "generate_audio": bool(b.get("gen_audio", True)),
                       "resolution": b.get("resolution", "720p"),
                       "duration": str(b.get("duration", "auto")),
                       "aspect_ratio": b.get("aspect", "auto")}
            if str(b.get("seed") or "").strip().isdigit():
                payload["seed"] = int(b["seed"])
            imgs = list(b.get("images") or [])
            if use_mem:
                added = 0
                for f in load_projects().get(mproj, {}).get("refs", []):
                    if len(imgs) >= 9:
                        break
                    fp = proj_folder(mproj) / f
                    if fp.is_file():
                        imgs.append({"name": f, "b64": base64.b64encode(fp.read_bytes()).decode()})
                        added += 1
                if added:
                    prompt += "\n\nUsa las últimas " + (str(added) + " imágenes" if added > 1 else "imagen") + " como referencia de personajes y estilo del proyecto."
                    payload["prompt"] = prompt
            vids = b.get("videos") or []
            auds = b.get("audios") or []
            if len(imgs) > 9 or len(vids) > 3 or len(auds) > 3 or len(imgs) + len(vids) + len(auds) > 12:
                return self._json({"error": "Máximo 9 imágenes, 3 videos y 3 audios (12 archivos en total)."})
            if len(imgs) == 1 and not vids and not auds and not b.get("force_ref"):
                payload["image_url"] = "data:image/png;base64," + imgs[0]["b64"]
                if b.get("end_image"):
                    payload["end_image_url"] = "data:image/png;base64," + b["end_image"]["b64"]
                model_id = VIDEO_MODELS[model]["i2v"]
            elif imgs or vids or auds:
                if imgs:
                    payload["image_urls"] = ["data:image/png;base64," + i["b64"] for i in imgs]
                if vids:
                    payload["video_urls"] = ["data:video/mp4;base64," + v["b64"] for v in vids]
                if auds:
                    payload["audio_urls"] = ["data:audio/mpeg;base64," + a["b64"] for a in auds]
                model_id = VIDEO_MODELS[model]["r2v"]
            else:
                model_id = VIDEO_MODELS[model]["t2v"]

        else:  # kling pro/standard
            multi = b.get("multi_prompt") or []
            if not prompt and not multi:
                return self._json({"error": "Escribe el prompt del video (o tomas multi-prompt)."})
            d = str(b.get("duration", "5"))
            payload = {"duration": d if d.isdigit() else "5",
                       "generate_audio": bool(b.get("gen_audio", True)),
                       "aspect_ratio": b.get("aspect", "16:9"),
                       "shot_type": b.get("shot_type", "customize")}
            if multi:
                payload["multi_prompt"] = multi
            else:
                payload["prompt"] = prompt
            if (b.get("negative") or "").strip():
                payload["negative_prompt"] = b["negative"].strip()
            if b.get("cfg") is not None:
                payload["cfg_scale"] = float(b["cfg"])
            img = b.get("image")
            if img:
                payload["start_image_url"] = "data:image/png;base64," + img["b64"]
                if b.get("end_image"):
                    payload["end_image_url"] = "data:image/png;base64," + b["end_image"]["b64"]
                model_id = VIDEO_MODELS[model]["i2v"]
            else:
                model_id = VIDEO_MODELS[model]["t2v"]
        try:
            with self._fal_req(f"{FAL_QUEUE}/{model_id}", payload, timeout=120) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._json({"error": self._fal_err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con fal.ai: {e.reason}"})
        rid = data.get("request_id")
        if not rid:
            return self._json({"error": "fal.ai no devolvió un id de trabajo"})
        with LOCK:
            PENDING_VIDEOS[rid] = {"model_id": model_id,
                                   "meta": {"prompt": prompt or "avatar con audio", "model": model,
                                            "cost": float(b.get("cost_est") or 0),
                                            "project": b.get("project", ""),
                                            "sub": b.get("sub", ""),
                                            "save_desktop": b.get("save_desktop", True)}}
            save_jobs()
        return self._json({"id": rid})

    def h_videostatus(self):
        q = parse_qs(urlparse(self.path).query)
        rid = q.get("id", [""])[0]
        job = PENDING_VIDEOS.get(rid)
        if not job:
            return self._json({"error": "Trabajo desconocido (¿se reinició el server?)"})
        mid = job["model_id"]
        try:
            with self._fal_req(f"{FAL_QUEUE}/{mid}/requests/{rid}/status") as r:
                st = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._json({"error": self._fal_err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con fal.ai: {e.reason}"})
        status = st.get("status", "")
        if status in ("IN_QUEUE", "IN_PROGRESS"):
            return self._json({"done": False, "status": status, "queue": st.get("queue_position")})
        if status != "COMPLETED":
            with LOCK:
                PENDING_VIDEOS.pop(rid, None); save_jobs()
            return self._json({"error": f"El trabajo terminó con estado {status}"})
        try:
            with self._fal_req(f"{FAL_QUEUE}/{mid}/requests/{rid}") as r:
                res = json.loads(r.read())
            vurl = (res.get("video") or {}).get("url", "")
            if not vurl:
                raise ValueError("sin URL de video en la respuesta")
            with urllib.request.urlopen(vurl, timeout=600) as vr:
                raw = vr.read()
        except urllib.error.HTTPError as e:
            return self._json({"error": self._fal_err(e)})
        except Exception as e:
            return self._json({"error": f"No pude descargar el video: {e}"})
        m = job["meta"]
        cost = m.get("cost") or 0
        if model_dur := res.get("duration"):  # omnihuman factura por duración real
            cost = round(float(model_dur) * 0.14, 4)
        name = self._save_audio(raw, "vid", "mp4",
            {"kind": "vid", "prompt": m["prompt"][:160], "voice": "", "model": m["model"],
             "size": "mp4", "quality": "", "mode": "video", "cost": cost, "output_tokens": 0,
             "ts": time.strftime("%Y-%m-%d %H:%M"), "project": m.get("project", ""), "sub": m.get("sub", "")},
            m.get("save_desktop", True))
        with LOCK:
            PENDING_VIDEOS.pop(rid, None); save_jobs()
        return self._json({"done": True, "file": name, "url": "/file?name=" + name, "cost": cost})

    def h_histfav(self):
        b = self._body()
        f = os.path.basename(b.get("file", ""))
        jp = phist_json(self._proj(b), self._sub(b))
        with LOCK:
            h = load_json(jp, [])
            for it in h:
                if it.get("file") == f:
                    it["fav"] = bool(b.get("fav"))
            save_json(jp, h)
        return self._json({"ok": True})

    def h_imgcolors(self):
        # etiquetas de color (multi-color) de una imagen, en Historial o Mis imágenes
        b = self._body()
        f = os.path.basename(b.get("file", ""))
        allow = ("r", "y", "g", "b")
        seen, clean = set(), []
        for c in (b.get("colors") or []):
            if c in allow and c not in seen:
                seen.add(c); clean.append(c)
        scope = b.get("scope", "hist")
        jp = pshelf_json(self._proj(b), self._sub(b)) if scope == "shelf" else phist_json(self._proj(b), self._sub(b))
        with LOCK:
            h = load_json(jp, [])
            for it in h:
                if it.get("file") == f:
                    if clean:
                        it["colors"] = clean
                    else:
                        it.pop("colors", None)
            save_json(jp, h)
        return self._json({"ok": True})

    def h_setproject(self):
        global ACTIVE_PROJ, ACTIVE_SUB
        b = self._body()
        ACTIVE_PROJ = (b.get("project") or "").strip()
        sub = (b.get("sub") or "").strip()
        ACTIVE_SUB = sub if sub and any(s["key"] == sub for s in list_subs(ACTIVE_PROJ)) else ""
        return self._json({"ok": True, "project": ACTIVE_PROJ, "sub": ACTIVE_SUB})

    def h_promptlib(self):
        # guarda la biblioteca completa (la ventana la administra en memoria y persiste el objeto entero)
        b = self._body()
        cats = b.get("categories") if isinstance(b.get("categories"), list) else []
        items = b.get("items") if isinstance(b.get("items"), list) else []
        data = {"categories": list(cats), "items": [x for x in items if isinstance(x, dict)]}
        with LOCK:
            save_json(PROMPTS_JSON, data)
        return self._json({"ok": True})

    def h_promptstage(self):
        # la ventana "Biblioteca" envía un prompt compuesto para que el estudio lo cargue
        b = self._body()
        p = str(b.get("prompt", "") or "").strip()
        if not p:
            return self._json({"error": "prompt vacío"}, 400)
        with PROMPT_STAGE_LOCK:
            PROMPT_STAGE.append({"prompt": p})
            if len(PROMPT_STAGE) > 20:
                del PROMPT_STAGE[:-20]
        return self._json({"ok": True})

    def h_promptinbox(self):
        # el historial del estudio envía un prompt a la biblioteca (se apilan hasta que la biblioteca los recoge)
        b = self._body()
        p = str(b.get("prompt", "") or "").strip()
        if not p:
            return self._json({"error": "prompt vacío"}, 400)
        with PROMPT_INBOX_LOCK:
            PROMPT_INBOX.append({"prompt": p, "title": str(b.get("title", "") or "")})
            if len(PROMPT_INBOX) > 200:
                del PROMPT_INBOX[:-200]
        return self._json({"ok": True})

    def h_stage(self):
        # una ventana "Ver todo" deja una imagen para que el estudio la recoja como referencia
        b = self._body()
        src = "shelf" if b.get("src") == "shelf" else "history"
        f = os.path.basename(str(b.get("file", "")))
        if not f:
            return self._json({"error": "sin archivo"}, 400)
        pr = self._proj(b)
        sb = self._sub(b)
        base = pshelf_dir(pr, sb) if src == "shelf" else phist_dir(pr, sb)
        if not (base / f).is_file():
            return self._json({"error": "la imagen no existe"}, 404)
        with STAGE_LOCK:
            STAGE.append({"src": src, "file": f, "project": pr, "sub": sb})
            if len(STAGE) > 50:
                del STAGE[:-50]
        return self._json({"ok": True})

    def _chat(self, messages, max_tokens=400):
        payload = {"model": DISTILL_MODEL, "messages": messages, "max_tokens": max_tokens}
        with urllib.request.urlopen(urllib.request.Request(API_CHAT, data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"}), timeout=90) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"].strip()

    def h_detectsubjects(self):
        # Detecta personas/objetos + orientación (yaw/pitch) con visión gpt-4o (structured outputs).
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API de OpenAI (botón API)."})
        b64 = (b.get("b64") or "").strip()
        if not b64:
            return self._json({"error": "No recibí la imagen."})
        try:
            w = int(b.get("w") or 0); h = int(b.get("h") or 0)
        except Exception:
            w = h = 0
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["detections"],
            "properties": {"detections": {
                "type": "array",
                "description": "Una entrada por cada persona u objeto destacado y distinto que sea visible.",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["label", "category", "box", "facing"],
                    "properties": {
                        "label": {"type": "string", "description": "Sustantivo corto en español, p. ej. 'niña', 'hombre', 'auto', 'perro', 'silla'."},
                        "category": {"type": "string", "enum": ["person", "animal", "vehicle", "object"]},
                        "box": {"type": "array", "description": "Caja normalizada [x0,y0,x1,y1], origen arriba-izquierda, cada valor 0.0-1.0, x1>x0, y1>y0.", "items": {"type": "number"}},
                        "facing": {
                            "type": "object", "additionalProperties": False,
                            "required": ["yaw_label", "yaw_deg", "pitch_label", "pitch_deg"],
                            "properties": {
                                "yaw_label": {"type": "string", "enum": ["front", "front_left", "front_right", "left_profile", "right_profile", "back_left", "back_right", "back", "unknown"], "description": "Hacia dónde mira el frente del cuerpo/cara respecto a la cámara, en términos de la pantalla (left=hacia la izquierda de la imagen)."},
                                "yaw_deg": {"type": "number", "description": "Yaw aprox en grados. 0=de frente a cámara, +90=girado hacia la derecha de la imagen (perfil), 180=de espaldas, -90=hacia la izquierda. Rango -180..180."},
                                "pitch_label": {"type": "string", "enum": ["level", "looking_up", "looking_down", "unknown"]},
                                "pitch_deg": {"type": "number", "description": "Pitch aprox en grados. 0=nivel, positivo=mirando hacia arriba, negativo=hacia abajo. Rango -90..90."}
                            }
                        }
                    }
                }
            }}
        }
        sysmsg = ("Eres un motor de detección visual preciso. Analizas UNA imagen y devuelves cada persona y objeto "
                  "destacado distinto. El sistema de coordenadas tiene el ORIGEN (0,0) en la esquina SUPERIOR-IZQUIERDA, "
                  "x aumenta hacia la derecha, y aumenta hacia abajo; x e y van de 0.0 a 1.0 relativos al ancho/alto. "
                  "Las cajas son [x0,y0,x1,y1] = esquina superior-izquierda y luego inferior-derecha, con x1>x0 y y1>y0. "
                  "Estima la orientación (facing) a partir de la cabeza y el cuerpo visibles. Si no estás seguro de la "
                  "orientación, usa 'unknown'. Devuelve SOLO datos conformes al esquema; no inventes objetos que no se ven.")
        usertext = (("La imagen mide %d de ancho por %d de alto (px). " % (w, h) if (w and h) else "")
                    + "Detecta cada persona y cada objeto destacado. Para cada uno da una caja ajustada normalizada "
                      "[x0,y0,x1,y1] (origen arriba-izquierda), una etiqueta corta, la categoría y la orientación (facing).")
        payload = {"model": DETECT_MODEL, "temperature": 0, "max_tokens": 1600,
                   "messages": [
                       {"role": "system", "content": sysmsg},
                       {"role": "user", "content": [
                           {"type": "text", "text": usertext},
                           {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64, "detail": "high"}}]}],
                   "response_format": {"type": "json_schema", "json_schema": {"name": "detections", "strict": True, "schema": schema}}}
        try:
            with urllib.request.urlopen(urllib.request.Request(API_CHAT, data=json.dumps(payload).encode(),
                    headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"}), timeout=90) as r:
                data = json.loads(r.read())
            content = data["choices"][0]["message"]["content"]
            out = json.loads(content)
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
        except Exception as e:
            return self._json({"error": f"No pude leer la detección: {e}"})
        dets = out.get("detections", []) if isinstance(out, dict) else []
        return self._json({"detections": dets})

    def h_magicprompt(self):
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API de OpenAI (botón API)."})
        p = (b.get("prompt") or "").strip()
        if not p:
            return self._json({"error": "Escribe primero un prompt para mejorarlo."})
        video = b.get("mode") == "video"
        sys = ("Eres director de fotografía y experto en prompts. Reescribe el prompt del usuario como un prompt "
               "rico y detallado para un modelo de generación de "
               + ("video: añade movimiento de cámara, ritmo, iluminación, lente, atmósfera y estilo"
                  if video else
                  "imágenes: añade composición, iluminación, lente, paleta, atmósfera y estilo")
               + ". Conserva el idioma y la intención original. Devuelve SOLO el prompt mejorado, sin comillas ni explicaciones, máximo 120 palabras.")
        try:
            out = self._chat([{"role": "system", "content": sys}, {"role": "user", "content": p}])
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
        return self._json({"prompt": out})

    def h_describe(self):
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API de OpenAI (botón API)."})
        f = os.path.basename(b.get("file", ""))
        pr = self._proj(b)
        sb = self._sub(b)
        fp = phist_dir(pr, sb) / f              # imágenes del historial...
        if not fp.is_file():
            fp = pshelf_dir(pr, sb) / f         # ...o del estante (Mis imágenes)
        if not fp.is_file():
            return self._json({"error": "No encuentro esa imagen."})
        # detalle de visión: high = lectura fiel (gpt-4o-mini soporta low/high/auto, no 'original')
        detail = b.get("detail", "high")
        if detail not in ("low", "high", "auto"):
            detail = "high"
        mime = MIME.get(fp.suffix.lstrip(".").lower(), "image/png").split(";")[0]
        uri = f"data:{mime};base64," + base64.b64encode(fp.read_bytes()).decode()
        sysmsg = ("Eres director de arte y analista visual experto. Te dan UNA imagen y devuelves un PROMPT en español "
                  "para recrearla con un modelo de generación de imágenes. Lo MÁS IMPORTANTE es el CONCEPTO VISUAL: "
                  "EMPIEZA SIEMPRE por eso y solo DESPUÉS describe el contenido. Estructura así, en prosa fluida (sin viñetas ni encabezados):\n"
                  "1) CONCEPTO VISUAL primero: estilo (p. ej. ilustración digital, acuarela, óleo, gouache, concept art, anime/manga, render 3D, "
                  "fotorrealista, pixel art, cómic, etc.); técnica y medio (lineart, cell-shading, pinceladas sueltas/impasto, sfumato, semi-realista, "
                  "grano de película, etc.); paleta de color (colores dominantes, temperatura cálida/fría, saturación, contraste, armonía); "
                  "artistas, estudios o movimientos que usan ese estilo/técnica (nómbralos con 'al estilo de…' cuando sea claro, p. ej. Studio Ghibli, "
                  "Moebius, Sargent, Loish, etc.); y otros aspectos técnicos del concepto: iluminación, atmósfera/mood, composición y encuadre, "
                  "acabado/textura, y lente/cámara si es una fotografía.\n"
                  "2) DESPUÉS describe lo que se ve: sujeto(s), vestuario, escena, fondo y elementos clave.\n"
                  "Devuelve SOLO el prompt (sin comillas ni explicaciones), en español, detallado pero conciso (máx ~160 palabras).")
        try:
            out = self._chat([
                {"role": "system", "content": sysmsg},
                {"role": "user", "content": [
                    {"type": "text", "text": "Analiza esta imagen y devuelve el prompt empezando por el concepto visual (estilo, técnica, paleta, artistas, aspectos técnicos) y luego lo que se ve."},
                    {"type": "image_url", "image_url": {"url": uri, "detail": detail}}]}], max_tokens=700)
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
        return self._json({"prompt": out})

    def h_upscale(self):
        # upscale 2× con gpt-image-2 (edits) — usa tu clave de OpenAI, sin fal.ai
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API de OpenAI (botón API)."})
        f = os.path.basename(b.get("file", ""))
        pr = self._proj(b)
        sb = self._sub(b)
        fp = phist_dir(pr, sb) / f
        if not fp.is_file():
            return self._json({"error": "No encuentro esa imagen."})
        raw0 = fp.read_bytes()
        if not sniff_image(raw0):
            return self._json({"error": "La imagen no es PNG/JPEG/WebP."})
        dims = img_dims(raw0)
        size = upscale_size(dims[0], dims[1], float(b.get("factor", 2))) if dims else "1536x1024"
        orig = next((x for x in load_json(phist_json(pr, sb), []) if x.get("file") == f), {})
        ct = MIME.get(sniff_image(raw0), "image/png").split(";")[0]
        prompt = ("Upscale this exact image to a higher resolution. Increase sharpness and recover fine "
                  "detail and texture, and reduce noise and compression artifacts. Keep the content, "
                  "composition, colors, framing and style identical — do not add, remove or alter anything.")
        boundary = "----studio" + uuid.uuid4().hex
        parts = []

        def field(n, v):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode())

        field("model", "gpt-image-2")
        field("prompt", prompt)
        field("size", size)
        field("quality", "high")
        field("n", "1")
        field("output_format", "png")
        field("moderation", "low")
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="{safe_fn(f)}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + raw0 + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        try:
            with urllib.request.urlopen(urllib.request.Request(API_EDIT, data=b"".join(parts),
                    headers={"Authorization": f"Bearer {key()}", "Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=300) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
        meta = {"prompt": "[mejorada 2×] " + (orig.get("prompt") or ""), "size": size,
                "quality": "high", "mode": "upscale", "output_format": "png",
                "project": pr, "sub": sb, "save_desktop": b.get("save_desktop", True)}
        res = self._save_results(data, meta, model_used="gpt-image-2")
        imgs = res.get("images", [])
        if not imgs:
            return self._json({"error": "El upscale no devolvió imagen."})
        return self._json({"file": imgs[0]["file"], "size": size, "cost": res.get("cost", 0)})

    def _fal_wait(self, mid, payload, tries=150):
        """Envía a la cola de fal y espera el resultado (para trabajos de 30s-5min)."""
        with self._fal_req(f"{FAL_QUEUE}/{mid}", payload, timeout=120) as r:
            rid = json.loads(r.read()).get("request_id")
        for _ in range(tries):
            time.sleep(2)
            with self._fal_req(f"{FAL_QUEUE}/{mid}/requests/{rid}/status") as r:
                st = json.loads(r.read()).get("status", "")
            if st == "COMPLETED":
                with self._fal_req(f"{FAL_QUEUE}/{mid}/requests/{rid}") as r:
                    return json.loads(r.read())
            if st not in ("IN_QUEUE", "IN_PROGRESS"):
                raise ValueError(f"terminó con estado {st}")
        raise ValueError("tardó demasiado; inténtalo de nuevo")

    def h_music(self):
        b = self._body()
        if not fal_key():
            return self._json({"error": "La música usa fal.ai: conecta tu clave en la sección Video."})
        prompt = (b.get("prompt") or "").strip()
        if len(prompt) < 10:
            return self._json({"error": "Describe la música con al menos 10 caracteres."})
        if b.get("model") == "minimax":
            mid = "fal-ai/minimax-music"
            payload = {"prompt": prompt[:2000],
                       "audio_setting": {"format": "mp3", "sample_rate": 44100, "bitrate": 256000}}
            if (b.get("lyrics") or "").strip():
                payload["lyrics"] = b["lyrics"].strip()[:3500]
                payload["lyrics_optimizer"] = True
            if b.get("instrumental"):
                payload["is_instrumental"] = True
            label, ext = "MiniMax", "mp3"
        else:
            mid = "fal-ai/lyria2"
            payload = {"prompt": prompt}
            if (b.get("negative") or "").strip():
                payload["negative_prompt"] = b["negative"].strip()
            label, ext = "Lyria 2", "wav"
        if str(b.get("seed") or "").strip().isdigit():
            payload["seed"] = int(b["seed"])
        try:
            res = self._fal_wait(mid, payload)
            url = (res.get("audio") or {}).get("url", "")
            if not url:
                raise ValueError("sin URL de audio en la respuesta")
            with urllib.request.urlopen(url, timeout=300) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            return self._json({"error": self._fal_err(e)})
        except (urllib.error.URLError, ValueError) as e:
            return self._json({"error": f"Música: {getattr(e, 'reason', e)}"})
        if ".mp3" in url:
            ext = "mp3"
        elif ".wav" in url:
            ext = "wav"
        name = self._save_audio(raw, "mus", ext,
            {"kind": "music", "prompt": prompt[:160], "voice": label, "model": mid.split("/")[-1],
             "size": ext, "quality": "", "mode": "audio", "cost": 0, "output_tokens": 0,
             "ts": time.strftime("%Y-%m-%d %H:%M"), "project": b.get("project", ""), "sub": b.get("sub", "")},
            b.get("save_desktop", True))
        return self._json({"file": name})

    def h_lipsync(self):
        b = self._body()
        if not fal_key():
            return self._json({"error": "El lip-sync usa fal.ai: conecta tu clave en la sección Video."})
        vid = b.get("video")
        if vid and vid.get("hist_file"):
            pr = self._proj(b)
            sb = self._sub(b)
            fp = phist_dir(pr, sb) / os.path.basename(vid["hist_file"])
            if not fp.is_file():
                fp = phist_dir(pr) / os.path.basename(vid["hist_file"])
            if not fp.is_file():
                fp = HIST_DIR / os.path.basename(vid["hist_file"])
            if not fp.is_file():
                return self._json({"error": "No encuentro ese video del historial."})
            v_uri = "data:video/mp4;base64," + base64.b64encode(fp.read_bytes()).decode()
        elif vid and vid.get("b64"):
            v_uri = "data:video/mp4;base64," + vid["b64"]
        else:
            return self._json({"error": "Sube o elige el video a sincronizar."})
        a = self._audio_b64(b.get("audio"))
        if not a:
            return self._json({"error": "Sube o elige el audio (voz del historial o archivo)."})
        payload = {"video_url": v_uri, "audio_url": f"data:{a[1]};base64," + a[0],
                   "guidance_scale": float(b.get("guidance", 1))}
        if b.get("loop_mode") in ("pingpong", "loop"):
            payload["loop_mode"] = b["loop_mode"]
        if str(b.get("seed") or "").strip().isdigit():
            payload["seed"] = int(b["seed"])
        mid = "fal-ai/latentsync"
        try:
            with self._fal_req(f"{FAL_QUEUE}/{mid}", payload, timeout=120) as r:
                rid = json.loads(r.read()).get("request_id")
        except urllib.error.HTTPError as e:
            return self._json({"error": self._fal_err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con fal.ai: {e.reason}"})
        if not rid:
            return self._json({"error": "fal.ai no devolvió un id de trabajo"})
        with LOCK:
            PENDING_VIDEOS[rid] = {"model_id": mid,
                                   "meta": {"prompt": "[lip-sync] " + (b.get("label") or ""),
                                            "model": "latentsync", "cost": 0,
                                            "project": b.get("project", ""),
                                            "sub": b.get("sub", ""),
                                            "save_desktop": b.get("save_desktop", True)}}
            save_jobs()
        return self._json({"id": rid})

    def h_distill(self):
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API (botón API)."})
        project = b.get("project", "")
        prompts = [h["prompt"] for h in load_json(phist_json(project), []) if h.get("prompt")][:40]
        if not prompts:
            return self._json({"error": "Este proyecto aún no tiene imágenes para analizar."})
        sys = ("Eres un director de arte. A partir de los prompts de un proyecto, destila un DESCRIPTOR DE ESTILO "
               "reutilizable en español, conciso (máx 120 palabras): técnica/medio, paleta, iluminación, composición, "
               "mood y detalles recurrentes. Se antepondrá a futuros prompts. Solo el descriptor.")
        payload = {"model": DISTILL_MODEL, "messages": [
            {"role": "system", "content": sys}, {"role": "user", "content": "Prompts:\n- " + "\n- ".join(prompts)}]}
        try:
            with urllib.request.urlopen(urllib.request.Request(API_CHAT, data=json.dumps(payload).encode(),
                    headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"}), timeout=60) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
        return self._json({"style": data["choices"][0]["message"]["content"].strip()})

    def _err(self, e):
        try:
            err = json.loads(e.read()).get("error", {})
        except Exception:
            return f"HTTP {e.code}"
        if err.get("code") == "moderation_blocked":
            det = err.get("moderation_details") or {}
            cats = det.get("categories") or []
            stage = det.get("moderation_stage")
            es = {"harassment": "acoso", "self-harm": "autolesión",
                  "sexual": "contenido sexual", "violence": "violencia"}
            hint = "Bloqueado por la política de contenido de OpenAI."
            if cats:
                hint += " Categoría: " + ", ".join(es.get(c, c) for c in cats) + "."
            if stage == "input":
                hint += " Vino del prompt — reescríbelo sin ese contenido."
            elif stage == "output":
                hint += " La imagen generada se bloqueó — cambia el prompt e intenta de nuevo."
            return hint
        return err.get("message", f"HTTP {e.code}")


if __name__ == "__main__":
    PENDING_VIDEOS.update(load_json(JOBS_JSON, {}))  # recupera trabajos de video en curso
    trash_purge()   # vacía la papelera de borrados con más de 14 días
    print(f"Gio Studio en  http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
