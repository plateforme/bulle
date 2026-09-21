"""Le routage des appels d'outils (cerveau/server.py, Tools.call) — la moitié déterministe de Bulle.

Chaque cas ici correspond à un incident réel des journaux des 19-21/09. Ils sont aujourd'hui jugés par un
aller-retour LLM de deux minutes dans tests/banc.yaml, alors que rien là-dedans ne dépend du modèle : ce qui
est testé, c'est ce que le cerveau FAIT de l'appel une fois que le modèle l'a produit.

Les cas tournent en mode simulation : un outil à effet renvoie alors {"fait": "<nom> (simulé)"}, ce qui nomme
l'outil réellement exécuté — exactement ce qu'on veut vérifier. Un outil en LECTURE SEULE, lui, n'est pas simulé
(c'est tout l'intérêt du mode) : il part vers sa route, qui n'existe pas ici, et renvoie « outil inconnu ».
On lit alors la redirection plutôt que « fait ».
"""
import asyncio
import json

import pytest

import outils_composes as oc
import server


def outils(*noms):
    """Un catalogue d'outils minimal, sans réseau : seuls les noms comptent pour le routage."""
    t = server.Tools()
    t.specs = [{"type": "function", "function": {"name": n, "parameters": {}}} for n in noms]
    return t


def executer(tools, nom, args, said="", sim=True):
    """→ (outil réellement exécuté ou None, arguments réellement passés, redirection éventuelle).

    Synchrone à dessein : `asyncio.run` ici évite d'ajouter pytest-asyncio aux dépendances de la CI.
    Les arguments rendus sont ceux que `call()` dit avoir employés : tantôt le dictionnaire d'origine corrigé
    sur place, tantôt celui d'un appel déballé. C'est cette version-là que `answer()` envoie au client, au
    journal et aux mesures — avant le 21/09 il la reconstituait, et se trompait.
    """
    args = dict(args)
    res, fait, args_faits = asyncio.run(tools.call(nom, args, said, sim=sim))
    d = json.loads(res)
    execute = (d.get("fait") or "").replace(" (simulé)", "") or None
    return execute, args_faits, (fait if fait != nom else None)


@pytest.fixture
def sans_outils_composes(monkeypatch):
    """Vide le catalogue des outils composés : on teste le routage, pas ce que les outils font ensuite.

    Sans ça, `volume_musique` ou `afficher` s'exécuteraient pour de vrai et iraient chercher Spotify.
    """
    monkeypatch.setattr(oc, "OUTILS", {})


# ---------------------------------------------------------------- intervalles Twenty
def test_twenty_deux_operateurs_sur_un_champ_deviennent_un_and():
    """« les tâches de la semaine prochaine » : Twenty refuse deux opérateurs sur le même champ."""
    args = server.twenty_intervalles({"dueAt": {"gte": "2026-09-28", "lte": "2026-10-04"}})
    assert "dueAt" not in args
    assert args["and"] == [{"dueAt": {"gte": "2026-09-28"}}, {"dueAt": {"lte": "2026-10-04"}}]


def test_twenty_un_seul_operateur_est_laisse_tel_quel():
    args = server.twenty_intervalles({"dueAt": {"gte": "2026-09-28"}})
    assert args == {"dueAt": {"gte": "2026-09-28"}}


def test_twenty_ne_touche_pas_un_champ_composite():
    """{"name": {"firstName": …}} n'est pas un intervalle : deux clés, mais ce ne sont pas des opérateurs."""
    args = server.twenty_intervalles({"name": {"firstName": "Greg", "lastName": "Fabre"}})
    assert args == {"name": {"firstName": "Greg", "lastName": "Fabre"}}
    assert "and" not in args


def test_twenty_conserve_un_and_deja_present():
    args = server.twenty_intervalles({"and": [{"status": {"eq": "TODO"}}], "dueAt": {"gte": "1", "lte": "2"}})
    assert {"status": {"eq": "TODO"}} in args["and"]
    assert len(args["and"]) == 3


# ---------------------------------------------------------------- volume relatif (le bug qui baissait le son)
def test_augmente_le_volume_de_10_pourcent_est_une_variation(sans_outils_composes):
    """Incident 19/09 : « augmente le volume de 10 % » réglait le volume À 10 %, donc le son TOMBAIT."""
    _, args, _ = executer(outils("volume_musique"), "volume_musique", {"niveau": 10},
                          said="Augmente le volume de 10 %.")
    assert args["changement"] == 10
    assert args["niveau"] is None


def test_baisse_le_volume_de_10_donne_une_variation_negative(sans_outils_composes):
    _, args, _ = executer(outils("volume_musique"), "volume_musique", {"niveau": 10},
                          said="Baisse le volume de 10 %.")
    assert args["changement"] == -10


def test_mets_le_volume_a_40_reste_un_niveau_absolu(sans_outils_composes):
    """La phrase ne dit pas « de 40 » : c'est bien une consigne absolue, on n'y touche pas."""
    _, args, _ = executer(outils("volume_musique"), "volume_musique", {"niveau": 40},
                          said="Mets le volume de la musique à 40.")
    assert args["niveau"] == 40
    assert not args.get("changement")


def test_volume_pct_est_renomme_en_niveau(sans_outils_composes):
    """Le LLM emprunte le paramètre d'un autre outil ; la correction doit précéder l'exécution."""
    _, args, _ = executer(outils("volume_musique"), "volume_musique", {"volume_pct": 55},
                          said="Mets le volume à 55.")
    assert args["niveau"] == 55
    assert "volume_pct" not in args


# ---------------------------------------------------------------- appels emballés dans Twenty
def test_appel_emballe_dans_twenty_est_redirige(sans_outils_composes):
    """Incident 19/09 : « arrête la musique » arrivait en twenty_execute_tool(toolName="pause")."""
    execute, _, redirige = executer(outils("twenty_execute_tool", "pause"), "twenty_execute_tool",
                                    {"toolName": "pause", "arguments": {}})
    assert execute == "pause"
    assert redirige == "pause"


def test_appel_emballe_deux_fois_est_deballe(sans_outils_composes):
    """Vu le 20/09 : twenty_execute_tool(toolName="twenty_execute_tool", arguments={toolName: "pause"})."""
    execute, _, _ = executer(outils("twenty_execute_tool", "pause"), "twenty_execute_tool",
                             {"toolName": "twenty_execute_tool", "arguments": {"toolName": "pause", "arguments": {}}})
    assert execute == "pause"


def test_correction_de_parametre_apres_deballage(sans_outils_composes):
    """Vu le 20/09 : la correction s'appliquait à « twenty_execute_tool » et ratait la vraie cible.

    Après déballage, c'est le dictionnaire INTERNE qui est corrigé, et c'est lui que `call()` rapporte comme
    « arguments réellement passés » — donc lui que le client, le journal et le banc voient.
    """
    _, args, redirige = executer(outils("twenty_execute_tool", "volume_musique"), "twenty_execute_tool",
                                 {"toolName": "volume_musique", "arguments": {"volume_pct": 55}},
                                 said="Mets le volume à 55.")
    assert redirige == "volume_musique"
    assert args["niveau"] == 55
    assert "volume_pct" not in args


def test_un_vrai_appel_twenty_n_est_pas_redirige(sans_outils_composes):
    """find_many_tasks est un outil de Twenty, pas un des nôtres : il doit rester emballé."""
    _, args, redirige = executer(outils("twenty_execute_tool"), "twenty_execute_tool",
                                 {"toolName": "find_many_tasks", "arguments": {}})
    assert redirige is None
    assert args["toolName"] == "find_many_tasks"


# ---------------------------------------------------------------- musique
def test_play_search_sans_recherche_devient_play(sans_outils_composes):
    """Incident 20/09 : « Joue. » partait en recherche vide (400) puis Bulle annonçait un titre inventé."""
    execute, _, redirige = executer(outils("play_search", "play"), "play_search", {"query": ""}, said="Joue.")
    assert execute == "play"
    assert redirige == "play"


def test_play_search_avec_une_recherche_est_conserve(sans_outils_composes):
    execute, args, _ = executer(outils("play_search", "play"), "play_search",
                                {"query": "Daft Punk"}, said="Joue Daft Punk.")
    assert execute == "play_search"
    assert args["query"] == "Daft Punk"


def test_commande_media_sans_tv_vise_la_musique(sans_outils_composes):
    """La consigne « jamais commande_media pour la musique » ne tenait pas : l'ampli répondait 500."""
    execute, _, redirige = executer(outils("commande_media", "pause"), "commande_media",
                                    {"action": "pause"}, said="Arrête la musique.")
    assert execute == "pause"
    assert redirige == "pause"


def test_commande_media_avec_tv_reste_sur_la_tv(sans_outils_composes):
    execute, _, redirige = executer(outils("commande_media", "pause"), "commande_media",
                                    {"action": "pause"}, said="Mets la TV sur pause.")
    assert execute == "commande_media"
    assert redirige is None


# ---------------------------------------------------------------- noms d'outils inventés
def test_nom_invente_par_le_llm_est_traduit(sans_outils_composes):
    """« displayer » pour « afficher » : table d'alias dans config/regles.yaml."""
    execute, _, _ = executer(outils("afficher"), "displayer", {"texte": "bonjour"})
    assert execute == "afficher"


def test_outil_vraiment_inconnu_remonte_une_erreur(sans_outils_composes):
    """Hors simulation, un nom qu'aucun alias ne rattrape doit dire son ignorance, pas inventer."""
    tools = outils("afficher")
    res, _, _ = asyncio.run(tools.call("teleporter", {}, "", sim=False))
    assert "outil inconnu" in json.loads(res)["erreur"]


def test_prefixe_twelve_retrouve_l_outil_twenty(sans_outils_composes):
    """Le modèle écrit « twelve » pour « twenty ». Le 21/09, « twelve_learn_tools » n'était rattrapé par rien :
    ni la table d'alias, ni la signature (les arguments ne ressemblent à aucun outil composé), et Bulle
    répondait « je ne peux pas arrêter le rappel »."""
    tools = outils("twenty_learn_tools", "twenty_execute_tool")
    res, _, _ = asyncio.run(tools.call("twelve_learn_tools", {"toolNames": ["lister_rappels"]}, "", sim=False))
    assert json.loads(res)["erreur"] == "outil inconnu : twenty_learn_tools"   # le nom a bien été réparé


def test_prefixe_twelve_colle_a_un_outil_maison():
    """« twelve_lancer_scene » : le préfixe se colle aussi à des outils qui n'ont rien de Twenty. Le nom nu
    existe, donc on le prend — et SURTOUT avant la reconnaissance par signature, qui envoyait cet appel sur
    jouer_playlist le 21/09 : « Radio 9 » devenait 683 titres de favoris."""
    execute, _, _ = executer(outils("jouer_playlist", "lancer_scene"), "twelve_lancer_scene", {"nom": "Radio 9"})
    assert execute == "lancer_scene"


def test_prefixe_twelve_sans_cible_ne_devine_rien(sans_outils_composes):
    """Réparer un préfixe est une égalité de noms, pas une devinette : sans cible, on dit son ignorance."""
    tools = outils("afficher")
    res, _, _ = asyncio.run(tools.call("twelve_teleporter", {}, "", sim=False))
    assert json.loads(res)["erreur"] == "outil inconnu : twelve_teleporter"


# ---------------------------------------------------------------- ce que call() dit avoir fait
def test_call_rend_les_arguments_qu_elle_a_reellement_passes():
    """Le vrai défaut du 21/09 n'était pas le routage mais le COMPTE RENDU : `answer()` reconstituait les
    arguments avec `args.get("arguments", {})`, ce qui ne vaut que pour un appel emballé par Twenty. Pour
    toute autre redirection il envoyait un dictionnaire vide — au client, au journal, et au banc, qui juge
    l'agent de nuit sur ces arguments-là."""
    res, fait, args_faits = asyncio.run(
        outils("jouer_playlist", "lancer_scene").call("twelve_lancer_scene", {"nom": "Radio 9"}, "", sim=True))
    assert fait == "lancer_scene"
    assert args_faits == {"nom": "Radio 9"}


def test_call_rend_les_arguments_deballes_d_un_appel_emballe(sans_outils_composes):
    """Emballé par Twenty, c'est l'INTÉRIEUR qui a été exécuté : c'est donc lui qu'il faut rapporter."""
    res, fait, args_faits = asyncio.run(outils("pause", "twenty_execute_tool").call(
        "twenty_execute_tool", {"toolName": "pause", "arguments": {"nom": "salon"}}, "", sim=True))
    assert fait == "pause"
    assert args_faits == {"nom": "salon"}


def test_call_rend_le_nom_repare_meme_sans_redirection(sans_outils_composes):
    """Une réparation qui se contente de renommer restait invisible : `mesures.outil()` recevait « displayer »,
    qui passe le filtre des étiquettes et ouvre sa propre série Prometheus. Soixante saturent le budget, après
    quoi un vrai nouvel outil devient « autre »."""
    res, fait, _ = asyncio.run(outils("afficher").call("displayer", {"texte": "bonjour"}, "", sim=True))
    assert fait == "afficher"


def test_call_rend_le_nom_demande_quand_elle_n_a_rien_change(sans_outils_composes):
    """Sans quoi `answer()` ne pourrait plus distinguer une redirection d'un appel ordinaire."""
    res, fait, args_faits = asyncio.run(outils("afficher").call("afficher", {"texte": "bonjour"}, "", sim=True))
    assert fait == "afficher"
    assert args_faits == {"texte": "bonjour"}
