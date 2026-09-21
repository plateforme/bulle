"""Choisir les outils envoyés au LLM pour une phrase, au lieu de lui tendre tout le catalogue.

Les cinquante-cinq outils de Bulle partaient dans le prompt à CHAQUE tour d'outil de CHAQUE échange : leurs
descriptions et leurs schémas font l'essentiel du préremplissage, payé sur la 3090 partagée avant même que le
modèle commence à écrire. Et `alias_outils` dans regles.yaml dit le reste : « displayer », « get_time »,
« twelve_hour_time » — un modèle noyé sous les noms finit par en inventer.

On envoie donc les outils du sujet dont on parle, plus ceux qu'on vient d'appeler. Le tri se fait sur des mots,
dans `cerveau.selection_outils` de regles.yaml : c'est un seuil de plus à régler à chaud, et c'est dans la zone
de l'agent de nuit, qui peut donc rattraper lui-même un synonyme manquant.

**Toutes les portes de sortie mènent au catalogue complet.** Section absente, `actif: false`, aucun groupe
reconnu, sélection vide, sélection trop large pour valoir le coup : on renvoie `specs` tel quel. Se tromper en
envoyant trop coûte du temps de GPU ; se tromper en envoyant trop peu rend Bulle incapable d'une chose qu'elle
sait faire, et ça, ça ne se rattrape pas au tour suivant.

Un piège à garder en tête pour le banc : deux outils que le modèle confond doivent voyager ENSEMBLE. Le cas
« Joue mes titres likés » interdit `commande_media` ; si la sélection le retirait du catalogue dès qu'on parle
de musique, le cas passerait sans plus rien prouver. C'est pourquoi `commande_media` est dans le groupe
« musique » alors qu'il ne sert qu'à la TV — et pourquoi `tests/unitaires/test_selection_outils.py` vérifie que
chaque cas du banc garde son outil attendu **et** ses interdits.
"""
import re
import unicodedata

import regles

_compiles = {}          # motif écrit dans regles.yaml → expression compilée (les règles sont relues à chaud)


def _norm(t):
    return "".join(ch for ch in unicodedata.normalize("NFD", str(t).lower()) if unicodedata.category(ch) != "Mn").strip()


def _regex(motif):
    r = _compiles.get(motif)
    if r is None:
        r = _compiles[motif] = re.compile(motif, re.I)
    return r


def groupes(phrase):
    """Les noms des groupes que cette phrase déclenche. Séparé de `choisir` parce que c'est ce qu'on veut lire
    dans un journal ou un test quand Bulle n'a pas trouvé un outil qu'elle possède."""
    cfg = regles.c("selection_outils", {}) or {}
    p = _norm(phrase)
    return [nom for nom, g in (cfg.get("groupes") or {}).items()
            if (g or {}).get("mots") and _regex(g["mots"]).search(p)]


def choisir(specs, phrase, deja=()):
    """→ la sous-liste de `specs` à envoyer au LLM, dans le même ordre. `deja` : outils déjà appelés dans cet
    échange, qu'on garde visibles — un modèle à qui on retire le schéma d'un outil dont sa propre trace parle
    encore se met à le rappeler de travers."""
    cfg = regles.c("selection_outils", {}) or {}
    if not cfg.get("actif"): return specs
    noms = {sp["function"]["name"] for sp in specs}
    touches = groupes(phrase)
    if not touches: return specs                     # sujet inconnu : on ne devine pas, on donne tout

    motifs = list(cfg.get("toujours") or [])
    for nom in touches:
        motifs += list(((cfg.get("groupes") or {}).get(nom) or {}).get("outils") or [])
    retenus = {n for n in noms if regles.correspond(n, motifs)} | ({n for n in deja} & noms)
    if not retenus: return specs

    # Au-delà du plafond, la sélection ne fait plus gagner grand-chose et n'ajoute que du risque de trou.
    # On ne tronque JAMAIS pour rentrer dedans : couper une liste d'outils au hasard, c'est décider à la place
    # du modèle lequel il n'a pas le droit d'utiliser.
    if len(retenus) > int(cfg.get("plafond", 30)): return specs
    return [sp for sp in specs if sp["function"]["name"] in retenus]
