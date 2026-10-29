"""Persistencia local del bot."""
from contextlib import contextmanager
import sqlite3

import config


def iniciar() -> None:
    """Crea las tablas necesarias si todavía no existen."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conexion() as con:
        # WAL permite que el panel, Telegram y el bucle lean y escriban a la vez sin "database is locked".
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY,
                publicada_en TEXT,
                tipo TEXT,
                likes INTEGER NOT NULL DEFAULT 0,
                comentarios INTEGER NOT NULL DEFAULT 0,
                caption TEXT NOT NULL DEFAULT '',
                permalink TEXT
            );

            CREATE TABLE IF NOT EXISTS publicaciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                estado TEXT NOT NULL DEFAULT 'borrador',
                tipo TEXT NOT NULL DEFAULT 'post',
                tema TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL DEFAULT '',
                idea_imagen TEXT NOT NULL DEFAULT '',
                foto_local TEXT,
                image_url TEXT,
                programada_para TEXT,
                ig_media_id TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS mensajes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ig_msg_id TEXT NOT NULL UNIQUE,
                ig_usuario_id TEXT NOT NULL,
                texto TEXT NOT NULL DEFAULT '',
                categoria TEXT NOT NULL,
                respuesta TEXT NOT NULL DEFAULT '',
                estado TEXT NOT NULL,
                recibido TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ajustes (
                clave TEXT PRIMARY KEY,
                valor TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS telegram_links (
                ig_msg_id TEXT PRIMARY KEY,
                telegram_message_id INTEGER,
                chat_id TEXT NOT NULL,
                creado TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS banco_fotos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                ruta TEXT NOT NULL,
                origen TEXT NOT NULL DEFAULT 'local',
                drive_id TEXT UNIQUE,
                agregada TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                usos_post INTEGER NOT NULL DEFAULT 0,
                usos_historia INTEGER NOT NULL DEFAULT 0,
                ultimo_uso TEXT,
                activa INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS promo_meses (
                mes TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                adset_id TEXT NOT NULL,
                presupuesto INTEGER NOT NULL,
                moneda TEXT NOT NULL DEFAULT 'ARS',
                creado TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS promo_posts (
                media_id TEXT PRIMARY KEY,
                caption TEXT NOT NULL DEFAULT '',
                permalink TEXT NOT NULL DEFAULT '',
                activo INTEGER NOT NULL DEFAULT 1,
                agregado TEXT NOT NULL,
                ad_id TEXT,
                creative_id TEXT,
                mes TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS metricas_diarias (
                fecha TEXT PRIMARY KEY,
                seguidores INTEGER,
                alcance INTEGER,
                publicaciones INTEGER,
                sincronizado TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        columnas = {r[1] for r in con.execute("PRAGMA table_info(publicaciones)")}
        for nombre, tipo in (("story_text", "TEXT NOT NULL DEFAULT ''"),
                             ("story_cta", "TEXT NOT NULL DEFAULT ''"),
                             ("foto_origen", "TEXT"),
                             ("intentos", "INTEGER NOT NULL DEFAULT 0"),
                             ("auto", "INTEGER NOT NULL DEFAULT 0"),
                             ("banco_id", "INTEGER")):
            if nombre not in columnas:
                con.execute(f"ALTER TABLE publicaciones ADD COLUMN {nombre} {tipo}")
        for tabla, nuevas in (("media", ("alcance", "guardados", "compartidos", "vistas")),
                              ("promo_meses", ("gastado", "alcance", "impresiones", "conversaciones"))):
            existentes = {r[1] for r in con.execute(f"PRAGMA table_info({tabla})")}
            for nombre in nuevas:
                if nombre not in existentes:
                    con.execute(f"ALTER TABLE {tabla} ADD COLUMN {nombre} REAL")
        # uso: '' = sirve para todo, 'historia' = solo historias, 'post' = solo publicaciones del feed
        if "uso" not in {r[1] for r in con.execute("PRAGMA table_info(banco_fotos)")}:
            con.execute("ALTER TABLE banco_fotos ADD COLUMN uso TEXT NOT NULL DEFAULT ''")


@contextmanager
def conexion():
    """Abre una conexión con filas accesibles por nombre y confirma cada bloque."""
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def obtener_ajuste(clave: str, defecto: str = "") -> str:
    with conexion() as con:
        fila = con.execute("SELECT valor FROM ajustes WHERE clave=?", (clave,)).fetchone()
        return fila["valor"] if fila else defecto


def guardar_ajuste(clave: str, valor: str) -> None:
    with conexion() as con:
        con.execute("INSERT INTO ajustes(clave,valor) VALUES(?,?) "
                    "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (clave, valor))
