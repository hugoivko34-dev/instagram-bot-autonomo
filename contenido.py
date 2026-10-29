"""Generación de publicaciones con Claude + programación en horarios pico + publicación."""
from datetime import datetime, timedelta

import config
import db
import hosting
import ia
import ig_api
import metricas


def _validar_hora(hora_iso: str) -> None:
    try:
        hora = datetime.fromisoformat(hora_iso)
    except ValueError:
        raise SystemExit("La hora debe tener formato ISO, por ejemplo 2026-11-02T19:00:00-03:00.")
    if hora.tzinfo is None:
        raise SystemExit("La hora debe incluir zona horaria, por ejemplo -03:00.")


def generar_borradores(cantidad: int = 3) -> list[int]:
    if config.MODO_SIN_IA:
        raise SystemExit("Este comando necesita IA. En modo sin IA usá las fotos: python main.py fotos")
    with db.conexion() as con:
        mejores = [r["caption"] for r in con.execute(
            "SELECT caption FROM media ORDER BY likes + 3*comentarios DESC LIMIT 3")]
        recientes = [r["tema"] for r in con.execute(
            "SELECT tema FROM publicaciones ORDER BY id DESC LIMIT 10") if r["tema"]]
    sistema = ("Sos community manager experto en Instagram para negocios de servicios en Argentina. "
               "Escribís en español rioplatense, claro y cercano, sin prometer resultados falsos ni inventar datos, "
               "testimonios o precios.")
    usuario = (f"Negocio: {config.DESCRIPCION_NEGOCIO}\n"
               f"Publicaciones que mejor funcionaron: {mejores}\n"
               f"Temas ya usados (no repetir): {recientes}\n\n"
               f"Generá {cantidad} publicaciones distintas. Cada una: "
               '{"tema": "...", "caption": "texto completo con llamada a la acción y 5-8 hashtags", '
               '"idea_imagen": "descripción de la foto/diseño a usar"}. Devolvé una lista JSON.')
    ideas = ia.preguntar_json(sistema, usuario, 3000)
    ids = []
    for i in ideas:
        horario = metricas.proximo_horario_libre()  # lee la base: cada borrador se guarda antes del siguiente
        with db.conexion() as con:
            cur = con.execute(
                "INSERT INTO publicaciones (estado,tema,caption,idea_imagen,programada_para) VALUES ('borrador',?,?,?,?)",
                (i["tema"], i["caption"], i["idea_imagen"], horario.isoformat()))
            ids.append(cur.lastrowid)
    return ids


def aprobar(id_pub: int, image_url: str | None = None, hora_iso: str | None = None) -> None:
    with db.conexion() as con:
        fila = con.execute("SELECT * FROM publicaciones WHERE id=?", (id_pub,)).fetchone()
        if not fila:
            raise SystemExit(f"No existe la publicación {id_pub}")
        if not image_url and not fila["foto_local"]:
            raise SystemExit("Este borrador no tiene foto: pasá --imagen con una URL pública https:// (JPG).")
        if image_url and not image_url.startswith("https://"):
            raise SystemExit("La imagen debe ser una URL pública que empiece con https:// (JPG).")
        hora = hora_iso or fila["programada_para"]
        if hora_iso:
            _validar_hora(hora_iso)
        con.execute("UPDATE publicaciones SET estado='aprobada', image_url=?, programada_para=? WHERE id=?",
                    (image_url, hora, id_pub))


def rechazar(id_pub: int) -> None:
    with db.conexion() as con:
        con.execute("UPDATE publicaciones SET estado='rechazada' WHERE id=?", (id_pub,))


def editar(id_pub: int, caption: str | None = None, hora_iso: str | None = None) -> None:
    """Edita el texto y/o el horario de un borrador antes de aprobarlo."""
    if caption is None and hora_iso is None:
        raise SystemExit("Indicá --texto, --hora o ambos.")
    if hora_iso:
        _validar_hora(hora_iso)
    with db.conexion() as con:
        fila = con.execute("SELECT estado, tipo FROM publicaciones WHERE id=?", (id_pub,)).fetchone()
        if not fila:
            raise SystemExit(f"No existe la publicación {id_pub}")
        if fila["estado"] != "borrador":
            raise SystemExit("Solo se pueden editar borradores; primero rechazá el aprobado si querés reemplazarlo.")
        if caption is not None and fila["tipo"] == "historia":
            raise SystemExit("El texto de la historia está dentro de la imagen; configurá TEXTO_HISTORIA y CTA_HISTORIA en .env antes de crearla.")
        if caption is not None and hora_iso is not None:
            con.execute("UPDATE publicaciones SET caption=?, programada_para=? WHERE id=?",
                        (caption, hora_iso, id_pub))
        elif caption is not None:
            con.execute("UPDATE publicaciones SET caption=? WHERE id=?", (caption, id_pub))
        else:
            con.execute("UPDATE publicaciones SET programada_para=? WHERE id=?", (hora_iso, id_pub))


def recuperar_interrumpidas() -> None:
    """Si el bot se cortó en medio de una publicación, no la repite a ciegas: puede haber salido igual."""
    with db.conexion() as con:
        filas = con.execute("SELECT id, tipo FROM publicaciones WHERE estado='publicando'").fetchall()
        con.execute("UPDATE publicaciones SET estado='error', error='Se interrumpió mientras se publicaba. "
                    "Revisá en Instagram si salió antes de reprogramarla.' WHERE estado='publicando'")
    import telegram_bridge
    for f in filas:
        nombre = "El post" if f["tipo"] == "post" else "La historia"
        telegram_bridge.avisar(f"⚠️ {nombre} #{f['id']} se interrumpió mientras se publicaba. "
                               "Revisá en Instagram si salió.")


def _registrar_uso(banco_id: int | None, tipo: str) -> None:
    if not banco_id:
        return
    campo = "usos_post" if tipo == "post" else "usos_historia"
    with db.conexion() as con:
        con.execute(f"UPDATE banco_fotos SET {campo}={campo}+1, ultimo_uso=? WHERE id=?",
                    (datetime.now(config.ZONA).isoformat(), banco_id))


def publicar_pendientes() -> int:
    """Publica las aprobadas cuya hora ya llegó. Se llama en cada ciclo del bot."""
    import telegram_bridge
    ahora = datetime.now(config.ZONA)
    with db.conexion() as con:
        filas = con.execute("SELECT * FROM publicaciones WHERE estado='aprobada'").fetchall()
    publicadas = 0
    for f in filas:
        hora = datetime.fromisoformat(f["programada_para"])
        if hora > ahora:
            continue
        if f["auto"] and f["tipo"] == "historia" and ahora - hora > timedelta(hours=config.HORAS_MAX_ATRASO_HISTORIA):
            # El bot estuvo apagado: una historia de ayer no se publica hoy, ya se planifica la del día.
            with db.conexion() as con:
                con.execute("UPDATE publicaciones SET estado='vencida' WHERE id=?", (f["id"],))
            continue
        with db.conexion() as con:  # se marca antes de subir para que nunca se publique dos veces
            tomada = con.execute("UPDATE publicaciones SET estado='publicando' WHERE id=? AND estado='aprobada'",
                                 (f["id"],)).rowcount
        if not tomada:
            continue
        url = f["image_url"]
        try:
            url = url or hosting.subir(f["foto_local"])
            media_id = ig_api.publicar_imagen(url, f["caption"], historia=(f["tipo"] == "historia"))
        except Exception as e:
            intentos = f["intentos"] + 1
            estado = "error" if intentos >= config.INTENTOS_PUBLICACION else "aprobada"
            with db.conexion() as con:
                con.execute("UPDATE publicaciones SET estado=?, error=?, intentos=?, image_url=? WHERE id=?",
                            (estado, str(e)[:500], intentos, url, f["id"]))
            if estado == "error":
                telegram_bridge.avisar(f"⚠️ No pude publicar {'el post' if f['tipo'] == 'post' else 'la historia'} #{f['id']} tras {intentos} intentos: {e}")
            continue
        with db.conexion() as con:
            con.execute("UPDATE publicaciones SET estado='publicada', ig_media_id=?, image_url=?, error=NULL WHERE id=?",
                        (media_id, url, f["id"]))
        _registrar_uso(f["banco_id"], f["tipo"])
        publicadas += 1
        telegram_bridge.avisar(f"✅ Publiqué {'el post' if f['tipo'] == 'post' else 'la historia'} #{f['id']}: {f['tema']}")
    return publicadas


def listar(estados=("borrador", "aprobada")) -> list[dict]:
    marcas = ",".join("?" * len(estados))
    with db.conexion() as con:
        return [dict(r) for r in con.execute(
            f"SELECT * FROM publicaciones WHERE estado IN ({marcas}) ORDER BY programada_para", estados)]
