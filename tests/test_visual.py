"""Medidas visuais (video_visual.py) com um vídeo sintético gerado pelo ffmpeg.

Três blocos de cor sólida em 720x1280 (vertical): a cena muda aos 1 s e aos
3 s. Sem pessoa nem dado real. Tesseract e OpenCV são opcionais: sem eles a
medida correspondente fica None e o motivo vai para as limitações.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))
import video_visual as vv  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg ausente")
class TestMedir(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pasta = tempfile.mkdtemp()
        cls.arq = os.path.join(cls.pasta, "t.mp4")
        cmd = ["ffmpeg", "-v", "error", "-y",
               "-f", "lavfi", "-i", "color=c=red:s=720x1280:d=1:r=25",
               "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=2:r=25",
               "-f", "lavfi", "-i", "color=c=green:s=720x1280:d=2:r=25",
               "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0", "-pix_fmt", "yuv420p", cls.arq]
        subprocess.run(cmd, check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.pasta, ignore_errors=True)

    def test_mede_formato_duracao_e_cortes(self):
        m = vv.medir(self.arq)
        self.assertEqual(m["formato"], "vertical")
        self.assertAlmostEqual(m["duracao"], 5, delta=0.3)
        self.assertEqual(m["n_cortes"], 2)
        self.assertAlmostEqual(m["primeiro_corte"], 1.0, delta=0.1)
        self.assertGreater(m["quadros_lidos"], 3)
        self.assertTrue(any("não interpretação" in x for x in m["limitacoes"]))

    def test_resumo_em_faixas(self):
        r = vv.resumo_para_dna(vv.medir(self.arq))
        self.assertEqual(r["duracao_faixa"], "até 15 s")
        self.assertEqual(r["primeiro_corte_faixa"], "até 2 s")
        self.assertEqual(r["formato"], "vertical")


class TestFaixas(unittest.TestCase):
    def test_faixas(self):
        self.assertEqual(vv.formato(1080, 1920), "vertical")
        self.assertEqual(vv.formato(1920, 1080), "horizontal")
        self.assertEqual(vv.formato(1080, 1080), "quadrado")
        self.assertEqual(vv.faixa_primeiro_corte(None), "sem corte")
        self.assertEqual(vv.faixa_primeiro_corte(4), "2 a 5 s")
        self.assertTrue(vv.faixa_ritmo(6).startswith("acelerado"))
        self.assertEqual(vv.faixa_duracao(45), "30 a 60 s")
        self.assertEqual(vv.palavras_lidas("ab ~~ Oferta 50% hoje!"), ["Oferta", "hoje"])
        self.assertIsNone(vv.resumo_para_dna(None))


if __name__ == "__main__":
    unittest.main()
