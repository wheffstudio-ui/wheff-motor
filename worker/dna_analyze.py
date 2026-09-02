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

import requests

import wheff

ORG = os.environ.get("WHEFF_ORG", "wheff")
WORKER = f"gh-actions/{os.environ.get('GITHUB_RUN_ID', 'local')}"
GROQ_KEY = os.environ["GROQ_API_KEY"]
MODELO = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
AGENTE = "dna.analyze:v1"


def groq(sistema, usuario, max_tokens=4000):
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
    if r.status_code >= 300:
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:400]}")
    return json.loads(r.json()["choices"][0]["message"]["content"])


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

    # 1. DNA — sempre sobre o texto original
    dna = groq(SISTEMA_DNA,
               f"Idioma original: {tr['data'].get('idioma')}\n"
               f"Duração: {tr['data'].get('duracao')}s\n\n{texto}")
    if not isinstance(dna.get("confidence"), (int, float)):
        dna["confidence"] = 0.5
    dna["idioma_original"] = tr["data"].get("idioma")
    dna["limitacoes"] = ["Somente áudio analisado. "
                         "Sem análise visual, de corte ou de texto em tela."]

    art = wheff.criar_artefato(
        ORG, "content_dna", "HYPOTHESIS", "content-dna:v1", escopo="SHARED",
        status="AWAITING_APPROVAL",          # você decidiu ver tudo antes
        criado_por=f"agent:{AGENTE}", dados=dna,
        snapshot={"agente": AGENTE, "modelo": MODELO, "prompt": "dna:v1"})
    wheff.ligar(ORG, art, tr, "derived_from")
    print(f"  criado {art['artifact_key']} (confiança {dna['confidence']})")

    # 2. Tradução pt-BR — a do argos saiu em português de Portugal
    if (tr["data"].get("idioma") or "").lower() != "pt":
        t = groq(SISTEMA_TRAD, json.dumps(
            {"trechos": [{"id": x["id"], "texto": x["texto"]} for x in trechos]},
            ensure_ascii=False))
        por_id = {x["id"]: x["texto"] for x in (t.get("trechos") or [])}
        traduzidos = [{**x, "texto": por_id.get(x["id"], x["texto"])} for x in trechos]

        trad = wheff.criar_artefato(
            ORG, "translation", "DERIVED", "translation:v1", escopo="SHARED",
            criado_por="agent:traducao-groq:v1",
            dados={"idioma_origem": tr["data"].get("idioma"),
                   "idioma_destino": "pt-BR",
                   "texto": " ".join(x["texto"] for x in traduzidos),
                   "trechos": traduzidos, "via": f"groq:{MODELO}"},
            snapshot={"modelo": MODELO, "prompt": "traducao-ptbr:v1"})
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
