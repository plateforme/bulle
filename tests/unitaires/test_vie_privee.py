"""Ce qu'on garde d'une phrase qui n'était pas pour Bulle — et pendant combien de temps.

Le salon est une pièce habitée : le micro entend des conversations entières qui ne s'adressent pas à Bulle.
Ces tests vérifient qu'il n'en reste rien de lisible, et que l'analyste garde malgré tout de quoi travailler.
"""
import os
import time

import pytest

import journal
import server


def vider():
    journal.ecrire("delete from echanges")
    journal.ecrire("delete from incidents")
    journal.ecrire("delete from actions")
    journal.ecrire("delete from a_tester")
    journal.ecrire("delete from retours")


@pytest.fixture(autouse=True)
def base_propre():
    vider()
    yield
    vider()


# ---------------------------------------------------------------- premier mot : seulement s'il vise Bulle
@pytest.mark.parametrize("phrase, attendu", [
    ("Boule, ferme la lumière.", "boule"),          # Whisper entend « Boule » : ça, il faut le savoir
    ("Bulles, quelle heure est-il ?", "bulles"),
    ("Tu veux du café ou du thé ?", None),          # une conversation entre humains : rien ne doit rester
    ("Il est neuf heures et quart.", None),
    ("Les enfants sont couchés.", None),            # « les » ressemble de loin à « bulle » : pas assez
    ("Bulle, mets la musique.", None),              # le nom exact : rien à apprendre, elle a déjà répondu
    ("", None),
])
def test_on_ne_garde_le_premier_mot_que_s_il_ressemble_au_nom(phrase, attendu):
    assert server._premier_mot_proche(phrase) == attendu


# ---------------------------------------------------------------- ce qui est écrit dans la base
def test_un_enonce_ignore_ne_laisse_aucun_texte():
    journal.ignore("pas_nomme", longueur=42, premier_mot=None)
    ligne = journal.lire("select * from echanges")[0]
    assert ligne["brut"] is None
    assert ligne["texte"] is None
    assert ligne["longueur"] == 42
    assert ligne["ignore_raison"] == "pas_nomme"


def test_un_enonce_ignore_garde_de_quoi_travailler():
    """L'analyste a besoin de l'heure, de la longueur et du premier mot quand il vise Bulle."""
    journal.ignore("pas_nomme", longueur=24, premier_mot="boule")
    ligne = journal.lire("select * from echanges")[0]
    assert ligne["premier_mot"] == "boule"
    assert ligne["longueur"] == 24
    assert ligne["ts"] > 0
    assert ligne["brut"] is None


def test_une_hallucination_garde_le_motif_qui_l_a_filtree():
    """Le motif sert à régler les expressions de `hallucinations` ; ce n'est pas de la parole."""
    journal.ignore("hallucination", longueur=60, detail="indice_stt")
    ligne = journal.lire("select * from echanges")[0]
    assert ligne["ignore_detail"] == "indice_stt"
    assert ligne["brut"] is None


def test_un_echange_adresse_a_bulle_garde_tout():
    """La vie privée ne coûte rien sur ce qui lui était destiné : on a besoin du texte pour progresser."""
    journal.echange(source="voix", brut="Bulle, ferme les lumières.", texte="ferme les lumières.",
                    nomme=1, eveille=0, reponse="C'est fait.", duree=2.1)
    ligne = journal.lire("select * from echanges")[0]
    assert ligne["brut"] == "Bulle, ferme les lumières."
    assert ligne["reponse"] == "C'est fait."


# ---------------------------------------------------------------- purge
def test_le_texte_des_anciennes_lignes_ignorees_est_efface():
    """Les lignes d'avant la correction portent encore la conversation : on les nettoie, à chaque passage."""
    journal.echange(source="voix", ts=time.time(), brut="Tu veux du café ?", texte="Tu veux du café ?",
                    nomme=0, eveille=0, ignore_raison="pas_nomme")
    assert journal.effacer_textes_ignores() == 1
    ligne = journal.lire("select * from echanges")[0]
    assert ligne["brut"] is None and ligne["texte"] is None
    assert ligne["ignore_raison"] == "pas_nomme"       # la ligne reste, l'analyste en a besoin


def test_l_effacement_ne_touche_pas_au_texte_d_un_vrai_echange():
    journal.echange(source="voix", ts=time.time(), brut="Bulle, quelle heure ?", texte="quelle heure ?",
                    nomme=1, eveille=0, reponse="Il est 9 h.")
    assert journal.effacer_textes_ignores() == 0
    assert journal.lire("select * from echanges")[0]["brut"] == "Bulle, quelle heure ?"


def test_la_sauvegarde_prise_apres_l_effacement_ne_contient_plus_rien(tmp_path):
    """L'ordre de `entretien()` : effacer, PUIS sauvegarder, PUIS purger.

    À la première exécution le 21/09, la sauvegarde était prise avant l'effacement : elle a emporté dans sa
    copie les 1 071 transcriptions qu'on venait justement de retirer de la base. Ce test fixe l'ordre.
    """
    import sqlite3
    journal.echange(source="voix", ts=time.time(), brut="Tu veux du café ?", texte="Tu veux du café ?",
                    nomme=0, eveille=0, ignore_raison="pas_nomme")
    journal.effacer_textes_ignores()
    cible = journal.sauvegarder(str(tmp_path))
    with sqlite3.connect(cible) as c:
        restant = c.execute("select count(*) from echanges where ignore_raison is not null "
                            "and brut is not null").fetchone()[0]
    assert restant == 0


def test_la_purge_ne_s_occupe_plus_que_des_durees_de_vie():
    """Elle ne doit PAS effacer de texte : sinon elle le ferait après la sauvegarde, donc trop tard."""
    journal.echange(source="voix", ts=time.time(), brut="Tu veux du café ?", texte="Tu veux du café ?",
                    nomme=0, eveille=0, ignore_raison="pas_nomme")
    journal.purger({"echanges_jours": 30, "ignores_jours": 7})
    assert journal.lire("select * from echanges")[0]["brut"] == "Tu veux du café ?"


def test_les_vieilles_lignes_partent_selon_leur_duree_de_vie():
    vieux, recent = time.time() - 40 * 86400, time.time() - 1 * 86400
    journal.echange(source="voix", ts=vieux, texte="vieux", nomme=1, reponse="ok")
    journal.echange(source="voix", ts=recent, texte="récent", nomme=1, reponse="ok")
    journal.ignore("pas_nomme", longueur=10)
    journal.ecrire("update echanges set ts = ? where ignore_raison = 'pas_nomme'", (vieux,))
    journal.purger({"echanges_jours": 30, "ignores_jours": 7})
    restants = journal.lire("select texte, ignore_raison from echanges")
    assert [r["texte"] for r in restants] == ["récent"]


def test_ce_qui_porte_la_memoire_du_projet_survit():
    """Incidents, actions et tests à faire sont l'historique du projet : une purge d'échanges ne les touche pas."""
    vieux = time.time() - 40 * 86400
    journal.ecrire("insert into incidents(ts, nuit, type, gravite, cle, resume) values(?,?,?,?,?,?)",
                   (vieux, "2026-08-12", "outil_erreur", "haute", "erreur:x", "quelque chose"))
    journal.a_tester("agent", "Dis « ferme les lumières »", "eteindre_tout")
    journal.ecrire("update a_tester set ts = ?", (vieux,))
    journal.purger({"echanges_jours": 30, "ignores_jours": 7})
    assert len(journal.lire("select * from incidents")) == 1
    assert len(journal.lire("select * from a_tester where statut = 'a_faire'")) == 1


def test_un_test_deja_note_finit_par_partir():
    vieux = time.time() - 200 * 86400
    journal.a_tester("agent", "phrase", "attendu")
    journal.ecrire("update a_tester set ts = ?, statut = 'ok'", (vieux,))
    journal.purger({"tests_jours": 90})
    assert journal.lire("select * from a_tester") == []


# ---------------------------------------------------------------- sauvegarde
def test_la_sauvegarde_produit_une_base_lisible(tmp_path):
    journal.echange(source="voix", texte="quelque chose", nomme=1, reponse="ok")
    cible = journal.sauvegarder(str(tmp_path))
    assert os.path.exists(cible)
    import sqlite3
    with sqlite3.connect(cible) as c:
        assert c.execute("select count(*) from echanges").fetchone()[0] == 1


def test_les_vieilles_sauvegardes_sont_retirees(tmp_path):
    for jour in range(1, 6):
        (tmp_path / f"bulle-2026-09-0{jour}.db").write_text("factice", encoding="utf-8")
    journal.sauvegarder(str(tmp_path), garder=3)
    restantes = sorted(f for f in os.listdir(tmp_path) if f.endswith(".db"))
    assert len(restantes) == 3
    assert restantes[-1] == f"bulle-{time.strftime('%Y-%m-%d')}.db"    # celle du jour est gardée
