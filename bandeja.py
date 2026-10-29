"""Bandeja de mensajes: clasifica cada DM, responde lo simple y te deriva lo importante."""
import requests

import config
import db
import ia
import ig_api

SISTEMA = """Clasificás mensajes directos de Instagram para un negocio de servicios.
Categorías:
- "importante": reclamos, clientes actuales, consultas complejas o delicadas, propuestas comerciales, alianzas,
  prensa, o cualquier cosa que requiera criterio humano.
- "generico": saludos, preguntas simples (horarios, ubicación, cómo funciona), agradecimientos, spam leve.
- "interes_compra": la persona muestra intención clara de contratar/comprar o pide precio/presupuesto/turno.
Para "generico" escribí una respuesta breve, amable y natural (español rioplatense, sin inventar datos que no tengas).
Para "interes_compra" escribí una respuesta breve que agradezca y ofrezca continuar por WhatsApp (el enlace se agrega solo).
Para "importante" dejá "respuesta" vacía.
Devolvé: {"categoria": "...", "respuesta": "...", "resumen": "una línea para el dueño"}"""


def _avisar(texto: str) -> None:
    print("AVISO:", texto)
    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": config.TELEGRAM_CHAT_ID, "text": texto}, timeout=20)
        except requests.RequestException:
            pass


def _notificar_dm(ig_msg_id: str, usuario: str, texto: str, categoria: str) -> None:
    """Entrega el DM a Telegram para que el dueño pueda contestarlo desde ese mismo chat."""
    import telegram_bridge
    if not telegram_bridge.enviar_dm(ig_msg_id, texto, categoria):
        etiqueta = "Posible consulta de compra" if categoria == "interes_compra" else "Mensaje nuevo"
        _avisar(f"📩 {etiqueta} (usuario {usuario}):\n{texto[:200]}\nRespondé desde Instagram.")


def _ya_visto(msg_id: str) -> bool:
    with db.conexion() as con:
        return con.execute("SELECT 1 FROM mensajes WHERE ig_msg_id=?", (msg_id,)).fetchone() is not None


PALABRAS_COMPRA = ("precio", "presupuesto", "cotiz", "cuanto", "cuánto", "costo", "valor", "m2", "metros",
                   "contratar", "turno", "me interesa", "quiero", "necesito", "disponibilidad")


def _parece_compra(texto: str) -> bool:
    t = texto.lower()
    return any(p in t for p in PALABRAS_COMPRA)


def procesar() -> dict:
    mi_id = config.IG_USER_ID()
    cuentas = {"auto": 0, "pendientes": 0, "compra": 0}
    for conv in ig_api.conversaciones_recientes():
        mensajes = conv.get("messages", {}).get("data", [])
        if not mensajes:
            continue
        ultimo = mensajes[0]  # la API devuelve el más reciente primero
        if ultimo["from"]["id"] == mi_id or _ya_visto(ultimo["id"]):
            continue
        usuario = ultimo["from"]["id"]
        texto = ultimo.get("message", "")

        if config.MODO_SIN_IA:  # sin IA: no se responde nada; te avisa y respondés vos desde la app
            compra = _parece_compra(texto)
            with db.conexion() as con:
                con.execute("INSERT INTO mensajes (ig_msg_id, ig_usuario_id, texto, categoria, respuesta, estado) "
                            "VALUES (?,?,?,?,?,?)",
                            (ultimo["id"], usuario, texto, "interes_compra" if compra else "importante", "",
                             "pendiente_humano"))
            _notificar_dm(ultimo["id"], usuario, texto, "interes_compra" if compra else "importante")
            cuentas["compra" if compra else "pendientes"] += 1
            continue

        historial = "\n".join(f"{'Yo' if m['from']['id'] == mi_id else 'Cliente'}: {m.get('message', '')}"
                              for m in reversed(mensajes))
        r = ia.preguntar_json(SISTEMA + f"\nNegocio: {config.DESCRIPCION_NEGOCIO}", f"Conversación:\n{historial}", 600)
        categoria, respuesta = r["categoria"], (r.get("respuesta") or "").strip()
        estado = "pendiente_humano"

        if categoria == "interes_compra":
            if config.WHATSAPP_NUMERO:
                respuesta += f"\n\nEscribime por WhatsApp así avanzamos: https://wa.me/{config.WHATSAPP_NUMERO}"
            cuentas["compra"] += 1
        elif categoria == "importante":
            respuesta = ""
            cuentas["pendientes"] += 1

        if respuesta and config.RESPUESTAS_AUTOMATICAS:
            try:
                ig_api.enviar_mensaje(usuario, respuesta)
                estado = "respondido_auto"
                cuentas["auto"] += 1
            except ig_api.ErrorInstagram as e:
                _avisar(f"No pude responder a {usuario}: {e}")

        with db.conexion() as con:
            con.execute("INSERT INTO mensajes (ig_msg_id, ig_usuario_id, texto, categoria, respuesta, estado) VALUES (?,?,?,?,?,?)",
                        (ultimo["id"], usuario, texto, categoria, respuesta, estado))
        _notificar_dm(ultimo["id"], usuario, texto, categoria)
    return cuentas


def pendientes() -> list[dict]:
    with db.conexion() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM mensajes WHERE estado='pendiente_humano' ORDER BY recibido")]


def responder_manual(ig_msg_id: str, texto: str) -> None:
    with db.conexion() as con:
        fila = con.execute("SELECT ig_usuario_id FROM mensajes WHERE ig_msg_id=?", (ig_msg_id,)).fetchone()
        if not fila:
            raise SystemExit("No encontré ese mensaje. Mirá los IDs con: python main.py bandeja")
        ig_api.enviar_mensaje(fila["ig_usuario_id"], texto)
        con.execute("UPDATE mensajes SET estado='respondido_humano', respuesta=? WHERE ig_msg_id=?", (texto, ig_msg_id))
