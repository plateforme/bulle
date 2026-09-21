"""Le jeton partagé qui protège le cerveau (BA-4).

Le cerveau écoute sur 0.0.0.0 et pilote une maison habitée : allumer, éteindre, jouer, écrire dans le CRM,
et changer des réglages qui partent dans git. Ces tests vérifient qu'on ne peut rien faire de tout ça sans
le secret — et qu'on peut tout faire avec.
"""
import pytest
from fastapi.testclient import TestClient

import regles
import server

JETON = "jeton-de-test-Xy9"


@pytest.fixture
def avec_jeton(monkeypatch):
    monkeypatch.setenv("BULLE_JETON", JETON)
    return JETON


@pytest.fixture
def client(avec_jeton):
    with TestClient(server.app) as c:
        yield c


def entete(j=JETON):
    return {"Authorization": "Bearer " + j}


# ---------------------------------------------------------------- lecture du secret
def test_le_jeton_vient_de_l_environnement(avec_jeton):
    assert regles.jeton() == JETON


def test_le_jeton_vient_du_fichier_si_l_environnement_est_muet(monkeypatch, tmp_path):
    """C'est ce chemin-là qui fait marcher la boucle de nuit : son unité systemd n'a pas d'EnvironmentFile."""
    monkeypatch.delenv("BULLE_JETON", raising=False)
    fichier = tmp_path / "jeton"
    fichier.write_text("  secret-du-fichier\n", encoding="utf-8")   # espaces et retour à la ligne compris
    monkeypatch.setattr(regles, "JETON_FICHIER", str(fichier))
    assert regles.jeton() == "secret-du-fichier"


def test_sans_secret_configure_rien_n_est_valide(monkeypatch, tmp_path):
    """Le piège à éviter : un jeton vide qui validerait une requête sans en-tête."""
    monkeypatch.delenv("BULLE_JETON", raising=False)
    monkeypatch.setattr(regles, "JETON_FICHIER", str(tmp_path / "absent"))
    assert regles.jeton() == ""
    assert regles.jeton_valide("") is False
    assert regles.jeton_valide(None) is False
    assert regles.jeton_valide("n'importe quoi") is False


@pytest.mark.parametrize("presente, attendu", [
    (JETON, True),
    ("Bearer " + JETON, True),
    ("bearer " + JETON, True),
    ("  Bearer   " + JETON + "  ", True),
    (JETON + "x", False),
    (JETON[:-1], False),
    ("", False),
    (None, False),
])
def test_formes_acceptees_de_l_entete(avec_jeton, presente, attendu):
    assert regles.jeton_valide(presente) is attendu


# ---------------------------------------------------------------- démarrage
def test_le_cerveau_refuse_de_demarrer_sans_jeton(monkeypatch, tmp_path):
    """« Démarrer ouvert sur le réseau » n'est pas un repli acceptable : on refuse de démarrer."""
    monkeypatch.delenv("BULLE_JETON", raising=False)
    monkeypatch.setattr(regles, "JETON_FICHIER", str(tmp_path / "absent"))
    with pytest.raises(RuntimeError, match="refuse de démarrer"):
        with TestClient(server.app):
            pass


# ---------------------------------------------------------------- /health reste ouvert
def test_health_reste_accessible_sans_jeton(client, monkeypatch):
    """C'est la sonde de deployer_pi.sh et de l'analyste : la fermer casserait le déploiement.

    On neutralise le chargement du catalogue : il irait interroger cinq serveurs d'outils et Open WebUI, et
    aucun test unitaire de ce dépôt ne touche au réseau (c'est 14 s à lui seul sinon).
    """
    async def rien(*a, **kw):
        pass
    monkeypatch.setattr(server.tools, "load", rien)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------- WebSocket
def test_le_websocket_refuse_une_connexion_sans_jeton(client):
    """Sans ça, n'importe quel appareil du réseau peut envoyer {"type":"text"} et faire agir la maison."""
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass


def test_le_websocket_refuse_un_mauvais_jeton(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers=entete("pas-le-bon")):
            pass


def test_le_websocket_accepte_le_bon_jeton(client):
    with client.websocket_connect("/ws", headers=entete()) as ws:
        assert ws is not None


# ---------------------------------------------------------------- plans
def test_un_plan_ne_se_telecharge_pas_sans_jeton(client):
    """Les plans disent les lieux que Greg a demandés : ça ne se sert pas à tout le réseau."""
    assert client.get("/plan/0123456789abcdef.png").status_code == 401


# ---------------------------------------------------------------- page de suivi
def test_la_page_de_suivi_demande_le_jeton(client):
    r = client.get("/suivi")
    assert r.status_code == 401
    assert "jeton" in r.text.lower()


def test_la_page_de_suivi_s_ouvre_avec_l_entete(client):
    r = client.get("/suivi", headers=entete())
    assert r.status_code == 200
    assert "Suivi de" in r.text


def test_la_connexion_pose_un_cookie_et_la_page_s_ouvre(client):
    r = client.post("/suivi/connexion", data={"jeton": JETON})
    assert r.status_code == 200                       # suivi de la redirection par le client de test
    assert client.cookies.get("bulle_jeton")
    assert "Suivi de" in r.text


def test_un_mauvais_jeton_a_la_connexion_est_refuse(client):
    r = client.post("/suivi/connexion", data={"jeton": "pas-le-bon"})
    assert r.status_code == 401
    assert "refusé" in r.text
    assert not client.cookies.get("bulle_jeton")


def test_on_ne_peut_pas_ecrire_un_reglage_sans_jeton(client):
    """Le pire de la page : un réglage écrit ici part dans config/regles.yaml ET dans un commit git."""
    r = client.post("/suivi/reglage", data={"nom": "duree des cartes", "valeur": "99"})
    assert r.status_code == 401


def test_on_ne_peut_pas_noter_un_test_sans_jeton(client):
    r = client.post("/suivi/test/1", data={"statut": "ok", "commentaire": ""})
    assert r.status_code == 401
