"""
Adaptadores de leitura (Fase 6). Implementados e testados com respostas
simuladas; NENHUM foi validado com conta real (não há contas ainda).
Versões conferidas na documentação oficial em 18/10/2026 (CONECTORES.md).

Cada um devolve um Resultado: linhas normalizadas (base.linha), erros por
conta/página e os corpos brutos para auditoria. Falha numa conta não apaga
as outras: o resultado fica "parcial".
"""
import json
import os

from .base import AcessoExpirado, Cliente, ErroConector, Resultado, linha, num

META_VERSAO = os.environ.get("META_API_VERSION", "v25.0")          # Graph/Marketing: v25.0 vigente
GOOGLE_ADS_VERSAO = os.environ.get("GOOGLE_ADS_API_VERSION", "v25")  # suportada até ago/2027
GA4_VERSAO = "v1beta"
TIKTOK_VERSAO = os.environ.get("TIKTOK_ADS_API_VERSION", "v1.3")


def _bruto(res, conector, pedido, corpo):
    res.brutos.append({"conector": conector, "pedido": pedido, "corpo": corpo})


# ── Meta Ads (API de Marketing, insights por campanha e dia) ─────────────────
META_AUTH = {190, 102, 10, 200}          # token inválido/expirado, sem permissão
META_LIMITE = {4, 17, 32, 613, 80000, 80004}


def _meta_erro(corpo):
    return (corpo or {}).get("error") or {}


def meta_ads(token, contas, desde, ate, sessao=None, dormir=None, conversao="purchase"):
    cli = Cliente("meta_ads", sessao, dormir=dormir or __import__("time").sleep,
                  auth_expirado=lambda st, c: _meta_erro(c).get("code") in META_AUTH,
                  limite=lambda st, c: _meta_erro(c).get("code") in META_LIMITE)
    res = Resultado()
    campos = "campaign_id,campaign_name,spend,impressions,clicks,reach,actions,action_values,account_currency"
    for conta in contas:
        url = f"https://graph.facebook.com/{META_VERSAO}/act_{conta}/insights"
        params = {"level": "campaign", "fields": campos, "time_increment": 1, "limit": 500,
                  "time_range": json.dumps({"since": desde, "until": ate}), "access_token": token}
        try:
            while url:
                corpo = cli.pedir("GET", url, params=params)
                _bruto(res, "meta_ads", {"conta": conta, "desde": desde, "ate": ate}, corpo)
                for d in corpo.get("data") or []:
                    acoes = {a.get("action_type"): num(a.get("value")) for a in d.get("actions") or []}
                    valores = {a.get("action_type"): num(a.get("value")) for a in d.get("action_values") or []}
                    res.linhas.append(linha(
                        "meta_ads", conta, "campanha", d.get("campaign_id"), d.get("campaign_name"),
                        d.get("date_start"), META_VERSAO, moeda=d.get("account_currency"),
                        esperados=("gasto", "impressoes", "cliques", "alcance", "conversoes"),
                        gasto=num(d.get("spend")), impressoes=num(d.get("impressions")),
                        cliques=num(d.get("clicks")), alcance=num(d.get("reach")),
                        # Conversão só do tipo configurado; sem ele na resposta, fica ausente (não zero)
                        conversoes=acoes.get(conversao), receita=valores.get(conversao)))
                url = ((corpo.get("paging") or {}).get("next"))
                params = None          # a URL "next" já traz tudo
        except AcessoExpirado as e:
            res.acesso_expirado = True; res.erros.append(str(e)); break
        except ErroConector as e:
            res.erros.append(f"conta {conta}: {e}")
    return res


# ── Google (OAuth com refresh token) ─────────────────────────────────────────
def google_token(client_id, client_secret, refresh_token, sessao=None):
    cli = Cliente("google_oauth", sessao, tentativas=3,
                  auth_expirado=lambda st, c: st in (400, 401) and (c or {}).get("error") in ("invalid_grant", "unauthorized_client", "invalid_client"))
    corpo = cli.pedir("POST", "https://oauth2.googleapis.com/token",
                      data={"client_id": client_id, "client_secret": client_secret,
                            "refresh_token": refresh_token, "grant_type": "refresh_token"})
    if not corpo.get("access_token"):
        raise AcessoExpirado("google: o refresh token não devolveu acesso")
    return corpo["access_token"]


def _google_auth(st, c):
    err = (c[0] if isinstance(c, list) and c else c or {}).get("error") or {}
    return st == 401 or err.get("status") in ("UNAUTHENTICATED", "PERMISSION_DENIED")


YOUTUBE = {"YOUTUBE", "YOUTUBE_SEARCH", "YOUTUBE_WATCH"}


def google_ads(access_token, developer_token, clientes, desde, ate, login_customer_id=None, sessao=None, dormir=None):
    """
    Campanhas por dia, segmentadas por rede. Gera:
      • a linha da campanha no dia (soma de todas as redes) — é o TOTAL;
      • a linha "recorte=youtube" com só o que rodou no YouTube — é um
        RECORTE do total: somar as duas contaria a mesma despesa duas vezes.
    """
    cli = Cliente("google_ads", sessao, dormir=dormir or __import__("time").sleep, auth_expirado=_google_auth)
    res = Resultado()
    gaql = ("SELECT customer.currency_code, campaign.id, campaign.name, segments.date, segments.ad_network_type, "
            "metrics.cost_micros, metrics.impressions, metrics.clicks, metrics.conversions, metrics.conversions_value "
            f"FROM campaign WHERE segments.date BETWEEN '{desde}' AND '{ate}'")
    h = {"Authorization": f"Bearer {access_token}", "developer-token": developer_token}
    if login_customer_id:
        h["login-customer-id"] = str(login_customer_id).replace("-", "")
    for cid in clientes:
        cid = str(cid).replace("-", "")
        url = f"https://googleads.googleapis.com/{GOOGLE_ADS_VERSAO}/customers/{cid}/googleAds:search"
        soma = {}
        try:
            pagina = None
            while True:
                corpo = cli.pedir("POST", url, headers=h, json={"query": gaql, **({"pageToken": pagina} if pagina else {})})
                _bruto(res, "google_ads", {"cliente": cid, "desde": desde, "ate": ate, "pagina": pagina}, corpo)
                for r in corpo.get("results") or []:
                    camp, seg, m = r.get("campaign") or {}, r.get("segments") or {}, r.get("metrics") or {}
                    rede = seg.get("adNetworkType") or ""
                    for recorte in ([""] + (["youtube"] if rede in YOUTUBE else [])):
                        k = (camp.get("id"), seg.get("date"), recorte)
                        s = soma.setdefault(k, {"nome": camp.get("name"), "moeda": (r.get("customer") or {}).get("currencyCode"),
                                                "gasto": None, "impressoes": None, "cliques": None, "conversoes": None, "receita": None})
                        for campo, v in (("gasto", (num(m.get("costMicros")) or 0) / 1e6 if m.get("costMicros") is not None else None),
                                         ("impressoes", num(m.get("impressions"))), ("cliques", num(m.get("clicks"))),
                                         ("conversoes", num(m.get("conversions"))), ("receita", num(m.get("conversionsValue")))):
                            if v is not None:
                                s[campo] = (s[campo] or 0) + v
                pagina = corpo.get("nextPageToken")
                if not pagina:
                    break
        except AcessoExpirado as e:
            res.acesso_expirado = True; res.erros.append(str(e)); break
        except ErroConector as e:
            res.erros.append(f"cliente {cid}: {e}")
        for (camp_id, data, recorte), s in soma.items():
            res.linhas.append(linha("google_ads", cid, "campanha", camp_id, s["nome"], data, GOOGLE_ADS_VERSAO,
                                    moeda=s["moeda"], recorte=recorte, esperados=("gasto", "impressoes", "cliques"),
                                    gasto=s["gasto"], impressoes=s["impressoes"], cliques=s["cliques"],
                                    conversoes=s["conversoes"], receita=s["receita"]))
    return res


# ── GA4 (Data API, relatório por dia e origem/mídia) ────────────────────────
def ga4(access_token, propriedade, desde, ate, sessao=None, dormir=None, limite_linhas=10000):
    cli = Cliente("ga4", sessao, dormir=dormir or __import__("time").sleep, auth_expirado=_google_auth)
    res = Resultado()
    url = f"https://analyticsdata.googleapis.com/{GA4_VERSAO}/properties/{propriedade}:runReport"
    metricas = ["sessions", "totalUsers", "keyEvents", "purchaseRevenue"]
    corpo_base = {"dateRanges": [{"startDate": desde, "endDate": ate}],
                  "dimensions": [{"name": "date"}, {"name": "sessionSourceMedium"}],
                  "metrics": [{"name": m} for m in metricas], "limit": limite_linhas}
    offset = 0
    try:
        while True:
            corpo = cli.pedir("POST", url, headers={"Authorization": f"Bearer {access_token}"},
                              json={**corpo_base, "offset": offset})
            _bruto(res, "ga4", {"propriedade": propriedade, "desde": desde, "ate": ate, "offset": offset}, corpo)
            nomes = [m.get("name") for m in corpo.get("metricHeaders") or []]
            linhas = corpo.get("rows") or []
            for r in linhas:
                dims = [d.get("value") for d in r.get("dimensionValues") or []]
                vals = dict(zip(nomes, [num(v.get("value")) for v in r.get("metricValues") or []]))
                d = dims[0] if dims else ""
                data = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
                res.linhas.append(linha("ga4", propriedade, "origem", dims[1] if len(dims) > 1 else "", dims[1] if len(dims) > 1 else "",
                                        data, GA4_VERSAO, esperados=("sessoes", "usuarios"),
                                        sessoes=vals.get("sessions"), usuarios=vals.get("totalUsers"),
                                        conversoes=vals.get("keyEvents"), receita=vals.get("purchaseRevenue")))
            offset += len(linhas)
            if not linhas or offset >= int(corpo.get("rowCount") or 0):
                break
    except AcessoExpirado as e:
        res.acesso_expirado = True; res.erros.append(str(e))
    except ErroConector as e:
        res.erros.append(str(e))
    return res


# ── TikTok Ads (Business API, relatório integrado) ───────────────────────────
# Códigos de erro do TikTok: conferir na documentação quando houver conta.
TIKTOK_AUTH = {40102, 40104, 40105}
TIKTOK_LIMITE = {40100}


def tiktok_ads(token, anunciantes, desde, ate, sessao=None, dormir=None):
    cli = Cliente("tiktok_ads", sessao, dormir=dormir or __import__("time").sleep,
                  auth_expirado=lambda st, c: st == 401 or (c or {}).get("code") in TIKTOK_AUTH,
                  limite=lambda st, c: (c or {}).get("code") in TIKTOK_LIMITE)
    res = Resultado()
    url = f"https://business-api.tiktok.com/open_api/{TIKTOK_VERSAO}/report/integrated/get/"
    for adv in anunciantes:
        pagina, total = 1, 1
        try:
            while pagina <= total:
                corpo = cli.pedir("GET", url, headers={"Access-Token": token}, params={
                    "advertiser_id": adv, "report_type": "BASIC", "data_level": "AUCTION_CAMPAIGN",
                    "dimensions": json.dumps(["campaign_id", "stat_time_day"]),
                    "metrics": json.dumps(["campaign_name", "spend", "impressions", "clicks", "reach", "conversion"]),
                    "start_date": desde, "end_date": ate, "page": pagina, "page_size": 1000})
                if (corpo.get("code") or 0) != 0:
                    raise ErroConector(f"tiktok_ads: código {corpo.get('code')}: {corpo.get('message')}")
                _bruto(res, "tiktok_ads", {"anunciante": adv, "pagina": pagina}, corpo)
                dados = corpo.get("data") or {}
                for it in dados.get("list") or []:
                    d, m = it.get("dimensions") or {}, it.get("metrics") or {}
                    res.linhas.append(linha("tiktok_ads", adv, "campanha", d.get("campaign_id"), m.get("campaign_name"),
                                            str(d.get("stat_time_day") or "")[:10], TIKTOK_VERSAO,
                                            esperados=("gasto", "impressoes", "cliques"),
                                            gasto=num(m.get("spend")), impressoes=num(m.get("impressions")),
                                            cliques=num(m.get("clicks")), alcance=num(m.get("reach")),
                                            conversoes=num(m.get("conversion"))))
                total = int((dados.get("page_info") or {}).get("total_page") or 1)
                pagina += 1
        except AcessoExpirado as e:
            res.acesso_expirado = True; res.erros.append(str(e)); break
        except ErroConector as e:
            res.erros.append(f"anunciante {adv}: {e}")
    return res


# ── Vendas: contrato de eventos (Hotmart / Kiwify) ───────────────────────────
# Mesmo formato que core/financas.js aplicarEvento recebe na plataforma:
# {plataforma, idExterno, tipo, em, valorBruto, taxa, idEvento, produto}.
# Nenhum dado do comprador sai daqui (nome, e-mail, documento).
HOTMART_TIPO = {"PURCHASE_APPROVED": "aprovado", "PURCHASE_COMPLETE": "aprovado", "PURCHASE_REFUNDED": "reembolsado",
                "PURCHASE_CHARGEBACK": "chargeback", "PURCHASE_CANCELED": "cancelado", "PURCHASE_PROTEST": "contestado"}
KIWIFY_TIPO = {"paid": "aprovado", "approved": "aprovado", "refunded": "reembolsado", "chargedback": "chargeback",
               "refused": "cancelado", "canceled": "cancelado"}


def _iso(v):
    from datetime import datetime, timezone
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc).isoformat()
    return str(v or "").replace(" ", "T")


def evento_hotmart(p):
    d = (p or {}).get("data") or {}
    compra, preco = d.get("purchase") or {}, (d.get("purchase") or {}).get("price") or {}
    tipo = HOTMART_TIPO.get(p.get("event"))
    if not tipo or not compra.get("transaction"):
        return None
    return {"plataforma": "hotmart", "idExterno": compra["transaction"], "tipo": tipo,
            "em": _iso(compra.get("approved_date") if tipo == "aprovado" and compra.get("approved_date") else p.get("creation_date")),
            "valorBruto": preco.get("value"), "moeda": preco.get("currency_value"),
            "idEvento": p.get("id") or f"{p.get('event')}:{p.get('creation_date')}", "produto": (d.get("product") or {}).get("name", "")}


def evento_kiwify(p):
    tipo = KIWIFY_TIPO.get(str((p or {}).get("order_status") or "").lower())
    if not tipo or not p.get("order_id"):
        return None
    com = p.get("Commissions") or {}
    centavos = lambda v: (num(v) / 100) if num(v) is not None else None  # noqa: E731
    return {"plataforma": "kiwify", "idExterno": p["order_id"], "tipo": tipo,
            "em": _iso(p.get("approved_date") if tipo == "aprovado" and p.get("approved_date") else p.get("updated_at") or p.get("created_at")),
            "valorBruto": centavos(com.get("charge_amount")), "taxa": centavos(com.get("kiwify_fee")),
            "idEvento": f"{p['order_id']}:{p.get('order_status')}:{p.get('updated_at') or p.get('created_at')}",
            "produto": (p.get("Product") or {}).get("product_name", "")}
