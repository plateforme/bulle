"""Les signaux non verbaux joints à un énoncé : ce qu'on en accepte, et de qui on les garde.

Ce sont les premières mesures qui décriront un énoncé sans le transcrire — durée, niveau, probabilité de
parole, retards entre micros, angle et distance de la Kinect. Elles préparent la détection « est-ce que ça
m'était adressé » (la littérature l'appelle *device-directed speech detection*, et elle marche mal sans une
modalité non verbale). Deux choses doivent tenir avant tout le reste :

  - **rien de textuel ne peut entrer**, même si une version future du Pi en envoyait par erreur ;
  - **le salon n'est pas journalisé** : d'un énoncé qui ne s'adressait visiblement pas à Bulle, il ne reste
    toujours que l'heure, la longueur et la raison. Les signaux ne sont gardés que pour les ratés de peu.
"""
import json

import pytest

import journal
import regles
import server


def vider():
    journal.ecrire("delete from echanges")


@pytest.fixture(autouse=True)
def base_propre():
    vider()
    yield
    vider()


PI = {"duree_s": 1.84, "niveau": 812.0, "vad": 0.93, "nettete": [6.1, 5.4, 4.9],
      "retards": [-1.5, 2.0, 0.5], "angle_kinect": -12.0, "distance_m": 2.4, "presence": True}


# ---------------------------------------------------------------- rien de textuel ne passe
def test_seules_les_cles_connues_sont_acceptees():
    propres = server._signaux_propres(dict(PI, texte="ferme la lumière", brut="tu veux du café ?"))
    assert set(propres) <= set(server.SIGNAUX)
    assert "texte" not in propres and "brut" not in propres


def test_une_cle_connue_qui_porte_du_texte_est_jetee():
    """Le Pi est de confiance, mais ce champ vit des jours en base : on ne laisse passer que des nombres."""
    propres = server._signaux_propres({"duree_s": "Bulle, ferme la lumière", "niveau": 800.0})
    assert "duree_s" not in propres
    assert propres["niveau"] == 800.0


def test_une_liste_qui_contient_autre_chose_que_des_nombres_est_jetee():
    assert "retards" not in server._signaux_propres({"retards": [1.0, "deux", 3.0]})
    assert server._signaux_propres({"retards": [1.0, 2.0]})["retards"] == [1.0, 2.0]


def test_les_booleens_deviennent_des_entiers():
    """SQLite et Prometheus s'accommodent mal d'un vrai booléen JSON ; et 0/1 se lit aussi bien."""
    assert server._signaux_propres({"presence": True})["presence"] == 1
    assert server._signaux_propres({"presence": False})["presence"] == 0


def test_ce_qui_n_est_pas_un_dictionnaire_ne_donne_rien():
    for n_importe_quoi in (None, "Bulle ferme la lumière", 42, ["a"]):
        assert server._signaux_propres(n_importe_quoi) == {}


def test_les_signaux_du_pi_passent_en_entier():
    assert server._signaux_propres(PI) == {k: (1 if v is True else v) for k, v in PI.items()}


# ---------------------------------------------------------------- de qui on garde ces signaux
def test_une_conversation_du_salon_ne_laisse_aucun_signal():
    """Le cœur de la règle : la géométrie de qui parlait, d'où et combien de temps en dirait déjà trop."""
    assert server._signaux_a_garder("pas_nomme", proche=None, depuis_reponse=600) is False


def test_un_premier_mot_qui_ressemble_au_nom_est_un_rate_de_peu():
    assert server._signaux_a_garder("pas_nomme", proche="boule", depuis_reponse=600) is True


def test_un_enonce_juste_apres_une_reponse_est_un_rate_de_peu():
    """La fenêtre venait de se fermer : c'est exactement l'incident « ignoré juste après une réponse »."""
    assert server._signaux_a_garder("pas_nomme", proche=None, depuis_reponse=5) is True
    assert server._signaux_a_garder("pas_nomme", proche=None, depuis_reponse=31) is False


def test_une_hallucination_garde_ses_signaux():
    """Du bruit que Whisper a mis en mots : personne ne parlait à personne, et le niveau dit pourquoi."""
    assert server._signaux_a_garder("hallucination", proche=None, depuis_reponse=600) is True


def test_le_reglage_coupe_tout(monkeypatch):
    vrai = regles.c
    monkeypatch.setattr(regles, "c", lambda cle, defaut=None: (
        {"actif": False} if cle == "signaux_non_verbaux" else vrai(cle, defaut)))
    assert server._signaux_a_garder("pas_nomme", proche="boule", depuis_reponse=1) is False
    assert server._signaux_a_garder("hallucination", proche=None, depuis_reponse=1) is False


# ---------------------------------------------------------------- ce qui arrive en base
def test_un_enonce_ignore_sans_signaux_reste_aussi_pauvre_qu_avant():
    journal.ignore("pas_nomme", longueur=42, premier_mot=None, signaux=None)
    ligne = journal.lire("select * from echanges")[0]
    assert ligne["signaux"] is None
    assert ligne["brut"] is None and ligne["texte"] is None


def test_un_rate_de_peu_garde_ses_signaux_en_json():
    journal.ignore("pas_nomme", longueur=42, premier_mot="boule", signaux=server._signaux_propres(PI))
    ligne = journal.lire("select * from echanges")[0]
    relu = json.loads(ligne["signaux"])
    assert relu["angle_kinect"] == -12.0 and relu["vad"] == 0.93
    assert ligne["brut"] is None and ligne["texte"] is None      # les signaux n'ouvrent pas la porte au texte


def test_l_effacement_de_nuit_ne_touche_pas_aux_signaux():
    """`effacer_textes_ignores` vise le texte. Les signaux sont des nombres : ils partent avec la durée de vie
    des énoncés ignorés (sept jours), pas avec cet effacement-là."""
    journal.ignore("pas_nomme", longueur=42, premier_mot="boule", signaux={"duree_s": 1.5})
    journal.effacer_textes_ignores()
    assert json.loads(journal.lire("select * from echanges")[0]["signaux"])["duree_s"] == 1.5


def test_un_echange_adresse_a_bulle_garde_ses_signaux():
    """Un échange nommé est déjà journalisé en entier : les signaux n'y ajoutent aucun risque, et c'est là que
    se trouvent les exemples POSITIFS dont toute détection aura besoin."""
    journal.echange(source="voix", texte="ferme la lumière", nomme=1, reponse="c'est fait",
                    signaux=server._signaux_propres(PI))
    ligne = journal.lire("select * from echanges")[0]
    assert json.loads(ligne["signaux"])["distance_m"] == 2.4
    assert ligne["texte"] == "ferme la lumière"
