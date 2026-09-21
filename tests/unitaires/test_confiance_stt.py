"""La confiance de Whisper, et ce que Bulle en fait.

Le 21/09, « Jour à du 9 à la nuit. » a créé un rappel pour 21 h : rien n'empêchait Bulle d'agir sur une phrase
qu'elle n'avait manifestement pas comprise. Le signal existait pourtant — la passerelle STT demande déjà
`verbose_json` à Whisper, qui rend un `avg_logprob` par segment — mais `transcribe()` ne gardait que le texte.

Les valeurs ci-dessous viennent d'un étalonnage fait sur la VM le 21/09 : cinq phrases de synthèse passées à
Whisper, six paliers de bruit, trente transcriptions relues une à une. Il n'y a PAS de frontière nette — les
deux populations se chevauchent largement, et un premier essai trop court (trois phrases) m'avait fait croire
le contraire. Le seuil n'est donc pas une limite mais un arbitrage, et ces deux listes sont là pour que le
prochain qui y touche voie ce qu'il déplace.
"""
import pytest

import mesures
import regles
import server


# ---------------------------------------------------------------- la mesure
def test_la_confiance_est_ponderee_par_la_duree():
    """Un « oui » d'un dixième de seconde ne doit pas peser autant qu'une phrase de trois secondes."""
    conf = server.confiance({"segments": [{"start": 0.0, "end": 3.0, "avg_logprob": -0.30},
                                          {"start": 3.0, "end": 3.1, "avg_logprob": -1.50}]})
    assert -0.35 < conf < -0.30                   # la phrase l'emporte largement sur l'interjection


def test_un_seul_segment_donne_sa_propre_valeur():
    assert server.confiance({"segments": [{"start": 0.0, "end": 1.5, "avg_logprob": -0.512}]}) == pytest.approx(-0.512)


@pytest.mark.parametrize("reponse", [
    {"text": ""},                                              # la passerelle a rejeté avant : format simple
    {"segments": []},
    {"segments": [{"start": 0.0, "end": 1.0}]},                # segment sans mesure
    {"segments": [{"start": 2.0, "end": 2.0, "avg_logprob": None}]},
])
def test_sans_mesure_la_confiance_est_inconnue(reponse):
    """None, et surtout pas zéro : zéro serait la meilleure confiance possible, donc le contraire du vrai."""
    assert server.confiance(reponse) is None


# ---------------------------------------------------------------- la décision
# Les trente mesures de l'étalonnage, rangées par ce que la transcription valait vraiment.
JUSTES = [-0.445, -0.537, -0.489, -0.265, -0.446, -0.545, -0.584, -0.620, -0.206, -0.236, -0.327]
FAUSSES = [-0.786, -0.772, -0.843, -0.483, -0.612, -0.437, -0.750, -0.718, -0.535, -0.753,
           -0.301, -0.350, -0.548, -0.702, -0.667, -0.500, -0.597, -0.768, -0.769]


def test_aucune_transcription_juste_n_est_ecartee():
    """Faire répéter Greg pour rien est une régression, pas un moindre mal. La pire bonne mesure est à -0,62 :
    la marge est mince, et c'est elle que ce test protège."""
    assert [c for c in JUSTES if server.douteuse(c)] == []


def test_le_seuil_attrape_une_part_utile_des_mauvaises():
    """Un seuil qui n'attrape plus rien ne serait qu'une illusion de garde-fou — et il en coûterait le
    soupçon qu'on a traité le problème."""
    attrapees = [c for c in FAUSSES if server.douteuse(c)]
    assert len(attrapees) >= len(FAUSSES) // 2


def test_sans_mesure_on_n_ecarte_rien():
    """Un énoncé qu'on n'a pas mesuré ne doit pas être écarté sur une valeur inventée."""
    assert not server.douteuse(None)


def test_sans_seuil_le_controle_est_inactif(monkeypatch):
    """Vider la clé dans regles.yaml doit suffire à revenir au comportement d'avant, sans déploiement."""
    monkeypatch.setattr(regles, "c", lambda cle, defaut=None: None if cle == "confiance_stt_min" else defaut)
    assert not server.douteuse(-2.0)


def test_le_seuil_du_depot_tient_les_deux_bouts():
    """Le réglage livré doit satisfaire les deux tests ci-dessus. Écrit à part pour que le message soit clair
    quand quelqu'un déplace la valeur dans regles.yaml sans relire l'étalonnage."""
    seuil = float(regles.c("confiance_stt_min"))
    assert max(JUSTES) > seuil, "le seuil écarterait des transcriptions justes"
    assert seuil > min(FAUSSES), "le seuil n'attraperait plus rien"


# ---------------------------------------------------------------- vie privée et mesures
def test_la_raison_est_declaree_dans_les_mesures():
    """Une raison absente de RAISONS est rangée dans « autre » : l'incident deviendrait invisible du tableau."""
    assert "transcription_douteuse" in mesures.RAISONS


def test_la_confiance_part_en_mesure_sans_rien_du_salon():
    """Un nombre, jamais le texte — la règle qui ne se discute pas."""
    mesures.confiance(-0.42)
    rendu = mesures.rendu()
    assert "bulle_confiance_transcription" in rendu
    assert "-0.42" in rendu
