"""Reposer une question veut dire « vérifie », pas « répète » (cerveau/server.py, _sans_redite).

Le 21/09 à 18h43 Greg demande la météo : l'outil est appelé, la carte s'affiche. À 18h45 il la redemande :
le modèle retrouve sa réponse dans l'historique et la ressert AU CARACTÈRE PRÈS, sans rappeler l'outil — donc
sans carte, et avec une température qui pouvait dater. C'est aussi le « tu répètes deux fois les mêmes
réponses » qu'il avait signalé le 20/09.
"""
import server


def fil(*paires):
    m = []
    for q, r in paires:
        m += [{"role": "user", "content": q}, {"role": "assistant", "content": r}]
    return m


def test_la_meme_question_retire_la_reponse_precedente():
    h = fil(("quelle est la météo aujourd'hui ?", "Il fait 12,7 °C à Montréal."))
    assert server._sans_redite(h, "quelle est la météo aujourd'hui?") == []


def test_une_reformulation_proche_compte_comme_la_meme_question():
    """À la voix, deux fois la même demande ne donne jamais deux fois la même transcription."""
    h = fil(("Quelle est la météo aujourd'hui ?", "Il fait 12,7 °C."))
    assert server._sans_redite(h, "quelle est la meteo aujourd hui") == []


def test_une_question_de_suite_garde_tout_le_fil():
    """« Et demain ? » n'a de sens qu'avec ce qui précède : on ne doit surtout pas le couper."""
    h = fil(("quelle est la météo aujourd'hui ?", "Il fait 12,7 °C à Montréal."))
    assert server._sans_redite(h, "et demain ?") == h


def test_une_autre_question_garde_tout_le_fil():
    h = fil(("quelle est la météo aujourd'hui ?", "Il fait 12,7 °C à Montréal."))
    assert server._sans_redite(h, "allume la lumière du salon") == h


def test_seule_la_paire_concernee_est_retiree():
    """Le reste de la conversation sert encore : on coupe la redite, pas la mémoire."""
    h = fil(("allume le salon", "C'est allumé."), ("quelle est la météo ?", "Il fait 12,7 °C."))
    assert server._sans_redite(h, "quelle est la météo ?") == h[:2]


def test_un_fil_vide_ou_court_ne_pose_pas_de_probleme():
    assert server._sans_redite([], "quelle est la météo ?") == []
    court = [{"role": "assistant", "content": "Bonjour."}]
    assert server._sans_redite(court, "quelle est la météo ?") == court


def test_une_question_vide_ne_coupe_rien():
    h = fil(("quelle est la météo ?", "Il fait 12,7 °C."))
    assert server._sans_redite(h, "") == h
