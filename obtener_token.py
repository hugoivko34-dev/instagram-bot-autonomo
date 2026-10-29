"""Valida y guarda un token de Instagram Login para @ivkomaderas_.

Meta Developers > API de Instagram > Configuración con inicio de sesión con Instagram
permite añadir @ivkomaderas_ y generar el token. Pegalo aquí cuando se solicite.
"""
import getpass
import os
import shutil
import sys

import requests

import config

ENV = config.PROJECT_DIR / ".env"
ENV_EXAMPLE = config.PROJECT_DIR / ".env.example"
OBJETIVO = "ivkomaderas_"


def _get(url, **params):
    try:
        r = requests.get(url, params=params, timeout=60)
        data = r.json()
    except requests.RequestException:
        raise SystemExit("No pude conectar con Instagram. No se guardó ningún token.")
    except ValueError:
        raise SystemExit("Instagram devolvió una respuesta inválida. No se guardó ningún token.")
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        raise SystemExit("Instagram respondió con un error: " + err.get("message", "respuesta oculta"))
    return data


def guardar_env(valores):
    if not ENV.exists():
        shutil.copy(ENV_EXAMPLE, ENV)
    with ENV.open(encoding="utf-8") as f:
        lineas = f.read().splitlines()
    obsoletas = {"META_APP_ID", "META_APP_SECRET", "META_SHORT_TOKEN", "FB_PAGE_ID",
                 "INSTAGRAM_USERNAME"}
    lineas = [linea for linea in lineas
              if linea.split("=", 1)[0].strip() not in obsoletas]
    for k, v in valores.items():
        encontrado = False
        for i, linea in enumerate(lineas):
            if linea.split("=", 1)[0].strip() == k and not linea.lstrip().startswith("#"):
                lineas[i] = f"{k}={v}"
                encontrado = True
        if not encontrado:
            lineas.append(f"{k}={v}")
    with ENV.open("w", encoding="utf-8") as f:
        f.write("\n".join(lineas) + "\n")


def main():
    print("Este asistente acepta únicamente el token de @ivkomaderas_.")
    app_id = os.getenv("INSTAGRAM_APP_ID", "").strip() or input(
        "Identificador de la app de Instagram (Meta Developers): ").strip()
    app_secret = os.getenv("INSTAGRAM_APP_SECRET", "").strip() or getpass.getpass(
        "Clave secreta de la app de Instagram (entrada oculta): ").strip()
    token = os.getenv("INSTAGRAM_SHORT_TOKEN", "").strip() or getpass.getpass(
        "Token generado para @ivkomaderas_ (entrada oculta): ").strip()
    if not (app_id and app_secret and token):
        sys.exit("Faltan datos. No se guardó ningún token.")

    # Meta puede entregar un token corto o uno ya intercambiado desde su panel.
    # Primero intenta intercambiar; si el token ya es largo, verifica el recibido directamente.
    token_final = token
    try:
        intercambio = _get("https://graph.instagram.com/access_token", grant_type="ig_exchange_token",
                           client_secret=app_secret, access_token=token)
        token_final = intercambio.get("access_token") or token
    except SystemExit:
        pass
    try:
        perfil = _get("https://graph.instagram.com/me", fields="user_id,username", access_token=token_final)
    except SystemExit:
        sys.exit("Instagram no aceptó el token. Revisá que sea reciente y se haya generado para @ivkomaderas_.")
    usuario = (perfil.get("username") or "").casefold()
    if usuario != OBJETIVO:
        sys.exit(f"El token pertenece a @{usuario or 'desconocida'}, no a @{OBJETIVO}. No se guardó nada.")

    guardar_env({"IG_USER_ID": str(perfil.get("user_id") or perfil.get("id")),
                 "IG_ACCESS_TOKEN": token_final, "INSTAGRAM_API_MODE": "instagram_login"})
    print(f"Conexión guardada para @{OBJETIVO}. Probá: python main.py init")
    print("La clave secreta se usó solo para el intercambio y no se guardó.")


if __name__ == "__main__":
    main()
