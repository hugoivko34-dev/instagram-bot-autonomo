"""Piloto automático: banco de fotos + una historia por día + un post por semana.

- Las fotos entran al banco desde Google Drive (DRIVE_CARPETA_ID) o desde la carpeta local fotos/automatico/.
- Cada historia y cada post se eligen con anticipación y se avisan por Telegram con la foto.
  Si no tocás nada, se publican solos; desde el aviso podés pedir otra foto o saltear esa publicación.
- Los posts usan fotos que nunca salieron en el feed. Las historias pueden repetir fotos, rotando
  siempre la que hace más tiempo que no se usa.
"""
import re
import shutil
import socket
import threading
import time as reloj
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path

from PIL import Image

import config
import db
import drive
import fotos
import metricas
import telegram_bridge

BANCO = config.PROJECT_DIR / "fotos" / "banco"
ENTRADA_LOCAL = config.PROJECT_DIR / "fotos" / "automatico"
ESTADOS_OCUPAN = ("aprobada", "publicando", "publicada", "salteada")
# (día de la semana, hora) para el post mientras no haya métricas suficientes. 0 = lunes.
DEFECTO_POST = [(3, 19), (2, 19), (1, 19), (4, 19), (0, 19), (5, 12), (6, 12)]
NOMBRE_GENERICO = re.compile(
    r"(?i)^(img|dsc|dscn|pxl|mvimg|whatsapp|photo|image|imagen|screenshot|captura|signal|fb_img|foto)[\s_\-]")

_lock = threading.Lock()
PUERTO_CANDADO = 8767


@contextmanager
def _candado():
    """Evita que el bot y el panel planifiquen o importen a la vez (son procesos distintos)."""
    if not _lock.acquire(timeout=120):
        raise RuntimeError("Otra planificación sigue en curso; probá de nuevo en un momento.")
    s = socket.socket()
    try:
        for _ in range(240):
            try:
                s.bind(("127.0.0.1", PUERTO_CANDADO))
                break
            except OSError:
                reloj.sleep(0.5)
        else:
            raise RuntimeError("Otra planificación sigue en curso; probá de nuevo en un momento.")
        yield
    finally:
        s.close()
        _lock.release()


# ---------- Banco de fotos ----------
def _destino_libre(nombre: str) -> Path:
    BANCO.mkdir(parents=True, exist_ok=True)
    base = Path(nombre)
    destino, n = BANCO / f"{base.stem}.jpg", 2
    while destino.exists():
        destino, n = BANCO / f"{base.stem}_{n}.jpg", n + 1
    return destino


def _guardar_jpg(origen: Path, destino: Path) -> None:
    """Normaliza a JPG (también convierte HEIC del iPhone) y valida que sea una imagen."""
    fotos._cargar(origen).save(destino, "JPEG", quality=95)


ETIQUETA_USO = re.compile(r"\(\s*(historias?|stor(?:y|ies)|publicaci[oó]n(?:es)?|post|feed)\s*\)", re.I)
USOS = {"historia": "historia", "post": "post"}


def separar_uso(texto: str) -> tuple[str, str]:
    """'Living (historia)' -> ('Living', 'historia'). Sin etiqueta, la foto sirve para las dos cosas."""
    uso = ""
    m = ETIQUETA_USO.search(texto)
    if m:
        palabra = m.group(1).lower()
        uso = "historia" if palabra.startswith(("hist", "stor")) else "post"
        texto = ETIQUETA_USO.sub(" ", texto)
    return re.sub(r"[ \t]{2,}", " ", texto).strip(), uso


def _agregar(nombre: str, ruta: Path, origen: str, drive_id: str | None = None) -> int:
    nombre, uso = separar_uso(nombre)
    with db.conexion() as con:
        return con.execute(
            "INSERT INTO banco_fotos (nombre, ruta, origen, drive_id, agregada, uso) VALUES (?,?,?,?,?,?)",
            (nombre or "Trabajo terminado", str(ruta), origen, drive_id, datetime.now(config.ZONA).isoformat(),
             uso)).lastrowid


def importar() -> int:
    """Trae fotos nuevas de Drive y de fotos/automatico/ al banco. Devuelve cuántas entraron."""
    with _candado():
        return _importar()


def _importar() -> int:
    nuevas = 0
    ENTRADA_LOCAL.mkdir(parents=True, exist_ok=True)
    for p in sorted(ENTRADA_LOCAL.iterdir()):
        if not p.is_file() or p.suffix.lower() not in fotos.EXT:
            continue
        destino = _destino_libre(p.name)
        try:
            _guardar_jpg(p, destino)
        except Exception as e:
            telegram_bridge.avisar(f"⚠️ No pude leer la foto {p.name}: {e}")
            shutil.move(str(p), str(p.with_suffix(p.suffix + ".error")))
            continue
        _agregar(p.stem, destino, "local")
        p.unlink()
        nuevas += 1

    if drive.configurado():
        with db.conexion() as con:
            conocidas = {r[0] for r in con.execute("SELECT drive_id FROM banco_fotos WHERE drive_id IS NOT NULL")}
        temporal = BANCO / "_descarga"
        for a in drive.fotos_nuevas():
            if a["id"] not in conocidas:
                destino = _destino_libre(a["name"])
                drive.descargar(a, temporal)
                try:
                    _guardar_jpg(temporal, destino)
                except Exception:
                    telegram_bridge.avisar(f"⚠️ La foto de Drive {a['name']} no se pudo abrir; "
                                           "la dejé en la subcarpeta usadas sin publicarla.")
                    drive.marcar_usada(a)
                    continue
                finally:
                    temporal.unlink(missing_ok=True)
                _agregar(Path(a["name"]).stem, destino, "drive", a["id"])
                nuevas += 1
            drive.marcar_usada(a)  # también reintenta mover las que ya estaban en el banco
    return nuevas


def agregar_archivo(origen: Path, nombre: str, fuente: str = "panel") -> int:
    """Suma al banco una foto subida desde el panel o Telegram. Devuelve su ID."""
    archivo = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", nombre)[:80].strip(" .") or "foto"
    destino = _destino_libre(f"{archivo}.jpg")
    _guardar_jpg(origen, destino)
    return _agregar(nombre, destino, fuente)


def importar_carpeta(carpeta: Path) -> int:
    """Carga al banco fotos ya existentes (por ejemplo fotos/usadas) sin moverlas ni borrarlas."""
    with db.conexion() as con:
        nombres = {r[0] for r in con.execute("SELECT nombre FROM banco_fotos")}
    n = 0
    for p in sorted(carpeta.iterdir()):
        if p.is_file() and p.suffix.lower() in fotos.EXT and separar_uso(p.stem)[0] not in nombres:
            destino = _destino_libre(p.name)
            _guardar_jpg(p, destino)
            _agregar(p.stem, destino, "local")
            n += 1
    return n


def resumen_banco() -> dict:
    with db.conexion() as con:
        total = con.execute("SELECT COUNT(*) FROM banco_fotos WHERE activa=1").fetchone()[0]
        sin_post = con.execute("SELECT COUNT(*) FROM banco_fotos WHERE activa=1 AND usos_post=0 "
                               "AND uso!='historia'").fetchone()[0]
        historias = con.execute("SELECT COUNT(*) FROM banco_fotos WHERE activa=1 AND uso!='post'").fetchone()[0]
    return {"total": total, "sin_post": sin_post, "historias": historias}


# ---------- Elección de fotos y horarios ----------
def _activas_ids() -> set[int]:
    with db.conexion() as con:
        return {r[0] for r in con.execute(
            "SELECT banco_id FROM publicaciones WHERE estado IN ('aprobada','publicando') AND banco_id IS NOT NULL")}


def _elegir(tipo: str):
    ocupadas = _activas_ids()
    with db.conexion() as con:
        # Las fotos marcadas para ese tipo van primero; las marcadas para el otro tipo no se usan.
        if tipo == "post":
            filas = con.execute("SELECT * FROM banco_fotos WHERE activa=1 AND usos_post=0 AND uso!='historia' "
                                "ORDER BY uso='post' DESC, agregada, id").fetchall()
        else:
            filas = con.execute("SELECT * FROM banco_fotos WHERE activa=1 AND uso!='post' "
                                "ORDER BY usos_historia, uso='historia' DESC, COALESCE(ultimo_uso, ''), "
                                "agregada, id").fetchall()
        if tipo == "historia":
            # Que nunca falte la historia del día: si no queda ninguna, se usa una marcada para publicación.
            filas = list(filas) + con.execute(
                "SELECT * FROM banco_fotos WHERE activa=1 AND uso='post' "
                "ORDER BY usos_historia, COALESCE(ultimo_uso, ''), agregada, id").fetchall()
    for f in filas:
        if f["id"] not in ocupadas and Path(f["ruta"]).exists():
            return f
    return None


def _fecha_local(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone(config.ZONA).date()


def _hay(tipo: str, desde: date, hasta: date) -> bool:
    with db.conexion() as con:
        marcas = ",".join("?" * len(ESTADOS_OCUPAN))
        filas = [r[0] for r in con.execute(
            f"SELECT programada_para FROM publicaciones WHERE tipo=? AND estado IN ({marcas}) "
            "AND programada_para IS NOT NULL", (tipo, *ESTADOS_OCUPAN))]
        if tipo == "post":  # cuenta también lo que se publicó directo desde la app de Instagram
            filas += [r[0] for r in con.execute(
                "SELECT publicada_en FROM media WHERE publicada_en IS NOT NULL AND tipo != 'VIDEO'")]
    for iso in filas:
        try:
            d = _fecha_local(iso.replace("+0000", "+00:00"))
        except ValueError:
            continue
        if desde <= d <= hasta:
            return True
    return False


def _momento(fecha: date, hora: int) -> datetime:
    return datetime.combine(fecha, time(hora), tzinfo=config.ZONA)


def _hora_historia(fecha: date) -> int:
    picos = metricas.horarios_pico()
    if picos is not metricas.HORARIOS_DEFECTO:
        horas = sorted(h for h, d in picos if d == fecha.weekday())
        if horas:
            return horas[0]
    return config.HORA_HISTORIA


def _ranking_post() -> list[tuple[int, int]]:
    r = metricas.resumen_horarios()
    propios = [(x["dia"], x["hora"]) for x in r["ranking"]] if r["suficiente"] else []
    return propios + [x for x in DEFECTO_POST if x not in propios]


def _rotar(texto: str, fecha: date) -> str:
    """TEXTO_HISTORIA y CTA_HISTORIA aceptan varias opciones separadas por | y se van turnando."""
    opciones = [o.strip() for o in texto.split("|") if o.strip()] or [texto]
    return opciones[fecha.toordinal() % len(opciones)]


def _caption(nombre: str, ruta: Path) -> str:
    if not NOMBRE_GENERICO.match(nombre) and not re.fullmatch(r"[\d\W_]+", nombre):
        return nombre  # como hasta ahora: el pie de foto es el nombre del archivo
    if not config.MODO_SIN_IA:
        try:
            return fotos._analizar([ruta], "", "trabajo terminado")["caption"]
        except Exception:
            pass
    return fotos._plantilla("")["caption"]


# ---------- Planificación ----------
def _crear(tipo: str, foto, hora: datetime) -> int:
    ruta = Path(foto["ruta"])
    img = fotos._cargar(ruta)
    fotos.SALIDA.mkdir(exist_ok=True)
    salida = fotos.SALIDA / f"auto_{tipo}_{hora:%Y-%m-%d}_{foto['id']}.jpg"
    caption = texto = cta = ""
    if tipo == "post":
        fotos.hacer_post(img, salida)
        caption = _caption(foto["nombre"], ruta)
    else:
        texto, cta = _rotar(config.TEXTO_HISTORIA, hora.date()), _rotar(config.CTA_HISTORIA, hora.date())
        fotos.hacer_historia(img, texto, cta, salida)
    with db.conexion() as con:
        id_pub = con.execute(
            "INSERT INTO publicaciones (estado,tipo,tema,caption,idea_imagen,foto_local,programada_para,"
            "story_text,story_cta,foto_origen,auto,banco_id) VALUES ('aprobada',?,?,?,?,?,?,?,?,?,1,?)",
            (tipo, foto["nombre"], caption, foto["nombre"], str(salida), hora.isoformat(),
             texto, cta, str(ruta), foto["id"])).lastrowid
    cuando = hora.strftime("%d/%m a las %H:%M")
    detalle = f"\nTexto: {caption}" if tipo == "post" else ""
    telegram_bridge.enviar_programada(
        id_pub, salida,
        f"📅 {'Post' if tipo == 'post' else 'Historia'} #{id_pub} programado para el {cuando}.{detalle}\n"
        "Si no hacés nada, se publica solo.")
    return id_pub


def _avisar_una_vez(clave: str, texto: str, cada_dias: int = 1) -> None:
    hoy = datetime.now(config.ZONA).date()
    ultimo = db.obtener_ajuste(f"aviso_{clave}")
    if ultimo and (hoy - date.fromisoformat(ultimo)).days < cada_dias:
        return
    db.guardar_ajuste(f"aviso_{clave}", hoy.isoformat())
    telegram_bridge.avisar(texto)


def planificar() -> list[int]:
    """Deja programadas la historia de hoy y mañana y el post de la semana. Devuelve los IDs creados."""
    with _candado():
        return _planificar()


def _planificar() -> list[int]:
    ahora = datetime.now(config.ZONA)
    margen = ahora + timedelta(minutes=30)
    creadas = []

    for dias in (0, 1):  # historia diaria
        fecha = ahora.date() + timedelta(days=dias)
        if _hay("historia", fecha, fecha):
            continue
        hora = _momento(fecha, _hora_historia(fecha))
        if hora < margen:
            if dias:
                continue
            hora = margen.replace(second=0, microsecond=0)  # hoy ya pasó el horario: sale en un rato
        foto = _elegir("historia")
        if not foto:
            _avisar_una_vez("sin_fotos", "📭 No tengo fotos en el banco para la historia. Subí fotos a la carpeta de Drive.")
            break
        creadas.append(_crear("historia", foto, hora))

    lunes = ahora.date() - timedelta(days=ahora.weekday())
    for semana in (0, 1):  # un post por semana (lunes a domingo), en el mejor horario que quede libre
        inicio = lunes + timedelta(weeks=semana)
        if _hay("post", inicio, inicio + timedelta(days=6)):
            continue
        candidatos = [_momento(inicio + timedelta(days=d), h) for d, h in _ranking_post()]
        futuros = [m for m in candidatos if m >= ahora + timedelta(hours=1)]  # en orden de rendimiento
        if not futuros:
            continue
        hora = futuros[0]
        if hora - ahora > timedelta(days=config.DIAS_AVISO_POST):
            break  # todavía falta: se elige la foto más cerca de la fecha
        foto = _elegir("post")
        if not foto:
            _avisar_una_vez("sin_fotos_post", "📭 No quedan fotos sin usar para el post semanal. "
                                              "Subí fotos nuevas a la carpeta de Drive.")
            break
        creadas.append(_crear("post", foto, hora))
        break

    banco = resumen_banco()
    if banco["total"] and banco["sin_post"] < 2:
        _avisar_una_vez("pocas_fotos", f"📷 Quedan {banco['sin_post']} fotos sin usar para posts. "
                                       "Subí más a la carpeta de Drive cuando puedas.", cada_dias=7)
    return creadas


# ---------- Acciones desde Telegram ----------
def accion(nombre: str, id_pub: int) -> str:
    """'otra' cambia la foto; 'saltar' no publica nada en ese lugar."""
    with db.conexion() as con:
        f = con.execute("SELECT * FROM publicaciones WHERE id=?", (id_pub,)).fetchone()
        if not f or f["estado"] != "aprobada":
            return "Esa publicación ya no se puede cambiar (ya salió o fue cancelada)."
        if nombre == "saltar":
            con.execute("UPDATE publicaciones SET estado='salteada' WHERE id=?", (id_pub,))
            return f"⏭ Listo, la {f['tipo']} #{id_pub} no se va a publicar."
        con.execute("UPDATE publicaciones SET estado='rechazada' WHERE id=?", (id_pub,))
        if f["banco_id"]:  # la foto descartada pasa al final de la fila
            ahora = datetime.now(config.ZONA).isoformat()
            con.execute("UPDATE banco_fotos SET agregada=?, ultimo_uso=? WHERE id=?", (ahora, ahora, f["banco_id"]))
    nuevas = planificar()
    return "🔄 Busqué otra foto." if nuevas else "Cancelada. No encontré otra foto disponible por ahora."


def estado() -> str:
    lineas = []
    with db.conexion() as con:
        for f in con.execute("SELECT id, tipo, tema, programada_para FROM publicaciones "
                             "WHERE estado='aprobada' ORDER BY programada_para"):
            hora = datetime.fromisoformat(f["programada_para"]).astimezone(config.ZONA)
            lineas.append(f"• {f['tipo']} #{f['id']} — {hora:%a %d/%m %H:%M} — {f['tema']}")
    b = resumen_banco()
    return ("Próximas publicaciones:\n" + ("\n".join(lineas) or "(ninguna)") +
            f"\n\nBanco: {b['total']} fotos, {b['sin_post']} sin usar en el feed.")
