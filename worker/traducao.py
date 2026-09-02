"""
Tradução para português — argos-translate (MIT), offline, sem custo de API.

Como funciona, por idioma de origem:

    português  →  nada a fazer
    inglês     →  argos  en→pt
    outro      →  whisper task=translate (→ inglês)  →  argos  en→pt

O pivô pelo inglês existe porque é o par com melhor qualidade e o único
garantido para qualquer idioma que o Whisper reconheça.

IMPORTANTE — leia antes de mudar isto:
A tradução é para VOCÊ ler. A análise da IA é feita sempre sobre o texto
ORIGINAL, nunca sobre a tradução. Modelo de linguagem lê inglês tão bem
quanto português, e analisar uma tradução automática significa analisar o
erro do tradutor junto com o conteúdo. Gíria de marketing é exatamente onde
tradutor automático mais erra — "crush it", "double down", "no-brainer".
"""
import os

_INSTALADO = set()


def _garantir_par(de, para):
    """Baixa o pacote do par de idiomas na primeira vez que precisar dele."""
    import argostranslate.package
    import argostranslate.translate

    chave = f"{de}->{para}"
    if chave in _INSTALADO:
        return
    ja_tem = any(p.from_code == de and p.to_code == para
                 for p in argostranslate.package.get_installed_packages())
    if not ja_tem:
        argostranslate.package.update_package_index()
        pacote = next((p for p in argostranslate.package.get_available_packages()
                       if p.from_code == de and p.to_code == para), None)
        if pacote is None:
            raise RuntimeError(f"argos não tem o par {chave}")
        argostranslate.package.install_from_path(pacote.download())
    _INSTALADO.add(chave)


def _argos(texto, de, para):
    import argostranslate.translate
    _garantir_par(de, para)
    return argostranslate.translate.translate(texto, de, para)


def traduzir_trechos(trechos, idioma_origem, caminho_audio=None, modelo_whisper=None):
    """
    Devolve (trechos_traduzidos, como_foi_feito) ou (None, motivo) quando
    não há nada a traduzir.

    Traduz trecho a trecho para preservar os segundos — é isso que permite
    clicar numa frase traduzida e o vídeo abrir no ponto exato.
    """
    origem = (idioma_origem or "").lower()

    if origem in ("pt", "por", "pt-br"):
        return None, "já está em português"

    # Idioma que o argos não cobre bem: passa pelo inglês via Whisper
    if origem != "en" and caminho_audio and modelo_whisper:
        from faster_whisper import WhisperModel
        m = WhisperModel(modelo_whisper, device="cpu", compute_type="int8")
        segs, _ = m.transcribe(caminho_audio, task="translate", vad_filter=True)
        trechos = [{"id": i, "inicio": round(s.start, 2), "fim": round(s.end, 2),
                    "texto": s.text.strip()} for i, s in enumerate(segs)]
        origem = "en"
        via = f"whisper:translate({idioma_origem}→en) + argos:en→pt"
    else:
        via = f"argos:{origem}→pt"

    if origem != "en":
        return None, f"idioma '{idioma_origem}' sem caminho de tradução"

    traduzidos = []
    for t in trechos:
        texto = (t.get("texto") or "").strip()
        traduzidos.append({**t, "texto": _argos(texto, "en", "pt") if texto else ""})

    return traduzidos, via
