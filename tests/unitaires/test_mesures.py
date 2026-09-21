"""Ce que Bulle publie sur /metrics — et surtout ce qu'elle n'y publie pas.

Le tableau de bord « Bulle » de Grafana repose entièrement sur ces mesures : si le cerveau cesse de s'annoncer au
relais ou si une étiquette change de nom, les panneaux se vident sans rien dire. Et comme /metrics est ouvert
(comme /health), tout ce qui y entre est lisible par n'importe qui sur le réseau local : le salon est une pièce
habitée, il ne doit jamais rien en sortir d'autre que des nombres.
"""
import re

import pytest

import mesures
import server


@pytest.fixture(autouse=True)
def compteurs_neufs():
    mesures._compteurs.clear(); mesures._jauges.clear(); mesures._outils_vus.clear()
    yield
    mesures._compteurs.clear(); mesures._jauges.clear(); mesures._outils_vus.clear()


def valeur(texte, debut):
    """La valeur de la première ligne de /metrics qui commence par `debut`."""
    for ligne in texte.splitlines():
        if ligne.startswith(debut) and not ligne.startswith("#"):
            return float(ligne.rsplit(" ", 1)[1])
    return None


# ---------------------------------------------------------------- l'identité au relais
def test_le_cerveau_s_annonce_au_relais():
    """Sans agent à nous, le collecteur ia-usage range Bulle dans « système / scripts et tests », un bac que le
    tableau de bord exclut : elle disparaît du classement alors qu'elle consomme (constaté le 21/09/2026)."""
    assert "bulle" in server.http.headers["user-agent"].lower()
    assert server.AGENT_HTTP != server.AGENT_HTTP_BANC        # le banc de nuit n'est pas le salon
    assert "banc" in server.AGENT_HTTP_BANC.lower()


# ---------------------------------------------------------------- les trois étapes d'un échange
def test_les_trois_etapes_sont_comptees_separement():
    mesures.etape("transcription", 0.4)
    mesures.etape("reflexion", 2.5)
    mesures.etape("reflexion", 1.5, mode="banc")
    mesures.etape("synthese", 0.9)
    t = mesures.rendu()
    assert valeur(t, 'bulle_etape_secondes_total{etape="transcription",mode="salon"}') == 0.4
    assert valeur(t, 'bulle_etape_secondes_total{etape="reflexion",mode="salon"}') == 2.5
    assert valeur(t, 'bulle_etape_secondes_total{etape="reflexion",mode="banc"}') == 1.5
    assert valeur(t, 'bulle_etape_secondes_total{etape="synthese",mode="salon"}') == 0.9


def test_une_etape_inconnue_n_ouvre_pas_de_serie():
    mesures.etape("pensée magique", 3.0)
    assert "pens" not in mesures.rendu()


def test_un_echec_de_synthese_est_compte_a_part():
    mesures.etape("synthese", 0.2, ok=False)
    t = mesures.rendu()
    assert valeur(t, 'bulle_etape_erreurs_total{etape="synthese",mode="salon"}') == 1
    assert valeur(t, 'bulle_etape_appels_total{etape="synthese",mode="salon"}') == 1


# ---------------------------------------------------------------- vie privée
def test_une_raison_inconnue_ne_devient_pas_une_etiquette():
    """Une raison d'écarter un énoncé est un mot choisi dans server.py. Si un jour l'une d'elles charriait un
    bout de phrase, elle vivrait des mois dans Prometheus : on ne laisse passer que l'ensemble connu."""
    mesures.ignore("Tu veux du café ou du thé ?")
    t = mesures.rendu()
    assert "café" not in t
    assert valeur(t, 'bulle_enonces_ignores_total{raison="autre"}') == 1


def test_les_raisons_ecartees_par_le_cerveau_sont_toutes_declarees():
    """Ajouter une raison dans server.py sans l'ajouter ici la ferait silencieusement tomber dans « autre »."""
    source = open(server.__file__, encoding="utf-8").read()
    for raison in re.findall(r'mesures\.ignore\("([^"]+)"\)', source):
        assert raison in mesures.RAISONS, raison
    assert len(re.findall(r'mesures\.ignore\(', source)) == len(mesures.RAISONS)


def test_un_nom_d_outil_invente_ne_fait_pas_enfler_prometheus():
    """Le nom vient des `tool_calls` du modèle : il peut être inventé, et donc contenir ce qu'il vient
    d'entendre. On le nettoie, et au-delà de OUTILS_MAX noms distincts on cesse d'ouvrir des séries."""
    mesures.outil("Allume la lumière du salon s'il te plaît")
    assert "lumi" not in mesures.rendu() and "salon" not in mesures.rendu()
    assert 'outil="invente"' in mesures.rendu()
    for i in range(mesures.OUTILS_MAX + 10):
        mesures.outil(f"outil_invente_{i}")
    t = mesures.rendu()
    assert len([l for l in t.splitlines() if l.startswith("bulle_outils_total")]) <= mesures.OUTILS_MAX + 2
    assert 'outil="autre"' in t


def test_aucune_mesure_ne_transporte_de_texte_libre():
    mesures.echange("voix", 3.0, 1.1); mesures.outil("meteo"); mesures.ignore("pas_nomme")
    mesures.etape("reflexion", 2.0); mesures.caracteres_dits(120); mesures.modele("gpt-oss:20b-32k")
    for ligne in mesures.rendu().splitlines():
        if ligne.startswith("#"): continue
        for etiquette in re.findall(r'(\w+)="([^"]*)"', ligne):
            cle, val = etiquette
            assert cle in ("etape", "mode", "raison", "outil", "issue", "source", "modele"), cle
            assert " " not in val or cle == "modele", val


# ---------------------------------------------------------------- forme du document servi
def test_le_rendu_est_du_texte_prometheus_valide():
    mesures.echange("banc", 1.0); mesures.clients(1); mesures.modele("gpt-oss:20b-32k")
    lignes = mesures.rendu().rstrip("\n").split("\n")
    declares = set()
    for ligne in lignes:
        if ligne.startswith("# HELP") or ligne.startswith("# TYPE"):
            declares.add(ligne.split()[2]); continue
        m = re.match(r'^([a-z_]+)(\{[^}]*\})? (-?[\d.e+]+)$', ligne)
        assert m, ligne
        assert m.group(1) in declares, ligne          # une série sans HELP/TYPE n'est pas documentée
        assert m.group(1) in mesures.AIDE, m.group(1)
    assert "bulle_up 1" in lignes


def test_le_cerveau_sert_les_mesures_sans_jeton():
    """La sonde de Prometheus vit sur CT101 ; y recopier le jeton de Bulle serait une machine de plus à
    compromettre pour une page qui ne dit rien de privé."""
    chemins = {r.path for r in server.app.routes if hasattr(r, "path")}
    assert "/metrics" in chemins
    source = open(server.__file__, encoding="utf-8").read()
    bloc = source.split('@app.get("/metrics"')[1].split("@app.get")[0]
    assert "jeton_valide" not in bloc


# ---------------------------------------------------------------- le délai qu'on peut citer en public
def test_le_delai_ressenti_est_distinct_du_temps_du_modele():
    """`premiere_phrase` s'arrête quand la phrase entre dans la file : ni la transcription en amont, ni la
    synthèse en aval n'y sont. Citer ce chiffre comme « le temps avant qu'elle parle » serait faux — et c'est
    exactement ce qu'un lecteur comprendrait (relevé le 21/09 en préparant la publication)."""
    mesures.echange("voix", 5.0, 1.6)
    mesures.premier_son(4.3)
    t = mesures.rendu()
    assert valeur(t, 'bulle_premiere_phrase_secondes_total{source="voix"}') == 1.6
    assert valeur(t, "bulle_premier_son_secondes_total") == 4.3
    assert valeur(t, "bulle_premiers_sons_total") == 1


def test_le_delai_ressenti_se_cumule_pour_faire_une_moyenne():
    for d in (3.0, 4.0, 5.0):
        mesures.premier_son(d)
    t = mesures.rendu()
    assert valeur(t, "bulle_premier_son_secondes_total") == 12
    assert valeur(t, "bulle_premiers_sons_total") == 3


def test_les_deux_series_sont_documentees():
    """Une serie sans HELP ni TYPE n'est pas lisible par qui reprend le tableau de bord."""
    assert "bulle_premier_son_secondes_total" in mesures.AIDE
    assert "bulle_premiers_sons_total" in mesures.AIDE
