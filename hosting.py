"""Sube una imagen local a Cloudinary (plan gratis) para obtener la URL pública que exige Instagram."""
import hashlib
import time

import requests

import config


def subir(ruta_local: str) -> str:
    nube, clave, secreto = config.CLOUDINARY_NUBE(), config.CLOUDINARY_KEY(), config.CLOUDINARY_SECRET()
    ts = str(int(time.time()))
    firma = hashlib.sha1(f"timestamp={ts}{secreto}".encode()).hexdigest()
    try:
        with open(ruta_local, "rb") as f:
            r = requests.post(f"https://api.cloudinary.com/v1_1/{nube}/image/upload",
                              data={"api_key": clave, "timestamp": ts, "signature": firma},
                              files={"file": f}, timeout=120)
        datos = r.json()
    except requests.RequestException:
        raise RuntimeError("No se pudo conectar con Cloudinary. Revisá la conexión a internet.") from None
    except ValueError:
        raise RuntimeError(f"Cloudinary devolvió una respuesta inválida (HTTP {r.status_code}).") from None
    if r.status_code >= 400 or "secure_url" not in datos:
        raise RuntimeError(f"No pude subir la imagen: {datos.get('error', {}).get('message', r.text)}")
    return datos["secure_url"]
