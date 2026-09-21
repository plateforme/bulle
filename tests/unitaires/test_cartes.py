"""Les cartes affichées sur la TV (cerveau/cartes.py).

La carte est le rendu d'un RÉSULTAT D'OUTIL, jamais du texte du LLM : c'est donc du code pur, et tout ce qui
apparaît à l'écran du salon se teste ici sans démarrer quoi que ce soit.

Les fixtures reprennent la forme réelle des réponses d'outils (clés françaises du serveur maison, clés anglaises
de Spotify, enveloppe `result.records` de Twenty).
"""
import json

import cartes


def carte(nom, resultat, args=None):
    return cartes.depuis_outil(nom, args or {}, json.dumps(resultat, ensure_ascii=False))


# ---------------------------------------------------------------- météo
def test_meteo_donne_une_carte_de_cles():
    c = carte("meteo_actuelle", {"ville": "Montréal", "temperature_c": 8.4, "ressenti_c": 5.1,
                                 "vent_kmh": 5.2, "ciel": "ciel dégagé"})
    assert c["gabarit"] == "cles"
    assert c["titre"] == "Montréal"
    assert c["items"][0] == {"cle": "maintenant", "valeur": "8°", "note": "dégagé"}


def test_meteo_arrondit_les_decimales():
    """À 3,5 m de la TV, « 8,4° » et « 8° » se lisent pareil — mais « 5,2 km/h » se lit mal."""
    c = carte("meteo_actuelle", {"ville": "Montréal", "temperature_c": -4.0, "vent_kmh": 5.2})
    assert c["items"][0]["valeur"] == "-4°"
    assert c["items"][2]["valeur"] == "5 km/h"


def test_previsions_garde_trois_jours():
    jours = [{"date": "2026-09-21", "max_c": 12, "ciel": "nuageux"},
             {"date": "2026-09-22", "max_c": 15, "ciel": "ciel dégagé"},
             {"date": "2026-09-23", "max_c": 9, "ciel": "pluie"},
             {"date": "2026-09-24", "max_c": 7, "ciel": "pluie"}]
    c = carte("previsions", {"ville": "Montréal", "jours": jours})
    assert len(c["items"]) == 3
    assert c["items"][0]["cle"] == "lundi"
    assert c["items"][1]["note"] == "dégagé"


def test_previsions_sans_jour_ne_montre_rien():
    assert carte("previsions", {"ville": "Montréal", "jours": []}) is None


# ---------------------------------------------------------------- agenda
def test_agenda_affiche_les_heures_a_la_francaise():
    c = carte("agenda_du_jour", {"date": "Aujourd'hui",
                                 "evenements": [{"debut": "2026-09-21T14:00", "titre": "Dentiste"},
                                                {"debut": "09:30", "titre": "Revue de nuit", "passe": True}]})
    assert c["gabarit"] == "liste"
    assert c["items"][0] == {"cle": "14 h 00", "texte": "Dentiste", "passe": False, "prochain": True}
    assert c["items"][1]["cle"] == "9 h 30"
    assert c["items"][1]["passe"] is True


def test_agenda_marque_le_prochain_evenement():
    """Un seul point corail par carte : le premier événement qui reste à venir, même s'il n'est pas le premier."""
    c = carte("agenda_du_jour", {"date": "Aujourd'hui", "evenements": [
        {"debut": "09:30", "titre": "Revue de nuit", "passe": True},
        {"debut": "14:00", "titre": "Dentiste"},
        {"debut": "18:45", "titre": "Hockey"}]})
    assert [i.get("prochain", False) for i in c["items"]] == [False, True, False]


def test_agenda_tout_passe_n_a_pas_de_prochain():
    """Le soir, tout est derrière : aucune ligne ne doit porter le point (il désignerait un événement fini)."""
    c = carte("agenda_du_jour", {"evenements": [{"debut": "09:30", "titre": "Revue", "passe": True},
                                                {"debut": "14:00", "titre": "Dentiste", "passe": True}]})
    assert all("prochain" not in i for i in c["items"])


def test_evenement_sur_la_journee_n_a_pas_d_heure():
    c = carte("prochains_evenements", {"evenements": [{"date_debut": "2026-09-25", "titre": "Congé"}]})
    assert c["items"][0]["cle"] == "—"


# ---------------------------------------------------------------- musique
def test_musique_donne_une_carte_media_persistante():
    c = carte("get_playback", {"titre": "Digital Love", "artiste": "Daft Punk",
                               "album": "Discovery", "appareil": "CXN100"})
    assert c["gabarit"] == "media"
    assert c["texte"] == "Digital Love"
    assert c["note"] == "Daft Punk · Discovery"
    assert c["accent"] == "CXN100"
    assert c["duree"] == 0        # elle reste tant que ça joue


def test_musique_accepte_les_cles_anglaises_de_spotify():
    c = carte("play_search", {"item": {"name": "Instant Death"}, "artist": "Author & Punisher",
                              "device": "CXN100", "image": "https://exemple/pochette.jpg"})
    assert c["texte"] == "Instant Death"
    assert c["image"] == "https://exemple/pochette.jpg"


def test_musique_sans_titre_ne_montre_rien():
    assert carte("get_playback", {"appareil": "CXN100"}) is None


# ---------------------------------------------------------------- maison
def test_etat_maison_compte_les_lumieres():
    c = carte("etat_maison", {"lumieres_allumees": ["Lampe salon", "Cuisine"],
                              "meteo": {"temperature": 8.4},
                              "lecteurs": [{"nom": "CXN100", "etat": "playing", "titre": "Digital Love"}]})
    assert c["items"][0] == {"cle": "lumières allumées", "valeur": "2"}
    assert c["items"][1]["valeur"] == "8°"
    assert c["items"][2]["cle"] == "CXN100"


# ---------------------------------------------------------------- CRM
def test_taches_du_crm_donnent_une_liste():
    res = {"result": {"count": 4, "records": [
        {"title": "Rappeler le notaire", "dueAt": "2026-09-28T07:00:00.000Z", "status": "TODO"},
        {"title": "Envoyer la facture", "dueAt": "2026-10-02T07:00:00.000Z", "status": "DONE"}]}}
    c = carte("twenty_execute_tool", res, {"toolName": "find_many_tasks"})
    assert c["gabarit"] == "liste"
    assert c["titre"] == "Tâches · 4"
    assert c["items"][0] == {"cle": "28 sept.", "texte": "Rappeler le notaire", "passe": False}
    assert c["items"][1]["passe"] is True


def test_contact_du_crm_utilise_le_nom_composite():
    res = {"result": {"records": [{"name": {"firstName": "Greg", "lastName": "Fabre"}}]}}
    c = carte("twenty_execute_tool", res, {"toolName": "find_many_people"})
    assert c["titre"] == "Contacts"       # un seul résultat : pas de compteur
    assert c["items"][0]["texte"] == "Greg Fabre"


def test_catalogue_d_outils_twenty_ne_s_affiche_pas():
    """Seuls les find_ montrent quelque chose : un schéma d'outil n'a rien à faire sur la TV du salon."""
    assert carte("twenty_execute_tool", {"result": {"records": [{"title": "x"}]}},
                 {"toolName": "get_tool_catalog"}) is None


# ---------------------------------------------------------------- garde-fous
def test_un_outil_sans_carte_ne_montre_rien():
    assert carte("eteindre", {"ok": True, "fait": "1 lumière éteinte"}) is None


def test_un_echec_d_outil_ne_montre_rien():
    """« non_fait » se dit à voix haute ; l'afficher en plus ne servirait qu'à inquiéter."""
    assert carte("meteo_actuelle", {"ok": False, "non_fait": ["service indisponible"]}) is None


def test_un_resultat_qui_n_est_pas_du_json_ne_casse_rien():
    assert cartes.depuis_outil("meteo_actuelle", {}, "Internal Server Error") is None
    assert cartes.depuis_outil("meteo_actuelle", {}, None) is None
