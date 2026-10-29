"""Métricas: sincroniza tus publicaciones, calcula horarios pico y arma reportes."""
from collections import defaultdict
from datetime import datetime, timedelta

import config
import db
import ia
import ig_api

# Horarios de respaldo mientras no haya datos suficientes (hora local, 0=lunes)
HORARIOS_DEFECTO = [(h, d) for d in range(7) for h in (12, 19, 21)]


def sincronizar() -> int:
    items = ig_api.listar_media(50)
    with db.conexion() as con:
        for m in items:
            con.execute(
                "INSERT INTO media (id, publicada_en, tipo, likes, comentarios, caption, permalink) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET publicada_en=excluded.publicada_en, "
                "tipo=excluded.tipo, likes=excluded.likes, comentarios=excluded.comentarios, "
                "caption=excluded.caption, permalink=excluded.permalink",
                (m["id"], m.get("timestamp"), m.get("media_type"), m.get("like_count", 0),
                 m.get("comments_count", 0), m.get("caption", ""), m.get("permalink")),
            )
    for m in items:
        try:
            datos = ig_api.insights_media(m["id"])
        except ig_api.ErrorInstagram:
            continue  # Meta no da insights de algunas publicaciones (muy viejas o anteriores a la cuenta profesional)
        with db.conexion() as con:
            con.execute("UPDATE media SET alcance=?, guardados=?, compartidos=?, vistas=? WHERE id=?",
                        (datos.get("reach"), datos.get("saved"), datos.get("shares"), datos.get("views"), m["id"]))
    registrar_dia()
    return len(items)


def registrar_dia() -> None:
    """Una fila por día con seguidores y alcance, para ver la evolución en Power BI."""
    perfil = ig_api.perfil()
    try:
        valores = ig_api.alcance_diario(2)
        alcance = valores[-1].get("value") if valores else None
    except ig_api.ErrorInstagram:
        alcance = None
    with db.conexion() as con:
        con.execute("INSERT OR REPLACE INTO metricas_diarias(fecha,seguidores,alcance,publicaciones) VALUES(?,?,?,?)",
                    (datetime.now(config.ZONA).date().isoformat(), perfil.get("followers_count"), alcance,
                     perfil.get("media_count")))


def _hora_local(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").astimezone(config.ZONA)


def horarios_pico(cantidad: int = 6) -> list[tuple[int, int]]:
    """Devuelve [(hora, dia_semana)] con mayor interacción promedio. Con pocos datos usa valores por defecto."""
    with db.conexion() as con:
        filas = con.execute("SELECT publicada_en, likes, comentarios FROM media WHERE publicada_en IS NOT NULL").fetchall()
    if len(filas) < 15:
        return HORARIOS_DEFECTO
    acum = defaultdict(list)
    for f in filas:
        t = _hora_local(f["publicada_en"])
        acum[(t.hour, t.weekday())].append(f["likes"] + 3 * f["comentarios"])
    ranking = sorted(acum.items(), key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
    mejores = [k for k, _ in ranking[:cantidad]]
    return mejores or HORARIOS_DEFECTO


def resumen_horarios() -> dict:
    """Ranking explicable por horario, con cantidad de publicaciones y nivel de evidencia."""
    with db.conexion() as con:
        filas = con.execute(
            "SELECT publicada_en, likes, comentarios FROM media WHERE publicada_en IS NOT NULL"
        ).fetchall()
    acum = defaultdict(list)
    por_hora = defaultdict(list)
    for f in filas:
        try:
            t = _hora_local(f["publicada_en"])
        except (ValueError, TypeError):
            continue
        score = f["likes"] + 3 * f["comentarios"]
        acum[(t.weekday(), t.hour)].append(score)
        por_hora[t.hour].append(score)
    ranking = sorted(
        ({"dia": dia, "hora": hora, "promedio": round(sum(vals) / len(vals), 1), "muestra": len(vals)}
         for (dia, hora), vals in acum.items()),
        key=lambda x: (x["promedio"], x["muestra"]), reverse=True,
    )
    return {
        "muestra": len(filas),
        "suficiente": len(filas) >= 15,
        "ranking": ranking[:8],
        "por_hora": [{"hora": h, "promedio": round(sum(v) / len(v), 1), "muestra": len(v)}
                     for h, v in sorted(por_hora.items())],
        "nota": ("Basado en interacción media de tus publicaciones (likes + 3 × comentarios)."
                 if len(filas) >= 15 else
                 f"Hay {len(filas)} publicaciones con métricas; todavía no alcanza para una recomendación fiable. "
                 "Los horarios recomendados son una guía provisional."),
    }


def proximo_horario_libre(tipo: str = "post", despues_de: datetime | None = None) -> datetime:
    """Próximo horario pico libre para ese tipo ('post' o 'historia'), sin chocar con lo ya programado."""
    ahora = datetime.now(config.ZONA)
    minimo = max(ahora + timedelta(minutes=30), despues_de or ahora)
    with db.conexion() as con:
        ocupados = [datetime.fromisoformat(r["programada_para"]) for r in con.execute(
            "SELECT programada_para FROM publicaciones WHERE estado IN ('aprobada','borrador') "
            "AND tipo=? AND programada_para IS NOT NULL", (tipo,))]
    picos = horarios_pico()
    for dia in range(0, 30):
        fecha = (minimo + timedelta(days=dia)).date()
        for h in sorted(h for h, d in picos if d == fecha.weekday()):
            c = datetime(fecha.year, fecha.month, fecha.day, h, 0, tzinfo=config.ZONA)
            if c <= minimo:
                continue
            mismo_dia = sum(1 for o in ocupados if o.astimezone(config.ZONA).date() == fecha)
            tope = config.MAX_PUBLICACIONES_POR_DIA if tipo == "post" else config.MAX_HISTORIAS_POR_DIA
            if mismo_dia >= tope:
                continue
            if all(abs((c - o).total_seconds()) > 3 * 3600 for o in ocupados):
                return c
    return minimo + timedelta(days=1)


def reporte() -> str:
    perfil = ig_api.perfil()
    sincronizar()
    with db.conexion() as con:
        top = con.execute("SELECT caption, likes, comentarios, permalink FROM media "
                          "ORDER BY likes + 3*comentarios DESC LIMIT 5").fetchall()
    try:
        alcance = sum(v.get("value", 0) for v in ig_api.alcance_diario(14))
    except ig_api.ErrorInstagram:
        alcance = None
    lineas = [f"@{perfil.get('username')}: {perfil.get('followers_count')} seguidores, {perfil.get('media_count')} publicaciones"]
    if alcance is not None:
        lineas.append(f"Alcance últimos 14 días: {alcance}")
    pico = ", ".join(f"{['lun','mar','mié','jue','vie','sáb','dom'][d]} {h}h" for h, d in horarios_pico(4))
    lineas.append(f"Mejores horarios (hora local): {pico}")
    lineas.append("Top publicaciones:")
    for t in top:
        lineas.append(f"  - {t['likes']} likes / {t['comentarios']} coment. — {(t['caption'] or '')[:60]!r}")
    resumen = "\n".join(lineas)
    if config.MODO_SIN_IA:
        return resumen
    analisis = ia.preguntar(
        "Sos analista de redes sociales. Da 3 recomendaciones concretas y breves en español rioplatense.",
        f"Negocio: {config.DESCRIPCION_NEGOCIO}\n\nDatos:\n{resumen}")
    return resumen + "\n\nRecomendaciones:\n" + analisis
