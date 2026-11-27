"""
Redes e contatos de uma candidata do Match de Parceria (26/11/2026).

Job parceira.contatos  payload {pedido_id, usuario, links: [...]}

1. Maigret (código aberto, MIT; https://github.com/soxoj/maigret): procura o
   mesmo @ nos ~500 sites mais usados e devolve os perfis achados, com o que
   cada um mostra em público (nome, bio, cidade, seguidores, links).
   Mesmo @ não quer dizer mesma pessoa: a tela pede que o adm confira.
2. Lê as páginas públicas achadas (Jina Reader, gratuito, o mesmo do Vigia) e
   os links da ficha, e tira delas WhatsApp, celular e e-mail que a própria
   pessoa publicou (wa.me, api.whatsapp.com, números escritos, e-mails).
3. Confere cada número com o phonenumbers (a biblioteca do Google, a mesma
   base que o PhoneInfoga usa no modo local): válido? celular ou fixo? que
   estado? E conta em quantas páginas o mesmo número aparece.

Nada é inventado: se a pessoa nunca publicou o número, ele não aparece.
O resultado vai para public.parceira_contatos, que só o adm lê (LGPD).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone

import requests

ORG = os.environ.get("WHEFF_ORG", "wheff")
WORKER = "parceira-contatos"
TIMEOUT = 25
MAX_PAGINAS = 20
TOP_SITES = int(os.environ.get("MAIGRET_TOP_SITES", "500"))

# Sites onde vale ler a página procurando contato (os outros só entram na lista de redes)
LER = re.compile(r"instagram|tiktok|youtube|facebook|twitter|x\.com|threads|linktr|beacons|linkedin|pinterest|kwai|"
                 r"substack|medium|bio\.link|campsite|taplink|about\.me|carrd|wa\.me|whatsapp|hotmart|kiwify|\.com\.br",
                 re.I)

RE_WA = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send/?\?(?:[^\s\"'<>]*?&)?phone=|whatsapp://send\?phone=)\+?(\d{10,15})", re.I)
RE_TEL = re.compile(r"(?<![\d/=])(?:\+?\s?55[\s.\-]?)?\(?([1-9]{2})\)?[\s.\-]?(9\d{4})[\s.\-]?(\d{4})(?![\d])")  # só celular escrito: fixo de 8 dígitos confunde com qualquer número
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
EMAIL_LIXO = re.compile(r"(example\.|sentry|wixpress|\.png$|\.jpg$|\.webp$|\.gif$|noreply|no-reply|@2x|domain\.com|email\.com$)", re.I)


def _usuario_ok(u):
    u = (u or "").strip().lstrip("@").strip("/")
    u = u.split("/")[-1]
    if not re.fullmatch(r"[A-Za-z0-9._\-]{2,40}", u):
        raise ValueError(f"@ inválido: {u!r}")
    return u


# ── 3. números ─────────────────────────────────────────────────────────────
def conferir_numero(digitos):
    """Devolve {valor, formatado, tipo, regiao, valido} ou None."""
    import phonenumbers
    from phonenumbers import geocoder, number_type, PhoneNumberType
    d = re.sub(r"\D", "", digitos)
    if not d:
        return None
    try:
        n = phonenumbers.parse("+" + d if d.startswith("55") and len(d) >= 12 else d, "BR")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(n):
        return None
    t = number_type(n)
    tipo = "celular" if t in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE) else "fixo"
    return {
        "valor": phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164),
        "formatado": phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
        "tipo": tipo,
        "regiao": geocoder.description_for_number(n, "pt") or "",
        "valido": True,
    }


# ── 2. extrair de um texto ─────────────────────────────────────────────────
def extrair_contatos(texto, fonte):
    """Lista de {tipo, valor, formatado, regiao, fonte} achados no texto."""
    achados, vistos = [], set()
    texto = texto or ""
    for m in RE_WA.finditer(texto):
        c = conferir_numero(m.group(1))
        if c and ("whatsapp", c["valor"]) not in vistos:
            vistos.add(("whatsapp", c["valor"]))
            achados.append({**c, "tipo": "whatsapp", "linha": c["tipo"], "fonte": fonte})
    for m in RE_TEL.finditer(texto):
        c = conferir_numero("".join(m.groups()))
        if c and not any(v == c["valor"] for _, v in vistos):
            vistos.add((c["tipo"], c["valor"]))
            achados.append({**c, "fonte": fonte})
    for e in set(RE_EMAIL.findall(texto)):
        e = e.strip(".").lower()
        if not EMAIL_LIXO.search(e) and ("email", e) not in vistos:
            vistos.add(("email", e))
            achados.append({"tipo": "email", "valor": e, "formatado": e, "fonte": fonte})
    return achados


def juntar(listas):
    """Junta por (tipo, valor) e conta as páginas onde cada um apareceu."""
    por = {}
    for c in listas:
        k = (c["tipo"], c["valor"])
        if k not in por:
            por[k] = {**{x: y for x, y in c.items() if x != "fonte"}, "fontes": []}
        if c["fonte"] not in por[k]["fontes"]:
            por[k]["fontes"].append(c["fonte"])
    ordem = {"whatsapp": 0, "celular": 1, "email": 2, "fixo": 3}
    return sorted(por.values(), key=lambda c: (ordem.get(c["tipo"], 9), -len(c["fontes"])))


# ── 1. Maigret ─────────────────────────────────────────────────────────────
def rodar_maigret(usuario):
    pasta = tempfile.mkdtemp(prefix="maigret-")
    cmd = [sys.executable, "-m", "maigret", usuario, "--top-sites", str(TOP_SITES), "--timeout", "15",
           "--no-autoupdate", "--no-progressbar", "--no-color", "-J", "simple", "--folderoutput", pasta]
    subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    arq = os.path.join(pasta, f"report_{usuario}_simple.json")
    if not os.path.exists(arq):
        raise RuntimeError("o Maigret não gerou relatório")
    with open(arq, encoding="utf-8") as f:
        return json.load(f)


def perfis_do_maigret(rel):
    perfis = []
    for site, v in (rel or {}).items():
        st = v.get("status") or {}
        if st.get("status") != "Claimed":
            continue
        ids = st.get("ids") or {}
        links = ids.get("links") or []
        if isinstance(links, str):
            links = re.findall(r"https?://[^\s'\"\],]+", links)
        perfis.append({
            "site": site, "url": v.get("url_user") or st.get("url"),
            "nome": ids.get("fullname") or ids.get("name") or "",
            "bio": ids.get("bio") or ids.get("description") or "",
            "local": ids.get("location") or "",
            "seguidores": ids.get("follower_count") or ids.get("followers_count") or "",
            "links": links[:10], "tags": st.get("tags") or [],
            "extra": " ".join(str(x) for k, x in ids.items() if k in ("email", "emails", "phone", "website", "external_url")),
        })
    # quem tem dado público primeiro, depois as redes conhecidas
    return sorted(perfis, key=lambda p: (not (p["nome"] or p["bio"]), not LER.search(p["url"] or ""), p["site"]))


def ler_pagina(url):
    r = requests.get("https://r.jina.ai/" + url, headers={"Accept": "text/plain", "X-Return-Format": "text"}, timeout=TIMEOUT)
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code}")
    # o Jina devolve os links em markdown: mantém (wa.me aparece aí)
    return r.text[:200000]


def buscar(usuario, links_ficha):
    rel = rodar_maigret(usuario)
    perfis = perfis_do_maigret(rel)
    achados = []
    for p in perfis:  # o que o próprio Maigret já trouxe da página
        achados += extrair_contatos(" ".join([p["bio"], p["extra"], " ".join(p["links"])]), p["url"])
    fila = []
    for u in list(links_ficha or []) + [p["url"] for p in perfis if LER.search(p["url"] or "")] + [l for p in perfis for l in p["links"]]:
        if u and u.startswith("http") and u not in fila:
            fila.append(u)
    lidas, falharam = [], []
    for u in fila[:MAX_PAGINAS]:
        try:
            achados += extrair_contatos(ler_pagina(u), u)
            lidas.append(u)
        except Exception as e:
            falharam.append({"url": u, "erro": str(e)[:120]})
    return {
        "usuario": usuario,
        "em": datetime.now(timezone.utc).isoformat(),
        "perfis": perfis,
        "contatos": juntar(achados),
        "paginas_lidas": lidas,
        "paginas_falharam": falharam,
        "sites_checados": TOP_SITES,
        "limites": [
            "Mesmo @ em outro site não garante que é a mesma pessoa: confira antes de usar.",
            "Só aparece contato que a própria pessoa publicou em página pública.",
            "Instagram e TikTok às vezes pedem login e não abrem para leitura.",
        ],
    }


def gravar(pedido_id, usuario, corpo):
    """Grava o resultado SÓ no pedido certo: id válido, ainda na fila e do
    mesmo @ do job (27/11/2026, auditoria item 2). Qualquer conta da equipe
    pode pôr um job na fila; sem esta conferência, um job com o pedido_id de
    outra busca sobrescreveria o resultado dela."""
    import wheff
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(pedido_id or "")):
        raise ValueError("pedido_id inválido")
    corpo["atualizado_em"] = datetime.now(timezone.utc).isoformat()
    r = wheff._check(requests.patch(
        f"{wheff.URL}/rest/v1/parceira_contatos",
        params={"id": f"eq.{pedido_id}", "status": "eq.FILA", "usuario": f"eq.{usuario}"},
        headers={**wheff.H, "Prefer": "return=representation"}, json=corpo, timeout=30), "gravar contatos")
    if not r.json():
        raise ValueError("pedido não está na fila ou é de outro @: nada gravado")


def main():
    import wheff
    job = wheff.pegar_job(ORG, WORKER, ["parceira.contatos"], minutos=40)
    if not job or not job.get("id"):
        print("nada na fila de contatos")
        return 0
    p = job.get("payload") or {}
    pedido = p.get("pedido_id")
    print(f"Tarefa {job['id']} — @{p.get('usuario')}")
    try:
        usuario = _usuario_ok(p.get("usuario"))
        if pedido:   # confere ANTES de gastar a busca
            r = requests.get(f"{wheff.URL}/rest/v1/parceira_contatos",
                             params={"id": f"eq.{pedido}", "status": "eq.FILA", "usuario": f"eq.{usuario}", "select": "id"},
                             headers=wheff.H, timeout=30)
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(pedido)) or r.status_code >= 300 or not r.json():
                raise ValueError("pedido não está na fila ou é de outro @")
        res = buscar(usuario, [l for l in (p.get("links") or []) if isinstance(l, str)][:10])
        if pedido:
            gravar(pedido, usuario, {"status": "PRONTO", "resultado": res, "erro": None})
        wheff.terminar_job(ORG, job["id"])
        print(f"  OK: {len(res['perfis'])} perfis, {len(res['contatos'])} contatos, {len(res['paginas_lidas'])} páginas lidas")
        return 0
    except Exception as e:
        traceback.print_exc()
        definitivo = isinstance(e, ValueError)
        st = wheff.terminar_job(ORG, job["id"], erro=f"{type(e).__name__}: {e}", definitivo=definitivo)
        if pedido and st == "FAILED":
            try:
                gravar(pedido, str(p.get("usuario") or "").lstrip("@").split("/")[-1], {"status": "ERRO", "erro": str(e)[:500]})
            except Exception:
                pass   # pedido de outro @ ou fora da fila: não mexe nele
        return 1


if __name__ == "__main__":
    sys.exit(main())
