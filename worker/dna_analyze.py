"""
dna.analyze — lê a transcrição e produz o DNA do conteúdo.

Recebe da fila:  { "video_artifact_id": ..., "transcript_artifact_id": ... }
Produz:          DNA-000N          o que o vídeo faz e por quê  (HYPOTHESIS)
                 TRANSLATION-000N  texto em português BRASILEIRO (DERIVED)

A IA lê o texto ORIGINAL, nunca a tradução.
"""
import json
import os
import sys
import time

import re

import requests

import wheff

ORG = os.environ.get("WHEFF_ORG", "wheff")
WORKER = f"gh-actions/{os.environ.get('GITHUB_RUN_ID', 'local')}"
GROQ_KEY = os.environ["GROQ_API_KEY"]
AGENTE = "dna.analyze:v1"

# O Groq muda o catálogo de modelos sem aviso — foi assim que a primeira
# execução quebrou. Em vez de fixar um nome, perguntamos quais existem hoje
# e pegamos o melhor da lista. O modelo realmente usado fica gravado no
# context_snapshot do artefato, que é justamente para isso que ele serve.
PREFERENCIA = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "moonshotai/kimi-k2-instruct",
    "qwen/qwen3-32b",
    "llama-3.1-8b-instant",
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

    # Nenhum da lista: pega qualquer um que não seja de áudio ou moderação
    resto = sorted(x for x in disponiveis
                   if not any(p in x for p in ("whisper", "tts", "guard", "vision")))
    if not resto:
        raise RuntimeError(f"nenhum modelo de texto disponível: {sorted(disponiveis)}")
    _MODELO = resto[0]
    print(f"  modelo (fallback): {_MODELO}")
    return _MODELO


def groq(sistema, usuario, max_tokens=4000, tentativas=4):
    """
    Chama o Groq respeitando o teto de tokens por minuto do plano gratuito.

    O 429 não é erro de verdade: é o Groq dizendo "espera N segundos". Ele
    informa quanto esperar, então esperamos em vez de falhar a tarefa toda.
    """
    MODELO = escolher_modelo()
    for tentativa in range(1, tentativas + 1):
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}",
                     "Content-Type": "application/json"},
            timeout=180,
            json={"model": MODELO, "temperature": 0.3, "max_tokens": max_tokens,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": sistema},
                               {"role": "user", "content": usuario}]},
        )
        if r.status_code == 429 and tentativa < tentativas:
            espera = float(r.headers.get("retry-after") or 0)
            if not espera:
                m = re.search(r"try again in ([0-9.]+)s", r.text)
                espera = float(m.group(1)) if m else 20.0
            espera = min(espera + 2, 70)
            print(f"    teto de tokens atingido, esperando {espera:.0f}s "
                  f"(tentativa {tentativa}/{tentativas})")
            time.sleep(espera)
            continue
        if r.status_code >= 300:
            raise RuntimeError(f"Groq {r.status_code}: {r.text[:400]}")
        return json.loads(r.json()["choices"][0]["message"]["content"])
    raise RuntimeError("Groq: teto de tokens não liberou depois de várias esperas")


# O LIMITE ABSOLUTO no fim do prompt é o que impede o modelo de inventar
# "corte rápido aos 2s" a partir de uma transcrição de áudio. Sem ele, a
# plataforma produz dado falso com aparência de dado real.
SISTEMA_DNA = """Você analisa conteúdo de redes sociais para uma operação de marketing brasileira.

Recebe a transcrição de um vídeo com o segundo exato de cada fala. Devolve JSON:

{
  "formato": "talking_head | ugc | tutorial | storytelling | entrevista | pov | reacao | demonstracao | outro",
  "hook": { "texto": "", "tipo": "curiosidade | contradicao | promessa | dor | provocacao | identificacao | ceticismo", "inicio": 0.0, "fim": 0.0, "por_que_funciona": "" },
  "estrutura": [ { "inicio": 0.0, "fim": 0.0, "papel": "hook | problema | contexto | argumento | prova | virada | cta", "resumo": "" } ],
  "promessa": "",
  "dor_explorada": "",
  "desejo_explorado": "",
  "objecao_tratada": "",
  "angulos": [],
  "emocao_dominante": "",
  "mecanismos_retencao": [ { "nome": "", "inicio": 0.0, "descricao": "" } ],
  "cta": { "texto": "", "tipo": "direto | suave | ausente", "inicio": 0.0 },
  "padrao_reaproveitavel": "",
  "afirmacoes": [ { "claim": "", "confidence": 0.0, "evidence": [ { "trecho_id": 0, "inicio": 0.0, "fim": 0.0 } ] } ],
  "confidence": 0.0
}

REGRAS:
- Todo "inicio"/"fim" vem dos segundos reais da transcrição. Nunca invente número.
- Toda afirmação em "afirmacoes" aponta o trecho que a sustenta. Sem evidência, não afirme.
- "confidence" no topo é sua confiança geral na leitura (0 a 1). Seja honesto: transcrição curta ou confusa = confiança baixa.
- "padrao_reaproveitavel" descreve a ESTRUTURA reaproveitável em outro nicho, não o assunto. Ex: "ceticismo declarado antes da prova", não "falar sobre autoestima".
- Escreva em português do Brasil.

LIMITE ABSOLUTO: você recebeu SOMENTE o áudio transcrito. Não viu imagem, corte, enquadramento, texto na tela nem edição. NUNCA afirme nada visual. Se algo depende de ver o vídeo, deixe de fora."""

SISTEMA_TRAD = """Você traduz para português do BRASIL, não de Portugal.

Recebe JSON com trechos numerados. Devolve JSON: { "trechos": [ { "id": 0, "texto": "" } ] }

REGRAS:
- Brasileiro de verdade: "você", nunca "tu"/"és"/"percebeste"/"achas".
- Gíria de marketing e negócio traduz pelo SENTIDO, não ao pé da letra.
- Mantenha o tom: se é agressivo, fica agressivo; se tem palavrão, mantém.
- Preserve os mesmos ids. Não junte nem divida trechos."""


def executar(job):
    tr = wheff.buscar_artefato(job["payload"]["transcript_artifact_id"])
    if not tr:
        raise RuntimeError("transcrição não encontrada")
    trechos = tr["data"].get("trechos") or []
    if not trechos:
        raise RuntimeError("transcrição vazia")

    texto = "\n".join(f"[{t['id']}] {t['inicio']}s–{t['fim']}s: {t['texto']}"
                      for t in trechos)

    # Se uma execução anterior morreu no meio, aproveita o que já ficou
    # pronto. Sem isso, cada tentativa criava um DNA duplicado.
    ja_tem = wheff.derivados_de(tr["id"], "content_dna")
    if ja_tem:
        art = ja_tem[0]
        print(f"  {art['artifact_key']} já existia — reaproveitando")
    else:
        art = None

    # 1. DNA — sempre sobre o texto original
    dna = None if art else groq(SISTEMA_DNA,
               f"Idioma original: {tr['data'].get('idioma')}\n"
               f"Duração: {tr['data'].get('duracao')}s\n\n{texto}")
    if dna is not None:
        if not isinstance(dna.get("confidence"), (int, float)):
            dna["confidence"] = 0.5
        dna["idioma_original"] = tr["data"].get("idioma")
        dna["limitacoes"] = ["Somente áudio analisado. "
                             "Sem análise visual, de corte ou de texto em tela."]

        art = wheff.criar_artefato(
            ORG, "content_dna", "HYPOTHESIS", "content-dna:v1", escopo="SHARED",
            status="AWAITING_APPROVAL",      # você decidiu ver tudo antes
            criado_por=f"agent:{AGENTE}", dados=dna,
            snapshot={"agente": AGENTE, "modelo": escolher_modelo(),
                      "prompt": "dna:v1"})
        wheff.ligar(ORG, art, tr, "derived_from")
        print(f"  criado {art['artifact_key']} (confiança {dna['confidence']})")

    # 2. Tradução pt-BR — a do argos saiu em português de Portugal
    ja_traduzido = [x for x in wheff.derivados_de(tr["id"], "translation")
                    if (x.get("data") or {}).get("idioma_destino") == "pt-BR"]
    if ja_traduzido:
        print(f"  {ja_traduzido[0]['artifact_key']} já existia — pulando")
    elif (tr["data"].get("idioma") or "").lower() != "pt":
        t = groq(SISTEMA_TRAD, json.dumps(
            {"trechos": [{"id": x["id"], "texto": x["texto"]} for x in trechos]},
            ensure_ascii=False), max_tokens=1500)
        por_id = {x["id"]: x["texto"] for x in (t.get("trechos") or [])}
        traduzidos = [{**x, "texto": por_id.get(x["id"], x["texto"])} for x in trechos]

        trad = wheff.criar_artefato(
            ORG, "translation", "DERIVED", "translation:v1", escopo="SHARED",
            criado_por="agent:traducao-groq:v1",
            dados={"idioma_origem": tr["data"].get("idioma"),
                   "idioma_destino": "pt-BR",
                   "texto": " ".join(x["texto"] for x in traduzidos),
                   "trechos": traduzidos,
                   "via": f"groq:{escolher_modelo()}"},
            snapshot={"modelo": escolher_modelo(),
                      "prompt": "traducao-ptbr:v1"})
        wheff.ligar(ORG, trad, tr, "derived_from")
        print(f"  criado {trad['artifact_key']} (pt-BR)")


def main():
    feitos = 0
    # Esvazia a fila: pode haver mais de uma análise esperando.
    while True:
        job = wheff.pegar_job(ORG, WORKER, ["dna.analyze"], minutos=10)
        if not job:
            break
        print(f"Tarefa {job['id']} — tentativa {job['attempts']}")
        try:
            executar(job)
            wheff.terminar_job(ORG, job["id"])
            feitos += 1
            print("  OK — aguardando sua aprovação")
        except Exception as e:
            print(f"  ERRO: {e}", file=sys.stderr)
            wheff.terminar_job(ORG, job["id"], erro=e)
            return 1
        if feitos >= 10:      # teto por execução, para não estourar o runner
            break

    print(f"{feitos} análise(s)." if feitos else "Nada na fila.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
