"""Bandeja de mensajes: clasifica cada DM, responde lo simple y te deriva lo importante."""
import re

import requests

import config
import db
import ia
import ig_api
import negocio

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

SISTEMA_LOCAL = """Sos el asistente de Instagram de {nombre}. Hablás en español rioplatense y seguís este tono: {tono}.
Respondé corto, máximo 3 oraciones. Nunca inventes precios, plazos ni datos que no estén en la información. No des precios ni plazos de presupuesto: los agrega el sistema. No escribas números de teléfono ni links: el sistema agrega el WhatsApp. Hablale al cliente de vos.
Usá esta información del negocio:
{contexto}

Leé toda la conversación (los m² pueden aparecer en un mensaje anterior), pero clasificá y respondé SOLO el ÚLTIMO mensaje del cliente. Si ya se le pasó un presupuesto y ahora hace otra pregunta, la intención es "pregunta", no "cotizar".
Si la conversación ya empezó, no saludes de nuevo (nada de "¡Hola!"). No ofrezcas una visita si ya se ofreció antes en la conversación. No supongas un servicio que el cliente no nombró.
Devolvé SOLO este JSON con todas las claves: {{"intencion":"cotizar|visita|derivar|pregunta|saludo|importante","servicio":"nombre exacto de la lista o pulido o cadena vacía","terminacion":"o cadena vacía","m2": número o null,"respuesta":"...","resumen":"una línea para el dueño"}}.
- cotizar: pide precio de un servicio cotizable. Si pregunta por pulido sin indicar hidrolaqueado o plastificado, servicio debe ser pulido. Si faltan m², preguntá cuántos aproximados son y si quiere brillante, satinado o mate, y ofrecé coordinar una visita.
- visita: prefiere que vayamos a ver o medir.
- derivar: servicio no cotizable (colocaciones, escaleras, reparaciones) o consulta fuera de la información.
- pregunta: duda respondible con la información y las preguntas frecuentes.
- saludo: saludo o agradecimiento; respondé breve y preguntá en qué podés ayudar.
- importante: reclamo, queja, insulto, acusación o comentario negativo sobre el negocio o el equipo; cliente con trabajo en curso; proveedor, propuesta comercial, búsqueda de trabajo u otra empresa; pedido de datos personales de alguien del equipo; mensaje raro o fuera de tema. Respuesta vacía: lo atiende el dueño."""


_PARA_EL_DUENO = re.compile(
    r"ladr|estaf|chorr|roban|robar|drog|caca|mierda|pelotud|boludo|forro|trucho|denunc|"
    r"busc\w* (personal|trabajo|empleo)|necesit\w* personal|\bpersonal\b\s*\?|empleo|curricul|\bcv\b")


def _analizar_local(historial: str, texto_cliente: str = "") -> dict:
    datos = {"intencion": "importante", "servicio": "", "terminacion": "", "m2": None,
             "respuesta": "", "resumen": ""}
    lineas = historial.splitlines()
    ultimo_cliente = next((l.split(":", 1)[1] for l in reversed(lineas) if l.lower().startswith("cliente:")), "")
    if _PARA_EL_DUENO.search(negocio._normalizar(ultimo_cliente)):  # insultos, acusaciones, búsquedas de trabajo
        return {"intencion": "importante", "servicio": "", "m2": None,
                "categoria": "importante", "respuesta": "", "aviso": ""}
    try:
        contexto = negocio.contexto()
        prompt = SISTEMA_LOCAL.format(nombre=negocio._datos.get("Nombre del negocio", "el negocio"),
                                      tono=negocio._datos.get("Tono de las respuestas", "cordial y cercano"),
                                      contexto=contexto)
        r = ia.preguntar_json(prompt, f"Conversación completa:\n{historial}", 600)
        if not isinstance(r, dict) or "intencion" not in r:
            raise ValueError("JSON incompleto")
        intencion = r.get("intencion")
        if intencion not in {"cotizar", "visita", "derivar", "pregunta", "saludo", "importante"}:
            raise ValueError("Intención inválida")
        # servicio, terminación y m² salen primero de lo que escribió el cliente; la IA solo completa lo que falte
        dichos = "\n".join(l.split(":", 1)[1] for l in historial.splitlines() if l.lower().startswith("cliente:"))
        det_servicio, det_terminacion, det_m2 = negocio.detectar(dichos or historial)
        servicio = negocio.resolver_servicio(r.get("servicio"))
        if det_servicio and (det_servicio != "pulido" or not servicio):
            servicio = det_servicio
        terminacion = det_terminacion or str(r.get("terminacion") or "")
        # los m² tienen que salir de lo que escribió el cliente: la IA a veces los inventa
        m2 = det_m2
        if intencion == "derivar":  # texto fijo y solo con el servicio que el cliente nombró en su último mensaje
            nombrado = negocio.detectar(ultimo_cliente)[0]
            if nombrado and nombrado != "pulido":
                servicio = nombrado
                r["respuesta"] = f"{servicio} lo cotizamos personalmente, así te pasamos un precio justo."
            else:
                servicio = ""
                r["respuesta"] = "Esa consulta te la responde alguien del equipo."
        if any(l.lower().startswith("yo:") for l in lineas):  # conversación empezada: sin "¡Hola!" de nuevo
            r["respuesta"] = re.sub(r"^\s*¡?hola[!.,]*\s*", "", str(r.get("respuesta") or ""), flags=re.I)
            r["respuesta"] = r["respuesta"][:1].upper() + r["respuesta"][1:]
        if any(l.lower().startswith("yo:") and "aproximado es" in l for l in lineas) or "aproximado es" in historial:
            m2 = negocio.detectar(ultimo_cliente)[2]  # ya se cotizó: solo se recotiza si el cliente da m² nuevos
        cot = None
        if intencion in {"cotizar", "derivar"} and servicio and m2 and m2 > 0:
            cot = negocio.cotizar(servicio, terminacion, m2)
            if cot:
                intencion = "cotizar"
        numero = negocio.whatsapp()
        if numero:  # el link lo agrega el sistema: no repetir el número suelto
            for n in {numero, numero[3:]}:
                r["respuesta"] = str(r.get("respuesta") or "").replace(n, "").replace(":  ", ": ")
        if intencion == "cotizar" and m2 and m2 > 0:
            if cot:
                respuesta = cot + ("" if "coordinemos una visita" in historial
                                   else "\n\n¿Querés que coordinemos una visita para confirmarlo?")
                categoria = "interes_compra"
            else:
                intencion, categoria = "derivar", "interes_compra"
                respuesta = str(r.get("respuesta") or "¡Gracias por escribir!").strip()
                numero = negocio.whatsapp()
                if numero:
                    respuesta += f"\n\nPara coordinar, escribinos por WhatsApp: https://wa.me/{numero}"
        elif intencion in {"visita", "derivar"}:
            categoria = "interes_compra"
            respuesta = str(r.get("respuesta") or "¡Gracias por escribir!").strip()
            numero = negocio.whatsapp()
            if numero:
                respuesta += f"\n\nPara coordinar{' la visita' if intencion == 'visita' else ''}, escribinos por WhatsApp: https://wa.me/{numero}"
        elif intencion == "cotizar":
            categoria = "interes_compra"
            respuesta = str(r.get("respuesta") or "¿Cuántos m² aproximados son?").strip()
        elif intencion in {"pregunta", "saludo"}:
            categoria = "generico"
            respuesta = str(r.get("respuesta") or "").strip()
        else:
            categoria, respuesta = "importante", ""
        aviso = ""
        if intencion in {"visita", "derivar"}:
            etiqueta = "🔔 Pidió una visita" if intencion == "visita" else "🔔 Consulta para atender vos"
            m2txt = f" · {m2:g} m²" if m2 else ""
            aviso = f"{etiqueta}: {servicio or 'sin servicio'}{m2txt}\nCliente: {texto_cliente[:200]}\nResumen: {r.get('resumen') or ''}"
        return {"intencion": intencion, "servicio": servicio, "m2": m2,
                "categoria": categoria, "respuesta": respuesta, "aviso": aviso}
    except (ValueError, TypeError, KeyError):
        return {"intencion": "importante", "servicio": "", "m2": None,
                "categoria": "importante", "respuesta": "", "aviso": ""}


def simular(historial: str) -> dict:
    if config.IA_PROVEEDOR != "local":
        raise RuntimeError("La simulación está disponible con IA_PROVEEDOR=local")
    texto_cliente = next((l.split(":", 1)[1].strip() for l in reversed(historial.splitlines())
                          if l.lower().startswith("cliente:") and ":" in l), historial[:200])
    return _analizar_local(historial, texto_cliente)


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


def _viejo(mensaje: dict, horas: int = 23) -> bool:
    from datetime import datetime, timedelta, timezone
    try:
        enviado = datetime.fromisoformat(mensaje["created_time"].replace("+0000", "+00:00"))
    except (KeyError, ValueError):
        return False
    return datetime.now(timezone.utc) - enviado > timedelta(hours=horas)


def _pide_borrado(texto: str) -> bool:
    t = negocio._normalizar(texto)
    return "borrar mis datos" in t or "delete my data" in t


def borrar_usuario(usuario: str, ig_msg_id: str) -> None:
    """Pedido de borrado (política de privacidad): elimina todo lo del usuario y lo confirma."""
    with db.conexion() as con:
        ids = [r["ig_msg_id"] for r in con.execute("SELECT ig_msg_id FROM mensajes WHERE ig_usuario_id=?", (usuario,))]
        con.executemany("DELETE FROM telegram_links WHERE ig_msg_id=?", [(i,) for i in ids])
        con.execute("DELETE FROM mensajes WHERE ig_usuario_id=?", (usuario,))
        # queda solo la marca del pedido, sin usuario ni texto, para no procesarlo de nuevo
        con.execute("INSERT OR IGNORE INTO mensajes (ig_msg_id, ig_usuario_id, texto, categoria, respuesta, estado) "
                    "VALUES (?,'','','borrado','','borrado')", (ig_msg_id,))
    try:
        ig_api.enviar_mensaje(usuario, "Listo, eliminamos todos tus datos de nuestros registros.")
    except ig_api.ErrorInstagram:
        pass
    _avisar(f"🗑️ Un usuario pidió borrar sus datos: se eliminaron {len(ids)} mensajes guardados.")


def borrar_antiguos(dias: int = 365) -> None:
    with db.conexion() as con:
        con.execute("DELETE FROM mensajes WHERE recibido < datetime('now', ?)", (f"-{dias} days",))


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

        if _viejo(ultimo):  # Instagram solo deja responder dentro de las 24 h: los viejos se marcan como vistos
            with db.conexion() as con:
                con.execute("INSERT OR IGNORE INTO mensajes (ig_msg_id, ig_usuario_id, texto, categoria, respuesta, estado) "
                            "VALUES (?,?,?,?,?,?)", (ultimo["id"], usuario, texto, "importante", "", "vencido"))
            continue

        if _pide_borrado(texto):
            borrar_usuario(usuario, ultimo["id"])
            continue

        # la IA local solo atiende mensajes: el resto del bot puede seguir sin IA
        if config.MODO_SIN_IA and config.IA_PROVEEDOR != "local":  # sin IA: te avisa y respondés vos desde la app
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
        if config.IA_PROVEEDOR == "local":
            try:
                analisis = simular(historial)
            except RuntimeError as e:
                _avisar(str(e))
                with db.conexion() as con:
                    con.execute("INSERT INTO mensajes (ig_msg_id, ig_usuario_id, texto, categoria, respuesta, estado) "
                                "VALUES (?,?,?,?,?,?)",
                                (ultimo["id"], usuario, texto, "importante", "", "pendiente_humano"))
                _notificar_dm(ultimo["id"], usuario, texto, "importante")
                cuentas["pendientes"] += 1
                continue
            categoria, respuesta = analisis["categoria"], analisis["respuesta"]
            if analisis["aviso"]:
                _avisar(analisis["aviso"])
            estado = "pendiente_humano"
            if categoria == "interes_compra":
                cuentas["compra"] += 1
            elif categoria == "importante":
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
            continue
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
