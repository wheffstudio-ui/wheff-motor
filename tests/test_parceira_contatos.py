import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))
import parceira_contatos as pc  # noqa: E402


def test_whatsapp_link():
    r = pc.extrair_contatos("fale comigo https://wa.me/5511987654321 ok", "u")
    assert r[0]["tipo"] == "whatsapp" and r[0]["valor"] == "+5511987654321"


def test_api_whatsapp_e_numero_escrito_sem_duplicar():
    t = "api.whatsapp.com/send?phone=5521998765432&text=oi · Contato (21) 99876-5432"
    r = pc.extrair_contatos(t, "u")
    assert [c["tipo"] for c in r] == ["whatsapp"]


def test_celular_escrito_e_regiao():
    r = pc.extrair_contatos("Agenda: +55 31 98888-7777", "u")
    assert r[0]["tipo"] == "celular" and r[0]["valor"] == "+5531988887777"


def test_numeros_soltos_ignorados():
    assert pc.extrair_contatos("em 2024 vendi 1234567890 copias; CEP 01310-100", "u") == []


def test_email_e_lixo():
    r = pc.extrair_contatos("parcerias: Ana.Lima@gmail.com · logo@2x.png · a@example.com", "u")
    assert [c["valor"] for c in r] == ["ana.lima@gmail.com"]


def test_juntar_conta_fontes():
    a = pc.extrair_contatos("wa.me/5511987654321", "p1") + pc.extrair_contatos("wa.me/5511987654321", "p2")
    j = pc.juntar(a)
    assert len(j) == 1 and j[0]["fontes"] == ["p1", "p2"]


def test_usuario_invalido():
    import pytest
    with pytest.raises(ValueError):
        pc._usuario_ok("a b; rm")
    assert pc._usuario_ok("https://instagram.com/ana.lima/") == "ana.lima"


def test_gravar_so_no_pedido_certo(monkeypatch):
    import pytest
    os.environ.setdefault("SUPABASE_URL", "http://x")
    os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
    chamadas = []

    class R:
        status_code = 200
        text = "[]"
        def json(self):
            return []

    def patch(url, params=None, **k):
        chamadas.append(params)
        return R()
    monkeypatch.setattr(pc.requests, "patch", patch)
    with pytest.raises(ValueError):
        pc.gravar("nao-e-uuid", "ana", {})
    assert chamadas == []
    with pytest.raises(ValueError):   # 0 linhas: outro @ ou fora da fila
        pc.gravar("123e4567-e89b-12d3-a456-426614174000", "ana", {"status": "PRONTO"})
    assert chamadas[0] == {"id": "eq.123e4567-e89b-12d3-a456-426614174000", "status": "eq.FILA", "usuario": "eq.ana"}
