#!/usr/bin/env python3
"""
LEITURA DE DOCUMENTO — Docling (IBM Research, licenca MIT)

O problema que isto resolve, e o que NAO resolve:

Ferramenta comum de PDF embaralha coluna, perde tabela e mistura cabecalho
com corpo. O texto chega picado e o raciocinio some. O Docling corrige isso:
ele devolve hierarquia — titulo, secao, tabela, ordem de leitura.

O que ele NAO resolve: um manual de 2.600 paginas nao cabe no contexto de
nenhum modelo. Isso e limite de contexto, nao da ferramenta. Quem prometer
o contrario esta mentindo.

Entao a estrategia aqui e outra: o artefato guarda o MAPA (o sumario que o
Docling reconheceu) e os TRECHOS (cortados por secao, nunca no meio da
frase). O texto completo em markdown vai para o Storage e e lido por secao
quando alguma etapa precisar. E a diferenca entre picar o livro no
liquidificador e ler capitulo por capitulo tomando notas.

O arquivo vive no Supabase Storage privado. Nunca no repositorio do motor,
que e publico.
"""
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
import wheff

ORG = os.environ.get("WHEFF_ORG", "wheff")
BUCKET = os.environ.get("WHEFF_BUCKET", "documentos")
WORKER = "docs_ingest@actions"

# Limites do schema research-source:v1. Estourar em silencio seria pior que
# recusar: o artefato virava invalido e so descobririamos na leitura.
MAX_ITENS = 2000
MAX_SECOES = 500
MAX_CHARS_ITEM = 6000
MAX_RESUMO = 2000


# ── Storage ────────────────────────────────────────────────────────────────
# O Storage exige `apikey` alem do Authorization, igual ao resto da API. As
# chaves novas do Supabase nao sao JWT, entao o gateway nao consegue deduzir o
# projeto so pelo Bearer — sem o apikey a chamada volta 401.
_SH = {"apikey": wheff.KEY, "Authorization": f"Bearer {wheff.KEY}"}


def _diagnostico() -> str:
    """Descobre POR QUE o Storage recusou, em vez de repetir a mensagem dele.

    O Storage responde 'Bucket not found' tanto para bucket inexistente quanto
    para bucket que a chave nao tem direito de ver — de proposito, para nao
    revelar o que existe. As duas causas tem correcoes opostas, entao vale
    perguntar quantos buckets esta chave enxerga: service_role enxerga todos,
    uma chave publica nao enxerga nenhum.
    """
    try:
        r = requests.get(f"{wheff.URL}/storage/v1/bucket", headers=_SH, timeout=30)
        if r.status_code >= 300:
            return (f"a chave nem consegue listar buckets ({r.status_code}: "
                    f"{r.text[:160]}). Quase certamente SUPABASE_SERVICE_KEY nao e "
                    f"a chave secreta do projeto.")
        nomes = [b.get("name") for b in r.json()]
        if not nomes:
            return ("esta chave enxerga ZERO buckets. Ela le as tabelas porque o RLS "
                    "delas esta permissivo, mas nao e service_role — o Storage nao "
                    "da acesso administrativo a ela. Troque o segredo "
                    "SUPABASE_SERVICE_KEY pela chave SECRETA do projeto "
                    "(Supabase > Settings > API Keys), nao a publicavel.")
        if BUCKET not in nomes:
            return (f"a chave e valida e enxerga {len(nomes)} bucket(s) — {nomes} — "
                    f"mas '{BUCKET}' nao esta entre eles. Crie o bucket com esse nome "
                    f"exato, ou ajuste WHEFF_BUCKET no workflow.")
        return (f"o bucket '{BUCKET}' existe e a chave o enxerga, entao o problema e "
                f"no caminho do arquivo, nao no bucket.")
    except Exception as e:
        return f"nao consegui diagnosticar ({type(e).__name__}: {e})"


def baixar(caminho: str) -> bytes:
    r = requests.get(
        f"{wheff.URL}/storage/v1/object/{BUCKET}/{caminho}",
        headers=_SH, timeout=300)
    if r.status_code >= 300:
        raise RuntimeError(
            f"nao consegui baixar '{caminho}' do bucket '{BUCKET}' "
            f"({r.status_code}). {_diagnostico()} "
            f"Resposta crua do Storage: {r.text[:200]}")
    return r.content


def subir(caminho: str, conteudo: bytes, tipo="text/markdown") -> str:
    r = requests.post(
        f"{wheff.URL}/storage/v1/object/{BUCKET}/{caminho}",
        headers={**_SH, "Content-Type": tipo, "x-upsert": "true"},
        data=conteudo, timeout=300)
    if r.status_code >= 300:
        raise RuntimeError(f"nao consegui guardar o markdown ({r.status_code}): {r.text[:300]}")
    return caminho


# ── Conversao ──────────────────────────────────────────────────────────────
def converter(caminho_local: Path):
    """Devolve (markdown, avisos, n_tabelas, paginas_por_titulo).

    O markdown e o caminho garantido: e a saida estavel do Docling entre
    versoes. As paginas vem do dicionario interno, que muda mais — por isso
    vem embrulhado em try/except: perder o numero da pagina degrada o
    resultado, nao quebra a leitura.
    """
    from docling.document_converter import DocumentConverter

    avisos = []
    conv = DocumentConverter()
    res = conv.convert(str(caminho_local))
    doc = res.document

    md = doc.export_to_markdown()
    if not md.strip():
        raise RuntimeError(
            "o Docling leu o arquivo mas nao extraiu texto nenhum. "
            "Provavelmente e um PDF de paginas escaneadas sem camada de "
            "texto — precisa de OCR, que nao esta ligado.")

    n_tabelas = len(getattr(doc, "tables", []) or [])

    paginas = {}
    try:
        d = doc.export_to_dict()
        for t in d.get("texts", []):
            if t.get("label") not in ("section_header", "title"):
                continue
            txt = (t.get("text") or "").strip()
            prov = t.get("prov") or []
            if txt and prov and prov[0].get("page_no"):
                paginas.setdefault(txt, prov[0]["page_no"])
    except Exception as e:
        avisos.append(f"numeros de pagina indisponiveis nesta versao do Docling: {e}")

    return md, avisos, n_tabelas, paginas


# ── Mapa e trechos ─────────────────────────────────────────────────────────
RE_TITULO = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)


def fatiar(md: str, paginas: dict):
    """Quebra o markdown por titulo. Cada pedaco carrega a secao de onde saiu.

    Cortar por secao e o ponto inteiro: e o que faz o trecho continuar
    fazendo sentido sozinho. Cortar a cada N caracteres parte a frase no
    meio, e foi exatamente isso que fez as outras ferramentas falharem.
    """
    marcas = [(m.start(), len(m.group(1)), m.group(2).strip()) for m in RE_TITULO.finditer(md)]

    if not marcas:
        # Documento sem titulo nenhum: uma secao so, e isso vira ressalva.
        return [{"titulo": "(documento sem titulos)", "nivel": 1,
                 "texto": md.strip()}], False

    pedacos = []
    for i, (pos, nivel, titulo) in enumerate(marcas):
        fim = marcas[i + 1][0] if i + 1 < len(marcas) else len(md)
        corpo = md[pos:fim]
        corpo = RE_TITULO.sub("", corpo, count=1).strip()
        pedacos.append({"titulo": titulo, "nivel": nivel, "texto": corpo,
                        "pagina": paginas.get(titulo)})

    return pedacos, True


def quebrar_longo(texto: str, limite=MAX_CHARS_ITEM):
    """Secao maior que o limite vira varios trechos — quebrando em paragrafo,
    nunca no meio da frase."""
    if len(texto) <= limite:
        return [texto]
    partes, atual = [], ""
    for par in texto.split("\n\n"):
        if len(atual) + len(par) + 2 > limite and atual:
            partes.append(atual.strip())
            atual = ""
        if len(par) > limite:
            # Paragrafo unico gigante (tabela larga, bloco corrido): corta na
            # ultima quebra de linha antes do limite.
            while len(par) > limite:
                corte = par.rfind("\n", 0, limite)
                corte = corte if corte > limite // 2 else limite
                partes.append(par[:corte].strip())
                par = par[corte:]
        atual += par + "\n\n"
    if atual.strip():
        partes.append(atual.strip())
    return [p for p in partes if p.strip()]


def montar(pedacos, com_titulos):
    """Transforma os pedacos em secoes (o mapa) e itens (os trechos)."""
    secoes, itens = [], []
    truncado = False

    for p in pedacos:
        if len(secoes) < MAX_SECOES:
            s = {"titulo": p["titulo"][:300], "nivel": min(6, max(1, p["nivel"]))}
            if p.get("pagina"):
                s["pagina_inicial"] = p["pagina"]
            if p["texto"]:
                s["resumo"] = p["texto"][:MAX_RESUMO]
            secoes.append(s)

        if not p["texto"]:
            continue

        for parte in quebrar_longo(p["texto"]):
            if len(itens) >= MAX_ITENS:
                truncado = True
                break
            it = {
                "id": f"i{len(itens) + 1:03d}",
                "texto": parte[:MAX_CHARS_ITEM],
                # Documento e citacao literal: o texto nao foi reescrito.
                "verbatim": True,
                "secao": p["titulo"][:300],
            }
            if p.get("pagina"):
                it["pagina"] = p["pagina"]
            itens.append(it)
        if truncado:
            break

    return secoes, itens, truncado


# ── Job ────────────────────────────────────────────────────────────────────
def processar(job):
    p = job.get("payload") or {}
    caminho = p.get("storage_path")
    if not caminho:
        raise RuntimeError("job sem 'storage_path' — nao sei qual arquivo ler")

    nome = p.get("nome_arquivo") or Path(caminho).name
    formato = Path(nome).suffix.lstrip(".").lower() or "pdf"
    print(f"  lendo {nome} ({formato}) de {BUCKET}/{caminho}")

    dados_brutos = baixar(caminho)
    print(f"  baixado: {len(dados_brutos) / 1_048_576:.1f} MB")

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / nome
        local.write_bytes(dados_brutos)
        md, avisos, n_tabelas, paginas = converter(local)

    print(f"  extraido: {len(md)} caracteres, {n_tabelas} tabelas")

    pedacos, com_titulos = fatiar(md, paginas)
    secoes, itens, truncado = montar(pedacos, com_titulos)
    print(f"  mapa: {len(secoes)} secoes | trechos: {len(itens)}"
          + ("  (truncado)" if truncado else ""))

    if not itens:
        raise RuntimeError("o documento foi lido mas nao gerou nenhum trecho aproveitavel")

    if not com_titulos:
        avisos.append("o extrator nao reconheceu nenhum titulo — o corte por "
                      "secao nao e confiavel neste arquivo")

    md_path = f"convertidos/{Path(caminho).stem}.md"
    subir(md_path, md.encode("utf-8"))
    print(f"  markdown completo em {BUCKET}/{md_path}")

    limitacoes = []
    if truncado:
        limitacoes.append(
            f"O documento gerou mais de {MAX_ITENS} trechos. O artefato guarda os "
            f"primeiros; o texto completo esta em {md_path} e continua legivel por secao.")
    if len(pedacos) > MAX_SECOES:
        limitacoes.append(
            f"O sumario tem {len(pedacos)} secoes e o artefato guarda {MAX_SECOES}.")
    if not com_titulos:
        limitacoes.append("Documento sem estrutura de titulos: os trechos podem nao se "
                          "sustentar sozinhos.")
    limitacoes.append("Trechos de documento nao passaram por marcador — nenhuma etapa do "
                      "playbook e alcancada ate alguem marcar o que importa.")

    dados = {
        "titulo": p.get("titulo") or nome,
        "origem": {
            "plataforma": "documento",
            "titulo_original": nome,
            "idioma": p.get("idioma", "pt-BR"),
            "pais": p.get("pais", "Brasil"),
        },
        "coleta": {
            "modo": "automatica",
            "ferramenta": "docling",
            "coletado_em": datetime.now(timezone.utc).date().isoformat(),
            "coletado_por": WORKER,
        },
        "documento": {
            "nome_arquivo": nome,
            "formato": formato,
            "extrator": "docling",
            # So e verdade se o Docling achou titulos. Declarar sempre true
            # seria mentir para quem for confiar no corte por secao.
            "estrutura_preservada": bool(com_titulos),
            "arquivo_original": caminho,
            "arquivo_convertido": md_path,
            "caracteres": len(md),
            "tabelas": n_tabelas,
            "truncado": truncado,
            "secoes": secoes,
        },
        "itens": itens,
        "destino": p.get("destino") or ["audience_profile"],
        "etapas_playbook": [],
        "representatividade": {
            "n_itens": len(itens),
            "n_fontes_distintas": 1,
            # Um documento e uma voz so. Por mais longo que seja, nao vira
            # amostra de publico — vira material de referencia.
            "serve_para_conclusao": False,
            "vieses_conhecidos": ["Fonte unica: reflete quem escreveu o documento."],
        },
        "limitacoes": limitacoes,
    }
    if avisos:
        dados["documento"]["avisos_extracao"] = avisos[:50]
    if p.get("paginas"):
        dados["documento"]["paginas"] = int(p["paginas"])

    art = wheff.criar_artefato(
        ORG, "research_source", "OBSERVED", "research-source:v1", dados,
        criado_por=WORKER, escopo="ORG", status="APPROVED",
        snapshot={"extrator": "docling", "bucket": BUCKET,
                  "arquivo": caminho, "limites": {"itens": MAX_ITENS, "secoes": MAX_SECOES}},
    )
    print(f"  OK {art['artifact_key']} — {len(itens)} trechos, {len(secoes)} secoes")
    return art


def anotar(msg: str) -> None:
    """Publica uma anotacao no GitHub Actions.

    O log do Actions exige login; a ANOTACAO nao — ela sai na API publica do
    repositorio. Quando o worker nao enxerga nem a tabela jobs, este e o
    unico canal que sobra para dizer o que esta acontecendo.
    """
    print("::error::" + msg.replace("\r", "").replace("\n", "%0A"))


def sondar() -> str:
    """Quem sou eu para este banco, e o que eu consigo ver?"""
    partes = []
    for rotulo, url in (
        ("jobs de qualquer tipo", f"{wheff.URL}/rest/v1/jobs?select=job_type,status,org_id&limit=50"),
        ("artefatos",             f"{wheff.URL}/rest/v1/artifacts?select=type&limit=5"),
        ("marcas",                f"{wheff.URL}/rest/v1/marcas?select=id&limit=5"),
    ):
        try:
            r = requests.get(url, headers=wheff.H, timeout=30)
            if r.status_code >= 300:
                partes.append(f"{rotulo}: HTTP {r.status_code} {r.text[:90]}")
            else:
                linhas = r.json()
                extra = ""
                if rotulo.startswith("jobs") and linhas:
                    tipos = sorted({f"{x['job_type']}/{x['status']}/{x['org_id']}" for x in linhas})
                    extra = " -> " + ", ".join(tipos[:8])
                partes.append(f"{rotulo}: {len(linhas)}{extra}")
        except Exception as e:
            partes.append(f"{rotulo}: {type(e).__name__}")
    projeto = wheff.URL.split("//")[-1].split(".")[0]
    return f"projeto={projeto} org={ORG} | " + " | ".join(partes)


def main():
    job = wheff.pegar_job(ORG, WORKER, ["doc.ingest"])
    # Postgres devolve uma linha de NULLs quando nao ha job, e um dict de
    # NULLs e verdadeiro. Por isso a checagem e pelo id, nao pelo objeto.
    if not job or not job.get("id"):
        # "Nada na fila" pode ser fila vazia OU tarefa que existe e o
        # claim_job nao enxerga — e as duas exigem acoes opostas. Ficar em
        # silencio nas duas custou duas rodadas de investigacao, entao aqui
        # ele conta o que ve e por que nao serviu.
        print("nada na fila de documentos — conferindo se e mesmo fila vazia")
        try:
            r = requests.get(
                f"{wheff.URL}/rest/v1/jobs"
                f"?job_type=eq.doc.ingest&select=id,org_id,status,attempts,max_attempts,available_at"
                f"&order=created_at.desc&limit=10",
                headers=wheff.H, timeout=30)
            linhas = r.json() if r.status_code < 300 else []
            if not linhas:
                print("  nenhuma tarefa doc.ingest visivel para esta chave.")
                # Se a plataforma mostra a tarefa e o worker nao a ve, a
                # diferenca esta na credencial ou no projeto — nunca na fila.
                anotar("Nao vejo nenhuma tarefa doc.ingest. Sondagem: " + sondar())
            for x in linhas:
                motivos = []
                if x.get("org_id") != ORG:
                    motivos.append(f"org '{x.get('org_id')}' != '{ORG}' que este worker pede")
                if x.get("status") != "QUEUED":
                    motivos.append(f"status {x.get('status')}, e so QUEUED e pegavel")
                if (x.get("available_at") or "") > datetime.now(timezone.utc).isoformat():
                    motivos.append(f"agendada para {x['available_at']}, ainda no futuro")
                explicacao = ("; ".join(motivos) if motivos else
                              "deveria ter sido pega e nao foi — o problema esta no "
                              "claim_job, nao nesta tarefa")
                print(f"  {x['id'][:8]} status={x['status']} org={x['org_id']} "
                      f"tentativas={x['attempts']}/{x['max_attempts']} -> {explicacao}")
                # O log do Actions exige login, entao a conclusao vai para o
                # campo que a plataforma ja mostra na tela de Fontes. Sem isto
                # o diagnostico morre num lugar que a dona nao alcanca.
                requests.patch(
                    f"{wheff.URL}/rest/v1/jobs?id=eq.{x['id']}", headers=wheff.H, timeout=30,
                    json={"last_error": "diagnostico do motor: " + explicacao})
        except Exception as e:
            print(f"  nao consegui conferir a fila: {type(e).__name__}: {e}")
        return 0

    print(f"Tarefa {job['id']} — tentativa {job.get('attempts')}")
    try:
        art = processar(job)
        wheff.terminar_job(ORG, job["id"])
        wheff.registrar_evento(ORG, "job.completed", job_id=job["id"],
                               artifact_id=art["id"], actor_type="worker",
                               actor_id=WORKER)
        return 0
    except Exception as e:
        traceback.print_exc()
        wheff.terminar_job(ORG, job["id"], erro=f"{type(e).__name__}: {e}")
        print(f"ERRO: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
