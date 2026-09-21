"""Le visage de la TV (face.py), côté Python.

`Face` est du Python pur : `command()`, `update()` et `layout()` ne demandent ni carte graphique ni Kinect —
seul le dessin est dans le shader. Ce qui se calcule ici se teste donc ici, et c'est presque tout ce que la
refonte du 21/09 change : le fil corail ne prend plus trois formes sans rapport, il en prend une qui réagit.

Ce que le shader fait des nombres rendus par `layout()` ne se vérifie qu'à l'œil, sur la TV.
"""
import os
import sys

import pytest

DEPOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, DEPOT)                 # face.py est à la racine du dépôt, comme sur le Pi

pytest.importorskip("pygame")             # face.py importe pygame et carte.py ; le venv de la VM peut s'en passer
face_mod = pytest.importorskip("face")


def visage(**cmd):
    f = face_mod.Face(1280, 720)
    if cmd: f.command(cmd)
    return f


def avancer(f, secondes, dt=1 / 30.0, t0=0.0):
    """Fait tourner la boucle d'animation sans écran, au pas du visage (30 i/s)."""
    t = t0
    for _ in range(max(1, int(secondes / dt))):
        t += dt
        f.update(dt, t)
    return t


# ---------------------------------------------------------------- écoute
def test_le_trait_d_ecoute_s_allonge_avec_la_voix_entendue():
    """Le trait réagit à ce que le micro entend, au lieu de battre à vide : c'est toute la différence entre
    « Bulle est allumée » et « Bulle t'entend »."""
    fort, faible = visage(state="listening"), visage(state="listening")
    for f, niveau in ((fort, 0.8), (faible, 0.0)):
        for _ in range(10):                       # le compagnon en envoie une quinzaine par seconde
            f.command({"ecoute": niveau}); f.update(1 / 30.0, 0.3)
    assert fort.layout(0.3)["u_fil"][0] > faible.layout(0.3)["u_fil"][0]


def test_le_trait_d_ecoute_retombe_seul_sans_message():
    """Un datagramme perdu, un compagnon qui meurt : le trait ne doit pas rester figé en l'air."""
    f = visage(state="listening")
    f.command({"ecoute": 1.0}); f.update(1 / 30.0, 0.1)
    haut = f.layout(0.1)["u_fil"][0]
    avancer(f, 0.5, t0=0.1)
    assert f.layout(0.6)["u_fil"][0] < haut


def test_sans_compagnon_le_trait_garde_la_respiration_d_avant():
    """Visage lancé seul (--demo, ou vieux compagnon) : un trait figé à sa longueur minimale se lirait comme
    une panne. L'intensité doit donc continuer de varier avec le temps."""
    f = visage(state="listening")
    avancer(f, face_mod.ECOUTE_REPLI_S + 0.5)
    intensites = {round(f.layout(t)["u_sta"][3], 4) for t in (0.0, 0.5, 1.0)}
    assert len(intensites) == 3


def test_avec_un_niveau_recent_l_intensite_ne_depend_plus_du_temps():
    """Dès qu'un compagnon parle, c'est lui qui pilote : la respiration ne doit plus s'ajouter par-dessus."""
    f = visage(state="listening")
    f.command({"ecoute": 0.5}); f.update(1 / 30.0, 0.1)
    assert f.layout(0.0)["u_sta"][3] == f.layout(1.0)["u_sta"][3]


def test_le_trait_penche_vers_qui_parle():
    """Le décalage vient du regard suivi par la Kinect, pas de la direction des micros — celle-ci n'est mesurée
    qu'APRÈS l'énoncé, trop tard pour animer le trait pendant qu'on parle."""
    droite, centre = visage(state="listening", gaze=[1.0, 0.0]), visage(state="listening", gaze=[0.0, 0.0])
    for f in (droite, centre): f.update(1 / 30.0, 0.1)
    assert droite.layout(0.1)["u_fil"][1] > centre.layout(0.1)["u_fil"][1] == 0.0
    assert droite.layout(0.1)["u_fil"][2] != 0.0            # et il s'incline du même côté


def test_un_regard_oublie_ne_penche_plus_le_trait():
    """`track_age` périme le suivi au bout de trois secondes : sans personne devant, le trait revient droit."""
    f = visage(state="listening", gaze=[1.0, 0.0])
    avancer(f, 4.0)
    assert f.layout(4.0)["u_fil"][1] == 0.0


# ---------------------------------------------------------------- réflexion
def test_la_reflexion_n_expose_qu_un_point_qui_va_et_vient():
    """Trois points qui clignotent, c'était un troisième signe pour la même chose. Un seul point, qui balaie —
    et deux boucles de moins dans le shader, qui tourne à 30 i/s sur un Pi 3."""
    f = visage(state="thinking")
    f.update(1 / 30.0, 0.1)
    assert "u_dots" not in f.layout(0.1), "l'ancien uniforme des trois points traîne encore"
    positions = [f.layout(t)["u_fil"][3] for t in (0.0, 0.3, 0.7, 1.1)]
    assert len(set(positions)) == len(positions)
    assert max(abs(x) for x in positions) <= 26 * f.K + 1e-6


def test_le_point_de_reflexion_reste_dans_le_tiers_du_visage():
    """Les cartes arrivent PENDANT la réflexion : le point et la carte coexistent, et le visage glisse dans son
    tiers au même moment. Le balayage doit tenir dans ce tiers, sinon le point passe sous la carte."""
    f = visage(state="thinking")
    f.set_carte({"gabarit": "liste", "titre": "Aujourd'hui", "items": []})
    avancer(f, 3.0)                               # le temps que le visage ait fini de se ranger
    centre = f.layout(3.0)["u_st"][0]
    demi = 26 * f.K * f.zoom
    assert 0 < centre - demi and centre + demi < f.W * (1 - face_mod.CARTE_PART)


# ---------------------------------------------------------------- veille de nuit
def test_la_nuit_le_blanc_tire_vers_l_ambre():
    """Assombrir ne suffit pas : à 3 h du matin, un blanc franc reste un écran. La teinte en fait une lampe."""
    f = visage(nuit=True)
    avancer(f, 4.0)
    r, v, b = f.layout(4.0)["u_teinte"]
    assert r > 1.0 and b < 0.8 and v < r                 # plus de rouge, beaucoup moins de bleu


def test_le_jour_la_teinte_ne_change_rien():
    """Le rapport doit valoir exactement 1 le jour, sinon on assombrit le visage sans le vouloir."""
    f = visage()
    avancer(f, 2.0)
    assert [round(x, 6) for x in f.layout(2.0)["u_teinte"]] == [1.0, 1.0, 1.0]


def test_les_zzz_ont_disparu():
    """Ils faisaient dessin animé à côté du reste. Le sommeil se dit maintenant par des paupières qui respirent."""
    f = visage(nuit=True, state="idle")
    avancer(f, 1.0)
    assert "u_z" not in f.layout(1.0)
    assert "zzz" not in face_mod.BASE


def test_en_sommeil_les_paupieres_respirent():
    """Une pulsation lente (6 s, 0,55 → 1) : ça dit « en veille » sans rien ajouter à l'écran."""
    f = visage()
    f.set_emotion("sommeil")
    avancer(f, 2.0)
    clartes = [f.layout(t)["u_ell"][2] for t in (0.0, 1.5, 3.0)]
    assert max(clartes) > min(clartes) * 1.2


# ---------------------------------------------------------------- lien avec le cerveau
def test_sans_battement_le_visage_se_declare_hors_ligne():
    """Le cerveau tombe, compagnon.py sort en 75, systemd le relance en boucle — et la TV affichait jusqu'ici
    une Bulle attentive qui n'écoutait plus rien."""
    f = visage()
    avancer(f, face_mod.HORS_LIGNE_S + 1.0)
    assert f.hors_ligne
    assert f.layout(0.0)["u_vide"] > 0.99


def test_au_demarrage_on_laisse_sa_chance_au_compagnon():
    """Il met quelques secondes à se connecter : annoncer la panne avant serait faux."""
    f = visage()
    avancer(f, face_mod.HORS_LIGNE_S - 2.0)
    assert not f.hors_ligne


def test_un_battement_ramene_le_visage_en_fondu():
    """Retour progressif : un clignotement à chaque reconnexion serait pire que la panne."""
    f = visage()
    avancer(f, face_mod.HORS_LIGNE_S + 1.0)
    f.command({"lien": True})
    milieu = avancer(f, 0.1)
    assert 0.01 < f.layout(milieu)["u_vide"] < 0.99       # ni encore vide, ni déjà revenu
    avancer(f, 1.0, t0=milieu)
    assert f.layout(0.0)["u_vide"] < 0.01
    assert not f.hors_ligne


def test_sans_lien_ne_declare_jamais_la_panne():
    """Visage lancé seul (--demo, --grid, --sans-lien) : personne n'envoie de battement, et c'est normal."""
    f = visage()
    f.sans_lien = True
    avancer(f, face_mod.HORS_LIGNE_S + 5.0)
    assert not f.hors_ligne and f.layout(0.0)["u_vide"] < 0.01


def test_hors_ligne_efface_le_fil_et_les_cartes():
    """L'écran doit dire une seule chose. Un fil corail qui ondule à côté d'yeux vides dirait le contraire."""
    f = visage(state="listening")
    f.set_carte({"gabarit": "texte", "texte": "Kyutai"})
    avancer(f, face_mod.HORS_LIGNE_S + 1.0)
    assert f.layout(0.0)["u_sta"][0] < 0.01
    assert f.carte_alpha() < 0.01


def test_le_point_de_reflexion_s_attarde_aux_extremites():
    """Un balayage à vitesse constante fait machine ; le ralenti aux extrémités fait hésitation."""
    import math
    ecart = []
    f = visage(state="thinking")
    for t in (0.0, 0.05, 0.1):                    # près du centre du balayage, là où le point va le plus vite
        ecart.append(f.layout(t)["u_fil"][3])
    brut = [math.sin(t * 2.2) * 26 * f.K for t in (0.0, 0.05, 0.1)]
    assert abs(ecart[2] - ecart[0]) > abs(brut[2] - brut[0])
