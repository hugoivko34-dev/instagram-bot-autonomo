"""Lee las fotos nuevas de una carpeta de Google Drive.

La carpeta se comparte (como Editor) con la cuenta de servicio del archivo credenciales_google.json.
Cada foto descargada se mueve a la subcarpeta "usadas" de Drive para no traerla dos veces.
"""
from pathlib import Path

import config

API = "https://www.googleapis.com/drive/v3/files"
CARPETA_FOLDER = "application/vnd.google-apps.folder"
COMUNES = {"supportsAllDrives": "true"}

_sesion = None


class ErrorDrive(Exception):
    pass


def configurado() -> bool:
    return bool(config.DRIVE_CARPETA_ID)


def _s():
    global _sesion
    if _sesion is None:
        if not config.GOOGLE_CREDENCIALES.exists():
            raise ErrorDrive(f"Falta el archivo {config.GOOGLE_CREDENCIALES.name} con la cuenta de servicio de Google.")
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(
            str(config.GOOGLE_CREDENCIALES), scopes=["https://www.googleapis.com/auth/drive"])
        _sesion = AuthorizedSession(cred)
    return _sesion


def _pedir(metodo: str, url: str, **kw):
    import requests
    try:
        r = _s().request(metodo, url, timeout=120, **kw)
    except requests.RequestException:
        raise ErrorDrive("No se pudo conectar con Google Drive.") from None
    except Exception as e:  # errores de credenciales de google-auth
        raise ErrorDrive(f"Google rechazó las credenciales: {e}") from None
    if r.status_code == 404:
        raise ErrorDrive("No encontré la carpeta de Drive. Revisá DRIVE_CARPETA_ID y que esté compartida "
                         f"con {cuenta_servicio()} como Editor.")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            msg = r.text
        raise ErrorDrive(f"Google Drive respondió con un error: {msg[:300]}")
    return r


def cuenta_servicio() -> str:
    import json
    try:
        return json.loads(config.GOOGLE_CREDENCIALES.read_text(encoding="utf-8")).get("client_email", "?")
    except (OSError, ValueError):
        return "(cuenta de servicio)"


def _listar(q: str) -> list[dict]:
    archivos, token = [], None
    while True:
        params = {"q": q, "fields": "nextPageToken,files(id,name,mimeType,createdTime)", "pageSize": 100,
                  "orderBy": "createdTime", "includeItemsFromAllDrives": "true", **COMUNES}
        if token:
            params["pageToken"] = token
        datos = _pedir("GET", API, params=params).json()
        archivos += datos.get("files", [])
        token = datos.get("nextPageToken")
        if not token:
            return archivos


def _carpeta_usadas() -> str:
    q = (f"'{config.DRIVE_CARPETA_ID}' in parents and name='usadas' "
         f"and mimeType='{CARPETA_FOLDER}' and trashed=false")
    existentes = _listar(q)
    if existentes:
        return existentes[0]["id"]
    datos = _pedir("POST", API, params=COMUNES, json={
        "name": "usadas", "mimeType": CARPETA_FOLDER, "parents": [config.DRIVE_CARPETA_ID]}).json()
    return datos["id"]


def fotos_nuevas() -> list[dict]:
    """Imágenes de la carpeta principal (no incluye la subcarpeta usadas)."""
    return _listar(f"'{config.DRIVE_CARPETA_ID}' in parents and mimeType contains 'image/' and trashed=false")


def descargar(archivo: dict, destino: Path) -> None:
    r = _pedir("GET", f"{API}/{archivo['id']}", params={"alt": "media", **COMUNES})
    destino.write_bytes(r.content)


def marcar_usada(archivo: dict) -> None:
    _pedir("PATCH", f"{API}/{archivo['id']}", params={
        "addParents": _carpeta_usadas(), "removeParents": config.DRIVE_CARPETA_ID, **COMUNES})
