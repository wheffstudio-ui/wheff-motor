"""
community.manifesto — a segunda etapa do pipeline de Comunidade.

Recebe da fila:  {"community_dna_artifact_id": "..."}
Exige:           community_dna APPROVED
Produz:          MANIF-000N  community_manifesto  (DERIVED, aguardando aprovacao)

Esta tarefa estava sendo enfileirada pelo community.py desde o comeco e nao
tinha ninguem para consumi-la. Ficava parada na fila para sempre.

A trava desta etapa: o DNA precisa estar APROVADO, nao so existir. Manifesto
e texto que vai ser lido em voz alta e virar identidade de grupo — escrever
isso por cima de uma hipotese que a dona ainda nao leu seria construir em
cima de areia. Se o DNA estiver AWAITING_APPROVAL, o worker devolve a tarefa
para a fila explicando, em vez de gerar.
"""
import os
import sys

import llm
import wheff

ORG = os.environ.get("WHEFF_ORG", "wheff")
WORKER = f"gh-actions/{os.environ.get('GITHUB_RUN_ID', 'local')}"
AGENTE = "community.manifesto:v1"
PROMPT_V = "community-manifesto:v1"


SISTEMA = """Voce escreve o manifesto de uma comunidade, para uma operacao de coproducao de lancamentos digitais no Brasil.

Recebe o DNA da comunidade, ja aprovado pela dona da operacao. Escreve o texto que declara o movimento — aquele que um membro leria e pensaria "isso e sobre mim".

Devolva JSON exatamente nesta forma:

{
  "titulo": "",
  "texto": "",
  "decisoes": [ { "decisao": "", "porque": "", "veio_de": "" } ],
  "versoes_alternativas": [ { "tom": "", "texto": "" } ],
  "precisa_de_voce": [],
  "confidence": 0.0,
  "limitacoes": []
}

REGRAS QUE NAO PODEM SER QUEBRADAS:

1. O manifesto e para ser LIDO EM VOZ ALTA. Frases curtas. Nada de periodo de quatro linhas com tres subordinadas. Se voce nao consegue ler em voz alta sem tomar folego no meio, reescreva.

2. Fale na PRIMEIRA PESSOA DO PLURAL — "nos". Manifesto que fala "voce" e anuncio; manifesto que fala "nos" e pertencimento. Use o nome que os membros usam entre si, se o DNA trouxer um.

3. Use as palavras do publico que estao no DNA. Se la diz "travada" e "publi", use "travada" e "publi". Traduzir para jargao de marketing mata o reconhecimento, que e a unica coisa que o manifesto precisa produzir.

4. O inimigo aparece como SISTEMA, PRATICA ou CRENCA. Nunca uma pessoa, nunca um grupo de pessoas, nunca um concorrente nomeado. Se o texto der a entender que existe um "eles" humano a ser derrotado, reescreva.

5. NAO PROMETA RESULTADO. Nem valor, nem prazo, nem garantia. Manifesto declara o que o grupo defende e o que se recusa a aceitar — nao o que ele entrega. No minuto em que vira promessa, deixa de ser manifesto e vira anuncio, e a marca proibe promessa de resultado rapido.

6. Cada entrada de "decisoes" registra UMA escolha do texto e de onde ela veio: "chamei de X porque o DNA diz Y". Toda escolha de peso precisa ter origem rastreavel no DNA. Se voce escolheu por conta propria, diga isso na decisao — nao invente uma origem.

7. "precisa_de_voce" e onde vai o que voce NAO pode decidir sozinho: nome publico da comunidade, promessa que exigiria compromisso real da operacao, qualquer afirmacao sobre historico ou numero que voce nao tem. Deixar isso em aberto vale mais que preencher com plausivel.

8. "versoes_alternativas" traz no maximo duas, com tons genuinamente diferentes (por exemplo: um mais sobrio, um mais direto). Nao mande a mesma coisa com sinonimos trocados — isso desperdica a escolha de quem vai ler.

9. "confidence" e a sua confianca real de 0 a 1. DNA raso ou contraditorio = confianca baixa. Ser honesto aqui vale mais que parecer seguro.

10. Portugues do Brasil. O texto tem entre 150 e 500 palavras: manifesto longo ninguem le, manifesto curto demais nao diz nada.

LIMITE: voce recebeu apenas o DNA. Nao tem conversa de membro real, nem comunidade existente, nem metrica. O que voce escreve e uma PROPOSTA para a dona aprovar, editar ou recusar — nunca um texto publicado."""


def montar_entrada(dna: dict) -> str:
    """So o que serve para escrever. O DNA inteiro estouraria o teto do minuto."""
    d = dna
    ten = d.get("tensao") or {}
    tr = d.get("transformacao_coletiva") or {}
    ide = d.get("identidade") or {}
    lin = d.get("linguagem") or {}
    per = d.get("pertencimento") or {}

    partes = [
        f"NOME INTERNO: {d.get('nome', '')}",
        f"CAUSA: {d.get('causa', '')}",
        "",
        f"TENSAO COMPARTILHADA: {ten.get('compartilhada', '')}",
        f"INIMIGO (sistema/pratica/crenca): {ten.get('inimigo', '')}",
        f"CUSTO DE NAO AGIR: {ten.get('custo_de_nao_agir', '')}",
        "",
        f"TRAVESSIA — DE: {tr.get('de', '')}",
        f"TRAVESSIA — PARA: {tr.get('para', '')}",
        f"POR QUE JUNTAS: {tr.get('juntas_porque', '')}",
        "",
        f"NOS SOMOS: {ide.get('nos_somos', '')}",
        f"NOS NAO SOMOS: {ide.get('nos_nao_somos', '')}",
        f"COMO SE CHAMAM: {ide.get('como_nos_chamamos', '')}",
        f"ORGULHO: {ide.get('orgulho', '')}",
        "",
        f"CRENCAS: {llm.corta(d.get('crencas'), 8)}",
        f"EXPRESSOES DELAS: {llm.corta(lin.get('expressoes'), 14)}",
        f"NAO USAR: {llm.corta(lin.get('evitar'), 10)}",
        f"TERMOS PROPRIOS: {llm.corta(lin.get('termos_proprios'), 6, campo='termo')}",
        f"QUEM PERTENCE: {llm.corta(per.get('criterios'), 6)}",
        f"QUEM NAO PERTENCE: {llm.corta(per.get('nao_pertence'), 6)}",
        f"DIFERENCIACAO: {d.get('diferenciacao', '')}",
        "",
        # Os riscos entram no prompt de proposito: sao a lista do que o texto
        # NAO pode fazer. Manifesto e onde comunidade vira seita mais rapido.
        f"RISCOS JA MAPEADOS (o texto nao pode cair neles): "
        f"{llm.corta(d.get('riscos'), 6, campo='risco')}",
    ]
    return "\n".join(x for x in partes if x.strip() not in ("", ":"))


def executar(job):
    p = job.get("payload") or {}
    dna_id = p.get("community_dna_artifact_id")

    if dna_id:
        art_dna = wheff.buscar_artefato(dna_id)
    else:
        # Sem id no payload, procura o DNA atual aprovado. Assim a tarefa
        # continua valendo se alguem enfileirar na mao.
        achados = wheff.buscar_por_tipo(ORG, "community_dna", status="APPROVED")
        art_dna = achados[0] if achados else None

    if not art_dna:
        raise RuntimeError(
            "nao achei nenhum DNA de comunidade. O manifesto deriva dele — "
            "rode a estrategia de comunidade primeiro.")

    if art_dna.get("status") != "APPROVED":
        raise RuntimeError(
            f"o DNA {art_dna['artifact_key']} esta {art_dna.get('status')}, nao APPROVED. "
            f"Manifesto e texto que vira identidade de grupo: escrever por cima de "
            f"hipotese que voce ainda nao leu seria construir em areia. "
            f"Aprove o DNA na plataforma e esta tarefa segue sozinha.")

    dna = art_dna.get("data") or {}

    # Retomada: se uma tentativa anterior ja gerou o manifesto, nao gasta
    # token de novo nem cria artefato duplicado.
    ja = wheff.derivados_de(art_dna["id"], "community_manifesto")
    if ja:
        print(f"  ja existe {ja[0]['artifact_key']} — nada a fazer")
        return ja[0]

    print(f"  base: {art_dna['artifact_key']} v{art_dna['version']} "
          f"(confianca {dna.get('confidence')})")

    man = llm.groq(SISTEMA, montar_entrada(dna), max_tokens=3000)

    # ── Recusas ────────────────────────────────────────────────────────────
    texto = (man.get("texto") or "").strip()
    if len(texto) < 100:
        raise RuntimeError("o modelo devolveu um texto curto demais para ser manifesto")

    palavras = len(texto.split())
    if palavras > 800:
        raise RuntimeError(
            f"o manifesto veio com {palavras} palavras. Acima de 800 ninguem le em "
            f"voz alta, e ler em voz alta e o teste da etapa. Recusando.")

    if not (man.get("decisoes") or []):
        raise RuntimeError(
            "o modelo nao registrou nenhuma decisao. Sem dizer de onde veio cada "
            "escolha, o texto e opiniao sem rastro — e ninguem consegue discutir "
            "com ele depois. Recusando o resultado.")

    # A regra 5 e a que mais escapa: promessa transforma manifesto em anuncio.
    suspeitas = [t for t in ("garantimos", "garantia", "prometemos", "em 30 dias",
                             "em 7 dias", "resultado garantido", "faturar", "R$")
                 if t.lower() in texto.lower()]
    if suspeitas:
        raise RuntimeError(
            f"o texto contem promessa ou cifra ({', '.join(suspeitas)}). Manifesto "
            f"declara o que o grupo defende, nao o que entrega. Recusando.")

    man["texto"] = texto
    if not isinstance(man.get("confidence"), (int, float)):
        man["confidence"] = 0.5

    limitacoes = list(man.get("limitacoes") or [])
    limitacoes.append(
        "Proposta gerada a partir do DNA, sem conversa com membro real: nenhuma "
        "frase daqui foi testada com quem deveria se reconhecer nela.")
    if float(dna.get("confidence") or 0) < 0.6:
        limitacoes.append(
            f"O DNA de origem tem confianca {dna.get('confidence')}. O manifesto "
            f"nao pode ser mais confiavel que a base dele.")
    if not (man.get("precisa_de_voce") or []):
        limitacoes.append(
            "O agente nao devolveu nenhuma decisao para voce — desconfie: nome "
            "publico e compromisso da operacao nao sao dele para escolher.")
    man["limitacoes"] = limitacoes

    # A confianca nunca supera a da base. Manifesto seguro sobre DNA incerto
    # e falsa seguranca que se propaga para tudo que derivar dele.
    teto = float(dna.get("confidence") or 1.0)
    man["confidence"] = round(min(float(man["confidence"]), teto), 2)

    art = wheff.criar_artefato(
        ORG, "community_manifesto", "DERIVED", "community-manifesto:v1", escopo="ORG",
        status="AWAITING_APPROVAL", criado_por=f"agent:{AGENTE}", dados=man,
        snapshot={"agente": AGENTE, "prompt": PROMPT_V, "modelo": llm.escolher_modelo(),
                  "dna": f"{art_dna['artifact_key']}:v{art_dna['version']}"})

    wheff.ligar(ORG, art, art_dna, "derived_from")

    print(f"  criado {art['artifact_key']} — {palavras} palavras, "
          f"{len(man.get('decisoes') or [])} decisoes, confianca {man['confidence']}")
    if man.get("precisa_de_voce"):
        print(f"  devolveu {len(man['precisa_de_voce'])} decisoes para voce")
    return art


def main():
    job = wheff.pegar_job(ORG, WORKER, ["community.manifesto"], minutos=15)
    if not job or not job.get("id"):
        print("nada na fila de manifesto")
        return 0
    print(f"Tarefa {job['id']} — tentativa {job.get('attempts')}")
    try:
        executar(job)
        wheff.terminar_job(ORG, job["id"])
        print("OK — aguardando sua aprovacao")
        return 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        wheff.terminar_job(ORG, job["id"], erro=f"{type(e).__name__}: {e}")
        print(f"ERRO: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
