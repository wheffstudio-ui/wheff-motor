"""
Conversa com o Groq — a parte que todo agente repete.

Extraido do community.py quando o segundo agente precisou das mesmas coisas.
Copiar teria significado manter duas versoes da logica de 429, e essa e
justamente a que ja custou uma tarde para acertar.

Nenhum segredo mora aqui. A chave chega por variavel de ambiente.
"""
import json
import os
import re
import time

import requests

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")

# Ordem de preferencia. O catalogo gratuito do Groq muda sem aviso — modelos
# somem —, entao a escolha e feita perguntando o que existe HOJE, e o modelo
# escolhido fica gravado no context_snapshot de quem gerou.
PREFERENCIA = [
    "llama-3.3-70b-versatile", "openai/gpt-oss-120b",
    "moonshotai/kimi-k2-instruct", "qwen/qwen3-32b", "llama-3.1-8b-instant",
]
_MODELO = None


def escolher_modelo():
    global _MODELO
    if _MODELO:
        return _MODELO
    forcado = os.environ.get("GROQ_MODEL")
    if forcado:
        _MODELO = forcado
        return _MODELO
    if not GROQ_KEY:
        raise RuntimeError("falta GROQ_API_KEY — o agente nao tem como pensar")
    r = requests.get("https://api.groq.com/openai/v1/models",
                     headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Groq /models {r.status_code}: {r.text[:300]}")
    disponiveis = {m["id"] for m in r.json().get("data", [])}
    for m in PREFERENCIA:
        if m in disponiveis:
            _MODELO = m
            print(f"  modelo: {m}")
            return m
    resto = sorted(x for x in disponiveis
                   if not any(p in x for p in ("whisper", "tts", "guard", "vision")))
    if not resto:
        raise RuntimeError("nenhum modelo de texto disponivel")
    _MODELO = resto[0]
    print(f"  modelo (fallback): {_MODELO}")
    return _MODELO


def groq(sistema, usuario, max_tokens=4000, tentativas=4, temperatura=0.4):
    """Chama o modelo e devolve JSON. 429 e espera, nao falha — quase sempre."""
    modelo = escolher_modelo()
    for t in range(1, tentativas + 1):
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}",
                     "Content-Type": "application/json"},
            timeout=240,
            json={"model": modelo, "temperature": temperatura, "max_tokens": max_tokens,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": sistema},
                               {"role": "user", "content": usuario}]},
        )
        if r.status_code == 429:
            # Dois 429 diferentes exigem respostas opostas: "esperei demais
            # neste minuto" (esperar resolve) e "esta chamada sozinha e maior
            # que o teto" (esperar nunca resolve, so encurtar).
            det = re.search(r"Limit (\d+), Used (\d+), Requested (\d+)", r.text)
            if det:
                limite, usado, pedido = (int(x) for x in det.groups())
                if pedido > limite:
                    raise RuntimeError(
                        f"a chamada sozinha pede {pedido} tokens e o teto e {limite}. "
                        f"Esperar nao resolve: e preciso encurtar o prompt ou o max_tokens.")
                print(f"    teto do minuto: {usado}/{limite}, pedindo {pedido}")
            if t >= tentativas:
                raise RuntimeError(f"Groq 429 apos {tentativas} tentativas: {r.text[:300]}")
            espera = float(r.headers.get("retry-after") or 0)
            if not espera:
                m = re.search(r"try again in ([0-9.]+)s", r.text)
                espera = float(m.group(1)) if m else 20.0
            espera = min(espera + 2, 70)
            print(f"    esperando {espera:.0f}s ({t}/{tentativas})")
            time.sleep(espera)
            continue
        if r.status_code >= 300:
            raise RuntimeError(f"Groq {r.status_code}: {r.text[:400]}")
        return json.loads(r.json()["choices"][0]["message"]["content"])
    raise RuntimeError("Groq: teto de tokens nao liberou")


def corta(itens, n, campo="texto"):
    """Manda os N primeiros e AVISA quantos ficaram de fora.

    O teto gratuito do Groq e por minuto e conta entrada + saida: perfil rico
    demais deixa de caber. Cortar de forma previsivel, dizendo o que foi
    cortado, e melhor do que estourar e nao gerar nada.
    """
    vals = [(x.get(campo) if isinstance(x, dict) else x) or "" for x in (itens or [])]
    vals = [v for v in vals if v]
    return " | ".join(vals[:n]) + (f"  (+{len(vals)-n} nao enviados)" if len(vals) > n else "")
