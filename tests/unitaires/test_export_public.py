"""L'export public (outils/exporter_public.py) : ce qui décrit la maison ne sort pas du dépôt.

Écrit le 21/09/2026, avant la première publication. L'audit n'a trouvé aucun jeton, mais un plan d'adressage
complet, le nom du tailnet, un courriel et un point « maison » précis à onze mètres — rien de secret isolément,
une cible une fois réuni. Le dépôt reçoit vingt commits par jour de trois agents dont la consigne est de
raconter les incidents, adresses comprises : sans ce test, la prochaine fuite partirait à la prochaine
publication sans que personne l'ait décidé.

Les exemples ci-dessous sont FABRIQUÉS (plages de documentation, domaines réservés) : ce fichier est exporté,
lui, et il n'a pas à contenir ce qu'il interdit.
"""
import os
import sys

import pytest

ICI = os.path.dirname(os.path.abspath(__file__))
DEPOT = os.path.dirname(os.path.dirname(ICI))
sys.path.insert(0, os.path.join(DEPOT, "outils"))

import exporter_public as ep  # noqa: E402

VIDE = {"exclus": [], "renommer": {}, "remplacements": [], "interdits": []}
# Assemblées à l'exécution : écrites en clair, ces chaînes feraient échouer l'export de CE fichier.
PRIVEE = ".".join(["192", "168", "7", "42"])
TAILNET = "grafana-truc.tail0000aa" + ".ts" + ".net"
COURRIEL = "quelquun" + "@" + "fournisseur.fr"


def natures(arbre, table=VIDE):
    return [nature for _, _, nature in ep.transformer(arbre, table)[1]]


# --- le filet : ce qu'il doit attraper sans qu'on le lui ait appris

@pytest.mark.parametrize("ligne, nature", [
    (f'PI = "bulle@{PRIVEE}"', "adresse de réseau privé"),
    ("ssh root@" + ".".join(["10", "0", "0", "5"]), "adresse de réseau privé"),
    ("# vu sur " + ".".join(["172", "20", "1", "9"]), "adresse de réseau privé"),
    ("relais : " + ".".join(["100", "101", "12", "3"]), "adresse Tailscale (CGNAT)"),
    (f"[tableau](https://{TAILNET}/d/bulle)", "nom de machine Tailscale"),
    (f"contact : {COURRIEL}", "adresse de courriel"),
    ("SIGNAL_NUMERO=+" + "15145550199", "numéro de téléphone"),
])
def test_le_filet_attrape(ligne, nature):
    assert nature in natures({"x.py": ligne.encode()})


@pytest.mark.parametrize("ligne", [
    "Détails : http://192.0.2.31:8802/suivi",                       # plage de documentation : c'est la cible des remplacements
    "After=getty@tty1.service",                                     # une unité systemd, pas un courriel
    "UA = 'BulleAI/1.0 (contact : contact@example.org)'",
    "OS Version: Windows 11 Home 10.0.26200",                       # trois nombres : pas une adresse
    "m_par_px = 156543.03392 * cos(lat) / 2 ** zoom",
    "volume : +10 %",
])
def test_le_filet_laisse_passer(ligne):
    assert natures({"x.py": ligne.encode()}) == []


def test_une_fuite_ne_recopie_jamais_ce_qu_elle_a_trouve():
    """Le rapport finit dans un terminal ou un journal de CI : il nomme l'endroit et la nature, pas la valeur."""
    _, fuites = ep.transformer({"a/b.py": f"x = 1\nh = '{PRIVEE}'\n".encode()}, VIDE)
    assert fuites == [("a/b.py", 2, "adresse de réseau privé")]
    assert PRIVEE not in repr(fuites)


# --- la table : remplacer, exclure, renommer

def test_un_remplacement_garde_l_architecture_lisible():
    table = dict(VIDE, remplacements=[(r"192\.168\.7\.(\d{1,3})", r"192.0.2.\1")])
    public, fuites = ep.transformer({"x.py": f"PI = '{PRIVEE}'".encode()}, table)
    assert public["x.py"] == b"PI = '192.0.2.42'" and fuites == []


def test_un_interdit_de_la_maison_rattrape_un_remplacement_devenu_insuffisant():
    """Le remplacement vise une tournure ; si la phrase est reformulée, c'est l'interdit qui arrête l'export."""
    table = dict(VIDE, remplacements=[("chez Machin", "à la maison")], interdits=["(?i)machin"])
    assert natures({"x.md": "Le salon de MACHIN".encode()}, table) == ["motif propre à la maison"]
    assert natures({"x.md": "Le salon, chez Machin".encode()}, table) == []


def test_la_table_privee_ne_sort_jamais():
    """Même si personne ne pense à l'exclure : elle contient par construction tout ce qu'on veut taire."""
    public, _ = ep.transformer({ep.TABLE: f"interdits: ['{PRIVEE}']".encode(), "x.py": b"pass"}, VIDE)
    assert list(public) == ["x.py"]


def test_exclure_un_dossier_et_renommer_un_fichier():
    table = dict(VIDE, exclus=["notes"], renommer={"README.en.md": "README.md", "README.md": "README.fr.md"})
    public, _ = ep.transformer({"notes/a.md": b"x", "notes_publiques.md": b"y",
                                "README.md": b"fr", "README.en.md": b"en"}, table)
    assert public == {"notes_publiques.md": b"y", "README.fr.md": b"fr", "README.md": b"en"}


def test_un_fichier_binaire_passe_tel_quel():
    png = b"\x89PNG\r\n\x1a\n\xff\xfe" + PRIVEE.encode()              # illisible en UTF-8 : ni réécrit, ni inspecté
    public, fuites = ep.transformer({"planche.png": png}, VIDE)
    assert public["planche.png"] == png and fuites == []


# --- le dépôt lui-même

@pytest.mark.parametrize("note", ["connaissances/produit.md", "connaissances/publication.md"])
def test_les_notes_de_strategie_ne_partent_pas(note):
    """Non versionnées le 21/09/2026, exclues d'avance : le jour où on les commite, elles ne doivent pas suivre."""
    if not os.path.exists(os.path.join(DEPOT, ep.TABLE)):
        pytest.skip("pas de table privée ici : c'est le dépôt public, où ces notes n'existent pas")
    table = ep.charger_table(DEPOT)
    public, _ = ep.transformer({note: b"x", "connaissances/full-duplex.md": b"y"}, table)
    assert list(public) == ["connaissances/full-duplex.md"]


def test_le_depot_est_exportable():
    """Le vrai juge : les fichiers suivis, avec la table, ne laissent rien passer.

    S'il échoue, le message dit où et quoi (sans la valeur). Deux issues : corriger la source si l'adresse
    n'avait rien à y faire, ou ajouter un remplacement dans outils/public.yaml si c'est une valeur de production.
    """
    try:
        arbre = ep.fichiers_suivis(DEPOT)
    except (OSError, ep.subprocess.CalledProcessError):
        pytest.skip("pas de git ici (le Pi, une archive) : rien à exporter")
    _, fuites = ep.transformer(arbre, ep.charger_table(DEPOT))
    assert fuites == [], "\n" + "\n".join(f"  {c}:{n} : {nature}" for c, n, nature in fuites)
