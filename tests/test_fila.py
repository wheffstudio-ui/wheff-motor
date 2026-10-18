"""
Fase 7: a fila do motor, sem rede. Simula o banco (as chamadas HTTP do
wheff.py) e confere o comportamento:
  • erro volta para a fila COM espera, e para no limite de tentativas;
  • erro definitivo (download indisponível) não tenta de novo;
  • reprocessar uma mídia reaproveita VIDEO e TRANSCRIPT, sem duplicar.

Rodar:  python tests/test_fila.py
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(AQUI, "..", "worker"))
os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "chave-de-teste")

import wheff  # noqa: E402


class Resp:
    def __init__(self, dados=None, status=200):
        self.status_code, self._d = status, dados
        self.text = "" if dados is None else "x"

    def json(self):
        return self._d


class BancoFalso:
    """Guarda o job e os eventos; responde como o PostgREST."""
    def __init__(self, attempts, max_attempts=3):
        self.job = {"attempts": attempts, "max_attempts": max_attempts}
        self.patches, self.eventos = [], []

    def get(self, url, **kw):
        return Resp([dict(self.job)])

    def patch(self, url, json=None, **kw):
        self.patches.append(json)
        self.job.update(json)
        return Resp([], 204)

    def post(self, url, json=None, **kw):
        if "/events" in url:
            self.eventos.append(json["event_type"])
        return Resp([], 201)


def com_banco(banco):
    return mock.patch.multiple(wheff.requests, get=banco.get, patch=banco.patch, post=banco.post)


class TestTerminarJob(unittest.TestCase):
    def test_sucesso(self):
        b = BancoFalso(1)
        with com_banco(b):
            st = wheff.terminar_job("wheff", "j1")
        self.assertEqual(st, "COMPLETED")
        self.assertIn("job.completed", b.eventos)

    def test_erro_volta_com_espera(self):
        b = BancoFalso(1)
        with com_banco(b):
            st = wheff.terminar_job("wheff", "j1", erro="falhou")
        self.assertEqual(st, "QUEUED")
        disp = datetime.fromisoformat(b.patches[-1]["available_at"])
        minutos = (disp - datetime.now(timezone.utc)).total_seconds() / 60
        self.assertTrue(1.5 < minutos <= 2.1, minutos)          # 1ª falha: 2 min
        self.assertIn("job.retry", b.eventos)

    def test_espera_cresce(self):
        self.assertEqual([wheff.espera_minutos(n) for n in (1, 2, 3, 4)], [2, 4, 8, 16])
        self.assertEqual(wheff.espera_minutos(20), 360)        # teto de 6 h

    def test_limite_de_tentativas(self):
        b = BancoFalso(3, 3)
        with com_banco(b):
            st = wheff.terminar_job("wheff", "j1", erro="falhou de novo")
        self.assertEqual(st, "FAILED")
        self.assertNotIn("available_at", b.patches[-1])
        self.assertIn("job.failed", b.eventos)

    def test_definitivo_nao_tenta_de_novo(self):
        b = BancoFalso(1, 3)
        with com_banco(b):
            st = wheff.terminar_job("wheff", "j1", erro="[download indisponível] privado", definitivo=True)
        self.assertEqual(st, "FAILED")


class TestReprocessarMidia(unittest.TestCase):
    def test_nao_duplica(self):
        import media_ingest as mi
        criados, enfileirados = [], []
        video = {"id": "v1", "version": 1, "artifact_key": "VIDEO-0007"}
        transcript = {"id": "t1", "version": 1, "artifact_key": "TRANSCRIPT-0007",
                      "data": {"idioma": "pt", "trechos": [{"texto": "oi"}], "duracao": 3}}

        def baixar(url, pasta):
            arq = os.path.join(pasta, "midia.m4a")
            with open(arq, "wb") as f:
                f.write(b"mesmo arquivo")
            return {"title": "x"}, arq

        derivados = {("v1", "transcript"): [transcript], ("t1", "translation"): [{"id": "tr1"}]}
        with mock.patch.object(mi, "baixar", baixar), \
             mock.patch.object(mi.wheff, "buscar_por_hash", lambda o, t, h: video), \
             mock.patch.object(mi.wheff, "derivados_de", lambda i, t: derivados.get((i, t), [])), \
             mock.patch.object(mi.wheff, "criar_artefato", lambda *a, **k: criados.append(a) or {}), \
             mock.patch.object(mi.wheff, "ligar", lambda *a, **k: None), \
             mock.patch.object(mi.wheff, "etapa", lambda *a, **k: None), \
             mock.patch.object(mi.wheff, "enfileirar", lambda *a, **k: enfileirados.append(k.get("idempotency_key"))), \
             mock.patch.object(mi, "transcrever", lambda c: self.fail("não devia transcrever de novo")):
            mi.executar({"id": "j1", "payload": {"url": "https://x/reel/1", "source_artifact_id": "s1"}})
        self.assertEqual(criados, [], "reprocessamento criou peça nova")
        self.assertEqual(enfileirados, ["dna:v1"])            # mesma chave: a fila não duplica

    def test_download_indisponivel_e_definitivo(self):
        import media_ingest as mi
        falha = mock.Mock(returncode=1, stderr="ERROR: [youtube] abc: Private video. Sign in if you've been granted access")
        with mock.patch.object(mi.subprocess, "run", lambda *a, **k: falha), tempfile.TemporaryDirectory() as p:
            with self.assertRaises(mi.DownloadIndisponivel) as ctx:
                mi.baixar("https://youtu.be/abc", p)
        self.assertTrue(str(ctx.exception).startswith(mi.MARCA_INDISPONIVEL))


class TestTranscricaoManual(unittest.TestCase):
    def test_sem_tempos_nao_inventa_segundos(self):
        os.environ.setdefault("GROQ_API_KEY", "teste")
        import dna_analyze as d
        manual = [{"id": 0, "inicio": None, "fim": None, "texto": "Oi gente"},
                  {"id": 1, "inicio": None, "fim": None, "texto": "hoje vou mostrar"}]
        texto, corte = d.texto_compacto(manual)
        self.assertEqual(texto, "[trecho 0] Oi gente\n[trecho 1] hoje vou mostrar")
        self.assertNotIn("s–", texto)
        self.assertTrue(d.sem_tempos(manual))
        com_tempo = [{"id": 0, "inicio": 0.0, "fim": 2.1, "texto": "Oi"}]
        self.assertFalse(d.sem_tempos(com_tempo))
        self.assertIn("0.0s–2.1s", d.texto_compacto(com_tempo)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
