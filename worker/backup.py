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
]


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

    for t in TABELAS:
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

    resumo["total_linhas"] = total
    with open(os.path.join(DESTINO, "_resumo.json"), "w", encoding="utf-8") as f:
        json.dump(resumo, f, ensure_ascii=False, indent=1)

    print(f"\n{total} linhas em {len(TABELAS)} tabelas.")
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
