"""Les fonctions pures des outils composés (cerveau/outils_composes.py).

Ce que Bulle DIT compte autant que ce qu'elle fait : ces chaînes sont lues à voix haute par la synthèse, donc
un pluriel de trop s'entend. Et le sélecteur de réglages décide, à la voix, ce qui est écrit dans regles.yaml.
"""
import outils_composes as oc


# ---------------------------------------------------------------- accords (Bulle lit ce texte à voix haute)
def test_une_seule_lumiere_se_dit_au_singulier():
    """« 1 lumières éteintes » s'entendait ; corrigé le 21/09."""
    assert oc._compte(["Lampe salon"], "éteinte") == "1 lumière éteinte"


def test_plusieurs_lumieres_se_disent_au_pluriel():
    assert oc._compte(["Lampe salon", "Cuisine", "Entrée"], "allumée") == "3 lumières allumées"


def test_aucune_lumiere_reste_au_pluriel():
    assert oc._compte([], "éteinte") == "0 lumières éteintes"


# ---------------------------------------------------------------- correspondance tolérante
def test_la_correspondance_ignore_accents_et_casse():
    """Le LLM écrit « blanche » là où Home Assistant attend « Blanc », « neon » pour « Néon »."""
    assert oc.correspondance("blanche", ["Blanc", "Néon", "Chaud"]) == "Blanc"
    assert oc.correspondance("neon", ["Blanc", "Néon", "Chaud"]) == "Néon"
    assert oc.correspondance("ÉTINCELLE", ["Étincelle", "Vague"]) == "Étincelle"


def test_une_valeur_inconnue_ne_correspond_a_rien():
    """Mieux vaut dire « palette inconnue » que d'appliquer la première de la liste."""
    assert oc.correspondance("turquoise", ["Blanc", "Néon"]) is None
    assert oc.correspondance("", ["Blanc"]) is None


def test_norm_retire_les_accents():
    assert oc._norm("Éteins la lumière") == "eteins la lumiere"


# ---------------------------------------------------------------- réglages à la voix
def test_le_reglage_le_mieux_couvert_gagne():
    """`booleen=False` est ce que passe l'outil « reglage » dès que la valeur dite contient un chiffre."""
    assert oc._trouver_reglage("garde les cartes trente secondes 30", booleen=False) == "duree des cartes"
    assert oc._trouver_reglage("mets le volume maximum de la musique à 70", booleen=False) == "volume maximum"
    assert oc._trouver_reglage("baisse la luminosité du panneau LED", booleen=False) == "luminosite du panneau led"


def test_couper_les_cartes_vise_le_booleen_pas_la_duree():
    """« coupe les cartes » et « durée des cartes » partagent un mot : c'est le TYPE qui départage."""
    assert oc._trouver_reglage("coupe les cartes", booleen=True) == "cartes"
    assert oc._trouver_reglage("c'est quoi la durée des cartes", booleen=False) == "duree des cartes"


def test_oublie_reconnait_le_reglage_oubli():
    """Correspondance par préfixe : sinon « oublie » ne trouve pas « oubli »."""
    assert oc._trouver_reglage("au bout de combien de temps tu oublies") == "oubli"


def test_une_demande_hors_liste_ne_trouve_aucun_reglage():
    """Incident : « change la couleur de ton visage » tombait sur « luminosité DU panneau led »,
    parce que le mot vide « du » comptait comme un mot commun."""
    assert oc._trouver_reglage("change la couleur de ton visage en rouge") is None


# ---------------------------------------------------------------- lecture d'une valeur dite à voix haute
def test_un_nombre_est_extrait_de_la_phrase():
    assert oc._valeur_yaml("30 secondes") == 30
    assert oc._valeur_yaml("mets-le à 70") == 70
    assert oc._valeur_yaml("-5") == -5


def test_la_virgule_decimale_est_acceptee():
    assert oc._valeur_yaml("1,5") == 1


def test_une_phrase_sans_nombre_ne_donne_rien():
    assert oc._valeur_yaml("beaucoup plus long") is None


def test_les_formulations_de_oui_et_non_sont_comprises():
    for oui in ("oui", "active", "activé", "allume", "allumées", "remets-les", "vrai"):
        assert oc._valeur_yaml(oui, booleen=True) is True, oui
    for non in ("non", "coupe", "désactive", "désactivé", "inactif", "arrête", "faux"):
        assert oc._valeur_yaml(non, booleen=True) is False, non
    assert oc._valeur_yaml("peut-être", booleen=True) is None


def test_un_refus_n_est_jamais_lu_comme_un_accord():
    """« non » contenait « on », « désactive » contenait « active » : la comparaison par sous-chaîne
    retournait l'interrupteur. Sur /suivi, choisir « non » pour les cartes les rallumait."""
    assert oc._valeur_yaml("non", booleen=True) is False
    assert oc._valeur_yaml("non, coupe les cartes", booleen=True) is False
    assert oc._valeur_yaml("désactive les cartes", booleen=True) is False
    assert oc._valeur_yaml("inactif", booleen=True) is False


# ---------------------------------------------------------------- catalogue
def test_chaque_outil_compose_se_decrit_pour_le_llm():
    """Le modèle ne lit QUE la description : un outil sans description est un outil qu'il n'appellera jamais bien."""
    for nom, o in oc.OUTILS.items():
        assert o["description"].strip(), nom
        assert set(o["requis"]) <= set(o["parametres"] if isinstance(o["parametres"], dict) else {}) or callable(
            o["parametres"]), nom
