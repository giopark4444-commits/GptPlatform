#!/usr/bin/env python3
"""
Estudio v4 — gpt-image-2 / gpt-image-1 (OpenAI) · app independiente
UI premium minimalista. Crear + Editar, referencias en ambos, memoria visual por
proyecto, historial con filtro y borrado, estimador de precio, moderación,
transparente (gpt-image-1), presets completos incl. anamórficos, editor de
máscara integrado, pegado desde portapapeles, atajos de teclado, resultados
múltiples. Sin dependencias: solo Python 3.
"""
import json, base64, os, re, shutil, time, uuid, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = 7860
HOME = Path.home()
KEY_FILE = HOME / ".openai_key"
EL_KEY_FILE = HOME / ".elevenlabs_key"
EL_API = "https://api.elevenlabs.io/v1"
ROOT = HOME / "image-studio"
HIST_DIR = ROOT / "historial"
HIST_JSON = ROOT / "historial.json"
PROJ_JSON = ROOT / "proyectos.json"
CONF_JSON = ROOT / "config.json"
PROJ_DIR = ROOT / "proyectos"
HIST_DIR.mkdir(parents=True, exist_ok=True)
PROJ_DIR.mkdir(parents=True, exist_ok=True)

PRICE_OUT = 30.0
PRICE_IN = 5.0
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
        "mp3": "audio/mpeg", "wav": "audio/wav", "aac": "audio/aac", "flac": "audio/flac",
        "opus": "audio/ogg", "pcm": "application/octet-stream",
        "txt": "text/plain; charset=utf-8", "srt": "text/plain; charset=utf-8",
        "vtt": "text/vtt", "json": "application/json"}


def key():
    return KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""


def el_key():
    return EL_KEY_FILE.read_text().strip() if EL_KEY_FILE.exists() else ""


def load_json(p, d):
    # si el archivo está corrupto o falta, intenta el respaldo .bak
    for cand in (p, p.with_suffix(p.suffix + ".bak")):
        try:
            return json.loads(cand.read_text())
        except Exception:
            continue
    return d


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


def add_history(item):
    h = load_json(HIST_JSON, [])
    h.insert(0, item)
    save_json(HIST_JSON, h[:500])


def safe(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:60] or "proj"


def load_projects():
    raw = load_json(PROJ_JSON, {})
    out = {}
    for k, v in raw.items():
        d = {"style": v, "refs": []} if isinstance(v, str) else {"style": v.get("style", ""), "refs": v.get("refs", [])}
        # estilo.md en la carpeta del proyecto manda: editable a mano y a prueba de JSON corrupto
        f = PROJ_DIR / safe(k) / "estilo.md"
        if f.exists():
            try:
                d["style"] = f.read_text()
            except Exception:
                pass
        out[k] = d
    return out


def proj_folder(name):
    d = PROJ_DIR / safe(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_key(k):
    try:
        urllib.request.urlopen(urllib.request.Request(API_MODELS, headers={"Authorization": f"Bearer {k}"}), timeout=20).read()
        return True
    except Exception:
        return False


def save_dir():
    raw = load_json(CONF_JSON, {}).get("save_dir") or ""
    return Path(os.path.expanduser(raw)) if raw else HOME / "Desktop"


def g1_size(size):
    try:
        w, h = map(int, size.split("x"))
    except Exception:
        return "1024x1024"
    r = w / h
    return "1536x1024" if r > 1.2 else "1024x1536" if r < 0.83 else "1024x1024"


HTML = r"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Studio</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%23e0a571'/%3E%3Cpath d='M12 5l1.6 4.7 4.7 1.2-3.8 2.7L15.8 18 12 15.3 8.2 18l1.3-4.4-3.8-2.7 4.7-1.2z' fill='%231a1206'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
 --bg:#0a0a0b;--surface:#101012;--surface2:#161618;--elev:#1c1c1f;
 --line:rgba(255,255,255,.06);--line2:rgba(255,255,255,.11);
 --txt:#ededee;--mut:#9a9aa1;--faint:#67676f;
 --accent:#e0a571;--accent-dim:rgba(224,165,113,.14);--ok:#7bd99a;--bad:#e57373;
 --ui:'Schibsted Grotesk',-apple-system,sans-serif;--mono:'Geist Mono',ui-monospace,monospace;
 --z-sticky:5;--z-modal:30;--z-lightbox:40;--z-toast:60;
}
*{box-sizing:border-box}
::selection{background:var(--accent-dim)}
body{margin:0;font-family:var(--ui);background:var(--bg);color:var(--txt);font-size:14px;line-height:1.45;
 -webkit-font-smoothing:antialiased;
 background-image:radial-gradient(1200px 600px at 80% -10%,rgba(224,165,113,.05),transparent 60%);}
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
 position:sticky;top:0;z-index:var(--z-sticky);background:rgba(10,10,11,.82);backdrop-filter:blur(14px)}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;letter-spacing:.02em}
.brand .dot{width:22px;height:22px;border-radius:7px;background:linear-gradient(140deg,var(--accent),#b87a45);
 display:flex;align-items:center;justify-content:center;color:#1a1206}
.brand .dot svg{width:13px;height:13px;stroke-width:2}
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

.wrap{display:grid;grid-template-columns:362px 1fr 312px;gap:1px;background:var(--line);
 min-height:calc(100vh - 59px)}
.wrap>*{background:var(--bg)}
@media(max-width:1180px){.wrap{grid-template-columns:1fr}}
.col{padding:22px;overflow:auto}
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

.presets{display:flex;flex-wrap:wrap;gap:6px}
.pgroup{font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);width:100%;margin:8px 0 2px}
.pgroup:first-child{margin-top:0}
.chip{font-family:var(--mono);font-size:11px;background:var(--surface);border:1px solid var(--line);color:var(--mut);
 border-radius:7px;padding:5px 9px;cursor:pointer;transition:.15s;display:inline-flex;align-items:center;gap:7px}
.chip:hover{border-color:var(--line2);color:var(--txt)}
.chip.on{background:var(--accent-dim);border-color:var(--accent);color:var(--accent)}

.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.drop{display:flex;align-items:center;justify-content:center;gap:8px;border:1px dashed var(--line2);border-radius:10px;
 padding:14px;color:var(--mut);font-size:12.5px;cursor:pointer;background:var(--surface);transition:.16s;text-align:center}
.drop:hover,.drop.hot{border-color:var(--accent);color:var(--txt);background:var(--surface2)}
.thumbs{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}
.thumb{position:relative;width:54px;height:54px;border-radius:9px;overflow:hidden;border:1px solid var(--line2)}
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
.primary{width:100%;display:flex;align-items:center;justify-content:center;gap:9px;background:var(--txt);color:#0a0a0b;
 border:0;border-radius:11px;padding:14px;font-size:14px;font-weight:600;cursor:pointer;transition:.16s}
.primary:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(0,0,0,.4)}
.primary:disabled{opacity:.35;cursor:not-allowed;transform:none;box-shadow:none}
.hint{font-size:11px;color:var(--faint);margin-top:10px;line-height:1.55}

/* center */
.canvas{aspect-ratio:4/3;width:100%;max-height:74vh;margin:0 auto;display:flex;align-items:center;justify-content:center;border:1px solid var(--line);border-radius:16px;
 overflow:hidden;background:var(--surface);position:relative;
 background-image:linear-gradient(45deg,rgba(255,255,255,.012) 25%,transparent 25%,transparent 75%,rgba(255,255,255,.012) 75%),linear-gradient(45deg,rgba(255,255,255,.012) 25%,transparent 25%,transparent 75%,rgba(255,255,255,.012) 75%);
 background-size:24px 24px;background-position:0 0,12px 12px}
.canvas img.result{max-width:100%;max-height:100%;display:block;cursor:zoom-in;border-radius:3px}
.floaters{position:absolute;top:12px;right:12px;display:flex;gap:7px;opacity:0;transform:translateY(-4px);transition:.18s;z-index:2}
.canvas:hover .floaters,.canvas:focus-within .floaters{opacity:1;transform:none}
.fbtn{width:34px;height:34px;border-radius:9px;background:rgba(16,16,18,.82);backdrop-filter:blur(8px);border:1px solid var(--line2);
 color:var(--txt);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:.15s;text-decoration:none}
.fbtn:hover{background:var(--elev);border-color:var(--mut)}.fbtn svg{width:16px;height:16px}
.lightbox{position:fixed;inset:0;background:rgba(5,5,6,.93);backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;z-index:var(--z-lightbox);cursor:zoom-out;padding:30px 30px 90px}
.lightbox img{max-width:94vw;max-height:86vh;border-radius:8px;box-shadow:0 30px 90px rgba(0,0,0,.7)}
.lbbar{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);display:flex;align-items:center;gap:10px;
 background:rgba(16,16,18,.9);backdrop-filter:blur(10px);border:1px solid var(--line2);border-radius:12px;
 padding:9px 12px;max-width:min(760px,92vw);cursor:default}
.lbprompt{font-size:12px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:430px}
.lbbar button,.lbbar a{display:flex;align-items:center;gap:6px;background:var(--surface);border:1px solid var(--line2);
 color:var(--txt);border-radius:8px;padding:7px 11px;font-size:12px;cursor:pointer;text-decoration:none;transition:.15s;flex:none}
.lbbar button:hover,.lbbar a:hover{border-color:var(--mut)}
.lbbar svg{width:13px;height:13px}
.mini{display:inline-block;border:1px solid currentColor;border-radius:1.5px;opacity:.65;flex:none}
.empty{color:var(--faint);font-size:13px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:12px;padding:20px;max-width:420px}
.empty svg{width:30px;height:30px;stroke-width:1.3;opacity:.6}
.empty .kbdhint{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--faint)}
.empty .errmsg{color:var(--bad);line-height:1.5;max-width:380px;overflow-wrap:anywhere}
.retry{display:flex;align-items:center;gap:7px;background:var(--surface);border:1px solid var(--line2);color:var(--txt);
 border-radius:9px;padding:8px 16px;font-size:12.5px;cursor:pointer;transition:.15s}
.retry:hover{border-color:var(--mut)}
.spin{width:34px;height:34px;border:2.5px solid var(--line2);border-top-color:var(--accent);border-radius:50%;animation:sp .8s linear infinite}@keyframes sp{to{transform:rotate(360deg)}}
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
#galFilter{font-size:12px;padding:8px 11px;margin-bottom:10px}
.gal{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.gcard{position:relative;border:1px solid var(--line);border-radius:10px;overflow:hidden;cursor:zoom-in;background:var(--surface);transition:.16s}
.gcard:hover{border-color:var(--line2)}
.gcard img{width:100%;aspect-ratio:1/1;object-fit:cover;display:block}
.gcard .c{font-family:var(--mono);font-size:9.5px;color:var(--faint);padding:5px 6px;display:flex;justify-content:space-between}
.gfloat{position:absolute;top:5px;right:5px;display:flex;gap:4px;opacity:0;transform:translateY(-3px);transition:.15s}
.gcard:hover .gfloat{opacity:1;transform:none}
.gfbtn{width:25px;height:25px;border-radius:7px;background:rgba(12,12,14,.86);backdrop-filter:blur(6px);border:1px solid var(--line2);
 color:var(--txt);display:flex;align-items:center;justify-content:center;cursor:pointer;text-decoration:none;transition:.15s}
.gfbtn:hover{background:var(--elev);border-color:var(--mut)}.gfbtn svg{width:12px;height:12px;stroke-width:1.8}
.gfbtn.arm{border-color:var(--bad);color:var(--bad);background:rgba(229,115,115,.12)}
.more{width:100%;display:flex;align-items:center;justify-content:center;gap:7px;background:var(--surface);
 border:1px solid var(--line);color:var(--mut);border-radius:9px;padding:9px;font-size:12px;cursor:pointer;margin-top:10px;transition:.16s}
.more:hover{color:var(--txt);border-color:var(--line2)}

/* modal */
.overlay{position:fixed;inset:0;background:rgba(5,5,6,.78);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;z-index:var(--z-modal)}
.modal{background:var(--surface);border:1px solid var(--line2);border-radius:18px;padding:30px;max-width:440px;width:92%;
 box-shadow:0 30px 80px rgba(0,0,0,.6)}
.modal .ic{width:42px;height:42px;border-radius:12px;background:var(--accent-dim);display:flex;align-items:center;justify-content:center;color:var(--accent);margin-bottom:16px}
.modal h2{margin:0 0 7px;font-size:19px;font-weight:600}
.modal p{color:var(--mut);font-size:13px;margin:0 0 18px;line-height:1.55}.modal a{color:var(--accent)}
.modal input{margin-bottom:8px}.kmsg{font-size:12px;color:var(--mut);min-height:16px;margin-bottom:12px}

/* editor de imagen: máscara · anotar · pins */
.maskbox{background:var(--surface);border:1px solid var(--line2);border-radius:16px;padding:18px;max-width:980px;width:94%;
 box-shadow:0 30px 80px rgba(0,0,0,.6)}
.masktop{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:12px;flex-wrap:wrap}
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

/* audio */
#audioStage{display:flex;flex-direction:column;gap:14px;flex:1;min-height:0}
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

/* toasts */
.toasts{position:fixed;top:18px;left:50%;transform:translateX(-50%);z-index:var(--z-toast);display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none}
.toast{display:flex;align-items:center;gap:9px;background:var(--elev);border:1px solid var(--line2);border-radius:10px;
 padding:10px 16px;font-size:13px;color:var(--txt);box-shadow:0 12px 40px rgba(0,0,0,.5);
 animation:toastIn .25s cubic-bezier(.2,.7,.2,1);transition:.25s;max-width:min(480px,90vw)}
.toast::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--ok);flex:none}
.toast.bad::before{background:var(--bad)}
@keyframes toastIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}

.hide{display:none!important}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:var(--line2);border-radius:9px;border:2px solid var(--bg)}

@media (prefers-reduced-motion: reduce){
 .an{animation:none!important;opacity:1!important;transform:none!important}
 .toast{animation:none!important}
 .floaters,.gfloat{transition:none!important}
 .primary:hover{transform:none}
 *{transition-duration:.01ms!important}
 .spin{animation-duration:1.6s!important}
}
</style></head><body>

<div class="toasts" id="toasts"></div>

<div class="overlay hide" id="keyModal"><div class="modal">
  <div class="ic"><svg viewBox="0 0 24 24"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg></div>
  <h2>Conecta tu API de OpenAI</h2>
  <p>Pega tu clave para empezar. Se guarda solo en tu equipo (<span class="mono">~/.openai_key</span>) y nunca sale de aquí. Consíguela en <a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com</a>.</p>
  <input type="password" id="keyInput" placeholder="sk-proj-…" autocomplete="off">
  <div class="kmsg" id="keyMsg"></div>
  <button class="primary" id="keySave">Conectar</button>
</div></div>

<div class="overlay hide" id="maskModal"><div class="maskbox">
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

<div class="top">
  <div class="brand"><span class="dot"><svg viewBox="0 0 24 24"><path d="M12 3l1.9 5.6L19.5 10l-4.6 3.3L16.5 19 12 15.7 7.5 19l1.6-5.7L4.5 10l5.6-1.4z"/></svg></span>Studio</div>
  <div class="seg">
    <button id="mCrear" class="on"><svg viewBox="0 0 24 24"><path d="M12 3l1.9 5.6L19.5 10l-4.6 3.3L16.5 19 12 15.7 7.5 19l1.6-5.7L4.5 10l5.6-1.4z"/></svg>Crear<kbd>1</kbd></button>
    <button id="mEditar"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 15l5-5 4 4 3-3 6 6"/><circle cx="9" cy="9" r="1.4"/></svg>Editar<kbd>2</kbd></button>
    <button id="mAudio"><svg viewBox="0 0 24 24"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg>Audio<kbd>3</kbd></button>
  </div>
  <div class="right">
    <span class="sess" id="sessTot">Sesión <b class="mono">$0.0000</b> · <b class="mono">0</b> gen</span>
    <button class="ghost" id="cfgBtn"><span class="kdot" id="kdot"></span>API</button>
  </div>
</div>

<div class="wrap">
  <!-- IZQUIERDA -->
  <div class="col an">
   <div id="imgPanel">
    <div class="field" id="editBox">
      <label><span id="refLbl">Referencias · opcional</span></label>
      <div class="drop" id="drop"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>Arrastra, pega (⌘V) o elige</div>
      <input type="file" id="files" accept="image/png,image/jpeg,image/webp" multiple class="hide">
      <div class="thumbs" id="thumbs"></div>
      <div class="thumbs" id="maskThumb"></div>
      <div class="grid2" style="margin-top:9px;gap:7px">
        <div class="drop" id="maskPaint" style="padding:9px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/></svg>Pintar máscara</div>
        <div class="drop" id="dropMask" style="padding:9px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>Subir máscara PNG</div>
      </div>
      <input type="file" id="maskFile" accept="image/png" class="hide">
    </div>

    <div class="field">
      <label id="lblPrompt">Prompt</label>
      <textarea id="prompt" placeholder="Describe lo que imaginas…"></textarea>
    </div>

    <div class="field">
      <div class="slabel"><label>Ancho</label><input class="vnum" id="wv" type="number" min="512" max="3840" step="16" value="1536"></div>
      <input type="range" id="w" min="512" max="3840" step="16" value="1536">
      <div class="slabel" style="margin-top:6px"><label>Alto</label><input class="vnum" id="hv" type="number" min="512" max="3840" step="16" value="1024"></div>
      <input type="range" id="h" min="512" max="3840" step="16" value="1024">
      <label class="check" style="margin-top:10px"><input type="checkbox" id="lock"> Mantener proporción</label>
    </div>

    <div class="field">
      <label>Presets · relación de aspecto</label>
      <div class="presets" id="presets">
        <span class="pgroup">Social</span>
        <span class="chip" data-w="1024" data-h="1024">1:1</span>
        <span class="chip" data-w="1024" data-h="1280">4:5</span>
        <span class="chip" data-w="1088" data-h="1920">9:16</span>
        <span class="chip" data-w="1920" data-h="1088">16:9</span>
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
        <span class="chip" data-w="2400" data-h="1000">2.4:1</span>
        <span class="chip" data-w="3072" data-h="1024">Pano 3:1</span>
        <span class="pgroup">Resolución · escala el ratio actual</span>
        <span class="chip rchip" data-long="1920">1080</span>
        <span class="chip rchip" data-long="2560">2K</span>
        <span class="chip rchip" data-long="3072">3K</span>
        <span class="chip rchip" data-long="3840">4K</span>
      </div>
    </div>

    <div class="field grid2">
      <div><label>Calidad</label><select id="quality"><option value="high">High</option><option value="auto" selected>Auto</option><option value="medium">Medium</option><option value="low">Low</option></select></div>
      <div><label>Cantidad</label><select id="n"><option>1</option><option>2</option><option>3</option><option>4</option></select></div>
    </div>

    <details class="adv"><summary><svg viewBox="0 0 24 24" style="width:14px;height:14px"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>Ajustes avanzados<svg class="chev" viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M6 9l6 6 6-6"/></svg></summary>
      <div class="advbody">
        <div class="grid2" style="margin-bottom:12px">
          <div><label>Formato</label><select id="fmt"><option value="png">PNG</option><option value="jpeg">JPEG</option><option value="webp">WebP</option></select></div>
          <div><label>Fondo</label><select id="bg"><option value="auto">Auto</option><option value="opaque">Sólido</option><option value="transparent">Transparente</option></select></div>
        </div>
        <div class="grid2">
          <div><label>Moderación</label><select id="mod"><option value="auto">Auto</option><option value="low">Low</option></select></div>
          <div><label>Fidelidad</label><select disabled><option>High</option></select></div>
        </div>
        <div id="compBox" class="hide" style="margin-top:12px"><div class="slabel"><label>Compresión</label><span class="v" id="compv">80%</span></div><input type="range" id="comp" min="0" max="100" step="5" value="80"></div>
        <label class="check" style="margin-top:12px"><input type="checkbox" id="saveDesk" checked> Guardar copia en una carpeta</label>
        <div id="dirBox" style="margin-top:10px">
          <label>Carpeta de guardado</label>
          <div style="display:flex;gap:7px">
            <input type="text" id="saveDir" placeholder="~/Desktop" spellcheck="false">
            <button class="ghost" id="dirApply" style="flex:none">Aplicar</button>
          </div>
          <p class="hint" id="dirMsg" style="margin-top:6px"></p>
        </div>
        <p class="hint">Transparente usa <span class="mono">gpt-image-1</span> (tamaño fijo). Moderación <b>low</b> es el mínimo de OpenAI; no es "sin censura".</p>
      </div>
    </details>

    <div class="meta"><span class="mono" id="ratio">3:2</span><span class="valid ok" id="valid">válido</span></div>
    <div class="estbar"><span>Costo estimado</span><span class="num" id="estv">~$0.00</span></div>
    <button class="primary" id="go"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg><span id="goTxt">Generar</span></button>
    <p class="hint" id="saveWhere"></p>
    <p class="hint">Lado 512–3840 · múltiplos de 16 · ≥0.8 MP. El estimado es aproximado; el costo real aparece al terminar. <kbd>⌘</kbd><kbd>↵</kbd> genera.</p>
   </div>

   <div id="audioPanel" class="hide">
    <div class="seg" id="audSeg" style="margin-bottom:18px;width:100%">
      <button class="on" id="audTTS" style="flex:1;justify-content:center"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>Voz</button>
      <button id="audSTT" style="flex:1;justify-content:center"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h5"/></svg>Transcribir</button>
      <button id="audSFX" style="flex:1;justify-content:center"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M11 5L6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a9 9 0 0 1 0 14"/></svg>Efectos</button>
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
      <p class="hint">OpenAI: máx 4096 caracteres; instrucciones de tono solo con <span class="mono">gpt-4o-mini-tts</span>. ElevenLabs cobra en créditos de tu plan. Se guarda en historial y tu carpeta. <kbd>⌘</kbd><kbd>↵</kbd> genera.</p>
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
   </div>
  </div>

  <!-- CENTRO -->
  <div class="col mid an">
   <div id="imgStage" style="display:flex;flex-direction:column;flex:1;min-height:0">
    <div class="canvas" id="canvas">
      <div class="empty" id="emptyState"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg><div>Tu imagen aparecerá aquí</div><div class="kbdhint"><kbd>⌘</kbd><kbd>↵</kbd> generar · <kbd>1</kbd>/<kbd>2</kbd> cambiar modo · <kbd>⌘</kbd><kbd>V</kbd> pegar referencia</div></div>
      <div class="spin hide" id="spinner"></div>
      <img class="result hide" id="resultImg" alt="Resultado">
      <div class="floaters hide" id="floaters">
        <button class="fbtn" id="fCopy" title="Copiar prompt + referencias usadas"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
        <button class="fbtn" id="fAdd" title="Usar como referencia"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button>
        <a class="fbtn" id="fDl" title="Descargar" download><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg></a>
      </div>
    </div>
    <div class="strip hide" id="strip"></div>
    <div class="resbar hide" id="resbar">
      <span class="costtag" id="cost"></span>
      <div class="acts"><a id="dl" download="imagen.png"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar</a>
      <button id="again"><svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>Otra</button></div>
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
      <h3 class="eyebrow"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Proyecto · memoria</h3>
      <select id="projSel"></select>
      <div class="btnrow">
        <button id="newProj"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M12 5v14M5 12h14"/></svg>Nuevo</button>
        <button id="distill"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M12 3l1.9 5.6L19.5 10l-4.6 3.3L16.5 19 12 15.7 7.5 19l1.6-5.7L4.5 10l5.6-1.4z"/></svg>Destilar</button>
        <button id="delProj" title="Borrar proyecto (doble clic)"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>Borrar</button>
      </div>
      <label style="margin-top:14px">estilo.md · texto</label>
      <textarea id="style" placeholder="Estilo: técnica, paleta, luz, mood…"></textarea>
      <div class="btnrow"><button id="saveProj"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>Guardar estilo</button></div>
      <label style="margin-top:14px">Memoria visual · referencias</label>
      <div class="drop" id="dropPref" style="padding:10px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 5v14M5 12h14"/></svg>Añadir referencia</div>
      <input type="file" id="prefFile" accept="image/png,image/jpeg,image/webp" multiple class="hide">
      <div class="thumbs" id="prefThumbs"></div>
      <label class="check" style="margin-top:10px"><input type="checkbox" id="useVis" checked> Usar memoria visual al generar</label>
      <p class="hint">Con esto activo, estas imágenes se adjuntan solas como referencia en cada generación del proyecto (Crear y Editar), para mantener el mismo estilo sin re-subirlas. El estilo se guarda como <span class="mono">estilo.md</span> en la carpeta del proyecto y se antepone siempre al prompt.</p>
    </div>
    <div class="sec hide" id="audSec">
      <h3 class="eyebrow"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>Audio</h3>
      <div id="audList"></div>
    </div>
    <div class="sec">
      <h3 class="eyebrow"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l3 2"/></svg>Historial<span class="mono" id="galCount" style="margin-left:auto;font-weight:400"></span></h3>
      <select id="galFilter"><option value="*">Todos los proyectos</option></select>
      <div class="gal" id="gal"></div>
      <button class="more hide" id="galMore"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M6 9l6 6 6-6"/></svg>Ver más</button>
    </div>
  </div>
</div>

<div class="lightbox hide" id="lightbox">
  <img id="lbImg" src="" alt="Vista completa">
  <div class="lbbar" id="lbBar">
    <span class="lbprompt" id="lbPrompt"></span>
    <button id="lbUse"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Usar prompt</button>
    <a id="lbDl" download><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar</a>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
let mode='crear',refs=[],mask=null,sessCost=0,sessN=0,ratio=1.5,projects={};
let results=[],active=0,lastResult=null;
let hist=[],shown=30;

function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function toast(msg,kind){const t=document.createElement('div');t.className='toast'+(kind==='bad'?' bad':'');
 t.textContent=msg;$('toasts').appendChild(t);
 setTimeout(()=>{t.style.opacity='0';t.style.transform='translateY(-6px)';setTimeout(()=>t.remove(),260)},2600)}

async function checkKey(){const r=await(await fetch('/keystatus')).json();$('kdot').classList.toggle('on',r.ok);
 if(!r.ok)$('keyModal').classList.remove('hide');return r.ok}
$('cfgBtn').onclick=()=>$('keyModal').classList.remove('hide');
$('keySave').onclick=async()=>{const k=$('keyInput').value.trim();if(!k)return;$('keyMsg').textContent='Validando…';
 const r=await(await fetch('/setkey',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})})).json();
 if(r.ok){$('keyMsg').textContent='Conectada ✓';$('keyModal').classList.add('hide');$('kdot').classList.add('on');toast('API conectada')}
 else{$('keyMsg').textContent=(r.error||'clave inválida')}};

function bumpSess(c,n=1){sessCost+=c||0;sessN+=n;
 $('sessTot').innerHTML='Sesión <b class="mono">$'+sessCost.toFixed(4)+'</b> · <b class="mono">'+sessN+'</b> gen'}
function setMode(m){mode=m;
 $('mCrear').classList.toggle('on',m==='crear');$('mEditar').classList.toggle('on',m==='editar');$('mAudio').classList.toggle('on',m==='audio');
 const aud=m==='audio';
 $('imgPanel').classList.toggle('hide',aud);$('imgStage').classList.toggle('hide',aud);
 $('audioPanel').classList.toggle('hide',!aud);$('audioStage').classList.toggle('hide',!aud);
 if(!aud){$('lblPrompt').textContent=m==='editar'?'Instrucción de edición':'Prompt';
  $('refLbl').textContent=m==='editar'?'Imágenes a editar / combinar':'Referencias · opcional';
  $('goTxt').textContent=m==='editar'?'Editar':'Generar'}}
$('mCrear').onclick=()=>setMode('crear');$('mEditar').onclick=()=>setMode('editar');$('mAudio').onclick=()=>setMode('audio');

function gcd(a,b){return b?gcd(b,a%b):a}function fr(a,b){const g=gcd(a,b);return(a/g)+':'+(b/g)}
function snap(v){return Math.round(v/16)*16}
function estTokens(){const W=+$('w').value,H=+$('h').value,MP=W*H/1e6,q=$('quality').value;
 let t;if(q==='low'||q==='auto')t=129+64*MP;else if(q==='medium')t=1150+577*MP;else t=4600+2308*MP;return Math.max(80,Math.round(t))}
function validate(){const W=+$('w').value,H=+$('h').value,long=Math.max(W,H),mp=W*H;let ok=true,msg='válido';
 if(long>3840){ok=false;msg='lado > 3840'}else if(mp<800000){ok=false;msg='muy pequeña'}
 $('valid').textContent=msg;$('valid').className='valid '+(ok?'ok':'bad');$('ratio').textContent=fr(W,H);
 const n=+$('n').value,est=estTokens()*n*30/1e6;$('estv').textContent='~$'+est.toFixed(est<0.1?4:3)+(n>1?' ×'+n:'');$('go').disabled=!ok}
let selRes=0;
function clearRes(){selRes=0;document.querySelectorAll('.rchip').forEach(x=>x.classList.remove('on'))}
function applyRes(){if(!selRes)return;
 let W=+$('w').value,H=+$('h').value;const r=W/H;
 if(W>=H){W=selRes;H=W/r}else{H=selRes;W=H*r}
 W=Math.max(512,Math.min(3840,snap(W)));H=Math.max(512,Math.min(3840,snap(H)));
 $('w').value=W;$('h').value=H;$('wv').value=W;$('hv').value=H;ratio=W/H;validate()}
$('w').oninput=()=>{if($('lock').checked){$('h').value=snap(Math.min(3840,Math.max(512,$('w').value/ratio)));$('hv').value=$('h').value}$('wv').value=$('w').value;clearRes();validate()};
$('h').oninput=()=>{if($('lock').checked){$('w').value=snap(Math.min(3840,Math.max(512,$('h').value*ratio)));$('wv').value=$('w').value}$('hv').value=$('h').value;clearRes();validate()};
$('lock').onchange=()=>ratio=$('w').value/$('h').value;
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
 if(c.dataset.long){const was=c.classList.contains('on');
  document.querySelectorAll('.rchip').forEach(x=>x.classList.remove('on'));
  if(was){selRes=0;return}
  selRes=+c.dataset.long;c.classList.add('on');applyRes();return}
 document.querySelectorAll('.chip[data-w]').forEach(x=>x.classList.remove('on'));c.classList.add('on');
 $('w').value=c.dataset.w;$('h').value=c.dataset.h;$('wv').value=c.dataset.w;$('hv').value=c.dataset.h;ratio=c.dataset.w/c.dataset.h;
 if(selRes)applyRes();else validate()};
$('quality').onchange=validate;$('n').onchange=validate;
$('fmt').onchange=()=>$('compBox').classList.toggle('hide',$('fmt').value==='png');
$('comp').oninput=()=>$('compv').textContent=$('comp').value+'%';
let cfgEffective='~/Desktop';
function renderSaveWhere(){
 $('saveWhere').innerHTML='Se guarda en <span class="mono">~/image-studio/historial</span>'
  +($('saveDesk').checked?' + copia en <span class="mono">'+esc(cfgEffective)+'</span>':' (sin copia extra)');
 $('dirMsg').textContent='Copia en: '+cfgEffective;
 $('dirBox').style.opacity=$('saveDesk').checked?'1':'.4'}
$('saveDesk').checked=localStorage.getItem('studio_desk')!=='0';
$('saveDesk').onchange=()=>{localStorage.setItem('studio_desk',$('saveDesk').checked?'1':'0');renderSaveWhere()};
async function loadConfig(){const r=await(await fetch('/config')).json();
 $('saveDir').value=r.save_dir||'';cfgEffective=r.effective;renderSaveWhere();
 voiceStyles=r.voice_styles||[];renderVStyles()}
$('dirApply').onclick=async()=>{
 const r=await(await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({save_dir:$('saveDir').value})})).json();
 if(r.error){toast(r.error,'bad');return}
 cfgEffective=r.effective;renderSaveWhere();toast('Las copias irán a '+r.effective)};

function fileToB64(f){return new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(f)})}
function xicon(){return '<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>'}
function renderThumbs(){$('thumbs').innerHTML=refs.map((r,i)=>`<div class="thumb"><img src="data:image/png;base64,${r.b64}" alt="${esc(r.name)}"><button class="x" data-i="${i}" title="Quitar">${xicon()}</button></div>`).join('')}
async function addFiles(list){let added=0;
 for(const f of list){if(!f.type.startsWith('image/'))continue;
  if(f.size>50*1024*1024){toast(f.name+' supera 50MB','bad');continue}
  refs.push({name:f.name,b64:await fileToB64(f)});added++}
 if(added)renderThumbs();return added}
$('drop').onclick=()=>$('files').click();
$('files').onchange=e=>{addFiles(e.target.files);e.target.value=''};
$('thumbs').onclick=e=>{const b=e.target.closest('.x');if(b){refs.splice(+b.dataset.i,1);renderThumbs()}};
['dragover','dragenter'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.add('hot')}));
['dragleave','drop'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.remove('hot')}));
// arrastrar a cualquier parte de la ventana
window.addEventListener('dragover',e=>{e.preventDefault();$('drop').classList.add('hot')});
window.addEventListener('dragleave',e=>{if(!e.relatedTarget)$('drop').classList.remove('hot')});
window.addEventListener('drop',async e=>{e.preventDefault();$('drop').classList.remove('hot');
 const audF=e.dataTransfer&&[...e.dataTransfer.files].find(f=>f.type.startsWith('audio/')||/\.(mp3|m4a|wav|webm|ogg|oga|flac|mpga)$/i.test(f.name));
 if(audF){setSttFile(audF);return}
 const sf=e.dataTransfer&&e.dataTransfer.getData('text/x-studio-file');
 if(sf){const b=await(await fetch('/file?name='+encodeURIComponent(sf))).blob();
  refs.push({name:sf,b64:await blobToB64(b)});renderThumbs();toast('Añadida como referencia');return}
 if(e.dataTransfer&&e.dataTransfer.files.length){const n=await addFiles(e.dataTransfer.files);if(n)toast(n+(n>1?' imágenes añadidas':' imagen añadida')+' como referencia')}});
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
 s.innerHTML='<option value="">Sin proyecto</option>'+Object.keys(projects).map(n=>`<option ${n===cur?'selected':''}>${esc(n)}</option>`).join('');renderProj()}
function renderProj(){const n=$('projSel').value,p=projects[n];$('style').value=p?p.style:'';
 $('prefThumbs').innerHTML=p?p.refs.map(f=>`<div class="thumb"><img src="/pfile?project=${encodeURIComponent(n)}&name=${encodeURIComponent(f)}" alt=""><button class="x" data-f="${esc(f)}" title="Quitar">${xicon()}</button></div>`).join(''):''}
$('projSel').onchange=()=>{localStorage.setItem('studio_proj',$('projSel').value);renderProj()};
$('useVis').checked=localStorage.getItem('studio_usevis')!=='0';
$('useVis').onchange=()=>localStorage.setItem('studio_usevis',$('useVis').checked?'1':'0');
$('newProj').onclick=async()=>{const n=(prompt('Nombre del proyecto:')||'').trim();if(!n)return;
 if(projects[n]){$('projSel').value=n;localStorage.setItem('studio_proj',n);renderProj();toast('El proyecto "'+n+'" ya existía · seleccionado');return}
 await fetch('/project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});
 await loadProjects();$('projSel').value=n;localStorage.setItem('studio_proj',n);renderProj();toast('Proyecto "'+n+'" creado')};
$('delProj').onclick=async()=>{const n=$('projSel').value;
 if(!n){toast('Elige el proyecto a borrar','bad');return}
 if(!$('delProj').classList.contains('arm')){
  $('delProj').classList.add('arm');toast('Clic otra vez para borrar "'+n+'" (estilo y referencias)','bad');
  setTimeout(()=>$('delProj').classList.remove('arm'),2500);return}
 $('delProj').classList.remove('arm');
 await fetch('/projectdel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});
 localStorage.setItem('studio_proj','');$('projSel').value='';
 await loadProjects();toast('Proyecto "'+n+'" borrado · sus imágenes del historial se conservan')};
$('saveProj').onclick=async()=>{const n=$('projSel').value;if(!n){toast('Elige o crea un proyecto','bad');return}await fetch('/project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,style:$('style').value})});projects[n].style=$('style').value;toast('Estilo guardado')};
$('distill').onclick=async()=>{const n=$('projSel').value;if(!n){toast('Elige un proyecto','bad');return}$('distill').textContent='…';
 const r=await(await fetch('/distill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:n})})).json();$('distill').innerHTML='Destilar';
 if(r.error){toast(r.error,'bad');return}$('style').value=r.style;toast('Estilo destilado · revisa y guarda')};
async function postRef(project,name,b64){
 await fetch('/projectref',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project,image:{name,b64}})})}
$('dropPref').onclick=()=>{if(!$('projSel').value){toast('Elige o crea un proyecto primero','bad');return}$('prefFile').click()};
$('prefFile').onchange=async e=>{const n=$('projSel').value;if(!n){toast('Elige o crea un proyecto primero','bad');return}
 let added=0;for(const f of e.target.files){await postRef(n,f.name,await fileToB64(f));added++}
 e.target.value='';await loadProjects();
 if(added)toast(added+(added>1?' referencias añadidas':' referencia añadida')+' a la memoria de "'+n+'"')};
['dragover','dragenter'].forEach(ev=>$('dropPref').addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();$('dropPref').classList.add('hot')}));
$('dropPref').addEventListener('dragleave',e=>{e.preventDefault();$('dropPref').classList.remove('hot')});
$('dropPref').addEventListener('drop',async e=>{e.preventDefault();e.stopPropagation();$('dropPref').classList.remove('hot');$('drop').classList.remove('hot');
 const n=$('projSel').value;if(!n){toast('Elige o crea un proyecto primero','bad');return}
 let added=0;
 const sf=e.dataTransfer.getData('text/x-studio-file');
 if(sf){const b=await(await fetch('/file?name='+encodeURIComponent(sf))).blob();await postRef(n,sf,await blobToB64(b));added++}
 else for(const f of e.dataTransfer.files){if(!f.type.startsWith('image/'))continue;await postRef(n,f.name,await fileToB64(f));added++}
 await loadProjects();
 if(added)toast(added+(added>1?' referencias añadidas':' referencia añadida')+' a la memoria de "'+n+'"')});
$('prefThumbs').onclick=async e=>{const b=e.target.closest('.x');if(!b)return;const n=$('projSel').value;
 await fetch('/projectrefdel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:n,file:b.dataset.f})});await loadProjects()};

// ===== historial =====
const GDL='<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>';
const GCP='<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const GPL='<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>';
const GTR='<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>';
function galFiltered(){const f=$('galFilter').value;
 const imgs=hist.filter(it=>!['tts','stt','sfx'].includes(it.kind));
 return f==='*'?imgs:imgs.filter(it=>(it.project||'')===f)}
function renderGal(){const items=galFiltered();
 $('gal').innerHTML=items.slice(0,shown).map(it=>{const fn=encodeURIComponent(it.file),p=esc(it.prompt||'');
  return `<div class="gcard" data-file="${esc(it.file)}" data-p="${p}"><img src="/file?name=${fn}" alt="${p.slice(0,60)}" title="${p}" loading="lazy" draggable="true">
   <div class="gfloat"><a class="gfbtn" href="/file?name=${fn}" download="${esc(it.file)}" title="Descargar">${GDL}</a>
   <button class="gfbtn gcopy" title="Copiar prompt">${GCP}</button>
   <button class="gfbtn gref" title="Usar como referencia">${GPL}</button>
   <button class="gfbtn gdel" title="Borrar (doble clic)">${GTR}</button></div>
   <div class="c"><span>$${(it.cost||0).toFixed(4)}</span><span>${esc(it.size||'')}</span></div></div>`}).join('')
  ||'<div class="hint">Aún no hay imágenes'+($('galFilter').value!=='*'?' en este proyecto':'')+'</div>';
 $('galMore').classList.toggle('hide',items.length<=shown);
 $('galCount').textContent=items.length||''}
async function loadGal(){hist=await(await fetch('/history')).json();
 const f=$('galFilter'),cur=f.value;
 const names=[...new Set(hist.map(it=>it.project||''))].filter(Boolean);
 f.innerHTML='<option value="*">Todos los proyectos</option><option value="">Sin proyecto</option>'
  +names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
 f.value=[...f.options].some(o=>o.value===cur)?cur:'*';
 renderGal();renderAud()}
$('galFilter').onchange=()=>{shown=30;renderGal()};
$('galMore').onclick=()=>{shown+=30;renderGal()};
function blobToB64(b){return new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(b)})}
$('gal').addEventListener('dragstart',e=>{const card=e.target.closest('.gcard');if(!card)return;
 e.dataTransfer.setData('text/x-studio-file',card.dataset.file);e.dataTransfer.effectAllowed='copy'});
$('gal').onclick=async e=>{
 if(e.target.closest('a'))return;
 const cp=e.target.closest('.gcopy'),rf=e.target.closest('.gref'),del=e.target.closest('.gdel'),card=e.target.closest('.gcard');
 if(cp){$('prompt').value=card.dataset.p;try{navigator.clipboard.writeText(card.dataset.p)}catch(x){}flash(cp);toast('Prompt copiado');return}
 if(rf){const b=await(await fetch('/file?name='+encodeURIComponent(card.dataset.file))).blob();refs.push({name:card.dataset.file,b64:await blobToB64(b)});renderThumbs();flash(rf);toast('Añadida como referencia');return}
 if(del){
  if(del.classList.contains('arm')){
   await fetch('/historydel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:card.dataset.file})});
   hist=hist.filter(it=>it.file!==card.dataset.file);renderGal();toast('Imagen eliminada')}
  else{del.classList.add('arm');setTimeout(()=>del.classList.remove('arm'),1800)}
  return}
 if(card)openLb('/file?name='+encodeURIComponent(card.dataset.file),card.dataset.p,card.dataset.file)};

// ===== lightbox =====
function openLb(src,p,file){$('lbImg').src=src;$('lbPrompt').textContent=p||'';
 $('lbPrompt').classList.toggle('hide',!p);
 if(file){$('lbDl').href='/file?name='+encodeURIComponent(file);$('lbDl').setAttribute('download',file)}
 else{$('lbDl').href=src;$('lbDl').setAttribute('download','imagen.png')}
 $('lbUse').onclick=ev=>{ev.stopPropagation();$('prompt').value=p||'';toast('Prompt cargado')};
 $('lightbox').classList.remove('hide')}
$('lightbox').onclick=()=>$('lightbox').classList.add('hide');
$('lbBar').onclick=e=>e.stopPropagation();
$('resultImg').onclick=()=>{if(results.length)openLb(results[active].image,lastResult?lastResult.prompt:'',null)};

// ===== resultado(s) =====
function showState(s){$('emptyState').classList.toggle('hide',s!=='empty');$('spinner').classList.toggle('hide',s!=='spin');
 $('resultImg').classList.toggle('hide',s!=='result');$('floaters').classList.toggle('hide',s!=='result')}
function err(m){let msg=typeof m==='string'?m:(m&&m.message)||'Error inesperado';
 $('emptyState').innerHTML='<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>'
  +'<div class="errmsg">'+esc(msg)+'</div><button class="retry" id="retryBtn">Reintentar</button>';
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
 const useVisual=$('useVis').checked&&proj&&pdata&&pdata.refs.length>0;
 if(mode==='editar'&&refs.length===0&&!useVisual){toast('Sube una imagen (o activa memoria visual)','bad');return}
 if(mask&&refs.length===0&&useVisual)toast('Ojo: la máscara se aplicará a la primera referencia del proyecto');
 $('resbar').classList.add('hide');$('strip').classList.add('hide');showState('spin');
 $('go').disabled=true;const prevTxt=$('goTxt').textContent;$('goTxt').textContent='Generando…';
 const body={prompt,size:$('w').value+'x'+$('h').value,quality:$('quality').value,n:+$('n').value,
  output_format:$('fmt').value,background:$('bg').value,moderation:$('mod').value,project:proj,
  save_desktop:$('saveDesk').checked};
 if($('fmt').value!=='png')body.output_compression=+$('comp').value;
 let url='/generate';const willEdit=mode==='editar'||useVisual||refs.length>0;
 const refsUsed=refs.map(r=>({name:r.name,b64:r.b64}));
 if(willEdit){url='/edit';body.images=refs;if(mask)body.mask=mask;body.use_project_refs=useVisual}
 try{
  const d=await(await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.error){err(d.error)}
  else{
   results=d.images&&d.images.length?d.images:[{image:d.image}];
   lastResult={prompt,refsUsed,fmt:$('fmt').value};
   renderStrip();showResult(0);
   $('resbar').classList.remove('hide');
   bumpSess(d.cost||0,results.length);
   $('cost').innerHTML='<b>$'+(d.cost||0).toFixed(4)+'</b> · '+(d.output_tokens||0)+' tok'
    +(results.length>1?' · '+results.length+' imágenes':'')
    +(d.via_visual?' · memoria visual':'')+(d.model_used==='gpt-image-1'?' · transparente':'');
   loadGal()}
 }catch(e){err(e)}
 $('goTxt').textContent=prevTxt;validate();
}
$('go').onclick=run;$('again').onclick=run;

function flash(el){const c=el.style.color;el.style.color='var(--accent)';setTimeout(()=>el.style.color=c,650)}
$('fCopy').onclick=()=>{if(!lastResult)return;$('prompt').value=lastResult.prompt;refs=lastResult.refsUsed.map(r=>({name:r.name,b64:r.b64}));renderThumbs();try{navigator.clipboard.writeText(lastResult.prompt)}catch(e){}flash($('fCopy'));toast('Prompt y referencias restauradas')};
$('fAdd').onclick=()=>{if(!results.length)return;refs.push({name:'generada.png',b64:results[active].image.split(',')[1]});renderThumbs();flash($('fAdd'));toast('Añadida como referencia')};

// ===== audio: voz (TTS) y transcripción =====
const VOICES=['alloy','ash','ballad','coral','echo','fable','onyx','nova','sage','shimmer','verse'];
let selVoice=localStorage.getItem('studio_voice')||'nova';
if(!VOICES.includes(selVoice))selVoice='nova';
$('voices').innerHTML=VOICES.map(v=>`<span class="chip vchip${v===selVoice?' on':''}" data-v="${v}">${v}</span>`).join('');
$('voices').onclick=e=>{const c=e.target.closest('.vchip');if(!c)return;
 selVoice=c.dataset.v;localStorage.setItem('studio_voice',selVoice);
 [...$('voices').children].forEach(x=>x.classList.toggle('on',x.dataset.v===selVoice))};
function audTab(t){['audTTS','audSTT','audSFX'].forEach(id=>$(id).classList.toggle('on',id===t));
 $('ttsBox').classList.toggle('hide',t!=='audTTS');
 $('sttBox').classList.toggle('hide',t!=='audSTT');
 $('sfxBox').classList.toggle('hide',t!=='audSFX')}
$('audTTS').onclick=()=>audTab('audTTS');
$('audSTT').onclick=()=>audTab('audSTT');
$('audSFX').onclick=()=>audTab('audSFX');
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
function renderAud(){const items=hist.filter(it=>['tts','stt','sfx'].includes(it.kind));
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

// ===== atajos de teclado =====
document.addEventListener('keydown',e=>{
 if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();
  if(mode==='audio'){
   if(!$('sttBox').classList.contains('hide'))runSTT();
   else if(!$('sfxBox').classList.contains('hide'))runSFX();
   else runTTS()}
  else if(!$('go').disabled)run();return}
 if(e.key==='Escape'){
  if(!$('lightbox').classList.contains('hide')){$('lightbox').classList.add('hide');return}
  if(!$('maskModal').classList.contains('hide')){$('maskModal').classList.add('hide');return}
  if(!$('keyModal').classList.contains('hide')){$('keyModal').classList.add('hide');return}}
 const tag=document.activeElement.tagName;
 if(tag==='TEXTAREA'||tag==='INPUT'||tag==='SELECT')return;
 if(e.key==='1')setMode('crear');
 if(e.key==='2')setMode('editar');
 if(e.key==='3')setMode('audio')});

// miniaturas de proporción en los presets
function buildMinis(){document.querySelectorAll('.chip[data-w]').forEach(c=>{const W=+c.dataset.w,H=+c.dataset.h,m=14;
 let bw,bh;if(W>=H){bw=m;bh=Math.max(3,Math.round(m*H/W))}else{bh=m;bw=Math.max(3,Math.round(m*W/H))}
 const s=document.createElement('span');s.className='mini';s.style.width=bw+'px';s.style.height=bh+'px';c.insertBefore(s,c.firstChild)})}
buildMinis();validate();loadProjects();loadGal();loadConfig();checkKey();setProv(prov);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, o, code=200):
        self._send(code, json.dumps(o, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, HTML, "text/html; charset=utf-8")
        if self.path == "/keystatus":
            return self._json({"ok": bool(key())})
        if self.path == "/history":
            return self._json(load_json(HIST_JSON, []))
        if self.path == "/projects":
            return self._json(load_projects())
        if self.path == "/config":
            conf = load_json(CONF_JSON, {})
            return self._json({"save_dir": conf.get("save_dir", ""),
                               "effective": str(save_dir()).replace(str(HOME), "~"),
                               "voice_styles": conf.get("voice_styles", [])})
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
            fp = HIST_DIR / os.path.basename(name)
            ctype = MIME.get(fp.suffix.lstrip(".").lower(), "application/octet-stream")
            return self._send(200, fp.read_bytes(), ctype) if fp.exists() else self._send(404, "no", "text/plain")
        if self.path.startswith("/pfile?"):
            q = parse_qs(urlparse(self.path).query)
            fp = proj_folder(q.get("project", [""])[0]) / os.path.basename(q.get("name", [""])[0])
            return self._send(200, fp.read_bytes(), f"image/{fp.suffix.lstrip('.') or 'png'}") if fp.exists() else self._send(404, "no", "text/plain")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        try:
            h = {"/setkey": self.h_setkey, "/generate": self.h_generate, "/edit": self.h_edit,
                 "/project": self.h_project, "/projectdel": self.h_projectdel, "/projectref": self.h_projectref,
                 "/projectrefdel": self.h_projectrefdel, "/distill": self.h_distill,
                 "/historydel": self.h_historydel, "/config": self.h_config,
                 "/speech": self.h_speech, "/transcribe": self.h_transcribe,
                 "/elkey": self.h_elkey, "/elspeech": self.h_elspeech,
                 "/elsfx": self.h_elsfx, "/elclone": self.h_elclone}.get(self.path)
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
        pr = load_projects()
        cur = pr.get(b["name"], {"style": "", "refs": []})
        if "style" in b:  # solo pisa el estilo si la petición lo trae (crear ≠ guardar)
            cur["style"] = b["style"]
            try:
                (proj_folder(b["name"]) / "estilo.md").write_text(b["style"])
            except Exception:
                pass
        pr[b["name"]] = cur
        save_json(PROJ_JSON, pr)
        return self._json({"ok": True})

    def h_projectdel(self):
        name = self._body().get("name", "")
        pr = load_projects()
        if name in pr:
            del pr[name]
            save_json(PROJ_JSON, pr)
        try:
            shutil.rmtree(PROJ_DIR / safe(name))
        except Exception:
            pass
        return self._json({"ok": True})

    def h_projectref(self):
        b = self._body()
        pr = load_projects()
        name, img = b["project"], b["image"]
        fn = f"ref_{uuid.uuid4().hex[:8]}_{safe(img.get('name','ref'))}"
        if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            fn += ".png"
        (proj_folder(name) / fn).write_bytes(base64.b64decode(img["b64"]))
        cur = pr.get(name, {"style": "", "refs": []})
        cur.setdefault("refs", []).append(fn)
        pr[name] = cur
        save_json(PROJ_JSON, pr)
        return self._json({"ok": True, "file": fn})

    def h_projectrefdel(self):
        b = self._body()
        pr = load_projects()
        f = os.path.basename(b["file"])
        try:
            (proj_folder(b["project"]) / f).unlink()
        except Exception:
            pass
        if b["project"] in pr:
            pr[b["project"]]["refs"] = [x for x in pr[b["project"]].get("refs", []) if x != f]
            save_json(PROJ_JSON, pr)
        return self._json({"ok": True})

    def h_config(self):
        b = self._body()
        conf = load_json(CONF_JSON, {})
        if "save_dir" in b:
            raw = (b.get("save_dir") or "").strip()
            if raw:
                p = Path(os.path.expanduser(raw))
                try:
                    p.mkdir(parents=True, exist_ok=True)
                    t = p / ".studio_test"
                    t.write_text("")
                    t.unlink()
                except Exception as e:
                    return self._json({"error": f"No puedo escribir en esa carpeta: {e}"})
            conf["save_dir"] = raw
        if "voice_styles" in b and isinstance(b["voice_styles"], list):
            conf["voice_styles"] = b["voice_styles"][:50]
        save_json(CONF_JSON, conf)
        return self._json({"ok": True, "effective": str(save_dir()).replace(str(HOME), "~")})

    def h_historydel(self):
        f = os.path.basename(self._body().get("file", ""))
        if not f:
            return self._json({"error": "Falta el archivo"})
        h = load_json(HIST_JSON, [])
        save_json(HIST_JSON, [x for x in h if x.get("file") != f])
        try:
            (HIST_DIR / f).unlink()
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
        total = round(out_t * PRICE_OUT / 1e6 + in_t * PRICE_IN / 1e6, 5)
        items = data.get("data", [])
        per = round(total / max(1, len(items)), 5)
        images = []
        for d in items:
            b64 = d["b64_json"]
            raw = base64.b64decode(b64)
            name = f"img_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.{ext}"
            (HIST_DIR / name).write_bytes(raw)
            if meta.get("save_desktop", True):
                try:
                    d = save_dir()
                    d.mkdir(parents=True, exist_ok=True)
                    (d / name).write_bytes(raw)
                except Exception:
                    pass
            add_history({"file": name, "prompt": meta["prompt"], "size": meta["size"],
                         "quality": meta["quality"], "mode": meta["mode"], "cost": per,
                         "output_tokens": out_t, "ts": time.strftime("%Y-%m-%d %H:%M"), "project": meta.get("project", "")})
            images.append({"image": f"data:{mime};base64," + b64, "file": name, "cost": per})
        first = images[0]["image"] if images else ""
        return {"images": images, "image": first, "cost": total, "output_tokens": out_t,
                "via_visual": via_visual, "model_used": model_used}

    def h_generate(self):
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API (botón API)."})
        bg = b.get("background", "auto")
        transparent = bg == "transparent"
        model = "gpt-image-1" if transparent else "gpt-image-2"
        size = g1_size(b.get("size", "1024x1024")) if transparent else b.get("size", "1536x1024")
        fmt = b.get("output_format", "png")
        if transparent and fmt == "jpeg":
            fmt = "png"
        prompt = self._style_prefix(b.get("project")) + b.get("prompt", "")
        payload = {"model": model, "prompt": prompt, "size": size, "quality": b.get("quality", "auto"),
                   "n": b.get("n", 1), "output_format": fmt, "background": bg, "moderation": b.get("moderation", "auto")}
        if b.get("output_compression") is not None and fmt != "png":
            payload["output_compression"] = b["output_compression"]
        try:
            with urllib.request.urlopen(urllib.request.Request(API_GEN, data=json.dumps(payload).encode(),
                    headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"}), timeout=240) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
        meta = {"prompt": b.get("prompt", ""), "size": size, "quality": payload["quality"],
                "mode": "crear", "output_format": fmt, "project": b.get("project", ""),
                "save_desktop": b.get("save_desktop", True)}
        return self._json(self._save_results(data, meta, model_used=model))

    def h_edit(self):
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API (botón API)."})
        bg = b.get("background", "auto")
        transparent = bg == "transparent"
        model = "gpt-image-1" if transparent else "gpt-image-2"
        size = g1_size(b.get("size", "1024x1024")) if transparent else b.get("size", "1024x1024")
        fmt = b.get("output_format", "png")
        if transparent and fmt == "jpeg":
            fmt = "png"
        prompt = self._style_prefix(b.get("project")) + b.get("prompt", "")
        boundary = "----studio" + uuid.uuid4().hex
        parts = []

        def field(n, v):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode())

        def filepart(n, fn, raw):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"; filename="{fn}"\r\nContent-Type: image/png\r\n\r\n'.encode() + raw + b"\r\n")

        field("model", model)
        field("prompt", prompt)
        field("size", size)
        field("quality", b.get("quality", "auto"))
        field("n", str(b.get("n", 1)))
        field("output_format", fmt)
        field("moderation", b.get("moderation", "auto"))
        if bg in ("opaque", "transparent"):
            field("background", bg)
        if b.get("output_compression") is not None and fmt != "png":
            field("output_compression", str(b["output_compression"]))
        nimg = 0
        for img in b.get("images", []):
            filepart("image[]", img.get("name", "ref.png"), base64.b64decode(img["b64"]))
            nimg += 1
        via_visual = False
        if b.get("use_project_refs") and b.get("project"):
            for f in load_projects().get(b["project"], {}).get("refs", []):
                fp = proj_folder(b["project"]) / f
                if fp.exists():
                    filepart("image[]", f, fp.read_bytes())
                    nimg += 1
                    via_visual = True
        if nimg == 0:
            return self._json({"error": "No hay imágenes de referencia."})
        if b.get("mask"):
            filepart("mask", b["mask"].get("name", "mask.png"), base64.b64decode(b["mask"]["b64"]))
        parts.append(f"--{boundary}--\r\n".encode())
        try:
            with urllib.request.urlopen(urllib.request.Request(API_EDIT, data=b"".join(parts),
                    headers={"Authorization": f"Bearer {key()}", "Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=300) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._json({"error": self._err(e)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Sin conexión con OpenAI: {e.reason}"})
        meta = {"prompt": b.get("prompt", ""), "size": size, "quality": b.get("quality", "auto"),
                "mode": "editar", "output_format": fmt, "project": b.get("project", ""),
                "save_desktop": b.get("save_desktop", True)}
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
        (HIST_DIR / name).write_bytes(raw)
        if b.get("save_desktop", True):
            try:
                d = save_dir()
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
        fn = b.get("name", "audio.mp3")
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
        (HIST_DIR / name).write_text(raw)
        if b.get("save_desktop", True):
            try:
                d = save_dir()
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
        (HIST_DIR / name).write_bytes(raw)
        if save_desktop:
            try:
                d = save_dir()
                d.mkdir(parents=True, exist_ok=True)
                (d / name).write_bytes(raw)
            except Exception:
                pass
        hist_item["file"] = name
        add_history(hist_item)
        return name

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
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{f.get("name","muestra.mp3")}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
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

    def h_distill(self):
        b = self._body()
        if not key():
            return self._json({"error": "Conecta tu API (botón API)."})
        project = b.get("project", "")
        prompts = [h["prompt"] for h in load_json(HIST_JSON, []) if h.get("project") == project and h.get("prompt")][:40]
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
            return json.loads(e.read()).get("error", {}).get("message", f"HTTP {e.code}")
        except Exception:
            return f"HTTP {e.code}"


if __name__ == "__main__":
    print(f"Estudio v4 en  http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
