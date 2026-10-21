"""
Sincroniza os conectores configurados (Fase 6). Roda no GitHub Actions
(workflow "Conectores"), uma vez por dia.

Só roda o conector que tiver os segredos no GitHub. Sem nenhum, o workflow
nem chega aqui. Uma falha num conector não para os outros. Cada um grava
o estado em conectores_estado (sql/conectores.sql):
  sincronizando → ativo | parcial | acesso_expirado | erro
"ativo" só depois de uma coleta real sem erro.

Janela: do checkpoint menos 3 dias (conversões atrasadas mudam dias
recentes) até ontem; na primeira vez, os últimos 30 dias.
"""
import datetime as dt
import json
import os
import sys

import requests

import wheff
from conectores import adaptadores as A
from conectores.base import AcessoExpirado, ErroConector, deduplicar, redigir

BRT = dt.timezone(dt.timedelta(hours=-3))
ONTEM = (dt.datetime.now(BRT).date() - dt.timedelta(days=1))
E = os.environ.get
lista = lambda v: [x.strip() for x in str(v or "").split(",") if x.strip()]  # noqa: E731

PROXIMO = {
    "acesso_expirado": "Gerar um token novo e atualizar o segredo no GitHub (CONECTORES.md).",
    "erro": "Ver o erro abaixo; conferir permissões e a versão da API na documentação oficial.",
    "parcial": "Parte das contas falhou: conferir as contas listadas no erro.",
    "ativo": "Nada a fazer.",
}


def rest(metodo, tabela, params=None, corpo=None, prefer=None):
    h = dict(wheff.H)
    if prefer:
        h["Prefer"] = prefer
    r = requests.request(metodo, f"{wheff.URL}/rest/v1/{tabela}", headers=h, params=params or {},
                         data=json.dumps(corpo) if corpo is not None else None, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"{tabela}: {r.status_code} {r.text[:300]}")
    return r


def estado(cid, **campos):
    campos.update({"id": cid, "atualizado_em": dt.datetime.now(dt.timezone.utc).isoformat()})
    rest("POST", "conectores_estado", params={"on_conflict": "id"}, corpo=[campos],
         prefer="resolution=merge-duplicates")


def checkpoint(cid):
    r = rest("GET", "conectores_estado", params={"id": f"eq.{cid}", "select": "checkpoint"}).json()
    c = r[0]["checkpoint"] if r and r[0].get("checkpoint") else None
    inicio = (dt.date.fromisoformat(c) - dt.timedelta(days=3)) if c else ONTEM - dt.timedelta(days=29)
    return min(inicio, ONTEM).isoformat(), ONTEM.isoformat()


def gravar(res, cid):
    linhas = deduplicar(res.linhas)
    for i in range(0, len(linhas), 500):
        rest("POST", "metricas_canal", corpo=linhas[i:i + 500],
             params={"on_conflict": "plataforma,conta,nivel,entidade_id,data,recorte"},
             prefer="resolution=merge-duplicates")
    brutos = [{"conector": cid, "pedido": b["pedido"], "corpo": json.loads(redigir(json.dumps(b["corpo"]))[:200000] or "{}")}
              for b in res.brutos[:50]]
    if brutos:
        try:
            rest("POST", "conector_bruto", corpo=brutos)
        except Exception as e:  # noqa: BLE001 — auditoria não derruba a coleta
            print(f"  (bruto não guardado: {str(e)[:120]})")
    return len(linhas)


def rodar(cid, versao, coletar):
    desde, ate = checkpoint(cid)
    agora = dt.datetime.now(dt.timezone.utc).isoformat()
    estado(cid, estado="sincronizando", ultima_tentativa=agora, versao_api=versao)
    try:
        res = coletar(desde, ate)
    except AcessoExpirado as e:
        estado(cid, estado="acesso_expirado", erro=redigir(str(e))[:1000], proximo_passo=PROXIMO["acesso_expirado"])
        print(f"{cid}: acesso expirado"); return False
    except Exception as e:  # noqa: BLE001
        estado(cid, estado="erro", erro=redigir(f"{type(e).__name__}: {e}")[:1000], proximo_passo=PROXIMO["erro"])
        print(f"{cid}: erro {redigir(str(e))[:200]}"); return False
    n = gravar(res, cid) if res.linhas else 0
    st = res.estado
    campos = {"estado": st, "linhas": n, "erro": redigir(" | ".join(res.erros))[:1000] or None,
              "proximo_passo": PROXIMO.get(st, "")}
    if st in ("ativo", "parcial"):
        campos.update({"ultima_sinc_ok": agora, "checkpoint": ate})
    estado(cid, **campos)
    print(f"{cid}: {st}, {n} linha(s) de {desde} a {ate}")
    return st in ("ativo", "parcial")


def main():
    feitos = []
    if E("META_ADS_TOKEN") and E("META_ADS_CONTAS"):
        feitos.append(rodar("meta_ads", A.META_VERSAO, lambda d, a: A.meta_ads(
            E("META_ADS_TOKEN"), lista(E("META_ADS_CONTAS")), d, a, conversao=E("META_CONVERSAO") or "purchase")))
    google = None
    if E("GOOGLE_OAUTH_REFRESH_TOKEN") and E("GOOGLE_OAUTH_CLIENT_ID"):
        try:
            google = A.google_token(E("GOOGLE_OAUTH_CLIENT_ID"), E("GOOGLE_OAUTH_CLIENT_SECRET"), E("GOOGLE_OAUTH_REFRESH_TOKEN"))
        except (AcessoExpirado, ErroConector) as e:
            for cid in ("google_ads", "ga4"):
                estado(cid, estado="acesso_expirado", erro=redigir(str(e))[:500], proximo_passo=PROXIMO["acesso_expirado"])
    if google and E("GOOGLE_ADS_DEVELOPER_TOKEN") and E("GOOGLE_ADS_CUSTOMER_IDS"):
        feitos.append(rodar("google_ads", A.GOOGLE_ADS_VERSAO, lambda d, a: A.google_ads(
            google, E("GOOGLE_ADS_DEVELOPER_TOKEN"), lista(E("GOOGLE_ADS_CUSTOMER_IDS")), d, a, E("GOOGLE_ADS_LOGIN_CUSTOMER_ID"))))
    if google and E("GA4_PROPERTY_ID"):
        feitos.append(rodar("ga4", A.GA4_VERSAO, lambda d, a: A.ga4(google, E("GA4_PROPERTY_ID"), d, a)))
    if E("TIKTOK_ADS_TOKEN") and E("TIKTOK_ADVERTISER_IDS"):
        feitos.append(rodar("tiktok_ads", A.TIKTOK_VERSAO, lambda d, a: A.tiktok_ads(
            E("TIKTOK_ADS_TOKEN"), lista(E("TIKTOK_ADVERTISER_IDS")), d, a)))
    if not feitos:
        print("Nenhum conector com segredo configurado.")
        return 0
    # Auditoria: respostas brutas guardadas por 30 dias
    limite = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
    try:
        rest("DELETE", "conector_bruto", params={"coletado_em": f"lt.{limite}"})
    except Exception:  # noqa: BLE001
        pass
    return 0 if all(feitos) else 1


if __name__ == "__main__":
    sys.exit(main())
