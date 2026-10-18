"""
Notificações no celular (item 18 da lista da plataforma).

Todo dia às 8h (Brasília) monta os avisos de cada pessoa da lista de acesso,
guarda na caixa de avisos da plataforma (public.avisos, o sino) e, para quem
ativou o celular (public.push_inscricoes), envia também por Web Push:

  · só o que é da função dela (área do Perfil -> função, a mesma divisão do
    Guia); a parceira recebe o que ela grava no dia seguinte;
  · sem repetir: o que já foi avisado fica em public.push_avisos;
  · uma notificação por pessoa por dia, com até 3 linhas; tocar abre a tela
    do aviso mais urgente;
  · segunda-feira: resumo da semana (com um parágrafo da IA, se houver chave
    do Groq; sem chave, só os números).

Nenhum número é inventado: tudo vem do banco da plataforma (tabela
plataforma, campo dados) e da fila do motor (artifacts).

Modo teste (TESTE=1): manda uma notificação de teste para todos os aparelhos.
"""
import datetime as dt
import json
import os

import requests
from pywebpush import WebPushException, webpush

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
VAPID_PRIVATE_KEY = os.environ["VAPID_PRIVATE_KEY"]
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT") or "mailto:wheffstudio@gmail.com"
TESTE = os.environ.get("TESTE") == "1"
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

BRT = dt.timezone(dt.timedelta(hours=-3))            # Brasília, sem horário de verão
AGORA = dt.datetime.now(BRT)
HOJE = AGORA.date().isoformat()
AMANHA = (AGORA.date() + dt.timedelta(days=1)).isoformat()
HA_2_DIAS = (AGORA.date() - dt.timedelta(days=2)).isoformat()
SEMANA = AGORA.strftime("%G-%V")

# Área do Perfil -> função (igual ao Guia, core/guia.js)
DA_AREA = {
    "Coprodução / TI": "coprodutora", "Edição de Vídeos": "editora", "Design de Posts": "designer",
    "Identidade Visual (Marca)": "designer", "Gestão de Tráfego Pago e Orgânico": "trafego",
}
TODOS = {"atraso", "meta", "roteiro", "producao", "aprovacao"}
POR_FUNCAO = {
    "coprodutora": TODOS, "estrategista": TODOS,
    "trafego": {"atraso", "meta"}, "otimizadora": {"atraso", "meta"},
    "designer": {"roteiro", "producao", "aprovacao"}, "editora": {"roteiro", "producao", "aprovacao"},
    "analista": {"meta", "aprovacao"}, "coletora": {"aprovacao"},
}
ROT_FASE = {"pre": "Pré-lançamento", "captura": "Captação de Leads", "evento": "Evento / CPLs",
            "carrinho": "Abertura de Carrinho", "followup": "Follow-up & Objeções", "pos": "Pós-lançamento"}
ROT_PECA = {"brand_profile": "Perfil de Marca", "audience_profile": "Perfil de Público",
            "content_dna": "DNA de Conteúdo", "community_dna": "DNA da Comunidade"}


# ── Banco (REST do Supabase, com a chave de serviço) ─────────────────────────
def ler(tabela, params=None, opcional=False):
    r = requests.get(f"{URL}/rest/v1/{tabela}", headers=H, params=params or {"select": "*"}, timeout=60)
    if opcional and r.status_code in (400, 404):
        return []
    r.raise_for_status()
    return r.json()


def inserir(tabela, linhas):
    if not linhas:
        return
    r = requests.post(f"{URL}/rest/v1/{tabela}", headers={**H, "Prefer": "resolution=ignore-duplicates"},
                      data=json.dumps(linhas), timeout=60)
    r.raise_for_status()


def atualizar(tabela, filtro, dados):
    requests.patch(f"{URL}/rest/v1/{tabela}", headers=H, params=filtro, data=json.dumps(dados), timeout=60)


def apagar(tabela, filtro):
    requests.delete(f"{URL}/rest/v1/{tabela}", headers=H, params=filtro, timeout=60)


def ddmm(d):
    return f"{d[8:10]}/{d[5:7]}" if d and len(d) >= 10 else ""


# ── Os avisos de um espaço (as mesmas regras do Painel geral) ────────────────
def avisos_do_espaco(dados):
    out = []

    def add(tipo, chave, texto, abrir, peso):
        out.append({"tipo": tipo, "chave": chave, "texto": texto, "abrir": abrir, "peso": peso})

    for f in dados.get("funnels") or []:
        if f.get("isTemplate"):
            continue
        for s in f.get("stages") or []:
            fim = s.get("dataFim") or ""
            if fim and fim < HOJE and s.get("status") != "done":
                add("atraso", f"etapa:{s.get('id')}:{fim}",
                    f"Etapa atrasada: {s.get('name') or 'etapa'} ({f.get('name') or 'funil'}), venceu {ddmm(fim)}", "funnels", 3)

    for l in dados.get("lancamentos") or []:
        if l.get("status") == "encerrado":
            continue
        for fase in l.get("fases") or []:
            fim = fase.get("endDate") or ""
            if fim and fim < HOJE and fase.get("status") != "done":
                add("atraso", f"fase:{l.get('id')}:{fase.get('phaseId')}:{fim}",
                    f"Fase atrasada em {l.get('nome') or 'lançamento'}: {ROT_FASE.get(fase.get('phaseId'), fase.get('phaseId'))}, "
                    f"terminava {ddmm(fim)}", "lancamentos", 3)

    for o in dados.get("okrs") or []:
        for k in o.get("krs") or []:
            if k.get("status") == "em_risco":
                add("meta", f"kr:{k.get('id') or k.get('descricao')}:{SEMANA}",
                    f"Meta em risco: {k.get('descricao') or 'resultado-chave'}", "okrs", 2)

    for e in dados.get("events") or []:
        if e.get("date") != AMANHA or e.get("status") == "done":
            continue
        sem_roteiro = not e.get("copyId") and not any(
            str(e.get(c) or "").strip() for c in ("copy_hook", "copy_corpo", "copy_cta", "copy_estrutura"))
        if sem_roteiro:
            add("roteiro", f"roteiro:{e.get('id')}:{AMANHA}", f"Amanhã sem roteiro: {e.get('title') or 'conteúdo'}", "calendar", 2)
        if e.get("paraProdutora") is True or "produtora" in str(e.get("responsible") or "").lower():
            hora = f" às {e['time']}" if e.get("time") else ""
            add("grava", f"grava:{e.get('id')}:{AMANHA}", f"Amanhã você grava: {e.get('title') or 'conteúdo'}{hora}", "portal", 3)

    for p in dados.get("producao") or []:
        prazo = p.get("prazo") or ""
        if prazo and prazo < HOJE and p.get("status") not in ("aprovada", "publicada"):
            add("producao", f"producao:{p.get('id')}:{prazo}",
                f"Produção atrasada: {p.get('titulo') or 'peça'}, prazo {ddmm(prazo)}", "producao", 2)

    for a in dados.get("aprovacoes") or []:
        if a.get("decisao") == "pendente" and str(a.get("solicitadoEm") or "")[:10] <= HA_2_DIAS:
            rot = (a.get("objeto") or {}).get("rotulo") or "item"
            add("aprovacao", f"aprov:{a.get('id')}", f"Esperando sua decisão há mais de 2 dias: {rot}", "aprovacoes", 2)
    return out


def avisos_do_motor():
    """Peças do motor esperando aprovação há mais de 2 dias (valem para a equipe)."""
    out = []
    pecas = ler("artifacts", {"select": "id,type,created_at", "status": "eq.AWAITING_APPROVAL",
                              "is_current": "eq.true"}, opcional=True)
    for p in pecas:
        if str(p.get("created_at") or "")[:10] <= HA_2_DIAS:
            out.append({"tipo": "aprovacao", "chave": f"peca:{p['id']}", "abrir": "aprovacoes", "peso": 2,
                        "texto": f"{ROT_PECA.get(p.get('type'), 'Peça do motor')} esperando aprovação"})
    return out


# ── Envio ────────────────────────────────────────────────────────────────────
def enviar(inscricao, payload):
    """True = entregue. Aparelho que saiu (404/410) é apagado da lista."""
    try:
        webpush(subscription_info={"endpoint": inscricao["endpoint"],
                                   "keys": {"p256dh": inscricao["p256dh"], "auth": inscricao["auth"]}},
                data=json.dumps(payload), vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT}, ttl=12 * 3600)
        atualizar("push_inscricoes", {"id": f"eq.{inscricao['id']}"}, {"ultimo_envio": AGORA.isoformat(), "falhas": 0})
        return True
    except WebPushException as e:
        cod = getattr(e.response, "status_code", None)
        if cod in (404, 410):
            apagar("push_inscricoes", {"id": f"eq.{inscricao['id']}"})
            print(f"  aparelho saiu (HTTP {cod}) — removido")
        else:
            atualizar("push_inscricoes", {"id": f"eq.{inscricao['id']}"}, {"falhas": (inscricao.get("falhas") or 0) + 1})
            print(f"  falhou (HTTP {cod}): {str(e)[:160]}")
        return False


def resumo_semana(espacos):
    etapas = [s for d in espacos.values() for f in (d.get("funnels") or []) if not f.get("isTemplate")
              for s in (f.get("stages") or [])]
    feitas = sum(1 for s in etapas if s.get("status") == "done")
    atrasadas = sum(1 for s in etapas if s.get("dataFim") and s["dataFim"] < HOJE and s.get("status") != "done")
    fim_semana = (AGORA.date() + dt.timedelta(days=6)).isoformat()
    conteudos = sum(1 for d in espacos.values() for e in (d.get("events") or []) if HOJE <= str(e.get("date") or "") <= fim_semana)
    pendentes = sum(1 for d in espacos.values() for a in (d.get("aprovacoes") or []) if a.get("decisao") == "pendente")
    numeros = (f"Funil: {feitas} de {len(etapas)} etapas concluídas, {atrasadas} atrasadas. "
               f"{conteudos} conteúdos nos próximos 7 dias. {pendentes} aprovações pendentes.")
    texto = numeros
    if os.environ.get("GROQ_API_KEY"):
        try:
            from llm import groq
            resp = groq("Você escreve avisos curtos para a equipe da Wheff, em português do Brasil, tom sereno e direto, "
                        "sem exagero e sem inventar nada além dos números dados. No máximo 2 frases. "
                        'Responda só em JSON: {"resumo": "..."}',
                        f"Escreva o resumo da semana a partir destes números: {numeros}", max_tokens=160)
            frase = str((resp or {}).get("resumo") or "").strip()
            if frase:
                texto = frase[:240]
        except Exception as e:  # sem IA, vai só com os números
            print(f"  resumo sem IA ({str(e)[:80]})")
    return {"titulo": "Wheff · semana", "corpo": texto, "abrir": "painel", "tag": "wheff-semana"}


def pessoas_da_lista():
    """
    Todos da lista de acesso (papeis) que já entraram pelo menos uma vez:
    e-mail -> id do login. Fase 7E: a caixa de avisos vale para todos, com
    ou sem celular ativado. Sem a lista de usuários (API de administração),
    volta ao jeito antigo: só quem ativou o celular.
    """
    try:
        r = requests.get(f"{URL}/auth/v1/admin/users", headers=H, params={"per_page": 1000}, timeout=60)
        r.raise_for_status()
        usuarios = {str(u.get("email") or "").lower(): u["id"] for u in (r.json().get("users") or []) if u.get("email")}
    except Exception as e:  # noqa: BLE001
        print(f"  não li a lista de usuários ({str(e)[:100]}) — só quem ativou o celular")
        return None
    return usuarios


def gravar_caixa(email, avisos, push, quando=None):
    """Guarda na caixa de avisos (sql/avisos.sql). Mesma chave não repete."""
    if not avisos or not email:
        return True
    linhas = [{"email": email, "chave": a["chave"], "tipo": a.get("tipo", ""), "texto": a["texto"],
               "abrir": a.get("abrir", ""), "push": push, "push_em": quando} for a in avisos]
    r = requests.post(f"{URL}/rest/v1/avisos?on_conflict=email,chave", data=json.dumps(linhas), timeout=60,
                      headers={**H, "Prefer": "resolution=ignore-duplicates"})
    if r.status_code in (400, 404):
        print("  caixa de avisos ainda não existe (rode sql/avisos.sql) — seguindo só com o celular")
        return False
    r.raise_for_status()
    return True


def main():
    inscricoes = ler("push_inscricoes")
    print(f"{len(inscricoes)} aparelho(s) ativado(s)")

    if TESTE:
        if not inscricoes:
            print("Nenhum aparelho ativado — o teste é só de celular.")
        for i in inscricoes:
            ok = enviar(i, {"titulo": "Wheff · teste", "corpo": "As notificações estão funcionando. Toque para abrir.",
                            "abrir": "notificacoes", "tag": "wheff-teste"})
            print(f"  teste → {i.get('email')} ({i.get('dispositivo')}): {'ok' if ok else 'falhou'}")
        return

    espacos = {p["marca_id"]: (p.get("dados") or {}) for p in ler("plataforma", {"select": "marca_id,dados"})}
    papeis = {str(p["email"]).lower(): p for p in ler("papeis", opcional=True)}
    perfis = {p["id"]: p for p in ler("perfis", {"select": "id,area"}, opcional=True)}
    por_espaco = {mid: avisos_do_espaco(d) for mid, d in espacos.items()}
    do_motor = avisos_do_motor()

    # Limpa o registro de push com mais de 60 dias (não volta a avisar o que já passou)
    apagar("push_avisos", {"enviado_em": f"lt.{(AGORA - dt.timedelta(days=60)).isoformat()}"})

    aparelhos_de = {}
    for i in inscricoes:
        aparelhos_de.setdefault(str(i.get("email") or "").lower(), []).append(i)

    # Quem recebe: todos da lista (caixa interna) — ou, sem a lista de
    # usuários, só quem tem aparelho (comportamento anterior)
    usuarios = pessoas_da_lista()
    if usuarios is None:
        destinatarios = {e: aps[0]["user_id"] for e, aps in aparelhos_de.items() if e}
    else:
        destinatarios = {e: usuarios[e] for e in papeis if e in usuarios}
        for e, aps in aparelhos_de.items():          # quem tem aparelho e não está na lista (antigo)
            destinatarios.setdefault(e, aps[0]["user_id"])
    caixa_ok = True

    for email, uid in destinatarios.items():
        papel = papeis.get(email, {})
        aparelhos = aparelhos_de.get(email, [])
        if papel.get("papel") == "produtora":
            avisos = [a for a in por_espaco.get(papel.get("marca_id"), []) if a["tipo"] == "grava"]
        else:
            funcao = DA_AREA.get((perfis.get(uid) or {}).get("area"), "coprodutora")
            tipos = POR_FUNCAO.get(funcao, TODOS)
            avisos = [a for lista in por_espaco.values() for a in lista if a["tipo"] in tipos] + \
                     [a for a in do_motor if a["tipo"] in tipos]

        ja = {x["chave"] for x in ler("push_avisos", {"select": "chave", "user_id": f"eq.{uid}"})}
        if caixa_ok:
            ja |= {x["chave"] for x in ler("avisos", {"select": "chave", "email": f"eq.{email}"}, opcional=True)}
        novos, vistos = [], set()
        for a in sorted(avisos, key=lambda a: -a["peso"]):
            if a["chave"] not in ja and a["chave"] not in vistos:
                novos.append(a)
                vistos.add(a["chave"])

        if novos:
            status, quando = "sem_aparelho", None
            if aparelhos:
                linhas = [a["texto"] for a in novos[:3]] + ([f"e mais {len(novos) - 3}"] if len(novos) > 3 else [])
                payload = {"titulo": "Wheff · hoje", "corpo": "\n".join(linhas), "abrir": novos[0]["abrir"], "tag": "wheff-diario"}
                entregues = sum(enviar(i, payload) for i in aparelhos)
                status, quando = ("enviado" if entregues else "falhou"), AGORA.isoformat()
                if entregues:
                    inserir("push_avisos", [{"user_id": uid, "chave": a["chave"]} for a in novos])
            if caixa_ok:
                caixa_ok = gravar_caixa(email, novos, status, quando)
            print(f"{email}: {len(novos)} aviso(s) novo(s) — celular: {status}")
        else:
            print(f"{email}: nada novo hoje")

        # Segunda-feira: resumo da semana para a equipe (uma vez por semana)
        if AGORA.weekday() == 0 and papel.get("papel") != "produtora" and f"semana:{SEMANA}" not in ja:
            resumo = resumo_semana(espacos)
            status, quando = "sem_aparelho", None
            if aparelhos:
                ok = sum(enviar(i, resumo) for i in aparelhos)
                status, quando = ("enviado" if ok else "falhou"), AGORA.isoformat()
                if ok:
                    inserir("push_avisos", [{"user_id": uid, "chave": f"semana:{SEMANA}"}])
            if caixa_ok:
                caixa_ok = gravar_caixa(email, [{"chave": f"semana:{SEMANA}", "tipo": "semana", "texto": resumo["corpo"],
                                                  "abrir": resumo["abrir"]}], status, quando)
            print(f"  resumo da semana — celular: {status}")


if __name__ == "__main__":
    main()
