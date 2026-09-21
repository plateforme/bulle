"""Le relevé thermique du Pi (pi/tracker.py, classe Thermique).

Le bilan de santé ne tournait qu'à 3 h du matin, quand le Pi est froid depuis des heures : il n'a donc jamais
signalé une surchauffe, alors que la machine tourne bridée en journée (mesuré le 21/09 : 76,3 °C au repos,
throttled=0x20002). Ces cas fixent la règle qui remplace cet instantané par le pire des 24 h.

`pi/tracker.py` parle à la Kinect par ctypes dès l'import : on n'importe donc que la classe, en la relisant
depuis le source, sans charger libfreenect — aucun test de ce dépôt ne demande de matériel.
"""
import os
import types

import pytest

DEPOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def Thermique():
    """Extrait la classe du source, sans exécuter le reste du module (qui ouvre la Kinect)."""
    source = open(os.path.join(DEPOT, "pi", "tracker.py"), encoding="utf-8").read()
    debut = source.index("class Thermique:")
    fin = source.index("def inclinaison():")
    module = types.ModuleType("thermique_extrait")
    exec(compile(source[debut:fin], "tracker.py", "exec"), module.__dict__)
    return module.Thermique


@pytest.fixture
def sonde(Thermique, monkeypatch):
    """Une sonde dont on pilote ce que « lisent » sysfs et vcgencmd."""
    t = Thermique()
    lectures = {"temp": 60.0, "mot": "0x0"}
    monkeypatch.setattr(t, "_temperature", staticmethod(lambda: lectures["temp"]))
    monkeypatch.setattr(t, "_throttled", staticmethod(lambda: lectures["mot"]))
    t.lectures = lectures
    return t


def nourrir(sonde, now, echantillons):
    """echantillons : (secondes écoulées, température, mot throttled)."""
    for dt, temp, mot in echantillons:
        sonde.lectures["temp"], sonde.lectures["mot"] = temp, mot
        sonde.releve(now + dt)
    return sonde.bilan(now + echantillons[-1][0], sonde.lectures["mot"])


# ---------------------------------------------------------------- le pic, pas l'instantané
def test_le_bilan_retient_le_pic_pas_la_derniere_mesure(sonde):
    """Tout l'objet du ticket : à 3 h le Pi est à 50 °C, ça ne dit rien de sa journée."""
    b = nourrir(sonde, 1000.0, [(0, 52.0, "0x0"), (30, 81.7, "0x20002"), (60, 50.1, "0x20000")])
    assert b["temperature_c"] == 50.1          # l'instantané reste disponible
    assert b["temperature_max_c"] == 81.7      # mais c'est le pic qui compte
    assert b["temperature_max_il_y_a_min"] == 0


def test_le_moment_du_pic_est_date(sonde):
    b = nourrir(sonde, 1000.0, [(0, 84.4, "0x20002")] + [(30 * i, 55.0, "0x20000") for i in range(1, 41)])
    assert b["temperature_max_c"] == 84.4
    assert b["temperature_max_il_y_a_min"] == 20      # 40 mesures × 30 s


# ---------------------------------------------------------------- bridage : seuls les bits bas comptent
@pytest.mark.parametrize("mot, bride", [
    ("0x0", False),
    ("0x20000", False),     # « la fréquence a DÉJÀ été bridée » : collant depuis le démarrage, ne dit rien de maintenant
    ("0xa0000", False),
    ("0x2", True),          # bridé en ce moment
    ("0x20002", True),      # ce qu'on a mesuré en journée le 21/09
    ("0x50005", True),
])
def test_seuls_les_bits_bas_disent_ce_qui_se_passe_maintenant(sonde, mot, bride):
    b = nourrir(sonde, 1000.0, [(0, 70.0, mot)])
    assert b["bride_maintenant"] is bride


def test_la_part_de_temps_bride_est_comptee(sonde):
    b = nourrir(sonde, 1000.0, [(30 * i, 70.0, "0x20002" if i < 3 else "0x20000") for i in range(10)])
    assert b["bride_part"] == 0.3
    assert b["bride_maintenant"] is False       # plus maintenant, mais 30 % du temps observé


# ---------------------------------------------------------------- fenêtre glissante
def test_les_mesures_de_plus_de_24_h_sont_oubliees(sonde):
    b = nourrir(sonde, 1000.0, [(0, 90.0, "0x20002"),                  # vieux pic
                                (25 * 3600, 60.0, "0x0"),              # plus de 24 h après
                                (25 * 3600 + 30, 61.0, "0x0")])
    assert b["temperature_max_c"] == 61.0       # le pic de la veille est sorti de la fenêtre
    assert b["fenetre_h"] == 0.0


def test_la_fenetre_dit_sur_quelle_duree_porte_le_pic(sonde):
    """Un suivi redémarré il y a deux minutes ne prouve pas que la journée a été calme."""
    b = nourrir(sonde, 1000.0, [(0, 60.0, "0x0"), (120, 62.0, "0x0")])
    assert b["fenetre_h"] == 0.0
    b = nourrir(sonde, 1000.0, [(6 * 3600, 62.0, "0x0")])
    assert b["fenetre_h"] == 6.0


# ---------------------------------------------------------------- robustesse : jamais casser le suivi
def test_une_sonde_muette_ne_fait_pas_tomber_le_suivi(sonde):
    """Le relevé vit dans la boucle temps réel du suivi Kinect : il n'a pas le droit de lever."""
    sonde.lectures["temp"], sonde.lectures["mot"] = None, None
    sonde.releve(1000.0)
    b = sonde.bilan(1000.0, None)
    assert b == {"throttled": None}


@pytest.mark.parametrize("mot", ["pas du tout hexadécimal", "", None, "throttled=", "0x"])
def test_un_mot_throttled_illisible_ne_casse_rien(sonde, mot):
    """Si vcgencmd change un jour de format, le suivi Kinect doit continuer de tourner, pas tomber."""
    sonde.lectures["mot"] = mot
    sonde.releve(1000.0)
    assert sonde.bilan(1000.0, mot)["bride_maintenant"] is False


# ---------------------------------------------------------------- le verdict de l'analyste sur ce relevé
@pytest.fixture
def incidents_depuis(monkeypatch):
    """L'analyste, privé de ses accès distants : il lit les journaux du Pi et du cerveau par SSH et `bash -lc`,
    et aucun test de ce dépôt ne touche au réseau (c'est 24 s à lui seul sinon)."""
    import sys
    sys.path.insert(0, os.path.join(DEPOT, "agent"))
    import analyste
    monkeypatch.setattr(analyste, "sh", lambda *a, **kw: "")
    return analyste.incidents_depuis


def chauffe(incidents_depuis, **suivi):
    """→ l'incident pi_chauffe levé, ou None."""
    import time
    sante = {"suivi": suivi, "services_vm": {}, "services_pi": {}}
    inc = incidents_depuis(time.time() + 3600, sante)      # fenêtre dans le futur : aucun échange à analyser
    return next((i for i in inc if i["type"] == "pi_chauffe"), None)


def test_un_pi_qui_vit_sa_vie_normale_n_alerte_pas(incidents_depuis):
    """Mesuré le 21/09 : JARVIS oscille entre 73,6 et 79,5 °C sans rien perdre. Alerter là-dessus,
    c'est produire un incident par jour que plus personne ne lira."""
    assert chauffe(incidents_depuis, temperature_max_c=79.5, bride_part=0.0, fenetre_h=24) is None


def test_le_bridage_alerte_meme_sans_temperature_extreme(incidents_depuis):
    """C'est LE signal : le Pi perd vraiment de la fréquence."""
    i = chauffe(incidents_depuis, temperature_max_c=79.0, bride_part=0.15, fenetre_h=24)
    assert i is not None and "15%" in i["resume"]


def test_une_vraie_surchauffe_alerte(incidents_depuis):
    assert chauffe(incidents_depuis, temperature_max_c=84.4, bride_part=0.0, fenetre_h=24) is not None


def test_un_bridage_anecdotique_ne_suffit_pas(incidents_depuis):
    """Quelques secondes bridées sur 24 h, c'est la vie d'un Pi, pas un incident."""
    assert chauffe(incidents_depuis, temperature_max_c=77.0, bride_part=0.01, fenetre_h=24) is None


def test_sans_releve_on_retombe_sur_l_ancien_comportement(incidents_depuis):
    """Tant qu'un Pi n'a pas reçu le nouveau tracker.py, l'analyste doit continuer de juger comme avant."""
    import time
    sante = {"suivi": {}, "pi_temperature": 85.0, "pi_throttled": "0x20002", "services_vm": {}, "services_pi": {}}
    inc = incidents_depuis(time.time() + 3600, sante)
    assert any(i["type"] == "pi_chauffe" for i in inc)
