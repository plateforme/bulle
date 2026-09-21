"""La chorégraphie GPU de la boucle de nuit (agent/boucle.py, main).

Une seule carte, trois acteurs qui s'en disputent la place sans se parler : la boucle veut 17,7 Go pour le
modèle de l'agent, `ia-warmup` en épingle 13,8 pour celui de Bulle **toutes les 30 minutes**, et l'arbitre VRAM
redémarre la synthèse vocale quand ça sature. Dans la nuit du 21/09, Ollama n'a chargé que 32 des 66 couches de
l'agent : la moitié du modèle sur le processeur, et 5 chantiers sur 6 expirés à 1200 s.

Ces cas fixent l'ordre des opérations. Rien n'y touche au vrai GPU ni à systemd : `run` est capturé.
"""
import os
import sys
import time

import pytest

DEPOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(DEPOT, "agent"))
sys.path.insert(0, os.path.join(DEPOT, "cerveau"))

import boucle  # noqa: E402
import journal  # noqa: E402


@pytest.fixture
def nuit(monkeypatch):
    """Une nuit complète, sans GPU, sans systemd, sans agent : on n'observe que les commandes lancées."""
    journal.ecrire("delete from incidents")
    journal.ecrire("delete from actions")
    # un incident traitable, sinon la boucle n'a rien à faire et saute tout le bloc
    import datetime
    journal.ecrire("insert into incidents(ts, nuit, type, gravite, cle, resume, detail) values(?,?,?,?,?,?,?)",
                   (time.time(), datetime.date.today().isoformat(), "outil_erreur", "haute", "erreur:essai",
                    "quelque chose a échoué", "{}"))

    commandes, chantiers = [], []
    monkeypatch.setattr(boucle, "run", lambda cmd, **kw: (commandes.append(" ".join(map(str, cmd))), (0, ""))[1])
    monkeypatch.setattr(boucle, "banc", lambda *a, **kw: {"reussis": 46, "total": 46, "cas": []})
    monkeypatch.setattr(boucle, "exploitation", lambda nuit: None)
    monkeypatch.setattr(boucle, "rechauffer_bulle", lambda: commandes.append("rechauffer_bulle"))
    monkeypatch.setattr(boucle, "chantier", lambda *a, **kw: chantiers.append(a[0]["cle"]) or "pret_pour_revue")
    # « --fin 23:59 » ne se déclenche jamais (la garde « avant midi » de la boucle l'en empêche) : sans ça, ces
    # tests échouaient chaque matin entre 6 h 30 et midi, heure de Montréal, où la vraie boucle s'arrête. Un test
    # qui dépend de l'heure qu'il est finit par être ignoré, et c'est une suite entière qui ne sert plus à rien.
    monkeypatch.setattr(sys, "argv", ["boucle.py", "--essai", "--max", "1", "--fin", "23:59"])
    monkeypatch.setattr(boucle.time, "sleep", lambda s: None)   # la vraie boucle attend 6 s que la carte se vide

    def lancer(vram_libre):
        monkeypatch.setattr(boucle, "_vram_libre", lambda: vram_libre)
        commandes.clear(); chantiers.clear()
        boucle.main()
        return list(commandes), list(chantiers)   # des copies : deux appels dans un même test se marcheraient dessus

    return lancer


def rang(commandes, fragment):
    """Position de la première commande contenant ce fragment, ou -1."""
    return next((i for i, c in enumerate(commandes) if fragment in c), -1)


# ---------------------------------------------------------------- la minuterie qui rallumait le modèle
def test_la_minuterie_de_prechauffage_est_arretee_avant_les_chantiers(nuit):
    """C'est LA cause des 5 chantiers expirés : décharger le modèle de Bulle ne sert à rien si sa minuterie
    le remet 30 minutes plus tard, en plein milieu d'un chantier de 20 minutes."""
    commandes, chantiers = nuit(vram_libre=22000)
    arret = rang(commandes, "stop ia-warmup.timer")
    assert arret >= 0, "la minuterie n'est jamais arrêtée"
    assert arret < rang(commandes, "keep_alive"), "elle doit être arrêtée AVANT de décharger le modèle"
    assert chantiers, "avec la place nécessaire, les chantiers doivent avoir lieu"


def test_la_minuterie_est_toujours_remise_a_la_fin(nuit):
    commandes, _ = nuit(vram_libre=22000)
    assert rang(commandes, "start ia-warmup.timer") >= 0


def test_elle_est_remise_apres_le_rechauffage_pas_avant(nuit):
    """Sinon elle repart pendant qu'on recharge le modèle de Bulle, et les deux se gênent."""
    commandes, _ = nuit(vram_libre=22000)
    assert rang(commandes, "rechauffer_bulle") < rang(commandes, "start ia-warmup.timer")


def test_la_synthese_vocale_est_rendue_avant_la_minuterie(nuit):
    commandes, _ = nuit(vram_libre=22000)
    assert rang(commandes, "start kyutai-tts") < rang(commandes, "start ia-warmup.timer")


# ---------------------------------------------------------------- ne pas commencer une nuit perdue d'avance
def test_sans_la_place_on_n_entame_aucun_chantier(nuit):
    """Sans VRAM, Ollama met le modèle sur le processeur : chaque chantier expire au bout de 20 min.
    Dix incidents, c'est trois heures de nuit brûlées pour rien. Autant le dire tout de suite."""
    commandes, chantiers = nuit(vram_libre=9000)
    assert chantiers == []
    assert any("nuit annulée" in (a["titre"] or "") for a in journal.lire("select * from actions"))


def test_meme_annulee_la_nuit_rend_ce_qu_elle_a_pris(nuit):
    """Le plus important : si on renonce, il ne faut surtout pas laisser Bulle sans voix au réveil."""
    commandes, _ = nuit(vram_libre=9000)
    assert rang(commandes, "start kyutai-tts") >= 0
    assert rang(commandes, "start ia-warmup.timer") >= 0
    assert rang(commandes, "rechauffer_bulle") >= 0


def test_le_seuil_est_celui_du_modele_de_l_agent(nuit):
    """Juste en dessous : on renonce. Juste au-dessus : on y va."""
    _, sans = nuit(vram_libre=boucle.VRAM_AGENT_MIO - 1)
    _, avec = nuit(vram_libre=boucle.VRAM_AGENT_MIO)
    assert sans == [] and avec != []
