"""
Fase 6: contrato dos conectores com respostas simuladas (sem rede, sem conta).
Cobre: token vencido, 429, resposta parcial, paginação, métrica ausente,
duplicidade, recorte do YouTube sem dupla contagem, eventos de venda e
segredo fora do registro.

Rodar:  python tests/test_conectores.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worker"))
from conectores import adaptadores as A  # noqa: E402
from conectores.base import AcessoExpirado, Cliente, deduplicar, redigir  # noqa: E402


class R:
    def __init__(self, corpo, status=200, headers=None):
        self.status_code, self._c, self.headers, self.text = status, corpo, headers or {}, ""

    def json(self):
        return self._c


class Sessao:
    """Responde pela ordem, filtrando por um pedaço da URL."""
    def __init__(self, roteiro):
        self.roteiro, self.pedidos = roteiro, []

    def request(self, metodo, url, **kw):
        self.pedidos.append((metodo, url, kw))
        for i, (trecho, resp) in enumerate(self.roteiro):
            if trecho in url:
                return self.roteiro.pop(i)[1]
        raise AssertionError("pedido inesperado: " + url)


sem_espera = lambda s: None  # noqa: E731


class TestBase(unittest.TestCase):
    def test_429_tenta_de_novo_respeitando_retry_after(self):
        esperas = []
        s = Sessao([("x", R({}, 429, {"Retry-After": "3"})), ("x", R({"ok": 1}))])
        c = Cliente("t", s, dormir=esperas.append)
        self.assertEqual(c.pedir("GET", "https://api/x"), {"ok": 1})
        self.assertEqual(esperas, [3.0])

    def test_segredo_nao_vai_para_o_registro(self):
        self.assertNotIn("SEGREDO", redigir("https://g/x?access_token=SEGREDO&a=1"))
        self.assertNotIn("SEGREDO", redigir("Authorization: Bearer SEGREDO"))
        s = Sessao([("me", R({"ok": 1}))])
        c = Cliente("t", s)
        c.pedir("GET", "https://graph/me?access_token=SEGREDO")
        self.assertNotIn("SEGREDO", str(c.log))

    def test_duplicidade(self):
        l1 = {"plataforma": "p", "conta": "c", "nivel": "n", "entidade_id": "1", "data": "2026-10-01", "recorte": "", "gasto": 1}
        l2 = dict(l1, gasto=2)
        self.assertEqual([x["gasto"] for x in deduplicar([l1, l2])], [2])


class TestMeta(unittest.TestCase):
    def test_paginacao_e_metrica_ausente(self):
        p1 = {"data": [{"campaign_id": "1", "campaign_name": "A", "date_start": "2026-10-01", "spend": "10.5",
                        "impressions": "100", "clicks": "5", "actions": [{"action_type": "purchase", "value": "2"}]}],
              "paging": {"next": "https://graph.facebook.com/v25.0/act_9/insights?after=X&access_token=T"}}
        p2 = {"data": [{"campaign_id": "1", "campaign_name": "A", "date_start": "2026-10-02", "spend": "3",
                        "impressions": "40", "clicks": "1"}]}
        s = Sessao([("act_9/insights?after", R(p2)), ("act_9/insights", R(p1))])
        r = A.meta_ads("T", ["9"], "2026-10-01", "2026-10-02", sessao=s, dormir=sem_espera)
        self.assertEqual(r.estado, "ativo")
        self.assertEqual([l["gasto"] for l in r.linhas], [10.5, 3.0])
        self.assertEqual(r.linhas[0]["conversoes"], 2.0)
        self.assertIsNone(r.linhas[1]["conversoes"], "conversão ausente não vira zero")
        self.assertIn("conversoes", r.linhas[1]["faltando"])
        self.assertIn("alcance", r.linhas[0]["faltando"])

    def test_token_vencido(self):
        s = Sessao([("insights", R({"error": {"code": 190, "message": "Error validating access token"}}, 400))])
        r = A.meta_ads("T", ["9", "10"], "2026-10-01", "2026-10-02", sessao=s, dormir=sem_espera)
        self.assertEqual(r.estado, "acesso_expirado")
        self.assertEqual(len(s.pedidos), 1, "não insiste com token vencido")

    def test_parcial_uma_conta_falha(self):
        ok = {"data": [{"campaign_id": "1", "date_start": "2026-10-01", "spend": "1", "impressions": "1", "clicks": "1", "reach": "1"}]}
        s = Sessao([("act_1/", R(ok)), ("act_2/", R({"error": {"code": 100, "message": "Invalid parameter"}}, 400))])
        r = A.meta_ads("T", ["1", "2"], "2026-10-01", "2026-10-01", sessao=s, dormir=sem_espera)
        self.assertEqual(r.estado, "parcial")
        self.assertEqual(len(r.linhas), 1)
        self.assertTrue(any("conta 2" in e for e in r.erros))

    def test_limite_da_meta_com_http_400(self):
        s = Sessao([("insights", R({"error": {"code": 17, "message": "User request limit reached"}}, 400)),
                    ("insights", R({"data": []}))])
        r = A.meta_ads("T", ["9"], "2026-10-01", "2026-10-01", sessao=s, dormir=sem_espera)
        self.assertEqual(r.estado, "ativo")


class TestGoogleAds(unittest.TestCase):
    def test_youtube_e_recorte_nao_soma_duas_vezes(self):
        pag1 = {"results": [
            {"campaign": {"id": "7", "name": "Vídeo"}, "segments": {"date": "2026-10-01", "adNetworkType": "YOUTUBE"},
             "metrics": {"costMicros": "4000000", "impressions": "100", "clicks": "2"}, "customer": {"currencyCode": "BRL"}},
            {"campaign": {"id": "7", "name": "Vídeo"}, "segments": {"date": "2026-10-01", "adNetworkType": "SEARCH"},
             "metrics": {"costMicros": "1000000", "impressions": "50", "clicks": "3"}}], "nextPageToken": "P2"}
        pag2 = {"results": [{"campaign": {"id": "8", "name": "Busca"}, "segments": {"date": "2026-10-01", "adNetworkType": "SEARCH"},
                             "metrics": {"costMicros": "2500000", "impressions": "10", "clicks": "1"}}]}
        s = Sessao([("googleAds:search", R(pag1)), ("googleAds:search", R(pag2))])
        r = A.google_ads("AT", "DEV", ["123-456-7890"], "2026-10-01", "2026-10-01", sessao=s, dormir=sem_espera)
        total = sum(l["gasto"] for l in r.linhas if not l["recorte"])
        yt = [l for l in r.linhas if l["recorte"] == "youtube"]
        self.assertAlmostEqual(total, 7.5)                 # 4 + 1 + 2.5, sem contar o YouTube de novo
        self.assertEqual(len(yt), 1); self.assertAlmostEqual(yt[0]["gasto"], 4.0)
        self.assertEqual(s.pedidos[1][2]["json"].get("pageToken"), "P2")   # paginou
        self.assertIn("v25", s.pedidos[0][1])

    def test_refresh_token_revogado(self):
        s = Sessao([("oauth2", R({"error": "invalid_grant"}, 400))])
        with self.assertRaises(AcessoExpirado):
            A.google_token("id", "secret", "rt", sessao=s)


class TestGA4(unittest.TestCase):
    def test_paginacao_por_offset_e_metrica_ausente(self):
        cab = [{"name": "sessions"}, {"name": "totalUsers"}, {"name": "keyEvents"}, {"name": "purchaseRevenue"}]
        lin = lambda d, origem: {"dimensionValues": [{"value": d}, {"value": origem}],  # noqa: E731
                                 "metricValues": [{"value": "10"}, {"value": "8"}, {"value": ""}, {"value": "0"}]}
        p1 = {"metricHeaders": cab, "rows": [lin("20261001", "google / organic")], "rowCount": 2}
        p2 = {"metricHeaders": cab, "rows": [lin("20261002", "instagram / social")], "rowCount": 2}
        s = Sessao([(":runReport", R(p1)), (":runReport", R(p2))])
        r = A.ga4("AT", "123", "2026-10-01", "2026-10-02", sessao=s, dormir=sem_espera, limite_linhas=1)
        self.assertEqual([l["data"] for l in r.linhas], ["2026-10-01", "2026-10-02"])
        self.assertEqual(s.pedidos[1][2]["json"]["offset"], 1)
        self.assertIsNone(r.linhas[0]["conversoes"])        # keyEvents vazio = ausente
        self.assertEqual(r.linhas[0]["receita"], 0.0)       # zero informado continua zero


class TestTikTok(unittest.TestCase):
    def test_paginas_e_token_invalido(self):
        p = lambda n, tot: {"code": 0, "data": {"page_info": {"page": n, "total_page": tot}, "list": [  # noqa: E731
            {"dimensions": {"campaign_id": f"c{n}", "stat_time_day": "2026-10-01 00:00:00"},
             "metrics": {"campaign_name": "X", "spend": "1.5", "impressions": "9", "clicks": "1"}}]}}
        s = Sessao([("integrated", R(p(1, 2))), ("integrated", R(p(2, 2)))])
        r = A.tiktok_ads("T", ["adv"], "2026-10-01", "2026-10-01", sessao=s, dormir=sem_espera)
        self.assertEqual(len(r.linhas), 2); self.assertEqual(r.linhas[0]["data"], "2026-10-01")
        s2 = Sessao([("integrated", R({"code": 40105, "message": "Access token is invalid"}))])
        self.assertEqual(A.tiktok_ads("T", ["adv"], "a", "b", sessao=s2, dormir=sem_espera).estado, "acesso_expirado")


class TestVendas(unittest.TestCase):
    def test_hotmart(self):
        e = A.evento_hotmart({"id": "evt1", "event": "PURCHASE_REFUNDED", "creation_date": 1792000000000,
                              "data": {"product": {"name": "Curso"}, "purchase": {"transaction": "HP1", "price": {"value": 497.0, "currency_value": "BRL"}},
                                       "buyer": {"email": "a@b.com", "name": "Fulana"}}})
        self.assertEqual((e["idExterno"], e["tipo"], e["valorBruto"], e["idEvento"]), ("HP1", "reembolsado", 497.0, "evt1"))
        self.assertNotIn("@", str(e)); self.assertNotIn("Fulana", str(e))
        self.assertIsNone(A.evento_hotmart({"event": "CLUB_FIRST_ACCESS", "data": {}}))

    def test_kiwify_centavos(self):
        e = A.evento_kiwify({"order_id": "K1", "order_status": "paid", "approved_date": "2026-10-10 10:00",
                             "Commissions": {"charge_amount": 49700, "kiwify_fee": 4970}, "Product": {"product_name": "Curso"}})
        self.assertEqual((e["tipo"], e["valorBruto"], e["taxa"], e["em"]), ("aprovado", 497.0, 49.7, "2026-10-10T10:00"))


class TestSincronizador(unittest.TestCase):
    def test_falha_num_conector_nao_para_os_outros(self):
        from unittest import mock
        os.environ.update({"SUPABASE_URL": "https://x", "SUPABASE_SERVICE_KEY": "k"})
        import conectores_sync as S
        from conectores.base import Resultado, linha
        chamadas = []

        class Resp:
            status_code, text = 200, ""
            def json(self): return []

        def rest(metodo, tabela, params=None, corpo=None, prefer=None):
            chamadas.append((metodo, tabela, corpo))
            return Resp()

        def meta(*a, **k):
            r = Resultado()
            r.linhas = [linha("meta_ads", "1", "campanha", "c", "C", "2026-10-01", "v25.0", gasto=1.0)] * 2   # duplicada
            return r

        def tiktok(*a, **k):
            raise RuntimeError("quebrou")

        env = {"META_ADS_TOKEN": "T", "META_ADS_CONTAS": "1", "TIKTOK_ADS_TOKEN": "X", "TIKTOK_ADVERTISER_IDS": "9"}
        with mock.patch.dict(os.environ, env), mock.patch.object(S, "rest", rest), \
             mock.patch.object(S.A, "meta_ads", meta), mock.patch.object(S.A, "tiktok_ads", tiktok):
            codigo = S.main()
        estados = [(c[2][0]["id"], c[2][0]["estado"]) for c in chamadas if c[1] == "conectores_estado" and c[0] == "POST"]
        self.assertIn(("meta_ads", "ativo"), estados)
        self.assertIn(("tiktok_ads", "erro"), estados)
        gravadas = [c[2] for c in chamadas if c[1] == "metricas_canal"]
        self.assertEqual(len(gravadas[0]), 1, "linha duplicada gravada uma vez")
        self.assertEqual(codigo, 1, "a execução sinaliza que um conector falhou")
        self.assertNotIn("sincronizando", [e for c, e in estados if c == "meta_ads"][-1:])


if __name__ == "__main__":
    unittest.main(verbosity=1)
