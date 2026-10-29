"""Configuración: todo se lee del archivo .env (nunca pongas claves en el código)."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


def _req(nombre: str) -> str:
    valor = os.getenv(nombre, "").strip()
    if not valor:
        raise SystemExit(f"Falta la variable {nombre} en el archivo .env (mirá .env.example)")
    return valor


# --- Instagram / Meta ---
IG_USER_ID = lambda: _req("IG_USER_ID")          # ID de la cuenta profesional de Instagram
IG_TOKEN = lambda: _req("IG_ACCESS_TOKEN")       # Token de acceso de Instagram (larga duración)
# Cuenta autorizada para esta copia del bot. No se toma de .env para impedir
# que una configuración accidental dirija publicaciones a otra cuenta.
INSTAGRAM_USERNAME = "ivkomaderas_"
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v26.0")
INSTAGRAM_API_MODE = os.getenv("INSTAGRAM_API_MODE", "instagram_login").strip().lower()
if INSTAGRAM_API_MODE not in {"instagram_login", "facebook_login"}:
    raise SystemExit("INSTAGRAM_API_MODE debe ser instagram_login o facebook_login")
# En Instagram Login, la cuenta profesional es también el recurso usado para los DMs.
ID_MENSAJES = lambda: _req("IG_USER_ID") if INSTAGRAM_API_MODE == "instagram_login" else _req("FB_PAGE_ID")

# --- Claude (para generar contenido y clasificar mensajes) ---
ANTHROPIC_KEY = lambda: _req("ANTHROPIC_API_KEY")
MODELO = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

# --- Modo sin IA: no hace falta clave de Anthropic. Los textos salen de plantillas y de tus notas,
#     y los mensajes te llegan como aviso para que respondas vos desde la app de Instagram. ---
MODO_SIN_IA = os.getenv("MODO_SIN_IA", "si").lower() == "si"
HASHTAGS = os.getenv("HASHTAGS", "#pisosdemadera #pulidodepisos #plastificado #pisosdeparquet #decoracion #hogar")
CIERRE_POST = os.getenv("CIERRE_POST", "Escribinos por DM o WhatsApp para consultar.")
TEXTO_HISTORIA = os.getenv("TEXTO_HISTORIA", "Pulido y plastificado de pisos")
CTA_HISTORIA = os.getenv("CTA_HISTORIA", "Consultanos por DM")

# --- Tu negocio ---
DESCRIPCION_NEGOCIO = os.getenv(
    "DESCRIPCION_NEGOCIO",
    "Negocio de servicios. Describí acá a qué te dedicás, tu cliente ideal y tu tono de voz.",
)
WHATSAPP_NUMERO = os.getenv("WHATSAPP_NUMERO", "").strip()  # solo dígitos, ej: 5491112345678

# --- Alojamiento de imágenes (Cloudinary, plan gratis): Instagram exige una URL pública ---
CLOUDINARY_NUBE = lambda: _req("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_KEY = lambda: _req("CLOUDINARY_API_KEY")
CLOUDINARY_SECRET = lambda: _req("CLOUDINARY_API_SECRET")

# --- Estilo de las historias ---
NOMBRE_MARCA = os.getenv("NOMBRE_MARCA", "").strip()          # ej: "Pisos Pérez"
FUENTE_PATH = os.getenv("FUENTE_PATH", "").strip()            # ruta a una .ttf propia (opcional)
_c = os.getenv("COLOR_MARCA", "181,101,29").split(",")        # RGB del botón de la historia
COLOR_MARCA = tuple(int(x) for x in _c[:3])

# --- Notificaciones opcionales por Telegram (para avisarte de mensajes importantes) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# --- Comportamiento ---
ZONA = ZoneInfo(os.getenv("ZONA_HORARIA", "America/Argentina/Buenos_Aires"))
RESPUESTAS_AUTOMATICAS = os.getenv("RESPUESTAS_AUTOMATICAS", "no").lower() == "si"
MINUTOS_ENTRE_REVISIONES = int(os.getenv("MINUTOS_ENTRE_REVISIONES", "5"))
MAX_PUBLICACIONES_POR_DIA = int(os.getenv("MAX_PUBLICACIONES_POR_DIA", "1"))
MAX_HISTORIAS_POR_DIA = int(os.getenv("MAX_HISTORIAS_POR_DIA", "3"))
# --- Piloto automático: historia diaria + post semanal tomando fotos del banco (Drive o fotos/) ---
AUTOPILOTO = os.getenv("AUTOPILOTO", "si").lower() == "si"
HORA_HISTORIA = int(os.getenv("HORA_HISTORIA", "12"))        # se usa mientras no haya métricas suficientes
DIAS_AVISO_POST = int(os.getenv("DIAS_AVISO_POST", "2"))     # con cuánta anticipación se elige y avisa el post
HORAS_MAX_ATRASO_HISTORIA = int(os.getenv("HORAS_MAX_ATRASO_HISTORIA", "6"))
INTENTOS_PUBLICACION = int(os.getenv("INTENTOS_PUBLICACION", "3"))

# --- Google Drive: carpeta de donde el bot toma las fotos ---
DRIVE_CARPETA_ID = os.getenv("DRIVE_CARPETA_ID", "").strip()
GOOGLE_CREDENCIALES = Path(os.getenv("GOOGLE_CREDENCIALES", "credenciales_google.json")).expanduser()
if not GOOGLE_CREDENCIALES.is_absolute():
    GOOGLE_CREDENCIALES = PROJECT_DIR / GOOGLE_CREDENCIALES

DB_PATH = Path(os.getenv("DB_PATH", "bot.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_DIR / DB_PATH
