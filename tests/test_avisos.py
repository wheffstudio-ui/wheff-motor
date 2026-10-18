"""
Fase 7E: caixa de avisos. Simula o banco e o Web Push e confere:
  • todos da lista recebem na caixa, com ou sem celular;
  • quem tem celular: push registrado como enviado (ou falhou);
  • quem não tem: "sem_aparelho";
  • cada um só o que é da função; a parceira, só o que grava;
  • rodar de novo não repete.

Rodar:  python tests/test_avisos.py
"""
import datetime as dt
import json
import os
import sys
import unittest
from unittest import mock
from urllib.parse import urlparse, parse_qs

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(AQUI, "..", "worker"))
for k, v in {"SUPABASE_URL": "https://exemplo.supabase.co", "SUPABASE_SERVICE_KEY": "k", "VAPID_PRIVATE_KEY": "v"}.items():
    os.environ.setdefault(k, v)
class _WebPushException(Exception):
    def __init__(self, msg="", response=None):
        super().__init__(msg)
        self.response = response


sys.modules.setdefault("pywebpush", mock.Mock(WebPushException=_WebPushException, webpush=lambda **k: None))

import notificacoes as N  # noqa: E402


class R:
    def __init__(self, dados=None, status=200):
        self.status_code, self._d = status, dados
        self.text = json.dumps(dados)

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Banco:
    def __init__(self, push_falha=False):
        velho = (N.AGORA.date() - dt.timedelta(days=5)).isoformat()
        self.t = {
            "push_inscricoes": [{"id": 1, "user_id": "u-adm", "email": "adm@x.com", "endpoint": "e", "p256dh": "p", "auth": "a"}],
            "plataforma": [{"marca_id": "wheff", "dados": {"funnels": [{"name": "Funil", "stages": [
                {"id": "s1", "name": "Captação", "dataFim": velho, "status": "doing"}]}]}},
                           {"marca_id": "produtora", "dados": {}}],
            "papeis": [{"email": "adm@x.com", "papel": "admin"}, {"email": "equipe@x.com", "papel": "equipe"},
                       {"email": "prod@x.com", "papel": "produtora", "marca_id": "produtora"}],
            "perfis": [{"id": "u-adm", "area": "Coprodução / TI"}, {"id": "u-eq", "area": "Edição de Vídeos"}],
            "artifacts": [{"id": "a1", "type": "content_dna", "created_at": velho + "T10:00:00Z"}],
            "push_avisos": [], "avisos": [],
        }
        self.envios = []
        self.push_falha = push_falha

    def get(self, url, params=None, **kw):
        if "/auth/v1/admin/users" in url:
            return R({"users": [{"id": "u-adm", "email": "adm@x.com"}, {"id": "u-eq", "email": "equipe@x.com"},
                                {"id": "u-prod", "email": "prod@x.com"}]})
        tab = urlparse(url).path.rsplit("/", 1)[-1]
        linhas = list(self.t.get(tab, []))
        for k, v in (params or {}).items():
            if k in ("select", "per_page") or not str(v).startswith("eq."):
                continue
            linhas = [x for x in linhas if str(x.get(k)) == v[3:]]
        if tab == "artifacts":
            linhas = self.t["artifacts"]
        return R(linhas)

    def post(self, url, data=None, **kw):
        tab = urlparse(url).path.rsplit("/", 1)[-1]
        for linha in json.loads(data):
            chave = (linha.get("email"), linha.get("chave")) if tab == "avisos" else (linha.get("user_id"), linha.get("chave"))
            existentes = {(x.get("email"), x.get("chave")) if tab == "avisos" else (x.get("user_id"), x.get("chave")) for x in self.t[tab]}
            if chave not in existentes:
                self.t[tab].append(linha)
        return R([], 201)

    def patch(self, *a, **k):
        return R([], 204)

    def delete(self, *a, **k):
        return R([], 204)

    def webpush(self, **k):
        if self.push_falha:
            raise N.WebPushException("falhou", response=mock.Mock(status_code=500))
        self.envios.append(json.loads(k["data"]))


def rodar(b):
    with mock.patch.multiple(N.requests, get=b.get, post=b.post, patch=b.patch, delete=b.delete), \
         mock.patch.object(N, "webpush", b.webpush), mock.patch.object(N, "AGORA", N.AGORA.replace(hour=8)):
        N.main()


class TestCaixa(unittest.TestCase):
    def test_todos_recebem_com_ou_sem_celular(self):
        b = Banco()
        rodar(b)
        por = {}
        for a in b.t["avisos"]:
            por.setdefault(a["email"], []).append(a)
        # adm (coprodução): atraso + aprovação, com celular -> enviado
        self.assertEqual({a["tipo"] for a in por["adm@x.com"]}, {"atraso", "aprovacao"})
        self.assertTrue(all(a["push"] == "enviado" and a["push_em"] for a in por["adm@x.com"]))
        # equipe (edição): só aprovação, sem celular -> sem_aparelho
        self.assertEqual([a["tipo"] for a in por["equipe@x.com"]], ["aprovacao"])
        self.assertEqual(por["equipe@x.com"][0]["push"], "sem_aparelho")
        # parceira: não recebe atraso nem aprovação
        self.assertNotIn("prod@x.com", por)
        self.assertEqual(len(b.envios), 1)        # uma notificação, só para quem tem aparelho

    def test_nao_repete(self):
        b = Banco()
        rodar(b)
        antes = len(b.t["avisos"])
        rodar(b)
        self.assertEqual(len(b.t["avisos"]), antes)
        self.assertEqual(len(b.envios), 1)        # segunda passagem não manda de novo

    def test_push_que_falha_fica_registrado(self):
        b = Banco(push_falha=True)
        rodar(b)
        adm = [a for a in b.t["avisos"] if a["email"] == "adm@x.com"]
        self.assertTrue(adm and all(a["push"] == "falhou" for a in adm))


if __name__ == "__main__":
    unittest.main(verbosity=2)
