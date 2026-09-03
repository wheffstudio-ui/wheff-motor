#!/usr/bin/env python3
"""
COLETA DE PESQUISA — YouTube e Reddit

Duas fontes, dois trabalhos diferentes:

YouTube e para PUBLICO. Comentario em video de nicho brasileiro e onde a
pessoa fala do proprio jeito — e materia-prima da etapa 11 do playbook, que
exige a frase exata do cliente, nao a parafrase.

Reddit e para MERCADO. Influenciadora brasileira nao esta la, entao nao
serve para entender o publico daqui. Serve para outra coisa: ver o que ja
roda la fora e ainda nao chegou no Brasil, e que angulo ninguem explorou.
Por isso todo item do Reddit nasce com destino market_research e o pais
declarado — frase traduzida entende mercado, nunca vira copy.

O que este worker NAO faz: julgar. Ele marca `linguagem` no que veio do
YouTube e `referencia_externa` no que veio de fora, e para por ai. Decidir
se um comentario e dor, desejo ou objecao e leitura, e leitura sem contexto
e chute. Quem marca e uma pessoa, na tela de Fontes.
"""
import os
import re
import sys
import traceback
from datetime import datetime, timezone

import requests
import wheff

ORG = os.environ.get("WHEFF_ORG", "wheff")
WORKER = "research_collect@actions"

MAX_ITENS = 2000
MAX_CHARS_ITEM = 6000
# Comentario de uma palavra ("top", "kkkk", um emoji solto) nao sustenta
# nenhuma afirmacao. Guardar isso so infla o numero e faz a amostra parecer
# maior do que e.
MIN_CHARS_ITEM = 15


def _corta(texto, limite=MAX_CHARS_ITEM):
    return texto if len(texto) <= limite else texto[:limite].rstrip() + "…"


def _util(texto):
    """Descarta o que nao e frase. Devolve o texto limpo ou None."""
    t = (texto or "").strip()
    if len(t) < MIN_CHARS_ITEM:
        return None
    # So emoji, so pontuacao, ou so risada
    if not re.search(r"[a-zA-ZÀ-ÿ]{3}", t):
        return None
    if re.fullmatch(r"(k|h|a|e|s|r|j){4,}[\s!?.]*", t, re.I):
        return None
    return t


# ── YouTube ────────────────────────────────────────────────────────────────
def coletar_youtube(url, limite):
    import yt_dlp

    opts = {
        "skip_download": True,
        "getcomments": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                # max_comments[, max_parents, max_replies, max_replies_por_thread]
                "max_comments": [str(limite), "all", str(limite), "10"],
                # 'top' traz o que teve concordancia; e um recorte enviesado
                # para o comentario performatico, e isso fica registrado em
                # criterio_recorte para poder ser descontado depois.
                "comment_sort": ["top"],
            }
        },
    }
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)

    brutos = info.get("comments") or []
    if not brutos:
        raise RuntimeError(
            "nenhum comentario voltou. Ou o video tem comentarios desativados, "
            "ou o YouTube bloqueou o runner. Nesse caso, colar na tela de "
            "Fontes resolve sem depender de robo.")

    itens, descartados = [], 0
    for c in brutos:
        t = _util(c.get("text"))
        if not t:
            descartados += 1
            continue
        if len(itens) >= MAX_ITENS:
            break
        it = {
            "id": f"i{len(itens) + 1:03d}",
            "texto": _corta(t),
            "verbatim": True,
            # Comentario e a voz do publico por definicao. Os outros
            # marcadores exigem leitura, e leitura e trabalho de pessoa.
            "marcadores": ["linguagem"],
        }
        if c.get("author"):
            it["autor"] = str(c["author"])[:120]
        if c.get("timestamp"):
            try:
                it["publicado_em"] = datetime.fromtimestamp(
                    c["timestamp"], timezone.utc).date().isoformat()
            except Exception:
                pass
        m = {}
        if isinstance(c.get("like_count"), int):
            m["curtidas"] = c["like_count"]
        if c.get("parent") and c["parent"] != "root":
            m["e_resposta"] = True
        if m:
            it["metricas"] = m
        itens.append(it)

    origem = {
        "plataforma": "youtube",
        "url": url,
        "titulo_original": (info.get("title") or "")[:400],
        "autor_conteudo": (info.get("uploader") or "")[:200],
        "idioma": "pt-BR",
        "pais": "Brasil",
    }
    if info.get("upload_date"):
        d = info["upload_date"]
        origem["publicado_em"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"

    coleta = {
        "modo": "automatica",
        "ferramenta": "yt-dlp",
        "coletado_em": datetime.now(timezone.utc).date().isoformat(),
        "coletado_por": WORKER,
        "total_disponivel": info.get("comment_count"),
        "criterio_recorte": f"os {limite} mais curtidos (comment_sort=top)",
        "descartados": descartados,
    }
    return origem, coleta, itens, ["audience_profile"], 1


# ── Reddit ─────────────────────────────────────────────────────────────────
def coletar_reddit(p, limite):
    import praw

    faltando = [k for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")
                if not os.environ.get(k)]
    if faltando:
        raise RuntimeError(
            "faltam os segredos " + ", ".join(faltando) + ". Crie um app do tipo "
            "'script' em reddit.com/prefs/apps (e gratuito) e guarde as duas "
            "chaves em Settings > Secrets do repositorio.")

    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ.get("REDDIT_USER_AGENT", "wheff-pesquisa/1.0"),
        check_for_async=False,
    )
    reddit.read_only = True

    sub = p.get("subreddit") or "all"
    termo = p.get("query")
    periodo = p.get("periodo", "year")

    alvo = reddit.subreddit(sub)
    posts = (alvo.search(termo, sort="relevance", time_filter=periodo, limit=limite)
             if termo else alvo.top(time_filter=periodo, limit=limite))

    itens, descartados, n_posts = [], 0, 0
    por_post = max(3, limite // 10)

    for post in posts:
        n_posts += 1
        corpo = _util(post.selftext) or _util(post.title)
        if corpo and len(itens) < MAX_ITENS:
            itens.append({
                "id": f"i{len(itens) + 1:03d}",
                "texto": _corta(f"{post.title}\n\n{post.selftext}".strip()),
                "verbatim": True,
                "traduzido_de": "en",
                "url": f"https://reddit.com{post.permalink}",
                "publicado_em": datetime.fromtimestamp(
                    post.created_utc, timezone.utc).date().isoformat(),
                "metricas": {"curtidas": int(post.score),
                             "respostas": int(post.num_comments)},
                # Post de fora e referencia de mercado ate alguem provar o
                # contrario. Nao e dor do publico brasileiro.
                "marcadores": ["referencia_externa"],
            })

        try:
            post.comments.replace_more(limit=0)
            for c in post.comments[:por_post]:
                t = _util(getattr(c, "body", ""))
                if not t:
                    descartados += 1
                    continue
                if len(itens) >= MAX_ITENS:
                    break
                itens.append({
                    "id": f"i{len(itens) + 1:03d}",
                    "texto": _corta(t),
                    "verbatim": True,
                    "traduzido_de": "en",
                    "url": f"https://reddit.com{post.permalink}",
                    "metricas": {"curtidas": int(getattr(c, "score", 0) or 0)},
                    "marcadores": ["referencia_externa"],
                })
        except Exception as e:
            # Um post com comentario inacessivel nao derruba a coleta inteira.
            print(f"    aviso: comentarios de {post.id} indisponiveis ({e})")

    if not itens:
        raise RuntimeError(
            f"a busca em r/{sub} nao devolveu nada aproveitavel. "
            f"Tente outro termo, outro subreddit, ou periodo maior.")

    origem = {
        "plataforma": "reddit",
        "url": f"https://reddit.com/r/{sub}" + (f"/search?q={termo}" if termo else ""),
        "titulo_original": f"r/{sub}" + (f" — \"{termo}\"" if termo else " — top"),
        "idioma": p.get("idioma", "en"),
        "pais": p.get("pais", "Estados Unidos"),
    }
    coleta = {
        "modo": "automatica",
        "ferramenta": "praw",
        "coletado_em": datetime.now(timezone.utc).date().isoformat(),
        "coletado_por": WORKER,
        "criterio_recorte": (f"busca por \"{termo}\" ordenada por relevancia, periodo {periodo}"
                             if termo else f"top do periodo {periodo}"),
        "descartados": descartados,
    }
    return origem, coleta, itens, ["market_research"], n_posts


# ── Montagem comum ─────────────────────────────────────────────────────────
def montar_dados(titulo, origem, coleta, itens, destino, n_fontes):
    de_fora = not re.search(r"brasil", origem.get("pais", ""), re.I)

    limitacoes = []
    if len(itens) < 30:
        limitacoes.append(f"Amostra pequena: {len(itens)} itens. "
                          "Serve para levantar hipotese, nao para concluir.")
    if n_fontes < 3:
        limitacoes.append(f"Vem de {n_fontes} fonte(s) distinta(s) — reflete a audiencia de "
                          "quem publicou, nao o publico.")
    if "mais curtidos" in (coleta.get("criterio_recorte") or ""):
        limitacoes.append("Recorte por popularidade enviesa para o comentario performatico, "
                          "nao para o sincero.")
    if de_fora:
        limitacoes.append("Fonte de fora do Brasil: serve para entender mercado, nunca como "
                          "linguagem do publico brasileiro.")
    limitacoes.append("O robo so marcou a origem. Dor, desejo e objecao exigem leitura — "
                      "ate alguem marcar na tela de Fontes, isto nao alimenta essas etapas.")

    etapas = sorted({e for it in itens for m in it.get("marcadores", [])
                     for e in {"linguagem": [10, 11, 12]}.get(m, [])})

    return {
        "titulo": titulo,
        "origem": origem,
        "coleta": coleta,
        "itens": itens,
        "destino": destino,
        "etapas_playbook": etapas,
        "representatividade": {
            "n_itens": len(itens),
            "n_fontes_distintas": n_fontes,
            "serve_para_conclusao": len(itens) >= 30 and n_fontes >= 3,
            "vieses_conhecidos": (
                ["Fonte de fora do Brasil."] if de_fora else []
            ) + ([f"Recorte: {coleta['criterio_recorte']}."]
                 if coleta.get("criterio_recorte") else []),
        },
        "limitacoes": limitacoes,
    }


# ── Job ────────────────────────────────────────────────────────────────────
def processar(job):
    tipo = job["job_type"]
    p = job.get("payload") or {}
    limite = int(p.get("limite") or 100)

    if tipo == "research.youtube":
        url = p.get("url")
        if not url:
            raise RuntimeError("job sem 'url' — nao sei de qual video pegar comentario")
        print(f"  YouTube: {url} (ate {limite} comentarios)")
        origem, coleta, itens, destino, n = coletar_youtube(url, limite)
        titulo = p.get("titulo") or f"Comentarios — {origem['titulo_original'][:120]}"
    elif tipo == "research.reddit":
        print(f"  Reddit: r/{p.get('subreddit')} q={p.get('query')!r} (ate {limite} posts)")
        origem, coleta, itens, destino, n = coletar_reddit(p, limite)
        titulo = p.get("titulo") or f"Reddit — {origem['titulo_original']}"
    else:
        raise RuntimeError(f"tipo de job desconhecido: {tipo}")

    if p.get("destino"):
        destino = p["destino"]

    dados = montar_dados(titulo[:200], origem, coleta, itens, destino, n)
    print(f"  {len(itens)} itens uteis, {coleta.get('descartados', 0)} descartados")

    art = wheff.criar_artefato(
        ORG, "research_source", "OBSERVED", "research-source:v1", dados,
        criado_por=WORKER, escopo="ORG", status="APPROVED",
        snapshot={"ferramenta": coleta["ferramenta"], "job_type": tipo,
                  "limite": limite, "min_chars_item": MIN_CHARS_ITEM},
    )
    print(f"  OK {art['artifact_key']} — {len(itens)} itens")
    return art


def main():
    job = wheff.pegar_job(ORG, WORKER, ["research.youtube", "research.reddit"])
    # Postgres devolve uma linha de NULLs quando nao ha job, e um dict de
    # NULLs e verdadeiro. Por isso a checagem e pelo id.
    if not job or not job.get("id"):
        print("nada na fila de pesquisa")
        return 0

    print(f"Tarefa {job['id']} ({job['job_type']}) — tentativa {job.get('attempts')}")
    try:
        art = processar(job)
        wheff.terminar_job(ORG, job["id"])
        wheff.registrar_evento(ORG, "job.completed", job_id=job["id"],
                               artifact_id=art["id"], actor_type="worker",
                               actor_id=WORKER)
        return 0
    except Exception as e:
        traceback.print_exc()
        wheff.terminar_job(ORG, job["id"], erro=f"{type(e).__name__}: {e}")
        print(f"ERRO: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
