"""Fabrique l'arbre PUBLIC de Bulle : le dépôt tel qu'on peut le montrer, sans ce qui décrit la maison.

  python outils/exporter_public.py --verifier          ne produit rien, dit seulement ce qui fuirait
  python outils/exporter_public.py ../BulleAI-public   écrit l'arbre expurgé dans ce dossier (qui doit être vide)

Pourquoi un export plutôt qu'un ménage dans le dépôt (décidé le 21/09/2026, avant la première publication) :

  - Le dépôt qui fait foi pilote une maison habitée. Ses adresses, son `HA_URL` par défaut, le contact exigé
    par Nominatim sont des valeurs DE PRODUCTION : les retirer du code pour pouvoir le montrer, c'est risquer
    la maison pour une vitrine. Ici le code source ne change pas ; seule la copie publiée est réécrite.
  - Trois agents commitent vingt fois par jour, et un commentaire « à la maison » cite volontiers une adresse
    (c'est même la consigne : raconter l'incident). Un ménage fait une fois serait défait dans la semaine.
    D'où le FILET : des motifs génériques qui font échouer l'export — et `pytest` — dès qu'une fuite revient.
  - L'historique contient les mêmes adresses et un courriel dans chacun de ses commits. On ne le réécrit pas :
    l'arbre exporté part dans un dépôt NEUF, avec son propre historique. Ne jamais rendre public le dépôt
    privé lui-même — son historique, ses journaux d'Actions et ses branches d'agent viendraient avec.

Ce qui est propre à la maison (quoi remplacer par quoi) vit dans `outils/public.yaml`, qui n'est JAMAIS exporté :
il contient par construction tout ce qu'on veut taire. Le filet, lui, est générique et vit ici, en clair.

N'importe RIEN du cerveau, comme `valider.py` : il doit tourner sur une machine nue.
"""
import argparse
import os
import re
import subprocess
import sys

import yaml

DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE = "outils/public.yaml"

# Le filet. Volontairement générique : il doit attraper la fuite que personne n'a encore écrite, pas seulement
# celles qu'on connaît. Les plages 192.0.2.x / 198.51.100.x (RFC 5737, réservées à la documentation) ne sont pas
# dedans : c'est vers elles qu'on réécrit.
FILET = [
    ("adresse de réseau privé", r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    ("adresse Tailscale (CGNAT)", r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    ("nom de machine Tailscale", r"[A-Za-z0-9-]+\.ts\.net\b"),
    ("adresse de courriel", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    ("numéro de téléphone", r"(?<![\w.])\+\d{10,13}\b"),
]
# Ce qui ressemble à un courriel sans en être un, ou qui n'a rien de privé.
COURRIELS_ADMIS = {"getty@tty1.service", "bulle-agent@local"}
DOMAINES_ADMIS = ("@example.org", "@example.com", "@example.net")     # RFC 2606 : n'appartiennent à personne


def charger_table(depot=DEPOT):
    """La table privée — vide si elle manque, ce qui est le cas normal dans le dépôt PUBLIC.

    Là-bas l'arbre est déjà expurgé : le filet seul doit passer, et il sert alors à ce qu'une contribution
    extérieure ne réintroduise pas une adresse.
    """
    try:
        with open(os.path.join(depot, TABLE), encoding="utf-8") as f:
            t = yaml.safe_load(f) or {}
    except FileNotFoundError:
        t = {}
    return {"exclus": list(t.get("exclus") or []), "renommer": dict(t.get("renommer") or {}),
            "remplacements": [tuple(r) for r in (t.get("remplacements") or [])],
            "interdits": list(t.get("interdits") or [])}


def fichiers_suivis(depot=DEPOT):
    """{chemin: octets} des fichiers suivis par git, lus dans le répertoire de travail.

    Le répertoire de travail et non `git archive HEAD` : le test doit juger ce qu'on s'apprête à commiter, pas ce
    qui l'est déjà. Les fichiers non suivis (etat/, journaux, notes de travail) ne partent donc jamais.
    """
    noms = subprocess.run(["git", "-C", depot, "ls-files", "-z"], capture_output=True, check=True).stdout
    arbre = {}
    for nom in noms.decode("utf-8").split("\0"):
        if nom and os.path.isfile(os.path.join(depot, nom)):      # un fichier supprimé mais pas encore commité
            with open(os.path.join(depot, nom), "rb") as f:
                arbre[nom] = f.read()
    return arbre


def _texte(octets):
    try:
        return octets.decode("utf-8")
    except UnicodeDecodeError:
        return None                                               # image, police : ni réécrit, ni inspecté


def _courriel_admis(adresse):
    return adresse in COURRIELS_ADMIS or adresse.endswith(DOMAINES_ADMIS)


def transformer(arbre, table):
    """Applique la table à un arbre {chemin: octets} → (arbre public, fuites).

    Fonction pure, sans disque ni git : c'est elle que les tests exercent, avec des arbres fabriqués.
    Une fuite est un triplet (chemin, numéro de ligne, nature) — JAMAIS le texte trouvé : ce rapport finit dans
    un terminal, un journal de CI ou une conversation, et il n'a pas à y recopier ce qu'il protège.
    """
    exclus = set(table["exclus"]) | {TABLE}                       # la table contient tout ce qu'on veut taire
    public = {}
    for chemin, octets in arbre.items():
        if chemin in exclus or any(chemin.startswith(e.rstrip("/") + "/") for e in exclus):
            continue
        texte = _texte(octets)
        if texte is not None:
            for motif, remplacement in table["remplacements"]:
                texte = re.sub(motif, remplacement, texte)
            octets = texte.encode("utf-8")
        public[table["renommer"].get(chemin, chemin)] = octets

    filet = FILET + [("motif propre à la maison", m) for m in table["interdits"]]
    fuites = []
    for chemin in sorted(public):
        texte = _texte(public[chemin])
        if texte is None: continue
        for n, ligne in enumerate(texte.splitlines(), 1):
            for nature, motif in filet:
                for trouve in re.finditer(motif, ligne):
                    if nature == "adresse de courriel" and _courriel_admis(trouve.group(0).lower()): continue
                    fuites.append((chemin, n, nature))
    return public, fuites


def exporter(destination, depot=DEPOT):
    """Écrit l'arbre public dans `destination`. Renvoie les fuites ; s'il y en a, rien n'est écrit."""
    public, fuites = transformer(fichiers_suivis(depot), charger_table(depot))
    if fuites:
        return fuites
    for chemin, octets in public.items():
        cible = os.path.join(destination, chemin)
        os.makedirs(os.path.dirname(cible) or ".", exist_ok=True)
        with open(cible, "wb") as f:
            f.write(octets)
    return []


def main():
    ap = argparse.ArgumentParser(description="Fabrique l'arbre public de Bulle, ou vérifie qu'il serait propre.")
    ap.add_argument("destination", nargs="?", help="dossier à créer (ou vide) où écrire l'arbre public")
    ap.add_argument("--verifier", action="store_true", help="ne rien écrire : lister seulement ce qui fuirait")
    a = ap.parse_args()
    if a.verifier == bool(a.destination):
        ap.error("donner une destination, ou --verifier — l'un ou l'autre")

    if a.verifier:
        _, fuites = transformer(fichiers_suivis(), charger_table())
    else:
        # Un export se fait depuis un arbre propre : sinon on publie un état que personne n'a commité ni testé.
        sale = subprocess.run(["git", "-C", DEPOT, "status", "--porcelain", "--untracked-files=no"],
                              capture_output=True, text=True, check=True).stdout.strip()
        if sale:
            sys.exit("Des fichiers suivis sont modifiés : commiter ou remiser avant d'exporter.\n" + sale)
        if os.path.isdir(a.destination) and os.listdir(a.destination):
            sys.exit(f"{a.destination} n'est pas vide : l'export n'écrase rien, choisir un dossier neuf.")
        fuites = exporter(a.destination)

    if fuites:
        print(f"{len(fuites)} fuite(s) — rien n'a été écrit :", file=sys.stderr)
        for chemin, n, nature in fuites:
            print(f"  {chemin}:{n} : {nature}", file=sys.stderr)
        print(f"Corriger la source, ou ajouter un remplacement dans {TABLE}.", file=sys.stderr)
        sys.exit(1)
    if a.verifier:
        print("Rien ne fuit : l'arbre public serait propre.")
    else:
        print(f"Arbre public écrit dans {a.destination}.")
        print("Il reste à y lancer `pytest` (les remplacements ne doivent rien casser), puis à le commiter dans le")
        print("dépôt PUBLIC — un dépôt neuf, jamais le dépôt privé rendu visible.")


if __name__ == "__main__":
    main()
