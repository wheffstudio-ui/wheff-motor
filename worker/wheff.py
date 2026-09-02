"""
Conversa com o banco da Wheff (Supabase).

Este arquivo NÃO contém segredo nenhum. As chaves chegam por variável de
ambiente, vindas dos GitHub Secrets. Nunca escreva chave aqui dentro.
"""
import os
import requests

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]

# service_role ignora as regras de segurança do banco — é por isso que ele
# só existe dentro do runner do GitHub, nunca no navegador.
H = {
    "apikey": KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
}


def _check(r, oque):
    if r.status_code >= 300:
        raise RuntimeError(f"{oque} falhou ({r.status_code}): {r.text[:500]}")
    return r


def pegar_job(org, worker, tipos, minutos=30):
    """Pede uma tarefa à fila. Devolve None se não houver nada para fazer."""
    r = _check(requests.post(
        f"{URL}/rest/v1/rpc/claim_job",
        headers=H, timeout=30,
        json={"p_org": org, "p_worker": worker,
              "p_types": tipos, "p_lease_minutes": minutos},
    ), "claim_job")
    return r.json() or None


def proximo_codigo(org, tipo):
    """Pede o próximo código legível: VIDEO-0001, TRANSCRIPT-0002..."""
    r = _check(requests.post(
        f"{URL}/rest/v1/rpc/next_artifact_key",
        headers=H, timeout=30,
        json={"p_org": org, "p_type": tipo},
    ), "next_artifact_key")
    return r.json()


def buscar_artefato(artifact_id):
    """Lê uma peça pelo id."""
    r = _check(requests.get(
        f"{URL}/rest/v1/artifacts?id=eq.{artifact_id}&select=*",
        headers=H, timeout=30), "buscar artefato")
    linhas = r.json()
    return linhas[0] if linhas else None


def criar_artefato(org, tipo, nivel, schema, dados, criado_por,
                   escopo="ORG", content_hash=None, snapshot=None,
                   status="APPROVED", brand_id=None):
    """Grava uma peça de trabalho e devolve o registro criado."""
    corpo = {
        "org_id": org,
        "brand_id": brand_id,
        "scope": escopo,
        "artifact_key": proximo_codigo(org, tipo),
        "type": tipo,
        "knowledge_level": nivel,
        "schema_version": schema,
        "content_hash": content_hash,
        "context_snapshot": snapshot or {},
        "data": dados,
        "created_by": criado_por,
        "status": status,
    }
    h = dict(H); h["Prefer"] = "return=representation"
    r = _check(requests.post(f"{URL}/rest/v1/artifacts",
                             headers=h, json=corpo, timeout=60), "criar artefato")
    art = r.json()[0]
    registrar_evento(org, "artifact.created", artifact_id=art["id"],
                     artifact_version=art["version"], actor_type="worker",
                     actor_id=criado_por,
                     payload={"artifact_key": art["artifact_key"], "type": tipo})
    return art


def ligar(org, de, para, relacao, metadata=None):
    """Genealogia: 'de' veio de 'para'. Ex.: VIDEO derived_from SOURCE."""
    _check(requests.post(
        f"{URL}/rest/v1/artifact_links", headers=H, timeout=30,
        json={"org_id": org,
              "from_artifact_id": de["id"],   "from_version": de["version"],
              "to_artifact_id":   para["id"], "to_version":   para["version"],
              "relation": relacao, "metadata": metadata or {}},
    ), "ligar artefatos")


def registrar_evento(org, tipo, artifact_id=None, artifact_version=None,
                     job_id=None, actor_type="worker", actor_id=None, payload=None):
    """O diário de bordo. Nunca se apaga, nunca se reescreve."""
    _check(requests.post(
        f"{URL}/rest/v1/events", headers=H, timeout=30,
        json={"org_id": org, "event_type": tipo,
              "artifact_id": artifact_id, "artifact_version": artifact_version,
              "job_id": job_id, "actor_type": actor_type, "actor_id": actor_id,
              "payload": payload or {}},
    ), "registrar evento")


def enfileirar(org, tipo, payload, idempotency_key, prioridade=100):
    """Cria a próxima tarefa. Chave repetida = ignora, não duplica."""
    h = dict(H)
    h["Prefer"] = "return=representation,resolution=ignore-duplicates"
    r = requests.post(f"{URL}/rest/v1/jobs", headers=h, timeout=30,
                      json={"org_id": org, "job_type": tipo, "payload": payload,
                            "idempotency_key": idempotency_key,
                            "priority": prioridade})
    if r.status_code >= 300:
        raise RuntimeError(f"enfileirar falhou ({r.status_code}): {r.text[:500]}")
    criado = r.json()
    if criado:
        registrar_evento(org, "job.queued", job_id=criado[0]["id"],
                         actor_type="system", payload={"job_type": tipo})
    return criado[0] if criado else None


def terminar_job(org, job_id, erro=None):
    """Fecha a tarefa. Sem erro = pronto; com erro = volta para a fila."""
    if erro is None:
        corpo = {"status": "COMPLETED", "completed_at": "now()",
                 "lease_expires_at": None}
        evento = "job.completed"
    else:
        # Devolve para a fila. Se estourar as tentativas, o claim_job
        # marca como FAILED na próxima passagem.
        corpo = {"status": "QUEUED", "claimed_by": None, "claimed_at": None,
                 "lease_expires_at": None, "last_error": str(erro)[:2000]}
        evento = "job.failed"

    _check(requests.patch(f"{URL}/rest/v1/jobs?id=eq.{job_id}",
                          headers=H, json=corpo, timeout=30), "terminar job")
    registrar_evento(org, evento, job_id=job_id, actor_type="worker",
                     payload={"erro": str(erro)[:500]} if erro else {})
