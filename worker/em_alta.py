"""
Em Alta pelo motor (27/11/2026).

O Google bloqueia o Worker da Cloudflare ("Sorry...", 429/503) nas buscas do
Em Alta. Este script roda de hora em hora no GitHub Actions, busca os mesmos
RSS públicos que a tela usava e guarda o XML, como veio, em
public.em_alta_cache (chave = endereço do RSS). A tela lê de lá.

Fontes: Google Trends (Brasil), Google Notícias geral e entretenimento, e o
nicho de cada espaço (plataforma.dados -> settings -> emAltaNicho, o primeiro
termo antes da vírgula, igual à tela). Nada é inventado nem resumido.
"""
import datetime as dt
import os
from urllib.parse import quote

import requests

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept-Language": "pt-BR,pt;q=0.9"}

# Os mesmos endereços de EMALTA_RSS (app_wheff_v33.html). A chave tem de bater.
TRENDS = "https://trends.google.com/trending/rss?geo=BR"
GERAL = "https://news.google.com/rss?hl=pt-BR&gl=BR&ceid=BR:pt-419"
FOFOCA = "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT?hl=pt-BR&gl=BR&ceid=BR:pt-419"


SEGUROS = "-_.!~*'()"   # os mesmos que encodeURIComponent deixa


def url_nicho(n):
    return f"https://news.google.com/rss/search?q={quote(n, safe=SEGUROS)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"


def nichos():
    r = requests.get(f"{URL}/rest/v1/plataforma", headers=H,
                     params={"select": "nicho:dados->settings->>emAltaNicho"}, timeout=30)
    r.raise_for_status()
    vistos = []
    for linha in r.json():
        n = (linha.get("nicho") or "").split(",")[0].strip()
        if n and n not in vistos:
            vistos.append(n)
    return vistos


def buscar(u):
    try:
        r = requests.get(u, headers=UA, timeout=30)
        if r.status_code != 200:
            return None, f"a fonte respondeu {r.status_code}"
        if "<rss" not in r.text[:500] and "<feed" not in r.text[:500]:
            return None, "a fonte devolveu um formato inesperado"
        return r.text, None
    except requests.RequestException as e:
        return None, f"falha de rede: {type(e).__name__}"


def gravar(chave, xml, erro):
    corpo = {"chave": chave, "erro": erro, "atualizado_em": dt.datetime.now(dt.timezone.utc).isoformat()}
    if xml is not None:
        corpo["xml"] = xml          # se falhou, fica o último XML bom
    r = requests.post(f"{URL}/rest/v1/em_alta_cache", headers={**H, "Prefer": "resolution=merge-duplicates"},
                      params={"on_conflict": "chave"}, json=corpo, timeout=30)
    r.raise_for_status()


def main():
    fontes = [TRENDS, GERAL, FOFOCA] + [url_nicho(n) for n in nichos()]
    falhas = 0
    for u in fontes:
        xml, erro = buscar(u)
        gravar(u, xml, erro)
        print(("ok   " if xml else "ERRO ") + u + (f" — {erro}" if erro else f" ({len(xml)} bytes)"))
        falhas += 1 if erro else 0
    if falhas == len(fontes):
        raise SystemExit("nenhuma fonte respondeu")


if __name__ == "__main__":
    main()
