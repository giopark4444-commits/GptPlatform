#!/usr/bin/env python3
"""
Estudio v4 — gpt-image-2 (OpenAI) · app independiente
UI premium minimalista. Crear + Editar, referencias en ambos, memoria visual por
proyecto, historial con filtro y borrado, estimador de precio, moderación,
presets completos incl. anamórficos, editor de máscara integrado, pegado desde
portapapeles, atajos de teclado, resultados múltiples. Sin dependencias: solo Python 3.
"""
import io, json, base64, os, re, shutil, struct, subprocess, threading, time, uuid, urllib.request, urllib.error, zipfile, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

LOCK = threading.Lock()  # serializa lecturas-escrituras de los JSON

PORT = 7860
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
HIST_DIR.mkdir(parents=True, exist_ok=True)
PROJ_DIR.mkdir(parents=True, exist_ok=True)
SHELF_DIR.mkdir(parents=True, exist_ok=True)

PRICE_OUT = 30.0
PRICE_IN = 5.0          # USD por 1M de tokens de texto de entrada
PRICE_IN_IMG = 8.0      # USD por 1M de tokens de imagen de entrada (referencias)
PRICE_IN_IMG_CACHED = 2.0  # USD por 1M de tokens de imagen de entrada cacheados
DISTILL_MODEL = "gpt-4o-mini"
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


def is_general(proj):
    return (proj or "").strip().lower() in ("", "general")


def phist_dir(proj):
    d = HIST_DIR if is_general(proj) else PROJ_DIR / safe(proj) / "historial"
    d.mkdir(parents=True, exist_ok=True)
    return d


def phist_json(proj):
    return HIST_JSON if is_general(proj) else PROJ_DIR / safe(proj) / "historial.json"


def pshelf_dir(proj):
    d = SHELF_DIR if is_general(proj) else PROJ_DIR / safe(proj) / "estante"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pshelf_json(proj):
    return SHELF_JSON if is_general(proj) else PROJ_DIR / safe(proj) / "estante.json"


def add_history(item):
    with LOCK:
        jp = phist_json(item.get("project"))
        h = load_json(jp, [])
        h.insert(0, item)
        save_json(jp, h)  # sin tope: la galería recuerda todo (por proyecto)


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
 position:sticky;top:0;z-index:var(--z-sticky);background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(14px)}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;letter-spacing:.02em}
.projbar{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);display:flex;align-items:center;gap:8px;z-index:1}
.projbtn{display:flex;align-items:center;gap:9px;background:var(--surface2);border:1px solid var(--line);color:var(--txt);
 border-radius:11px;padding:8px 14px;font-size:13.5px;font-weight:500;cursor:pointer;transition:.16s;font-family:var(--ui);max-width:300px}
.projbtn:hover{border-color:var(--accent);background:var(--elev)}
.projbtn svg{width:15px;height:15px;stroke:var(--mut);fill:none;stroke-width:1.7;flex:none}
.projbtn .chev{width:13px;height:13px;margin-left:2px}
.projbtn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* modal de proyectos */
.projmodal{max-width:780px}
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
.top .right{margin-left:auto;display:flex;align-items:center;gap:14px}
.sess{font-size:12px;color:var(--mut)}.sess b{color:var(--txt);font-weight:500}
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
.thumb{position:relative;width:60px;height:60px;border-radius:9px;overflow:hidden;border:1px solid var(--line2)}
.thumb img{width:100%;height:100%;object-fit:cover}
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
.hint{font-size:11px;color:var(--faint);margin-top:10px;line-height:1.55}
.hint.warn{color:#e0b070;border-left:2px solid #e0b070;padding-left:9px}

/* center */
.canvas{aspect-ratio:4/3;width:100%;max-height:74vh;margin:0 auto;display:flex;align-items:center;justify-content:center;border:1px solid var(--line);border-radius:16px;
 overflow:hidden;background:var(--surface);position:relative;
 background-image:linear-gradient(45deg,rgba(255,255,255,.012) 25%,transparent 25%,transparent 75%,rgba(255,255,255,.012) 75%),linear-gradient(45deg,rgba(255,255,255,.012) 25%,transparent 25%,transparent 75%,rgba(255,255,255,.012) 75%);
 background-size:24px 24px;background-position:0 0,12px 12px}
.canvas img.result{max-width:100%;max-height:100%;display:block;cursor:zoom-in;border-radius:3px}
.floaters{position:absolute;top:12px;right:12px;display:flex;gap:7px;opacity:0;transform:translateY(-4px);transition:.18s;z-index:2}
.canvas:hover .floaters,.canvas:focus-within .floaters{opacity:1;transform:none}
.fbtn{width:34px;height:34px;border-radius:9px;background:color-mix(in srgb,var(--elev) 88%,transparent);backdrop-filter:blur(8px);border:1px solid var(--line2);
 color:var(--txt);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:.15s;text-decoration:none}
.fbtn:hover{background:var(--elev);border-color:var(--mut)}.fbtn svg{width:16px;height:16px}
.lightbox{position:fixed;inset:0;background:rgba(5,5,6,.93);backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;z-index:var(--z-lightbox);cursor:zoom-out;padding:30px 30px 90px}
.lightbox img{max-width:94vw;max-height:86vh;border-radius:8px;box-shadow:0 30px 90px rgba(0,0,0,.7)}
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
.lbprompt{font-size:12.5px;line-height:1.5;color:rgba(255,255,255,.85);white-space:pre-wrap;word-break:break-word;max-height:26vh;overflow-y:auto}
.lbbtns{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.lbbar button,.lbbar a{display:flex;align-items:center;gap:6px;background:var(--surface);border:1px solid var(--line2);
 color:var(--txt);border-radius:8px;padding:7px 11px;font-size:12px;cursor:pointer;text-decoration:none;transition:.15s;flex:none}
.lbbar button:hover,.lbbar a:hover{border-color:var(--mut)}
.lbbar svg{width:13px;height:13px}
.mini{display:inline-block;border:1px solid currentColor;border-radius:1.5px;opacity:.65;flex:none}
.empty{color:var(--faint);font-size:13px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:12px;padding:20px;max-width:420px}
.empty svg{width:30px;height:30px;stroke-width:1.3;opacity:.6}
.empty .kbdhint{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--faint)}
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
.sov{position:absolute;inset:0;display:flex;align-items:flex-start;gap:4px;padding:5px;opacity:0;
 background:linear-gradient(to bottom,rgba(0,0,0,.55),transparent 48%);transition:.15s}
.scard:hover .sov{opacity:1}
.sbtn{width:24px;height:24px;border-radius:7px;border:0;background:rgba(0,0,0,.62);backdrop-filter:blur(6px);
 display:flex;align-items:center;justify-content:center;cursor:pointer;text-decoration:none}
.sbtn svg{width:13px;height:13px;stroke:#fff;fill:none;stroke-width:2}
.sbtn:hover{background:rgba(0,0,0,.85)}
.sbtn.use{margin-right:auto}
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
.gal.selmode .gcard{cursor:pointer}
.gal.selmode .gcard .gfloat{display:none}
.gal.selmode .gcard::after{content:'';position:absolute;top:6px;left:6px;width:20px;height:20px;border-radius:50%;border:2px solid #fff;background:rgba(12,12,14,.55);box-shadow:0 0 0 1px rgba(0,0,0,.25);z-index:2}
.gal.selmode .gcard.sel::after{background:var(--accent);border-color:var(--accent);content:'✓';color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
.gal.selmode .gcard.sel{outline:2px solid var(--accent);outline-offset:-2px}
.galbulk{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0;padding:8px 10px;background:var(--accent-dim);border:1px solid var(--accent);border-radius:10px;font-size:12.5px}
.galbulk .gbcount{font-weight:600;margin-right:auto}
.galbulk button{border:1px solid var(--line2);background:var(--surface);color:var(--txt);border-radius:8px;padding:6px 10px;font-size:12px;cursor:pointer;font-family:inherit;display:flex;align-items:center;gap:5px}
.galbulk button:hover{border-color:var(--mut)}
.galbulk button.bdel.arm{border-color:var(--bad);color:var(--bad)}
.magic{float:right;background:none;border:0;color:var(--faint);cursor:pointer;padding:0 2px;line-height:1;transition:.15s}
.magic:hover{color:var(--accent)}
.magic svg{width:13px;height:13px}
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
    <div class="setlabel">Guía de la app · cómo sacarle todo el jugo</div>
    <div class="guide">
      <details open><summary>🚀 Lo básico</summary>
        <p>Arriba eliges entre <b>Imagen</b>, <b>Audio</b> y <b>Video</b> (también con las teclas <kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd>). Escribe tu idea en el cuadro de <b>prompt</b> y pulsa <kbd>Enter</kbd> para generar (<kbd>Shift</kbd>+<kbd>Enter</kbd> hace salto de línea). El <b>costo se estima antes</b> de generar y se muestra el <b>costo real</b> después; el gasto de la sesión aparece arriba a la derecha. Toda la app es 100% <b>gpt-image-2</b> de OpenAI para imagen.</p>
      </details>
      <details><summary>🖼️ Imagen</summary>
        <p><b>Crear</b> = texto → imagen. <b>Editar</b> = subes una o varias imágenes de referencia + un prompt (incluso con máscara para cambiar solo una zona).</p>
        <p><b>Referencias (opcional):</b> arrastra, pega (<kbd>⌘V</kbd>) o elige imágenes; también puedes <b>soltar un video</b> y se abre un selector de <b>fotogramas</b> (mueves la línea de tiempo y capturas los que quieras).</p>
        <p><b>Máscara / Anotar / Pins:</b> pinta la zona a editar, dibuja flechas/círculos o pon pines numerados con una instrucción por punto.</p>
        <p><b>Tamaño:</b> sliders de ancho/alto + <b>candado de proporción</b>, presets por proporción (nativos de gpt-image-2 en verde) y chips de resolución 720p→4K.</p>
        <p><b>Avanzado:</b> formato (PNG/JPEG/WebP), moderación, e <b>imágenes parciales</b> (preview en vivo mientras genera). <b>✨</b> junto al prompt lo mejora con IA. Puedes generar varias a la vez con <b>Cantidad</b>.</p>
      </details>
      <details><summary>📁 Proyectos</summary>
        <p>El botón junto a «Gio Studio» abre tus <b>proyectos</b>. Cada uno tiene su <b>propia memoria, su historial y sus «Mis imágenes»</b>. Puedes crear, renombrar (lápiz) y borrar; la portada es la última imagen. El espacio <b>General</b> se puede renombrar y también funciona como un proyecto completo.</p>
        <p><b>Memoria visual:</b> guarda imágenes de referencia que se <b>adjuntan solas</b> en cada generación del proyecto (para mantener un estilo/personaje). Además puedes guardar un <b>Estilo</b> de texto que se antepone a tus prompts, y <b>Destilar</b> (la IA resume el estilo a partir de tus prompts del proyecto).</p>
      </details>
      <details><summary>🕑 Historial</summary>
        <p>Cada imagen generada queda aquí. Al pasar el cursor aparecen acciones: <b>★ favorita</b>, <b>Mejorar 2×</b> (upscale con IA), <b>Comparar A/B</b>, <b>Iterar</b> (editar con un cambio), <b>Descargar</b>, <b>Copiar prompt</b>, <b>Enviar prompt a la biblioteca</b> 📖, <b>Usar como referencia</b> y <b>Borrar</b> (doble clic).</p>
        <p><b>Seleccionar:</b> activa el modo selección para marcar varias y <b>enviarlas a la biblioteca</b> o <b>borrarlas en lote</b>. <b>Buscar</b> filtra por prompt; <b>Ver todo</b> abre una galería en pestaña aparte.</p>
      </details>
      <details><summary>🗂️ Mis imágenes</summary>
        <p>Tu estante <b>local</b> (no se sube a OpenAI), siempre a mano bajo el lienzo. Arrastra imágenes del historial o del resultado para guardarlas ahí; puedes cambiar la carpeta. Soltar un <b>video</b> abre el selector de fotogramas. Clic en una imagen para ampliarla.</p>
      </details>
      <details><summary>📖 Prompt Library (biblioteca de prompts)</summary>
        <p>El botón <b>«Prompt Library»</b> abre tu biblioteca en una <b>pestaña aparte</b>. Guarda prompts con <b>★ favorito</b> y <b>veredicto</b> (✓ sirve / ✗ no sirve / — sin probar), búscalos y fíltralos.</p>
        <p><b>Categorías en árbol:</b> crea <b>subcarpetas</b> (botón ＋), <b>arrástralas</b> para reordenar (soltar arriba/abajo) o anidar (soltar en el centro), renómbralas (lápiz/doble clic).</p>
        <p><b>Compositor(es):</b> puedes tener <b>varios a la vez</b> (botón «Nuevo compositor»). Combinas prompts («+ Compositor»), los mejoras con <b>IA</b>, y los <b>envías a la interfaz principal</b> para generar — o los guardas. Las <b>plantillas</b> con <code>{variables}</code> te piden rellenar los huecos al usarlas.</p>
        <p><b>Mover prompts:</b> arrastra una tarjeta a una categoría, o usa el botón <b>«Mover»</b> (menú con todas). Desde el historial puedes enviar prompts y se <b>apilan</b> aquí (insignia «nuevo»). Atajo <kbd>⌘</kbd>/<kbd>Ctrl</kbd>+<kbd>K</kbd> en la interfaz principal: busca e inserta un prompt sin abrir la pestaña.</p>
      </details>
      <details><summary>🔍 Visor a pantalla completa</summary>
        <p>Clic en cualquier imagen para verla grande. Navega entre imágenes con las <b>flechas</b> <kbd>←</kbd> <kbd>→</kbd>. Abajo: <b>Usar prompt</b>, <b>A la biblioteca</b>, <b>Describir</b> (visión → prompt) y <b>Descargar</b>. <kbd>Esc</kbd> cierra.</p>
      </details>
      <details><summary>🎵 Audio y 🎬 Video</summary>
        <p><b>Audio:</b> voz (TTS) con tono y voces, <b>Transcripción</b>, <b>Efectos de sonido</b> y <b>Música</b>. <b>Video:</b> Seedance, Kling y OmniHuman vía fal.ai. Estas secciones de video y algunas de audio necesitan conectar su <b>clave</b> (botón API / fal).</p>
      </details>
      <details><summary>🎨 Personalización y respaldo</summary>
        <p><b>6 temas</b> (3 oscuros + 3 claros), <b>idioma</b> Español/English/Français, <b>tamaño del texto</b> ajustable, y <b>carpetas</b> de guardado por proyecto — todo aquí en Ajustes. El botón <b>Backup</b> (arriba) descarga/sincroniza todo tu contenido.</p>
      </details>
      <details><summary>⌨️ Atajos de teclado</summary>
        <p><kbd>Enter</kbd> genera · <kbd>Shift</kbd>+<kbd>Enter</kbd> salto de línea · <kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd> cambia de modo · <kbd>←</kbd>/<kbd>→</kbd> navega en el visor · <kbd>⌘</kbd>/<kbd>Ctrl</kbd>+<kbd>K</kbd> buscador de prompts · <kbd>Esc</kbd> cierra ventanas · doble clic en la papelera borra.</p>
      </details>
    </div>
  </div>
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
</div></div>

<div class="overlay hide" id="bakModal"><div class="modal">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <div class="ic"><svg viewBox="0 0 24 24"><path d="M17.5 19a4.5 4.5 0 0 0 .4-8.98 6 6 0 0 0-11.8 1.18A4 4 0 0 0 6.5 19h11z"/><path d="M12 12v5M9.5 14.5L12 17l2.5-2.5"/></svg></div>
  <h2>Backup y sincronización</h2>
  <p id="bakInfo">Cargando…</p>
  <div class="kmsg" id="bakState"></div>
  <button class="primary" id="bakSync" style="margin-bottom:8px"><svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg><span id="bakSyncTxt">Sincronizar ahora</span></button>
  <button class="ghost" id="bakZip" style="width:100%;justify-content:center;padding:11px"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar respaldo .zip</button>
  <p class="hint" style="margin-top:12px">El zip incluye <b>todo</b>: el historial y «Mis imágenes» de General y de <b>cada proyecto</b> (con las imágenes reales), estilos/memoria y configuración. Las claves API no se incluyen (se conectan una vez por equipo). En otro Mac: restaura el zip en <span class="mono">~/image-studio</span> o usa "Sincronizar".</p>
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
  <div class="projbar">
    <button class="projbtn" id="projBtn" title="Proyectos — cada uno con su memoria, historial y Mis imágenes">
      <svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
      <span id="projBtnLbl">General</span>
      <svg class="chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>
    </button>
    <select id="projSel" class="hide"></select>
    <button class="projbtn" id="promptLibBtn" title="Biblioteca de prompts — guarda tus prompts favoritos y los que sirven, por categorías (abre en pestaña aparte)">
      <svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
      <span>Prompt Library</span>
    </button>
  </div>
  <div class="seg">
    <button id="mImagen" class="on"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg>Imagen<kbd>1</kbd></button>
    <button id="mAudio"><svg viewBox="0 0 24 24"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg>Audio<kbd>2</kbd></button>
    <button id="mVideo"><svg viewBox="0 0 24 24"><rect x="2" y="5" width="14" height="14" rx="3"/><path d="M16 10l6-3v10l-6-3z"/></svg>Video<kbd>3</kbd></button>
  </div>
  <div class="right">
    <span class="sess" id="sessTot"><span>Sesión</span> <b class="mono" id="sessCostV">$0.0000</b> · <b class="mono" id="sessNV">0</b> <span>gen</span></span>
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
    </div>

    <div class="field">
      <div class="slabel"><label>Ancho</label><input class="vnum" id="wv" type="number" min="512" max="3840" step="16" value="1536"></div>
      <input type="range" id="w" min="512" max="3840" step="16" value="1536">
      <div class="slabel" style="margin-top:6px"><label>Alto</label><input class="vnum" id="hv" type="number" min="512" max="3840" step="16" value="1024"></div>
      <input type="range" id="h" min="512" max="3840" step="16" value="1024">
      <button type="button" id="lockBtn" class="lockbtn" aria-pressed="false" title="Bloquear proporción: al mover un lado, el otro se ajusta">
        <svg class="lk-open" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/></svg>
        <svg class="lk-closed" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
        <span>Mantener proporción</span></button>
      <input type="checkbox" id="lock" class="hide">
    </div>

    <div class="field">
      <label>Presets · relación de aspecto</label>
      <div class="presets" id="presets">
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
    <div class="estbar"><span>Costo estimado</span><span class="num" id="estv">~$0.00</span></div>
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

      <div class="estbar"><span>Costo estimado</span><span class="num" id="ttsEst">~$0.0000</span></div>
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
      <div class="estbar" style="margin-top:14px"><span>Costo estimado</span><span class="num" id="sttEst">~$0.006/min</span></div>
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

      <div class="estbar"><span>Costo estimado</span><span class="num" id="vidEst">~$—</span></div>
      <button class="primary" id="vidGo"><svg viewBox="0 0 24 24"><rect x="2" y="5" width="14" height="14" rx="3"/><path d="M16 10l6-3v10l-6-3z"/></svg><span id="vidGoTxt">Generar video</span></button>
      <p class="hint">El video se genera en la nube de fal y tarda 1–5 min; puedes seguir usando la app mientras. Se guarda en historial y tu carpeta. <kbd>↵</kbd> genera · <kbd>⇧</kbd><kbd>↵</kbd> salto de línea.</p>
    </div>
   </div>
  </div>

  <!-- CENTRO -->
  <div class="col mid an">
   <div id="imgStage" style="display:flex;flex-direction:column;flex:none">
    <div class="canvas" id="canvas">
      <div class="empty" id="emptyState"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg><div>Tu imagen aparecerá aquí</div><div class="kbdhint"><kbd>⌘</kbd><kbd>↵</kbd> generar · <kbd>1</kbd> Imagen <kbd>2</kbd> Audio <kbd>3</kbd> Video · <kbd>⌘</kbd><kbd>V</kbd> pegar</div></div>
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
        <div style="display:flex;gap:7px;flex:none">
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
      <div class="shelfgrid" id="shelfGrid"></div>
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
      <h3 class="eyebrow"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l3 2"/></svg>Historial<button class="chip" id="galFavBtn" title="Ver solo favoritas (★)" style="margin-left:auto">★</button><button class="ghost sm" id="galSelBtn" title="Seleccionar varias" style="margin-left:6px;text-transform:none;white-space:nowrap;flex:none"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>Seleccionar</button><button class="ghost sm" id="galAll" title="Ver todas en una ventana" style="margin-left:6px;text-transform:none;white-space:nowrap;flex:none"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>Ver todo</button><span class="mono" id="galCount" style="margin-left:10px;font-weight:400"></span></h3>
      <input type="text" id="galSearch" placeholder="Buscar en prompts…" spellcheck="false">
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

<div class="lightbox hide" id="lightbox">
  <button class="mclose" title="Cerrar"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <button class="lbnav prev" id="lbPrev" title="Anterior (←)"><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg></button>
  <button class="lbnav next" id="lbNext" title="Siguiente (→)"><svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button>
  <img id="lbImg" src="" alt="Vista completa">
  <div class="lbbar" id="lbBar">
    <span class="lbprompt" id="lbPrompt"></span>
    <div class="lbbtns">
    <button id="lbUse"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Usar prompt</button>
    <button id="lbLib"><svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>A la biblioteca</button>
    <button id="lbDesc"><svg viewBox="0 0 24 24"><path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5z"/><path d="M19 14l.7 2.3L22 17l-2.3.7L19 20l-.7-2.3L16 17l2.3-.7z"/></svg>Describir</button>
    <a id="lbDl" download><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar</a>
    </div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
let mode='crear',refs=[],mask=null,sessCost=0,sessN=0,ratio=1.5,projects={};
const REF_IMG_TOKENS=500; // respaldo si aún no conocemos las dimensiones; el real lo da la API
// tokens de imagen de entrada ≈ parches de 32px (esquema de gpt-image), tope 1536
function refTokens(w,h){if(!w||!h)return REF_IMG_TOKENS;return Math.min(1536,Math.ceil(w/32)*Math.ceil(h/32));}
// rellena r.tok midiendo cada referencia que aún no se haya medido; revalida al terminar
function ensureRefTokens(){refs.filter(r=>r.tok===undefined).forEach(r=>{r.tok=null;
 const im=new Image();im.onload=()=>{r.tok=refTokens(im.naturalWidth,im.naturalHeight);validate();};
 im.onerror=()=>{r.tok=REF_IMG_TOKENS;validate();};im.src='data:image/png;base64,'+r.b64;});}
let results=[],active=0,lastResult=null;
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
$('bakZip').onclick=()=>{const a=document.createElement('a');a.href='/backup.zip';a.download='studio-backup.zip';
 document.body.appendChild(a);a.click();a.remove();toast('Generando respaldo .zip…')};
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
 $('sessCostV').textContent='$'+sessCost.toFixed(4);$('sessNV').textContent=sessN}
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
 let t;if(q==='low'||q==='auto')t=129+64*MP;else if(q==='medium')t=1150+577*MP;else t=4600+2308*MP;
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
 $('estv').textContent='~$'+est.toFixed(est<0.1?4:3)+(n>1?' ×'+n:'');$('go').disabled=!ok;
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
$('presets').onclick=e=>{const c=e.target.closest('.chip');if(!c)return;
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
async function loadConfig(){const r=await(await fetch('/config')).json();
 genLabel=r.general_label||'General';
 $('saveDir').value=r.save_dir||'';cfgEffective=r.effective;renderSaveWhere();
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
function renderThumbs(){$('thumbs').innerHTML=refs.map((r,i)=>`<div class="thumb"><img src="data:image/png;base64,${r.b64}" alt="${esc(r.name)}"><button class="x" data-i="${i}" title="Quitar">${xicon()}</button></div>`).join('')}
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
$('thumbs').onclick=e=>{const b=e.target.closest('.x');if(b){refs.splice(+b.dataset.i,1);renderThumbs()}};
['dragover','dragenter'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.add('hot')}));
['dragleave','drop'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.remove('hot')}));
// arrastrar a cualquier parte de la ventana
window.addEventListener('dragover',e=>{e.preventDefault();$('drop').classList.add('hot')});
window.addEventListener('dragleave',e=>{if(!e.relatedTarget)$('drop').classList.remove('hot')});
window.addEventListener('drop',async e=>{e.preventDefault();$('drop').classList.remove('hot');
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

async function loadProjects(){projects=await(await fetch('/projects')).json();const s=$('projSel');
 const cur=s.value||localStorage.getItem('studio_proj')||'';
 s.innerHTML=`<option value="">${esc(genLabel)}</option>`+Object.keys(projects).filter(n=>n).map(n=>`<option ${n===cur?'selected':''}>${esc(n)}</option>`).join('');renderProj()}
async function setActiveProject(n){try{await fetch('/setproject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:n})});}catch(e){}}
async function switchProject(){const n=$('projSel').value;localStorage.setItem('studio_proj',n);await setActiveProject(n);renderProj();await loadGal();await loadShelf();}
let styleTab='img';
function stashStyle(){const n=$('projSel').value;if(!projects[n])return;
 projects[n][styleTab==='img'?'style':'style_video']=$('style').value}
function renderProj(){const n=$('projSel').value,p=projects[n];
 {const l=$('memProjLbl');if(l)l.textContent=n||genLabel;}
 {const b=$('projBtnLbl');if(b)b.textContent=n||genLabel;}
 $('style').value=p?(styleTab==='img'?(p.style||''):(p.style_video||'')):'';
 $('style').placeholder=styleTab==='img'?'Estilo: técnica, paleta, luz, mood…':'Estilo de video: cámara, movimiento, ritmo, grading…';
 $('prefThumbs').innerHTML=p?p.refs.map(f=>`<div class="thumb"><img src="/pfile?project=${encodeURIComponent(n)}&name=${encodeURIComponent(f)}" alt=""><button class="x" data-f="${esc(f)}" title="Quitar">${xicon()}</button></div>`).join(''):''}
$('styleSeg').onclick=e=>{const btn=e.target.closest('button');if(!btn)return;
 stashStyle();styleTab=btn.dataset.st;
 [...$('styleSeg').children].forEach(x=>x.classList.toggle('on',x.dataset.st===styleTab));renderProj()};
$('style').addEventListener('input',stashStyle);
$('projSel').onchange=()=>switchProject();
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
  return `<div class="projitem${active?' active':''}" data-name="${esc(c.name)}" data-label="${esc(c.label)}">
   <div class="projcard">${cov}</div>
   <div class="projfoot"><div class="mtext"><div class="pname">${esc(c.label)}</div><div class="pcount">${cnt}</div></div>${acts}</div></div>`}).join('');}
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
  mt.innerHTML='<input class="prename" type="text" maxlength="60" title="Enter para guardar · Esc para cancelar">';
  const inp=mt.querySelector('.prename');inp.value=label;inp.focus();inp.select();
  inp.addEventListener('click',ev=>ev.stopPropagation());
  inp.addEventListener('keydown',ev=>{if(ev.key==='Enter'){ev.preventDefault();renameProject(name,inp.value)}else if(ev.key==='Escape'){ev.preventDefault();renderProjCards(lastProjCards)}});
  return}
 const del=e.target.closest('.pdel');
 if(del){e.stopPropagation();
  if(!del.classList.contains('arm')){[...$('projGrid').querySelectorAll('.pdel.arm')].forEach(x=>x.classList.remove('arm'));
   del.classList.add('arm');
   toast((name?('Borra "'+label+'" y TODO su contenido'):('Vacía «'+label+'»: borra su historial y Mis imágenes'))+' · clic otra vez','bad');
   setTimeout(()=>del.classList.remove('arm'),2800);return}
  del.classList.remove('arm');await deleteProject(name);openProjModal();return}
 if(e.target.closest('input'))return;
 $('projSel').value=name;await switchProject();$('projModal').classList.add('hide')};
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
 const uri=dt.getData('text/x-studio-b64'),hist=dt.getData('text/x-studio-file'),shelf=dt.getData('text/x-studio-shelf');
 if(uri){const c=uri.indexOf(',');out.push({name:'generada.png',b64:c>=0?uri.slice(c+1):uri});}
 else if(hist){const b=await(await fetch('/file?name='+encodeURIComponent(hist))).blob();out.push({name:hist,b64:await blobToB64(b)});}
 else if(shelf){const b=await(await fetch('/shelffile?name='+encodeURIComponent(shelf))).blob();out.push({name:shelf,b64:await blobToB64(b)});}
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
$('prefThumbs').onclick=async e=>{const b=e.target.closest('.x');if(!b)return;const n=$('projSel').value;
 await fetch('/projectrefdel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:n,file:b.dataset.f})});await loadProjects()};

// ===== historial =====
const GDL='<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>';
const GCP='<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const GPL='<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>';
const GTR='<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>';
const GST='<svg viewBox="0 0 24 24"><path d="M12 3l2.4 5.9 6.1.4-4.7 4 1.5 6-5.3-3.3L6.7 19.3l1.5-6-4.7-4 6.1-.4z"/></svg>';
const GUP='<svg viewBox="0 0 24 24"><path d="M21 3h-6m6 0v6m0-6L13 11"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/></svg>';
const GCM='<svg viewBox="0 0 24 24"><rect x="3" y="5" width="8" height="14" rx="2"/><rect x="13" y="5" width="8" height="14" rx="2"/></svg>';
const GIT='<svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>';
const GLB='<svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><path d="M11 7h5"/></svg>';
function galFiltered(){const q=$('galSearch').value.trim().toLowerCase();
 const fav=$('galFavBtn').classList.contains('on');
 let imgs=hist.filter(it=>!['tts','stt','sfx','vid'].includes(it.kind));
 if(q)imgs=imgs.filter(it=>(it.prompt||'').toLowerCase().includes(q));
 if(fav)imgs=imgs.filter(it=>it.fav);
 return imgs}
$('galSearch').oninput=()=>{shown=30;renderGal()};
$('galFavBtn').onclick=()=>{$('galFavBtn').classList.toggle('on');shown=30;renderGal()};
function curProj(){return $('projSel')?($('projSel').value||''):''}
$('galAll').onclick=()=>{const p=encodeURIComponent(curProj()),fav=$('galFavBtn').classList.contains('on');
 window.open('/galeria?'+(fav?'fav=1&':'')+'project='+p,'_blank','noopener');};
// las ventanas "Ver todo" dejan imágenes en el servidor (/stage); el estudio las recoge (real, no depende del navegador)
async function addRefFromServer(src,file,project){try{
 const pq='&project='+encodeURIComponent(project||'');
 const url=(src==='shelf'?'/shelffile?name=':'/file?name=')+encodeURIComponent(file)+pq;
 const b=await(await fetch(url)).blob();
 refs.push({name:file,b64:await blobToB64(b)});renderThumbs();
 if(mode!=='editar')setMode('editar');validate();toast('Imagen añadida como referencia (desde Ver todo)');
}catch(e){toast('No pude añadir la referencia','bad')}}
async function pollStage(){try{const r=await(await fetch('/stage')).json();
 if(r.items&&r.items.length)for(const it of r.items)await addRefFromServer(it.src,it.file,it.project);}catch(e){}}
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
window.addEventListener('focus',()=>{pollAll();loadGal();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){pollAll();loadGal();}});
function renderGal(){const items=galFiltered();
 $('gal').innerHTML=items.map(it=>{const fn=encodeURIComponent(it.file),p=esc(it.prompt||'');
  return `<div class="gcard${selFiles.has(it.file)?' sel':''}" data-file="${esc(it.file)}" data-p="${p}"><img src="/file?name=${fn}" alt="${p.slice(0,60)}" title="${p}" loading="lazy" draggable="true">
   <div class="gfloat"><button class="gfbtn gstar${it.fav?' fav':''}" title="${it.fav?'Quitar de favoritas':'Favorita'}">${GST}</button>
   <button class="gfbtn gup" title="Mejorar 2× (upscale)">${GUP}</button>
   <button class="gfbtn gcmp" title="Comparar A/B (elige dos)">${GCM}</button>
   <button class="gfbtn giter" title="Iterar: editar con un cambio">${GIT}</button>
   <a class="gfbtn" href="/file?name=${fn}" download="${esc(it.file)}" title="Descargar">${GDL}</a>
   <button class="gfbtn gcopy" title="Copiar prompt">${GCP}</button>
   <button class="gfbtn glib" title="Enviar prompt a la biblioteca">${GLB}</button>
   <button class="gfbtn gref" title="Usar como referencia">${GPL}</button>
   <button class="gfbtn gdel" title="Borrar (doble clic)">${GTR}</button></div>
   <div class="c"><span>$${(it.cost||0).toFixed(4)}</span><span>${esc(it.size||'')}</span></div></div>`}).join('')
  ||'<div class="hint">Aún no hay imágenes en este proyecto</div>';
 $('galMore').classList.add('hide');
 $('gal').classList.toggle('selmode',selMode);
 $('galCount').textContent=items.length||''}
// ===== selección múltiple del historial =====
let selMode=false;const selFiles=new Set();
function renderBulk(){const bar=$('galBulk');if(!selMode){bar.classList.add('hide');return}
 bar.classList.remove('hide');
 bar.innerHTML='<span class="gbcount">'+selFiles.size+' seleccionada'+(selFiles.size===1?'':'s')+'</span>'
  +'<button id="bulkLib">'+GLB+'A la biblioteca</button>'
  +'<button id="bulkDel" class="bdel">'+GTR+'Borrar</button>'
  +'<button id="bulkExit">Salir</button>';
 $('bulkExit').onclick=()=>{selMode=false;selFiles.clear();renderGal();renderBulk()};
 $('bulkLib').onclick=async()=>{if(!selFiles.size){toast('Selecciona imágenes primero','bad');return}
  let n=0;for(const f of selFiles){const it=hist.find(x=>x.file===f);if(it&&(it.prompt||'').trim()){try{await fetch('/promptinbox',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:it.prompt})});n++}catch(e){}}}
  toast(n?(n+' prompt(s) enviados a la biblioteca'):'Ninguna tenía prompt',n?'':'bad');selMode=false;selFiles.clear();renderGal();renderBulk()};
 $('bulkDel').onclick=async(e)=>{const b=e.currentTarget;if(!selFiles.size){toast('Selecciona imágenes primero','bad');return}
  if(!b.classList.contains('arm')){b.classList.add('arm');b.lastChild.textContent='¿Borrar '+selFiles.size+'?';setTimeout(()=>{b.classList.remove('arm');renderBulk()},2600);return}
  for(const f of [...selFiles]){try{await fetch('/historydel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:f})});hist=hist.filter(x=>x.file!==f)}catch(e){}}
  const k=selFiles.size;selMode=false;selFiles.clear();renderGal();renderBulk();toast(k+' imagen(es) borradas')}}
$('galSelBtn').onclick=()=>{selMode=!selMode;selFiles.clear();renderGal();renderBulk()};
async function loadGal(){hist=await(await fetch('/history')).json();renderGal();renderAud()}
$('galMore').onclick=()=>{shown+=30;renderGal()};
function blobToB64(b){return new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(b)})}
$('gal').addEventListener('dragstart',e=>{const card=e.target.closest('.gcard');if(!card)return;
 e.dataTransfer.setData('text/x-studio-file',card.dataset.file);e.dataTransfer.effectAllowed='copy'});
$('gal').onclick=async e=>{
 if(selMode){const card=e.target.closest('.gcard');if(card){const f=card.dataset.file;if(selFiles.has(f))selFiles.delete(f);else selFiles.add(f);card.classList.toggle('sel');renderBulk()}return}
 if(e.target.closest('a'))return;
 const cp=e.target.closest('.gcopy'),rf=e.target.closest('.gref'),del=e.target.closest('.gdel'),
  star=e.target.closest('.gstar'),up=e.target.closest('.gup'),lib=e.target.closest('.glib'),
  cmp=e.target.closest('.gcmp'),iter=e.target.closest('.giter'),card=e.target.closest('.gcard');
 if(lib){const p=(hist.find(x=>x.file===card.dataset.file)||{}).prompt||card.dataset.p||'';
  if(!p.trim()){toast('Esta imagen no tiene prompt','bad');return}
  sendPromptToLib(p);flash(lib);return}
 if(cmp){if(!cmpA){cmpA=card.dataset.file;cmp.classList.add('fav');toast('A elegida · ahora pulsa comparar en otra imagen')}
  else if(cmpA===card.dataset.file){cmpA=null;cmp.classList.remove('fav');toast('Comparación cancelada')}
  else{openCmp(cmpA,card.dataset.file);cmpA=null;renderGal()}
  return}
 if(iter){const b=await(await fetch('/file?name='+encodeURIComponent(card.dataset.file))).blob();
  refs=[{name:card.dataset.file,b64:await blobToB64(b)}];mask=null;renderThumbs();renderMaskThumb();
  setMode('editar');$('prompt').value='';$('prompt').placeholder='Describe solo el cambio: "ahora de noche", "quita el texto", "hazlo acuarela"…';
  $('prompt').focus();toast('Iterando sobre esa imagen · describe el cambio');return}
 if(star){const it=hist.find(x=>x.file===card.dataset.file);if(!it)return;
  it.fav=!it.fav;star.classList.toggle('fav',it.fav);
  fetch('/histfav',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:it.file,fav:it.fav})});
  if($('galFavBtn').classList.contains('on'))renderGal();return}
 if(up){up.classList.add('busy');toast('Mejorando 2× con IA · ~30s…');
  try{const d=await(await fetch('/upscale',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:card.dataset.file,save_desktop:$('saveDesk').checked})})).json();
   if(d.error)toast(d.error,'bad');
   else{await loadGal();openLb('/file?name='+encodeURIComponent(d.file),'[mejorada 2×]',d.file);toast('Lista en '+d.size)}
  }catch(x){toast(String(x),'bad')}
  up.classList.remove('busy');return}
 if(cp){$('prompt').value=card.dataset.p;try{navigator.clipboard.writeText(card.dataset.p)}catch(x){}flash(cp);toast('Prompt copiado');return}
 if(rf){const b=await(await fetch('/file?name='+encodeURIComponent(card.dataset.file))).blob();refs.push({name:card.dataset.file,b64:await blobToB64(b)});renderThumbs();flash(rf);toast('Añadida como referencia');return}
 if(del){
  if(del.classList.contains('arm')){
   await fetch('/historydel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:card.dataset.file})});
   hist=hist.filter(it=>it.file!==card.dataset.file);renderGal();toast('Imagen eliminada')}
  else{del.classList.add('arm');setTimeout(()=>del.classList.remove('arm'),1800)}
  return}
 if(card){openLb('/file?name='+encodeURIComponent(card.dataset.file),card.dataset.p,card.dataset.file);lbScope='gal';lbCurFile=card.dataset.file;lbSyncNav()}};

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
 if(lbScope==='gal'){openLb('/file?name='+encodeURIComponent(c.dataset.file),c.dataset.p||'',c.dataset.file);lbScope='gal';lbCurFile=c.dataset.file;lbSyncNav()}
 else{const it=shelfItems.find(x=>x.file===c.dataset.shelf);if(it){openLb('/shelffile?name='+encodeURIComponent(it.file),'','');lbScope='shelf';lbCurFile=it.file;lbSyncNav()}}}
function openLb(src,p,file){lbScope=null;lbCurFile=null;$('lbImg').src=src;$('lbPrompt').textContent=p||'';
 $('lightbox').dataset.file=file||'';$('lbDesc').style.display=file?'':'none';
 $('lbPrompt').classList.toggle('hide',!p);
 if(file){$('lbDl').href='/file?name='+encodeURIComponent(file);$('lbDl').setAttribute('download',file)}
 else{$('lbDl').href=src;$('lbDl').setAttribute('download','imagen.png')}
 $('lbUse').style.display=p?'':'none';
 $('lbUse').onclick=ev=>{ev.stopPropagation();$('prompt').value=p||'';toast('Prompt cargado')};
 $('lbLib').style.display=p?'':'none';
 $('lbLib').onclick=ev=>{ev.stopPropagation();sendPromptToLib(p||'')};
 $('lightbox').classList.remove('hide');lbSyncNav()}
function lbSyncNav(){const pv=$('lbPrev'),nx=$('lbNext');
 if(!lbScope||!lbCurFile){pv.classList.add('off');nx.classList.add('off');return}
 const sel=lbScope==='gal'?'#gal .gcard':'#shelfGrid .scard',attr=lbScope==='gal'?'file':'shelf';
 const cards=[...document.querySelectorAll(sel)],idx=cards.findIndex(c=>c.dataset[attr]===lbCurFile);
 pv.classList.toggle('off',idx<=0);nx.classList.toggle('off',idx<0||idx>=cards.length-1)}
$('lightbox').onclick=()=>$('lightbox').classList.add('hide');
$('lbBar').onclick=e=>e.stopPropagation();
$('lbPrev').onclick=e=>{e.stopPropagation();lbNavigate(-1)};
$('lbNext').onclick=e=>{e.stopPropagation();lbNavigate(1)};
$('resultImg').onclick=()=>{if(results.length)openLb(results[active].image,lastResult?lastResult.prompt:'',null)};
$('resultImg').addEventListener('dragstart',e=>{if(!results.length){e.preventDefault();return}
 e.dataTransfer.setData('text/x-studio-b64',results[active].image);e.dataTransfer.effectAllowed='copy';});

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

async function run(){
 const prompt=$('prompt').value.trim();if(!prompt){toast('Escribe el prompt','bad');$('prompt').focus();return}
 const proj=$('projSel').value,pdata=projects[proj];
 const useVisual=$('useVis').checked&&pdata&&pdata.refs.length>0;
 if(mode==='editar'&&refs.length===0&&!useVisual){toast('Sube una imagen (o activa memoria visual)','bad');return}
 if(mask&&refs.length===0&&useVisual)toast('Ojo: la máscara se aplicará a la primera referencia del proyecto');
 $('resbar').classList.add('hide');$('strip').classList.add('hide');showState('spin');
 $('go').disabled=true;const prevTxt=$('goTxt').textContent;$('goTxt').textContent='Generando…';
 const body={prompt,size:$('w').value+'x'+$('h').value,quality:$('quality').value,n:+$('n').value,
  output_format:$('fmt').value,moderation:$('mod').value,
  partial_images:+($('partImg').value||0),project:proj,
  save_desktop:$('saveDesk').checked};
 if($('fmt').value!=='png')body.output_compression=+$('comp').value;
 let url='/generate';const willEdit=mode==='editar'||useVisual||refs.length>0;
 const refsUsed=refs.map(r=>({name:r.name,b64:r.b64}));
 if(willEdit){url='/edit';body.images=refs;if(mask)body.mask=mask;body.use_project_refs=useVisual}
 const willStream=(+($('partImg').value||0))>0 && +$('n').value===1;
 try{
  let d;
  const resp=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(willStream&&resp.body&&(resp.headers.get('content-type')||'').includes('ndjson')){
   const reader=resp.body.getReader(),dec=new TextDecoder();let buf='';
   const ext=$('fmt').value==='jpeg'?'jpeg':$('fmt').value;
   for(;;){const{value,done}=await reader.read();if(done)break;
    buf+=dec.decode(value,{stream:true});let nl;
    while((nl=buf.indexOf('\n'))>=0){const ln=buf.slice(0,nl).trim();buf=buf.slice(nl+1);if(!ln)continue;
     let ev;try{ev=JSON.parse(ln)}catch(_){continue}
     if(ev.type==='partial'){showState('result');$('floaters').classList.add('hide');$('resultImg').src='data:image/'+ext+';base64,'+ev.b64;}
     else if(ev.type==='done')d=ev.result;
     else if(ev.type==='error')d={error:ev.error};}}
   if(!d)d={error:'El preview no devolvió resultado.'};
  }else{d=await resp.json();}
  if(d.error){err(d.error)}
  else{
   results=d.images&&d.images.length?d.images:[{image:d.image}];
   lastResult={prompt,refsUsed,fmt:$('fmt').value};
   renderStrip();showResult(0);
   $('resbar').classList.remove('hide');
   bumpSess(d.cost||0,results.length);
   let ctxt='<b>$'+(d.cost||0).toFixed(4)+'</b>';
   // desglose salida (imagen) vs entrada (texto + referencias) cuando hubo imágenes de entrada
   if((d.in_img_tokens||0)>0)ctxt+=' <span style="color:var(--mut)">(salida $'+(d.out_cost||0).toFixed(4)
     +' + entrada $'+(d.in_cost||0).toFixed(4)+')</span>';
   ctxt+=' · '+(d.output_tokens||0)+' tok salida'+((d.in_img_tokens||0)>0?' · '+d.in_img_tokens+' tok ref':'');
   $('cost').innerHTML=ctxt
    +(results.length>1?' · '+results.length+' imágenes':'')
    +(d.via_visual?' · memoria visual':'');
   loadGal()}
 }catch(e){err(e)}
 $('goTxt').textContent=prevTxt;validate();
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
 $('batchEst').textContent=lines?imgs+(imgs>1?' imágenes':' imagen')+' ('+lines+'×'+n+') · ~$'+tot.toFixed(tot<0.1?4:2):'—';}
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
  project:$('projSel').value,save_desktop:$('saveDesk').checked};
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
 $('ttsEst').textContent='~$'+est.toFixed(4)}
$('ttsText').oninput=ttsEstCalc;
$('ttsModel').onchange=()=>{const mini=$('ttsModel').value==='gpt-4o-mini-tts';
 $('instrBox').classList.toggle('dim',!mini);$('speedBox').classList.toggle('dim',mini);ttsEstCalc()};
$('ttsModel').onchange();
$('ttsSpeed').oninput=()=>$('speedv').textContent=(+$('ttsSpeed').value).toFixed(2)+'×';
function showAudResult(d,title){$('audPlayer').src=d.audio;
 $('audTitle').textContent=title;
 $('audCost').innerHTML=d.credits!==undefined?'<b>'+d.credits+' cr</b>':'<b>$'+(d.cost||0).toFixed(4)+'</b>';
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
    project:$('projSel').value,save_desktop:$('saveDesk').checked};
   d=await(await fetch('/elspeech',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
   if(d.error)toast(d.error,'bad');
   else{showAudResult(d,(body.voice_name||'ElevenLabs')+' · '+$('elModel').value.replace('eleven_',''));
    bumpSess(0);loadGal();fetch('/elstatus').then(x=>x.json()).then(s=>{if(s.ok)renderElQuota(s)});
    toast('Voz generada · '+d.credits+' créditos')}
  }else{
   const m=$('ttsModel').value;
   const body={input:text,model:m,voice:selVoice,format:$('ttsFmt').value,
    project:$('projSel').value,save_desktop:$('saveDesk').checked};
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
 const body={input:text,influence:+$('sfxInf').value,project:$('projSel').value,save_desktop:$('saveDesk').checked};
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
 $('sttEst').textContent=sttDur?'~$'+(sttDur*p).toFixed(4):'~$'+p.toFixed(3)+'/min'}
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
  temperature:+$('sttTemp').value,duration:sttDur,project:$('projSel').value,save_desktop:$('saveDesk').checked};
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
  return `<div class="arow" data-file="${esc(it.file)}">
   <button class="ap${playing?' playing':''}" title="${playable?(playing?'Pausar':'Reproducir'):'Ver transcripción'}">${playable?(playing?APAUSE:APLAY):ADOC}</button>
   <div class="ameta"><div class="at" title="${esc(it.prompt||'')}">${esc(it.prompt||it.file)}</div>
    <div class="as mono">${sub} · ${price}</div></div>
   <a class="gfbtn" href="/file?name=${encodeURIComponent(it.file)}" download="${esc(it.file)}" title="Descargar">${GDL}</a>
   <button class="gfbtn adel" title="Borrar (doble clic)">${GTR}</button></div>`}).join('')}
$('audList').onclick=async e=>{
 const row=e.target.closest('.arow');if(!row||e.target.closest('a'))return;
 const del=e.target.closest('.adel');
 if(del){if(del.classList.contains('arm')){
   await fetch('/historydel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:row.dataset.file})});
   if(playingFile===row.dataset.file){audEl.pause();playingFile=null}
   hist=hist.filter(it=>it.file!==row.dataset.file);renderAud();toast('Eliminado')}
  else{del.classList.add('arm');setTimeout(()=>del.classList.remove('arm'),1800)}
  return}
 if(!e.target.closest('.ap'))return;
 const it=hist.find(x=>x.file===row.dataset.file);if(!it)return;
 if(it.kind==='stt'){
  const t=await(await fetch('/file?name='+encodeURIComponent(it.file))).text();
  $('txText').value=t;$('txCost').innerHTML='<b>$'+(it.cost||0).toFixed(4)+'</b> · '+esc(it.model||'');
  $('txDl').href='/file?name='+encodeURIComponent(it.file);$('txDl').setAttribute('download',it.file);
  if(mode!=='audio')setMode('audio');
  $('audEmpty').classList.add('hide');$('txResult').classList.remove('hide');return}
 if(playingFile===it.file&&!audEl.paused){audEl.pause();playingFile=null}
 else{audEl.src='/file?name='+encodeURIComponent(it.file);audEl.play();playingFile=it.file}
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
 $('vidEst').textContent=est!==null?'~$'+est.toFixed(2)+note:note}
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
 const est=parseFloat(($('vidEst').textContent.match(/[\d.]+/)||[0])[0])||0;
 let body={cost_est:est,project:$('projSel').value,save_desktop:$('saveDesk').checked,
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
   project:$('projSel').value,save_desktop:$('saveDesk').checked};
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
  `<div class="arow" data-file="${esc(it.file)}">
   <button class="ap" title="Ver video"><svg viewBox="0 0 24 24"><path d="M7 4l13 8-13 8z"/></svg></button>
   <div class="ameta"><div class="at" title="${esc(it.prompt||'')}">${esc(it.prompt||it.file)}</div>
    <div class="as mono">${esc(it.model||'')} · $${(it.cost||0).toFixed(2)}</div></div>
   <a class="gfbtn" href="/file?name=${encodeURIComponent(it.file)}" download="${esc(it.file)}" title="Descargar">${GDL}</a>
   <button class="gfbtn vdel" title="Borrar (doble clic)">${GTR}</button></div>`).join('')}
$('vidList').onclick=async e=>{
 const row=e.target.closest('.arow');if(!row||e.target.closest('a'))return;
 const del=e.target.closest('.vdel');
 if(del){if(del.classList.contains('arm')){
   await fetch('/historydel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:row.dataset.file})});
   hist=hist.filter(it=>it.file!==row.dataset.file);renderVid();toast('Video eliminado')}
  else{del.classList.add('arm');setTimeout(()=>del.classList.remove('arm'),1800)}
  return}
 if(!e.target.closest('.ap'))return;
 const it=hist.find(x=>x.file===row.dataset.file);if(!it)return;
 if(mode!=='video')setMode('video');
 $('vidEmpty').classList.add('hide');$('vidProgress').classList.add('hide');$('vidResult').classList.remove('hide');
 $('vidPlayer').src='/file?name='+encodeURIComponent(it.file);
 $('vidTitle').textContent=(it.prompt||'').slice(0,60);
 $('vidCost').innerHTML='<b>$'+(it.cost||0).toFixed(2)+'</b>';
 $('vidDl').href='/file?name='+encodeURIComponent(it.file);$('vidDl').setAttribute('download',it.file)};

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
let shelfItems=[];
async function loadShelf(){try{const r=await(await fetch('/shelf')).json();
 shelfItems=r.items||[];if(r.dir)$('shelfDirLbl').textContent=r.dir;renderShelf();}catch(e){}}
function renderShelf(){
 $('shelfEmpty').classList.toggle('hide',shelfItems.length>0);
 $('shelfGrid').innerHTML=shelfItems.map(it=>{const u='/shelffile?name='+encodeURIComponent(it.file);
  return `<div class="scard" title="${esc(it.name||'')}" draggable="true" data-shelf="${esc(it.file)}"><img src="${u}" alt="${esc(it.name||'')}" loading="lazy" draggable="false">
  <div class="sov"><button class="sbtn use" data-file="${esc(it.file)}" title="Usar como referencia"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button>
  <a class="sbtn" href="${u}" download="${esc(it.name||it.file)}" title="Descargar"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg></a>
  <button class="sbtn desc" data-file="${esc(it.file)}" title="Describir → prompt (visión)"><svg viewBox="0 0 24 24"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg></button>
  <button class="sbtn del" data-file="${esc(it.file)}" title="Quitar del estante">${xicon()}</button></div></div>`}).join('');}
async function shelfAddImages(imgs){if(!imgs.length)return;
 const r=await(await fetch('/shelfadd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({images:imgs})})).json();
 if(r.error){toast(r.error,'bad');return;}
 if(r.skipped&&r.skipped.length)toast(r.skipped.length+' descartada(s) por formato no válido','bad');
 shelfItems=r.items||shelfItems;renderShelf();
 const n=imgs.length-(r.skipped?r.skipped.length:0);
 if(n>0)toast(n+(n>1?' imágenes guardadas':' imagen guardada')+' en tu estante');}
async function shelfAddFiles(files){const imgs=[];let bad=0;
 for(const f of files){if(!OK_IMG_TYPES.has(f.type)){bad++;continue}imgs.push({name:f.name,b64:await fileToB64(f)});}
 if(bad)toast(bad+(bad>1?' archivos ignorados':' archivo ignorado')+': solo PNG/JPEG/WebP/GIF','bad');
 await shelfAddImages(imgs);}
$('shelfAddBtn').onclick=()=>$('shelfFile').click();
$('shelfAll').onclick=()=>window.open('/galeria?src=shelf&project='+encodeURIComponent(curProj()),'_blank','noopener');
$('shelfFile').onchange=e=>{const arr=[...e.target.files];e.target.value='';const vid=arr.find(isVideoFile);
 if(vid)openVideoFrames(vid,'shelf');const imgs=arr.filter(f=>!isVideoFile(f));if(imgs.length)shelfAddFiles(imgs);};
$('shelfGrid').onclick=async e=>{const use=e.target.closest('.use'),del=e.target.closest('.del'),desc=e.target.closest('.desc');
 if(use){const it=shelfItems.find(x=>x.file===use.dataset.file);if(!it)return;
  const b=await(await fetch('/shelffile?name='+encodeURIComponent(it.file))).blob();
  refs.push({name:it.name||it.file,b64:await blobToB64(b)});renderThumbs();
  if(mode!=='editar')setMode('editar');validate();toast('Añadida como referencia');return;}
 if(desc){const it=shelfItems.find(x=>x.file===desc.dataset.file);if(!it)return;
  desc.classList.add('busy');toast('Describiendo imagen…');
  try{const d=await(await fetch('/describe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:it.file})})).json();
   if(d.error)toast(d.error,'bad');
   else{setMode('crear');$('prompt').value=d.prompt;validate();$('prompt').focus();toast('Prompt de la imagen copiado al panel Crear');}
  }catch(x){toast(String(x),'bad')}
  desc.classList.remove('busy');return;}
 if(del){const f=del.dataset.file;
  await fetch('/shelfdel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:f})});
  shelfItems=shelfItems.filter(x=>x.file!==f);renderShelf();return;}
 if(e.target.closest('a,.sbtn'))return;     // descargar u otro botón → su acción nativa
 const card=e.target.closest('.scard');     // clic en la imagen → ampliar en lightbox flotante
 if(card){const it=shelfItems.find(x=>x.file===card.dataset.shelf);
  if(it){openLb('/shelffile?name='+encodeURIComponent(it.file),'','');lbScope='shelf';lbCurFile=it.file;lbSyncNav()}}};
// arrastrar una imagen DEL estante hacia otra zona (p.ej. memoria visual o referencias)
$('shelfGrid').addEventListener('dragstart',e=>{const card=e.target.closest('.scard');if(!card)return;
 e.dataTransfer.setData('text/x-studio-shelf',card.dataset.shelf);e.dataTransfer.effectAllowed='copy';});
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
 const r=await(await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shelf_dir:p,project:curProj()})})).json();
 if(r.error){toast(r.error,'bad');return;}
 if(r.shelf_effective)$('shelfDirLbl').textContent=r.shelf_effective;
 $('shelfDirRow').classList.add('hide');toast('«Mis imágenes» de este proyecto se guardarán en '+(r.shelf_effective||'tu carpeta'));loadShelf();}
$('shelfDirEdit').onclick=async()=>{toast('Abriendo selector de carpeta…');
 try{const r=await(await fetch('/pickfolder')).json();
  if(r.path)saveShelfDir(r.path);else if(r.error)toast(r.error,'bad');
 }catch(e){toast(String(e),'bad')}};
$('shelfDirSave').onclick=()=>saveShelfDir($('shelfDirIn').value);
$('shelfDirIn').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();$('shelfDirSave').click();}});
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
let LANG=localStorage.getItem('studio_lang')||'es';
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
 document.querySelectorAll('#langSeg button').forEach(b=>b.classList.toggle('on',b.dataset.lang===lang));}
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
$('setGenPick').onclick=async()=>{toast('Abriendo selector de carpeta…');
 try{const r=await(await fetch('/pickfolder')).json();
  if(r.path){const c=await(await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({save_dir:r.path,project:curProj()})})).json();
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
(async()=>{await loadConfig();await loadProjects();await setActiveProject($('projSel').value);await loadGal();await loadShelf();})();
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
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;padding:22px 26px}
.tile{position:relative;border-radius:14px;overflow:hidden;border:1px solid #e3dccb;background:#fffdf6;
 box-shadow:0 1px 2px rgba(0,0,0,.05);transition:transform .18s,box-shadow .18s,border-color .18s}
.tile:hover{transform:translateY(-3px);box-shadow:0 10px 26px rgba(0,0,0,.13);border-color:#cfc4ac}
.tile>img{width:100%;aspect-ratio:1;object-fit:cover;display:block}
.acts{position:absolute;top:8px;right:8px;display:flex;gap:5px;flex-wrap:wrap;max-width:108px;justify-content:flex-end;opacity:0;transition:opacity .15s}
.tile:hover .acts{opacity:1}
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
.glbx{position:fixed;top:18px;right:18px;width:38px;height:38px;border-radius:10px;border:1px solid rgba(255,255,255,.2);
 background:rgba(16,16,18,.85);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer}
.glbx svg{width:18px;height:18px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round}
.glbbar{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);display:flex;flex-direction:column;gap:10px;cursor:default;
 background:rgba(16,16,18,.92);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,.14);border-radius:12px;padding:12px 14px;max-width:min(760px,92vw)}
.glbp{font-size:12.5px;line-height:1.5;color:rgba(255,255,255,.85);white-space:pre-wrap;word-break:break-word;max-height:24vh;overflow-y:auto}
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
}
"""

def gallery_html(src, fav=False, proj=""):
    import html as _h, json as _json
    from urllib.parse import quote as _q
    is_shelf = (src == "shelf")
    pq = ("&project=" + _q(proj)) if proj else ""
    plabel = (" · " + proj) if (proj and not is_general(proj)) else ""
    if is_shelf:
        items, title, base = load_json(pshelf_json(proj), []), "Mis imágenes" + plabel, "/shelffile?name="
    else:
        items, title, base = load_json(phist_json(proj), []), "Historial" + plabel, "/file?name="
        if fav:
            items = [it for it in items if it.get("fav")]
            title = "Historial · favoritas" + plabel
    GPL = '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>'
    GCP = '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
    GST = '<svg viewBox="0 0 24 24"><path d="M12 3l2.4 5.9 6.1.4-4.7 4 1.5 6-5.3-3.3L6.7 19.3l1.5-6-4.7-4 6.1-.4z"/></svg>'
    GDL = '<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>'
    GOP = '<svg viewBox="0 0 24 24"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/></svg>'
    GLB = '<svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>'
    tiles = []
    for it in items:
        f = it.get("file", "")
        if not f:
            continue
        u = base + _q(f) + pq
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
        btns.append('<button class="gb" data-act="open" title="Abrir en grande (flotante)">' + GOP + '</button>')
        capt = pa if (not is_shelf) else _h.escape(str(it.get("name", "") or ""))
        cap = ('<span class="cap">' + capt + '</span>') if capt else ""
        tiles.append('<figure class="tile" data-file="' + fa + '" data-fav="' + ('1' if favon else '0') + '" data-prompt="' + pa + '">'
                     '<img src="' + u + '" loading="lazy" alt="">'
                     '<div class="acts">' + "".join(btns) + '</div>' + cap + '</figure>')
    grid = "".join(tiles) if tiles else '<div class="empty">Aún no hay imágenes.</div>'
    pqg = ("?project=" + _q(proj)) if proj else ""
    if is_shelf:
        favlink = ""
    elif fav:
        favlink = '<a class="favtog on" href="/galeria' + pqg + '" title="Ver todas las imágenes">' + GST + 'Todas</a>'
    else:
        favlink = '<a class="favtog" href="/galeria?fav=1' + (("&project=" + _q(proj)) if proj else "") + '" title="Ver solo las favoritas">' + GST + 'Solo favoritas</a>'
    js = ("const SRC=" + _json.dumps("shelf" if is_shelf else "history") + ";"
          "const PROJ=" + _json.dumps(proj or "") + ";"
          "var PQ=PROJ?('&project='+encodeURIComponent(PROJ)):'';"
          "var BASE=(SRC==='shelf')?'/shelffile?name=':'/file?name=';"
          "const tEl=document.getElementById('gtoast');"
          "function gt(m){tEl.textContent=m;tEl.classList.add('show');clearTimeout(tEl._t);tEl._t=setTimeout(function(){tEl.classList.remove('show')},1800);}"
          "async function stageRef(file){try{var r=await fetch('/stage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({src:SRC,file:file,project:PROJ})});var j=await r.json();gt(j&&j.ok?'Enviada como referencia al estudio ✓':(j&&j.error?j.error:'No se pudo enviar'));}catch(x){gt('No se pudo enviar');}}"
          "async function copyP(p){try{await navigator.clipboard.writeText(p||'');gt('Prompt copiado');}catch(x){gt('No se pudo copiar');}}"
          "async function stageP(p){if(!(p||'').trim()){gt('Esta imagen no tiene prompt');return;}try{var r=await fetch('/promptinbox',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p})});var j=await r.json();gt(j&&j.ok?'Prompt enviado a la biblioteca ✓':(j&&j.error?j.error:'No se pudo enviar'));}catch(x){gt('No se pudo enviar');}}"
          "var glb=document.getElementById('glb'),glbImg=document.getElementById('glbImg'),glbP=document.getElementById('glbP'),glbDl=document.getElementById('glbDl'),glbCopy=document.getElementById('glbCopy');"
          "var curFile='',curPrompt='';"
          "function openLb(file,prompt){curFile=file;curPrompt=prompt||'';var u=BASE+encodeURIComponent(file)+PQ;"
          "glbImg.src=u;glbP.textContent=curPrompt;glbDl.href=u;glbDl.setAttribute('download',file);glbCopy.style.display=curPrompt?'':'none';glb.classList.add('show');}"
          "function closeLb(){glb.classList.remove('show');glbImg.src='';}"
          "var g=document.querySelector('.grid');"
          "if(g)g.addEventListener('click',async function(e){"
          "var b=e.target.closest('[data-act]');"
          "if(b){e.preventDefault();var tile=b.closest('.tile'),file=tile.dataset.file,act=b.dataset.act;"
          "if(act==='ref'){stageRef(file);}"
          "else if(act==='open'){openLb(file,tile.dataset.prompt);}"
          "else if(act==='prompt'){copyP(tile.dataset.prompt);}"
          "else if(act==='lib'){stageP(tile.dataset.prompt);}"
          "else if(act==='fav'){var on=tile.dataset.fav!=='1';tile.dataset.fav=on?'1':'0';b.classList.toggle('on',on);"
          "try{await fetch('/histfav',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:file,fav:on,project:PROJ})});}catch(x){}"
          "gt(on?'Marcada como favorita':'Quitada de favoritas');}return;}"
          "if(e.target.closest('a'))return;"
          "var tile=e.target.closest('.tile');if(tile)openLb(tile.dataset.file,tile.dataset.prompt);"
          "});"
          "glb.addEventListener('click',function(e){if(e.target===glb||e.target.closest('#glbClose'))closeLb();});"
          "document.addEventListener('keydown',function(e){if(e.key==='Escape')closeLb();});"
          "document.getElementById('glbRef').onclick=function(){stageRef(curFile);};"
          "glbCopy.onclick=function(){copyP(curPrompt);};")
    return ('<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + _h.escape(title) + ' · Gio Studio</title><style>' + GALERIA_CSS + '</style></head><body>'
            '<header><h1>' + _h.escape(title) + '</h1><span class="count">' + str(len(tiles)) + ' imágenes</span>'
            '<span class="hint">Pasa el cursor sobre una imagen para sus acciones</span>' + favlink + '</header>'
            '<main class="grid">' + grid + '</main>'
            '<div class="glb" id="glb"><button class="glbx" id="glbClose" title="Cerrar (Esc)"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button>'
            '<img id="glbImg" alt="">'
            '<div class="glbbar"><span class="glbp" id="glbP"></span><div class="glbbtns">'
            '<button class="gbtn" id="glbRef">' + GPL + 'Usar como referencia</button>'
            '<button class="gbtn" id="glbCopy">' + GCP + 'Copiar prompt</button>'
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

    def _pump_sse(self, resp, meta, model_used="gpt-image-2"):
        # lee el SSE de OpenAI: emite cada imagen parcial y, al final, guarda y emite el resultado
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
            self._emit({"type": "error", "error": "El streaming no devolvió la imagen final."})
            return
        res = self._save_results({"data": [{"b64_json": final_b64}], "usage": usage}, meta, model_used=model_used)
        self._emit({"type": "done", "result": res})

    def _stream_err(self, e):
        if isinstance(e, urllib.error.HTTPError):
            self._emit({"type": "error", "error": self._err(e)})
        elif isinstance(e, urllib.error.URLError):
            self._emit({"type": "error", "error": f"Sin conexión con OpenAI: {e.reason}"})
        else:
            self._emit({"type": "error", "error": str(e)})

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
            return self._json(load_json(phist_json(self._proj()), []))
        if self.path == "/projects":
            return self._json(load_projects())
        if self.path == "/projectcards":
            glabel = (load_json(CONF_JSON, {}).get("general_label") or "General")
            cards = []
            for n in [""] + [k for k in load_projects().keys() if k]:
                items = load_json(phist_json(n), [])
                imgs = [it for it in items if it.get("kind") not in ("tts", "stt", "sfx", "vid") and it.get("file")]
                cards.append({"name": n, "label": (glabel if n == "" else n), "count": len(imgs),
                              "cover": imgs[0]["file"] if imgs else ""})
            return self._json({"cards": cards})
        if urlparse(self.path).path == "/config":
            conf = load_json(CONF_JSON, {})
            pr = self._proj()   # proyecto activo (o ?project=): sus carpetas
            glabel = conf.get("general_label", "") or "General"
            return self._json({"save_dir": conf.get("save_dir", ""),
                               "effective": str(save_dir(pr)).replace(str(HOME), "~"),
                               "shelf_effective": str(shelf_dir(pr)).replace(str(HOME), "~"),
                               "project": pr, "project_label": (glabel if is_general(pr) else pr),
                               "general_label": glabel,
                               "voice_styles": conf.get("voice_styles", [])})
        if self.path == "/backupstatus":
            return self._json(backup_status())
        if self.path == "/backup.zip":
            real = ROOT.resolve()
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in real.rglob("*"):
                    if not p.is_file() or p.name.endswith(".tmp") or p.name == ".DS_Store" or ".git" in p.parts:
                        continue
                    try:
                        z.write(p, p.relative_to(real))
                    except Exception:
                        pass
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="studio-backup-{time.strftime("%Y%m%d_%H%M")}.zip"')
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
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            fp = phist_dir(self._proj()) / os.path.basename(name)
            ctype = MIME.get(fp.suffix.lstrip(".").lower(), "application/octet-stream")
            return self._send(200, fp.read_bytes(), ctype, {"Cache-Control": "private, max-age=86400"}) if fp.is_file() else self._send(404, "no", "text/plain")
        if self.path.startswith("/pfile?"):
            q = parse_qs(urlparse(self.path).query)
            fp = proj_folder(q.get("project", [""])[0]) / os.path.basename(q.get("name", [""])[0])
            ctype = MIME.get(fp.suffix.lstrip(".").lower(), "application/octet-stream")
            return self._send(200, fp.read_bytes(), ctype) if fp.is_file() else self._send(404, "no", "text/plain")
        if urlparse(self.path).path == "/shelf":
            pr = self._proj()
            shdir = shelf_dir(pr)
            return self._json({"items": load_json(pshelf_json(pr), []),
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
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            fp = pshelf_dir(self._proj()) / os.path.basename(name)
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
            return self._send(200, gallery_html(src, fav, proj), "text/html; charset=utf-8",
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
                 "/histfav": self.h_histfav, "/magicprompt": self.h_magicprompt,
                 "/describe": self.h_describe, "/upscale": self.h_upscale,
                 "/music": self.h_music, "/lipsync": self.h_lipsync,
                 "/shelfadd": self.h_shelf_add, "/shelfdel": self.h_shelf_del,
                 "/promptlib": self.h_promptlib, "/promptstage": self.h_promptstage,
                 "/promptinbox": self.h_promptinbox,
                 "/stage": self.h_stage, "/setproject": self.h_setproject}.get(self.path)
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
            try:  # reetiquetar el historial del proyecto
                jp = phist_json(new)
                h = load_json(jp, [])
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

    def h_config(self):
        b = self._body()
        with LOCK:
            return self._config_locked(b)

    def _config_locked(self, b):
        conf = load_json(CONF_JSON, {})
        prn = b.get("project", ACTIVE_PROJ)   # las carpetas son por proyecto
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
        return self._json({"ok": True, "effective": str(save_dir(prn)).replace(str(HOME), "~"),
                           "shelf_effective": str(shelf_dir(prn)).replace(str(HOME), "~")})

    def h_historydel(self):
        b = self._body()
        f = os.path.basename(b.get("file", ""))
        if not f:
            return self._json({"error": "Falta el archivo"})
        pr = self._proj(b)
        with LOCK:
            jp = phist_json(pr)
            h = load_json(jp, [])
            save_json(jp, [x for x in h if x.get("file") != f])
        try:
            (phist_dir(pr) / f).unlink()
        except Exception:
            pass
        return self._json({"ok": True})

    def h_shelf_add(self):
        b = self._body()
        imgs = b.get("images", [])
        if not imgs:
            return self._json({"error": "Sin imágenes"})
        pr = self._proj(b)
        sdir = pshelf_dir(pr)
        ext_dir = shelf_dir(pr)   # carpeta externa configurable de este proyecto (o la interna)
        mirror = ext_dir.resolve() != sdir.resolve()
        skipped = []
        with LOCK:
            sj = pshelf_json(pr)
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
        with LOCK:
            sj = pshelf_json(pr)
            items = load_json(sj, [])
            save_json(sj, [x for x in items if x.get("file") != f])
        try:
            (pshelf_dir(pr) / f).unlink()   # solo la copia interna; las copias en tu carpeta se conservan
        except Exception:
            pass
        return self._json({"ok": True})

    def _style_prefix(self, project):
        if not project:
            return ""
        st = load_projects().get(project, {}).get("style", "")
        return (st.strip() + "\n\n") if st.strip() else ""

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
            (phist_dir(meta.get("project", "")) / name).write_bytes(raw)
            if meta.get("save_desktop", True):
                try:
                    dst = save_dir(meta.get("project", ""))
                    dst.mkdir(parents=True, exist_ok=True)
                    (dst / name).write_bytes(raw)
                except Exception:
                    pass
            add_history({"file": name, "prompt": meta["prompt"], "size": meta["size"],
                         "quality": meta["quality"], "mode": meta["mode"], "cost": per,
                         "output_tokens": out_t, "ts": time.strftime("%Y-%m-%d %H:%M"), "project": meta.get("project", "")})
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
                "save_desktop": b.get("save_desktop", True)}
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
                    headers=hdr), timeout=240) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
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
        for img in b.get("images", []):
            raw = base64.b64decode(img["b64"])
            if not sniff_image(raw):
                return self._json({"error": f"'{img.get('name','imagen')}' no es PNG/JPEG/WebP/GIF (formatos que acepta OpenAI)."})
            total_bytes += len(raw)
            filepart("image[]", img.get("name", "ref.png"), raw)
            nimg += 1
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
                "save_desktop": b.get("save_desktop", True)}
        partial = max(0, min(3, int(b.get("partial_images") or 0)))
        stream = partial > 0 and int(b.get("n", 1)) == 1
        if stream:
            field("stream", "true")
            field("partial_images", str(partial))
        parts.append(f"--{boundary}--\r\n".encode())
        hdr = {"Authorization": f"Bearer {key()}", "Content-Type": f"multipart/form-data; boundary={boundary}"}
        if stream:   # preview en vivo (imágenes parciales)
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
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
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
        (phist_dir(b.get("project", "")) / name).write_bytes(raw)
        if b.get("save_desktop", True):
            try:
                d = save_dir(b.get("project", ""))
                d.mkdir(parents=True, exist_ok=True)
                (d / name).write_bytes(raw)
            except Exception:
                pass
        add_history({"file": name, "kind": "tts", "prompt": text[:160], "voice": voice, "model": model,
                     "size": fmt, "quality": "", "mode": "audio", "cost": cost, "output_tokens": 0,
                     "ts": time.strftime("%Y-%m-%d %H:%M"), "project": b.get("project", "")})
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
        (phist_dir(b.get("project", "")) / name).write_text(raw)
        if b.get("save_desktop", True):
            try:
                d = save_dir(b.get("project", ""))
                d.mkdir(parents=True, exist_ok=True)
                (d / name).write_text(raw)
            except Exception:
                pass
        add_history({"file": name, "kind": "stt", "prompt": (text or "")[:160], "model": model,
                     "size": ext, "quality": "", "mode": "audio", "cost": cost, "output_tokens": 0,
                     "ts": time.strftime("%Y-%m-%d %H:%M"), "project": b.get("project", "")})
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
        (phist_dir(hist_item.get("project", "")) / name).write_bytes(raw)
        if save_desktop:
            try:
                d = save_dir(hist_item.get("project", ""))
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
             "ts": time.strftime("%Y-%m-%d %H:%M"), "project": b.get("project", "")},
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
             "ts": time.strftime("%Y-%m-%d %H:%M"), "project": b.get("project", "")},
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
             "ts": time.strftime("%Y-%m-%d %H:%M"), "project": m.get("project", "")},
            m.get("save_desktop", True))
        with LOCK:
            PENDING_VIDEOS.pop(rid, None); save_jobs()
        return self._json({"done": True, "file": name, "url": "/file?name=" + name, "cost": cost})

    def h_histfav(self):
        b = self._body()
        f = os.path.basename(b.get("file", ""))
        jp = phist_json(self._proj(b))
        with LOCK:
            h = load_json(jp, [])
            for it in h:
                if it.get("file") == f:
                    it["fav"] = bool(b.get("fav"))
            save_json(jp, h)
        return self._json({"ok": True})

    def h_setproject(self):
        global ACTIVE_PROJ
        ACTIVE_PROJ = (self._body().get("project") or "").strip()
        return self._json({"ok": True, "project": ACTIVE_PROJ})

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
        base = pshelf_dir(pr) if src == "shelf" else phist_dir(pr)
        if not (base / f).is_file():
            return self._json({"error": "la imagen no existe"}, 404)
        with STAGE_LOCK:
            STAGE.append({"src": src, "file": f, "project": pr})
            if len(STAGE) > 50:
                del STAGE[:-50]
        return self._json({"ok": True})

    def _chat(self, messages, max_tokens=400):
        payload = {"model": DISTILL_MODEL, "messages": messages, "max_tokens": max_tokens}
        with urllib.request.urlopen(urllib.request.Request(API_CHAT, data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"}), timeout=90) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"].strip()

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
        fp = phist_dir(pr) / f                  # imágenes del historial...
        if not fp.is_file():
            fp = pshelf_dir(pr) / f             # ...o del estante (Mis imágenes)
        if not fp.is_file():
            return self._json({"error": "No encuentro esa imagen."})
        # detalle de visión: high = lectura fiel (gpt-4o-mini soporta low/high/auto, no 'original')
        detail = b.get("detail", "high")
        if detail not in ("low", "high", "auto"):
            detail = "high"
        mime = MIME.get(fp.suffix.lstrip(".").lower(), "image/png").split(";")[0]
        uri = f"data:{mime};base64," + base64.b64encode(fp.read_bytes()).decode()
        try:
            out = self._chat([{"role": "user", "content": [
                {"type": "text", "text": "Describe esta imagen como un prompt detallado (sujeto, composición, iluminación, lente, paleta, estilo) para recrearla con un modelo de generación de imágenes. Devuelve solo el prompt, en español."},
                {"type": "image_url", "image_url": {"url": uri, "detail": detail}}]}])
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
        fp = phist_dir(pr) / f
        if not fp.is_file():
            return self._json({"error": "No encuentro esa imagen."})
        raw0 = fp.read_bytes()
        if not sniff_image(raw0):
            return self._json({"error": "La imagen no es PNG/JPEG/WebP."})
        dims = img_dims(raw0)
        size = upscale_size(dims[0], dims[1], float(b.get("factor", 2))) if dims else "1536x1024"
        orig = next((x for x in load_json(phist_json(pr), []) if x.get("file") == f), {})
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
                "project": pr, "save_desktop": b.get("save_desktop", True)}
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
             "ts": time.strftime("%Y-%m-%d %H:%M"), "project": b.get("project", "")},
            b.get("save_desktop", True))
        return self._json({"file": name})

    def h_lipsync(self):
        b = self._body()
        if not fal_key():
            return self._json({"error": "El lip-sync usa fal.ai: conecta tu clave en la sección Video."})
        vid = b.get("video")
        if vid and vid.get("hist_file"):
            pr = self._proj(b)
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
    print(f"Gio Studio en  http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
