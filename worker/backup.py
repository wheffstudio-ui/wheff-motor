"""
Cópia de segurança das tabelas do Supabase.

Por que existe: o plano gratuito do Supabase não oferece backup para
download, e já pausou o projeto por inatividade uma vez. Sem isto, os
artefatos, a genealogia e o diário de bordo têm UMA cópia só.

Gera JSON de cada tabela. O workflow leva os arquivos para um repositório
PRIVADO — nada disso pode ir para o repositório público do motor.
"""
import json
import os
import sys
from datetime import datetime, timezone

import requests

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
DESTINO = os.environ.get("BACKUP_DIR", "backup")
PAGINA = 1000

H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}

TABELAS = [
    "artifacts",         # as peças de trabalho
    "artifact_links",    # a genealogia
    "events",            # o diário de bordo
    "jobs",              # a fila
    "artifact_counters",
    "marcas",
    "plataforma",        # o PDB inteiro, o dado antigo
    "papeis",            # a lista de acesso (quem entra e com que papel)
    "perfis",            # nome e foto da equipe
    "portal_progresso",  # o que a produtora marcou como gravado
    "conversas",         # atendimento
    "mensagens",
    "respostas_prontas",
    "onboarding",
]

# Cache que o motor refaz sozinho: não precisa de cópia.
FORA = {"em_alta_cache"}


def descobrir_tabelas():
    """Todas as tabelas e views que a API expõe (o "mapa" do PostgREST), para
    que tabela nova entre no backup sem ninguém lembrar de pôr na lista.
    As da lista fixa vêm primeiro; se a descoberta falhar, fica só a lista."""
    try:
        r = requests.get(f"{URL}/rest/v1/", headers={**H, "Accept": "application/openapi+json"}, timeout=60)
        r.raise_for_status()
        achadas = sorted(p.strip("/") for p in r.json().get("paths", {})
                         if p.count("/") == 1 and p != "/" and not p.startswith("/rpc"))
    except Exception as e:
        print(f"  (não deu para descobrir as tabelas: {e}; uso só a lista fixa)", file=sys.stderr)
        achadas = []
    return TABELAS + [t for t in achadas if t not in TABELAS and t not in FORA]


def listar_storage():
    """Nome, tamanho e data de cada arquivo de cada balde (sem baixar os
    arquivos): dá para saber o que existia se algo sumir."""
    saida = {}
    r = requests.get(f"{URL}/storage/v1/bucket", headers=H, timeout=60)
    r.raise_for_status()
    for b in r.json():
        arquivos, pilha = [], [""]
        while pilha:
            pasta = pilha.pop()
            ini = 0
            while True:
                q = requests.post(f"{URL}/storage/v1/object/list/{b['id']}", headers={**H, "Content-Type": "application/json"},
                                  json={"prefix": pasta, "limit": 1000, "offset": ini}, timeout=60)
                q.raise_for_status()
                lote = q.json()
                for o in lote:
                    cam = f"{pasta}/{o['name']}" if pasta else o["name"]
                    if o.get("id") is None:
                        pilha.append(cam)       # é pasta
                    else:
                        meta = o.get("metadata") or {}
                        arquivos.append({"caminho": cam, "bytes": meta.get("size"), "tipo": meta.get("mimetype"),
                                         "atualizado_em": o.get("updated_at")})
                if len(lote) < 1000:
                    break
                ini += 1000
        saida[b["id"]] = {"publico": b.get("public"), "arquivos": arquivos}
    return saida


def baixar_tabela(nome):
    """Lê a tabela inteira em páginas, para não estourar memória nem timeout."""
    linhas, inicio = [], 0
    while True:
        r = requests.get(
            f"{URL}/rest/v1/{nome}?select=*",
            headers={**H, "Range-Unit": "items",
                     "Range": f"{inicio}-{inicio + PAGINA - 1}"},
            timeout=120,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"{nome} ({r.status_code}): {r.text[:300]}")
        lote = r.json()
        linhas.extend(lote)
        if len(lote) < PAGINA:
            break
        inicio += PAGINA
    return linhas


def main():
    os.makedirs(DESTINO, exist_ok=True)
    agora = datetime.now(timezone.utc)
    resumo = {"gerado_em": agora.isoformat(), "tabelas": {}}
    total = 0
    falhou = False

    tabelas = descobrir_tabelas()
    for t in tabelas:
        try:
            linhas = baixar_tabela(t)
            caminho = os.path.join(DESTINO, f"{t}.json")
            with open(caminho, "w", encoding="utf-8") as f:
                json.dump(linhas, f, ensure_ascii=False, indent=1)
            tam = os.path.getsize(caminho)
            resumo["tabelas"][t] = {"linhas": len(linhas), "bytes": tam}
            total += len(linhas)
            print(f"  {t}: {len(linhas)} linhas ({tam // 1024} KB)")
        except Exception as e:
            # Uma tabela que falha não pode impedir o backup das outras.
            print(f"  {t}: FALHOU — {e}", file=sys.stderr)
            resumo["tabelas"][t] = {"erro": str(e)[:300]}
            falhou = True

    try:
        st = listar_storage()
        with open(os.path.join(DESTINO, "_storage.json"), "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        resumo["storage"] = {b: len(v["arquivos"]) for b, v in st.items()}
        print("  storage: " + ", ".join(f"{b} {n}" for b, n in resumo["storage"].items()))
    except Exception as e:
        print(f"  storage: FALHOU — {e}", file=sys.stderr)
        resumo["storage"] = {"erro": str(e)[:300]}

    resumo["total_linhas"] = total
    with open(os.path.join(DESTINO, "_resumo.json"), "w", encoding="utf-8") as f:
        json.dump(resumo, f, ensure_ascii=False, indent=1)

    print(f"\n{total} linhas em {len(tabelas)} tabelas.")
    if falhou:
        print("Backup PARCIAL — alguma tabela não veio.", file=sys.stderr)
        return 1

    # Um backup vazio é sinal de que algo está errado (chave sem permissão,
    # projeto pausado). Melhor gritar do que gravar um arquivo vazio por cima
    # de uma cópia boa.
    if total == 0:
        print("Backup VAZIO — nenhuma linha em nenhuma tabela.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
