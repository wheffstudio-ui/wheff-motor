"""
video.visual — mede o que se VÊ no vídeo, sem IA.

O Whisper só ouve. Este módulo olha os quadros do vídeo com ferramentas
abertas e gratuitas que rodam no processador comum do GitHub Actions:

  - ffmpeg / ffprobe (LGPL): duração, formato, cortes de cena e silêncios
  - Tesseract (Apache-2.0), via pytesseract: texto escrito na tela
  - OpenCV (Apache-2.0), classificador de rostos que já vem no pacote

Tudo aqui é MEDIDA, não interpretação: "o primeiro corte veio aos 1,4 s",
"há texto na tela no primeiro segundo", "aparece rosto em 80% dos quadros".
Nada entende a cena (cozinha, emoção, produto) — isso exigiria modelo de
visão com GPU. Quando uma ferramenta não está instalada, a medida dela fica
como None e o motivo vai para `limitacoes`; o resto continua valendo.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

LIMIAR_CENA = float(os.environ.get("VISUAL_LIMIAR_CENA", "0.32"))
MAX_QUADROS = 14          # quadros lidos para texto e rosto (custo fixo por vídeo)


def _rodar(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def sondar(caminho):
    """ffprobe: duração, largura, altura e se tem trilha de vídeo."""
    r = _rodar(["ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,width,height", "-of", "json", caminho], 60)
    if r.returncode != 0:
        return None
    j = json.loads(r.stdout or "{}")
    vid = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    dur = float((j.get("format") or {}).get("duration") or 0) or None
    if not vid:
        return {"tem_video": False, "duracao": dur}
    return {"tem_video": True, "duracao": dur,
            "largura": int(vid.get("width") or 0), "altura": int(vid.get("height") or 0)}


def formato(largura, altura):
    if not largura or not altura:
        return None
    r = largura / altura
    if r < 0.8:
        return "vertical"
    if r > 1.25:
        return "horizontal"
    return "quadrado"


def cortes(caminho):
    """Momentos (em segundos) em que a cena muda, pelo filtro de cena do ffmpeg."""
    r = _rodar(["ffmpeg", "-hide_banner", "-nostats", "-i", caminho, "-an",
                "-vf", f"select='gt(scene,{LIMIAR_CENA})',showinfo", "-f", "null", "-"], 600)
    return sorted({round(float(m), 2) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr or "")})


def silencios(caminho):
    """Trechos sem som (abaixo de -35 dB por 0,6 s ou mais)."""
    r = _rodar(["ffmpeg", "-hide_banner", "-nostats", "-i", caminho, "-vn",
                "-af", "silencedetect=noise=-35dB:d=0.6", "-f", "null", "-"], 600)
    ini = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", r.stderr or "")]
    fim = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", r.stderr or "")]
    return [(a, b) for a, b in zip(ini, fim)]


def instantes(duracao):
    """Onde olhar: o começo com lupa (0,5 s, 1 s, 2 s, 3 s) e o resto espalhado."""
    if not duracao:
        return [0.5, 1, 2, 3]
    base = [t for t in (0.5, 1.0, 2.0, 3.0) if t < duracao]
    resto = MAX_QUADROS - len(base)
    if duracao > 3.5 and resto > 0:
        passo = (duracao - 3.5) / resto
        base += [round(3.5 + passo * i, 2) for i in range(resto)]
    return base


def extrair_quadro(caminho, t, destino):
    r = _rodar(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", caminho,
                "-frames:v", "1", "-vf", "scale=720:-2", "-y", destino], 60)
    return r.returncode == 0 and os.path.exists(destino)


def _ocr():
    try:
        import pytesseract
        if not shutil.which("tesseract"):
            return None
        return pytesseract
    except ImportError:
        return None


def _rostos():
    try:
        import cv2
        arq = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        cls = cv2.CascadeClassifier(arq)
        return (cv2, cls) if not cls.empty() else None
    except Exception:
        return None


def palavras_lidas(texto):
    """Só palavras de verdade: o OCR devolve lixo em textura e fundo."""
    return [p for p in re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", texto or "")]


def faixa_duracao(d):
    if d is None:
        return None
    return "até 15 s" if d <= 15 else "15 a 30 s" if d <= 30 else "30 a 60 s" if d <= 60 else "mais de 60 s"


def faixa_primeiro_corte(t):
    if t is None:
        return "sem corte"
    return "até 2 s" if t <= 2 else "2 a 5 s" if t <= 5 else "depois de 5 s"


def faixa_ritmo(por10):
    if por10 is None:
        return None
    return "parado (até 1 corte a cada 10 s)" if por10 <= 1 else \
           "moderado (2 a 4 cortes a cada 10 s)" if por10 <= 4 else "acelerado (5+ cortes a cada 10 s)"


def medir(caminho):
    """Mede o vídeo e devolve o dicionário gravado no artefato VIDEO-VISUAL."""
    info = sondar(caminho)
    lim = []
    if not info or not info.get("tem_video"):
        return None
    dur = info.get("duracao")
    cs = [c for c in cortes(caminho) if c > 0.15]
    por10 = round(len(cs) / dur * 10, 1) if dur else None

    ocr, face = _ocr(), _rostos()
    if not ocr:
        lim.append("Texto na tela não medido: Tesseract não instalado nesta execução.")
    if not face:
        lim.append("Rosto não medido: OpenCV não instalado nesta execução.")

    quadros = []
    with tempfile.TemporaryDirectory() as pasta:
        for i, t in enumerate(instantes(dur)):
            arq = os.path.join(pasta, f"q{i}.png")
            if not extrair_quadro(caminho, t, arq):
                continue
            q = {"t": t}
            if ocr:
                try:
                    pal = palavras_lidas(ocr.image_to_string(arq, lang="por+eng", config="--psm 11"))
                except Exception:
                    pal = palavras_lidas(ocr.image_to_string(arq, config="--psm 11"))
                q["texto"] = len(pal) >= 2
                q["palavras"] = pal[:12]
            if face:
                cv2, cls = face
                img = cv2.imread(arq, cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    achados = cls.detectMultiScale(img, scaleFactor=1.15, minNeighbors=6,
                                                   minSize=(max(40, img.shape[1] // 12),) * 2)
                    q["rosto"] = len(achados) > 0
            quadros.append(q)

    inicio = [q for q in quadros if q["t"] <= 1.0]
    tres = [q for q in quadros if q["t"] <= 3.0]

    def prop(lista, campo):
        v = [q[campo] for q in lista if campo in q]
        return round(sum(v) / len(v), 2) if v else None

    texto_inicio = any(q.get("texto") for q in inicio) if ocr and inicio else None
    rosto_inicio = any(q.get("rosto") for q in tres) if face and tres else None
    sil = silencios(caminho)
    silencio_total = round(sum(b - a for a, b in sil), 1)

    return {
        "duracao": dur,
        "formato": formato(info.get("largura"), info.get("altura")),
        "resolucao": f'{info.get("largura")}x{info.get("altura")}',
        "cortes": cs[:300],
        "n_cortes": len(cs),
        "primeiro_corte": cs[0] if cs else None,
        "cortes_por_10s": por10,
        "texto_na_tela_inicio": texto_inicio,
        "texto_na_tela_proporcao": prop(quadros, "texto") if ocr else None,
        "palavras_no_inicio": sorted({p for q in inicio for p in q.get("palavras", [])})[:20],
        "rosto_nos_3s": rosto_inicio,
        "rosto_proporcao": prop(quadros, "rosto") if face else None,
        "silencio_total_s": silencio_total,
        "silencio_no_inicio": bool(sil and sil[0][0] <= 0.3),
        "quadros_lidos": len(quadros),
        "ferramentas": {"cortes": "ffmpeg scene", "texto": "tesseract" if ocr else None,
                        "rosto": "opencv haar" if face else None},
        "limitacoes": lim + ["Medidas, não interpretação: nada aqui entende a cena, a emoção ou o produto."],
    }


def resumo_para_dna(v):
    """O que vai para o DNA: as medidas em faixas legíveis, prontas para o cruzamento."""
    if not v:
        return None
    sim = lambda x: None if x is None else ("sim" if x else "não")
    return {
        "formato": v.get("formato"),
        "duracao_faixa": faixa_duracao(v.get("duracao")),
        "primeiro_corte_faixa": faixa_primeiro_corte(v.get("primeiro_corte")),
        "ritmo": faixa_ritmo(v.get("cortes_por_10s")),
        "texto_na_tela_inicio": sim(v.get("texto_na_tela_inicio")),
        "legenda_na_maior_parte": sim(None if v.get("texto_na_tela_proporcao") is None
                                      else v["texto_na_tela_proporcao"] >= 0.5),
        "rosto_nos_3s": sim(v.get("rosto_nos_3s")),
        "medidas": {k: v.get(k) for k in ("duracao", "n_cortes", "primeiro_corte", "cortes_por_10s",
                                          "texto_na_tela_proporcao", "rosto_proporcao", "silencio_total_s",
                                          "palavras_no_inicio")},
    }
