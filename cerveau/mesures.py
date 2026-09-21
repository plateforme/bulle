"""Ce que Bulle coûte au reste du stack, exposé à Prometheus sur /metrics.

Bulle n'a pas de carte graphique à elle : elle emprunte la RTX 3090 de la VM, partagée avec Open WebUI, l'IDE d'un
collègue et l'agent de nuit. Un échange la traverse en trois étapes — transcription (Whisper), réflexion (Ollama),
synthèse (Kyutai) — et **une seule des trois passe par le relais nginx** que le collecteur `ia-usage` sait lire.
Les deux autres n'étaient comptées nulle part : le 21/09/2026, le tableau de bord « IA locale » attribuait zéro
minute à Bulle alors qu'elle parlait toute la journée. D'où ce module, qui mesure les trois aux mêmes bornes.

**Rien de ce qui se dit au salon n'entre ici.** Pas de transcription, pas de réponse, pas de texte de carte : des
compteurs, des durées, un nombre de caractères. Les deux seules étiquettes qui viennent d'ailleurs que d'un
ensemble écrit en dur sont filtrées — la raison d'ignorer un énoncé (`RAISONS`) et le nom d'un outil, que le LLM
peut inventer de toutes pièces et qui pourrait donc charrier du texte entendu dans le salon.

/metrics est ouvert, comme /health et pour la même raison : c'est une sonde, elle ne dit rien de privé, et
Prometheus tourne sur CT101 où le jeton de Bulle n'a rien à faire.
"""
import re
import time
from collections import defaultdict

# Raisons d'ignorer un énoncé, telles que server.py les envoie au client et au journal. Ensemble fermé : une
# raison inconnue devient « autre » plutôt que de devenir une étiquette, parce qu'une étiquette Prometheus est
# gardée des mois et qu'on ne veut pas découvrir un jour du texte du salon dedans.
RAISONS = ("silence", "hallucination", "pas_nomme", "transcription_douteuse")
ETAPES = ("transcription", "reflexion", "synthese")
MODES = ("salon", "banc")
OUTILS_MAX = 60          # au-delà, on cesse d'ouvrir des séries : un LLM qui bafouille des noms d'outils
                         # inventés ferait sinon enfler la base de Prometheus sans rien apprendre à personne
# Un vrai nom d'outil est un identifiant : pas d'espace, pas d'accent, pas de phrase. On ne NETTOIE pas un nom
# inventé — retirer les espaces de « allume la lumière du salon » en garde tous les mots — on le refuse en bloc.
_NOM_OUTIL = re.compile(r"[a-z][a-z0-9_]{0,39}$")

_debut = time.time()
_compteurs = defaultdict(float)     # (nom, étiquettes triées) → valeur
_jauges = {}
_outils_vus = set()


def _c(nom, valeur=1.0, **etiquettes):
    _compteurs[(nom, tuple(sorted(etiquettes.items())))] += valeur


def echange(source, secondes, premiere_phrase=None):
    """Un échange complet, du texte reçu à la dernière phrase dite. `source` : voix | texte | banc."""
    source = source if source in ("voix", "texte", "banc") else "texte"
    _c("bulle_echanges_total", source=source)
    _c("bulle_reponse_secondes_total", secondes, source=source)
    if premiere_phrase is not None:
        # Le délai avant la PREMIÈRE phrase est ce que Greg ressent comme la vitesse de Bulle : la suite se dit
        # pendant que le modèle écrit encore. La moyenne des réponses complètes, elle, dépend surtout du nombre
        # d'outils appelés.
        _c("bulle_premiere_phrase_secondes_total", premiere_phrase, source=source)
        _c("bulle_premieres_phrases_total", source=source)


def premier_son(secondes):
    """Le délai que Greg RESSENT : de la fin de son énoncé au premier son qui sort de la TV.

    Distinct de `premiere_phrase`, qui ne compte que le modèle : celui-ci ajoute la transcription en amont et la
    synthèse de la première phrase en aval. C'est le seul des deux qu'on puisse citer en public sans tricher —
    personne dans un salon ne chronomètre une file d'attente interne (21/09).
    """
    _c("bulle_premier_son_secondes_total", float(secondes))
    _c("bulle_premiers_sons_total")


def ignore(raison):
    """Un énoncé entendu qui n'était pas pour Bulle. On ne compte QUE la raison — voir l'en-tête."""
    _c("bulle_enonces_ignores_total", raison=raison if raison in RAISONS else "autre")


def etape(nom, secondes, mode="salon", ok=True):
    """Le temps qu'a pris une des trois étapes. C'est du temps de calcul emprunté au GPU commun."""
    if nom not in ETAPES: return
    mode = mode if mode in MODES else "salon"
    _c("bulle_etape_secondes_total", secondes, etape=nom, mode=mode)
    _c("bulle_etape_appels_total", etape=nom, mode=mode)
    if not ok:
        _c("bulle_etape_erreurs_total", etape=nom, mode=mode)


def selection(offerts, catalogue):
    """Combien d'outils ont été tendus au modèle sur les combien qu'il en existe (selection_outils.py).

    C'est la seule façon de savoir si la sélection travaille : le rapport dit ce qu'on économise, et
    `bulle_selections_completes_total` compte les phrases qu'aucun groupe n'a reconnues, donc les mots qui
    manquent dans regles.yaml."""
    _c("bulle_selection_outils_offerts_total", float(offerts))
    _c("bulle_selection_outils_catalogue_total", float(catalogue))
    _c("bulle_selections_total")
    if offerts >= catalogue:
        _c("bulle_selections_completes_total")


def caracteres_dits(n):
    _c("bulle_caracteres_synthetises_total", float(n))


def outil(nom, ok=True):
    """Un outil appelé. Le nom vient des `tool_calls` du LLM : il est nettoyé et plafonné avant de devenir une
    étiquette, parce qu'un modèle qui hallucine peut y mettre n'importe quoi, y compris ce qu'il vient d'entendre."""
    n = (nom or "").strip().lower()
    if not n:
        n = "inconnu"
    elif not _NOM_OUTIL.match(n):
        n = "invente"
    elif n not in _outils_vus:
        if len(_outils_vus) >= OUTILS_MAX:
            n = "autre"
        else:
            _outils_vus.add(n)
    _c("bulle_outils_total", outil=n, issue="ok" if ok else "erreur")


def fuite(niveau):
    """Le pic de niveau que le haut-parleur renvoie dans les micros du Pi pendant que Bulle parle.

    C'est le chiffre qui decide si le coupe-parole est reglable (connaissances/full-duplex.md) : tant qu'on ne
    sait pas de combien une vraie voix le depasse, rouvrir le micro pendant qu'elle parle reviendrait a la
    laisser se repondre a elle-meme. Une jauge et pas un compteur : c'est un niveau, pas un cumul.
    """
    _jauges["bulle_fuite_haut_parleur"] = float(niveau)


def confiance(valeur):
    """La confiance de Whisper dans la dernière transcription (moyenne des avg_logprob, pondérée par la durée).

    Une jauge, comme `fuite`, et pour la même raison : c'est le chiffre qui doit décider d'un seuil qu'on a
    calibré sur des phrases de synthèse et pas sur le vrai salon. Une jauge ne retient que la dernière valeur
    entre deux relevés de Prometheus — assez pour voir où se situe une soirée ordinaire, pas pour compter les
    cas limites. Si un jour il faut cette finesse, ce sera un histogramme.

    Rien du salon n'entre ici : c'est un nombre, jamais le texte.
    """
    _jauges["bulle_confiance_transcription"] = float(valeur)


def clients(n):
    _jauges["bulle_clients_connectes"] = n


def modele(nom):
    _jauges["bulle_modele_info"] = ("modele", nom)


# ---------------------------------------------------------------- rendu Prometheus
AIDE = {
    "bulle_echanges_total": ("Échanges menés à terme, par source (voix, texte, banc de tests)", "counter"),
    "bulle_reponse_secondes_total": ("Temps cumulé des réponses complètes (à diviser par bulle_echanges_total)", "counter"),
    "bulle_premiere_phrase_secondes_total": ("Temps cumulé jusqu'à la première phrase mise en file (modèle seul)", "counter"),
    "bulle_premier_son_secondes_total": ("Temps cumulé de la fin de l'énoncé au premier son (transcription + modèle + synthèse)", "counter"),
    "bulle_premiers_sons_total": ("Énoncés ayant abouti à un son", "counter"),
    "bulle_premieres_phrases_total": ("Réponses ayant produit au moins une phrase", "counter"),
    "bulle_enonces_ignores_total": ("Énoncés entendus mais écartés, par raison (rien du texte n'est conservé)", "counter"),
    "bulle_etape_secondes_total": ("Temps de calcul emprunté au GPU par étape (transcription, réflexion, synthèse)", "counter"),
    "bulle_etape_appels_total": ("Appels par étape", "counter"),
    "bulle_etape_erreurs_total": ("Échecs par étape", "counter"),
    "bulle_selection_outils_offerts_total": ("Outils tendus au modèle, cumulés sur tous les tours", "counter"),
    "bulle_selection_outils_catalogue_total": ("Outils qui existaient à ces mêmes tours (à diviser pour le taux)", "counter"),
    "bulle_selections_total": ("Tours d'outils ayant fait l'objet d'une sélection", "counter"),
    "bulle_selections_completes_total": ("Tours où aucun groupe n'a reconnu la phrase : catalogue complet envoyé", "counter"),
    "bulle_caracteres_synthetises_total": ("Caractères envoyés à la synthèse vocale", "counter"),
    "bulle_outils_total": ("Appels d'outils, par outil et par issue", "counter"),
    "bulle_fuite_haut_parleur": ("Niveau renvoyé par le haut-parleur dans les micros pendant que Bulle parle", "gauge"),
    "bulle_clients_connectes": ("Clients connectés au /ws (le Pi du salon)", "gauge"),
    "bulle_demarrage_timestamp": ("Démarrage du cerveau (horodatage Unix)", "gauge"),
    "bulle_up": ("1 si le cerveau répond", "gauge"),
    "bulle_modele_info": ("Modèle de langage utilisé, en étiquette", "gauge"),
}


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _ligne(nom, etiquettes, valeur):
    lbl = "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in etiquettes) + "}" if etiquettes else ""
    return f"{nom}{lbl} {valeur:g}" if isinstance(valeur, float) else f"{nom}{lbl} {valeur}"


def rendu():
    """Le texte servi sur /metrics."""
    valeurs = defaultdict(list)
    for (nom, etiquettes), v in _compteurs.items():
        valeurs[nom].append((etiquettes, round(v, 3)))
    valeurs["bulle_up"].append(((), 1))
    valeurs["bulle_demarrage_timestamp"].append(((), int(_debut)))
    for nom, j in _jauges.items():
        valeurs[nom].append(((j,), 1) if isinstance(j, tuple) else ((), j))
    L = []
    for nom in sorted(valeurs):
        aide, typ = AIDE.get(nom, ("", "gauge"))
        L.append(f"# HELP {nom} {aide}")
        L.append(f"# TYPE {nom} {typ}")
        for etiquettes, v in sorted(valeurs[nom]):
            L.append(_ligne(nom, etiquettes, v))
    return "\n".join(L) + "\n"
