"""Avisos y respuestas manuales entre Instagram y un bot privado de Telegram."""
from __future__ import annotations

import json
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

import config
import db


_lock = threading.Lock()


def _call(token: str, method: str, **data) -> dict:
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=data, timeout=(10, 25))
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("No se pudo conectar con Telegram. Revisá el token y la conexión.") from None
    if response.status_code >= 400 or not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram rechazó la solicitud."))
    return payload["result"]


def obtener_bot(token: str) -> dict:
    return _call(token, "getMe")


def verificar_chat(token: str, chat_id: str) -> dict:
    chat = _call(token, "getChat", chat_id=chat_id)
    if chat.get("type") != "private":
        raise RuntimeError("Elegí el chat privado que abriste con tu bot, no un grupo.")
    return chat


def descubrir_chats(token: str) -> list[dict]:
    info = _call(token, "getWebhookInfo")
    if info.get("url"):
        raise RuntimeError("Este bot ya tiene un webhook activo. Desconectalo desde Telegram antes de usar el panel.")
    updates = _call(token, "getUpdates", timeout=0, limit=100, allowed_updates=["message"])
    vistos = {}
    for u in updates:
        m = u.get("message", {})
        c = m.get("chat", {})
        if c.get("type") == "private" and (m.get("text", "").startswith("/start") or c.get("id")):
            vistos[str(c.get("id"))] = {"chat_id": str(c.get("id")),
                                        "nombre": " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x),
                                        "usuario": c.get("username", "")}
    return list(vistos.values())


def enviar_dm(ig_msg_id: str, texto: str, categoria: str = "") -> bool:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    tag = " · posible consulta comercial" if categoria == "interes_compra" else ""
    body = (f"Instagram · @{config.INSTAGRAM_USERNAME}{tag}\n"
            f"Mensaje recibido:\n{texto[:3000]}\n\n"
            f"Referencia: {ig_msg_id}\n"
            "Respondé a este mensaje en Telegram para contestar por Instagram.")
    markup = {"force_reply": True, "input_field_placeholder": "Escribí tu respuesta para Instagram"}
    try:
        result = _call(config.TELEGRAM_TOKEN, "sendMessage", chat_id=config.TELEGRAM_CHAT_ID,
                       text=body, reply_markup=markup)
    except RuntimeError:
        return False
    with db.conexion() as con:
        con.execute("INSERT OR REPLACE INTO telegram_links(ig_msg_id,telegram_message_id,chat_id) VALUES(?,?,?)",
                    (ig_msg_id, result["message_id"], str(config.TELEGRAM_CHAT_ID)))
    return True


def _avisar(texto: str) -> None:
    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID:
        try:
            _call(config.TELEGRAM_TOKEN, "sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=texto[:4000])
        except RuntimeError:
            pass


def _log(texto: str) -> None:
    linea = f"[{datetime.now(config.ZONA):%Y-%m-%d %H:%M}] AVISO: {texto}"
    try:
        print(linea, flush=True)
    except UnicodeEncodeError:  # consola de Windows sin soporte para emojis
        print(linea.encode("ascii", "backslashreplace").decode(), flush=True)


def avisar(texto: str) -> None:
    """Deja el aviso en el log y, si Telegram está configurado, te lo manda."""
    _log(texto)
    _avisar(texto)


def enviar_programada(id_pub: int, imagen, texto: str) -> None:
    """Manda la imagen que se va a publicar con botones para cambiarla o saltearla."""
    _log(texto)
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    botones = {"inline_keyboard": [[{"text": "🔄 Otra foto", "callback_data": f"otra:{id_pub}"},
                                    {"text": "⏭ No publicar", "callback_data": f"saltar:{id_pub}"}]]}
    try:
        with open(imagen, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendPhoto",
                          data={"chat_id": config.TELEGRAM_CHAT_ID, "caption": texto[:1000],
                                "reply_markup": json.dumps(botones)},
                          files={"photo": f}, timeout=60)
    except (OSError, requests.RequestException):
        _avisar(texto)


def _boton(cq: dict) -> None:
    import planificador
    chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != str(config.TELEGRAM_CHAT_ID):
        return
    nombre, _, id_pub = (cq.get("data") or "").partition(":")
    try:
        _call(config.TELEGRAM_TOKEN, "answerCallbackQuery", callback_query_id=cq["id"])
        _call(config.TELEGRAM_TOKEN, "editMessageReplyMarkup", chat_id=chat_id,
              message_id=cq["message"]["message_id"], reply_markup={"inline_keyboard": []})
    except RuntimeError:
        pass
    if nombre in {"otra", "saltar"} and id_pub.isdigit():
        try:
            _avisar(planificador.accion(nombre, int(id_pub)))
        except Exception as exc:
            _avisar(f"⚠️ No pude hacer el cambio: {exc}")


def _mapear_respuesta(mensaje: dict) -> tuple[str, str] | None:
    """Devuelve (ig_msg_id, texto) cuando el mensaje responde a un aviso enlazado."""
    texto = (mensaje.get("text") or "").strip()
    reply = mensaje.get("reply_to_message", {})
    chat_id = str(mensaje.get("chat", {}).get("id", ""))
    respuesta = None
    if reply.get("message_id"):
        with db.conexion() as con:
            fila = con.execute("SELECT ig_msg_id FROM telegram_links WHERE telegram_message_id=? AND chat_id=?",
                               (reply["message_id"], chat_id)).fetchone()
        if fila:
            respuesta = (fila["ig_msg_id"], texto)
    if respuesta is None and texto.startswith("/responder "):
        partes = texto.split(maxsplit=2)
        if len(partes) == 3:
            with db.conexion() as con:
                fila = con.execute("SELECT ig_msg_id FROM telegram_links WHERE ig_msg_id=? AND chat_id=?",
                                   (partes[1], chat_id)).fetchone()
            if fila:
                respuesta = (fila["ig_msg_id"], partes[2])
    return respuesta


def _foto_recibida(message: dict) -> bool:
    """Una foto enviada al bot entra al banco; el texto que la acompaña es el pie de foto del post."""
    doc = message.get("document") or {}
    if message.get("photo"):
        file_id = message["photo"][-1]["file_id"]  # la versión de mayor tamaño
    elif doc.get("mime_type", "").startswith("image/") or doc.get("file_name", "").lower().endswith((".heic", ".heif")):
        file_id = doc["file_id"]
    else:
        return False
    import planificador
    try:
        info = _call(config.TELEGRAM_TOKEN, "getFile", file_id=file_id)
        r = requests.get(f"https://api.telegram.org/file/bot{config.TELEGRAM_TOKEN}/{info['file_path']}", timeout=90)
        r.raise_for_status()
    except (RuntimeError, requests.RequestException):
        _avisar("⚠️ No pude descargar la foto desde Telegram. Probá mandarla de nuevo.")
        return True
    caption = (message.get("caption") or "").strip()
    pie, uso = planificador.separar_uso(caption)
    nombre = (pie or f"Foto {datetime.now(config.ZONA):%d-%m-%Y %H.%M}") + (f" ({uso})" if uso else "")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / ("foto" + Path(info["file_path"]).suffix)
        ruta.write_bytes(r.content)
        try:
            banco_id = planificador.agregar_archivo(ruta, nombre, "telegram")
        except Exception:
            _avisar("⚠️ Ese archivo no es una imagen que pueda abrir.")
            return True
    b = planificador.resumen_banco()
    destino = {"historia": "solo para historias", "post": "solo para publicaciones"}.get(uso, "para historias y publicaciones")
    texto = f"📷 Foto #{banco_id} agregada al banco, {destino}."
    if uso == "historia":
        pass  # la historia no lleva pie de foto
    elif pie:
        texto += f"\nPie de foto: {pie}"
    else:
        texto += "\nNo tiene pie de foto: respondé a este mensaje con el texto que querés que lleve cuando salga como post."
    texto += (f"\nBanco: {b['sin_post']} fotos para publicaciones sin usar, {b['historias']} para historias."
              "\nTip: escribí (historia) o (publicacion) junto a la foto para elegir dónde sale.")
    try:
        enviado = _call(config.TELEGRAM_TOKEN, "sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=texto,
                        reply_to_message_id=message.get("message_id"))
        db.guardar_ajuste(f"tg_foto_{enviado['message_id']}", str(banco_id))
    except RuntimeError:
        pass
    return True


def _pie_de_foto(message: dict) -> bool:
    """Si respondés al aviso de una foto recibida, ese texto pasa a ser su pie de foto."""
    reply = (message.get("reply_to_message") or {}).get("message_id")
    texto = (message.get("text") or "").strip()
    banco_id = db.obtener_ajuste(f"tg_foto_{reply}") if reply else ""
    if not banco_id or not texto:
        return False
    import planificador
    pie, uso = planificador.separar_uso(texto)
    with db.conexion() as con:
        if pie:
            con.execute("UPDATE banco_fotos SET nombre=? WHERE id=?", (pie[:2000], int(banco_id)))
        if uso:
            con.execute("UPDATE banco_fotos SET uso=? WHERE id=?", (uso, int(banco_id)))
    cambios = []
    if pie:
        cambios.append("va a salir con ese pie de foto")
    if uso:
        cambios.append("queda solo para " + ("historias" if uso == "historia" else "publicaciones"))
    _avisar(f"✏️ Listo, la foto #{banco_id} " + " y ".join(cambios) + ".")
    return True


def procesar_actualizaciones(timeout: int = 20) -> int:
    """Escucha respuestas del chat configurado; puede correr en un único hilo largo."""
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID or not _lock.acquire(blocking=False):
        return 0
    procesadas = 0
    try:
        info = _call(config.TELEGRAM_TOKEN, "getWebhookInfo")
        if info.get("url"):
            return 0
        offset = int(db.obtener_ajuste("telegram_update_offset", "0") or 0)
        updates = _call(config.TELEGRAM_TOKEN, "getUpdates", offset=offset, timeout=timeout,
                        limit=100, allowed_updates=["message", "callback_query"])
        if not updates:
            return 0
        import bandeja
        for update in updates:
            if "callback_query" in update:
                _boton(update["callback_query"])
                db.guardar_ajuste("telegram_update_offset", str(update["update_id"] + 1))
                procesadas += 1
                continue
            message = update.get("message", {})
            chat = message.get("chat", {})
            if str(chat.get("id", "")) != str(config.TELEGRAM_CHAT_ID):
                db.guardar_ajuste("telegram_update_offset", str(update["update_id"] + 1))
                continue
            text = (message.get("text") or "").strip()
            if message.get("photo") or message.get("document"):
                # se confirma antes de procesar: si algo falla, la foto no se carga dos veces
                db.guardar_ajuste("telegram_update_offset", str(update["update_id"] + 1))
                if not _foto_recibida(message):
                    _avisar("Solo puedo recibir fotos (JPG, PNG, HEIC).")
                procesadas += 1
                continue
            if _pie_de_foto(message):
                pass
            elif text in {"/start", "/ayuda", "/help"}:
                _avisar("Cuando llegue un DM de Instagram, respondé a su aviso en este chat para contestarlo desde @"
                        + config.INSTAGRAM_USERNAME + ". También sirve /responder ID texto.\n"
                        "Mandame fotos y las agrego al banco: el texto que escribas junto a la foto es su pie de foto. "
                        "Agregá (historia) o (publicacion) para elegir dónde sale.\n"
                        "/estado muestra lo próximo que se va a publicar.")
            elif text == "/estado":
                import planificador
                _avisar(planificador.estado())
            else:
                target = _mapear_respuesta(message)
                if target and target[1]:
                    try:
                        bandeja.responder_manual(*target)
                        _avisar("✅ Respuesta enviada por Instagram.")
                    except Exception as exc:
                        _avisar(f"⚠️ No pude enviar la respuesta: {exc}")
                elif text:
                    _avisar("Para responder, usá la opción Responder sobre el aviso del DM. También sirve /responder ID texto.")
            db.guardar_ajuste("telegram_update_offset", str(update["update_id"] + 1))
            procesadas += 1
        return procesadas
    except RuntimeError:
        return procesadas
    finally:
        _lock.release()


def escuchar() -> None:
    while True:
        try:
            procesar_actualizaciones(timeout=20)
        except Exception as exc:  # que un error no apague la escucha de Telegram
            print("Error en Telegram:", repr(exc), flush=True)
            time.sleep(30)
        time.sleep(1)
