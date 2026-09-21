"""Le journal du niveau micro (compagnon.py, classe Niveaux).

Il écrivait une ligne par seconde, jour et nuit — 86 000 par jour sur la carte SD du Pi, à décrire surtout du
silence. Ces lignes ne servent qu'à l'analyste, qui compte les secondes de parole probable sous le seuil de
déclenchement. Tout l'enjeu de ces cas : **alléger sans rien changer à ce qu'il compte**.

`compagnon.py` importe sounddevice et websockets dès l'import : on extrait donc la classe du source, comme pour
le relevé thermique du suivi. Aucun test de ce dépôt ne demande de matériel.
"""
import os
import re
import types

import pytest

DEPOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def Niveaux():
    source = open(os.path.join(DEPOT, "compagnon.py"), encoding="utf-8").read()
    debut, fin = source.index("class Niveaux:"), source.index("class Listener:")
    module = types.ModuleType("niveaux_extrait")
    exec(compile(source[debut:fin], "compagnon.py", "exec"), module.__dict__)
    return module.Niveaux


def journal(n, secondes, plancher=20.0):
    """Rejoue une suite de probabilités de parole → les lignes réellement écrites."""
    return [ligne for p in secondes if (ligne := n.seconde(100.0, p, plancher))]


def comme_l_analyste(lignes, seuil_on=0.3):
    """La MÊME lecture que agent/analyste.py : (faibles, fortes)."""
    probas = [float(x) for x in re.findall(r"parole ([0-9.]+)", "\n".join(lignes))]
    return (sum(0.15 <= p < seuil_on for p in probas), sum(p >= 0.5 for p in probas))


# ---------------------------------------------------------------- l'allègement
def test_une_heure_de_silence_tient_en_quelques_lignes(Niveaux):
    n = Niveaux(seuil=0.10, calme_s=60)
    lignes = journal(n, [0.01] * 3600)
    assert len(lignes) == 60          # une par minute, au lieu de 3 600
    assert "60 s calmes" in lignes[0]


def test_la_parole_est_journalisee_seconde_par_seconde(Niveaux):
    """Ce que l'analyste compte doit rester à la seconde : c'est sa seule unité de mesure."""
    n = Niveaux(seuil=0.10, calme_s=60)
    assert len(journal(n, [0.42] * 30)) == 30


# ---------------------------------------------------------------- ce que compte l'analyste, à l'identique
@pytest.mark.parametrize("nom, secondes", [
    ("soirée calme", [0.02] * 1800),
    ("micro trop faible", [0.18, 0.22, 0.03, 0.19] * 50),
    ("conversation nette", [0.7, 0.8, 0.05, 0.9] * 40),
    ("mélange réaliste", ([0.01] * 200 + [0.17, 0.25, 0.62, 0.05] * 10) * 3),
    ("pile sur les bornes", [0.15, 0.299, 0.3, 0.5, 0.4999, 0.149] * 20),
])
def test_l_analyste_compte_exactement_la_meme_chose_qu_avant(Niveaux, nom, secondes):
    """Le vrai critère : l'allègement ne doit pas bouger d'une unité les compteurs de parole_sous_seuil."""
    avant = comme_l_analyste([f"niveau 100  plancher 20  parole {p:.2f}" for p in secondes])
    apres = comme_l_analyste(journal(Niveaux(seuil=0.10, calme_s=60), secondes))
    assert apres == avant, nom


def test_un_battement_ne_peut_jamais_porter_une_seconde_comptee(Niveaux):
    """Par construction : le battement ne résume que des secondes sous 0,10, or l'analyste compte à partir de 0,15."""
    n = Niveaux(seuil=0.10, calme_s=10)
    lignes = journal(n, [0.09, 0.05, 0.099] * 10)
    for ligne in lignes:
        p = float(re.search(r"parole ([0-9.]+)", ligne).group(1))
        assert p < 0.15


# ---------------------------------------------------------------- le battement, et pourquoi il existe
def test_le_silence_ne_devient_pas_muet(Niveaux):
    """Sans battement, un micro mort ressemblerait exactement à une pièce calme."""
    n = Niveaux(seuil=0.10, calme_s=60)
    lignes = journal(n, [0.0] * 180)
    assert len(lignes) == 3
    assert all("parole 0.00" in l for l in lignes)


def test_le_battement_porte_le_maximum_pas_la_moyenne(Niveaux):
    """La fiche voix-trop-faible demande de vérifier les faux déclenchements en silence (« parole < 0,05 »)."""
    n = Niveaux(seuil=0.10, calme_s=10)
    lignes = journal(n, [0.01] * 9 + [0.08])
    assert "parole 0.08" in lignes[0]


def test_le_battement_dit_combien_de_secondes_il_resume(Niveaux):
    n = Niveaux(seuil=0.10, calme_s=30)
    assert "(30 s calmes)" in journal(n, [0.01] * 30)[0]


def test_la_parole_remet_le_battement_a_zero(Niveaux):
    """Pendant une conversation, chaque seconde parle d'elle-même : pas besoin de résumé en plus."""
    n = Niveaux(seuil=0.10, calme_s=5)
    lignes = journal(n, [0.01] * 4 + [0.9] + [0.01] * 4)
    assert len(lignes) == 1                  # la seule ligne est celle de la parole
    assert "calmes" not in lignes[0]


# ---------------------------------------------------------------- forme des lignes
def test_la_ligne_garde_le_format_que_l_analyste_sait_lire(Niveaux):
    n = Niveaux(seuil=0.10, calme_s=60)
    ligne = journal(n, [0.42])[0]
    assert re.search(r"niveau\s+\d+\s+plancher\s+\d+\s+parole ([0-9.]+)", ligne)
    assert float(re.search(r"parole ([0-9.]+)", ligne).group(1)) == 0.42
