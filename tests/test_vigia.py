"""Vigia: só avisa quando o texto muda; números não contam. python tests/test_vigia.py"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worker"))
import vigia

DADOS = {"settings": {"vigia": {"alvos": [{"url": "https://ex.com/lp", "nome": "LP do concorrente"}, {"url": "ftp://x"}]}}}


class TesteVigia(unittest.TestCase):
    def rodar(self, estado, texto):
        gravado = []
        av = vigia.vigiar("m1", DADOS, estado, gravado.append, ler=lambda u: texto)
        return av, gravado

    def test_primeira_leitura_nao_avisa(self):
        av, g = self.rodar({}, "Oferta\nCurso completo")
        self.assertEqual(av, []); self.assertEqual(len(g), 1)   # ftp ignorado

    def test_numero_nao_e_mudanca(self):
        _, g = self.rodar({}, "Seguidores 1.200\nCurso")
        av, _ = self.rodar({"https://ex.com/lp": g[0]}, "Seguidores 1.350\nCurso")
        self.assertEqual(av, [])

    def test_texto_novo_avisa(self):
        _, g = self.rodar({}, "Oferta\nCurso completo")
        av, g2 = self.rodar({"https://ex.com/lp": g[0]}, "Oferta\nCurso completo\nBônus novo: mentoria")
        self.assertEqual(len(av), 1)
        self.assertIn("Bônus novo", av[0]["texto"]); self.assertIn("LP do concorrente", av[0]["texto"])
        self.assertTrue(g2[0].get("mudou_em"))

    def test_erro_de_leitura_nao_para(self):
        g = []
        def falha(u): raise RuntimeError("leitura 451")
        av = vigia.vigiar("m1", DADOS, {}, g.append, ler=falha)
        self.assertEqual(av, []); self.assertIn("451", g[0]["erro"])


if __name__ == "__main__":
    unittest.main()
