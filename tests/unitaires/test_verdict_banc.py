"""La règle qui décide du sort d'une correction de l'agent (tests/banc.py : taux, comparer).

C'est le point le plus coûteux du système : ici, une erreur soit rejette une correction saine — l'agent la
rejouera la nuit suivante, deux nuits pour un tirage au sort — soit annule une fusion déjà en production.
Le juge lui-même se teste donc, et sans LLM.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import banc  # noqa: E402  (tests/ est déjà sur le sys.path via conftest)


def rapport(taux_par_phrase):
    """Un rapport de banc réduit à ce que la comparaison regarde."""
    return {"cas": [{"phrase": p, "taux": t, "reussi": t * 2 > 1} for p, t in taux_par_phrase.items()]}


# ---------------------------------------------------------------- taux
def test_le_taux_se_lit_dans_les_essais():
    assert banc.taux({"essais": [{"ok": True}, {"ok": True}, {"ok": False}]}) == 2 / 3
    assert banc.taux({"essais": [{"ok": True}]}) == 1.0


def test_un_ancien_rapport_sans_taux_reste_lisible():
    """Les rapports d'avant ne portaient qu'un booléen : ils doivent rester comparables."""
    assert banc.taux({"reussi": True}) == 1.0
    assert banc.taux({"reussi": False}) == 0.0


# ---------------------------------------------------------------- régression franche
def test_un_cas_qui_passait_et_qui_casse_est_une_regression():
    regressions, instables = banc.comparer(rapport({"Ferme les lumières.": 1.0}),
                                           rapport({"Ferme les lumières.": 0.0}))
    assert regressions == ["Ferme les lumières."]
    assert instables == []


def test_un_cas_stable_ne_declenche_rien():
    regressions, instables = banc.comparer(rapport({"Joue.": 1.0}), rapport({"Joue.": 1.0}))
    assert (regressions, instables) == ([], [])


def test_une_amelioration_n_est_pas_une_regression():
    regressions, _ = banc.comparer(rapport({"Joue.": 0.0}), rapport({"Joue.": 1.0}))
    assert regressions == []


# ---------------------------------------------------------------- l'aléa n'est pas une régression
def test_un_cas_qui_oscille_apres_n_est_pas_une_regression():
    """2/3 sur trois essais : le modèle hésite, ce n'est pas la correction qui a cassé quelque chose."""
    regressions, instables = banc.comparer(rapport({"C'est parti.": 1.0}), rapport({"C'est parti.": 2 / 3}))
    assert regressions == []
    assert instables == ["C'est parti."]


def test_un_cas_deja_instable_avant_n_est_pas_impute_a_la_correction():
    """Il échouait déjà une fois sur trois sur la production : le rejet serait injuste."""
    regressions, instables = banc.comparer(rapport({"C'est parti.": 1 / 3}), rapport({"C'est parti.": 0.0}))
    assert regressions == []
    assert instables == ["C'est parti."]


def test_un_cas_absent_de_la_reference_est_laisse_de_cote():
    """Le cas que l'agent vient d'ajouter se juge à part, pas par comparaison."""
    regressions, instables = banc.comparer(rapport({"Joue.": 1.0}),
                                           rapport({"Joue.": 1.0, "Nouveau cas.": 0.0}))
    assert (regressions, instables) == ([], [])


# ---------------------------------------------------------------- plusieurs cas à la fois
def test_une_vraie_regression_est_vue_meme_au_milieu_du_bruit():
    avant = rapport({"a": 1.0, "b": 1.0, "c": 2 / 3})
    apres = rapport({"a": 1.0, "b": 0.0, "c": 1 / 3})
    regressions, instables = banc.comparer(avant, apres)
    assert regressions == ["b"]
    assert instables == ["c"]


def test_le_seuil_est_reglable():
    """Avec un seuil plus permissif, 2/3 compte comme « passe franchement »."""
    regressions, instables = banc.comparer(rapport({"a": 1.0}), rapport({"a": 2 / 3}), franc=0.6)
    assert (regressions, instables) == ([], [])
