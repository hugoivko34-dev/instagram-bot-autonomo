"""Convierte tus fotos de trabajos terminados en borradores de posts e historias.

Cómo se usa:
  1. Dejá las fotos en la carpeta  fotos/
  2. El pie de foto del post será exactamente el nombre del archivo (sin extensión).
  3. (Opcional) junto a cada foto, un .txt con el mismo nombre y los datos reales del trabajo:
        living-caballito.jpg
        living-caballito.txt  ->  "Living en Caballito, 38 m2, pulido y plastificado mate"
     La nota .txt sirve como contexto para la IA; no reemplaza el pie de foto.
  4. Para un antes/después: nombre_antes.jpg y nombre_despues.jpg
  5. python main.py fotos
"""
import base64
import io
import shutil
from datetime import timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:  # fotos HEIC del iPhone
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

import config
import db
import ia
import metricas

CARPETA = config.PROJECT_DIR / "fotos"
USADAS = CARPETA / "usadas"
SALIDA = config.PROJECT_DIR / "salida"
EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

SISTEMA = """Sos community manager de una empresa argentina de colocación, pulido y plastificado de pisos de madera.
Escribís en español rioplatense, cercano y profesional. Mirá la foto del trabajo terminado y escribí contenido
para Instagram basado en lo que REALMENTE se ve y en las notas del dueño.
Reglas: no inventes metros cuadrados, ubicaciones, precios, plazos, marcas ni testimonios que no estén en las notas;
si no sabés un dato, no lo menciones. Podés describir lo visible (tono de la madera, brillo, diseño del tablón,
ambiente). Cerrá con una llamada a la acción (escribinos por DM / WhatsApp para presupuesto)."""

PEDIDO = """Notas del dueño sobre este trabajo: {notas}
Es una foto de: {tipo}.
Devolvé JSON: {{"tema": "...", "caption": "texto completo del post con 5-8 hashtags", \
"texto_historia": "frase corta de máx. 7 palabras para poner sobre la foto de la historia", \
"cta_historia": "máx. 5 palabras, ej: Pedí tu presupuesto por DM"}}"""


# ---------- Manejo de imágenes ----------
def _cargar(ruta: Path) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(ruta)).convert("RGB")


def _recortar(img: Image.Image, ancho: int, alto: int) -> Image.Image:
    return ImageOps.fit(img, (ancho, alto), method=Image.LANCZOS, centering=(0.5, 0.5))


def _fuente(tam: int) -> ImageFont.FreeTypeFont:
    for nombre in (config.FUENTE_PATH, "DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if nombre:
            try:
                return ImageFont.truetype(nombre, tam)
            except OSError:
                continue
    return ImageFont.load_default(tam)


def _partir_texto(draw, texto, fuente, ancho_max):
    lineas, actual = [], ""
    for palabra in texto.split():
        prueba = f"{actual} {palabra}".strip()
        if draw.textlength(prueba, font=fuente) <= ancho_max:
            actual = prueba
        else:
            lineas.append(actual)
            actual = palabra
    return lineas + [actual] if actual else lineas


def hacer_post(img: Image.Image, destino: Path) -> None:
    _recortar(img, 1080, 1350).save(destino, "JPEG", quality=92)


def hacer_antes_despues(antes: Image.Image, despues: Image.Image, destino: Path) -> None:
    lienzo = Image.new("RGB", (1080, 1350), "white")
    lienzo.paste(_recortar(antes, 1080, 675), (0, 0))
    lienzo.paste(_recortar(despues, 1080, 675), (0, 675))
    d = ImageDraw.Draw(lienzo)
    f = _fuente(44)
    for texto, y in (("ANTES", 20), ("DESPUÉS", 695)):
        w = d.textlength(texto, font=f)
        d.rounded_rectangle((20, y, 20 + w + 40, y + 70), 12, fill=(0, 0, 0, 200))
        d.text((40, y + 8), texto, font=f, fill="white")
    lienzo.save(destino, "JPEG", quality=92)


def hacer_historia(img: Image.Image, texto: str, cta: str, destino: Path) -> None:
    base = _recortar(img, 1080, 1920).convert("RGBA")
    capa = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    for i in range(700):  # degradé oscuro en la parte baja para que se lea el texto
        d.line([(0, 1920 - i), (1080, 1920 - i)], fill=(0, 0, 0, int(190 * (1 - i / 700))))
    base = Image.alpha_composite(base, capa)
    d = ImageDraw.Draw(base)
    f_titulo, f_cta, f_marca = _fuente(84), _fuente(44), _fuente(36)
    lineas = _partir_texto(d, texto.upper(), f_titulo, 940)
    y = 1920 - 330 - 100 * len(lineas)
    for l in lineas:
        d.text((70, y), l, font=f_titulo, fill="white", stroke_width=2, stroke_fill="black")
        y += 100
    d.rounded_rectangle((70, y + 20, 70 + d.textlength(cta, font=f_cta) + 60, y + 100), 40, fill=config.COLOR_MARCA)
    d.text((100, y + 34), cta, font=f_cta, fill="white")
    if config.NOMBRE_MARCA:
        d.text((70, 1920 - 140), config.NOMBRE_MARCA, font=f_marca, fill=(255, 255, 255, 220))
    base.convert("RGB").save(destino, "JPEG", quality=92)


# ---------- Claude mira la foto ----------
def _imagen_para_claude(ruta: Path) -> dict:
    img = _cargar(ruta)
    img.thumbnail((1568, 1568))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(buf.getvalue()).decode()}}


def _plantilla(notas: str) -> dict:
    """Modo sin IA: el texto del post es tu nota (si la escribiste) + cierre fijo + hashtags."""
    cuerpo = notas or "Trabajo terminado."
    return {"tema": (notas.splitlines()[0][:50] if notas else "Trabajo terminado"),
            "caption": f"{cuerpo}\n\n{config.CIERRE_POST}\n\n{config.HASHTAGS}",
            "texto_historia": config.TEXTO_HISTORIA, "cta_historia": config.CTA_HISTORIA}


def _analizar(rutas: list[Path], notas: str, tipo: str) -> dict:
    if config.MODO_SIN_IA:
        return _plantilla(notas)
    contenido = [_imagen_para_claude(r) for r in rutas]
    contenido.append({"type": "text", "text": PEDIDO.format(notas=notas or "(sin notas)", tipo=tipo)})
    r = ia._cli().messages.create(model=config.MODELO, max_tokens=1500,
                                  system=SISTEMA + "\nRespondé SOLO con JSON válido.",
                                  messages=[{"role": "user", "content": contenido}])
    texto = "".join(b.text for b in r.content if b.type == "text")
    import json, re
    return json.loads(re.search(r"\{.*\}", texto, re.S).group(0))


# ---------- Flujo principal ----------
def _nota_escrita(ruta: Path) -> str:
    txt = ruta.with_suffix(".txt")
    return txt.read_text(encoding="utf-8").strip() if txt.exists() else ""


def _agrupar() -> list[dict]:
    """Devuelve trabajos: simples o pares antes/después."""
    archivos = sorted(p for p in CARPETA.iterdir() if p.is_file() and p.suffix.lower() in EXT)  # no entra a subcarpetas
    por_nombre = {p.stem.lower(): p for p in archivos}
    trabajos, usados = [], set()
    for p in archivos:
        s = p.stem.lower()
        if s.endswith("_antes") and s[:-6] + "_despues" in por_nombre:
            d = por_nombre[s[:-6] + "_despues"]
            trabajos.append({"nombre": p.stem[:-6], "antes": p, "despues": d, "archivos": [p, d]})
            usados |= {p, d}
    for p in archivos:
        if p not in usados:
            trabajos.append({"nombre": p.stem, "foto": p, "archivos": [p]})
    return trabajos


def procesar_nuevas() -> list[int]:
    """Crea un borrador de post y otro de historia por cada trabajo nuevo. Devuelve los IDs creados."""
    CARPETA.mkdir(exist_ok=True)
    USADAS.mkdir(exist_ok=True)
    SALIDA.mkdir(exist_ok=True)
    ids = []
    for t in _agrupar():
        nombre = t["nombre"]
        if "foto" in t:
            principal = t["foto"]
            notas = _nota_escrita(principal) or nombre
            tipo = "trabajo terminado"
            img = _cargar(principal)
            ruta_post = SALIDA / f"{nombre}_post.jpg"
            hacer_post(img, ruta_post)
            vistas = [principal]
        else:
            notas = _nota_escrita(t["despues"]) or _nota_escrita(t["antes"]) or nombre
            tipo = "antes y después (primera imagen: antes, segunda: después)"
            img = _cargar(t["despues"])
            ruta_post = SALIDA / f"{nombre}_antes_despues.jpg"
            hacer_antes_despues(_cargar(t["antes"]), img, ruta_post)
            vistas = [t["antes"], t["despues"]]

        datos = _analizar(vistas, notas, tipo)
        # El usuario eligió que el pie de foto refleje literalmente el nombre del archivo.
        datos["tema"] = nombre
        datos["caption"] = nombre
        ruta_historia = SALIDA / f"{nombre}_historia.jpg"
        hacer_historia(img, datos["texto_historia"], datos["cta_historia"], ruta_historia)

        hora_post = metricas.proximo_horario_libre("post")
        hora_hist = metricas.proximo_horario_libre("historia", despues_de=hora_post + timedelta(hours=1))
        fuente_historia = USADAS / (t["despues"].name if "despues" in t else t["foto"].name)
        with db.conexion() as con:
            for tipo_pub, ruta, hora, caption, texto_historia, cta_historia, origen in (
                    ("post", ruta_post, hora_post, datos["caption"], "", "", None),
                    ("historia", ruta_historia, hora_hist, "", datos["texto_historia"],
                     datos["cta_historia"], str(fuente_historia))):
                cur = con.execute(
                    "INSERT INTO publicaciones (estado,tipo,tema,caption,idea_imagen,foto_local,programada_para,"
                    "story_text,story_cta,foto_origen) VALUES ('borrador',?,?,?,?,?,?,?,?,?)",
                    (tipo_pub, datos["tema"], caption, nombre, str(ruta), hora.isoformat(),
                     texto_historia, cta_historia, origen))
                ids.append(cur.lastrowid)
        for a in t["archivos"]:  # mover originales (y sus .txt) para no procesarlos dos veces
            shutil.move(str(a), USADAS / a.name)
            if a.with_suffix(".txt").exists():
                shutil.move(str(a.with_suffix(".txt")), USADAS / a.with_suffix(".txt").name)
    return ids


def regenerar_historia(id_pub: int, texto: str, cta: str) -> None:
    """Actualiza el texto visual de una historia borrador y vuelve a generar su imagen."""
    with db.conexion() as con:
        fila = con.execute("SELECT * FROM publicaciones WHERE id=?", (id_pub,)).fetchone()
        if not fila or fila["tipo"] != "historia":
            raise ValueError("No encontré esa historia.")
        if fila["estado"] not in ("borrador", "aprobada"):
            raise ValueError("Solo podés cambiar el diseño de una historia que todavía no se publicó.")
        salida = Path(fila["foto_local"])
        origen = Path(fila["foto_origen"]) if fila["foto_origen"] else None
        if not origen or not origen.exists():
            nombre = fila["tema"]
            opciones = list(USADAS.glob(f"{nombre}_despues.*")) or list(USADAS.glob(f"{nombre}.*"))
            origen = opciones[0] if opciones else None
        if not origen or not origen.exists():
            raise ValueError("No encontré la foto original en fotos/usadas para reconstruir la historia.")
        hacer_historia(_cargar(origen), texto.strip(), cta.strip(), salida)
        con.execute("UPDATE publicaciones SET story_text=?, story_cta=?, foto_origen=? WHERE id=?",
                    (texto.strip(), cta.strip(), str(origen), id_pub))
