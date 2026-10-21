"""
Base dos conectores (Fase 6). Nenhum segredo aqui: tokens chegam por
variável de ambiente (GitHub Secrets).

O que toda chamada externa ganha:
  • nova tentativa em 429 e 5xx, respeitando Retry-After (teto de espera);
  • erro de acesso (token vencido/revogado) vira AcessoExpirado — não é
    tentado de novo, vira o estado "acesso expirado" na tela;
  • registro de cada chamada SEM segredo (token, chave e senha saem da URL);
  • linha normalizada com procedência e "faltando" para métrica ausente
    (ausente é None, nunca zero inventado).
"""
import re
import time
from datetime import datetime, timezone

import requests


class AcessoExpirado(Exception):
    """Token vencido, revogado ou sem permissão: precisa de alguém."""


class ErroConector(Exception):
    """Erro da API que tentar de novo não resolve."""


SEGREDOS = re.compile(r"((?:access_token|token|key|client_secret|refresh_token|password|hottok)=)[^&\s\"']+", re.I)


def redigir(texto):
    """Tira segredos de URL ou texto antes de registrar."""
    t = SEGREDOS.sub(r"\1***", str(texto or ""))
    return re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1***", t)


def agora_iso():
    return datetime.now(timezone.utc).isoformat()


class Cliente:
    """HTTP com novas tentativas e registro redigido."""

    def __init__(self, nome, sessao=None, tentativas=4, espera_max=60, dormir=time.sleep,
                 auth_expirado=None, limite=None):
        self.nome, self.s = nome, sessao or requests
        self.tentativas, self.espera_max, self.dormir = tentativas, espera_max, dormir
        # Cada API diz "token vencido" e "limite" do seu jeito (às vezes com HTTP 200/400)
        self.auth_expirado = auth_expirado or (lambda st, corpo: st == 401)
        self.limite = limite or (lambda st, corpo: False)
        self.log = []

    def pedir(self, metodo, url, **kw):
        for n in range(1, self.tentativas + 1):
            t0 = time.time()
            r = self.s.request(metodo, url, timeout=60, **kw)
            try:
                corpo = r.json()
            except Exception:  # noqa: BLE001
                corpo = {"_texto": (getattr(r, "text", "") or "")[:500]}
            self.log.append({"conector": self.nome, "url": redigir(url)[:300], "status": r.status_code,
                             "ms": int((time.time() - t0) * 1000), "tentativa": n})
            if self.auth_expirado(r.status_code, corpo):
                raise AcessoExpirado(f"{self.nome}: acesso expirado ou sem permissão ({r.status_code}): "
                                     f"{redigir(str(corpo)[:300])}")
            if r.status_code == 429 or r.status_code >= 500 or self.limite(r.status_code, corpo):
                if n == self.tentativas:
                    raise ErroConector(f"{self.nome}: limite/instabilidade após {n} tentativas ({r.status_code})")
                ra = (getattr(r, "headers", {}) or {}).get("Retry-After")
                espera = float(ra) if ra and str(ra).replace(".", "", 1).isdigit() else 2 ** n
                self.dormir(min(espera, self.espera_max))
                continue
            if r.status_code >= 400:
                raise ErroConector(f"{self.nome}: HTTP {r.status_code}: {redigir(str(corpo)[:300])}")
            return corpo
        raise ErroConector(f"{self.nome}: sem resposta")


def num(v):
    """Número da API (às vezes vem como texto). Ausente → None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


CAMPOS = ("gasto", "impressoes", "cliques", "alcance", "conversoes", "receita", "sessoes", "usuarios")


def linha(plataforma, conta, nivel, entidade_id, entidade, data, versao, moeda=None, recorte=None,
          esperados=(), **metricas):
    """Linha normalizada. `esperados`: métricas que o conector pediu — as
    que vierem ausentes vão para "faltando" (e ficam None)."""
    out = {"plataforma": plataforma, "conta": str(conta), "nivel": nivel, "entidade_id": str(entidade_id),
           "entidade": entidade or "", "data": data, "moeda": moeda, "recorte": recorte or "",
           "versao_api": versao, "coletado_em": agora_iso(), "fonte": "api"}
    for c in CAMPOS:
        out[c] = metricas.get(c)
    out["faltando"] = [c for c in esperados if out.get(c) is None]
    return out


def chave(l):
    return (l["plataforma"], l["conta"], l["nivel"], l["entidade_id"], l["data"], l["recorte"])


def deduplicar(linhas):
    """Mesma chave = mesma linha (a última coleta vale)."""
    vistas = {}
    for l in linhas:
        vistas[chave(l)] = l
    return list(vistas.values())


class Resultado:
    """O que uma coleta devolve. `parcial` quando parte das contas/páginas falhou."""

    def __init__(self):
        self.linhas, self.erros, self.brutos = [], [], []
        self.acesso_expirado = False

    @property
    def estado(self):
        if self.acesso_expirado:
            return "acesso_expirado"
        if self.erros and self.linhas:
            return "parcial"
        if self.erros:
            return "erro"
        return "ativo"
