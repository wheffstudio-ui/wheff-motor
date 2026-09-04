"""
community.dna — a primeira etapa do pipeline de Comunidade.

Recebe da fila:  {}  (não precisa de payload: as entradas vêm do contrato)
Exige:           brand_profile APPROVED  +  audience_profile APPROVED
Opcional:        market_research APPROVED  — se faltar, vira ressalva gravada
Produz:          CDNA-000N  community_dna  (HYPOTHESIS, aguardando aprovação)
Enfileira:       community.manifesto

O que este arquivo faz de diferente do dna_analyze: ele NÃO confia em quem
enfileirou. Antes de gastar um token, pergunta ao banco se o que ele exige
existe aprovado. Se não existe, devolve a tarefa com a lista do que falta —
em vez de gerar estratégia por cima de vazio.
"""
import os
import sys

import llm
import wheff

ORG = os.environ.get("WHEFF_ORG", "wheff")
WORKER = f"gh-actions/{os.environ.get('GITHUB_RUN_ID', 'local')}"
AGENTE = "community.dna:v1"
PROMPT_V = "community-dna:v1"

# ── O prompt ────────────────────────────────────────────────────────────────
# As três travas do schema aparecem aqui como regra explícita. Sem elas o
# modelo escreve manifesto motivacional que serviria para qualquer nicho.
SISTEMA = """Você é estrategista de comunidade para uma operação de coprodução de lançamentos digitais no Brasil.

Recebe o perfil da MARCA e o perfil do PÚBLICO, ambos aprovados pela dona da operação. Produz o DNA da comunidade: o que faz esse grupo de pessoas virar comunidade em vez de audiência.

Devolva JSON exatamente nesta forma:

{
  "nome": "",
  "causa": "",
  "tensao": { "compartilhada": "", "inimigo": "", "custo_de_nao_agir": "" },
  "transformacao_coletiva": { "de": "", "para": "", "juntas_porque": "" },
  "identidade": { "nos_somos": "", "nos_nao_somos": "", "como_nos_chamamos": "", "orgulho": "" },
  "crencas": [],
  "linguagem": {
    "expressoes": [], "evitar": [],
    "termos_proprios": [ { "termo": "", "significado": "" } ]
  },
  "simbolos": [ { "nome": "", "significado": "" } ],
  "pertencimento": {
    "criterios": [], "nao_pertence": [],
    "comportamentos_desejados": [], "comportamentos_proibidos": []
  },
  "diferenciacao": "",
  "riscos": [ { "risco": "", "sinal_de_alerta": "", "como_evitar": "" } ],
  "afirmacoes": [ { "claim": "", "confidence": 0.0,
                    "evidence": [ { "artifact_type": "", "campo": "", "trecho": "" } ] } ],
  "confidence": 0.0
}

REGRAS QUE NÃO PODEM SER QUEBRADAS:

1. "juntas_porque" precisa dizer por que essa travessia é melhor EM GRUPO do que sozinha. Se você não conseguir responder isso a partir do material recebido, escreva que não conseguiu e baixe a confiança. Não invente motivo. Sem essa resposta, o que está sendo descrito é um curso, não uma comunidade.

2. "riscos" é obrigatório e precisa ter pelo menos três itens reais: onde isto pode virar comunidade de fachada, manipulação, seita, ou grupo que promete pertencimento e entrega venda. Listar risco não é pessimismo — é o que separa estratégia de entusiasmo fabricado.

3. Toda entrada de "afirmacoes" aponta de qual artefato e de qual campo ela veio, com o trecho. Não afirme o que o material não sustenta.

4. "inimigo" é sistema, prática ou crença. Nunca uma pessoa, nunca um grupo de pessoas, nunca um concorrente nomeado.

5. Use a LINGUAGEM do público que você recebeu. Se ele diz "publi" e "travada", use essas palavras. Não traduza para jargão de marketing.

6. Respeite as proibições da marca. Se a marca proíbe promessa de resultado rápido, nada no DNA pode sugerir isso.

7. "confidence" é sua confiança real na leitura, de 0 a 1. Material raso ou contraditório = confiança baixa. Ser honesto aqui vale mais que parecer seguro.

8. Escreva em português do Brasil, na voz da marca.

LIMITE: você recebeu apenas os perfis de marca e público. Não tem dado de comportamento real de nenhuma comunidade existente, nem métrica, nem conversa de membro. Tudo que você produzir é HIPÓTESE a ser testada — escreva como tal."""




def montar_entrada(marca, publico, mercado):
    """
    Contexto por referência, não por cópia: o modelo recebe os campos que
    importam, não o artefato inteiro. Menos token, menos ruído, e fica claro
    de onde cada coisa veio quando ele citar a evidência.
    """
    m, p = marca["data"], publico["data"]
    partes = [
        "═══ MARCA ═══",
        f"Nome: {m.get('nome')}",
        f"Essência: {m.get('essencia')}",
        f"Propósito: {m.get('proposito')}",
        f"INIMIGO COMUM DECLARADO PELA MARCA: {m.get('inimigo_comum')}",
        f"Crenças: {llm.corta(m.get('crencas'), 6)}",
        f"Valores: {' | '.join(v.get('nome','') for v in (m.get('valores') or []))}",
        f"Tom de voz: {(m.get('tom_de_voz') or {}).get('descricao')}",
        f"Somos: {', '.join((m.get('tom_de_voz') or {}).get('somos') or [])}",
        f"NÃO somos: {', '.join((m.get('tom_de_voz') or {}).get('nao_somos') or [])}",
        f"PROIBIÇÕES: {' | '.join(m.get('proibicoes') or [])}",
        f"Não é para: {llm.corta(m.get('nao_e_para'), 5)}",
        f"Jargões: {' | '.join(m.get('jargoes') or [])}",
        "",
        "═══ PÚBLICO ═══",
        f"Nome: {p.get('nome')}",
        f"Resumo: {p.get('resumo')}",
        f"TENSÃO COMPARTILHADA DECLARADA: {p.get('tensao_compartilhada')}",
        "Dores: " + llm.corta(p.get("dores"), 12),
        "Desejos: " + llm.corta(p.get("desejos"), 8),
        "Objeções: " + llm.corta(p.get("objecoes"), 7),
        "Crenças dela: " + llm.corta(p.get("crencas"), 5),
        f"Expressões que ela usa: {', '.join((p.get('linguagem') or {}).get('expressoes') or [])}",
        f"Termos que a afastam: {', '.join((p.get('linguagem') or {}).get('evitar') or [])}",
        f"Confiança do perfil de público: {p.get('confidence')}",
    ]
    if p.get("limitacoes"):
        partes.append("Limitações do perfil de público: " + " | ".join(p["limitacoes"]))

    if mercado:
        mk = mercado["data"]
        partes += ["", "═══ MERCADO ═══", f"Nicho: {mk.get('nicho')}",
                   "Achados: " + " | ".join(a.get("texto", "") for a in (mk.get("achados") or [])),
                   "Lacunas: " + " | ".join(mk.get("lacunas") or [])]
    else:
        partes += ["", "═══ MERCADO ═══",
                   "NÃO HÁ pesquisa de mercado. Não afirme nada sobre concorrentes "
                   "ou posição relativa no mercado — você não tem base para isso."]
    return "\n".join(x for x in partes if x is not None)


def executar(job):
    # ── O contrato, verificado no banco antes de gastar token ───────────────
    marcas = wheff.buscar_por_tipo(ORG, "brand_profile", status="APPROVED")
    publicos = wheff.buscar_por_tipo(ORG, "audience_profile", status="APPROVED")
    mercados = wheff.buscar_por_tipo(ORG, "market_research", status="APPROVED")

    faltando = []
    if not marcas:
        faltando.append("brand_profile aprovado")
    if not publicos:
        faltando.append("audience_profile aprovado")
    if faltando:
        raise RuntimeError(
            "dependência não satisfeita: falta " + ", ".join(faltando) +
            ". Crie e APROVE em Marca e produto antes de rodar este time.")

    marca, publico = marcas[0], publicos[0]
    mercado = mercados[0] if mercados else None

    print(f"  entradas: {marca['artifact_key']} v{marca['version']}, "
          f"{publico['artifact_key']} v{publico['version']}, "
          f"mercado: {mercado['artifact_key'] if mercado else 'AUSENTE (ressalva)'}")

    # Já existe DNA derivado deste público? Não refaz.
    ja = wheff.derivados_de(publico["id"], "community_dna")
    if ja:
        print(f"  {ja[0]['artifact_key']} já existia — nada a fazer")
        return

    dna = llm.groq(SISTEMA, montar_entrada(marca, publico, mercado))

    # ── Validação: o schema exige, o worker confere ─────────────────────────
    if not isinstance(dna.get("confidence"), (int, float)):
        dna["confidence"] = 0.5
    tc = dna.get("transformacao_coletiva") or {}
    if not (tc.get("juntas_porque") or "").strip():
        raise RuntimeError("o modelo não respondeu 'juntas_porque' — sem isso "
                           "não é comunidade, é curso. Recusando o resultado.")
    if len(dna.get("riscos") or []) < 3:
        raise RuntimeError("menos de 3 riscos listados — agente que não vê risco "
                           "está fabricando entusiasmo. Recusando o resultado.")

    limitacoes = [
        "Hipótese não testada: nenhuma comunidade real existe ainda, "
        "nenhum comportamento de membro foi observado.",
        f"Derivado de um perfil de público com confiança {publico['data'].get('confidence')}.",
    ]
    limitacoes += publico["data"].get("limitacoes") or []
    if not mercado:
        limitacoes.append(
            "Gerado sem pesquisa de mercado independente: o posicionamento é "
            "declarado pela marca, não validado contra concorrentes.")
    dna["limitacoes"] = limitacoes

    art = wheff.criar_artefato(
        ORG, "community_dna", "HYPOTHESIS", "community-dna:v1", escopo="ORG",
        status="AWAITING_APPROVAL", criado_por=f"agent:{AGENTE}", dados=dna,
        snapshot={
            "agente": AGENTE, "prompt": PROMPT_V, "modelo": llm.escolher_modelo(),
            "marca": f"{marca['artifact_key']}:v{marca['version']}",
            "publico": f"{publico['artifact_key']}:v{publico['version']}",
            "mercado": f"{mercado['artifact_key']}:v{mercado['version']}" if mercado else None,
        })

    # Genealogia: derivado do público (a tensão), informado pela marca (o inimigo)
    wheff.ligar(ORG, art, publico, "derived_from")
    wheff.ligar(ORG, art, marca, "informed_by")
    if mercado:
        wheff.ligar(ORG, art, mercado, "informed_by")

    print(f"  criado {art['artifact_key']} (confiança {dna['confidence']}, "
          f"{len(dna.get('riscos') or [])} riscos)")

    wheff.enfileirar(ORG, "community.manifesto",
                     {"community_dna_artifact_id": art["id"]},
                     idempotency_key=f"manifesto:{art['id']}")
    print("  enfileirado community.manifesto")


def main():
    job = wheff.pegar_job(ORG, WORKER, ["community.dna"], minutos=15)
    if not job:
        print("Nada na fila.")
        return 0
    print(f"Tarefa {job['id']} — tentativa {job['attempts']}")
    try:
        executar(job)
        wheff.terminar_job(ORG, job["id"])
        print("OK — aguardando sua aprovação")
        return 0
    except Exception as e:
        print(f"ERRO: {e}", file=sys.stderr)
        wheff.terminar_job(ORG, job["id"], erro=e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
