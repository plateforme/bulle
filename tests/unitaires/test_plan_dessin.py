"""Le dessin des plans et des QR codes (cerveau/plan.py), sans réseau.

Tout ce qui est ici vient d'un défaut vu sur la TV le 21/09 : un plan sans aucun nom de rue, une barre d'échelle
qui annonçait 1 km sur le tiers du plan, et un QR code qui faisait MOURIR le visage. Le dessin est déterministe,
donc chacun de ces défauts se rejoue ici en une seconde et sans rien télécharger.
"""
import math
import os
import struct

import plan
import pytest
from PIL import Image


# ---------------------------------------------------------------- recoller les tronçons d'une rue
def test_les_troncons_bout_a_bout_font_une_seule_rue():
    """OSM redécoupe une rue à chaque changement d'attribut : le boulevard Pie-IX arrivait en douze morceaux de
    100 px alors qu'il traverse tout le cadre, donc aucun n'était assez long pour porter son nom."""
    morceaux = [[(0, 0), (50, 0)], [(100, 0), (50, 0)], [(100, 0), (200, 0)]]   # désordonnés, l'un à l'envers
    chaines = plan._recoller(morceaux)
    assert len(chaines) == 1
    assert chaines[0][0] == (0, 0) and chaines[0][-1] == (200, 0)


def test_deux_rues_distinctes_ne_sont_pas_soudees():
    chaines = plan._recoller([[(0, 0), (10, 0)], [(300, 0), (310, 0)]])
    assert len(chaines) == 2


def test_le_recollage_rend_les_chaines_de_la_plus_longue_a_la_plus_courte():
    chaines = plan._recoller([[(0, 0), (5, 0)], [(0, 90), (0, 40)], [(0, 40), (0, 0)]])
    assert len(chaines[0]) >= len(chaines[-1])


# ---------------------------------------------------------------- la portion visible d'un tracé
def test_on_garde_la_plus_longue_suite_de_points_dans_le_cadre():
    """Filtrer point par point créerait un faux segment droit d'un bord à l'autre, et l'étiquette se poserait
    dans le vide."""
    pts = [(-50, 10), (10, 10), (20, 10), (700, 10), (800, 10)]
    longueur, visible = plan._portion_visible(pts, 560, 380)
    assert visible == [(10, 10), (20, 10)]
    assert longueur == 10


def test_un_trace_entierement_hors_du_cadre_ne_donne_rien():
    assert plan._portion_visible([(900, 900), (950, 950)], 560, 380)[0] == 0


# ---------------------------------------------------------------- les noms, écrits comme sur un plan
def test_le_type_de_voie_et_le_cardinal_sont_abreges():
    """« Rue Sainte-Catherine Est » écrit en entier ne rentrait pas entre le repère et le bord : la rue de la
    destination, la plus utile du plan, n'était donc jamais nommée."""
    assert plan._court("Rue Sainte-Catherine Est") == "Ste-Catherine E."
    assert plan._court("Boulevard Pie-IX") == "Pie-IX"
    assert plan._court("Rue du Bord-de-l'Eau Ouest") == "du Bord-de-l'Eau O."


def test_un_nom_sans_type_ni_cardinal_est_laisse_tel_quel():
    assert plan._court("Autoroute Métropolitaine") == "Autoroute Métropolitaine"
    assert plan._court("Pie-IX") == "Pie-IX"


def test_les_etiquettes_evitent_le_repere_et_ce_qui_est_reserve():
    """Le repère central et la barre d'échelle sont dessinés avant : une étiquette par-dessus rend les deux
    illisibles (vu le 21/09, un nom écrit en travers du repère)."""
    largeur, hauteur = 704, 416
    img = Image.new("RGBA", (largeur, hauteur), (0, 0, 0, 255))
    traverse = [(0, hauteur / 2), (largeur, hauteur / 2)]        # une rue qui passe pile sur le repère
    barre = (10, hauteur - 40, 200, hauteur - 10)
    avant = img.tobytes()
    plan._noms(img, [("Rue Traversante", 4, traverse)], largeur, hauteur, reserve=[barre])
    # elle est bien écrite quelque part (le dessin a changé), mais pas sur le repère
    assert img.tobytes() != avant
    centre = img.crop((largeur // 2 - 20, hauteur // 2 - 20, largeur // 2 + 20, hauteur // 2 + 20))
    assert centre.getextrema()[0][1] == 0          # le canal rouge du centre est resté noir : rien n'y est écrit


def test_une_rue_trop_courte_pour_son_nom_n_est_pas_nommee():
    img = Image.new("RGBA", (704, 416), (0, 0, 0, 255))
    avant = img.tobytes()
    plan._noms(img, [("Rue Beaucoup Trop Longue Pour Ce Tronçon", 1, [(10, 10), (40, 10)])], 704, 416)
    assert img.tobytes() == avant


# ---------------------------------------------------------------- la barre d'échelle
def _graduation(zoom, largeur=704, hauteur=416, lat=45.54):
    img = Image.new("RGBA", (largeur, hauteur), (0, 0, 0, 255))
    x0, _, x1, _ = plan._echelle(img, lat, zoom, largeur, hauteur)
    m_par_px = 156543.03392 * math.cos(math.radians(lat)) / 2 ** zoom
    return (x1 - x0) * m_par_px, x1 - x0


def test_la_barre_reste_une_fraction_du_plan():
    """Elle annonçait « 1 km » en travers du tiers du plan : le code prenait la première graduation qui DÉPASSE
    la largeur permise au lieu de la dernière qui tient."""
    for zoom in (13, 14, 15, 16, 17):
        _, px = _graduation(zoom)
        assert px < 704 * 0.45, f"barre trop longue au zoom {zoom} : {px:.0f} px"
        assert px > 20, f"barre invisible au zoom {zoom} : {px:.0f} px"


def test_la_barre_tient_dans_le_plan_et_hors_du_fondu():
    img = Image.new("RGBA", (704, 416), (0, 0, 0, 255))
    x0, y0, x1, y1 = plan._echelle(img, 45.54, 16, 704, 416)
    assert 0 < x0 < x1 < 704
    assert y1 < 416 - plan._etendue(704, 416) * 0.3    # sinon elle s'efface avec le bord du plan


# ---------------------------------------------------------------- le QR code
@pytest.fixture
def fichier_qr(tmp_path, monkeypatch):
    monkeypatch.setattr(plan, "PLANS", str(tmp_path))
    chemin, cle = plan.qr("https://www.google.com/maps/dir/?api=1&destination=45.5084,-73.5665&travelmode=driving")
    assert os.path.exists(chemin) and len(cle) == 16
    return chemin


def test_le_qr_n_est_pas_en_palette_1_bit(fichier_qr):
    """LE défaut du 21/09 : segno écrit un PNG en palette 1 bit, pygame.transform.smoothscale n'accepte que du
    24 ou 32 bits, et l'exception tuait le processus du visage à chaque itinéraire demandé.
    """
    with open(fichier_qr, "rb") as f:
        profondeur, type_couleur = struct.unpack(">BB", f.read(26)[24:26])
    assert profondeur >= 8, "PNG en palette 1 bit : le visage ne saura pas le mettre à l'échelle"
    assert type_couleur == 6, "attendu RGBA : le QR se pose sur le fond sombre de la carte"


def test_le_qr_est_carre_et_lisible_de_loin(fichier_qr):
    im = Image.open(fichier_qr)
    assert im.size[0] == im.size[1]
    assert 380 <= im.size[0] <= 520          # assez grand pour être scanné du canapé, pas au point de manger l'écran


def test_le_qr_est_en_polarite_classique(fichier_qr):
    """En négatif (modules clairs sur fond sombre) il est plus beau sur la TV, mais les lecteurs ne le
    retrouvent pas — vérifié avec OpenCV, qui ne décodait que l'image ré-inversée."""
    im = Image.open(fichier_qr).convert("RGB")
    coin = im.crop((0, 0, 40, 40)).resize((1, 1)).getpixel((0, 0))     # la plaque, hors motif
    assert sum(coin) / 3 > 150, "la plaque doit être claire et les modules sombres"


def test_le_qr_se_decode_vraiment(fichier_qr):
    """Le seul test qui prouve qu'il sert à quelque chose. Sauté si OpenCV n'est pas installé."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    im = Image.open(fichier_qr)
    fond = Image.new("RGB", im.size, (13, 13, 13))                     # comme sur la carte : posé sur du noir
    fond.paste(im, (0, 0), im)
    texte, *_ = cv2.QRCodeDetector().detectAndDecode(np.array(fond)[:, :, ::-1].copy())
    assert texte.startswith("https://www.google.com/maps/dir/")


def test_deux_liens_differents_donnent_deux_fichiers(tmp_path, monkeypatch):
    monkeypatch.setattr(plan, "PLANS", str(tmp_path))
    assert plan.qr("https://exemple.test/a")[1] != plan.qr("https://exemple.test/b")[1]
    assert plan.qr("https://exemple.test/a")[1] == plan.qr("https://exemple.test/a")[1]
