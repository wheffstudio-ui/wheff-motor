"""
Conversa com o banco da Wheff (Supabase).

Este arquivo NÃO contém segredo nenhum. As chaves chegam por variável de
ambiente, vindas dos GitHub Secrets. Nunca escreva chave aqui dentro.
"""
import os
from datetime import datetime, timedelta, timezone

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
    # Fila vazia: o Postgres devolve uma LINHA com todos os campos nulos,
    # não um "nada". Em Python isso é um dict verdadeiro, então checar
    # `if not job` no chamador não bastava — tem que olhar o id.
    if r.status_code == 204 or not (r.text or "").strip():
        return None
    job = r.json()
    if not job or not job.get("id"):
        return None
    return job


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


def buscar_por_hash(org, tipo, content_hash):
    """
    A peça que já existe com esta impressão digital (o mesmo arquivo).
    Reprocessar uma tarefa que morreu no meio reaproveita o que ficou pronto
    em vez de criar outro VIDEO igual. Fase 7, 18/10/2026.
    """
    r = _check(requests.get(
        f"{URL}/rest/v1/artifacts?org_id=eq.{org}&type=eq.{tipo}"
        f"&content_hash=eq.{content_hash}&is_current=is.true&select=*"
        f"&order=created_at.asc&limit=1",
        headers=H, timeout=30), "buscar por impressão digital")
    linhas = r.json()
    return linhas[0] if linhas else None


def buscar_por_tipo(org, tipo, status=None, apenas_atual=True):
    """
    Peças de um tipo. Serve para o contrato de dependência: o worker
    pergunta ao banco se o que ele exige existe APROVADO, em vez de
    supor que existe porque alguém disse que criou.
    """
    q = f"{URL}/rest/v1/artifacts?org_id=eq.{org}&type=eq.{tipo}&select=*"
    if apenas_atual:
        q += "&is_current=is.true"
    if status:
        q += f"&status=eq.{status}"
    r = _check(requests.get(q, headers=H, timeout=30), "buscar por tipo")
    return r.json()


def derivados_de(artifact_id, tipo):
    """
    Peças de um tipo que já nasceram desta origem.

    Serve para uma tarefa poder ser repetida sem duplicar trabalho: se a
    execução anterior morreu no meio, o que já foi feito é reaproveitado
    em vez de recriado.
    """
    r = _check(requests.get(
        f"{URL}/rest/v1/artifact_links"
        f"?to_artifact_id=eq.{artifact_id}&relation=eq.derived_from"
        f"&select=from_artifact_id",
        headers=H, timeout=30), "buscar derivados")
    ids = [x["from_artifact_id"] for x in r.json()]
    if not ids:
        return []
    lista = ",".join(ids)
    r = _check(requests.get(
        f"{URL}/rest/v1/artifacts?id=in.({lista})&type=eq.{tipo}&select=*",
        headers=H, timeout=30), "buscar artefatos derivados")
    return r.json()


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
    """Genealogia: 'de' veio de 'para'. Ex.: VIDEO derived_from SOURCE.
    Ligação repetida (reprocessamento) é ignorada, não dá erro."""
    h = dict(H); h["Prefer"] = "resolution=ignore-duplicates"
    _check(requests.post(
        f"{URL}/rest/v1/artifact_links?on_conflict=from_artifact_id,to_artifact_id,relation",
        headers=h, timeout=30,
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


def espera_minutos(tentativas):
    """Espera antes da próxima tentativa: 2, 4, 8, 16… minutos (teto 6 h)."""
    return min(2 ** max(1, int(tentativas or 1)) , 360)


def terminar_job(org, job_id, erro=None, definitivo=False):
    """
    Fecha a tarefa.
    • Sem erro: pronta.
    • Com erro: volta para a fila COM espera (2, 4, 8 min…). Esgotadas as
      tentativas — ou erro definitivo, como vídeo privado — fica FAILED e
      para de gastar execução. Antes de 18/10/2026 voltava para a fila na
      hora e sem limite: uma tarefa quebrada tentava a cada 10 min para sempre.
    """
    agora = datetime.now(timezone.utc)
    if erro is None:
        corpo = {"status": "COMPLETED", "completed_at": agora.isoformat(),
                 "lease_expires_at": None}
        evento = "job.completed"
    else:
        r = _check(requests.get(
            f"{URL}/rest/v1/jobs?id=eq.{job_id}&select=attempts,max_attempts",
            headers=H, timeout=30), "ler tentativas")
        linha = (r.json() or [{}])[0]
        tent, maximo = int(linha.get("attempts") or 0), int(linha.get("max_attempts") or 3)
        acabou = definitivo or tent >= maximo
        corpo = {"claimed_by": None, "claimed_at": None, "lease_expires_at": None,
                 "last_error": str(erro)[:2000]}
        if acabou:
            corpo["status"] = "FAILED"
        else:
            corpo["status"] = "QUEUED"
            corpo["available_at"] = (agora + timedelta(minutes=espera_minutos(tent))).isoformat()
        evento = "job.failed" if acabou else "job.retry"

    _check(requests.patch(f"{URL}/rest/v1/jobs?id=eq.{job_id}",
                          headers=H, json=corpo, timeout=30), "terminar job")
    registrar_evento(org, evento, job_id=job_id, actor_type="worker",
                     payload={"erro": str(erro)[:500], "definitivo": bool(definitivo)} if erro else {})
    return corpo["status"]


def etapa(org, job_id, nome, minutos=30):
    """
    Batimento + etapa. Renova a reserva da tarefa (quem morrer no meio volta
    para a fila quando a reserva vencer) e registra em que passo está, com
    horário, no diário (events). A plataforma mostra a última etapa.
    Falhar aqui não derruba a tarefa: é registro, não trabalho.
    """
    try:
        lease = (datetime.now(timezone.utc) + timedelta(minutes=minutos)).isoformat()
        requests.patch(f"{URL}/rest/v1/jobs?id=eq.{job_id}&status=eq.RUNNING",
                       headers=H, json={"lease_expires_at": lease}, timeout=30)
        registrar_evento(org, "job.stage", job_id=job_id, actor_type="worker",
                         payload={"etapa": nome})
        print(f"  · {nome}")
    except Exception as e:  # noqa: BLE001
        print(f"  (não registrei a etapa '{nome}': {e})")
