"""Servidor local de control de Instagram; solo escucha en 127.0.0.1."""
from __future__ import annotations

import base64
import importlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key
from PIL import Image

import config
import contenido
import db
import fotos
import ig_api
import metricas
import planificador
import publicidad
import telegram_bridge
from panel_ui import PAGE

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
PID_PATH = ROOT / "salida" / "bot.pid"
LOG_PATH = ROOT / "salida" / "bot.log"
TAREA = "Bot Instagram ivkomaderas"
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _hora_local_guardada(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone(config.ZONA).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return ""


def _hora_iso(value: str | None) -> str | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.ZONA)
    return moment.isoformat()


def _title(row) -> str:
    source = Path(row["foto_local"]).stem if row["foto_local"] else (row["tema"] or f"Borrador {row['id']}")
    if row["auto"]:
        return row["tema"] or source
    for suffix in ("_antes_despues", "_historia", "_post"):
        if source.endswith(suffix):
            return source[:-len(suffix)]
    return source


def _recargar_config() -> None:
    """Aplica en el panel los cambios recién guardados en .env."""
    for clave, valor in dotenv_values(ENV_PATH).items():
        os.environ[clave] = valor or ""
    importlib.reload(config)


# ---------- Estado del bot ----------
def bot_corriendo() -> bool:
    """El bot reserva un puerto local mientras corre, sin importar quién lo inició."""
    prueba = socket.socket()
    try:
        prueba.bind(("127.0.0.1", 8766))
        return False
    except OSError:
        return True
    finally:
        prueba.close()


def _python_sin_consola() -> str:
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return str(pyw if pyw.exists() else sys.executable)


def _start_bot() -> None:
    if bot_corriendo():
        return
    subprocess.Popen([_python_sin_consola(), "-u", "main.py", "run", "--archivo-log"], cwd=ROOT,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=SIN_VENTANA | getattr(subprocess, "DETACHED_PROCESS", 0))
    for _ in range(20):
        if bot_corriendo():
            return
        time.sleep(0.25)


def _stop_bot() -> None:
    if not bot_corriendo():
        return
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        raise ValueError("No pude detener el bot. Cerralo desde el Administrador de tareas (pythonw.exe).") from None
    for _ in range(20):
        if not bot_corriendo():
            return
        time.sleep(0.25)


def _restart_bot() -> bool:
    if not bot_corriendo():
        return False
    _stop_bot()
    _start_bot()
    return True


def _acceso_inicio() -> Path:
    """Acceso directo en la carpeta Inicio del usuario: arranca el bot al iniciar sesión, sin pedir administrador."""
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{TAREA}.lnk"


def autostart_activo() -> bool:
    return _acceso_inicio().exists()


def _autostart(activar: bool) -> None:
    acceso = _acceso_inicio()
    if not activar:
        acceso.unlink(missing_ok=True)
        return
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:ACCESO);"
        "$s.TargetPath=$env:PYW;$s.Arguments=$env:ARGS;$s.WorkingDirectory=$env:DIR;"
        "$s.WindowStyle=7;$s.Description='Bot de Instagram @ivkomaderas_';$s.Save()")
    entorno = dict(os.environ, ACCESO=str(acceso), PYW=str(ROOT / "venv" / "Scripts" / "pythonw.exe"),
                   ARGS=f'"{ROOT / "main.py"}" run --archivo-log', DIR=str(ROOT))
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                       env=entorno, creationflags=SIN_VENTANA)
    if r.returncode != 0 or not acceso.exists():
        raise ValueError("Windows no permitió crear el acceso de inicio automático: " + (r.stderr or r.stdout).strip()[:200])


def log_reciente(lineas: int = 40) -> list[str]:
    if not LOG_PATH.exists():
        return []
    with LOG_PATH.open("rb") as f:
        f.seek(max(0, LOG_PATH.stat().st_size - 20000))
        texto = f.read().decode("utf-8", "replace")
    return [l for l in texto.splitlines() if l.strip()][-lineas:]


# ---------- Datos para la interfaz ----------
def get_posts() -> list[dict]:
    env = dotenv_values(ENV_PATH)
    with db.conexion() as con:
        rows = con.execute("SELECT * FROM publicaciones ORDER BY id DESC").fetchall()
    result = []
    for r in rows:
        f = Path(r["foto_local"]) if r["foto_local"] else None
        result.append({
            "id": r["id"], "estado": r["estado"], "tipo": r["tipo"], "caption": r["caption"],
            "foto": str(f) if f else "", "programada_para": r["programada_para"] or "",
            "display_time": _hora_local_guardada(r["programada_para"]), "name": _title(r),
            "story_text": r["story_text"] or env.get("TEXTO_HISTORIA", config.TEXTO_HISTORIA),
            "story_cta": r["story_cta"] or env.get("CTA_HISTORIA", config.CTA_HISTORIA),
            "error": r["error"] or "", "published": r["ig_media_id"] or "", "auto": bool(r["auto"]),
            "intentos": r["intentos"],
            "updated": str(f.stat().st_mtime_ns) if f and f.exists() else "0",
        })
    return result


def get_summary() -> dict:
    counts = {k: 0 for k in ("borrador", "aprobada", "publicada", "error")}
    with db.conexion() as con:
        for r in con.execute("SELECT estado,COUNT(*) n FROM publicaciones GROUP BY estado"):
            counts[r["estado"]] = r["n"]
        pending = con.execute("SELECT COUNT(*) n FROM mensajes WHERE estado='pendiente_humano'").fetchone()["n"]
        upcoming_rows = con.execute(
            "SELECT * FROM publicaciones WHERE estado='aprobada' ORDER BY programada_para LIMIT 8"
        ).fetchall()
        errores = con.execute("SELECT id, tipo, error FROM publicaciones WHERE estado='error' "
                              "ORDER BY id DESC LIMIT 5").fetchall()
    try:
        pending_photos = len(fotos._agrupar())
    except OSError:
        pending_photos = 0
    settings = dotenv_values(ENV_PATH)
    upcoming = [{"id": p["id"], "name": _title(p), "tipo": p["tipo"], "estado": p["estado"], "auto": bool(p["auto"]),
                 "display_time": _hora_local_guardada(p["programada_para"])} for p in upcoming_rows]
    return {"counts": counts, "messages_pending": pending, "upcoming": upcoming,
            "pending_photos": pending_photos, "running": bot_corriendo(), "autostart": autostart_activo(),
            "autopilot": settings.get("AUTOPILOTO", "si").lower() == "si",
            "drive": bool(settings.get("DRIVE_CARPETA_ID")) and config.GOOGLE_CREDENCIALES.exists(),
            "bank": planificador.resumen_banco(),
            "telegram": bool(settings.get("TELEGRAM_BOT_TOKEN") and settings.get("TELEGRAM_CHAT_ID")),
            "ai": bool(settings.get("ANTHROPIC_API_KEY")),
            "token_renewed": db.obtener_ajuste("token_renovado"),
            "errors": [dict(e) for e in errores], "log": log_reciente()}


def get_bank() -> dict:
    with db.conexion() as con:
        rows = con.execute("SELECT * FROM banco_fotos ORDER BY activa DESC, usos_post, agregada").fetchall()
        programadas = {r[0] for r in con.execute(
            "SELECT banco_id FROM publicaciones WHERE estado='aprobada' AND banco_id IS NOT NULL")}
    return {"summary": planificador.resumen_banco(),
            "photos": [{"id": r["id"], "nombre": r["nombre"], "origen": r["origen"], "activa": bool(r["activa"]),
                        "usos_post": r["usos_post"], "usos_historia": r["usos_historia"],
                        "ultimo_uso": _hora_local_guardada(r["ultimo_uso"]).replace("T", " "),
                        "agregada": _hora_local_guardada(r["agregada"]).replace("T", " "),
                        "programada": r["id"] in programadas, "uso": r["uso"]} for r in rows]}


def get_ads() -> dict:
    env = dotenv_values(ENV_PATH)
    try:
        gasto = json.loads(db.obtener_ajuste("promo_gasto", "{}"))
    except ValueError:
        gasto = {}
    promocionados = {p["media_id"]: p for p in publicidad.posts_promocionados()}
    posts, error_posts = [], ""
    try:
        for m in ig_api.listar_media(30, "media_url,thumbnail_url"):
            if m.get("media_type") == "VIDEO" and not m.get("thumbnail_url"):
                continue
            p = promocionados.get(m["id"], {})
            posts.append({"id": m["id"], "caption": (m.get("caption") or "")[:140], "fecha": (m.get("timestamp") or "")[:10],
                          "imagen": m.get("thumbnail_url") or m.get("media_url") or "", "link": m.get("permalink", ""),
                          "likes": m.get("like_count", 0), "promocionado": bool(p.get("activo"))})
    except Exception as e:  # sin conexión: igual se muestran las promocionadas
        error_posts = str(e)
    fila = publicidad._fila_mes()
    return {"configurada": bool(env.get("META_ADS_TOKEN") and env.get("META_AD_ACCOUNT_ID")),
            "cuenta": env.get("META_AD_ACCOUNT_ID", ""), "activa": env.get("PROMO_ACTIVA", "no") == "si",
            "presupuesto": env.get("PROMO_PRESUPUESTO_MENSUAL", ""), "objetivo": env.get("PROMO_OBJETIVO", "mensajes"),
            "zonas": publicidad.zonas(), "gasto": gasto, "campana_mes": bool(fila), "posts": posts,
            "error_posts": error_posts,
            "promocionados": [p for p in promocionados.values() if p["activo"]]}


def _guardar_env(clave: str, valor: str) -> None:
    set_key(str(ENV_PATH), clave, valor, quote_mode="always")
    os.environ[clave] = valor


def get_messages() -> dict:
    with db.conexion() as con:
        rows = con.execute("SELECT * FROM mensajes ORDER BY recibido DESC LIMIT 200").fetchall()
    env = dotenv_values(ENV_PATH)
    return {"telegram": bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")),
            "messages": [dict(r) for r in rows]}


def get_metrics() -> dict:
    with db.conexion() as con:
        media = con.execute("SELECT * FROM media WHERE publicada_en IS NOT NULL").fetchall()
        top = con.execute("SELECT caption,publicada_en,likes,comentarios,permalink FROM media "
                          "ORDER BY likes+3*comentarios DESC LIMIT 10").fetchall()
    settings = {r["clave"]: r["valor"] for r in _settings_rows()}
    try:
        audience = json.loads(settings.get("audience_online", "[]"))
    except (ValueError, TypeError):
        audience = []
    followers = settings.get("followers")
    reach = settings.get("daily_reach")
    return {
        "count": len(media), "likes": sum(r["likes"] for r in media),
        "comments": sum(r["comentarios"] for r in media),
        "followers": int(followers) if followers and str(followers).isdigit() else None,
        "reach": int(reach) if reach and str(reach).isdigit() else None,
        "ranking": metricas.resumen_horarios(),
        "audience": audience,
        "audience_note": ("Horarios de audiencia informados por Instagram; disponibles si Meta habilita ese dato para la cuenta."
                          if audience else
                          "Meta no entregó horarios de audiencia. Se muestran horarios de interacción observada como referencia; "
                          "con menos de 15 publicaciones el resultado es provisional."),
        "top": [{"caption": r["caption"], "published": r["publicada_en"] or "",
                 "likes": r["likes"], "comentarios": r["comentarios"], "permalink": r["permalink"] or ""}
                for r in top],
    }


def _settings_rows():
    with db.conexion() as con:
        return con.execute("SELECT clave,valor FROM ajustes").fetchall()


def _color_hex(rgb: str) -> str:
    try:
        r, g, b = (int(x) for x in rgb.split(",")[:3])
        return f"#{r:02x}{g:02x}{b:02x}"
    except ValueError:
        return "#b5651d"


def get_settings() -> dict:
    env = dotenv_values(ENV_PATH)
    cred = config.GOOGLE_CREDENCIALES
    return {
        "telegram": bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")),
        "telegram_token": bool(env.get("TELEGRAM_BOT_TOKEN")),
        "telegram_chat_id": env.get("TELEGRAM_CHAT_ID", ""),
        "ai_key": bool(env.get("ANTHROPIC_API_KEY")),
        "ai_enabled": env.get("MODO_SIN_IA", "si").lower() == "no",
        "auto_replies": env.get("RESPUESTAS_AUTOMATICAS", "no").lower() == "si",
        "business": env.get("DESCRIPCION_NEGOCIO", config.DESCRIPCION_NEGOCIO),
        "closing": env.get("CIERRE_POST", config.CIERRE_POST),
        "hashtags": env.get("HASHTAGS", config.HASHTAGS),
        "story_text": env.get("TEXTO_HISTORIA", config.TEXTO_HISTORIA),
        "story_cta": env.get("CTA_HISTORIA", config.CTA_HISTORIA),
        "timezone": env.get("ZONA_HORARIA", "America/Argentina/Buenos_Aires"),
        "interval": env.get("MINUTOS_ENTRE_REVISIONES", "5"),
        "autopilot": env.get("AUTOPILOTO", "si").lower() == "si",
        "story_hour": env.get("HORA_HISTORIA", str(config.HORA_HISTORIA)),
        "post_notice_days": env.get("DIAS_AVISO_POST", str(config.DIAS_AVISO_POST)),
        "brand": env.get("NOMBRE_MARCA", ""),
        "brand_color": _color_hex(env.get("COLOR_MARCA", "181,101,29")),
        "whatsapp": env.get("WHATSAPP_NUMERO", ""),
        "drive_folder": env.get("DRIVE_CARPETA_ID", ""),
        "drive_credentials": cred.exists(),
        "drive_account": __import__("drive").cuenta_servicio() if cred.exists() else "",
        "autostart": autostart_activo(),
        "running": bot_corriendo(),
        "token_renewed": db.obtener_ajuste("token_renovado"),
    }


def _save_environment(values: dict) -> None:
    allowed = {
        "TELEGRAM_BOT_TOKEN": "telegram_token", "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "ANTHROPIC_API_KEY": "anthropic_key", "DESCRIPCION_NEGOCIO": "business",
        "CIERRE_POST": "closing", "HASHTAGS": "hashtags", "TEXTO_HISTORIA": "story_text",
        "CTA_HISTORIA": "story_cta", "MINUTOS_ENTRE_REVISIONES": "interval",
    }
    for key, field in allowed.items():
        value = values.get(field)
        if value is not None and str(value).strip():
            set_key(str(ENV_PATH), key, str(value).strip(), quote_mode="always")
    # Estos pueden quedar vacíos a propósito (por ejemplo, sin nombre de marca en la historia).
    for key, field in {"NOMBRE_MARCA": "brand", "DRIVE_CARPETA_ID": "drive_folder"}.items():
        if values.get(field) is not None:
            valor = str(values[field]).strip()
            if field == "drive_folder":  # aceptar también la URL completa de la carpeta
                m = re.search(r"folders/([\w-]+)", valor)
                valor = m.group(1) if m else valor
            set_key(str(ENV_PATH), key, valor, quote_mode="always")
    if values.get("whatsapp") is not None:
        numero = re.sub(r"\D", "", str(values["whatsapp"]))
        if numero and not 8 <= len(numero) <= 15:
            raise ValueError("El WhatsApp va con código de país y solo números, por ejemplo 5491112345678.")
        set_key(str(ENV_PATH), "WHATSAPP_NUMERO", numero, quote_mode="always")
    if values.get("story_hour") not in (None, ""):
        hora = int(values["story_hour"])
        if not 0 <= hora <= 23:
            raise ValueError("La hora de la historia va de 0 a 23.")
        set_key(str(ENV_PATH), "HORA_HISTORIA", str(hora), quote_mode="always")
    if values.get("post_notice_days") not in (None, ""):
        dias = int(values["post_notice_days"])
        if not 1 <= dias <= 6:
            raise ValueError("El aviso del post va de 1 a 6 días antes.")
        set_key(str(ENV_PATH), "DIAS_AVISO_POST", str(dias), quote_mode="always")
    color = str(values.get("brand_color") or "")
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        rgb = ",".join(str(int(color[i:i + 2], 16)) for i in (1, 3, 5))
        set_key(str(ENV_PATH), "COLOR_MARCA", rgb, quote_mode="always")
    if values.get("autopilot") is not None:
        set_key(str(ENV_PATH), "AUTOPILOTO", "si" if values["autopilot"] else "no", quote_mode="always")
    ai_on = bool(values.get("ai_enabled"))
    if ai_on and not (values.get("anthropic_key") or dotenv_values(ENV_PATH).get("ANTHROPIC_API_KEY")):
        raise ValueError("Para habilitar la IA falta la clave de Anthropic.")
    if values.get("auto_replies") and not ai_on:
        raise ValueError("Primero habilitá la IA para activar respuestas automáticas.")
    set_key(str(ENV_PATH), "MODO_SIN_IA", "no" if ai_on else "si", quote_mode="always")
    set_key(str(ENV_PATH), "RESPUESTAS_AUTOMATICAS",
            "si" if values.get("auto_replies") else "no", quote_mode="always")


def _save_post(data: dict, approve: bool = False) -> None:
    post_id = int(data["id"])
    moment = _hora_iso(data.get("when"))
    with db.conexion() as con:
        row = con.execute("SELECT * FROM publicaciones WHERE id=?", (post_id,)).fetchone()
    if not row:
        raise ValueError("No encontré esa publicación.")
    if row["estado"] not in {"borrador", "aprobada"}:
        raise ValueError("Solo se pueden editar publicaciones que todavía no salieron.")
    if moment:
        contenido._validar_hora(moment)
    if row["tipo"] == "post":
        if data.get("caption") is not None:
            with db.conexion() as con:
                con.execute("UPDATE publicaciones SET caption=? WHERE id=?", (data["caption"], post_id))
    else:
        text = data.get("story_text")
        cta = data.get("story_cta")
        if (text is not None and text != row["story_text"]) or (cta is not None and cta != row["story_cta"]):
            actual_text = text if text is not None else (row["story_text"] or config.TEXTO_HISTORIA)
            actual_cta = cta if cta is not None else (row["story_cta"] or config.CTA_HISTORIA)
            fotos.regenerar_historia(post_id, actual_text, actual_cta)
    if moment:
        with db.conexion() as con:
            con.execute("UPDATE publicaciones SET programada_para=? WHERE id=?", (moment, post_id))
    if approve and row["estado"] == "borrador":
        contenido.aprobar(post_id)


def _leer_imagen(data: dict) -> tuple[str, bytes]:
    name = Path(str(data.get("name", ""))).name
    if not name or Path(name).suffix.lower() not in fotos.EXT:
        raise ValueError("Usá una imagen JPG, PNG, WebP o HEIC.")
    raw = base64.b64decode(data.get("data", ""), validate=True)
    if not raw or len(raw) > 15 * 1024 * 1024:
        raise ValueError("La imagen debe pesar hasta 15 MB.")
    try:
        with Image.open(BytesIO(raw)) as image:
            image.verify()
    except Exception:
        raise ValueError("El archivo no es una imagen válida.") from None
    return name, raw


def _upload(data: dict) -> list[int]:
    name, raw = _leer_imagen(data)
    if data.get("destino") == "banco":
        with tempfile.TemporaryDirectory() as tmp:
            temporal = Path(tmp) / name
            temporal.write_bytes(raw)
            return [planificador.agregar_archivo(temporal, Path(name).stem)]
    target_dir = ROOT / "fotos"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / name
    if target.exists():
        stem, suffix = target.stem, target.suffix
        n = 2
        while target.exists():
            target = target_dir / f"{stem}_{n}{suffix}"
            n += 1
    target.write_bytes(raw)
    return fotos.procesar_nuevas()


def _guardar_credenciales_google(data: dict) -> str:
    try:
        cred = json.loads(base64.b64decode(data.get("data", ""), validate=True))
    except ValueError:
        raise ValueError("El archivo no es un JSON válido.") from None
    if cred.get("type") != "service_account" or not cred.get("client_email") or not cred.get("private_key"):
        raise ValueError("Ese JSON no es la clave de una cuenta de servicio de Google (falta client_email o private_key).")
    config.GOOGLE_CREDENCIALES.write_text(json.dumps(cred, indent=2), encoding="utf-8")
    import drive
    drive._sesion = None
    return cred["client_email"]


def _bank_image(banco_id: int) -> bytes:
    with db.conexion() as con:
        fila = con.execute("SELECT ruta FROM banco_fotos WHERE id=?", (banco_id,)).fetchone()
    ruta = Path(fila["ruta"]).resolve() if fila else None
    if not ruta or not ruta.is_relative_to(planificador.BANCO.resolve()) or not ruta.is_file():
        raise ValueError
    img = Image.open(ruta)
    img.thumbnail((480, 480))
    buf = BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=82)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send_data(self, data, status=200, content_type="application/json; charset=utf-8"):
        raw = data if isinstance(data, bytes) else data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            return self.send_data(PAGE, content_type="text/html; charset=utf-8")
        rutas = {"/api/summary": get_summary, "/api/posts": lambda: {"posts": get_posts()},
                 "/api/messages": get_messages, "/api/metrics": get_metrics, "/api/settings": get_settings,
                 "/api/bank": get_bank, "/api/ads": get_ads}
        if path in rutas:
            return self.send_data(json.dumps(rutas[path](), ensure_ascii=False))
        if path == "/api/export":
            try:
                if urlsplit(self.path).query == "sync=1":
                    metricas.sincronizar()
                archivo = __import__("exportar").exportar()
            except Exception as e:
                return self.send_data(json.dumps({"error": str(e)}, ensure_ascii=False), 400)
            self.send_response(200)
            datos = archivo.read_bytes()
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", 'attachment; filename="metricas_instagram.xlsx"')
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            return self.wfile.write(datos)
        if path.startswith("/bank/"):
            try:
                return self.send_data(_bank_image(int(path.rsplit("/", 1)[-1])), content_type="image/jpeg")
            except (ValueError, OSError):
                return self.send_data(b"", 404, "text/plain")
        if path.startswith("/image/"):
            try:
                post_id = int(path.rsplit("/", 1)[-1])
                post = next(p for p in get_posts() if p["id"] == post_id)
                image_path = Path(post["foto"]).resolve()
                if not image_path.is_relative_to((ROOT / "salida").resolve()) or not image_path.is_file():
                    raise ValueError
                mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
                return self.send_data(image_path.read_bytes(), content_type=mime)
            except (ValueError, StopIteration, OSError):
                return self.send_data(b"", 404, "text/plain")
        return self.send_data("No encontrado", 404, "text/plain")

    def _json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 22 * 1024 * 1024:
            raise ValueError("La solicitud supera el límite de 22 MB.")
        return json.loads(self.rfile.read(size) or b"{}")

    def _action(self, data: dict) -> dict:
        action = data.get("action")
        if action == "start":
            _start_bot()
            if not bot_corriendo():
                raise ValueError("El bot no arrancó. Mirá la actividad reciente en Resumen.")
        elif action == "stop":
            _stop_bot()
        elif action == "restart":
            _stop_bot()
            _start_bot()
        elif action in {"save_post", "approve"}:
            _save_post(data, approve=action == "approve")
        elif action == "reject":
            with db.conexion() as con:
                row = con.execute("SELECT estado FROM publicaciones WHERE id=?", (int(data["id"]),)).fetchone()
            if not row or row["estado"] not in {"borrador", "aprobada", "error"}:
                raise ValueError("Esa publicación ya no se puede cancelar.")
            contenido.rechazar(int(data["id"]))
        elif action == "retry":
            cuando = (datetime.now(config.ZONA) + timedelta(minutes=2)).isoformat()
            with db.conexion() as con:
                n = con.execute("UPDATE publicaciones SET estado='aprobada', intentos=0, error=NULL, "
                                "programada_para=? WHERE id=? AND estado='error'", (cuando, int(data["id"]))).rowcount
            if not n:
                raise ValueError("Solo se pueden reintentar publicaciones con error.")
            return {"message": "Se reintentará en 2 minutos (con el bot encendido)."}
        elif action in {"swap", "skip"}:
            return {"message": planificador.accion("otra" if action == "swap" else "saltar", int(data["id"]))}
        elif action == "plan_now":
            _recargar_config()
            nuevas = planificador.importar()
            creadas = planificador.planificar()
            return {"message": f"Fotos nuevas: {nuevas}. Publicaciones programadas: {len(creadas)}."}
        elif action == "bank_toggle":
            with db.conexion() as con:
                con.execute("UPDATE banco_fotos SET activa=1-activa WHERE id=?", (int(data["id"]),))
        elif action == "bank_rename":
            nombre, uso = planificador.separar_uso(str(data.get("nombre", "")))
            if uso:
                with db.conexion() as con:
                    con.execute("UPDATE banco_fotos SET uso=? WHERE id=?", (uso, int(data["id"])))
            if not nombre or len(nombre) > 2000:
                raise ValueError("Escribí el pie de foto (hasta 2000 caracteres).")
            with db.conexion() as con:
                con.execute("UPDATE banco_fotos SET nombre=? WHERE id=?", (nombre, int(data["id"])))
        elif action == "bank_reset":
            with db.conexion() as con:
                con.execute("UPDATE banco_fotos SET usos_post=0 WHERE id=?", (int(data["id"]),))
        elif action == "bank_use":
            uso = str(data.get("uso", ""))
            if uso not in {"", "historia", "post"}:
                raise ValueError("Destino inválido.")
            with db.conexion() as con:
                con.execute("UPDATE banco_fotos SET uso=? WHERE id=?", (uso, int(data["id"])))
        elif action == "reply":
            text = str(data.get("text", "")).strip()
            if not text or len(text) > 1000:
                raise ValueError("La respuesta debe tener entre 1 y 1000 caracteres.")
            bandeja = __import__("bandeja")
            bandeja.responder_manual(str(data["ig_msg_id"]), text)
        elif action == "sync_metrics":
            synced = metricas.sincronizar()
            profile = ig_api.perfil()
            db.guardar_ajuste("followers", str(profile.get("followers_count", "")))
            daily_reach = None
            try:
                daily_reach = sum(v.get("value", 0) for v in ig_api.alcance_diario(14))
                db.guardar_ajuste("daily_reach", str(daily_reach))
            except ig_api.ErrorInstagram:
                pass
            audience_ok = False
            try:
                db.guardar_ajuste("audience_online", json.dumps(ig_api.audiencia_online()))
                audience_ok = True
            except ig_api.ErrorInstagram as exc:
                db.guardar_ajuste("audience_error", str(exc)[:250])
            with db.conexion() as con:
                con.execute("INSERT OR REPLACE INTO metricas_diarias(fecha,seguidores,alcance,publicaciones) "
                            "VALUES(?,?,?,?)", (datetime.now(config.ZONA).date().isoformat(),
                                                profile.get("followers_count"), daily_reach, profile.get("media_count")))
            return {"synced": synced, "audience_available": audience_ok}
        elif action == "test_instagram":
            profile = ig_api.perfil()
            username = (profile.get("username") or "").casefold()
            if username != config.INSTAGRAM_USERNAME:
                raise ValueError("La conexión no corresponde a la cuenta autorizada.")
            return {"message": f"Conexión correcta con @{username}."}
        elif action == "renew_token":
            dias = ig_api.renovar_token()
            db.guardar_ajuste("token_renovado", datetime.now(config.ZONA).date().isoformat())
            _restart_bot()
            return {"message": f"Token renovado: vale {dias} días más."}
        elif action == "test_telegram":
            _recargar_config()
            if not (config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID):
                raise ValueError("Primero guardá el token y el Chat ID de Telegram.")
            telegram_bridge._call(config.TELEGRAM_TOKEN, "sendMessage", chat_id=config.TELEGRAM_CHAT_ID,
                                  text="✅ Prueba del panel: las notificaciones del bot de @"
                                       f"{config.INSTAGRAM_USERNAME} llegan bien.")
            return {"message": "Mensaje de prueba enviado. Revisá Telegram."}
        elif action == "test_drive":
            _recargar_config()
            import drive
            if not drive.configurado():
                raise ValueError("Falta la carpeta de Drive.")
            nuevas = drive.fotos_nuevas()
            return {"message": f"Drive conectado. Fotos esperando en la carpeta: {len(nuevas)}."}
        elif action == "google_credentials":
            return {"message": f"Credenciales guardadas. Compartí la carpeta de Drive como Editor con: "
                               f"{_guardar_credenciales_google(data)}"}
        elif action == "autostart":
            _autostart(bool(data.get("on")))
            if data.get("on"):
                _start_bot()
        elif action == "ads_accounts":
            token = str(data.get("token", "")).strip()
            if not token:
                raise ValueError("Pegá el token de Meta.")
            os.environ["META_ADS_TOKEN"] = token
            try:
                cuentas = publicidad._api("GET", "me/adaccounts", fields="account_id,name,currency,account_status")
            except publicidad.ErrorPublicidad:
                os.environ["META_ADS_TOKEN"] = dotenv_values(ENV_PATH).get("META_ADS_TOKEN", "") or ""
                raise
            _guardar_env("META_ADS_TOKEN", token)
            db.guardar_ajuste("promo_page_id", "")
            db.guardar_ajuste("promo_ig_id", "")
            return {"cuentas": [{"id": c["account_id"], "nombre": c.get("name", ""), "moneda": c.get("currency", "")}
                                for c in cuentas.get("data", [])]}
        elif action == "ads_account":
            _guardar_env("META_AD_ACCOUNT_ID", str(data.get("id", "")).strip())
            info = publicidad.cuenta_info()
            return {"message": f"Conectado: {info['nombre']} ({info['moneda']}), cuenta {info['estado']}. "
                               "Página e Instagram vinculados encontrados."}
        elif action == "ads_check":
            info = publicidad.cuenta_info()
            return {"message": f"Cuenta {info['nombre']} ({info['moneda']}): {info['estado']}. Página e Instagram OK."}
        elif action == "ads_search":
            return {"zonas": publicidad.buscar_zonas(str(data.get("q", "")).strip())}
        elif action == "ads_zones":
            lista = data.get("zonas") or []
            for z in lista:
                if not z.get("key") or z.get("tipo") not in {"city", "region", "neighborhood", "zip", "subcity"}:
                    raise ValueError("Zona inválida.")
                if z.get("radio") and not 17 <= int(z["radio"]) <= 80:
                    raise ValueError("El radio alrededor de una ciudad va de 17 a 80 km.")
            publicidad.guardar_zonas(lista)
            return {"message": publicidad.aplicar_zonas() if publicidad.configurada() and publicidad.activa()
                    else "Zonas guardadas."}
        elif action == "ads_budget":
            monto = int(float(data.get("monto", 0)))
            if monto <= 0:
                raise ValueError("El presupuesto mensual tiene que ser mayor a cero.")
            _guardar_env("PROMO_PRESUPUESTO_MENSUAL", str(monto))
            if data.get("objetivo") in {"mensajes", "alcance"}:
                _guardar_env("PROMO_OBJETIVO", data["objetivo"])
            return {"message": publicidad.aplicar_presupuesto() if publicidad.configurada() and publicidad.activa()
                    else "Presupuesto guardado."}
        elif action == "ads_toggle":
            encender = bool(data.get("on"))
            if encender:
                if not publicidad.configurada():
                    raise ValueError("Primero conectá la cuenta publicitaria.")
                if publicidad.presupuesto_mensual() <= 0 or not publicidad.zonas():
                    raise ValueError("Definí el presupuesto mensual y al menos una zona.")
            _guardar_env("PROMO_ACTIVA", "si" if encender else "no")
            if encender:
                mensaje = (publicidad.pausar(False) if publicidad._fila_mes() else
                           publicidad.crear_mes() if any(p["activo"] for p in publicidad.posts_promocionados())
                           else "Publicidad activada. Elegí qué publicaciones promocionar.")
            else:
                mensaje = publicidad.pausar(True)
            _restart_bot()
            return {"message": mensaje}
        elif action == "ads_promote":
            return {"message": publicidad.agregar_post(str(data["id"]), str(data.get("caption", "")),
                                                       str(data.get("link", "")))}
        elif action == "ads_unpromote":
            return {"message": publicidad.quitar_post(str(data["id"]))}
        elif action == "ads_refresh":
            g = publicidad.gasto_del_mes()
            return {"message": "Gasto actualizado." if g.get("moneda") else "Todavía no hay campaña este mes."}
        elif action == "upload":
            return {"ids": _upload(data)}
        else:
            raise ValueError("Acción no reconocida.")
        return {"ok": True}

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            data = self._json()
            if path == "/api/action":
                return self.send_data(json.dumps(self._action(data), ensure_ascii=False))
            if path == "/api/upload":
                return self.send_data(json.dumps({"ids": _upload(data)}))
            if path == "/api/telegram/discover":
                env = dotenv_values(ENV_PATH)
                token = env.get("TELEGRAM_BOT_TOKEN", "")
                if not token:
                    raise ValueError("Primero guardá el token de tu bot de BotFather.")
                return self.send_data(json.dumps({"chats": telegram_bridge.descubrir_chats(token)}, ensure_ascii=False))
            if path == "/api/settings":
                env = dotenv_values(ENV_PATH)
                token = str(data.get("telegram_token", "")).strip()
                chat_id = str(data.get("telegram_chat_id", "")).strip()
                if token:
                    bot = telegram_bridge.obtener_bot(token)
                    if not bot.get("is_bot"):
                        raise ValueError("El token no corresponde a un bot de Telegram.")
                    set_key(str(ENV_PATH), "TELEGRAM_BOT_TOKEN", token, quote_mode="always")
                    if token != env.get("TELEGRAM_BOT_TOKEN", "") and not chat_id:
                        set_key(str(ENV_PATH), "TELEGRAM_CHAT_ID", "", quote_mode="always")
                if chat_id and chat_id != env.get("TELEGRAM_CHAT_ID", ""):
                    current_token = token or env.get("TELEGRAM_BOT_TOKEN", "")
                    if not current_token:
                        raise ValueError("Guardá primero el token del bot.")
                    telegram_bridge.verificar_chat(current_token, chat_id)
                    set_key(str(ENV_PATH), "TELEGRAM_CHAT_ID", chat_id, quote_mode="always")
                if data.get("anthropic_key"):
                    set_key(str(ENV_PATH), "ANTHROPIC_API_KEY", str(data["anthropic_key"]).strip(), quote_mode="always")
                _save_environment(data)
                _recargar_config()
                return self.send_data(json.dumps({"ok": True, "restarted": _restart_bot()}))
            return self.send_data(json.dumps({"error": "No encontrado"}), 404)
        except (Exception, SystemExit) as exc:
            message = str(exc) if not isinstance(exc, SystemExit) else str(exc)
            return self.send_data(json.dumps({"error": message or "No se pudo completar la operación."},
                                             ensure_ascii=False), 400)


if __name__ == "__main__":
    db.iniciar()
    print("Panel local: http://127.0.0.1:8765")
    try:
        ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
    except KeyboardInterrupt:
        pass  # el bot sigue funcionando aunque se cierre el panel
