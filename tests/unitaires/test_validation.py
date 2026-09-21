"""Le validateur de fichiers YAML (outils/valider.py) — il faut qu'il attrape vraiment quelque chose.

Un validateur qui dit toujours « valide » est pire que pas de validateur : il rassure. On lui donne donc ici des
fichiers volontairement cassés, dans un dossier temporaire, et on vérifie qu'il proteste.
"""
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "outils"))
import valider  # noqa: E402

REGLES_OK = {"cerveau": {"nom": "Bulle", "nom_variantes": ["bulle"], "lecture_seule": ["get_*"]},
             "nuit": {"auto": True}, "client": {"vad_seuil_on": 0.3}, "suivi": {"absence_s": 6.0},
             "journal": {"echanges_jours": 30, "ignores_jours": 7}}
BANC_OK = {"cas": [{"phrase": "Ferme les lumières.", "outil": "eteindre_tout"}]}
PANNES_OK = {"pannes": [{"id": "exemple", "zone": ["config/regles.yaml"]}]}


@pytest.fixture
def depot(tmp_path, monkeypatch):
    """Un faux dépôt, valide au départ ; chaque test n'y casse qu'une chose."""
    for dossier in ("config", "tests", "connaissances"):
        (tmp_path / dossier).mkdir()
    monkeypatch.setattr(valider, "DEPOT", str(tmp_path))

    def ecrire(regles=REGLES_OK, banc=BANC_OK, pannes=PANNES_OK):
        for chemin, contenu in (("config/regles.yaml", regles), ("tests/banc.yaml", banc),
                                ("connaissances/pannes.yaml", pannes)):
            if isinstance(contenu, str):
                (tmp_path / chemin).write_text(contenu, encoding="utf-8")
            else:
                (tmp_path / chemin).write_text(yaml.safe_dump(contenu, allow_unicode=True), encoding="utf-8")
        erreurs = []
        valider.valider_regles(erreurs)
        valider.valider_banc(erreurs)
        valider.valider_pannes(erreurs)
        return erreurs

    return ecrire


def test_un_depot_sain_ne_produit_aucune_erreur(depot):
    assert depot() == []


# ---------------------------------------------------------------- règles
def test_un_yaml_casse_est_signale(depot):
    erreurs = depot(regles="cerveau:\n  nom: Bulle\n   mauvais: indentation\n")
    assert any("YAML illisible" in e for e in erreurs)


def test_une_section_disparue_est_signalee(depot):
    erreurs = depot(regles={"cerveau": {"nom": "Bulle", "nom_variantes": ["bulle"]}})
    assert any("nuit" in e for e in erreurs)
    assert any("client" in e for e in erreurs)


def test_un_reveil_vide_est_signale(depot):
    """Sans variantes, plus aucune phrase ne réveille Bulle — et rien ne planterait pour le dire."""
    erreurs = depot(regles={**REGLES_OK, "cerveau": {"nom": "Bulle", "nom_variantes": []}})
    assert any("nom_variantes" in e for e in erreurs)


def test_un_outil_a_effet_en_lecture_seule_est_refuse(depot):
    """Un outil en lecture seule s'exécute POUR DE VRAI pendant le banc : « eteindre_tout » y éteindrait la maison."""
    regles = {**REGLES_OK, "cerveau": {**REGLES_OK["cerveau"], "lecture_seule": ["get_*", "eteindre_tout"]}}
    erreurs = depot(regles=regles)
    assert any("eteindre_tout" in e for e in erreurs)


def test_une_duree_de_vie_absurde_est_refusee(depot):
    """C'est la section où une faute de frappe coûte des données qu'on ne récupère pas : zéro jour viderait
    le journal à chaque nuit, et une valeur non numérique ferait échouer la purge en silence."""
    for mauvaise in ({"echanges_jours": 0}, {"echanges_jours": "trente"}, {"ignores_jours": -1},
                     {"echanges_jours": True}):
        erreurs = depot(regles={**REGLES_OK, "journal": mauvaise})
        assert any("journal." in e for e in erreurs), mauvaise


def test_la_section_journal_manquante_est_signalee(depot):
    regles = {k: v for k, v in REGLES_OK.items() if k != "journal"}
    assert any("journal" in e for e in depot(regles=regles))


# ---------------------------------------------------------------- banc
def test_une_cle_mal_orthographiee_est_signalee(depot):
    """« interdit » au lieu d'« interdits » : le cas ne vérifie plus rien et passe toujours."""
    erreurs = depot(banc={"cas": [{"phrase": "Joue.", "outil": "play", "interdit": ["play_search"]}]})
    assert any("interdit" in e for e in erreurs)


def test_un_cas_qui_n_attend_rien_est_signale(depot):
    erreurs = depot(banc={"cas": [{"phrase": "Joue.", "origine": "journal 19/09"}]})
    assert any("passera toujours" in e for e in erreurs)


def test_un_gabarit_de_carte_inconnu_est_signale(depot):
    erreurs = depot(banc={"cas": [{"phrase": "Quel temps ?", "outil": "meteo_actuelle", "carte": "tableau"}]})
    assert any("tableau" in e for e in erreurs)


def test_un_cas_en_double_est_signale(depot):
    cas = {"phrase": "Joue.", "outil": "play"}
    erreurs = depot(banc={"cas": [cas, dict(cas)]})
    assert any("double" in e for e in erreurs)


def test_deux_cas_sur_la_meme_phrase_mais_differents_sont_permis(depot):
    """Une même phrase peut vérifier l'outil dans un cas et la carte dans un autre."""
    erreurs = depot(banc={"cas": [{"phrase": "Quel temps ?", "outil": "meteo_actuelle"},
                                  {"phrase": "Quel temps ?", "carte": "cles"}]})
    assert erreurs == []


# ---------------------------------------------------------------- pannes
def test_une_fiche_sans_identifiant_est_signalee(depot):
    erreurs = depot(pannes={"pannes": [{"titre": "quelque chose"}]})
    assert any("sans « id »" in e for e in erreurs)


def test_un_identifiant_en_double_est_signale(depot):
    erreurs = depot(pannes={"pannes": [{"id": "meme"}, {"id": "meme"}]})
    assert any("double" in e for e in erreurs)


def test_une_zone_qui_n_existe_pas_est_signalee(depot):
    """La zone d'une fiche élargit ce que l'agent de nuit a le droit d'écrire : un chemin mort le prive d'accès."""
    erreurs = depot(pannes={"pannes": [{"id": "exemple", "zone": ["cerveau/disparu.py"]}]})
    assert any("disparu.py" in e for e in erreurs)


# ---------------------------------------------------------------- la sélection d'outils
def _regles_avec_selection(**sel):
    base = {"actif": True, "toujours": ["afficher"], "groupes": {"musique": {"mots": "joue|musique", "outils": ["play"]}}}
    r = {k: dict(v) for k, v in REGLES_OK.items()}
    r["cerveau"] = dict(REGLES_OK["cerveau"], selection_outils=dict(base, **sel))
    return r


def test_une_selection_bien_formee_passe(depot):
    assert depot(regles=_regles_avec_selection()) == []


def test_un_motif_de_selection_accentue_est_refuse(depot):
    """Les motifs sont comparés à la phrase SANS accents : « lumière » ne peut correspondre à rien, jamais —
    et le groupe est alors mort sans qu'aucun journal ne le dise."""
    erreurs = depot(regles=_regles_avec_selection(groupes={"maison": {"mots": "lumière|lampe", "outils": ["allumer"]}}))
    assert any("accents" in e for e in erreurs), erreurs


def test_un_groupe_sans_outils_est_refuse(depot):
    erreurs = depot(regles=_regles_avec_selection(groupes={"vide": {"mots": "joue"}}))
    assert any("selection_outils.groupes.vide" in e for e in erreurs), erreurs


def test_un_motif_de_selection_illisible_est_refuse(depot):
    erreurs = depot(regles=_regles_avec_selection(groupes={"casse": {"mots": "joue(", "outils": ["play"]}}))
    assert any("motif illisible" in e for e in erreurs), erreurs


def test_une_selection_active_sans_groupe_est_refusee(depot):
    """Tous les outils seraient encore envoyés — ce n'est pas une panne, mais ce n'est pas ce qui est écrit."""
    erreurs = depot(regles=_regles_avec_selection(groupes=None))
    assert any("aucun groupe" in e for e in erreurs), erreurs
