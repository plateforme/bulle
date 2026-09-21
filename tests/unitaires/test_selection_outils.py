"""La sélection d'outils : ce qu'on tend au modèle pour une phrase donnée (cerveau/selection_outils.py).

Un trou ici ne casse rien de visible — Bulle répond simplement « je ne sais pas encore le faire » pour une
chose qu'elle sait faire, et personne ne peut deviner pourquoi en lisant le journal. D'où le filet principal,
`test_chaque_cas_du_banc_garde_ses_outils` : il rejoue les 52 phrases de tests/banc.yaml et exige que la
sélection laisse passer l'outil attendu **et** les outils interdits. Les interdits comptent autant : un cas qui
oppose `jouer_favoris` à `commande_media` ne prouve plus rien si la sélection a retiré `commande_media` du
catalogue avant que le modèle ait eu à choisir.
"""
import os

import yaml

import regles
import selection_outils as sel

ICI = os.path.dirname(os.path.abspath(__file__))
BANC = os.path.join(os.path.dirname(ICI), "banc.yaml")

# Le catalogue tel que /health le donne en production. Écrit à la main à dessein : un outil ajouté aux serveurs
# OpenAPI sans groupe dans regles.yaml ne serait jamais sélectionné, et c'est cette liste qui le fait voir.
CATALOGUE = (
    "afficher", "agenda_du_jour", "allumer", "allumer_tout", "aujourdhui", "calculer_echeance", "calendrier_seances",
    "commande_media", "creer_brouillon", "creer_evenement", "creer_rappel", "derniers_courriels", "difference",
    "envoyer_courriel", "etat_appareil", "etat_maison", "eteindre", "eteindre_tout", "feries", "get_devices",
    "get_playback", "gpu_usage_by_person", "itineraire", "jouer_favoris", "jouer_morceau", "jouer_playlist",
    "lancer_scene", "lieu", "liker_morceau", "lire_courriel", "list_playlists", "lister_rappels", "lister_souvenirs",
    "meteo_actuelle", "oublier", "retenir",
    "mode_club", "mode_nuit", "next_track", "notifier_telephone", "pause", "play", "play_search", "previous_track",
    "previsions", "prochains_evenements", "rechercher_courriels", "reglage", "save_tracks", "server_status",
    "set_volume", "supprimer_evenement", "supprimer_rappel", "transfer_playback", "twenty_execute_tool",
    "twenty_get_tool_catalog", "twenty_learn_tools", "volume_musique",
)
SPECS = [{"type": "function", "function": {"name": n, "parameters": {}}} for n in CATALOGUE]


def offerts(phrase, deja=()):
    return {sp["function"]["name"] for sp in sel.choisir(SPECS, phrase, deja)}


def cas_du_banc():
    with open(BANC, encoding="utf-8") as f:
        return yaml.safe_load(f)["cas"]


# ---------------------------------------------------------------- le filet principal
def test_chaque_cas_du_banc_garde_ses_outils():
    """Le banc juge l'agent de nuit ; la sélection ne doit jamais lui retirer une pièce du problème."""
    manques = []
    for cas in cas_du_banc():
        phrase = cas["phrase"]
        vus = offerts(phrase)
        attendus = cas.get("outil") or []
        attendus = [attendus] if isinstance(attendus, str) else list(attendus)
        for nom in attendus + list(cas.get("interdits") or []):
            if nom in CATALOGUE and nom not in vus:
                manques.append(f"{phrase!r} → {nom} absent (groupes : {sel.groupes(phrase) or 'aucun'})")
    assert not manques, "mots manquants dans cerveau.selection_outils de regles.yaml :\n" + "\n".join(manques)


def test_les_groupes_ne_nomment_que_des_outils_qui_existent():
    """Une faute de frappe dans regles.yaml donne un groupe qui ne sélectionne rien, en silence."""
    cfg = regles.c("selection_outils", {}) or {}
    inconnus = []
    for nom, g in (cfg.get("groupes") or {}).items():
        for motif in list((g or {}).get("outils") or []) + list(cfg.get("toujours") or []):
            if not any(regles.correspond(o, [motif]) for o in CATALOGUE):
                inconnus.append(f"{nom} → {motif}")
    assert not inconnus, "outils nommés dans regles.yaml mais absents du catalogue : " + ", ".join(inconnus)


# ---------------------------------------------------------------- la sélection sélectionne vraiment
def test_la_musique_ne_tend_pas_les_lumieres():
    vus = offerts("Joue ma playlist electronic jazz.")
    assert "jouer_playlist" in vus and "play_search" in vus
    assert "allumer_tout" not in vus and "envoyer_courriel" not in vus
    assert len(vus) < len(CATALOGUE)


def test_les_lumieres_ne_tendent_pas_spotify():
    vus = offerts("Ferme les lumieres du salon.")
    assert {"eteindre", "eteindre_tout", "allumer"} <= vus      # les confusions restent possibles : c'est voulu
    assert "play_search" not in vus and "jouer_favoris" not in vus


def test_les_accents_ne_changent_rien():
    """La phrase arrive telle que Whisper l'écrit ; les motifs, eux, sont écrits sans accents."""
    assert offerts("Ferme les lumières.") == offerts("Ferme les lumieres.")
    assert "eteindre_tout" in offerts("Éteins toutes les lumières de la maison.")


def test_afficher_est_toujours_tendu():
    """Épeler un mot ou proposer un choix numéroté peut arriver sur n'importe quel sujet."""
    for phrase in ("Joue Daft Punk.", "Quelle est la meteo ?", "Ferme les lumieres."):
        assert "afficher" in offerts(phrase), phrase


# ---------------------------------------------------------------- toutes les portes de sortie mènent au catalogue
def test_une_phrase_qu_aucun_groupe_ne_reconnait_donne_tout():
    """Se tromper en envoyant trop coûte du GPU ; se tromper en envoyant trop peu rend Bulle incapable."""
    assert offerts("Explique-moi la difference entre un virus et une bacterie.") == set(CATALOGUE)


def test_actif_false_rend_le_catalogue_complet(monkeypatch):
    """Le coupe-circuit : un réglage à chaud doit suffire à revenir au comportement d'avant."""
    vrai = regles.c
    monkeypatch.setattr(regles, "c", lambda cle, defaut=None: (
        dict(vrai("selection_outils", {}) or {}, actif=False) if cle == "selection_outils" else vrai(cle, defaut)))
    assert offerts("Joue ma playlist electronic jazz.") == set(CATALOGUE)


def test_section_absente_rend_le_catalogue_complet(monkeypatch):
    """Un regles.yaml plus ancien que le code — le cas d'un déploiement à moitié fait."""
    vrai = regles.c
    monkeypatch.setattr(regles, "c", lambda cle, defaut=None: ({} if cle == "selection_outils" else vrai(cle, defaut)))
    assert offerts("Joue ma playlist electronic jazz.") == set(CATALOGUE)


def test_au_dela_du_plafond_on_renvoie_tout(monkeypatch):
    """Sélectionner cinquante outils sur cinquante-cinq ne gagne rien et ajoute un risque de trou."""
    vrai = regles.c
    monkeypatch.setattr(regles, "c", lambda cle, defaut=None: (
        dict(vrai("selection_outils", {}) or {}, plafond=3) if cle == "selection_outils" else vrai(cle, defaut)))
    assert offerts("Joue ma playlist electronic jazz.") == set(CATALOGUE)


# ---------------------------------------------------------------- les tours suivants
def test_un_outil_deja_appele_reste_offert():
    """Retirer le schéma d'un outil dont la trace de l'échange parle encore, c'est inviter le modèle à le
    rappeler de travers au tour suivant."""
    vus = offerts("Ferme les lumieres.", deja={"get_playback"})
    assert "get_playback" in vus
    assert "play_search" not in vus                 # le reste de la musique n'entre pas pour autant


def test_un_outil_deja_appele_qui_n_existe_pas_n_entre_pas():
    """Le nom vient des tool_calls du modèle : il peut être inventé de toutes pièces."""
    assert "displayer" not in offerts("Ferme les lumieres.", deja={"displayer"})
