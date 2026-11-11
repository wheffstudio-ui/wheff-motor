"""
VIGIA (11/11/2026) — olha, uma vez por dia, as páginas que a equipe pediu
(site, landing page, perfil público, biblioteca de anúncios de um concorrente)
e avisa só quando o conteúdo mudou.

Os endereços ficam na plataforma: Integrações → Automações → Vigia
(dados.settings.vigia.alvos). A leitura usa o Jina Reader (r.jina.ai), gratuito
e sem chave. Números (seguidores, curtidas, preços em contagem) são ignorados
na comparação: só muda de verdade quando o texto muda.

O estado de cada endereço fica em public.vigia_estado (sql/vigia.sql da
plataforma). Sem a tabela, o Vigia não roda e diz por quê.
"""
import datetime as dt
import hashlib
import re

import requests

LIMITE_ALVOS = 20          # por espaço, por dia
TIMEOUT = 40


def normalizar(texto):
    linhas = []
    for l in str(texto or "").splitlines():
        l = re.sub(r"\d+([.,]\d+)*", "#", l)        # números não contam como mudança
        l = re.sub(r"\s+", " ", l).strip()
        if len(l) >= 3:
            linhas.append(l)
    return linhas


def impressao(linhas):
    return hashlib.sha256("\n".join(linhas).encode("utf-8")).hexdigest()


def novidades(antes, depois, n=3):
    vistos = set(antes or [])
    return [l for l in depois if l not in vistos][:n]


def ler_pagina(url, get=requests.get):
    r = get("https://r.jina.ai/" + url, headers={"Accept": "text/plain", "X-Return-Format": "text"}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"leitura {r.status_code}")
    return r.text


def alvos_do_espaco(dados):
    v = ((dados.get("settings") or {}).get("vigia") or {})
    out = []
    for a in v.get("alvos") or []:
        url = str((a or {}).get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            out.append({"url": url, "nome": (a.get("nome") or url)[:80]})
    return out[:LIMITE_ALVOS]


def vigiar(marca_id, dados, estado, gravar, ler=ler_pagina, hoje=None):
    """estado: {url: linha de vigia_estado}. gravar(linha) faz o upsert.
    Devolve avisos no formato de notificacoes.avisos_do_espaco."""
    hoje = hoje or dt.date.today().isoformat()
    avisos = []
    for a in alvos_do_espaco(dados):
        url = a["url"]
        antes = estado.get(url) or {}
        try:
            linhas = normalizar(ler(url))
        except Exception as e:  # noqa: BLE001 — um endereço com erro não para os outros
            gravar({"marca_id": marca_id, "url": url, "nome": a["nome"], "erro": str(e)[:200],
                    "conferido_em": dt.datetime.now(dt.timezone.utc).isoformat()})
            continue
        h = impressao(linhas)
        linha = {"marca_id": marca_id, "url": url, "nome": a["nome"], "hash": h, "linhas": linhas[:400], "erro": None,
                 "conferido_em": dt.datetime.now(dt.timezone.utc).isoformat()}
        if antes.get("hash") and antes["hash"] != h:
            novo = novidades(antes.get("linhas"), linhas)
            linha["mudou_em"] = linha["conferido_em"]
            linha["novidades"] = novo
            trecho = f": {novo[0][:90]}" if novo else ""
            avisos.append({"tipo": "vigia", "chave": f"vigia:{url}:{h[:12]}",
                           "texto": f"Mudou {a['nome']}{trecho}", "abrir": "integracoes", "peso": 2})
        gravar(linha)
    return avisos
