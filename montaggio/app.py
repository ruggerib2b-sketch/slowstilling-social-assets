"""Servizio di montaggio per Slow Stilling.

n8n manda le scene (audio + immagine + testo), qui si monta il video con FFmpeg.
Nessuna AI: solo codice. Endpoint:
  POST /render        avvia un montaggio (risposta subito, lavoro in background)
  GET  /jobs/<id>     stato, capitoli e link ai file
  GET  /download/<id>/<video|srt|capitoli>
  POST /graphic       crea una grafica con un dato (PNG) da usare come immagine di una scena

Novita' (tutte opzionali: senza i nuovi campi il comportamento e' quello di prima):
  jingle_file / jingle_url / jingle_base64   il jingle: file fisso in <dati>/assets, indirizzo web o base64
  intro   {serie, numero, titolo, dopo_scene, durata, logo_file|logo_url}
          scheda con logo e jingle, inserita dopo le prime `dopo_scene` scene (l'aggancio parlato)
  outro   {testo, titolo, durata, logo_file|logo_url}
          schermata finale (5-20 s) con il jingle sotto, lasciando libera la destra per le schede finali
  sottotitoli  true/false   sottotitoli incisi nel video (default true)
  normalizza   true/false   volume uniforme tra le puntate, -16 LUFS (default true)
"""
import base64
import json
import os
import re
import subprocess
import threading
import traceback
import urllib.request
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file
from PIL import Image, ImageDraw, ImageFont

API_KEY = os.environ.get("API_KEY", "")
DATA = Path(os.environ.get("DATA_DIR", "/data"))
ALLOW_FILE_URLS = os.environ.get("ALLOW_FILE_URLS") == "1"  # solo per le prove in locale


def _cerca_font(env, prefisso, ripiego):
    """Font scelto da variabile d'ambiente, altrimenti file <dati>/assets/<prefisso>*.ttf, altrimenti DejaVu."""
    if os.environ.get(env):
        return os.environ[env]
    trovati = sorted((DATA / "assets").glob(prefisso + "*.ttf"))
    return str(trovati[0]) if trovati else ripiego


FONT_BOLD = _cerca_font("FONT_BOLD", "Marcellus", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf")
FONT_REG = _cerca_font("FONT_REG", "CormorantGaramond", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FPS = 25
FORMATI = {"16:9": (1920, 1080), "9:16": (1080, 1920)}
# Colori del marchio Slow Stilling
SFONDO = (13, 12, 10)
CREMA = (236, 231, 216)
ORO = (203, 164, 86)
ORO_SCURO = (143, 107, 42)
GRIGIO = (168, 159, 140)
# Una sola lavorazione alla volta: un server piccolo non regge due rendering insieme.
LOCK = threading.Lock()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024


# ---------- utilità ----------
def richiedi_chiave():
    if not API_KEY:
        abort(500, "API_KEY non impostata sul server")
    if request.headers.get("X-API-Key") != API_KEY:
        abort(401)


def job_dir(job_id):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        abort(404)
    return DATA / job_id


def scrivi_stato(d, **kw):
    f = d / "stato.json"
    stato = json.loads(f.read_text()) if f.exists() else {}
    stato.update(kw)
    f.write_text(json.dumps(stato, ensure_ascii=False))


def scarica(url, dest):
    schemi = ("http://", "https://") + (("file://",) if ALLOW_FILE_URLS else ())
    if not url.startswith(schemi):
        raise ValueError(f"URL non ammesso: {url[:60]}")
    req = urllib.request.Request(url, headers={"User-Agent": "slowstilling-montaggio/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
        while True:
            blocco = r.read(1 << 20)
            if not blocco:
                break
            out.write(blocco)


def esegui(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} errore: {p.stderr[-800:]}")
    return p.stdout


def durata(path):
    out = esegui(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


def tempo_srt(s):
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def tempo_capitolo(s):
    s = int(s)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def spezza_testo(testo, max_parole=8):
    parole = testo.split()
    return [" ".join(parole[i:i + max_parole]) for i in range(0, len(parole), max_parole)] or [""]


def righe_sottotitoli(testo, inizio, fine):
    """Distribuisce i blocchi di testo sulla durata, in proporzione alle parole."""
    blocchi = spezza_testo(testo)
    totale = sum(len(b.split()) for b in blocchi) or 1
    t, out = inizio, []
    for b in blocchi:
        d = (fine - inizio) * len(b.split()) / totale
        out.append((t, t + d, b))
        t += d
    return out


def scrivi_srt(path, righe):
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, testo) in enumerate(righe, 1):
            f.write(f"{i}\n{tempo_srt(a)} --> {tempo_srt(b)}\n{testo}\n\n")


# ---------- montaggio ----------
def clip_da_immagine(img, audio, dur, w, h, out, zoom_in=True, srt=None, af="apad", zmax=1.0):
    """Un'immagine ferma (con zoom lento) + un audio, per `dur` secondi."""
    frames = int(dur * FPS) + 1
    passo = (zmax - 1.0) / max(frames, 1)
    z = f"min(zoom+{passo:.6f},{zmax})" if zoom_in else f"if(eq(on,0),{zmax},max(zoom-{passo:.6f},1.0))"
    if zmax <= 1.0:  # foto ferma: nessun movimento, niente tremolio
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}"
    else:
        vf = (f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase,crop={w*2}:{h*2},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={FPS}")
    if srt is not None:
        stile = "FontName=DejaVu Sans,FontSize=%d,Bold=1,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=%d" % (
            13 if w > h else 9, 16 if w > h else 38)
        srt_f = str(srt).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        vf += f",subtitles='{srt_f}':force_style='{stile}'"
    vf += ",format=yuv420p"
    esegui(["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-i", str(audio),
            "-vf", vf, "-t", f"{dur:.3f}", "-r", str(FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-af", af, "-movflags", "+faststart", str(out)])


def adatta_foto(src, w, h):
    """Foto intera al centro su fondo sfocato e scurito, per non tagliare mai la bottiglia."""
    from PIL import Image, ImageFilter, ImageEnhance
    im = Image.open(src).convert("RGB")
    rapporto = (im.width / im.height) / (w / h)
    if 0.75 <= rapporto <= 1.8:  # foto d'atmosfera: a tutto schermo, leggermente scurita
        sc = max(w / im.width, h / im.height)
        im = im.resize((int(im.width * sc) + 1, int(im.height * sc) + 1), Image.LANCZOS)
        x, y = (im.width - w) // 2, (im.height - h) // 2
        im = ImageEnhance.Brightness(im.crop((x, y, x + w, y + h))).enhance(0.72)
        im.save(src.with_suffix(".foto.png"), format="PNG")
        return src.with_suffix(".foto.png")
    sf = im.copy()
    sc = max(w / sf.width, h / sf.height)
    sf = sf.resize((int(sf.width * sc) + 1, int(sf.height * sc) + 1), Image.LANCZOS)
    x, y = (sf.width - w) // 2, (sf.height - h) // 2
    sf = sf.crop((x, y, x + w, y + h)).filter(ImageFilter.GaussianBlur(28))
    sf = ImageEnhance.Brightness(sf).enhance(0.45)
    k = min(w * 0.8 / im.width, h * 0.8 / im.height)
    fg = im.resize((int(im.width * k), int(im.height * k)), Image.LANCZOS)
    fg = fg.filter(ImageFilter.UnsharpMask(radius=1.6, percent=70, threshold=2))
    sf.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    sf.save(src.with_suffix(".foto.png"), format="PNG")
    return src.with_suffix(".foto.png")


def clip_scena(d, i, scena, w, h, sottotitoli=True):
    base = d / f"s{i:03}"
    audio, img = base.with_suffix(".audio"), base.with_suffix(".img")
    if scena.get("audio_base64"):
        audio.write_bytes(base64.b64decode(scena["audio_base64"]))
    else:
        scarica(scena["audio_url"], audio)
    if scena.get("image_url"):
        scarica(scena["image_url"], img)
        img = adatta_foto(img, w * 2, h * 2)
    else:
        g = dict(scena["grafica"])
        g.setdefault("formato", "16:9" if w > h else "9:16")
        disegna_grafica(g).save(img, format="PNG")
    dur = durata(audio) + 0.35  # piccola pausa tra una scena e l'altra

    srt = None
    if sottotitoli:
        righe = righe_sottotitoli(scena.get("testo", ""), 0, dur - 0.35)
        srt = d / f"s{i:03}.srt"
        scrivi_srt(srt, righe)

    out = d / f"c{i:03}.mp4"
    clip_da_immagine(img, audio, dur, w, h, out, zoom_in=(i % 2 == 0), srt=srt,
                     af="apad=pad_dur=0.35", zmax=1.0)
    return out, dur


# ---------- schede con logo (intro e finale) ----------
def _font(path, px):
    """Carattere a una certa altezza. Cormorant ha le lettere piccole: lo ingrandisco un po' e, se variabile, lo metto in semibold."""
    if "Cormorant" in str(path):
        px = px * 1.2
    f = ImageFont.truetype(str(path), int(px))
    try:
        f.set_variation_by_axes([600])
    except Exception:
        pass
    return f


def _largezza_spaziata(dr, testo, font, spazio):
    return sum(dr.textlength(c, font=font) for c in testo) + spazio * max(len(testo) - 1, 0)


def _testo_spaziato(dr, x, y, testo, font, fill, spazio):
    for c in testo:
        dr.text((x, y), c, font=font, fill=fill)
        x += dr.textlength(c, font=font) + spazio


def asset(nome):
    """File fisso (jingle, logo) messo una volta per tutte in <dati>/assets."""
    p = DATA / "assets" / Path(str(nome)).name
    if not p.exists():
        raise ValueError(f"file non trovato in assets: {p.name}")
    return p


def _logo(dati, dest):
    """Logo da assets (logo_file) o da indirizzo (logo_url). Ritorna l'immagine PIL oppure None."""
    dati = dati or {}
    if dati.get("logo_file"):
        return Image.open(asset(dati["logo_file"])).convert("RGBA")
    url = dati.get("logo_url")
    if not url:
        return None
    scarica(url, dest)
    return Image.open(dest).convert("RGBA")


def _marchio(img, dr, w, h, cx, y, logo):
    """Disegna il marchio centrato in cx: il logo se c'e', altrimenti il nome in oro."""
    if logo is not None:
        larg = int(w * 0.26)
        alt = int(logo.height * larg / logo.width)
        lg = logo.resize((larg, alt), Image.LANCZOS)
        img.paste(lg, (int(cx - larg / 2), int(y)), lg)
        return y + alt
    f = _font(FONT_BOLD, h * 0.055)
    sp = h * 0.012
    testo = "SLOW STILLING"
    x = cx - _largezza_spaziata(dr, testo, f, sp) / 2
    _testo_spaziato(dr, x, y, testo, f, ORO, sp)
    return y + f.size


def disegna_intro(dati, w, h, logo=None):
    img = Image.new("RGB", (w, h), SFONDO)
    dr = ImageDraw.Draw(img)
    cx = w / 2
    y = _marchio(img, dr, w, h, cx, h * 0.26, logo)
    y += h * 0.035
    dr.rectangle([cx - w * 0.03, y, cx + w * 0.03, y + 3], fill=ORO_SCURO)
    y += h * 0.045
    f_c = _font(FONT_REG, h * 0.030)
    claim = "DISTILLATI DA CONOSCERE, SCEGLIERE, GUSTARE"
    sp = h * 0.006
    _testo_spaziato(dr, cx - _largezza_spaziata(dr, claim, f_c, sp) / 2, y, claim, f_c, GRIGIO, sp)
    serie = " · ".join(x for x in [dati.get("serie", ""), dati.get("numero", "")] if x)
    if serie or dati.get("titolo"):
        y += h * 0.12
        f_s = _font(FONT_REG, h * 0.042)
        if serie:
            sp2 = h * 0.007
            s_up = serie.upper()
            _testo_spaziato(dr, cx - _largezza_spaziata(dr, s_up, f_s, sp2) / 2, y, s_up, f_s, ORO, sp2)
            y += h * 0.075
        f_t = _font(FONT_BOLD, h * 0.06)
        for r in a_capo(dr, dati.get("titolo", ""), f_t, w * 0.78):
            dr.text((cx - dr.textlength(r, font=f_t) / 2, y), r, font=f_t, fill=CREMA)
            y += h * 0.085
    return img


def disegna_outro(dati, w, h, logo=None):
    """Testo a sinistra; la parte destra resta libera per le schede finali di YouTube."""
    img = Image.new("RGB", (w, h), SFONDO)
    dr = ImageDraw.Draw(img)
    m = int(w * 0.08)
    larg = w * (0.42 if w > h else 0.84)
    y = h * (0.20 if w > h else 0.12)
    if logo is not None:
        lw = int(w * 0.18)
        lh = int(logo.height * lw / logo.width)
        lg = logo.resize((lw, lh), Image.LANCZOS)
        img.paste(lg, (m, int(y)), lg)
        y += lh
    else:
        f = _font(FONT_BOLD, h * 0.04)
        sp = h * 0.009
        _testo_spaziato(dr, m, y, "SLOW STILLING", f, ORO, sp)
        y += f.size
    y += h * 0.03
    dr.rectangle([m, y, m + w * 0.05, y + 3], fill=ORO_SCURO)
    y += h * 0.05
    if dati.get("titolo"):
        f_p = _font(FONT_REG, h * 0.034)
        for r in a_capo(dr, dati["titolo"], f_p, larg):
            dr.text((m, y), r, font=f_p, fill=GRIGIO)
            y += h * 0.05
    # frase finale: sempre su una sola riga, corpo ridotto finche' ci sta
    testo = dati.get("testo", "Grazie per aver guardato")
    max_l = w * 0.84
    px = h * 0.05
    f_t = _font(FONT_BOLD, px)
    while dr.textlength(testo, font=f_t) > max_l and px > h * 0.02:
        px *= 0.95
        f_t = _font(FONT_BOLD, px)
    dr.text((m, h * 0.84), testo, font=f_t, fill=CREMA)
    return img


def silenzio(path, secondi):
    esegui(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", f"{secondi:.2f}", str(path)])


def clip_scheda(d, nome, img, jingle, dur, w, h, out):
    audio = jingle
    if audio is None:
        audio = d / f"{nome}_silenzio.wav"
        silenzio(audio, dur)
    # il jingle ha gia' la sua sfumatura; se e' piu' corto della scheda, resta silenzio
    clip_da_immagine(img, audio, dur, w, h, out, zoom_in=True, af="apad")
    return out, dur


def lavora(job_id, dati):
    d = DATA / job_id
    try:
        with LOCK:
            w, h = FORMATI[dati.get("formato", "16:9")]
            sottotitoli = dati.get("sottotitoli", True)
            scene = dati["scene"]
            intro, outro = dati.get("intro"), dati.get("outro")

            jingle = None
            if dati.get("jingle_base64"):
                jingle = d / "jingle.audio"
                jingle.write_bytes(base64.b64decode(dati["jingle_base64"]))
            elif dati.get("jingle_file"):
                jingle = asset(dati["jingle_file"])
            elif dati.get("jingle_url"):
                jingle = d / "jingle.audio"
                scarica(dati["jingle_url"], jingle)

            # Ordine finale: aggancio parlato -> scheda con jingle -> resto -> schermata finale
            n_hook = int(intro.get("dopo_scene", 0)) if intro else 0
            n_hook = max(0, min(n_hook, len(scene)))
            ordine = [("scena", i) for i in range(n_hook)]
            if intro:
                ordine.append(("intro", None))
            ordine += [("scena", i) for i in range(n_hook, len(scene))]
            if outro:
                ordine.append(("outro", None))

            scrivi_stato(d, stato="in corso", avanzamento="0/%d" % len(ordine))
            clip, t, srt_tot, capitoli = [], 0.0, [], []
            for k, (tipo, i) in enumerate(ordine):
                if tipo == "scena":
                    sc = scene[i]
                    c, dur = clip_scena(d, i, sc, w, h, sottotitoli)
                    if sc.get("capitolo"):
                        capitoli.append((t, sc["capitolo"]))
                    if sottotitoli:
                        srt_tot += righe_sottotitoli(sc.get("testo", ""), t, t + dur - 0.35)
                elif tipo == "intro":
                    logo = _logo(intro, d / "logo_intro.img")
                    img = d / "intro.png"
                    disegna_intro(intro, w, h, logo).save(img, format="PNG")
                    dur = float(intro.get("durata", 10))
                    c, dur = clip_scheda(d, "intro", img, jingle, dur, w, h, d / "intro.mp4")
                else:
                    logo = _logo(outro, d / "logo_outro.img")
                    img = d / "outro.png"
                    disegna_outro(outro, w, h, logo).save(img, format="PNG")
                    dur = max(5.0, min(20.0, float(outro.get("durata", 12))))
                    c, dur = clip_scheda(d, "outro", img, jingle, dur, w, h, d / "outro.mp4")
                clip.append(c)
                t += dur
                scrivi_stato(d, avanzamento=f"{k + 1}/{len(ordine)}")

            lista = d / "lista.txt"
            lista.write_text("".join(f"file '{c.name}'\n" for c in clip))
            grezzo = d / ("video_grezzo.mp4" if dati.get("normalizza", True) else "video.mp4")
            esegui(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lista),
                    "-c", "copy", "-movflags", "+faststart", str(grezzo)])
            if dati.get("normalizza", True):
                # volume uniforme tra le puntate (circa -16 LUFS, lo standard dei video online)
                esegui(["ffmpeg", "-y", "-i", str(grezzo), "-c:v", "copy",
                        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                        "-movflags", "+faststart", str(d / "video.mp4")])
                grezzo.unlink(missing_ok=True)
            scrivi_srt(d / "sottotitoli.srt", srt_tot)

            # Capitoli in formato YouTube: il primo deve partire da 00:00 e servono almeno 3 capitoli di 10 secondi.
            if capitoli and capitoli[0][0] > 0:
                capitoli.insert(0, (0.0, "Introduzione"))
            righe_cap = [f"{tempo_capitolo(a)} {nome}" for a, nome in capitoli]
            (d / "capitoli.txt").write_text("\n".join(righe_cap), encoding="utf-8")

            for c in clip:  # libera spazio: restano solo i file finali
                c.unlink(missing_ok=True)
            for p in list(d.glob("s[0-9][0-9][0-9].*")) + list(d.glob("intro*")) + list(d.glob("outro*")) \
                    + list(d.glob("logo_*")) + [d / "jingle.audio", d / "lista.txt"]:
                p.unlink(missing_ok=True)
            scrivi_stato(d, stato="pronto", durata_s=round(t, 1), capitoli=righe_cap,
                         capitoli_validi=len(capitoli) >= 3)
    except Exception as e:
        traceback.print_exc()
        scrivi_stato(d, stato="errore", errore=str(e)[:900])


# ---------- API ----------
@app.post("/render")
def render():
    richiedi_chiave()
    dati = request.get_json(force=True)
    scene = dati.get("scene") or []
    if not scene:
        abort(400, "scene mancanti")
    if dati.get("formato", "16:9") not in FORMATI:
        abort(400, "formato non valido")
    for i, sc in enumerate(scene):
        if not (sc.get("audio_url") or sc.get("audio_base64")) or not (sc.get("image_url") or sc.get("grafica")):
            abort(400, f"scena {i}: servono audio_url (o audio_base64) e image_url (o grafica)")
    for nome in ("intro", "outro"):
        if dati.get(nome) is not None and not isinstance(dati[nome], dict):
            abort(400, f"{nome} deve essere un oggetto")
    job_id = uuid.uuid4().hex[:12]
    d = DATA / job_id
    d.mkdir(parents=True)
    scrivi_stato(d, stato="in coda", id_video=dati.get("id_video", ""))
    threading.Thread(target=lavora, args=(job_id, dati), daemon=True).start()
    return jsonify(job_id=job_id), 202


@app.get("/jobs/<job_id>")
def stato(job_id):
    richiedi_chiave()
    f = job_dir(job_id) / "stato.json"
    if not f.exists():
        abort(404)
    return jsonify(json.loads(f.read_text()))


@app.get("/download/<job_id>/<cosa>")
def scarica_file(job_id, cosa):
    nomi = {"video": ("video.mp4", "video/mp4"), "srt": ("sottotitoli.srt", "text/plain"),
            "capitoli": ("capitoli.txt", "text/plain")}
    if cosa not in nomi:
        abort(404)
    f = job_dir(job_id) / nomi[cosa][0]
    if not f.exists():
        abort(404)
    return send_file(f, mimetype=nomi[cosa][1])


def a_capo(draw, testo, font, larghezza):
    righe, riga = [], ""
    for parola in testo.split():
        prova = (riga + " " + parola).strip()
        if draw.textlength(prova, font=font) <= larghezza:
            riga = prova
        else:
            righe.append(riga)
            riga = parola
    return righe + [riga]


def disegna_grafica(dati):
    w, h = FORMATI[dati.get("formato", "16:9")]
    img = Image.new("RGB", (w, h), (24, 18, 14))
    dr = ImageDraw.Draw(img)
    oro = (201, 162, 94)
    m = int(w * 0.08)
    dr.rectangle([m, int(h * 0.18), m + int(w * 0.06), int(h * 0.18) + 6], fill=oro)
    f_tit = _font(FONT_REG, h * 0.045)
    f_dato = _font(FONT_BOLD, h * (0.16 if w > h else 0.10))
    f_fonte = _font(FONT_REG, h * 0.028)
    y = int(h * 0.22)
    for r in a_capo(dr, dati.get("titolo", ""), f_tit, w - 2 * m):
        dr.text((m, y), r, font=f_tit, fill=(235, 226, 210))
        y += int(h * 0.06)
    y += int(h * 0.05)
    for r in a_capo(dr, str(dati.get("dato", "")), f_dato, w - 2 * m):
        dr.text((m, y), r, font=f_dato, fill=oro)
        y += int(f_dato.size * 1.15)
    if dati.get("fonte"):
        dr.text((m, int(h * 0.90)), "Fonte: " + dati["fonte"], font=f_fonte, fill=(150, 140, 126))
    return img


@app.post("/graphic")
def grafica():
    """Scheda con un dato in grande e la fonte sotto. Risponde con il PNG."""
    richiedi_chiave()
    img = disegna_grafica(request.get_json(force=True))
    d = DATA / "grafiche"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{uuid.uuid4().hex[:12]}.png"
    img.save(out)
    return send_file(out, mimetype="image/png")


@app.post("/admin/aggiorna")
def aggiorna():
    """Sostituisce app.py con una nuova versione e riavvia il servizio (serve la chiave API)."""
    richiedi_chiave()
    codice = base64.b64decode(request.get_json(force=True)["app_py_base64"]).decode("utf-8")
    compile(codice, "app.py", "exec")  # se non e' Python valido, non tocca nulla
    Path("/app/app.py").write_text(codice, encoding="utf-8")
    def riavvia():
        import time
        time.sleep(1)
        os.kill(os.getppid(), 15)  # ferma gunicorn: Docker lo riavvia con il codice nuovo
    threading.Thread(target=riavvia, daemon=True).start()
    return jsonify(ok=True, riavvio=True)


@app.post("/admin/scarica")
def scarica_asset():
    """Scarica un file (font, logo, jingle) da un indirizzo https in <dati>/assets (serve la chiave API)."""
    richiedi_chiave()
    d = request.get_json(force=True)
    url, nome = str(d.get("url", "")), Path(str(d.get("nome", ""))).name
    if not url.startswith("https://") or not nome.lower().endswith((".ttf", ".png", ".mp3")):
        abort(400, "servono un indirizzo https e un nome .ttf, .png o .mp3")
    (DATA / "assets").mkdir(parents=True, exist_ok=True)
    dest = DATA / "assets" / nome
    req = urllib.request.Request(url, headers={"User-Agent": "montaggio"})
    with urllib.request.urlopen(req, timeout=60) as r:
        dati = r.read(30 * 1024 * 1024 + 1)
    if len(dati) > 30 * 1024 * 1024 or len(dati) < 1000:
        abort(400, "file troppo grande o vuoto")
    dest.write_bytes(dati)
    return jsonify(ok=True, nome=nome, byte=len(dati))


@app.get("/salute")
def salute():
    return jsonify(ok=True)


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
