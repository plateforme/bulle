"""Le réveil : reconnaître « Bulle » dans une transcription, et écarter ce que Whisper invente.

C'est le portier de tout le système. Trop strict, Bulle n'entend pas ; trop laxiste, elle répond à la
télévision. Les deux erreurs sont arrivées les 19 et 20/09, d'où les cas ci-dessous.
"""
import regles
import server


def reveil():
    """(réveil n'importe où, apostrophe de début, apostrophe de fin, nom mal entendu en début)."""
    return regles.regex_nom()


# ---------------------------------------------------------------- reconnaître le nom
def test_le_nom_est_reconnu_n_importe_ou_dans_la_phrase():
    wake = reveil()[0]
    assert wake.search("Bulle, quelle heure est-il ?")
    assert wake.search("Dis-moi Bulle, il fait quel temps ?")
    assert wake.search("Quelle heure est-il, Bulle ?")


def test_une_phrase_sans_le_nom_n_est_pas_pour_bulle():
    wake = reveil()[0]
    assert not wake.search("Tu veux du café ?")
    assert not wake.search("Il est neuf heures et quart.")


def test_les_variantes_de_transcription_reveillent_aussi():
    """Whisper écrit « bull », « bule », « lobule » selon la distance et le bruit."""
    wake = reveil()[0]
    for variante in ("Bull, mets la musique.", "Bule, ferme les lumières.", "Lobule, quelle heure est-il ?"):
        assert wake.search(variante), variante


def test_le_vocatif_est_retire_de_la_demande():
    """Le LLM ne doit pas recevoir « Bulle, » : ça n'ajoute rien et ça déroute le modèle."""
    _, vocatif, vocatif_fin, _ = reveil()
    assert vocatif.sub("", "Bulle, quelle heure est-il ?") == "quelle heure est-il ?"
    assert vocatif.sub("", "Hé Bulle, ferme les lumières.") == "ferme les lumières."
    assert vocatif_fin.sub("", "Quelle heure est-il, Bulle") == "Quelle heure est-il"


def test_le_nom_mal_entendu_n_est_accepte_qu_en_debut_de_phrase():
    """« Boule, ferme la lumière » est pour Bulle ; « une boule de neige » ne l'est pas."""
    mal = reveil()[3]
    assert mal.match("Boule, ferme la lumière.")
    assert not mal.match("Une boule de neige est tombée.")
    assert not mal.match("Il a mangé une pull over boule.")


# ---------------------------------------------------------------- écarter ce que Whisper invente
def test_l_indice_recrache_par_whisper_est_ignore():
    """Incident 20/09 : l'indice sans son premier mot a été pris pour une demande et la musique est partie."""
    assert server._est_indice("Spotify, CXN100, Home Assistant, Montréal, mode club, titres likés")
    assert server._est_indice(regles.c("indice_stt"))


def test_une_vraie_demande_n_est_pas_prise_pour_l_indice():
    assert not server._est_indice("Mets de la musique sur le CXN100.")
    assert not server._est_indice("Quelle heure est-il ?")


def test_un_fragment_trop_court_n_est_pas_pris_pour_l_indice():
    """Sinon « Montréal » seul, une vraie question, serait jeté."""
    assert not server._est_indice("Montréal")
    assert not server._est_indice("mode club")


# ---------------------------------------------------------------- lecture seule
def test_correspond_accepte_le_nom_exact_et_le_prefixe():
    assert regles.correspond("meteo_actuelle", ["meteo_actuelle", "lieu"])
    assert regles.correspond("get_playback", ["get_*"])
    assert regles.correspond("lister_rappels", ["lister_*"])
    assert not regles.correspond("eteindre_tout", ["get_*", "lister_*"])


def test_les_outils_a_effet_ne_sont_pas_en_lecture_seule():
    """Un outil en lecture seule s'exécute VRAIMENT pendant le banc : la liste doit rester serrée."""
    lecture = regles.c("lecture_seule", [])
    for dangereux in ("allumer", "eteindre", "eteindre_tout", "allumer_tout", "mode_club", "play", "pause"):
        assert not regles.correspond(dangereux, lecture), dangereux
