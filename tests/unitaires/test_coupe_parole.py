"""Le coupe-parole : pouvoir interrompre Bulle pendant qu'elle parle — et la mesure qui le rend réglable.

Première marche du full-duplex (`connaissances/full-duplex.md`). Une seule ligne de `compagnon.py` rendait la
chaîne half-duplex : le micro est coupé tant que Bulle parle, sans quoi elle s'entend et se répond. On ne le
rouvre pas pour autant — on mesure d'abord **de combien une vraie voix doit dépasser sa propre fuite**, puis on
coupe seulement au-dessus de ce seuil.

Silero ne sert à rien ici, et c'est contre-intuitif : la synthèse de Bulle *est* une voix, le détecteur la
reconnaîtrait comme telle. Seul le niveau sépare quelqu'un qui parle de ce que le haut-parleur renvoie.

`compagnon.py` vit sur le Pi et importe `sounddevice` : on ne teste donc ici que ce qui est pur — la fenêtre de
fuite et le calcul du seuil — en chargeant le module sans ses dépendances matérielles.
"""
import importlib.util
import os
import sys
import time
import types

import pytest

DEPOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def cp():
    """`compagnon.py` chargé sans carte son ni micro : `sounddevice` et `websockets` sont remplacés par des
    objets vides le temps de l'import. Le Pi a ces modules, la CI non — et rien de ce qu'on teste ici ne les
    touche."""
    faux = {n: types.ModuleType(n) for n in ("sounddevice", "websockets")}
    faux["websockets"].exceptions = types.SimpleNamespace(ConnectionClosed=Exception)
    anciens = {n: sys.modules.get(n) for n in faux}
    sys.modules.update(faux)
    try:
        spec = importlib.util.spec_from_file_location("compagnon_test", os.path.join(DEPOT, "compagnon.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for n, ancien in anciens.items():
            if ancien is None: sys.modules.pop(n, None)
            else: sys.modules[n] = ancien


# ---------------------------------------------------------------- la mesure de fuite
def test_la_fuite_retient_le_pic(cp):
    f = cp.Fuite()
    for niveau in (120.0, 450.0, 200.0):
        f.bloc(niveau)
    assert f.pic() == 450.0


def test_une_fuite_ancienne_ne_compte_plus(cp):
    """Le volume change, la Kinect bouge, la pièce n'est pas la même le soir : un pic d'il y a des heures ne
    dit plus rien du seuil d'aujourd'hui."""
    f = cp.Fuite()
    f.bloc(450.0)
    f._quand = time.time() - cp.Fuite.FENETRE - 1
    assert f.pic() == 0.0


def test_sans_mesure_la_fuite_est_nulle(cp):
    assert cp.Fuite().pic() == 0.0


# ---------------------------------------------------------------- le seuil qui en découle
def _listener(cp, pic):
    """Un Listener sans micro : seuls `fuite` et `_seuil_coupe` nous intéressent."""
    lis = cp.Listener.__new__(cp.Listener)
    lis.fuite = cp.Fuite()
    if pic: lis.fuite.bloc(pic)
    return lis


def test_le_seuil_suit_la_fuite_mesuree(cp, monkeypatch):
    monkeypatch.setattr(cp, "COUPE_MARGE", 2.5)
    monkeypatch.setattr(cp, "COUPE_NIVEAU_MIN", 100.0)
    assert _listener(cp, 400.0)._seuil_coupe() == 1000.0


def test_le_plancher_protege_au_demarrage(cp, monkeypatch):
    """Sans plancher, un Pi qui vient de démarrer aurait un seuil à zéro : le premier craquement de parquet
    ferait taire Bulle."""
    monkeypatch.setattr(cp, "COUPE_MARGE", 2.5)
    monkeypatch.setattr(cp, "COUPE_NIVEAU_MIN", 400.0)
    assert _listener(cp, 0.0)._seuil_coupe() == 400.0
    assert _listener(cp, 10.0)._seuil_coupe() == 400.0


# ---------------------------------------------------------------- le garde-fou par défaut
def test_le_coupe_parole_est_desactive_par_defaut(cp):
    """Tant que la fuite n'a pas été regardée dans `bulle_fuite_haut_parleur`, l'activer reviendrait à laisser
    Bulle se couper la parole à elle-même. Le défaut du code et celui de regles.yaml doivent concorder."""
    import yaml
    assert cp.COUPE_ACTIF is False
    regles = yaml.safe_load(open(os.path.join(DEPOT, "config", "regles.yaml"), encoding="utf-8"))
    assert ((regles.get("client") or {}).get("coupe_parole") or {}).get("actif") is False


def test_les_reglages_du_coupe_parole_sont_relus(cp, tmp_path, monkeypatch):
    import yaml
    fichier = tmp_path / "regles.yaml"
    fichier.write_text(yaml.safe_dump({"client": {"coupe_parole": {"actif": True, "marge": 4, "niveau_min": 900,
                                                                   "blocs": 5, "apres_s": 1.5}}}), encoding="utf-8")
    monkeypatch.setenv("BULLE_REGLES", str(fichier))
    try:
        cp._regles_client()
        assert cp.COUPE_ACTIF is True and cp.COUPE_MARGE == 4 and cp.COUPE_NIVEAU_MIN == 900
        assert cp.COUPE_BLOCS == 5 and cp.COUPE_APRES_S == 1.5
    finally:
        cp.COUPE_ACTIF, cp.COUPE_MARGE, cp.COUPE_NIVEAU_MIN = False, 2.5, 400.0
        cp.COUPE_BLOCS, cp.COUPE_APRES_S = 3, 0.6


# ---------------------------------------------------------------- ce que le cerveau en fait
def test_la_fuite_arrive_jusqu_a_la_jauge():
    """Le Pi la joint à chaque énoncé ; sans la liste blanche du cerveau elle serait jetée en silence."""
    sys.path.insert(0, os.path.join(DEPOT, "cerveau"))
    import mesures
    import server
    assert "fuite" in server.SIGNAUX
    assert server._signaux_propres({"fuite": 312.4})["fuite"] == 312.4
    mesures.fuite(312.4)
    assert "bulle_fuite_haut_parleur 312.4" in mesures.rendu()
