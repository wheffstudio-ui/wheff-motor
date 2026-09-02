"""
media.ingest — baixa um post e transcreve o que é falado.

Recebe da fila:  { "url": "https://www.instagram.com/reel/..." }
Produz:          VIDEO-000N  (metadados)  +  TRANSCRIPT-000N (fala)
Enfileira:       dna.analyze

Roda no GitHub Actions, não na sua máquina.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

import wheff

ORG = os.environ.get("WHEFF_ORG", "wheff")
WORKER = f"gh-actions/{os.environ.get('GITHUB_RUN_ID', 'local')}"
MODELO = os.environ.get("WHISPER_MODEL", "base")


def baixar(url, pasta):
    """yt-dlp: metadados + arquivo de áudio."""
    saida = os.path.join(pasta, "midia")
    subprocess.run(
        ["yt-dlp", "--no-playlist", "--write-info-json", "--no-warnings",
         "-f", "bestaudio/best", "-o", saida + ".%(ext)s", url],
        check=True, capture_output=True, text=True, timeout=600,
    )
    info_path = next((os.path.join(pasta, f) for f in os.listdir(pasta)
                      if f.endswith(".info.json")), None)
    if not info_path:
        raise RuntimeError("yt-dlp não devolveu metadados")
    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)

    midia = next((os.path.join(pasta, f) for f in os.listdir(pasta)
                  if f.startswith("midia") and not f.endswith(".info.json")), None)
    if not midia:
        raise RuntimeError("yt-dlp não devolveu arquivo de mídia")
    return info, midia


def transcrever(caminho):
    """faster-whisper: o que é falado, com o segundo exato de cada trecho."""
    from faster_whisper import WhisperModel
    modelo = WhisperModel(MODELO, device="cpu", compute_type="int8")
    segmentos, info = modelo.transcribe(caminho, vad_filter=True)
    trechos = [{"id": i, "inicio": round(s.start, 2),
                "fim": round(s.end, 2), "texto": s.text.strip()}
               for i, s in enumerate(segmentos)]
    return {
        "idioma": info.language,
        "duracao": round(info.duration, 2),
        "texto": " ".join(t["texto"] for t in trechos),
        "trechos": trechos,   # é isto que permite "por que a IA disse isso?"
        "modelo": f"faster-whisper:{MODELO}",
    }


def executar(job):
    url = job["payload"]["url"]
    source_id = job["payload"].get("source_artifact_id")

    with tempfile.TemporaryDirectory() as pasta:
        info, midia = baixar(url, pasta)

        # Impressão digital do conteúdo: mesmo vídeo não reprocessa.
        with open(midia, "rb") as f:
            h = hashlib.sha256()
            for bloco in iter(lambda: f.read(1 << 20), b""):
                h.update(bloco)
        content_hash = h.hexdigest()

        video = wheff.criar_artefato(
            ORG, "video", "OBSERVED", "video:v1", escopo="SHARED",
            content_hash=content_hash, criado_por=f"worker:media/{WORKER}",
            dados={
                "url": url,
                "plataforma": info.get("extractor_key", "").lower(),
                "titulo": info.get("title"),
                "autor": info.get("uploader") or info.get("channel"),
                "duracao": info.get("duration"),
                "legenda": info.get("description"),
                "curtidas": info.get("like_count"),
                "comentarios": info.get("comment_count"),
                "visualizacoes": info.get("view_count"),
                "publicado_em": info.get("upload_date"),
            })
        print(f"  criado {video['artifact_key']}")

        if source_id:
            wheff.ligar(ORG, video, {"id": source_id, "version": 1}, "derived_from")

        t = transcrever(midia)
        transcript = wheff.criar_artefato(
            ORG, "transcript", "OBSERVED", "transcript:v1", escopo="SHARED",
            criado_por=f"worker:whisper/{WORKER}", dados=t,
            snapshot={"modelo": t["modelo"]})
        wheff.ligar(ORG, transcript, video, "derived_from")
        print(f"  criado {transcript['artifact_key']} "
              f"({len(t['trechos'])} trechos, {t['duracao']}s)")

        # Próximo elo da corrente. A IA lê a transcrição e produz o DNA.
        wheff.enfileirar(ORG, "dna.analyze",
                         {"video_artifact_id": video["id"],
                          "transcript_artifact_id": transcript["id"]},
                         idempotency_key=f"dna:{video['id']}")
        print("  enfileirado dna.analyze")


def main():
    job = wheff.pegar_job(ORG, WORKER, ["media.ingest"])
    if not job:
        print("Nada na fila.")
        return 0

    print(f"Tarefa {job['id']} — tentativa {job['attempts']}")
    try:
        executar(job)
        wheff.terminar_job(ORG, job["id"])
        print("OK")
        return 0
    except Exception as e:
        print(f"ERRO: {e}", file=sys.stderr)
        wheff.terminar_job(ORG, job["id"], erro=e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
