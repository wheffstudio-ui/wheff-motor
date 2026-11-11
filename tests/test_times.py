"""Times de agentes: contrato, lacunas sem invenção e etapa obrigatória. python tests/test_times.py"""
import json, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worker"))
os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co"); os.environ.setdefault("SUPABASE_SERVICE_KEY", "k")
import times

AQUI = os.path.dirname(os.path.abspath(__file__))
PB = json.load(open(os.path.join(AQUI, "..", "..", "wheff-plataforma", "playbooks", "publico-alvo.json"), encoding="utf-8")) \
    if os.path.exists(os.path.join(AQUI, "..", "..", "wheff-plataforma", "playbooks", "publico-alvo.json")) else \
    {"playbook_id": "PLAYBOOK-PUBLICO", "nome": "Público", "etapas": [{"id": "a", "nome": "A", "obrigatoria": True, "perguntas": ["Quem compra?"]}]}
MARCA = {"id": "m", "version": 2, "artifact_key": "BRAND-PROFILE-0001", "data": {"nome": "Marca X"}}


def buscar_com(tipos):
    return lambda org, t, status=None: [dict(MARCA, artifact_key=t.upper())] if t in tipos else []


def modelo_que_responde(pular=None, sem_base=False):
    def chamar(sis, usu):
        bloco = json.loads(usu.split("═══ ETAPAS DESTE BLOCO ═══\n")[1])
        return {"etapas": [{"id": e["id"], "resultado": "ok",
                            "respostas": [{"pergunta": q, "resposta": times.SEM_BASE if sem_base else "Mulheres 30+",
                                           "base": "" if sem_base else "brand_profile.nome", "confianca": 0.8}
                                          for q in (e.get("perguntas") or ["?"])]}
                           for e in bloco if e["id"] != pular]}
    return chamar


class TesteTimes(unittest.TestCase):
    def rodar(self, chamar, tipos=("brand_profile",)):
        criado, links = {}, []
        def criar(org, tipo, nivel, schema, escopo, status, criado_por, dados, snapshot):
            criado.update(tipo=tipo, status=status, dados=dados); return {"id": "n", "version": 1, "artifact_key": "X-0001"}
        times.executar({"payload": {"time": "audience_research"}}, chamar=chamar, buscar=buscar_com(tipos),
                       ler=lambda pid: (PB, 3), criar=criar, ligar=lambda *a: links.append(a))
        return criado, links

    def test_falta_entrada_nao_gasta_token(self):
        def nunca(*a): raise AssertionError("chamou a IA sem a entrada")
        with self.assertRaises(RuntimeError) as c:
            self.rodar(nunca, tipos=())
        self.assertIn("brand_profile aprovado", str(c.exception))

    def test_gera_para_aprovacao_com_genealogia(self):
        c, links = self.rodar(modelo_que_responde())
        self.assertEqual(c["tipo"], "audience_profile"); self.assertEqual(c["status"], "AWAITING_APPROVAL")
        self.assertEqual(len(c["dados"]["etapas"]), len(PB["etapas"]))
        self.assertEqual(c["dados"]["lacunas"], [])
        self.assertTrue(any("Gerado sem market_research" in l for l in c["dados"]["limitacoes"]))
        self.assertEqual(links[0][3], "derived_from")

    def test_sem_base_vira_lacuna_com_confianca_zero(self):
        c, _ = self.rodar(modelo_que_responde(sem_base=True))
        self.assertTrue(c["dados"]["lacunas"]); self.assertEqual(c["dados"]["confidence"], 0.0)

    def test_etapa_obrigatoria_pulada_recusa(self):
        obrig = next(e for e in PB["etapas"] if e.get("obrigatoria"))
        with self.assertRaises(RuntimeError):
            self.rodar(modelo_que_responde(pular=obrig["id"]))

    def test_time_desconhecido(self):
        with self.assertRaises(RuntimeError):
            times.executar({"payload": {"time": "nao_existe"}})

    def test_todo_time_tem_playbook_com_etapas(self):
        pasta = os.path.join(AQUI, "..", "..", "wheff-plataforma", "playbooks")
        if not os.path.isdir(pasta):
            self.skipTest("sem a pasta da plataforma ao lado")
        ids = {}
        for f in os.listdir(pasta):
            d = json.load(open(os.path.join(pasta, f), encoding="utf-8")); ids[d.get("playbook_id")] = len(d.get("etapas") or [])
        for tid, cfg in times.TIMES.items():
            self.assertGreater(ids.get(cfg["playbook"], 0), 0, tid)


if __name__ == "__main__":
    unittest.main()
