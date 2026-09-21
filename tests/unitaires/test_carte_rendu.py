"""Le dessin des cartes, côté Pi (carte.py).

Jusqu'ici rien ne testait ce fichier : il demande pygame, que la CI n'avait pas. Or c'est lui qui décide de ce
qui s'affiche dans le salon, et la refonte du 21/09 y met une règle qui se vérifie par la machine — « le corail
ne sort plus qu'à deux endroits ». Un test qui compte des pixels est plus sûr qu'une relecture.

Pas d'écran ici : `SDL_VIDEODRIVER=dummy` est posé par conftest.py, et une Surface se dessine sans fenêtre.
"""
import os
import sys

import pytest

DEPOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, DEPOT)                 # carte.py est à la racine du dépôt, comme sur le Pi

# pygame est dans tests/requirements.txt, donc présent en CI. Il ne l'est pas forcément dans le venv de la VM,
# qui fait tourner le cerveau et n'a rien à dessiner : mieux vaut sauter ce fichier que casser tout `pytest`.
pygame = pytest.importorskip("pygame")
carte = pytest.importorskip("carte")


@pytest.fixture(autouse=True)
def polices_neuves():
    """Les polices sont mises en cache pour la vie du processus : un test qui déplace les dossiers doit repartir
    de zéro, sinon il lit la résolution d'un test précédent."""
    yield
    carte._POLICES.clear(); carte._GRAISSES.clear()


def test_police_resout_geist_par_graisse():
    """La hiérarchie des cartes repose maintenant sur la graisse : chacune doit ouvrir son propre fichier."""
    for graisse, fichier in (("extralight", "Geist-ExtraLight.ttf"), ("light", "Geist-Light.ttf"),
                             ("regular", "Geist-Regular.ttf"), ("medium", "Geist-Medium.ttf")):
        assert os.path.basename(carte._fichier(graisse)) == fichier


def test_police_sans_geist_retombe_sur_la_police_du_systeme(monkeypatch):
    """Une police absente coûte un aspect, jamais une carte : le visage est l'affichage du salon."""
    monkeypatch.setattr(carte, "DOSSIERS", ())
    carte._POLICES.clear(); carte._GRAISSES.clear()
    assert "Geist" not in carte._fichier("light")
    assert carte.police(30, "light").size("Dentiste")[0] > 0


def test_police_met_en_cache_par_taille_et_par_graisse():
    """Deux graisses de même taille sont deux fichiers ouverts : les confondre rendrait tout en Regular."""
    assert carte.police(30, "light") is carte.police(30, "light")
    assert carte.police(30, "light") is not carte.police(30, "medium")


# ---------------------------------------------------------------- le corail
def zone_corail(surf):
    """Le rectangle qui contient tous les pixels corail de la surface, ou None s'il n'y en a aucun.

    `pygame.transform.threshold` marque et compte sans numpy, que la CI n'a pas : parcourir les 600 000 pixels
    d'une carte en Python coûterait une seconde par cas. Il ne compare PAS le canal alpha — le test en devient
    plus strict (un bord de lettre corail à moitié transparent compte aussi), ce qui est exactement ce qu'on
    veut : aucune source de corail ne doit subsister dans la texture.
    """
    masque = pygame.Surface(surf.get_size(), pygame.SRCALPHA)
    masque.fill((0, 0, 0, 0))
    n = pygame.transform.threshold(masque, surf, carte.ACCENT, (40, 40, 40, 255), (255, 255, 255, 255),
                                   1, None, True)
    return masque.get_bounding_rect() if n else None


@pytest.mark.parametrize("i", range(len(carte.EXEMPLES)))
@pytest.mark.parametrize("res", [(853, 720), (1280, 1080)])   # deux tiers de 1280×720 et de 1920×1080
def test_les_cartes_se_dessinent_aux_deux_resolutions(i, res):
    """Ce sont les cartes de la touche C : ce qui casse ici s'affiche noir dans le salon."""
    surf, filet = carte.rendre(carte.EXEMPLES[i], *res)
    assert surf.get_size() == res
    x, y, l, e = filet
    assert 0 <= x and 0 <= y and x + l <= res[0] and y + e <= res[1], "le filet sort de la carte"
    assert l == int(carte.FILET_L * res[1] / 720.0)


@pytest.mark.parametrize("i", range(len(carte.EXEMPLES)))
@pytest.mark.parametrize("res", [(853, 720), (1280, 1080)])
def test_aucun_corail_dans_la_texture_d_une_carte(i, res):
    """Un seul accent, réservé à ce que fait Bulle (21/09) — et ses deux usages restants sont HORS de la
    texture : le filet de tête est tracé par le shader du visage, le point « prochain » n'existe que sur une
    carte d'agenda. Toute autre tache corail ici est une régression : clés, notes, pastille, épellation."""
    surf, _ = carte.rendre(carte.EXEMPLES[i], *res)
    assert zone_corail(surf) is None


@pytest.mark.parametrize("res", [(853, 720), (1280, 1080)])
def test_le_point_corail_marque_la_ligne_du_prochain_evenement(res):
    """Le point vit dans une gouttière à gauche de l'heure, réservée sur toutes les lignes pour que la seule
    marquée ne soit pas aussi la seule décalée."""
    data = {"gabarit": "liste", "titre": "Aujourd'hui", "items": [
        {"cle": "9 h 30", "texte": "Point d'équipe", "passe": True},
        {"cle": "14 h 00", "texte": "Dentiste", "prochain": True},
        {"cle": "18 h 45", "texte": "Hockey"}]}
    surf, _ = carte.rendre(data, *res)
    S = res[1] / 720.0
    zone = zone_corail(surf)
    assert zone is not None, "le prochain événement n'est pas marqué"
    assert zone.width <= 12 * S and zone.height <= 12 * S, f"ce n'est plus un point : {zone}"
    assert zone.right <= int(72 * S) + int(28 * S), "le point déborde de sa gouttière"


# ---------------------------------------------------------------- chiffres
def test_les_grosses_valeurs_sont_tabulaires():
    """Trois prévisions côte à côte doivent tomber au même endroit. Geist dessine ses chiffres proportionnels
    (« 1 » : 35 px, « 0 » : 63 px à 96 px) et SDL_ttf n'expose pas « tnum »."""
    assert carte._valeur("11°", 96).get_width() == carte._valeur("44°", 96).get_width()


def test_la_valeur_affichee_porte_un_vrai_signe_moins():
    """Le cerveau envoie « -4° » — la voix ne lit pas cette chaîne et les tests de cerveau/cartes.py l'attendent.
    C'est l'écran, et lui seul, qui remplace le tiret par U+2212."""
    assert carte._moins("-4°") == "−4°"
