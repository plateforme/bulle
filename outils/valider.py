"""Valide les fichiers YAML du dépôt : syntaxe ET structure attendue.

  python outils/valider.py            valide tout, code de sortie 1 s'il y a une erreur

L'orchestrateur de nuit vérifie déjà qu'un YAML modifié par l'agent se charge (`agent/boucle.py`), mais « se
charge » ne veut pas dire « est utilisable » : un `cas:` renommé, une fiche de panne sans `id`, une section
`cerveau:` disparue passent le `yaml.safe_load` et ne se voient qu'au prochain démarrage du cerveau — donc en
production. Ce script est le même contrôle, en plus strict, appelable par la CI comme par la boucle de nuit.

N'importe RIEN du cerveau : il doit tourner sur une machine nue, sans dépendance ni base de données.
"""
import os
import re
import sys

import yaml

DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Clés reconnues d'un cas du banc — une faute de frappe (« interdit » au lieu d'« interdits ») rend le cas
# silencieusement permissif : il ne vérifie plus rien et passe toujours.
CLES_CAS = {"phrase", "outil", "args", "interdits", "aucun_outil", "reponse", "reponse_sans", "carte", "origine"}
GABARITS = {"liste", "cles", "media", "texte", "lieu", "aucune"}


def _charger(chemin, erreurs):
    try:
        with open(os.path.join(DEPOT, chemin), encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        erreurs.append(f"{chemin} : fichier absent")
    except yaml.YAMLError as e:
        erreurs.append(f"{chemin} : YAML illisible — {str(e).splitlines()[0]}")
    return None


def valider_regles(erreurs):
    d = _charger("config/regles.yaml", erreurs)
    if d is None: return
    for section in ("cerveau", "nuit", "client", "suivi", "journal"):
        if not isinstance(d.get(section), dict):
            erreurs.append(f"config/regles.yaml : section « {section} » absente ou mal formée")
    # Une durée de vie à zéro effacerait le journal à chaque nuit, et une valeur non numérique ferait échouer la
    # purge en silence. C'est la section où une faute de frappe coûte des données qu'on ne récupère pas.
    for cle, valeur in (d.get("journal") or {}).items():
        if not isinstance(valeur, (int, float)) or isinstance(valeur, bool) or valeur < 1:
            erreurs.append(f"config/regles.yaml : journal.{cle} doit être un nombre de jours d'au moins 1 (lu : {valeur!r})")
    c = d.get("cerveau") or {}
    if not c.get("nom"):
        erreurs.append("config/regles.yaml : cerveau.nom est vide — Bulle ne se reconnaîtrait plus")
    if not c.get("nom_variantes"):
        erreurs.append("config/regles.yaml : cerveau.nom_variantes est vide — plus aucun réveil ne fonctionnerait")
    for liste in ("consignes", "retour_negatif", "lecture_seule", "hallucinations"):
        if liste in c and not isinstance(c[liste], list):
            erreurs.append(f"config/regles.yaml : cerveau.{liste} devrait être une liste")
    # un outil à effet glissé en lecture seule s'exécuterait POUR DE VRAI à chaque passage du banc
    for interdit in ("allumer", "eteindre", "allumer_tout", "eteindre_tout", "mode_club", "play", "pause"):
        if interdit in (c.get("lecture_seule") or []):
            erreurs.append(f"config/regles.yaml : « {interdit} » ne peut pas être en lecture seule (il agit sur la maison)")
    valider_selection_outils(c, erreurs)


def valider_selection_outils(c, erreurs):
    """La sélection d'outils (cerveau/selection_outils.py) échoue en SILENCE : un motif qui ne compile pas, un
    groupe sans « outils », et Bulle se retrouve simplement incapable d'une chose qu'elle sait faire — sans une
    ligne dans le journal. C'est exactement ce que ce script existe pour attraper avant le démarrage."""
    sel = c.get("selection_outils")
    if sel is None: return                      # section absente = sélection désactivée, c'est un choix valide
    if not isinstance(sel, dict):
        erreurs.append("config/regles.yaml : cerveau.selection_outils devrait être une section")
        return
    if not isinstance(sel.get("plafond", 30), int) or sel.get("plafond", 30) < 1:
        erreurs.append(f"config/regles.yaml : selection_outils.plafond doit être un entier d'au moins 1 (lu : {sel.get('plafond')!r})")
    groupes = sel.get("groupes")
    if sel.get("actif") and not isinstance(groupes, dict):
        erreurs.append("config/regles.yaml : selection_outils est actif mais n'a aucun groupe")
        return
    for nom, g in (groupes or {}).items():
        ou = f"config/regles.yaml : selection_outils.groupes.{nom}"
        if not isinstance(g, dict) or not g.get("mots") or not g.get("outils"):
            erreurs.append(f"{ou} : il faut « mots » et « outils », sinon le groupe ne sélectionne rien")
            continue
        try:
            re.compile(g["mots"])
        except re.error as e:
            erreurs.append(f"{ou} : motif illisible — {e}")
        # Les motifs sont comparés à la phrase SANS ACCENTS : « lumière » ne peut correspondre à rien, jamais.
        accents = "".join(sorted({ch for ch in g["mots"] if ch in "àâäéèêëîïôöùûüçÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ"}))
        if accents:
            erreurs.append(f"{ou} : le motif contient des accents ({accents}) et ne correspondra jamais")
        if not isinstance(g["outils"], list):
            erreurs.append(f"{ou} : « outils » devrait être une liste")


def valider_banc(erreurs):
    d = _charger("tests/banc.yaml", erreurs)
    if d is None: return
    cas = d.get("cas")
    if not isinstance(cas, list) or not cas:
        erreurs.append("tests/banc.yaml : « cas » absent ou vide")
        return
    phrases = set()
    for i, c in enumerate(cas):
        ou = f"tests/banc.yaml, cas {i + 1}"
        if not isinstance(c, dict) or not c.get("phrase"):
            erreurs.append(f"{ou} : pas de « phrase »")
            continue
        ou = f"tests/banc.yaml, « {c['phrase'][:40]} »"
        inconnues = set(c) - CLES_CAS
        if inconnues:
            erreurs.append(f"{ou} : clé(s) inconnue(s) {sorted(inconnues)} — le cas ne vérifierait rien")
        if c.get("carte") and c["carte"] not in GABARITS:
            erreurs.append(f"{ou} : gabarit « {c['carte']} » inconnu (attendu : {', '.join(sorted(GABARITS))})")
        if not ({"outil", "interdits", "aucun_outil", "reponse", "reponse_sans", "carte"} & set(c)):
            erreurs.append(f"{ou} : le cas n'attend rien, il passera toujours")
        # deux cas identiques ne sont pas une erreur (une même phrase peut vérifier l'outil puis la carte),
        # mais deux cas identiques AU MÊME attendu sont une copie oubliée
        signature = (c["phrase"], str(sorted(c.items(), key=lambda kv: kv[0])))
        if signature in phrases:
            erreurs.append(f"{ou} : cas en double")
        phrases.add(signature)


def valider_pannes(erreurs):
    d = _charger("connaissances/pannes.yaml", erreurs)
    if d is None: return
    fiches = d.get("pannes")
    if not isinstance(fiches, list) or not fiches:
        erreurs.append("connaissances/pannes.yaml : « pannes » absent ou vide")
        return
    vus = set()
    for f in fiches:
        if not isinstance(f, dict) or not f.get("id"):
            erreurs.append(f"connaissances/pannes.yaml : fiche sans « id » ({str(f)[:60]})")
            continue
        if f["id"] in vus:
            erreurs.append(f"connaissances/pannes.yaml : identifiant « {f['id']} » en double")
        vus.add(f["id"])
        for zone in f.get("zone") or []:
            if "/" in zone and not os.path.exists(os.path.join(DEPOT, zone)):
                erreurs.append(f"connaissances/pannes.yaml, fiche « {f['id']} » : zone « {zone} » n'existe pas")


def main():
    erreurs = []
    valider_regles(erreurs)
    valider_banc(erreurs)
    valider_pannes(erreurs)
    if erreurs:
        print(f"{len(erreurs)} problème(s) :")
        for e in erreurs:
            print(" -", e)
        return 1
    print("config/regles.yaml, tests/banc.yaml, connaissances/pannes.yaml : valides")
    return 0


if __name__ == "__main__":
    sys.exit(main())
