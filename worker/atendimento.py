#!/usr/bin/env python3
"""
ATENDIMENTO — classifica conversas e transforma em materia-prima

Com 100 conversas por dia, ler todas nao escala. Este worker le, classifica,
responde o que ja tem resposta pronta, e deixa para a dona SO o que exige
decisao humana.

E faz a coisa que importa mais: transforma cada conversa em research_source
com as FRASES EXATAS da pessoa. A objecao dita no momento da decisao vale
mais que qualquer formulario — e e ela que a proxima oferta precisa resolver.

Duas travas de disciplina, herdadas do playbook de vendas:

  1. Frase exata, nunca parafrase. A etapa 11 do playbook de publico so
     aceita verbatim, e uma parafrase gravada aqui contamina tudo depois.

  2. Quem NAO comprou e a fonte mais valiosa. O motivo do nao vai marcado
     como objecao, e nao some junto com a conversa.
"""
import os
import re
import sys
import traceback
import unicodedata
from datetime import datetime, timezone

import requests

import llm
import wheff

ORG = os.environ.get("WHEFF_ORG", "wheff")
WORKER = f"gh-actions/{os.environ.get('GITHUB_RUN_ID', 'local')}"
AGENTE = "atendimento.classificar:v1"

# Quantas conversas por execucao. O teto do Groq gratuito e por minuto, e
# cada conversa e uma chamada — 12 cabe com folga em 10 minutos de ciclo.
LOTE = int(os.environ.get("WHEFF_LOTE_ATENDIMENTO", "12"))


def _sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def buscar(caminho):
    r = requests.get(f"{wheff.URL}/rest/v1/{caminho}", headers=wheff.H, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"buscar {caminho} ({r.status_code}): {r.text[:300]}")
    return r.json()


def atualizar(tabela, filtro, corpo):
    r = requests.patch(f"{wheff.URL}/rest/v1/{tabela}?{filtro}",
                       headers=wheff.H, json=corpo, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"atualizar {tabela} ({r.status_code}): {r.text[:300]}")


# ── Resposta pronta ────────────────────────────────────────────────────────
def resposta_pronta(texto, fase, prontas):
    """A duvida que se repete nao precisa da dona.

    Casamento por palavra-gatilho, de proposito simples: resposta automatica
    errada custa mais caro que resposta automatica que nao dispara.
    """
    alvo = _sem_acento(texto)
    for p in prontas:
        if not p.get("ativa"):
            continue
        fases = p.get("fases") or []
        if fases and fase and fase not in fases:
            continue
        gatilhos = [_sem_acento(g) for g in (p.get("gatilhos") or [])]
        if gatilhos and any(g and g in alvo for g in gatilhos):
            return p
    return None


# ── Classificacao ──────────────────────────────────────────────────────────
SISTEMA = """Voce classifica conversas de atendimento de uma operacao de lancamento digital no Brasil.

Recebe as mensagens de UMA conversa. Devolve JSON exatamente nesta forma:

{
  "resumo": "",
  "marcadores": [],
  "trechos": [ { "texto": "", "marcadores": [] } ],
  "precisa_humano": false,
  "motivo_humano": "",
  "intencao": ""
}

REGRAS QUE NAO PODEM SER QUEBRADAS:

1. Em "trechos", copie a FRASE EXATA da pessoa. Nao corrija gramatica, nao melhore, nao resuma, nao traduza para linguagem de marketing. A frase como ela foi digitada e o unico formato aceito. Se voce reescrever, o trecho vira inutil.

2. Marcadores validos, e apenas estes: dor, desejo, objecao, crenca, medo, evento_gatilho, linguagem, elogio, frustracao, comparacao, lacuna, referencia_externa, inercia, habito_atual.

3. "precisa_humano" e true quando: a pessoa esta decidindo a compra e fez uma pergunta especifica; reclamou de algo; pediu reembolso; relatou um problema de acesso; ou disse algo que voce nao entendeu com seguranca. Na duvida, marque true — deixar passar para a dona custa menos que responder errado.

4. "intencao" e uma palavra: duvida, compra, reclamacao, suporte, elogio, spam, outro.

5. So marque "objecao" quando a pessoa disser um motivo para NAO comprar. Duvida nao e objecao.

6. Se a conversa nao tiver nada aproveitavel — so emoji, so saudacao, spam — devolva trechos vazio. Inventar trecho para parecer produtivo e pior que devolver vazio.

Portugues do Brasil."""


def classificar(mensagens):
    entrada = "\n".join(
        f"[{m['autor']}] {m['texto'][:600]}" for m in mensagens[:40])
    return llm.groq(SISTEMA, entrada, max_tokens=1500)


def montar_fonte(conversa, mensagens, cls):
    """A conversa vira research_source. Mesmo artefato do comentario colado."""
    trechos = [t for t in (cls.get("trechos") or []) if (t.get("texto") or "").strip()]
    if not trechos:
        return None

    canal = conversa["canal"]
    plataforma = {"instagram_dm": "instagram", "instagram_comment": "instagram",
                  "whatsapp": "outro", "manual": "outro"}.get(canal, "outro")

    itens = []
    for i, t in enumerate(trechos[:200], 1):
        it = {
            "id": f"i{i:03d}",
            "texto": t["texto"].strip()[:6000],
            # Frase exata, e o prompt cobra isso. Se o modelo parafrasear,
            # o dado fica errado de um jeito que ninguem percebe depois.
            "verbatim": True,
        }
        marc = [m for m in (t.get("marcadores") or [])
                if m in ("dor", "desejo", "objecao", "crenca", "medo", "evento_gatilho",
                         "linguagem", "elogio", "frustracao", "comparacao", "lacuna",
                         "referencia_externa", "inercia", "habito_atual")]
        if marc:
            it["marcadores"] = marc[:10]
        itens.append(it)

    fase = conversa.get("fase_funil") or "nao declarada"
    return {
        "titulo": f"Conversa — {conversa.get('contato_nome') or conversa['contato_id'][:20]} ({canal})"[:200],
        "origem": {
            "plataforma": plataforma,
            "titulo_original": f"{canal} · fase {fase}",
            "idioma": "pt-BR",
            "pais": "Brasil",
        },
        "coleta": {
            "modo": "automatica",
            "ferramenta": "atendimento",
            "coletado_em": datetime.now(timezone.utc).date().isoformat(),
            "coletado_por": WORKER,
            "criterio_recorte": f"conversa individual, fase do funil: {fase}",
        },
        "itens": itens,
        "destino": ["audience_profile"],
        "etapas_playbook": [],
        "representatividade": {
            "n_itens": len(itens),
            "n_fontes_distintas": 1,
            # Uma conversa e uma pessoa. Nunca vira conclusao sozinha — mas
            # cem conversas viram, e e por isso que cada uma precisa entrar.
            "serve_para_conclusao": False,
            "vieses_conhecidos": ["Conversa individual: reflete uma pessoa, nao o publico."],
        },
        "limitacoes": [
            "Conversa individual — serve para levantar hipotese, nao para concluir.",
            "Classificada por modelo. Os trechos sao verbatim, mas os marcadores sao leitura "
            "automatica e podem estar errados.",
        ],
    }


def processar(conversa, prontas):
    msgs = buscar(f"mensagens?conversa_id=eq.{conversa['id']}"
                  f"&select=autor,texto,enviada_em&order=enviada_em.asc&limit=60")
    if not msgs:
        return "sem mensagens"

    da_pessoa = [m for m in msgs if m["autor"] == "pessoa"]
    if not da_pessoa:
        return "nada da pessoa ainda"

    cls = classificar(msgs)

    corpo = {
        "resumo": (cls.get("resumo") or "")[:1000],
        "marcadores": (cls.get("marcadores") or [])[:10],
        "precisa_humano": bool(cls.get("precisa_humano")),
        "motivo_humano": (cls.get("motivo_humano") or "")[:500] or None,
    }

    # Resposta pronta so entra quando o modelo NAO pediu humano. Automatizar
    # em cima de duvida que precisa de pessoa e como o suporte ruim funciona.
    sugerida = None
    if not corpo["precisa_humano"]:
        p = resposta_pronta(da_pessoa[-1]["texto"], conversa.get("fase_funil"), prontas)
        if p:
            sugerida = p
            corpo["status"] = "ESPERANDO"

    dados = montar_fonte(conversa, msgs, cls)
    if dados and not conversa.get("fonte_id"):
        art = wheff.criar_artefato(
            ORG, "research_source", "OBSERVED", "research-source:v1", dados,
            criado_por=f"agent:{AGENTE}", escopo="ORG", status="APPROVED",
            snapshot={"agente": AGENTE, "conversa": conversa["id"],
                      "canal": conversa["canal"], "modelo": llm.escolher_modelo()})
        corpo["fonte_id"] = art["id"]

    atualizar("conversas", f"id=eq.{conversa['id']}", corpo)

    if sugerida:
        atualizar("respostas_prontas", f"id=eq.{sugerida['id']}",
                  {"usos": (sugerida.get("usos") or 0) + 1})

    marca = "PRECISA DE VOCE" if corpo["precisa_humano"] else (
        f"resposta pronta: {sugerida['nome']}" if sugerida else "classificada")
    n = len((dados or {}).get("itens") or [])
    return f"{marca} | {n} trecho(s) | {cls.get('intencao', '?')}"


def main():
    try:
        prontas = buscar(f"respostas_prontas?org_id=eq.{ORG}&ativa=eq.true&select=*")
    except Exception:
        prontas = []

    # Conversas ainda nao viradas em fonte. `fonte_id is null` e o marcador de
    # "nao processada" — simples e sem coluna extra de controle.
    pend = buscar(f"conversas?org_id=eq.{ORG}&fonte_id=is.null"
                  f"&status=in.(ABERTA,ESPERANDO)&select=*"
                  f"&order=ultima_em.desc&limit={LOTE}")

    if not pend:
        print("nenhuma conversa para classificar")
        return 0

    print(f"{len(pend)} conversa(s) na fila")
    erros = 0
    for c in pend:
        rot = c.get("contato_nome") or c["contato_id"][:18]
        try:
            print(f"  {rot}: {processar(c, prontas)}")
        except Exception as e:
            erros += 1
            print(f"  {rot}: ERRO {type(e).__name__}: {e}", file=sys.stderr)
            # Uma conversa que falha nao derruba as outras onze.
            continue

    if erros:
        print(f"{erros} de {len(pend)} falharam", file=sys.stderr)
    return 1 if erros == len(pend) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        traceback.print_exc()
        print(f"ERRO: {e}", file=sys.stderr)
        sys.exit(1)
