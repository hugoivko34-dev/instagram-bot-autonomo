"""Cliente mínimo de la API oficial de Instagram (Graph API)."""
import time

import requests

import config


class ErrorInstagram(Exception):
    pass


_cuenta_verificada = False
REINTENTOS_GET = 3


def _url(ruta: str) -> str:
    host = "graph.instagram.com" if config.INSTAGRAM_API_MODE == "instagram_login" else "graph.facebook.com"
    return f"https://{host}/{config.GRAPH_VERSION}/{ruta.lstrip('/')}"


def _error(r, datos) -> ErrorInstagram:
    err = datos.get("error", {}) if isinstance(datos, dict) else {}
    if err.get("code") == 190:
        return ErrorInstagram("El token de Instagram venció o fue revocado. Ejecutá Conectar_Meta.bat para renovarlo.")
    return ErrorInstagram(err.get("message") or r.text[:300])


def _pedir(metodo: str, url: str, params: dict) -> dict:
    """Hace la llamada; los GET se reintentan ante cortes de red o errores 5xx de Meta."""
    intentos = REINTENTOS_GET if metodo == "GET" else 1
    for intento in range(1, intentos + 1):
        try:
            r = requests.request(metodo, url, params=params if metodo == "GET" else None,
                                 data=params if metodo != "GET" else None, timeout=60)
        except requests.RequestException:
            # requests exceptions include the full URL, which contains the access token.
            if intento < intentos:
                time.sleep(5 * intento)
                continue
            raise ErrorInstagram("No se pudo conectar con Instagram. Revisá la conexión a internet.") from None
        if r.status_code >= 500 and intento < intentos:
            time.sleep(5 * intento)
            continue
        try:
            datos = r.json() if r.content else {}
        except ValueError:
            datos = {}
        if r.status_code >= 400 or "error" in datos:
            raise _error(r, datos)
        return datos
    raise ErrorInstagram("Instagram no respondió.")


def _verificar_cuenta_objetivo() -> None:
    """No permite usar el token si no corresponde a la cuenta configurada."""
    global _cuenta_verificada
    if _cuenta_verificada:
        return
    datos = _pedir("GET", _url(config.IG_USER_ID()), {"fields": "username", "access_token": config.IG_TOKEN()})
    usuario = (datos.get("username") or "").casefold()
    if usuario != config.INSTAGRAM_USERNAME:
        raise ErrorInstagram(
            f"El token corresponde a @{datos.get('username', 'desconocida')}; "
            f"este bot solo está habilitado para @{config.INSTAGRAM_USERNAME}."
        )
    _cuenta_verificada = True


def _llamar(metodo: str, ruta: str, **params):
    _verificar_cuenta_objetivo()
    params["access_token"] = config.IG_TOKEN()
    return _pedir(metodo, _url(ruta), params)


def renovar_token() -> int:
    """Renueva el token largo (vale 60 días) y lo guarda en .env. Devuelve los días de validez nuevos."""
    if config.INSTAGRAM_API_MODE != "instagram_login":
        return 0
    datos = _pedir("GET", "https://graph.instagram.com/refresh_access_token",
                   {"grant_type": "ig_refresh_token", "access_token": config.IG_TOKEN()})
    nuevo = datos.get("access_token")
    if not nuevo:
        raise ErrorInstagram("Instagram no devolvió un token renovado.")
    import os
    from dotenv import set_key
    set_key(str(config.PROJECT_DIR / ".env"), "IG_ACCESS_TOKEN", nuevo, quote_mode="never")
    os.environ["IG_ACCESS_TOKEN"] = nuevo
    return int(datos.get("expires_in", 0)) // 86400


# ---------- Cuenta y métricas ----------
def perfil() -> dict:
    return _llamar("GET", config.IG_USER_ID(), fields="username,followers_count,media_count")


def listar_media(limite: int = 50, extra: str = "") -> list[dict]:
    campos = "id,caption,timestamp,media_type,like_count,comments_count,permalink" + (f",{extra}" if extra else "")
    datos = _llamar("GET", f"{config.IG_USER_ID()}/media", limit=limite, fields=campos)
    return datos.get("data", [])


def insights_media(media_id: str) -> dict:
    """Alcance, guardados, compartidos y vistas de una publicación."""
    datos = _llamar("GET", f"{media_id}/insights", metric="reach,saved,shares,views")
    return {d["name"]: (d.get("values") or [{}])[0].get("value") for d in datos.get("data", [])}


def alcance_diario(dias: int = 14) -> list[dict]:
    """Alcance por día de los últimos `dias` días."""
    hasta = int(time.time())
    desde = hasta - dias * 86400
    datos = _llamar("GET", f"{config.IG_USER_ID()}/insights", metric="reach",
                    period="day", since=desde, until=hasta)
    return datos.get("data", [{}])[0].get("values", [])


def audiencia_online() -> list[dict]:
    """Horas en que los seguidores están conectados; Meta puede no ofrecerlo a todas las cuentas."""
    datos = _llamar("GET", f"{config.IG_USER_ID()}/insights",
                    metric="online_followers", period="lifetime")
    return datos.get("data", [{}])[0].get("values", [])


# ---------- Publicar ----------
def publicar_imagen(image_url: str, caption: str = "", historia: bool = False) -> str:
    """Publica una foto en el feed o (historia=True) una historia. `image_url` debe ser una URL pública (JPG)."""
    params = {"image_url": image_url}
    if historia:
        params["media_type"] = "STORIES"
    else:
        params["caption"] = caption
    creacion = _llamar("POST", f"{config.IG_USER_ID()}/media", **params)
    creation_id = creacion["id"]
    for _ in range(24):  # esperar hasta 2 minutos a que Instagram procese la imagen
        estado = _llamar("GET", creation_id, fields="status_code").get("status_code")
        if estado == "FINISHED":
            break
        if estado == "ERROR":
            raise ErrorInstagram("Instagram no pudo procesar la imagen (¿URL pública y JPG?)")
        time.sleep(5)
    else:
        raise ErrorInstagram("Instagram tardó demasiado en procesar la imagen; se reintentará.")
    return _llamar("POST", f"{config.IG_USER_ID()}/media_publish", creation_id=creation_id)["id"]


# ---------- Mensajes directos ----------
def conversaciones_recientes(limite: int = 20) -> list[dict]:
    datos = _llamar("GET", f"{config.ID_MENSAJES()}/conversations", platform="instagram", limit=limite,
                    fields="id,messages.limit(3){id,message,from,created_time}")
    return datos.get("data", [])


def enviar_mensaje(usuario_id: str, texto: str) -> None:
    """Responde por DM. Instagram solo permite responder dentro de las 24 h del último mensaje del usuario."""
    import json
    _llamar("POST", f"{config.ID_MENSAJES()}/messages",
            recipient=json.dumps({"id": usuario_id}), message=json.dumps({"text": texto}))
