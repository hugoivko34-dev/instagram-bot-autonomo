"""Pequeño envoltorio para llamar a Claude y obtener JSON."""
import json
import re

import config
import requests

_cliente = None


def _cli():
    global _cliente
    if _cliente is None:
        import anthropic  # tarda varios segundos en cargar: solo cuando la IA se usa de verdad
        _cliente = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY())
    return _cliente


def preguntar(sistema: str, usuario: str, max_tokens: int = 2000, json_mode: bool = False) -> str:
    if config.IA_PROVEEDOR == "local":
        datos = {"model": config.IA_LOCAL_MODELO,
                 "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}],
                 "stream": False, "think": False,
                 "options": {"temperature": 0.2, "num_predict": max_tokens}}
        if json_mode:
            datos["format"] = "json"
        try:
            r = requests.post(f"{config.OLLAMA_URL.rstrip('/')}/api/chat", json=datos, timeout=180)
            r.raise_for_status()
            texto = r.json().get("message", {}).get("content", "")
            return re.sub(r"<think>.*?</think>", "", texto, flags=re.S).strip()
        except requests.RequestException as e:
            raise RuntimeError(f"La IA local (Ollama) no responde: {e}") from e
    r = _cli().messages.create(model=config.MODELO, max_tokens=max_tokens, system=sistema,
                               messages=[{"role": "user", "content": usuario}])
    return "".join(b.text for b in r.content if b.type == "text")


def local_disponible() -> bool:
    try:
        return requests.get(f"{config.OLLAMA_URL.rstrip('/')}/api/tags", timeout=3).ok
    except requests.RequestException:
        return False


def preguntar_json(sistema: str, usuario: str, max_tokens: int = 2000):
    texto = preguntar(sistema + "\nRespondé SOLO con JSON válido, sin texto extra.", usuario, max_tokens,
                      json_mode=config.IA_PROVEEDOR == "local")
    m = re.search(r"[\[{].*[\]}]", texto, re.S)
    if not m:
        raise ValueError(f"Claude no devolvió JSON: {texto[:200]}")
    return json.loads(m.group(0))
