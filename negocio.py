"""Lectura de la información comercial desde negocio.xlsx."""
import math
import re
import unicodedata
from pathlib import Path

import config

_mtime = None
_datos = {}
_precios = []
_faq = []


def _vacio(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _texto(v):
    return "" if v is None else str(v).strip()


def _cargar():
    global _mtime, _datos, _precios, _faq
    ruta = Path(config.PROJECT_DIR) / "negocio.xlsx"
    try:
        mtime = ruta.stat().st_mtime_ns
        if _mtime == mtime:
            return
        from openpyxl import load_workbook
        libro = load_workbook(ruta, data_only=True, read_only=True)
        datos, precios, faq = {}, [], []
        if "Negocio" in libro.sheetnames:
            for fila in libro["Negocio"].iter_rows(min_row=2, values_only=True):
                if len(fila) >= 2 and not _vacio(fila[0]) and not _vacio(fila[1]):
                    datos[_texto(fila[0])] = fila[1]
        if "Precios" in libro.sheetnames:
            for fila in libro["Precios"].iter_rows(min_row=2, values_only=True):
                fila = tuple(fila) + (None,) * max(0, 6-len(fila))
                if _vacio(fila[0]):
                    continue
                try:
                    precio = float(fila[3])
                except (TypeError, ValueError):
                    precio = None
                precios.append({"servicio": _texto(fila[0]), "terminacion": _texto(fila[1]),
                                "unidad": _texto(fila[2]), "precio": precio,
                                "cotiza": _texto(fila[4]).lower().startswith("s"), "notas": _texto(fila[5])})
        if "Preguntas frecuentes" in libro.sheetnames:
            anterior = None
            for fila in libro["Preguntas frecuentes"].iter_rows(min_row=2, values_only=True):
                q = _texto(fila[0]) if fila else ""
                a = _texto(fila[1]) if len(fila) > 1 else ""
                if q:
                    faq.append([q, a])
                    anterior = len(faq) - 1
                elif a and anterior is not None and not faq[anterior][1]:
                    faq[anterior][1] = a
        libro.close()
        _datos, _precios, _faq, _mtime = datos, precios, [(q, a) for q, a in faq if q and a], mtime
    except Exception:
        return


def _normalizar(valor):
    return "".join(c for c in unicodedata.normalize("NFD", str(valor).lower())
                   if unicodedata.category(c) != "Mn").strip()


def whatsapp():
    _cargar()
    valor = _datos.get("WhatsApp para derivar") or config.WHATSAPP_NUMERO
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) == 10:
        digitos = "549" + digitos
    return digitos if digitos.startswith("54") or len(digitos) != 10 else ""


def numero(texto):
    m = re.search(r"\d+(?:[.,]\d+)?", str(texto or ""))
    return float(m.group().replace(",", ".")) if m else None


CLAVES_SERVICIO = (("hidro", "Pulido e hidrolaqueado"), ("plastific", "Pulido y plastificado"),
                   ("deck", "Decks"), ("zocalo", "Colocación de zócalos"), ("escaler", "Escaleras"),
                   ("coloca", "Colocación de piso"), ("repar", "Reparaciones"), ("pulid", "pulido"),
                   ("pulir", "pulido"), ("pulan", "pulido"))


def detectar(texto):
    """Servicio, terminación y m² mencionados por el cliente (no depende de la IA)."""
    t = _normalizar(texto)
    servicio = next((s for clave, s in CLAVES_SERVICIO if clave in t), "")
    terminacion = next((x.capitalize() for x in ("brillante", "satinado", "mate") if re.search(rf"\b{x}\b", t)), "")
    m = re.findall(r"(\d+(?:[.,]\d+)?)\s*(?:m2|m²|mts?2?|metros)", t)
    return servicio, terminacion, (float(m[-1].replace(",", ".")) if m else None)


def resolver_servicio(nombre):
    """Lleva lo que devolvió la IA ("pulido e hidrolaqueado (Brillante...)") a un servicio de la planilla."""
    n = _normalizar(re.sub(r"\(.*?\)", "", str(nombre or "")))
    if not n:
        return ""
    if n == "pulido":
        return "pulido"
    for s in servicios():
        if _normalizar(s) == n or _normalizar(s).startswith(n) or n.startswith(_normalizar(s)):
            return s
    return detectar(n)[0]


def servicios():
    _cargar()
    return list(dict.fromkeys(f["servicio"] for f in _precios))


def contexto():
    _cargar()
    lineas = ["Datos del negocio:"]
    lineas.extend(f"- {k}: {_texto(v)}" for k, v in _datos.items())
    lineas.append("Servicios:")
    for servicio in servicios():
        filas = [f for f in _precios if f["servicio"] == servicio]
        variantes = ", ".join(dict.fromkeys(f["terminacion"] for f in filas if f["terminacion"]))
        lineas.append(f"- {servicio}" + (f" ({variantes})" if variantes else "") +
                      ("; cotiza el bot" if filas and all(f["cotiza"] and f["precio"] is not None for f in filas)
                       else "; deriva a una persona"))
    lineas.append("Preguntas frecuentes:")
    lineas.extend(f"- {q} {a}" for q, a in _faq)
    return "\n".join(lineas)


def cotizar(servicio, terminacion, m2):
    _cargar()
    solicitado = _normalizar(servicio)
    if solicitado == "pulido":  # sin tipo de terminación: las dos opciones juntas y la aclaración una sola vez
        nombres = list(dict.fromkeys(f["servicio"] for f in _precios
                                     if _normalizar(f["servicio"]).startswith("pulido") and f["cotiza"]))
        cotizaciones = [cotizar(n, terminacion, m2) for n in nombres]
        if not cotizaciones or not all(cotizaciones):
            return None
        partes = [c.split("\n", 1) for c in cotizaciones]
        return "\n".join("• " + p[0] for p in partes) + ("\n" + partes[0][1] if len(partes[0]) > 1 else "")
    filas = [f for f in _precios if _normalizar(f["servicio"]) == solicitado]
    if not filas or any(not f["cotiza"] or f["precio"] is None for f in filas):
        return None
    variante = _normalizar(terminacion)
    elegida = next((f for f in filas if variante and _normalizar(f["terminacion"]) == variante), None)
    if elegida:
        minp = maxp = elegida["precio"]
    else:
        precios = [f["precio"] for f in filas]
        minp, maxp = min(precios), max(precios)
    m2 = numero(m2)
    if m2 is None or m2 <= 0:
        return None
    minimo = numero(_datos.get("Trabajo mínimo (m²)"))
    cobrados = max(m2, minimo or 0)
    def pesos(n):
        return f"${n:,.0f}".replace(",", ".")
    nombre = filas[0]["servicio"]
    rotulo = f"{nombre} ({elegida['terminacion']})" if elegida and elegida["terminacion"] else nombre
    if minp == maxp:
        precio_txt = pesos(minp)
    else:
        precio_txt = f"entre {pesos(minp)} y {pesos(maxp)}"
    texto = f"Para {m2:g} m² de {rotulo} el aproximado es {pesos(cobrados * minp) if minp == maxp else 'entre ' + pesos(cobrados * minp) + ' y ' + pesos(cobrados * maxp)} ({precio_txt} por m²)."
    if minimo and m2 < minimo:
        texto += f" Se calcula el mínimo de {minimo:g} m² de trabajo."
    rendimiento = numero(_datos.get("Rendimiento por día (m²)"))
    if rendimiento:
        dias = math.ceil(cobrados / rendimiento)
        texto += f" Lleva alrededor de {dias} día{'s' if dias != 1 else ''} de trabajo."
    incluye = _texto(_datos.get("Qué incluye el precio"))
    if incluye:
        texto += f"\nIncluye: {incluye}"
    texto += "\nEs un valor aproximado: el precio final se confirma con una visita."
    validez = _texto(_datos.get("Validez del presupuesto"))
    if validez:
        texto += f" Validez: {validez}."
    return texto
