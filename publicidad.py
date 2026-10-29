"""Publicidad paga en Instagram con un presupuesto mensual fijo.

Cómo administra la plata:
- Cada mes crea en Meta una campaña con PRESUPUESTO TOTAL (lifetime_budget) y fecha de fin el último día del mes.
  Meta reparte ese monto a lo largo de los días y no puede gastar más: así nunca se excede y queda activa todo el mes.
- Si se activa a mitad de mes, usa solo la parte proporcional a los días que quedan.
- Al empezar un mes nuevo crea la campaña siguiente con las mismas publicaciones, así nunca se corta.
- Todo se crea en pausa y se activa recién cuando todos los pasos salieron bien; si algo falla, se borra.

Necesita (en .env): META_ADS_TOKEN (token de Facebook con ads_management), META_AD_ACCOUNT_ID y una
Página de Facebook vinculada a la cuenta de Instagram (se detecta sola o se indica en META_PAGE_ID).
"""
import calendar
import json
import os
from datetime import datetime, timedelta

import requests

import config
import db

SIN_DECIMALES = {"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"}


class ErrorPublicidad(Exception):
    pass


# ---------- Configuración ----------
def _env(nombre: str, defecto: str = "") -> str:
    return os.getenv(nombre, defecto).strip()


def configurada() -> bool:
    return bool(_env("META_ADS_TOKEN") and _env("META_AD_ACCOUNT_ID"))


def activa() -> bool:
    return _env("PROMO_ACTIVA", "no").lower() == "si"


def presupuesto_mensual() -> int:
    try:
        return int(float(_env("PROMO_PRESUPUESTO_MENSUAL", "0")))
    except ValueError:
        return 0


def objetivo() -> str:
    return "alcance" if _env("PROMO_OBJETIVO", "mensajes").lower() == "alcance" else "mensajes"


def _cuenta() -> str:
    c = _env("META_AD_ACCOUNT_ID")
    return c if c.startswith("act_") else f"act_{c}"


def zonas() -> list[dict]:
    try:
        return json.loads(db.obtener_ajuste("promo_zonas", "[]"))
    except ValueError:
        return []


def guardar_zonas(lista: list[dict]) -> None:
    db.guardar_ajuste("promo_zonas", json.dumps(lista, ensure_ascii=False))


# ---------- API de anuncios (graph.facebook.com) ----------
def _api(metodo: str, ruta: str, **params) -> dict:
    token = _env("META_ADS_TOKEN")
    if not token:
        raise ErrorPublicidad("Falta conectar la cuenta publicitaria de Meta (Panel → Publicidad).")
    datos = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
    datos["access_token"] = token
    url = f"https://graph.facebook.com/{config.GRAPH_VERSION}/{ruta.lstrip('/')}"
    try:
        r = requests.request(metodo, url, params=datos if metodo == "GET" else None,
                             data=datos if metodo != "GET" else None, timeout=60)
        cuerpo = r.json() if r.content else {}
    except requests.RequestException:
        raise ErrorPublicidad("No se pudo conectar con Meta. Revisá la conexión a internet.") from None
    except ValueError:
        raise ErrorPublicidad(f"Meta devolvió una respuesta inválida (HTTP {r.status_code}).") from None
    if r.status_code >= 400 or "error" in cuerpo:
        err = cuerpo.get("error", {})
        detalle = err.get("error_user_msg") or err.get("message") or r.text[:300]
        if err.get("code") == 190:
            detalle = "El token de publicidad venció o fue revocado. Generá uno nuevo y cargalo en Panel → Publicidad."
        raise ErrorPublicidad(detalle)
    return cuerpo


def _decimales(moneda: str) -> int:
    return 1 if moneda.upper() in SIN_DECIMALES else 100


def cuenta_info() -> dict:
    """Datos de la cuenta publicitaria y de la Página/Instagram vinculados."""
    c = _api("GET", _cuenta(), fields="name,currency,account_status,amount_spent,spend_cap,timezone_name")
    pagina = _env("META_PAGE_ID") or db.obtener_ajuste("promo_page_id")
    ig = db.obtener_ajuste("promo_ig_id")
    if not pagina or not ig:
        paginas = _api("GET", "me/accounts", fields="id,name,instagram_business_account{id,username}").get("data", [])
        elegida = next((p for p in paginas if (p.get("instagram_business_account") or {}).get("username", "").casefold()
                        == config.INSTAGRAM_USERNAME), None)
        if not elegida:
            raise ErrorPublicidad(f"No encontré una Página de Facebook vinculada a @{config.INSTAGRAM_USERNAME}. "
                                  "Vinculala desde Instagram → Configuración → Centro de cuentas, o desde la Página.")
        pagina, ig = elegida["id"], elegida["instagram_business_account"]["id"]
        db.guardar_ajuste("promo_page_id", pagina)
        db.guardar_ajuste("promo_ig_id", ig)
    estados = {1: "activa", 2: "deshabilitada", 3: "con pagos pendientes", 7: "en revisión", 9: "en período de gracia",
               101: "cerrada"}
    return {"nombre": c.get("name"), "moneda": c.get("currency", "ARS"), "estado": estados.get(c.get("account_status"),
            str(c.get("account_status"))), "estado_ok": c.get("account_status") == 1,
            "zona_horaria": c.get("timezone_name"), "page_id": pagina, "ig_id": ig}


def buscar_zonas(texto: str) -> list[dict]:
    """Busca ciudades, barrios, provincias o códigos postales de Argentina para segmentar."""
    datos = _api("GET", "search", type="adgeolocation", q=texto, country_code="AR", limit=15,
                 location_types=["city", "region", "neighborhood", "zip", "subcity"]).get("data", [])
    tipos = {"city": "Ciudad", "region": "Provincia", "neighborhood": "Barrio", "zip": "Código postal",
             "subcity": "Localidad"}
    return [{"key": d["key"], "tipo": d.get("type"), "nombre": d.get("name"),
             "detalle": ", ".join(x for x in (tipos.get(d.get("type"), d.get("type")), d.get("region")) if x)}
            for d in datos]


def _segmentacion() -> dict:
    geo, lista = {}, zonas()
    if not lista:
        raise ErrorPublicidad("Elegí al menos una zona donde mostrar la publicidad.")
    claves = {"city": "cities", "region": "regions", "neighborhood": "neighborhoods", "zip": "zips",
              "subcity": "subcities"}
    for z in lista:
        destino = claves.get(z["tipo"])
        if not destino:
            continue
        item = {"key": z["key"]}
        if z["tipo"] == "city" and z.get("radio"):
            item.update(radius=int(z["radio"]), distance_unit="kilometer")
        geo.setdefault(destino, []).append(item)
    geo["location_types"] = ["home", "recent"]
    return {"geo_locations": geo, "age_min": 18, "publisher_platforms": ["instagram"],
            "instagram_positions": ["stream", "story", "explore", "reels"]}


# ---------- Fechas y montos del mes ----------
def _mes(ahora: datetime | None = None) -> str:
    return (ahora or datetime.now(config.ZONA)).strftime("%Y-%m")


def _fin_de_mes(ahora: datetime) -> datetime:
    ultimo = calendar.monthrange(ahora.year, ahora.month)[1]
    return ahora.replace(day=ultimo, hour=23, minute=59, second=0, microsecond=0)


def _proporcion(ahora: datetime) -> float:
    """Parte del mes que queda (contando hoy)."""
    dias = calendar.monthrange(ahora.year, ahora.month)[1]
    return (dias - ahora.day + 1) / dias


def _fila_mes(mes: str | None = None):
    with db.conexion() as con:
        return con.execute("SELECT * FROM promo_meses WHERE mes=?", (mes or _mes(),)).fetchone()


def posts_promocionados() -> list[dict]:
    with db.conexion() as con:
        return [dict(r) for r in con.execute("SELECT * FROM promo_posts ORDER BY agregado DESC")]


# ---------- Armado de la campaña del mes ----------
def _crear_anuncio(info: dict, adset_id: str, media_id: str, nombre: str) -> tuple[str, str]:
    cta = ({"type": "INSTAGRAM_MESSAGE", "value": {"app_destination": "INSTAGRAM_DIRECT"}}
           if objetivo() == "mensajes" else None)
    params = {"name": f"Post {media_id}", "object_id": info["page_id"], "instagram_user_id": info["ig_id"],
              "source_instagram_media_id": media_id}
    if cta:
        params["call_to_action"] = cta
    creativo = _api("POST", f"{_cuenta()}/adcreatives", **params)["id"]
    anuncio = _api("POST", f"{_cuenta()}/ads", name=nombre[:200] or f"Post {media_id}", adset_id=adset_id,
                   creative={"creative_id": creativo}, status="PAUSED")["id"]
    return creativo, anuncio


def _borrar(*ids: str) -> None:
    for i in ids:
        if i:
            try:
                _api("DELETE", i)
            except ErrorPublicidad:
                pass


def crear_mes() -> str:
    """Crea la campaña del mes con el presupuesto proporcional y las publicaciones elegidas. Devuelve el mensaje."""
    if _fila_mes():
        return "La campaña de este mes ya existe."
    activos = [p for p in posts_promocionados() if p["activo"]]
    if not activos:
        raise ErrorPublicidad("Elegí al menos una publicación para promocionar.")
    monto = presupuesto_mensual()
    if monto <= 0:
        raise ErrorPublicidad("Definí el presupuesto mensual.")
    info = cuenta_info()
    if not info["estado_ok"]:
        raise ErrorPublicidad(f"La cuenta publicitaria está {info['estado']}. Revisala en el Administrador de anuncios.")
    ahora = datetime.now(config.ZONA)
    total = int(round(monto * _proporcion(ahora)))
    nombre_mes = ahora.strftime("%m/%Y")
    creados = []
    try:
        campana = _api("POST", f"{_cuenta()}/campaigns", name=f"Bot @{config.INSTAGRAM_USERNAME} {nombre_mes}",
                       objective="OUTCOME_ENGAGEMENT" if objetivo() == "mensajes" else "OUTCOME_AWARENESS",
                       special_ad_categories=[], buying_type="AUCTION", is_adset_budget_sharing_enabled=False,
                       status="PAUSED")["id"]
        creados.append(campana)
        adset = {"name": f"Presupuesto {nombre_mes}", "campaign_id": campana,
                 "lifetime_budget": total * _decimales(info["moneda"]),
                 "start_time": (ahora + timedelta(minutes=5)).isoformat(), "end_time": _fin_de_mes(ahora).isoformat(),
                 "billing_event": "IMPRESSIONS", "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                 "targeting": _segmentacion(), "status": "PAUSED"}
        if objetivo() == "mensajes":
            adset.update(optimization_goal="CONVERSATIONS", destination_type="INSTAGRAM_DIRECT",
                         promoted_object={"page_id": info["page_id"]})
        else:
            adset.update(optimization_goal="REACH")
        adset_id = _api("POST", f"{_cuenta()}/adsets", **adset)["id"]
        creados.append(adset_id)
        anuncios = []
        for p in activos:
            creativo, anuncio = _crear_anuncio(info, adset_id, p["media_id"], p["caption"] or p["media_id"])
            anuncios.append((p["media_id"], creativo, anuncio))
            creados.append(anuncio)
        for _, _, anuncio in anuncios:
            _api("POST", anuncio, status="ACTIVE")
        _api("POST", adset_id, status="ACTIVE")
        _api("POST", campana, status="ACTIVE")
    except ErrorPublicidad:
        _borrar(*reversed(creados))  # nada queda a medias gastando plata
        raise
    with db.conexion() as con:
        con.execute("INSERT INTO promo_meses (mes, campaign_id, adset_id, presupuesto, moneda, creado) "
                    "VALUES (?,?,?,?,?,?)", (_mes(ahora), campana, adset_id, total, info["moneda"], ahora.isoformat()))
        for media_id, creativo, anuncio in anuncios:
            con.execute("UPDATE promo_posts SET ad_id=?, creative_id=?, mes=?, error=NULL WHERE media_id=?",
                        (anuncio, creativo, _mes(ahora), media_id))
    monto_txt = f"{total:,}".replace(",", ".")
    return (f"Campaña de {nombre_mes} activa: {monto_txt} {info['moneda']} en total hasta el "
            f"{_fin_de_mes(ahora):%d/%m}, con {len(anuncios)} publicación(es).")


# ---------- Cambios desde el panel ----------
def agregar_post(media_id: str, caption: str = "", permalink: str = "") -> str:
    with db.conexion() as con:
        con.execute("INSERT INTO promo_posts (media_id, caption, permalink, activo, agregado) VALUES (?,?,?,1,?) "
                    "ON CONFLICT(media_id) DO UPDATE SET activo=1",
                    (media_id, caption[:300], permalink, datetime.now(config.ZONA).isoformat()))
    fila = _fila_mes()
    if not (configurada() and activa()):
        return "Publicación agregada. Se va a promocionar cuando actives la publicidad."
    if not fila:
        return crear_mes()
    with db.conexion() as con:
        p = con.execute("SELECT * FROM promo_posts WHERE media_id=?", (media_id,)).fetchone()
    if p["ad_id"] and p["mes"] == fila["mes"]:
        _api("POST", p["ad_id"], status="ACTIVE")
    else:
        creativo, anuncio = _crear_anuncio(cuenta_info(), fila["adset_id"], media_id, caption or media_id)
        try:
            _api("POST", anuncio, status="ACTIVE")
        except ErrorPublicidad:
            _borrar(anuncio)
            raise
        with db.conexion() as con:
            con.execute("UPDATE promo_posts SET ad_id=?, creative_id=?, mes=? WHERE media_id=?",
                        (anuncio, creativo, fila["mes"], media_id))
    return "Publicación agregada a la campaña del mes. El presupuesto se reparte entre todas las promocionadas."


def quitar_post(media_id: str) -> str:
    with db.conexion() as con:
        p = con.execute("SELECT * FROM promo_posts WHERE media_id=?", (media_id,)).fetchone()
        con.execute("UPDATE promo_posts SET activo=0 WHERE media_id=?", (media_id,))
    if p and p["ad_id"] and configurada():
        _api("POST", p["ad_id"], status="PAUSED")
    quedan = [x for x in posts_promocionados() if x["activo"]]
    fila = _fila_mes()
    if not quedan and fila and configurada():
        _api("POST", fila["campaign_id"], status="PAUSED")
        return "Ya no queda ninguna publicación promocionada: pausé la campaña del mes."
    return "Dejé de promocionar esa publicación."


def aplicar_presupuesto() -> str:
    """Al cambiar el presupuesto a mitad de mes: lo gastado + la parte proporcional del nuevo monto por los días que quedan."""
    fila = _fila_mes()
    if not fila:
        return "El presupuesto se aplica cuando se cree la campaña del mes."
    ahora = datetime.now(config.ZONA)
    gastado = gasto_del_mes()["gastado"]
    nuevo = int(round(gastado + presupuesto_mensual() * _proporcion(ahora)))
    nuevo = min(nuevo, presupuesto_mensual()) if gastado <= presupuesto_mensual() else int(gastado) + 1
    _api("POST", fila["adset_id"], lifetime_budget=nuevo * _decimales(fila["moneda"]))
    with db.conexion() as con:
        con.execute("UPDATE promo_meses SET presupuesto=? WHERE mes=?", (nuevo, fila["mes"]))
    return f"Presupuesto de este mes ajustado a {nuevo:,} {fila['moneda']} en total.".replace(",", ".")


def aplicar_zonas() -> str:
    fila = _fila_mes()
    if not fila:
        return "Zonas guardadas. Se usan cuando se cree la campaña del mes."
    _api("POST", fila["adset_id"], targeting=_segmentacion())
    return "Zonas actualizadas en la campaña del mes."


def pausar(pausada: bool) -> str:
    fila = _fila_mes()
    if fila and configurada():
        _api("POST", fila["campaign_id"], status="PAUSED" if pausada else "ACTIVE")
    return "Publicidad pausada." if pausada else "Publicidad reanudada."


def gasto_del_mes() -> dict:
    fila = _fila_mes()
    if not fila:
        return {"gastado": 0.0, "presupuesto": 0, "moneda": "", "estado": "sin campaña"}
    datos = _api("GET", f"{fila['campaign_id']}/insights", fields="spend,impressions,reach,actions",
                 date_preset="maximum").get("data", [])
    d = datos[0] if datos else {}
    estado = _api("GET", fila["campaign_id"], fields="effective_status").get("effective_status", "")
    conversaciones = sum(int(float(a.get("value", 0))) for a in d.get("actions", [])
                         if "messaging_conversation_started" in a.get("action_type", ""))
    resultado = {"gastado": float(d.get("spend", 0) or 0), "presupuesto": fila["presupuesto"], "moneda": fila["moneda"],
                 "alcance": int(d.get("reach", 0) or 0), "impresiones": int(d.get("impressions", 0) or 0),
                 "conversaciones": conversaciones, "estado": estado}
    db.guardar_ajuste("promo_gasto", json.dumps(resultado))
    with db.conexion() as con:  # historial por mes para el Excel / Power BI
        con.execute("UPDATE promo_meses SET gastado=?, alcance=?, impresiones=?, conversaciones=? WHERE mes=?",
                    (resultado["gastado"], resultado["alcance"], resultado["impresiones"],
                     resultado["conversaciones"], fila["mes"]))
    return resultado


# ---------- Mantenimiento automático (lo llama el bot) ----------
def mantener() -> str | None:
    """Crea la campaña del mes nuevo cuando corresponde y actualiza el gasto. Devuelve un aviso si hubo novedades."""
    if not (configurada() and activa()):
        return None
    aviso = None
    if not _fila_mes() and any(p["activo"] for p in posts_promocionados()) and presupuesto_mensual() > 0:
        aviso = "📣 " + crear_mes()
    gasto_del_mes()
    return aviso
