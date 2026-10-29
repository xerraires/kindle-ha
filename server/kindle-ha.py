#!/usr/bin/env python3
"""
Servidor de salvapantallas para Kindle (Paperwhite 1: 758x1024).
Consulta Home Assistant (clima + precio PVPC) y renderiza un PNG en
escala de grises optimizado para tinta electrónica.

Uso:
  HA_URL=http://HOME-ASSISTANT:8123 HA_TOKEN=xxx PORT=8090 ./venv/bin/python kindle-ha.py

Endpoints:
  GET /kindle.png   -> imagen PNG 758x1024 (lo que pide el Kindle)
  GET /             -> estado / ultima actualizacion

Config por variables de entorno: HA_URL, HA_TOKEN, PORT, DATA_TTL,
WEATHER_ENTITY, PRICE_ENTITY, TITLE (rotulo de la cabecera) y
FONT_DIR/FONT_BOLD/FONT_REG (fuentes; ver .env.example).
"""
import os
import io
import json
import math
import subprocess
import sys
import time
import threading
import traceback
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
from PIL import Image, ImageDraw, ImageFont
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- Config (variables de entorno) ----------------
HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
PORT = int(os.environ.get("PORT", "8090"))
WEATHER_ENTITY = os.environ.get("WEATHER_ENTITY", "weather.home")
PRICE_ENTITY = os.environ.get("PRICE_ENTITY", "sensor.esios_pvpc")
TITLE = os.environ.get("TITLE", "CASA")  # rotulo de la cabecera del PNG
W, H = 758, 1024
DATA_TTL = int(os.environ.get("DATA_TTL", "300"))  # cache de datos HA (s)

# Fuentes DejaVu: rutas tipicas de varias distros + fc-match de fontconfig.
# Se pueden fijar a mano con FONT_DIR (o FONT_BOLD/FONT_REG completos).
def _find_font(basename, fc_pattern):
    explicit = os.environ.get("FONT_BOLD" if "Bold" in basename else "FONT_REG")
    if explicit and os.path.isfile(explicit):
        return explicit
    candidates = []
    if os.environ.get("FONT_DIR"):
        candidates.append(os.path.join(os.environ["FONT_DIR"], basename))
    candidates += [
        os.path.join(d, basename)
        for d in (
            "/usr/share/fonts/truetype/dejavu",   # Debian/Ubuntu/Raspberry Pi OS
            "/usr/share/fonts/dejavu",             # Fedora
            "/usr/share/fonts/dejavu-sans-fonts",   # Fedora (paquete separado)
            "/usr/share/fonts/TTF",                # Arch
        )
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    try:  # ultimo recurso: preguntarle a fontconfig
        out = subprocess.check_output(
            ["fc-match", "-f", "%{file}", fc_pattern],
            text=True, timeout=10).strip()
        if out and os.path.isfile(out):
            return out
    except Exception:
        pass
    return None


F_BOLD = _find_font("DejaVuSans-Bold.ttf", "DejaVu Sans:bold")
F_REG = _find_font("DejaVuSans.ttf", "DejaVu Sans")

# ---------------- Caché de datos HA ----------------
_cache = {"ts": 0.0, "data": None, "lock": threading.Lock()}

# ---------------- Estado de bateria del Kindle ----------------
# Lo envia el parche (2-live-sleepscreen.lua) como ?battery=NN&charging=0/1
# en cada GET a /kindle.png. Se persiste para sobrevivir reinicios.
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
_battery = {"level": None, "charging": None, "ts": 0.0}

# Historial horario del día en curso (temp por hora local). HA solo devuelve
# horas FUTURAS en get_forecasts hourly; acumulando cada fetch tenemos el día
# completo para el pie MIN/MAX de temperatura con horas reales.
_HH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_hours.json")
_hh = {"day": None, "hours": {}}


def _load_hh():
    try:
        with open(_HH_FILE) as f:
            d = json.load(f)
        if isinstance(d.get("hours"), dict):
            _hh["day"], _hh["hours"] = d.get("day"), d["hours"]
    except Exception:
        pass


def _load_battery():
    try:
        with open(_STATE_FILE) as f:
            d = json.load(f)
        for k in ("level", "charging", "ts"):
            if k in d:
                _battery[k] = d[k]
    except Exception:
        pass


def _save_battery():
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(_battery, f)
    except Exception:
        pass


_load_battery()
_load_hh()


def _fetch_ha():
    headers = {"Authorization": f"Bearer {HA_TOKEN}",
               "Content-Type": "application/json"}
    data = {"weather": None, "prices": None, "current": None, "temp_now": None,
            "hourly": None, "sunrise": None, "sunset": None,
            "humidity": None, "wind_speed": None, "wind_unit": None,
            "min": None, "min_at": None, "max": None, "max_at": None,
            "error": None}

    # --- Clima: pronostico diario (7 dias) + temperatura actual ---
    try:
        r = requests.post(
            f"{HA_URL}/api/services/weather/get_forecasts?return_response=1",
            headers=headers,
            json={"entity_id": WEATHER_ENTITY, "type": "daily"},
            timeout=15,
        )
        r.raise_for_status()
        resp = r.json().get("service_response", {}).get(WEATHER_ENTITY, {})
        data["weather"] = resp.get("forecast", [])[:7]
        r2 = requests.get(f"{HA_URL}/api/states/{WEATHER_ENTITY}",
                          headers=headers, timeout=15)
        r2.raise_for_status()
        att = r2.json().get("attributes", {})
        data["temp_now"] = att.get("temperature")
        data["humidity"] = att.get("humidity")
        data["wind_speed"] = att.get("wind_speed")
        data["wind_unit"] = att.get("wind_speed_unit") or "km/h"
    except Exception as e:
        data["error"] = f"clima: {e}"

    # --- Pronostico horario (curva del dia) ---
    try:
        r3 = requests.post(
            f"{HA_URL}/api/services/weather/get_forecasts?return_response=1",
            headers=headers,
            json={"entity_id": WEATHER_ENTITY, "type": "hourly"},
            timeout=15,
        )
        r3.raise_for_status()
        resp = r3.json().get("service_response", {}).get(WEATHER_ENTITY, {})
        data["hourly"] = resp.get("forecast", [])[:48]
        # acumular el historial horario del día en curso (hora local)
        today = datetime.now().strftime("%Y-%m-%d")
        if _hh.get("day") != today:
            _hh["day"], _hh["hours"] = today, {}
        for e in (data.get("hourly") or []):
            try:
                hdt = datetime.fromisoformat(e["datetime"])
                if hdt.tzinfo is not None:
                    hdt = hdt.astimezone().replace(tzinfo=None)
            except Exception:
                continue
            t = e.get("temperature")
            if t is not None and hdt.strftime("%Y-%m-%d") == today:
                _hh["hours"][str(hdt.hour)] = float(t)
        try:
            with open(_HH_FILE, "w") as f:
                json.dump(_hh, f)
        except Exception:
            pass
    except Exception as e:
        data["error"] = (data["error"] + f"; horario: {e}") if data["error"] else f"horario: {e}"

    # --- Sol: amanece/atardece (sun.sun) ---
    try:
        r4 = requests.get(f"{HA_URL}/api/states/sun.sun", headers=headers, timeout=15)
        r4.raise_for_status()
        att = r4.json().get("attributes", {})
        data["sunrise"] = att.get("next_rising")
        data["sunset"] = att.get("next_setting")
    except Exception as e:
        data["error"] = (data["error"] + f"; sol: {e}") if data["error"] else f"sol: {e}"

    # --- Precio PVPC ---
    try:
        r = requests.get(f"{HA_URL}/api/states/{PRICE_ENTITY}",
                         headers=headers, timeout=15)
        r.raise_for_status()
        s = r.json()
        attrs = s.get("attributes", {})
        prices = [attrs.get(f"price_{h:02d}h") for h in range(24)]
        prices = [p for p in prices if isinstance(p, (int, float))]
        data["prices"] = prices
        data["current"] = float(s.get("state"))
        data["min"] = attrs.get("min_price")
        data["min_at"] = attrs.get("min_price_at")
        data["max"] = attrs.get("max_price")
        data["max_at"] = attrs.get("max_price_at")
    except Exception as e:
        data["error"] = (data["error"] + f"; precio: {e}") if data["error"] else f"precio: {e}"

    if data.get("weather") or data.get("prices"):
        data["fetched_at"] = time.time()
    return data


def get_data():
    now = time.time()
    with _cache["lock"]:
        if _cache["data"] and (now - _cache["ts"]) < DATA_TTL:
            return _cache["data"]
    d = _fetch_ha()
    with _cache["lock"]:
        _cache["data"] = d
        _cache["ts"] = time.time()
    return d


def _bg_loop():
    """Refresco horario de fondo: aunque el Kindle duerma (o falle un ciclo)
    seguimos consultando HA cada hora. Mantiene el historial horario del día
    (_hh) completo para el pie MIN/MAX con horas, y la caché caliente."""
    while True:
        time.sleep(60)
        try:
            with _cache["lock"]:
                stale = (_cache["data"] is not None
                         and time.time() - _cache["ts"] >= 3600)
            if stale:
                d = _fetch_ha()
                if d.get("weather") or d.get("prices"):
                    with _cache["lock"]:
                        _cache["data"] = d
                        _cache["ts"] = time.time()
        except Exception:
            pass


# ---------------- Utilidades de dibujo ----------------
def font(size, bold=True):
    return ImageFont.truetype(F_BOLD if bold else F_REG, size)


def text_w(draw, s, fnt):
    b = draw.textbbox((0, 0), s, font=fnt)
    return b[2] - b[0]


def draw_centered(draw, xy, s, fnt, fill=0, stroke=0):
    w = text_w(draw, s, fnt)
    x, y = xy
    draw.text((x - w / 2, y), s, font=fnt, fill=fill,
              stroke_width=stroke, stroke_fill=255 if stroke else None)


def draw_right(draw, xr, y, s, fnt, fill=0):
    draw.text((xr - text_w(draw, s, fnt), y), s, font=fnt, fill=fill)


def to_local_naive(dt):
    """Normaliza un datetime (aware o naive) a hora local naive."""
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def fmt_price(v):
    return f"{v:.3f}".replace(".", ",")


def price_band(prices, current):
    """Terciles del dia -> ('VALLE'|'MEDIO'|'PUNTA', idx)."""
    s = sorted(prices)
    n = len(s)
    t1, t2 = s[n // 3], s[(2 * n) // 3]
    if current <= t1:
        return "VALLE", 0
    if current <= t2:
        return "MEDIO", 1
    return "PUNTA", 2


def optimal_hours(prices, cur_hour):
    """La hora(s) más barata(s) del DÍA entero, agrupadas en rangos.
    Devuelve (rangos [h_ini,h_fin], precio_min, pasada); pasada=True cuando
    la hora actual ya superó la última hora óptima (-> "No hay hora óptima")."""
    pmin = min(prices)
    cand = [h for h, p in enumerate(prices) if p == pmin]
    ranges = []
    for h in cand:
        if ranges and h == ranges[-1][1] + 1:
            ranges[-1][1] = h
        else:
            ranges.append([h, h])
    if cur_hour is not None and 0 <= cur_hour < len(prices):
        passed = cur_hour > ranges[-1][1]
    else:
        passed = False
    return ranges, pmin, passed


COND_ES = {
    "clear": "Despejado", "sunny": "Soleado",
    "partly_cloudy": "Parc.nuboso", "cloudy": "Nuboso",
    "fog": "Niebla", "hail": "Granizo",
    "lightning": "Tormenta", "lightning-rainy": "Tormenta",
    "pouring": "LluviaFte", "rainy": "Lluvia",
    "snowy": "Nieve", "snowy-rainy": "Aguanieve",
    "windy": "Viento", "windy-variant": "Viento",
    "exceptional": "-",
}

WD_ES = ["LUN", "MAR", "MIE", "JUE", "VIE", "SAB", "DOM"]


def draw_icon(draw, kind, cx, cy, r, fill=0):
    """Iconos minimalistas en gris/negro para e-ink."""
    k = (kind or "").replace("-", "_")
    if "clear" in k or "sunny" in k:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=fill, width=3)
        for a in range(0, 360, 45):
            import math
            rad = math.radians(a)
            x1 = cx + (r + 4) * math.cos(rad)
            y1 = cy + (r + 4) * math.sin(rad)
            x2 = cx + (r + 12) * math.cos(rad)
            y2 = cy + (r + 12) * math.sin(rad)
            draw.line([x1, y1, x2, y2], fill=fill, width=2)
    elif "snowy" in k:
        draw.ellipse([cx - r, cy - r * 0.7, cx + r, cy + r * 0.7],
                     outline=fill, width=3)
        for dx in (-r * 0.5, 0, r * 0.5):
            draw.line([cx + dx, cy + r * 0.8, cx + dx, cy + r * 1.3],
                      fill=fill, width=2)
    elif "rainy" in k or "pouring" in k or "lightning" in k:
        draw.ellipse([cx - r, cy - r * 0.7, cx + r, cy + r * 0.7],
                     outline=fill, width=3)
        for dx in (-r * 0.5, r * 0.2, r * 0.6):
            draw.line([cx + dx, cy + r * 0.8, cx + dx - 3, cy + r * 1.3],
                      fill=fill, width=2)
        if "lightning" in k:
            draw.polygon([(cx - 4, cy + r),
                          (cx + 6, cy + r),
                          (cx - 2, cy + r * 1.6),
                          (cx + 8, cy + r * 1.6)],
                         fill=fill)
    elif "fog" in k:
        for i, oy in enumerate((-r * 0.4, 0, r * 0.4)):
            draw.line([cx - r, cy + oy, cx + r, cy + oy], fill=fill, width=3)
    elif "windy" in k:
        for i, oy in enumerate((-r * 0.4, 0, r * 0.4)):
            draw.line([cx - r, cy + oy, cx + r * 0.6, cy + oy], fill=fill, width=3)
    else:  # cloudy / default
        draw.ellipse([cx - r, cy - r * 0.6, cx + r, cy + r * 0.6],
                     outline=fill, width=3)
        draw.ellipse([cx - r * 0.5, cy - r, cx + r * 0.7, cy + r * 0.2],
                     outline=fill, width=3)


# ---------------- Render principal ----------------
def render():
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    data = get_data()

    if data.get("error") and not (data.get("weather") or data.get("prices")):
        f = font(40)
        draw_centered(d, (W / 2, H / 2 - 20), "SIN DATOS HA", f)
        f2 = font(22)
        draw_centered(d, (W / 2, H / 2 + 30), str(data.get("error"))[:40], f2)
        return img

    now = datetime.now()
    R = W - 24  # margen derecho

    # ----- Cabecera -----
    d.text((24, 8), TITLE, font=font(44), fill=0)
    f_batt = font(18)
    blvl = _battery.get("level")
    if blvl is not None:
        batt_txt = f"BATERIA {blvl}%"
        if _battery.get("charging"):
            batt_txt += " (CARGA)"
    else:
        batt_txt = "BATERIA --"
    d.text((24, 60), batt_txt, font=f_batt, fill=0)

    f_hora = font(30)
    f_sub = font(18)
    draw_right(d, R, 8, now.strftime("%H:%M"), f_hora)
    draw_right(d, R, 44, now.strftime("%a %d %b").upper(), f_sub)
    if data.get("fetched_at"):
        upd = "ACTUALIZADO " + datetime.fromtimestamp(data["fetched_at"]).strftime("%H:%M")
        draw_right(d, R, 68, upd, font(15), fill=128)

    d.line([(20, 94), (W - 20, 94)], fill=0, width=2)

    # ----- Tiempo (arriba): curva horaria del día + sol + strip diario -----
    if data.get("weather"):
        days = data["weather"][:7]
        hourly = data.get("hourly") or []

        # sol: amanecer/atardecer parseados (cabecera + sombreado nocturno)
        rise_dt = set_dt = None
        for iso in (data.get("sunrise"), data.get("sunset")):
            if not iso:
                continue
            try:
                dt = to_local_naive(datetime.fromisoformat(iso))
            except Exception:
                continue
            if rise_dt is None:
                rise_dt = dt
            else:
                set_dt = dt

        # fila donde estaba "TIEMPO": Ahora/H/V a la izquierda, sol a la der
        extra = []
        if data.get("temp_now") is not None:
            extra.append(f"Ahora {round(data['temp_now'])}°")
        if data.get("humidity") is not None:
            extra.append(f"{int(data['humidity'])}%")
        if data.get("wind_speed") is not None:
            extra.append(f"V {int(round(data['wind_speed']))}")
        if extra:
            d.text((24, 102), "  ".join(extra), font=font(20), fill=0)
        if rise_dt is not None and set_dt is not None:
            f_sol = font(20)
            t1 = f"↑ {rise_dt.strftime('%H:%M')}"
            t2 = f"↓ {set_dt.strftime('%H:%M')}"
            draw_right(d, R, 102, t1 + "   " + t2, f_sol)

        # puntos horarios para la curva (las próximas ~24 h de HA)
        # ojo: los datetimes de la previsión llegan en UTC -> normalizar a
        # hora local naive, o toda la curva (etiquetas, sol, medianoche)
        # sale desplazada 1 h
        pts = []
        for e in hourly:
            try:
                hdt = datetime.fromisoformat(e["datetime"])
                if hdt.tzinfo is not None:
                    hdt = hdt.astimezone().replace(tzinfo=None)
            except Exception:
                continue
            t = e.get("temperature")
            if t is None:
                continue
            pts.append({"dt": hdt, "temp": float(t),
                        "prob": e.get("precipitation_probability"),
                        "cond": e.get("condition", "")})
            if len(pts) >= 24:
                break

        if pts:
            bx0, bx1 = 40, 718
            n = len(pts)
            slot = (bx1 - bx0) / max(n - 1, 1)
            ctop, cbot = 152, 278
            tmax = max(p["temp"] for p in pts)
            tmin = min(p["temp"] for p in pts)
            span = max(tmax - tmin, 1.0)
            imax = next(i for i, p in enumerate(pts) if p["temp"] == tmax)
            imin = next(i for i, p in enumerate(pts) if p["temp"] == tmin)

            def X(i):
                return bx0 + slot * i

            def Y(v):
                # máx arriba (ctop), mín abajo (cbot)
                return cbot - (v - tmin) / span * (cbot - ctop)

            coords = [(X(i), Y(p["temp"])) for i, p in enumerate(pts)]

            # calidad de cobertura horaria por día (para el strip: si la
            # curva cubre el alba y la tarde de un día, sus tmin/tmax son
            # derivables de la curva y pueden diferir del daily oficial)
            by_day = {}
            for p in pts:
                dkey = p["dt"].strftime("%Y-%m-%d")
                e = by_day.setdefault(dkey, {"temps": [], "hours": []})
                e["temps"].append(p["temp"])
                e["hours"].append(p["dt"].hour)

            # área rellena bajo la curva (gris claro) + línea negra encima
            poly = [(bx0, cbot + 2)] + coords + [(bx1, cbot + 2)]
            d.polygon(poly, fill=224)
            for i in range(len(coords) - 1):
                d.line([coords[i], coords[i + 1]], fill=0, width=3)

            # marca de cambio de día: línea vertical + etiqueta MAÑANA
            # (la etiqueta a ctop+3, para no pisar la fila "Ahora/sol" de arriba)
            for i, p in enumerate(pts):
                if i > 0 and p["dt"].day != pts[i - 1]["dt"].day:
                    xm = X(i) - slot / 2
                    d.line([(xm, ctop - 14), (xm, cbot + 12)], fill=160,
                           width=2)
                    draw_centered(d, (xm, ctop + 3), "MAÑANA", font(13),
                                  stroke=2)
                    break

            for i, (x, y) in enumerate(coords):
                if i % 3 == 0:
                    # icono cada 3 h, con fondo blanco para "abrir" la curva
                    d.rectangle([x - 14, y - 14, x + 14, y + 14], fill=255)
                    draw_icon(d, pts[i]["cond"], x, y, 10)
                else:
                    d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=0)

            # barras de probabilidad de lluvia, alineadas con los puntos
            base, bh_max = 310, 26
            for i, p in enumerate(pts):
                prob = p.get("prob")
                if prob is None or prob <= 0:
                    continue
                x = X(i)
                hgt = max(prob / 100.0 * bh_max, 1.5)
                d.rectangle([x - 5, base - hgt, x + 5, base], fill=128)
                if prob >= 50:
                    draw_centered(d, (x, base - hgt - 16), f"{int(prob)}",
                                  font(13), fill=128)

            f_lbl = font(18)
            # halo blanco: legibles aunque caigan sobre iconos, curva o barras
            draw_centered(d, (coords[imax][0], coords[imax][1] - 26),
                          f"{int(round(tmax))}°", f_lbl, stroke=3)
            draw_centered(d, (coords[imin][0], coords[imin][1] + 8),
                          f"{int(round(tmin))}°", f_lbl, stroke=3)

            # fila de horas (pegada a las barras), ticks cada 3 h
            f_h = font(15)
            for ti in range(0, len(pts), 3):
                draw_centered(d, (X(ti), 318),
                              f"{pts[ti]['dt'].strftime('%H')}h", f_h, fill=128)

        # strip diario compacto y centrado (mini-icono + día + tmin/tmax):
        # sin HOY (el día en curso va en el pie MIN/MAX), un día más.
        # Coherencia con la curva: cuando la ventana horaria cubre el alba y
        # la tarde de un día del strip, su tmin/tmax salen de la CURVA (el
        # daily oficial puede diferir en ±1° y no debe contradecirla).
        f_d1 = font(20)
        parts = []
        for day in days[1:5]:
            dt = datetime.fromisoformat(day["datetime"])
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            lbl = WD_ES[dt.weekday()]
            dhi = day.get("temperature")
            dlo = day.get("templow")
            covered = by_day.get(dt.strftime("%Y-%m-%d"))
            if covered and min(covered["hours"]) <= 1 \
                    and max(covered["hours"]) >= 18:
                dhi = max(covered["temps"])
                dlo = min(covered["temps"])
            txt = lbl
            if dhi is not None:
                if dlo is not None:
                    txt += f" {int(round(dlo))}°/{int(round(dhi))}°"
                else:
                    txt += f" {int(round(dhi))}°"
            parts.append((day.get("condition", ""), txt))
        gap = 34
        total = sum(26 + text_w(d, t, f_d1) for _, t in parts) \
            + gap * (len(parts) - 1)
        x = (W - total) / 2
        y = 348
        for cond, txt in parts:
            draw_icon(d, cond, x + 9, y + 13, 9)
            x += 26
            d.text((x, y), txt, font=f_d1, fill=0)
            x += text_w(d, txt, f_d1) + gap

        # pie de temperatura del DÍA EN CURSO (estilo pie del precio).
        # Con horas reales solo si el historial horario cubre el día completo;
        # si no, los valores oficiales del pronóstico diario, sin horas
        f_mm_t = font(20)
        today = now.strftime("%Y-%m-%d")
        hist = (_hh.get("hours") or {}) if _hh.get("day") == today else {}
        merged = dict(hist)
        for p in pts:
            if p["dt"].strftime("%Y-%m-%d") == today:
                merged[p["dt"].hour] = p["temp"]
        dlo = days[0].get("templow")
        dhi = days[0].get("temperature")
        tmn = tmx = None
        # hay horas cuando el día cubre ~las 24 h (el forecast horario empieza
        # en la hora SIGUIENTE, así que a las 00:00 se capturan 1–23: primero
        # refresco del día = día completo menos la hora 0)
        if len(merged) >= 23:
            hmin = min(merged, key=merged.get)
            hmax = max(merged, key=merged.get)
            tmn = f"MIN {int(round(merged[hmin]))}° ({int(hmin):02d}h)"
            tmx = f"MAX {int(round(merged[hmax]))}° ({int(hmax):02d}h)"
        elif dlo is not None or dhi is not None:
            if dlo is not None:
                tmn = f"MIN {int(round(dlo))}°"
            if dhi is not None:
                tmx = f"MAX {int(round(dhi))}°"
        if tmn or tmx:
            wmin = text_w(d, tmn, f_mm_t) if tmn else 0
            wmax = text_w(d, tmx, f_mm_t) if tmx else 0
            x0 = (W - (wmin + 32 + wmax)) / 2
            if tmn:
                d.text((x0, 382), tmn, font=f_mm_t, fill=0)
            if tmx:
                d.text((x0 + wmin + 32, 382), tmx, font=f_mm_t, fill=0)

    d.line([(20, 440), (W - 20, 440)], fill=128, width=2)

    # ----- Precio (abajo): protagonista -----
    f_sec = font(26)
    d.text((24, 448), "PRECIO LUZ", font=f_sec, fill=0)

    prices = data.get("prices")
    current = data.get("current")

    # badge de tramo (terciles del dia)
    if prices and current is not None:
        band, bi = price_band(prices, current)
        f_badge = font(20)
        tw = text_w(d, band, f_badge)
        bw, bh = tw + 28, 34
        bx, by = R - bw, 446
        if bi == 2:      # PUNTA: negro, texto blanco
            d.rectangle([bx, by, bx + bw, by + bh], fill=0)
            d.text((bx + 14, by + 4), band, font=f_badge, fill=255)
        elif bi == 1:    # MEDIO: gris
            d.rectangle([bx, by, bx + bw, by + bh], fill=128)
            d.text((bx + 14, by + 4), band, font=f_badge, fill=0)
        else:            # VALLE: blanco con borde
            d.rectangle([bx, by, bx + bw, by + bh], outline=0, width=3)
            d.text((bx + 14, by + 4), band, font=f_badge, fill=0)

    # precio actual, en grande
    if current is not None:
        f_big = font(92)
        txt = fmt_price(current)
        d.text((20, 490), txt, font=f_big, fill=0)
        d.text((24 + text_w(d, txt, f_big) + 16, 557), "€/kWh",
               font=font(26), fill=0)

    # hora óptima: la más barata del DÍA; al pasarla, "No hay hora óptima"
    if prices:
        f_opt = font(22)
        cur_hour = now.hour if len(prices) == 24 else -1
        ranges, pmin, past = optimal_hours(prices, cur_hour)
        if ranges and not past:
            def hr(r):
                return (f"{r[0]:02d}h" if r[0] == r[1]
                        else f"{r[0]:02d}–{r[1]:02d}h")
            if len(ranges) == 1:
                label = "ÓPTIMA " + hr(ranges[0])
            elif len(ranges) == 2:
                label = "ÓPTIMAS " + hr(ranges[0]) + " y " + hr(ranges[1])
            else:
                label = "ÓPTIMAS " + hr(ranges[0]) + ", " + hr(ranges[1]) + ", …"
            if pmin is not None:
                label += f" · {fmt_price(pmin)} €"
            draw_right(d, R, 512, label, f_opt)
        else:
            draw_right(d, R, 512, "No hay hora óptima", f_opt, fill=128)

    # barras 24h
    if prices:
        left, right = 40, 718
        top, bottom = 640, 930
        cw = right - left
        n = len(prices)
        slot = cw / n
        bw = max(6, slot * 0.62)
        vmax = max(prices)
        if data.get("max"):
            vmax = max(vmax, data["max"])
        vmax = vmax * 1.08
        cur_hour = now.hour if n == 24 else -1

        # tramos por terciles (para pintar el valle)
        s = sorted(prices)
        t1 = s[n // 3]

        d.line([(left, bottom), (right, bottom)], fill=0, width=2)
        d.line([(left, top), (left, bottom)], fill=0, width=2)

        for i, p in enumerate(prices):
            x = left + slot * i + (slot - bw) / 2
            h = (p / vmax) * (bottom - top)
            y0b = bottom - h
            if i == cur_hour:
                d.rectangle([x, y0b, x + bw, bottom], fill=0)
                # flecha sobre la hora actual
                cx = x + bw / 2
                d.polygon([(cx - 9, y0b - 20), (cx + 9, y0b - 20),
                           (cx, y0b - 6)], fill=0)
            elif p <= t1:
                d.rectangle([x, y0b, x + bw, bottom], fill=128)
            else:
                d.rectangle([x, y0b, x + bw, bottom], outline=0, width=2)

        # etiquetas eje X (cada 3h)
        f_x = font(16)
        for hh in (0, 3, 6, 9, 12, 15, 18, 21, 23):
            if hh < n:
                x = left + slot * hh + slot / 2
                draw_centered(d, (x, bottom + 10), f"{hh:02d}h", f_x, fill=128)

    # pie: min / max
    f_mm = font(20)
    if data.get("min") is not None:
        draw_right(d, W // 2 - 16, 975,
                   f"MIN {fmt_price(data['min'])} ({int(data['min_at']):02d}h)"
                   if data.get("min_at") is not None
                   else f"MIN {fmt_price(data['min'])}", f_mm)
    if data.get("max") is not None:
        mn = f"MAX {fmt_price(data['max'])}"
        if data.get("max_at") is not None:
            mn += f" ({int(data['max_at']):02d}h)"
        d.text((W // 2 + 16, 975), mn, font=f_mm, fill=0)
    return img


def render_png():
    img = render()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/kindle.png"):
            qs = parse_qs(urlparse(self.path).query)
            if "battery" in qs:
                try:
                    lvl = int(qs["battery"][0])
                    if 0 <= lvl <= 100:
                        _battery["level"] = lvl
                        _battery["charging"] = (qs.get("charging", ["0"])[0] == "1")
                        _battery["ts"] = time.time()
                        _save_battery()
                        print(f"[battery] {lvl}% charged={_battery['charging']}")
                except (ValueError, IndexError):
                    pass
            try:
                png = render_png()
            except Exception:
                traceback.print_exc()
                body = b"render error (revisa los logs del servidor)"
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("X-Host-Epoch", str(time.time()))
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png)
        elif self.path.startswith("/force"):
            get_data()  # refresca cache
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
        else:
            with _cache["lock"]:
                ts = _cache["ts"]
            msg = ("Kindle HA screensaver\n"
                   f"Ultima actualizacion HA: "
                   f"{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else 'nunca'}\n"
                   f"PNG: http://<este-host>:{PORT}/kindle.png\n")
            body = msg.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    if not HA_TOKEN:
        print("AVISO: HA_TOKEN no definido; el render mostrara 'SIN DATOS HA'.")
    if not F_BOLD or not F_REG:
        print("ERROR: no encuentro las fuentes DejaVu (necesarias para el render).")
        print("  Instala el paquete de tu distra (p. ej. fonts-dejavu /")
        print("  dejavu-sans-fonts / ttf-dejavu) o fija FONT_DIR/FONT_BOLD/FONT_REG")
        print("  en el entorno con la ruta a DejaVuSans.ttf y DejaVuSans-Bold.ttf.")
        sys.exit(1)
    print(f"Fuentes: {F_BOLD} / {F_REG}")
    threading.Thread(target=_bg_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Sirviendo en 0.0.0.0:{PORT}  (PNG en /kindle.png)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
