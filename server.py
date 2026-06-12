#!/usr/bin/env python3
"""
Estudio v3 — gpt-image-2 / gpt-image-1 (OpenAI) · app independiente
UI premium minimalista. Crear + Editar, referencias en ambos, memoria visual por
proyecto, historial, estimador de precio, moderación, transparente (gpt-image-1),
presets completos incl. anamórficos. Sin dependencias: solo Python 3.
"""
import json, base64, os, re, time, uuid, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = 7860
HOME = Path.home()
KEY_FILE = HOME / ".openai_key"
ROOT = HOME / "image-studio"
HIST_DIR = ROOT / "historial"
HIST_JSON = ROOT / "historial.json"
PROJ_JSON = ROOT / "proyectos.json"
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


def key():
    return KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""


def load_json(p, d):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d


def save_json(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))


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
        out[k] = {"style": v, "refs": []} if isinstance(v, str) else {"style": v.get("style", ""), "refs": v.get("refs", [])}
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


def g1_size(size):
    try:
        w, h = map(int, size.split("x"))
    except Exception:
        return "1024x1024"
    r = w / h
    return "1536x1024" if r > 1.2 else "1024x1536" if r < 0.83 else "1024x1024"


HTML = r"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
 --bg:#0a0a0b;--surface:#101012;--surface2:#161618;--elev:#1c1c1f;
 --line:rgba(255,255,255,.06);--line2:rgba(255,255,255,.11);
 --txt:#ededee;--mut:#8c8c93;--faint:#5d5d65;
 --accent:#e0a571;--accent-dim:rgba(224,165,113,.14);--ok:#7bd99a;--bad:#e57373;
 --ui:'Schibsted Grotesk',-apple-system,sans-serif;--mono:'Geist Mono',ui-monospace,monospace;
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

/* top bar */
.top{display:flex;align-items:center;gap:18px;padding:15px 26px;border-bottom:1px solid var(--line);
 position:sticky;top:0;z-index:5;background:rgba(10,10,11,.82);backdrop-filter:blur(14px)}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;letter-spacing:.02em}
.brand .dot{width:22px;height:22px;border-radius:7px;background:linear-gradient(140deg,var(--accent),#b87a45);
 display:flex;align-items:center;justify-content:center;color:#1a1206}
.brand .dot svg{width:13px;height:13px;stroke-width:2}
.seg{display:flex;background:var(--surface2);border:1px solid var(--line);border-radius:11px;padding:3px;gap:2px}
.seg button{display:flex;align-items:center;gap:7px;background:transparent;border:0;color:var(--mut);
 padding:7px 15px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;transition:.18s}
.seg button:hover{color:var(--txt)}
.seg button.on{background:var(--elev);color:var(--txt);box-shadow:0 1px 0 rgba(255,255,255,.04) inset}
.top .right{margin-left:auto;display:flex;align-items:center;gap:14px}
.sess{font-size:12px;color:var(--mut)}.sess b{color:var(--txt);font-weight:500}
.ghost{display:flex;align-items:center;gap:7px;background:transparent;border:1px solid var(--line2);color:var(--mut);
 border-radius:9px;padding:7px 12px;font-size:12px;cursor:pointer;transition:.18s}
.ghost:hover{color:var(--txt);border-color:var(--mut)}

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

.slabel{display:flex;justify-content:space-between;align-items:baseline}
.slabel .v{font-family:var(--mono);font-size:13px;color:var(--txt)}
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
.canvas:hover .floaters{opacity:1;transform:none}
.fbtn{width:34px;height:34px;border-radius:9px;background:rgba(16,16,18,.82);backdrop-filter:blur(8px);border:1px solid var(--line2);
 color:var(--txt);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:.15s;text-decoration:none}
.fbtn:hover{background:var(--elev);border-color:var(--mut)}.fbtn svg{width:16px;height:16px}
.lightbox{position:fixed;inset:0;background:rgba(5,5,6,.93);backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;z-index:40;cursor:zoom-out;padding:30px}
.lightbox img{max-width:94vw;max-height:92vh;border-radius:8px;box-shadow:0 30px 90px rgba(0,0,0,.7)}
.mini{display:inline-block;border:1px solid currentColor;border-radius:1.5px;opacity:.65;flex:none}
.empty{color:var(--faint);font-size:13px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:12px}
.empty svg{width:30px;height:30px;stroke-width:1.3;opacity:.6}
.spin{width:34px;height:34px;border:2.5px solid var(--line2);border-top-color:var(--accent);border-radius:50%;animation:sp .8s linear infinite}@keyframes sp{to{transform:rotate(360deg)}}
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
#style{min-height:74px;font-size:12px}
.gal{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.gcard{position:relative;border:1px solid var(--line);border-radius:10px;overflow:hidden;cursor:pointer;background:var(--surface);transition:.16s}
.gcard:hover{border-color:var(--line2)}
.gcard img{width:100%;aspect-ratio:1/1;object-fit:cover;display:block}
.gcard .c{font-family:var(--mono);font-size:9.5px;color:var(--faint);padding:5px 6px;display:flex;justify-content:space-between}
.gfloat{position:absolute;top:5px;right:5px;display:flex;gap:4px;opacity:0;transform:translateY(-3px);transition:.15s}
.gcard:hover .gfloat{opacity:1;transform:none}
.gfbtn{width:25px;height:25px;border-radius:7px;background:rgba(12,12,14,.86);backdrop-filter:blur(6px);border:1px solid var(--line2);
 color:var(--txt);display:flex;align-items:center;justify-content:center;cursor:pointer;text-decoration:none}
.gfbtn:hover{background:var(--elev);border-color:var(--mut)}.gfbtn svg{width:12px;height:12px;stroke-width:1.8}

/* modal */
.overlay{position:fixed;inset:0;background:rgba(5,5,6,.78);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;z-index:30}
.modal{background:var(--surface);border:1px solid var(--line2);border-radius:18px;padding:30px;max-width:440px;width:92%;
 box-shadow:0 30px 80px rgba(0,0,0,.6)}
.modal .ic{width:42px;height:42px;border-radius:12px;background:var(--accent-dim);display:flex;align-items:center;justify-content:center;color:var(--accent);margin-bottom:16px}
.modal h2{margin:0 0 7px;font-size:19px;font-weight:600}
.modal p{color:var(--mut);font-size:13px;margin:0 0 18px;line-height:1.55}.modal a{color:var(--accent)}
.modal input{margin-bottom:8px}.kmsg{font-size:12px;color:var(--mut);min-height:16px;margin-bottom:12px}
.hide{display:none!important}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:var(--line2);border-radius:9px;border:2px solid var(--bg)}
</style></head><body>

<div class="overlay hide" id="keyModal"><div class="modal">
  <div class="ic"><svg viewBox="0 0 24 24"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg></div>
  <h2>Conecta tu API de OpenAI</h2>
  <p>Pega tu clave para empezar. Se guarda solo en tu equipo (<span class="mono">~/.openai_key</span>) y nunca sale de aquí. Consíguela en <a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com</a>.</p>
  <input type="password" id="keyInput" placeholder="sk-proj-…" autocomplete="off">
  <div class="kmsg" id="keyMsg"></div>
  <button class="primary" id="keySave">Conectar</button>
</div></div>

<div class="top">
  <div class="brand"><span class="dot"><svg viewBox="0 0 24 24"><path d="M12 3l1.9 5.6L19.5 10l-4.6 3.3L16.5 19 12 15.7 7.5 19l1.6-5.7L4.5 10l5.6-1.4z"/></svg></span>Studio</div>
  <div class="seg">
    <button id="mCrear" class="on"><svg viewBox="0 0 24 24"><path d="M12 3l1.9 5.6L19.5 10l-4.6 3.3L16.5 19 12 15.7 7.5 19l1.6-5.7L4.5 10l5.6-1.4z"/></svg>Crear</button>
    <button id="mEditar"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 15l5-5 4 4 3-3 6 6"/><circle cx="9" cy="9" r="1.4"/></svg>Editar</button>
  </div>
  <div class="right">
    <span class="sess" id="sessTot">Sesión <b class="mono">$0.0000</b> · <b class="mono">0</b></span>
    <button class="ghost" id="cfgBtn"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-2.82 1.17V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 8 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 3.6 15H3.5a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 5 9.4l-.06-.07a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9.6 3.6V3.5a2 2 0 0 1 4 0v.09A1.65 1.65 0 0 0 16 5.6l.07-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 20.4 11h.1a2 2 0 0 1 0 4h-.1z"/></svg>API</button>
  </div>
</div>

<div class="wrap">
  <!-- IZQUIERDA -->
  <div class="col an">
    <div class="field" id="editBox">
      <label><span id="refLbl">Referencias · opcional</span></label>
      <div class="drop" id="drop"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>Arrastra o elige imágenes</div>
      <input type="file" id="files" accept="image/png,image/jpeg,image/webp" multiple class="hide">
      <div class="thumbs" id="thumbs"></div>
      <div class="thumbs" id="maskThumb"></div>
      <div class="drop" id="dropMask" style="padding:9px;margin-top:9px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>Máscara inpainting · opcional</div>
      <input type="file" id="maskFile" accept="image/png" class="hide">
    </div>

    <div class="field">
      <label id="lblPrompt">Prompt</label>
      <textarea id="prompt" placeholder="Describe lo que imaginas…"></textarea>
    </div>

    <div class="field">
      <div class="slabel"><label>Ancho</label><span class="v" id="wv">1536</span></div>
      <input type="range" id="w" min="512" max="3840" step="16" value="1536">
      <div class="slabel" style="margin-top:6px"><label>Alto</label><span class="v" id="hv">1024</span></div>
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
        <span class="chip" data-w="3840" data-h="1600">2.4:1 4K</span>
        <span class="pgroup">Alta resolución</span>
        <span class="chip" data-w="2560" data-h="1440">2K</span>
        <span class="chip" data-w="3840" data-h="2160">4K</span>
        <span class="chip" data-w="3840" data-h="1280">Pano 3:1</span>
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
        <p class="hint">Transparente usa <span class="mono">gpt-image-1</span> (tamaño fijo). Moderación <b>low</b> es el mínimo de OpenAI; no es "sin censura".</p>
      </div>
    </details>

    <div class="meta"><span class="mono" id="ratio">3:2</span><span class="valid ok" id="valid">válido</span></div>
    <div class="estbar"><span>Costo estimado</span><span class="num" id="estv">~$0.00</span></div>
    <button class="primary" id="go"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg><span id="goTxt">Generar</span></button>
    <p class="hint">Lado 512–3840 · múltiplos de 16 · ≥0.8 MP. El estimado es aproximado; el costo real aparece al terminar. Se guarda en historial y Escritorio.</p>
  </div>

  <!-- CENTRO -->
  <div class="col mid an">
    <div class="canvas" id="canvas">
      <div class="empty" id="emptyState"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg>Tu imagen aparecerá aquí</div>
      <div class="spin hide" id="spinner"></div>
      <img class="result hide" id="resultImg" alt="">
      <div class="floaters hide" id="floaters">
        <button class="fbtn" id="fCopy" title="Copiar prompt + referencias usadas"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
        <button class="fbtn" id="fAdd" title="Usar como referencia"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button>
        <a class="fbtn" id="fDl" title="Descargar" download><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg></a>
      </div>
    </div>
    <div class="resbar hide" id="resbar">
      <span class="costtag" id="cost"></span>
      <div class="acts"><a id="dl" download="imagen.png"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>Descargar</a>
      <button id="again"><svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>Otra</button></div>
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
      </div>
      <label style="margin-top:14px">estilo.md · texto</label>
      <textarea id="style" placeholder="Estilo: técnica, paleta, luz, mood…"></textarea>
      <div class="btnrow"><button id="saveProj"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>Guardar estilo</button></div>
      <label style="margin-top:14px">Memoria visual · referencias</label>
      <div class="drop" id="dropPref" style="padding:10px;font-size:11.5px"><svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M12 5v14M5 12h14"/></svg>Añadir referencia</div>
      <input type="file" id="prefFile" accept="image/png,image/jpeg,image/webp" multiple class="hide">
      <div class="thumbs" id="prefThumbs"></div>
      <label class="check" style="margin-top:10px"><input type="checkbox" id="useVis" checked> Usar memoria visual al crear</label>
    </div>
    <div class="sec">
      <h3 class="eyebrow"><svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l3 2"/></svg>Historial</h3>
      <div class="gal" id="gal"></div>
    </div>
  </div>
</div>

<div class="lightbox hide" id="lightbox"><img id="lbImg" src="" alt=""></div>
<script>
const $=id=>document.getElementById(id);
let mode='crear',refs=[],mask=null,sessCost=0,sessN=0,ratio=1.5,projects={};

async function checkKey(){const r=await(await fetch('/keystatus')).json();if(!r.ok)$('keyModal').classList.remove('hide');return r.ok}
$('cfgBtn').onclick=()=>$('keyModal').classList.remove('hide');
$('keySave').onclick=async()=>{const k=$('keyInput').value.trim();if(!k)return;$('keyMsg').textContent='Validando…';
 const r=await(await fetch('/setkey',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})})).json();
 if(r.ok){$('keyMsg').textContent='Conectada ✓';$('keyModal').classList.add('hide')}else{$('keyMsg').textContent=(r.error||'clave inválida')}};

function setMode(m){mode=m;$('mCrear').classList.toggle('on',m==='crear');$('mEditar').classList.toggle('on',m==='editar');
 $('lblPrompt').textContent=m==='editar'?'Instrucción de edición':'Prompt';
 $('refLbl').textContent=m==='editar'?'Imágenes a editar / combinar':'Referencias · opcional';
 $('goTxt').textContent=m==='editar'?'Editar':'Generar'}
$('mCrear').onclick=()=>setMode('crear');$('mEditar').onclick=()=>setMode('editar');

function gcd(a,b){return b?gcd(b,a%b):a}function fr(a,b){const g=gcd(a,b);return(a/g)+':'+(b/g)}
function snap(v){return Math.round(v/16)*16}
function estTokens(){const W=+$('w').value,H=+$('h').value,MP=W*H/1e6,q=$('quality').value;
 let t;if(q==='low'||q==='auto')t=129+64*MP;else if(q==='medium')t=1150+577*MP;else t=4600+2308*MP;return Math.max(80,Math.round(t))}
function validate(){const W=+$('w').value,H=+$('h').value,long=Math.max(W,H),mp=W*H;let ok=true,msg='válido';
 if(long>3840){ok=false;msg='lado > 3840'}else if(mp<800000){ok=false;msg='muy pequeña'}
 $('valid').textContent=msg;$('valid').className='valid '+(ok?'ok':'bad');$('ratio').textContent=fr(W,H);
 const n=+$('n').value,est=estTokens()*n*30/1e6;$('estv').textContent='~$'+est.toFixed(est<0.1?4:3)+(n>1?' ×'+n:'');$('go').disabled=!ok}
$('w').oninput=()=>{if($('lock').checked){$('h').value=snap(Math.min(3840,Math.max(512,$('w').value/ratio)));$('hv').textContent=$('h').value}$('wv').textContent=$('w').value;validate()};
$('h').oninput=()=>{if($('lock').checked){$('w').value=snap(Math.min(3840,Math.max(512,$('h').value*ratio)));$('wv').textContent=$('w').value}$('hv').textContent=$('h').value;validate()};
$('lock').onchange=()=>ratio=$('w').value/$('h').value;
$('presets').onclick=e=>{const c=e.target.closest('.chip');if(!c)return;[...document.querySelectorAll('.chip')].forEach(x=>x.classList.remove('on'));c.classList.add('on');
 $('w').value=c.dataset.w;$('h').value=c.dataset.h;$('wv').textContent=c.dataset.w;$('hv').textContent=c.dataset.h;ratio=c.dataset.w/c.dataset.h;validate()};
$('quality').onchange=validate;$('n').onchange=validate;
$('fmt').onchange=()=>$('compBox').classList.toggle('hide',$('fmt').value==='png');
$('comp').oninput=()=>$('compv').textContent=$('comp').value+'%';

function fileToB64(f){return new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(f)})}
function xicon(){return '<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>'}
function renderThumbs(){$('thumbs').innerHTML=refs.map((r,i)=>`<div class="thumb"><img src="data:image/png;base64,${r.b64}"><button class="x" data-i="${i}">${xicon()}</button></div>`).join('')}
async function addFiles(list){for(const f of list){if(f.size>50*1024*1024){alert(f.name+' supera 50MB');continue}refs.push({name:f.name,b64:await fileToB64(f)})}renderThumbs()}
$('drop').onclick=()=>$('files').click();$('files').onchange=e=>addFiles(e.target.files);
$('thumbs').onclick=e=>{const b=e.target.closest('.x');if(b){refs.splice(+b.dataset.i,1);renderThumbs()}};
['dragover','dragenter'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.add('hot')}));
['dragleave','drop'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.remove('hot')}));
$('drop').addEventListener('drop',e=>addFiles(e.dataTransfer.files));
$('dropMask').onclick=()=>$('maskFile').click();
$('maskFile').onchange=async e=>{const f=e.target.files[0];if(!f)return;mask={name:f.name,b64:await fileToB64(f)};
 $('maskThumb').innerHTML=`<div class="thumb"><img src="data:image/png;base64,${mask.b64}"><button class="x" id="mx">${xicon()}</button></div>`;$('mx').onclick=()=>{mask=null;$('maskThumb').innerHTML=''}};

async function loadProjects(){projects=await(await fetch('/projects')).json();const s=$('projSel'),cur=s.value;
 s.innerHTML='<option value="">Sin proyecto</option>'+Object.keys(projects).map(n=>`<option ${n===cur?'selected':''}>${n}</option>`).join('');renderProj()}
function renderProj(){const n=$('projSel').value,p=projects[n];$('style').value=p?p.style:'';
 $('prefThumbs').innerHTML=p?p.refs.map(f=>`<div class="thumb"><img src="/pfile?project=${encodeURIComponent(n)}&name=${encodeURIComponent(f)}"><button class="x" data-f="${f}">${xicon()}</button></div>`).join(''):''}
$('projSel').onchange=renderProj;
$('newProj').onclick=async()=>{const n=prompt('Nombre del proyecto:');if(!n)return;await fetch('/project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,style:''})});await loadProjects();$('projSel').value=n;renderProj()};
$('saveProj').onclick=async()=>{const n=$('projSel').value;if(!n){alert('Elige o crea un proyecto');return}await fetch('/project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,style:$('style').value})});projects[n].style=$('style').value;$('saveProj').innerHTML='Guardado ✓';setTimeout(()=>loadProjects(),900)};
$('distill').onclick=async()=>{const n=$('projSel').value;if(!n){alert('Elige un proyecto');return}$('distill').textContent='…';
 const r=await(await fetch('/distill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:n})})).json();$('distill').innerHTML='Destilar';
 if(r.error){alert(r.error);return}$('style').value=r.style};
$('dropPref').onclick=()=>$('prefFile').click();
$('prefFile').onchange=async e=>{const n=$('projSel').value;if(!n){alert('Elige un proyecto primero');return}
 for(const f of e.target.files){const b64=await fileToB64(f);await fetch('/projectref',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:n,image:{name:f.name,b64}})})}await loadProjects()};
$('prefThumbs').onclick=async e=>{const b=e.target.closest('.x');if(!b)return;const n=$('projSel').value;
 await fetch('/projectrefdel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:n,file:b.dataset.f})});await loadProjects()};

const GDL='<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>';
const GCP='<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const GPL='<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>';
async function loadGal(){const h=await(await fetch('/history')).json();
 $('gal').innerHTML=h.slice(0,30).map(it=>{const fn=encodeURIComponent(it.file),p=(it.prompt||'').replace(/"/g,'&quot;');
  return `<div class="gcard" data-file="${it.file}" data-p="${p}"><img src="/file?name=${fn}">
   <div class="gfloat"><a class="gfbtn" href="/file?name=${fn}" download="${it.file}" title="Descargar">${GDL}</a>
   <button class="gfbtn gcopy" title="Copiar prompt">${GCP}</button>
   <button class="gfbtn gref" title="Usar como referencia">${GPL}</button></div>
   <div class="c"><span>$${(it.cost||0).toFixed(4)}</span><span>${it.size}</span></div></div>`}).join('')||'<div class="hint">Aún no hay imágenes</div>'}
function blobToB64(b){return new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(',')[1]);fr.readAsDataURL(b)})}
$('gal').onclick=async e=>{
 if(e.target.closest('a'))return;
 const cp=e.target.closest('.gcopy'),rf=e.target.closest('.gref'),card=e.target.closest('.gcard');
 if(cp){$('prompt').value=card.dataset.p;try{navigator.clipboard.writeText(card.dataset.p)}catch(x){}flash(cp);return}
 if(rf){const b=await(await fetch('/file?name='+encodeURIComponent(card.dataset.file))).blob();refs.push({name:card.dataset.file,b64:await blobToB64(b)});renderThumbs();flash(rf);return}
 if(card)$('prompt').value=card.dataset.p};

let lastResult=null;
function showState(s){$('emptyState').classList.toggle('hide',s!=='empty');$('spinner').classList.toggle('hide',s!=='spin');
 $('resultImg').classList.toggle('hide',s!=='result');$('floaters').classList.toggle('hide',s!=='result')}
function err(msg){$('emptyState').innerHTML='<span style="color:var(--bad)">'+msg+'</span>';showState('empty')}
async function run(){
 const prompt=$('prompt').value.trim();if(!prompt){alert('Escribe el prompt');return}
 const proj=$('projSel').value,pdata=projects[proj];
 const useVisual=$('useVis').checked&&proj&&pdata&&pdata.refs.length>0;
 if(mode==='editar'&&refs.length===0&&!useVisual){alert('Sube una imagen (o activa memoria visual)');return}
 $('resbar').classList.add('hide');showState('spin');$('go').disabled=true;
 const body={prompt,size:$('w').value+'x'+$('h').value,quality:$('quality').value,n:+$('n').value,
  output_format:$('fmt').value,background:$('bg').value,moderation:$('mod').value,project:proj};
 if($('fmt').value!=='png')body.output_compression=+$('comp').value;
 let url='/generate';const willEdit=mode==='editar'||useVisual||refs.length>0;
 const refsUsed=refs.map(r=>({name:r.name,b64:r.b64}));
 if(willEdit){url='/edit';body.images=refs;if(mask)body.mask=mask;body.use_project_refs=useVisual}
 try{
  const d=await(await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.error){err(d.error)}
  else{lastResult={prompt,image:d.image,refsUsed,fmt:$('fmt').value};
   $('resultImg').src=d.image;showState('result');
   const fn=(prompt.slice(0,24).replace(/\s+/g,'_')||'imagen')+'.'+$('fmt').value;
   $('fDl').href=d.image;$('fDl').download=fn;$('dl').href=d.image;$('dl').download=fn;
   $('resbar').classList.remove('hide');sessCost+=d.cost||0;sessN++;
   $('cost').innerHTML='<b>$'+(d.cost||0).toFixed(4)+'</b> · '+(d.output_tokens||0)+' tok'+(d.via_visual?' · memoria visual':'')+(d.model_used==='gpt-image-1'?' · transparente':'');
   $('sessTot').innerHTML='Sesión <b class="mono">$'+sessCost.toFixed(4)+'</b> · <b class="mono">'+sessN+'</b>';loadGal()}
 }catch(e){err(e)}
 validate();
}
$('go').onclick=run;$('again').onclick=run;

// ver completa
$('resultImg').onclick=()=>{if(!lastResult)return;$('lbImg').src=lastResult.image;$('lightbox').classList.remove('hide')};
$('lightbox').onclick=()=>$('lightbox').classList.add('hide');
// flotantes
function flash(el){const c=el.style.color;el.style.color='var(--accent)';setTimeout(()=>el.style.color=c,650)}
$('fCopy').onclick=()=>{if(!lastResult)return;$('prompt').value=lastResult.prompt;refs=lastResult.refsUsed.map(r=>({name:r.name,b64:r.b64}));renderThumbs();try{navigator.clipboard.writeText(lastResult.prompt)}catch(e){}flash($('fCopy'))};
$('fAdd').onclick=()=>{if(!lastResult)return;refs.push({name:'generada.png',b64:lastResult.image.split(',')[1]});renderThumbs();flash($('fAdd'))};
// miniaturas de proporción en los presets
function buildMinis(){document.querySelectorAll('.chip[data-w]').forEach(c=>{const W=+c.dataset.w,H=+c.dataset.h,m=14;
 let bw,bh;if(W>=H){bw=m;bh=Math.max(3,Math.round(m*H/W))}else{bh=m;bw=Math.max(3,Math.round(m*W/H))}
 const s=document.createElement('span');s.className='mini';s.style.width=bw+'px';s.style.height=bh+'px';c.insertBefore(s,c.firstChild)})}
buildMinis();validate();loadProjects();loadGal();checkKey();
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
        if self.path.startswith("/file?"):
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            fp = HIST_DIR / os.path.basename(name)
            return self._send(200, fp.read_bytes(), f"image/{fp.suffix.lstrip('.') or 'png'}") if fp.exists() else self._send(404, "no", "text/plain")
        if self.path.startswith("/pfile?"):
            q = parse_qs(urlparse(self.path).query)
            fp = proj_folder(q.get("project", [""])[0]) / os.path.basename(q.get("name", [""])[0])
            return self._send(200, fp.read_bytes(), f"image/{fp.suffix.lstrip('.') or 'png'}") if fp.exists() else self._send(404, "no", "text/plain")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        try:
            h = {"/setkey": self.h_setkey, "/generate": self.h_generate, "/edit": self.h_edit,
                 "/project": self.h_project, "/projectref": self.h_projectref,
                 "/projectrefdel": self.h_projectrefdel, "/distill": self.h_distill}.get(self.path)
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
        cur["style"] = b.get("style", cur.get("style", ""))
        pr[b["name"]] = cur
        save_json(PROJ_JSON, pr)
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

    def _style_prefix(self, project):
        if not project:
            return ""
        st = load_projects().get(project, {}).get("style", "")
        return (st.strip() + "\n\n") if st.strip() else ""

    def _save_and_cost(self, data, b64, meta, via_visual=False, model_used="gpt-image-2"):
        ext = meta.get("output_format", "png")
        name = f"img_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.{ext}"
        (HIST_DIR / name).write_bytes(base64.b64decode(b64))
        try:
            (HOME / "Desktop" / name).write_bytes(base64.b64decode(b64))
        except Exception:
            pass
        u = data.get("usage", {})
        out_t = u.get("output_tokens", 0) or 0
        in_t = u.get("input_tokens", 0) or 0
        cost = round(out_t * PRICE_OUT / 1e6 + in_t * PRICE_IN / 1e6, 5)
        add_history({"file": name, "prompt": meta["prompt"], "size": meta["size"],
                     "quality": meta["quality"], "mode": meta["mode"], "cost": cost,
                     "output_tokens": out_t, "ts": time.strftime("%Y-%m-%d %H:%M"), "project": meta.get("project", "")})
        mime = "image/" + ("jpeg" if ext == "jpeg" else ext)
        return {"image": f"data:{mime};base64," + b64, "cost": cost, "output_tokens": out_t,
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
        meta = {"prompt": b.get("prompt", ""), "size": size, "quality": payload["quality"],
                "mode": "crear", "output_format": fmt, "project": b.get("project", "")}
        return self._json(self._save_and_cost(data, data["data"][0]["b64_json"], meta, model_used=model))

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
        meta = {"prompt": b.get("prompt", ""), "size": size, "quality": b.get("quality", "auto"),
                "mode": "editar", "output_format": fmt, "project": b.get("project", "")}
        return self._json(self._save_and_cost(data, data["data"][0]["b64_json"], meta, via_visual, model))

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
        return self._json({"style": data["choices"][0]["message"]["content"].strip()})

    def _err(self, e):
        try:
            return json.loads(e.read()).get("error", {}).get("message", f"HTTP {e.code}")
        except Exception:
            return f"HTTP {e.code}"


if __name__ == "__main__":
    print(f"Estudio v3 en  http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
