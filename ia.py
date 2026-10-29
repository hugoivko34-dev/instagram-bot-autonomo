"""Pequeño envoltorio para llamar a Claude y obtener JSON."""
import json
import re

import config

_cliente = None


def _cli():
    global _cliente
    if _cliente is None:
        import anthropic  # tarda varios segundos en cargar: solo cuando la IA se usa de verdad
        _cliente = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY())
    return _cliente


def preguntar(sistema: str, usuario: str, max_tokens: int = 2000) -> str:
    r = _cli().messages.create(model=config.MODELO, max_tokens=max_tokens, system=sistema,
                               messages=[{"role": "user", "content": usuario}])
    return "".join(b.text for b in r.content if b.type == "text")


def preguntar_json(sistema: str, usuario: str, max_tokens: int = 2000):
    texto = preguntar(sistema + "\nRespondé SOLO con JSON válido, sin texto extra.", usuario, max_tokens)
    m = re.search(r"[\[{].*[\]}]", texto, re.S)
    if not m:
        raise ValueError(f"Claude no devolvió JSON: {texto[:200]}")
    return json.loads(m.group(0))
