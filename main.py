"""Bot de Instagram: python main.py <comando>. Ver README.md."""
import argparse
import os
import socket
import sys
import threading
import time
from datetime import date, datetime

# Windows consoles configured with legacy code pages may fail on emoji in captions.
# Keep the user's current encoding while escaping characters it cannot represent;
# when the output goes to a file (panel or inicio automático) write UTF-8.
for _flujo in (sys.stdout, sys.stderr):
    if _flujo is not None and hasattr(_flujo, "reconfigure"):
        if _flujo.isatty():
            _flujo.reconfigure(errors="backslashreplace")
        else:
            _flujo.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)

import bandeja
import config
import contenido
import db
import fotos
import ig_api
import metricas
import exportar
import planificador
import publicidad
import telegram_bridge

PUERTO_INSTANCIA = 8766  # solo sirve para impedir que corran dos bots a la vez


def mostrar_publicaciones(estados):
    for p in contenido.listar(estados):
        print(f"\n#{p['id']} [{p['tipo'].upper()} - {p['estado']}] programada: {p['programada_para']}")
        if p["foto_local"]:
            print(f"Imagen lista para revisar: {p['foto_local']}")
        else:
            print(f"Idea de imagen: {p['idea_imagen']}")
        print(f"Tema: {p['tema']}" + (f"\n---\n{p['caption']}" if p["caption"] else ""))


def log(texto: str) -> None:
    print(f"[{datetime.now(config.ZONA):%Y-%m-%d %H:%M}] {texto}", flush=True)


_ultimo_error: dict[str, str] = {}


def paso(nombre: str, funcion, *args):
    """Ejecuta una tarea del ciclo; si falla, las demás siguen. Repite el aviso solo si el error cambia."""
    try:
        resultado = funcion(*args)
    except (Exception, SystemExit) as e:
        texto = str(e) or repr(e)
        if _ultimo_error.get(nombre) != texto:
            _ultimo_error[nombre] = texto
            log(f"Error en {nombre}: {texto}")
            if nombre in {"publicar", "planificar", "fotos", "token", "publicidad"}:
                telegram_bridge._avisar(f"⚠️ Error en {nombre}: {texto[:500]}")
        return None
    if nombre in _ultimo_error:
        del _ultimo_error[nombre]
        log(f"{nombre}: volvió a funcionar.")
    return resultado


def ciclo():
    """Una vuelta completa del bot: publicar lo que toca, atender mensajes."""
    n = paso("publicar", contenido.publicar_pendientes)
    if n:
        log(f"Publicadas: {n}")
    r = paso("mensajes", bandeja.procesar)
    if r and any(r.values()):
        log(f"Mensajes -> {r}")


def renovar_token_si_toca() -> None:
    """El token largo dura 60 días: se renueva cada 7 para que nunca llegue a vencer."""
    ultima = db.obtener_ajuste("token_renovado")
    if ultima and (date.today() - date.fromisoformat(ultima)).days < 7:
        return
    dias = ig_api.renovar_token()
    db.guardar_ajuste("token_renovado", date.today().isoformat())
    log(f"Token de Instagram renovado ({dias} días de validez).")


def verificar_cuenta_objetivo() -> dict:
    perfil = ig_api.perfil()
    usuario = (perfil.get("username") or "").casefold()
    if usuario != config.INSTAGRAM_USERNAME:
        raise SystemExit(
            f"El bot está limitado a @{config.INSTAGRAM_USERNAME}, pero estas credenciales corresponden a "
            f"@{perfil.get('username', 'desconocida')}. No se realizó ninguna acción."
        )
    return perfil


def correr():
    candado = socket.socket()
    try:
        candado.bind(("127.0.0.1", PUERTO_INSTANCIA))
    except OSError:
        raise SystemExit("Ya hay un bot corriendo en esta computadora; no se inicia otro.")
    (config.PROJECT_DIR / "salida").mkdir(exist_ok=True)
    (config.PROJECT_DIR / "salida" / "bot.pid").write_text(str(os.getpid()))  # el panel lo usa para detenerlo

    while True:  # sin internet al prender la PC: esperar en vez de cerrarse
        try:
            perfil = verificar_cuenta_objetivo()
            break
        except ig_api.ErrorInstagram as e:
            log(f"No pude verificar la cuenta: {e}. Reintento en 5 minutos.")
            time.sleep(300)
    log(f"Cuenta conectada: @{perfil['username']}. Bot en marcha. Ctrl+C para frenar.")
    log("Piloto automático: historia diaria y post semanal." if config.AUTOPILOTO
        else "Piloto automático apagado: solo publica lo que apruebes.")
    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_bridge.escuchar, name="telegram-poll", daemon=True).start()
        log("Respuestas de Telegram habilitadas.")
    paso("recuperar", contenido.recuperar_interrumpidas)
    ultima_sync = None
    ultima_publicidad = None
    while True:
        hoy = datetime.now(config.ZONA).date()
        if ultima_sync != hoy:  # una vez por día: métricas y token
            if paso("métricas", metricas.sincronizar) is not None:
                ultima_sync = hoy
            paso("token", renovar_token_si_toca)
            paso("excel", exportar.exportar)  # métricas para Power BI en salida/metricas_instagram.xlsx
        hora = datetime.now(config.ZONA).strftime("%Y-%m-%d %H")
        if ultima_publicidad != hora:  # una vez por hora: campaña del mes y gasto
            ultima_publicidad = hora
            aviso = paso("publicidad", publicidad.mantener)
            if aviso:
                telegram_bridge.avisar(aviso)
        if config.AUTOPILOTO:
            n = paso("fotos", planificador.importar)
            if n:
                log(f"Fotos nuevas en el banco: {n}")
            paso("planificar", planificador.planificar)
        else:
            ids = paso("fotos", fotos.procesar_nuevas)  # fotos nuevas en fotos/ -> borradores de post + historia
            if ids:
                bandeja._avisar(f"📝 {len(ids)} borradores nuevos para aprobar: python main.py borradores")
        ciclo()
        time.sleep(config.MINUTOS_ENTRE_REVISIONES * 60)


def main():
    p = argparse.ArgumentParser(description="Bot de Instagram")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("init", help="crea la base de datos y prueba la conexión")
    s.add_parser("reporte", help="métricas y recomendaciones")
    s.add_parser("fotos", help="convierte las fotos nuevas de fotos/ en borradores de post e historia")
    g = s.add_parser("generar", help="(opcional) genera borradores solo de texto, sin foto")
    g.add_argument("-n", type=int, default=3)
    s.add_parser("borradores", help="muestra borradores y publicaciones aprobadas")
    a = s.add_parser("aprobar", help="aprueba un borrador y le asigna la imagen")
    a.add_argument("id", type=int)
    a.add_argument("--imagen", help="solo si el borrador no tiene foto: URL pública https:// JPG")
    a.add_argument("--hora", help="opcional, ej: 2026-11-02T19:00:00-03:00")
    e = s.add_parser("editar", help="edita el texto y/o el horario de un borrador antes de aprobarlo")
    e.add_argument("id", type=int)
    e.add_argument("--texto", help="texto completo para la descripción del post")
    e.add_argument("--hora", help="ej: 2026-11-02T19:00:00-03:00")
    r = s.add_parser("rechazar", help="descarta un borrador")
    r.add_argument("id", type=int)
    s.add_parser("bandeja", help="mensajes importantes pendientes de tu respuesta")
    rp = s.add_parser("responder", help="responde a un mensaje importante")
    rp.add_argument("msg_id")
    rp.add_argument("texto")
    rn = s.add_parser("run", help="deja el bot funcionando de forma continua")
    rn.add_argument("--archivo-log", action="store_true", help="escribe la salida en salida/bot.log")
    s.add_parser("drive", help="prueba la carpeta de Google Drive y muestra las fotos nuevas")
    s.add_parser("plan", help="trae fotos al banco y programa la historia diaria y el post semanal")
    s.add_parser("estado", help="próximas publicaciones automáticas y fotos en el banco")
    s.add_parser("excel", help="sincroniza métricas y genera salida/metricas_instagram.xlsx para Power BI")
    ib = s.add_parser("importar", help="agrega al banco las fotos de una carpeta, sin moverlas")
    ib.add_argument("carpeta")
    args = p.parse_args()

    if args.cmd == "run" and args.archivo_log:
        destino = config.PROJECT_DIR / "salida" / "bot.log"
        destino.parent.mkdir(exist_ok=True)
        if destino.exists() and destino.stat().st_size > 5 * 1024 * 1024:
            destino.replace(destino.with_suffix(".log.1"))
        sys.stdout = sys.stderr = open(destino, "a", encoding="utf-8", errors="backslashreplace", buffering=1)
    db.iniciar()
    if args.cmd == "init":
        print("Conectado como:", verificar_cuenta_objetivo())
    elif args.cmd == "reporte":
        print(metricas.reporte())
    elif args.cmd == "fotos":
        ids = fotos.procesar_nuevas()
        print(f"Borradores creados: {ids}" if ids else "No hay fotos nuevas en la carpeta fotos/")
        mostrar_publicaciones(("borrador",))
    elif args.cmd == "generar":
        metricas.sincronizar()
        print("Borradores creados:", contenido.generar_borradores(args.n))
        mostrar_publicaciones(("borrador",))
    elif args.cmd == "borradores":
        mostrar_publicaciones(("borrador", "aprobada"))
    elif args.cmd == "aprobar":
        contenido.aprobar(args.id, args.imagen, args.hora)
        print("Aprobada. Se publicará sola en el horario programado (con el bot corriendo).")
    elif args.cmd == "editar":
        contenido.editar(args.id, args.texto, args.hora)
        print("Borrador actualizado.")
    elif args.cmd == "rechazar":
        contenido.rechazar(args.id)
    elif args.cmd == "bandeja":
        for m in bandeja.pendientes():
            print(f"\nID {m['ig_msg_id']} ({m['recibido']})\n{m['texto']}")
    elif args.cmd == "responder":
        bandeja.responder_manual(args.msg_id, args.texto)
        print("Enviado.")
    elif args.cmd == "run":
        correr()
    elif args.cmd == "drive":
        import drive
        if not drive.configurado():
            raise SystemExit("Falta DRIVE_CARPETA_ID en el .env.")
        print(f"Cuenta de servicio: {drive.cuenta_servicio()}")
        nuevas = drive.fotos_nuevas()
        print(f"Fotos nuevas en la carpeta: {len(nuevas)}")
        for a in nuevas:
            print(" -", a["name"])
    elif args.cmd == "plan":
        print(f"Fotos importadas: {planificador.importar()}")
        print(f"Publicaciones programadas: {planificador.planificar()}")
        print(planificador.estado())
    elif args.cmd == "excel":
        metricas.sincronizar()
        print(f"Excel generado: {exportar.exportar()}")
    elif args.cmd == "estado":
        print(planificador.estado())
    elif args.cmd == "importar":
        from pathlib import Path
        print(f"Fotos agregadas al banco: {planificador.importar_carpeta(Path(args.carpeta))}")


if __name__ == "__main__":
    main()
