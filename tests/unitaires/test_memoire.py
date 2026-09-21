"""La mémoire longue : ce que Bulle retient d'une semaine sur l'autre, et à quelles conditions.

C'est la seule table du journal où de la parole survit aux durées de vie de `regles.yaml` (`purger`). Toute la
conception tient donc dans une contrainte : **rien n'y entre sans que Greg l'ait demandé à voix haute**. Pas
d'extraction automatique de ce qui se dit au salon, même quand une phrase a l'air importante. C'est ce qui
permet de garder la règle de vie privée intacte — un souvenir est un choix, pris à voix haute, effaçable à voix
haute, et listable à tout moment.

La littérature 2026 distingue mémoire de travail (les six derniers tours, qu'on a déjà), mémoire épisodique et
mémoire sémantique. Ceci est la sémantique, et seulement sa moitié explicite : l'autre moitié — deviner ce qui
mérite d'être retenu — est précisément ce qu'on refuse de faire ici.
"""
import asyncio
import json

import pytest

import journal
import outils_composes as oc
import regles
import server


class FauxCtx:
    """Le minimum dont les trois outils ont besoin : `sim`, `ok` et `non_fait` (cf. server.Ctx)."""

    def __init__(self, sim=False):
        self.sim = sim

    def ok(self, fait="", non_fait=(), **extra):
        return json.dumps({"ok": True, "fait": fait, "non_fait": list(non_fait), **extra}, ensure_ascii=False)

    def non_fait(self, raison, **extra):
        return json.dumps({"ok": False, "raison": raison, **extra}, ensure_ascii=False)


def appeler(nom, args, sim=False):
    return json.loads(asyncio.run(oc.OUTILS[nom]["fn"](FauxCtx(sim), dict(args))))


@pytest.fixture(autouse=True)
def memoire_vide():
    journal.ecrire("delete from memoire")
    yield
    journal.ecrire("delete from memoire")


# ---------------------------------------------------------------- retenir
def test_retenir_garde_le_fait():
    r = appeler("retenir", {"fait": "Greg est allergique aux arachides"})
    assert r["ok"] is True
    assert [f["fait"] for f in journal.souvenirs()] == ["Greg est allergique aux arachides"]


def test_retenir_refuse_le_vide():
    assert appeler("retenir", {"fait": "  "})["ok"] is False
    assert journal.souvenirs() == []


def test_le_banc_n_ecrit_rien_en_memoire():
    """Un outil composé s'exécute VRAIMENT en simulation (server.Tools.call le route avant le court-circuit) :
    sans cette garde, chaque passage du banc ajouterait des souvenirs à la base de Greg."""
    r = appeler("retenir", {"fait": "Greg déteste le jazz"}, sim=True)
    assert r["ok"] is True and "simulé" in r["fait"]
    assert journal.souvenirs() == []


# ---------------------------------------------------------------- oublier
def test_oublier_retrouve_le_souvenir_avec_les_mots_de_greg():
    journal.retenir("Greg est allergique aux arachides")
    journal.retenir("La poubelle sort le mardi soir")
    r = appeler("oublier", {"quoi": "la poubelle du mardi"})
    assert r["ok"] is True
    assert [f["fait"] for f in journal.souvenirs()] == ["Greg est allergique aux arachides"]


def test_oublier_ne_supprime_rien_au_hasard():
    """Le plus proche de rien reste n'importe quoi — et un souvenir effacé ne revient pas."""
    journal.retenir("Greg est allergique aux arachides")
    r = appeler("oublier", {"quoi": "le code du garage"})
    assert r["ok"] is False
    assert len(journal.souvenirs()) == 1


def test_oublier_sans_rien_en_memoire_le_dit():
    assert appeler("oublier", {"quoi": "n'importe quoi"})["ok"] is False


def test_le_banc_n_efface_rien():
    journal.retenir("Greg est allergique aux arachides")
    r = appeler("oublier", {"quoi": "les arachides"}, sim=True)
    assert r["ok"] is True and "simulé" in r["fait"]
    assert len(journal.souvenirs()) == 1


# ---------------------------------------------------------------- lister
def test_lister_rend_tout_et_le_compte():
    for f in ("Greg est allergique aux arachides", "La poubelle sort le mardi soir"):
        journal.retenir(f)
    r = appeler("lister_souvenirs", {})
    assert r["total"] == 2 and len(r["souvenirs"]) == 2


def test_lister_a_vide_ne_ment_pas():
    r = appeler("lister_souvenirs", {})
    assert r["ok"] is True and r["total"] == 0


def test_la_carte_montre_les_souvenirs():
    """Un nouvel outil composé n'a pas de carte tant que cartes.py ne le connaît pas (guide du dépôt). On part du
    vrai résultat de l'outil, pas d'un dictionnaire écrit à la main : c'est le contrat entre les deux."""
    import cartes
    journal.retenir("Greg est allergique aux arachides")
    resultat = asyncio.run(oc.OUTILS["lister_souvenirs"]["fn"](FauxCtx(), {}))
    c = cartes.depuis_outil("lister_souvenirs", {}, resultat)
    assert c and c["gabarit"] == "liste"
    assert c["items"][0]["texte"] == "Greg est allergique aux arachides"


def test_la_carte_ne_s_affiche_pas_quand_il_n_y_a_rien_a_montrer():
    resultat = asyncio.run(oc.OUTILS["lister_souvenirs"]["fn"](FauxCtx(), {}))
    import cartes
    assert cartes.depuis_outil("lister_souvenirs", {}, resultat) is None


# ---------------------------------------------------------------- relecture dans le prompt
def test_les_souvenirs_arrivent_dans_le_prompt():
    journal.retenir("Greg est allergique aux arachides")
    entete = server.entete_memoire()
    assert "allergique aux arachides" in entete


def test_un_prompt_sans_souvenir_ne_dit_rien():
    assert server.entete_memoire() == ""


def test_le_prompt_est_plafonne(monkeypatch):
    """Au-delà de `memoire.max`, ce sont les plus récents qui partent — et c'est le signe qu'il faudrait une
    vraie recherche, pas une troncature plus fine."""
    for i in range(10):
        journal.retenir(f"fait numero {i}")
    vrai = regles.c
    monkeypatch.setattr(regles, "c", lambda cle, defaut=None: (
        {"actif": True, "max": 3} if cle == "memoire" else vrai(cle, defaut)))
    entete = server.entete_memoire()
    assert "fait numero 9" in entete and "fait numero 0" not in entete


def test_le_reglage_coupe_la_relecture(monkeypatch):
    journal.retenir("Greg est allergique aux arachides")
    vrai = regles.c
    monkeypatch.setattr(regles, "c", lambda cle, defaut=None: (
        {"actif": False} if cle == "memoire" else vrai(cle, defaut)))
    assert server.entete_memoire() == ""


def test_le_banc_ne_voit_jamais_les_souvenirs():
    """Sinon le juge de l'agent de nuit dépendrait de ce que Greg a fait retenir la veille, et une correction
    parfaitement bonne pourrait être rejetée par un souvenir ajouté entre-temps."""
    journal.retenir("Greg est allergique aux arachides")
    prompt_banc = asyncio.run(server.system_prompt(sim=True))
    prompt_salon = asyncio.run(server.system_prompt(sim=False))
    assert "arachides" not in prompt_banc
    assert "arachides" in prompt_salon


# ---------------------------------------------------------------- ce qui ne doit PAS arriver
def test_la_purge_de_nuit_ne_touche_pas_aux_souvenirs():
    """Les échanges s'effacent au bout de trente jours ; un souvenir, jamais. C'est toute la différence entre
    la mémoire de conversation et la mémoire longue."""
    journal.retenir("Greg est allergique aux arachides")
    journal.ecrire("update memoire set ts = 0")                    # un souvenir d'il y a des années
    journal.purger({"echanges_jours": 1, "ignores_jours": 1, "incidents_jours": 1,
                    "actions_jours": 1, "tests_jours": 1, "retours_jours": 1})
    assert len(journal.souvenirs()) == 1


def test_l_effacement_des_textes_ignores_ne_touche_pas_aux_souvenirs():
    journal.retenir("Greg est allergique aux arachides")
    journal.effacer_textes_ignores()
    assert len(journal.souvenirs()) == 1


def test_retenir_et_oublier_ne_sont_pas_en_lecture_seule():
    """Un outil en lecture seule s'exécute pour de vrai pendant le banc. `retenir` et `oublier` écrivent :
    les y glisser ferait grossir la mémoire de Greg à chaque passage du banc."""
    lecture = regles.c("lecture_seule", [])
    assert not regles.correspond("retenir", lecture)
    assert not regles.correspond("oublier", lecture)
    assert regles.correspond("lister_souvenirs", lecture)          # lui ne fait que lire, et le banc doit le voir
