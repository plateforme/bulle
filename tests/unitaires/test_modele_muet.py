"""Ce que Bulle fait quand le modèle ne répond pas (cerveau/server.py, answer).

Le 21/09, une indexation lancée par quelqu'un d'autre a pris les huit cœurs de la VM pendant plusieurs minutes.
La question de Greg a dépassé le délai de 120 s et le cerveau a laissé filer l'exception : le client n'a reçu
qu'un `error` vide, a affiché une mine désolée, et l'a gardée. Greg : « Bulle semble être coincée », puis
« il fait la gueule depuis tout à l'heure ». Une panne se dit — elle ne se mime pas en silence.
"""
import asyncio
import json

import httpx
import pytest

import mesures
import server


class FausseWS:
    """Recueille ce que le cerveau envoie au salon."""

    def __init__(self):
        self.envois = []

    async def send_text(self, t):
        self.envois.append(json.loads(t))

    async def send_bytes(self, b):
        self.envois.append({"type": "__audio__", "octets": len(b)})


def repondre(monkeypatch, panne):
    """Joue un échange dont l'appel au modèle lève `panne` → (messages envoyés au client, exception ou None)."""
    ws = FausseWS()
    s = server.Session(ws)
    s.sim = True                                  # pas de synthèse vocale : on teste le cerveau, pas Kyutai

    def stream_qui_tombe(*a, **kw):
        raise panne

    monkeypatch.setattr(server.tools, "load", lambda: asyncio.sleep(0))
    monkeypatch.setattr(server.http, "stream", stream_qui_tombe)
    monkeypatch.setattr(server, "system_prompt", lambda sim: asyncio.sleep(0, result="consigne"))
    monkeypatch.setattr(server.journal, "echange", lambda **kw: 1)
    erreur = None
    try:
        asyncio.run(s.answer("quelle est la météo ?", source="voix"))
    except Exception as e:                        # ce qui remontait avant le correctif
        erreur = e
    return ws.envois, erreur


def phrases(envois):
    return " ".join(e.get("text", "") for e in envois if e.get("type") == "sentence")


@pytest.fixture(autouse=True)
def compteurs_neufs():
    mesures._compteurs.clear()
    yield
    mesures._compteurs.clear()


def test_un_modele_qui_ne_repond_pas_ne_laisse_plus_le_salon_sans_reponse(monkeypatch):
    envois, erreur = repondre(monkeypatch, httpx.ReadTimeout("timed out"))
    assert erreur is None, f"l'exception est remontée jusqu'au client : {erreur!r}"
    assert phrases(envois).strip(), "Bulle n'a rien dit du tout"


def test_elle_dit_que_la_machine_est_prise(monkeypatch):
    """Le mot compte : Greg doit comprendre que ce n'est ni lui, ni une question mal posée."""
    envois, _ = repondre(monkeypatch, httpx.ReadTimeout("timed out"))
    dit = phrases(envois).lower()
    assert "débordée" in dit or "prise" in dit, dit


def test_l_echange_se_termine_proprement(monkeypatch):
    """Sans `done`, le client resterait en attente et le visage en « thinking »."""
    envois, _ = repondre(monkeypatch, httpx.ReadTimeout("timed out"))
    assert [e for e in envois if e.get("type") == "done"]


def test_le_visage_prend_la_mine_de_la_panne(monkeypatch):
    envois, _ = repondre(monkeypatch, httpx.ReadTimeout("timed out"))
    assert {"type": "emotion", "emotion": "desole"} in envois


def test_une_coupure_reseau_est_traitee_comme_un_depassement(monkeypatch):
    """Ollama redémarré pendant un échange donne une ConnectError, pas un timeout : même réflexe attendu."""
    envois, erreur = repondre(monkeypatch, httpx.ConnectError("connection refused"))
    assert erreur is None and phrases(envois).strip()


def test_l_echec_est_compte_dans_les_mesures(monkeypatch):
    """Sinon le tableau de bord montre une Bulle en pleine forme pendant qu'elle rate tout."""
    repondre(monkeypatch, httpx.ReadTimeout("timed out"))
    erreurs = [v for (nom, etiq), v in mesures._compteurs.items()
               if nom == "bulle_etape_erreurs_total" and ("etape", "reflexion") in etiq]
    assert erreurs == [1.0]
