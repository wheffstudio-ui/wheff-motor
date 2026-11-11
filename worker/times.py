"""
TIMES DE AGENTES (11/11/2026) — um executor só para os times que seguem
playbook.

Recebe da fila:  time.run  {"time": "<id do time>"}
Para cada time:  confere no banco o que ele exige (aprovado), lê o playbook
                 aprovado na tabela playbooks e conduz as etapas em blocos,
                 só com o que veio das entradas.
Produz:          uma peça do tipo do time, HYPOTHESIS, AGUARDANDO APROVAÇÃO,
                 ligada às entradas (derived_from). Nada entra em uso sem a
                 aprovação da dona.

Travas, as mesmas do community.py:
  • não gasta token se falta entrada aprovada — devolve a lista do que falta;
  • pergunta sem base nas entradas fica "sem base nos dados", com confiança 0,
    e vai para a lista de lacunas da peça. Não se preenche com suposição;
  • etapa obrigatória do playbook que o modelo pulou recusa o resultado.

Os times aqui são os que o playbook descreve como trabalho de escrita e
planejamento. Publicar, editar o vídeo de verdade e mexer em anúncio
continuam dependendo das contas conectadas: estes times entregam o PLANO
(o que postar, como cortar, que campanha montar), não a execução.
"""
import json
import os
import sys

import requests

import llm
import wheff

ORG = os.environ.get("WHEFF_ORG", "wheff")
WORKER = f"gh-actions/{os.environ.get('GITHUB_RUN_ID', 'local')}"
POR_BLOCO = 4               # etapas por chamada (teto de tokens do Groq gratuito)
SEM_BASE = "sem base nos dados"

# id do time → playbook, o que exige aprovado, o que usa se houver, o que produz
TIMES = {
    "audience_research":  {"playbook": "PLAYBOOK-PUBLICO",    "exige": ["brand_profile"], "usa": ["market_research"], "produz": "audience_profile"},
    "market_research":    {"playbook": "PLAYBOOK-MERCADO",    "exige": ["brand_profile"], "usa": ["audience_profile"], "produz": "market_research"},
    "branding":           {"playbook": "PLAYBOOK-BRANDING",   "exige": ["brand_profile", "audience_profile", "market_research"], "usa": [], "produz": "brand_profile"},
    "product":            {"playbook": "PLAYBOOK-PRODUTO",    "exige": ["audience_profile", "market_research"], "usa": ["brand_profile", "community_dna"], "produz": "product_offer"},
    "demand_ugc":         {"playbook": "PLAYBOOK-DEMANDA",    "exige": ["audience_profile"], "usa": ["market_research", "community_dna"], "produz": "demand_test_plan"},
    "lp_copy":            {"playbook": "PLAYBOOK-LANDING",    "exige": ["brand_profile", "audience_profile"], "usa": ["community_dna", "product_offer"], "produz": "lp_copy"},
    "lp_design":          {"playbook": "PLAYBOOK-LANDING",    "exige": ["lp_copy"], "usa": ["brand_profile"], "produz": "lp_layout"},
    "trends":             {"playbook": "PLAYBOOK-TENDENCIAS", "exige": [], "usa": ["audience_profile", "market_research"], "produz": "trend_report"},
    "profile_alignment":  {"playbook": "PLAYBOOK-PERFIL",     "exige": ["brand_profile", "audience_profile", "community_dna"], "usa": [], "produz": "profile_plan"},
    "content_production": {"playbook": "PLAYBOOK-PRODUCAO",   "exige": ["copy"], "usa": ["brand_profile", "community_dna"], "produz": "production_pack"},
    "social_media":       {"playbook": "PLAYBOOK-SOCIAL",     "exige": ["brand_profile"], "usa": ["community_dna", "profile_plan"], "produz": "social_plan"},
    "video_edit":         {"playbook": "PLAYBOOK-EDICAO",     "exige": ["brand_profile"], "usa": ["production_pack"], "produz": "edit_plan"},
    "paid_traffic":       {"playbook": "PLAYBOOK-ORGANICO",   "exige": ["brand_profile", "audience_profile"], "usa": ["community_dna", "product_offer"], "produz": "traffic_plan"},
    "sales":              {"playbook": "PLAYBOOK-VENDAS",     "exige": ["brand_profile", "audience_profile"], "usa": ["product_offer"], "produz": "sales_playbook", "foco": "vendas"},
    "support":            {"playbook": "PLAYBOOK-VENDAS",     "exige": ["brand_profile"], "usa": ["community_dna", "product_offer"], "produz": "support_playbook", "foco": "suporte e onboarding"},
    # Big Idea: o método é a etapa "Big Ideia central, narrativa e ângulo único"
    # do playbook de Copy (decisão da dona: não é playbook separado).
    "big_idea":           {"playbook": "PLAYBOOK-COPY", "so_etapas": ["big_ideia"], "exige": ["brand_profile", "audience_profile", "community_dna"],
                           "usa": ["market_research", "product_offer"], "produz": "big_idea"},
    "auto_messages":      {"playbook": "PLAYBOOK-VENDAS",     "exige": ["brand_profile"], "usa": ["community_dna", "product_offer"], "produz": "message_flows",
                           "foco": "roteiros de mensagem para DM e WhatsApp, enviados por uma pessoa (sem automação não oficial)"},
}


# ── Banco ────────────────────────────────────────────────────────────────────
def ler_playbook(pid, get=requests.get):
    r = get(f"{wheff.URL}/rest/v1/playbooks", headers=wheff.H,
            params={"id": f"eq.{pid}", "select": "dados,versao"}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"tabela playbooks ({r.status_code}): rode sql/_local/playbooks_dados.sql no Supabase")
    linhas = r.json()
    if not linhas:
        raise RuntimeError(f"o {pid} não está no banco: rode sql/_local/playbooks_dados.sql no Supabase")
    return linhas[0]["dados"], linhas[0].get("versao")


def entradas(cfg, buscar=wheff.buscar_por_tipo):
    achadas, falta = {}, []
    for t in cfg["exige"]:
        a = buscar(ORG, t, status="APPROVED")
        if a:
            achadas[t] = a[0]
        else:
            falta.append(t)
    for t in cfg["usa"]:
        a = buscar(ORG, t, status="APPROVED")
        if a:
            achadas[t] = a[0]
    return achadas, falta


# ── Prompt ───────────────────────────────────────────────────────────────────
def sistema(pb, cfg):
    regras = "\n".join(f"- {x}" for x in (pb.get("regras") or [])[:15])
    proib = "\n".join(f"- {x}" for x in (pb.get("proibicoes") or [])[:15])
    foco = f"\nFoco deste time: {cfg['foco']}." if cfg.get("foco") else ""
    return f"""Você executa o método "{pb.get('nome')}" para uma operação de coprodução de lançamentos digitais no Brasil.
Papel: {pb.get('papel_do_agente') or ''}{foco}

Você recebe as peças aprovadas pela dona (marca, público, mercado…) e um bloco de etapas do método.
Responda cada pergunta de cada etapa usando SÓ o que está nas peças.

REGRAS QUE NÃO PODEM SER QUEBRADAS:
- Se as peças não trazem base para uma resposta, a resposta é exatamente "{SEM_BASE}" e a confiança é 0.
- Nunca invente número, depoimento, métrica, concorrente ou nome próprio que não esteja nas peças.
- "base" diz de qual peça e campo veio a resposta.
{regras}
Proibido:
{proib}

Devolva JSON exatamente nesta forma:
{{"etapas": [{{"id": "", "respostas": [{{"pergunta": "", "resposta": "", "base": "", "confianca": 0.0}}],
  "resultado": "", "teste": "", "teste_passou": false}}]}}"""


def montar_entrada(ents, bloco, anteriores):
    partes = []
    for tipo, a in ents.items():
        dados = json.dumps(a.get("data") or {}, ensure_ascii=False)[:6000]
        partes.append(f"═══ {tipo} ({a.get('artifact_key')} v{a.get('version')}) ═══\n{dados}")
    if anteriores:
        partes.append("═══ ETAPAS JÁ FEITAS (resultado) ═══\n" +
                      "\n".join(f"{e['id']}: {str(e.get('resultado') or '')[:300]}" for e in anteriores))
    partes.append("═══ ETAPAS DESTE BLOCO ═══\n" + json.dumps(
        [{k: e.get(k) for k in ("id", "nome", "objetivo", "perguntas", "teste", "produz")} for e in bloco],
        ensure_ascii=False))
    return "\n\n".join(partes)


# ── Execução ─────────────────────────────────────────────────────────────────
def conduzir(pb, cfg, ents, chamar=llm.groq):
    etapas_pb = pb.get("etapas") or []
    if cfg.get("so_etapas"):
        etapas_pb = [e for e in etapas_pb if e.get("id") in cfg["so_etapas"]]
    if not etapas_pb:
        raise RuntimeError(f"o {pb.get('playbook_id')} não tem etapas: o método ainda não foi escrito")
    feitas = []
    for i in range(0, len(etapas_pb), POR_BLOCO):
        bloco = etapas_pb[i:i + POR_BLOCO]
        resp = chamar(sistema(pb, cfg), montar_entrada(ents, bloco, feitas))
        por_id = {str(e.get("id")): e for e in (resp.get("etapas") or []) if isinstance(e, dict)}
        for e in bloco:
            r = por_id.get(str(e["id"]))
            if not r:
                if e.get("obrigatoria"):
                    raise RuntimeError(f"o modelo pulou a etapa obrigatória '{e.get('nome')}'. Recusando o resultado.")
                continue
            for q in r.get("respostas") or []:
                txt = str(q.get("resposta") or "").strip()
                if not txt or txt.lower() == SEM_BASE:
                    q["resposta"], q["confianca"], q["base"] = SEM_BASE, 0.0, ""
                elif not str(q.get("base") or "").strip():
                    q["confianca"] = min(float(q.get("confianca") or 0), 0.3)   # sem base declarada, confiança baixa
            r["id"], r["nome"] = e["id"], e.get("nome")
            feitas.append(r)
    return feitas


def resumir(feitas, ents, cfg, faltou_opcional):
    respostas = [q for e in feitas for q in (e.get("respostas") or [])]
    com_base = [q for q in respostas if q.get("resposta") != SEM_BASE]
    lacunas = [f"{e.get('nome')}: {q.get('pergunta')}" for e in feitas for q in (e.get("respostas") or [])
               if q.get("resposta") == SEM_BASE]
    conf = round(sum(float(q.get("confianca") or 0) for q in com_base) / len(respostas), 2) if respostas else 0.0
    limitacoes = ["Hipótese gerada pelo método: revise antes de usar."]
    limitacoes += [f"Gerado sem {t} aprovado." for t in faltou_opcional]
    if lacunas:
        limitacoes.append(f"{len(lacunas)} pergunta(s) sem base nas peças aprovadas: ver lacunas.")
    return {"etapas": feitas, "lacunas": lacunas, "limitacoes": limitacoes, "confidence": conf,
            "entradas": {t: f"{a.get('artifact_key')}:v{a.get('version')}" for t, a in ents.items()}}


def executar(job, chamar=llm.groq, buscar=wheff.buscar_por_tipo, ler=ler_playbook,
             criar=wheff.criar_artefato, ligar=wheff.ligar):
    tid = str((job.get("payload") or {}).get("time") or "")
    cfg = TIMES.get(tid)
    if not cfg:
        raise RuntimeError(f"time '{tid}' não tem executor (veja TIMES em worker/times.py)")
    ents, falta = entradas(cfg, buscar)
    if falta:
        raise RuntimeError("dependência não satisfeita: falta " + ", ".join(t + " aprovado" for t in falta) +
                           ". Crie e APROVE antes de rodar este time.")
    pb, versao = ler(cfg["playbook"])
    feitas = conduzir(pb, cfg, ents, chamar)
    dados = resumir(feitas, ents, cfg, [t for t in cfg["usa"] if t not in ents])
    dados.update({"time": tid, "playbook_id": cfg["playbook"], "playbook_versao": versao})
    art = criar(ORG, cfg["produz"], "HYPOTHESIS", f"{cfg['produz']}:playbook-v1", escopo="ORG",
                status="AWAITING_APPROVAL", criado_por=f"agent:time.{tid}:v1", dados=dados,
                snapshot={"agente": f"time.{tid}:v1", "playbook": f"{cfg['playbook']}:{versao}",
                          "modelo": llm.escolher_modelo() if chamar is llm.groq else "teste",
                          "entradas": dados["entradas"]})
    for t, a in ents.items():
        ligar(ORG, art, a, "derived_from" if t in cfg["exige"] else "informed_by")
    print(f"  criado {art.get('artifact_key')} ({cfg['produz']}, confiança {dados['confidence']}, "
          f"{len(dados['lacunas'])} lacuna(s))")
    return art


# ── Disparo automático (11/11/2026) ──────────────────────────────────────────
# Liga em A Linha da Operação ("Rodar os times sozinho"). Quando todas as
# entradas que um time exige estão aprovadas, põe o time na fila UMA vez por
# combinação de versões: aprovar uma peça nova destrava quem dependia dela, e
# rodar de novo sem nada novo não duplica. Times sem entrada obrigatória e
# times que refazem a própria entrada (Branding) só rodam pelo botão.
def auto_ligado(get=requests.get):
    r = get(f"{wheff.URL}/rest/v1/plataforma", headers=wheff.H, params={"select": "dados->settings->times_auto"}, timeout=30)
    if r.status_code >= 300:
        return False
    return any(v is True for linha in r.json() for v in linha.values())


def chave_auto(tid, ents, cfg):
    return f"time:auto:{tid}:" + ",".join(f"{t}={ents[t]['id']}v{ents[t]['version']}" for t in sorted(cfg["exige"]))


def resolver(buscar=wheff.buscar_por_tipo, enfileirar=wheff.enfileirar):
    postos = []
    for tid, cfg in TIMES.items():
        if not cfg["exige"] or cfg["produz"] in cfg["exige"]:
            continue
        ents, falta = entradas(dict(cfg, usa=[]), buscar)
        if falta:
            continue
        if enfileirar(ORG, "time.run", {"time": tid, "auto": True}, idempotency_key=chave_auto(tid, ents, cfg)):
            postos.append(tid)
    return postos


def main():
    try:
        if auto_ligado():
            postos = resolver()
            print("Disparo automático: " + (", ".join(postos) if postos else "nada novo para rodar"))
    except Exception as e:  # noqa: BLE001 — o disparo não pode travar a fila
        print(f"Disparo automático falhou: {e}", file=sys.stderr)
    feitos = 0
    while feitos < 3:                    # até 3 times por rodada de 10 min
        job = wheff.pegar_job(ORG, WORKER, ["time.run"], minutos=20)
        if not job:
            break
        print(f"Tarefa {job['id']} — {(job.get('payload') or {}).get('time')} — tentativa {job['attempts']}")
        try:
            executar(job)
            wheff.terminar_job(ORG, job["id"])
        except Exception as e:  # noqa: BLE001
            print(f"ERRO: {e}", file=sys.stderr)
            definitivo = str(e).startswith(("dependência", "time '", "o PLAYBOOK"))
            wheff.terminar_job(ORG, job["id"], erro=e, definitivo=definitivo)
        feitos += 1
    if not feitos:
        print("Nada na fila.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
