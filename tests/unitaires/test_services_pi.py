"""Les unités systemd du Pi (JARVIS) — ce que le déployeur ne vérifie pas.

`outils/deployer_pi.sh` regarde `systemctl is-active` huit secondes après un redémarrage : ça attrape un
programme qui refuse de démarrer, pas une unité dont la *configuration* cède plus tard. Or les deux pièges de
systemd qui nous concernent sont exactement de ce genre — une limite de redémarrage qui laisse Bulle muette, et
un `Conflicts=` sans ordre qui se joue à la milliseconde. Ils ne se voient qu'en production, dans le salon.

Ces cas lisent les fichiers du dépôt : ni Pi, ni systemd, ni réseau.
"""
import configparser
import math
import os
import re

import pytest

ICI = os.path.dirname(os.path.abspath(__file__))
DEPOT = os.path.dirname(os.path.dirname(ICI))
PI = os.path.join(DEPOT, "pi")
UNITES = ["kinectface-face.service", "kinectface-compagnon.service", "kinectface-tracker.service"]

# Temps que met le cerveau à répondre de nouveau après `systemctl restart kinectface-cerveau` : ollama décharge
# le modèle et la première réponse met une trentaine de secondes (guide du dépôt, tableau des machines).
RETOUR_CERVEAU_S = 30
# Valeurs par défaut de systemd quand l'unité ne les fixe pas (DefaultStartLimitIntervalSec / …Burst).
DEFAUT_FENETRE_S, DEFAUT_RAFALE = 10.0, 5


def lire(nom):
    c = configparser.ConfigParser(strict=False)      # systemd tolère les clés répétées, configparser non
    c.optionxform = str
    with open(os.path.join(PI, nom), encoding="utf-8") as f:
        c.read_file(f)
    return c


def unites(valeur):
    """Les noms d'unités d'une directive (After=, Conflicts=… : séparés par des espaces)."""
    return [u for u in (valeur or "").split() if u]


def _secondes(v, defaut):
    """Une durée systemd : « 2 », « 2s », « 1min ». Seules les formes qu'on écrit ici sont acceptées."""
    if v is None: return defaut
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(s|sec|min|m)?", str(v).strip())
    assert m, f"durée systemd non reconnue : {v!r}"
    return float(m.group(1)) * (60 if m.group(2) in ("min", "m") else 1)


def limites(unite):
    """StartLimitIntervalSec / StartLimitBurst vivent dans [Unit] ; systemd les tolère encore dans [Service]."""
    d = dict(unite["Service"]) if unite.has_section("Service") else {}
    d.update(unite["Unit"] if unite.has_section("Unit") else {})
    return d


def fenetre_de_reessai(unite):
    """Combien de temps systemd relancera l'unité avant d'abandonner (secondes ; inf = il n'abandonne jamais).

    Modèle : le programme meurt tout de suite (connexion refusée), donc un démarrage toutes les RestartSec.
    systemd renonce dès que « rafale » démarrages tiennent dans la fenêtre — et l'unité reste en `failed`,
    sans rien redémarrer, jusqu'à ce que quelqu'un s'en aperçoive.
    """
    lim = limites(unite)
    fenetre = _secondes(lim.get("StartLimitIntervalSec"), DEFAUT_FENETRE_S)
    if fenetre == 0: return math.inf                 # limite explicitement désactivée
    rafale = int(lim.get("StartLimitBurst", DEFAUT_RAFALE))
    pas = _secondes(unite["Service"].get("RestartSec"), 0.1)   # DefaultRestartSec = 100 ms
    return math.inf if pas * rafale > fenetre else pas * rafale


def test_le_client_tient_le_temps_que_le_cerveau_revienne():
    """Le client se termine EXPRÈS à chaque coupure du cerveau (`os._exit(75)`, compagnon.py) en comptant sur
    systemd pour le relancer. Si la limite de redémarrage s'épuise avant le retour du cerveau, personne ne le
    relance : Bulle n'écoute plus, le visage reste affiché, et rien ne le signale."""
    assert fenetre_de_reessai(lire("kinectface-compagnon.service")) >= RETOUR_CERVEAU_S


@pytest.mark.parametrize("nom", UNITES)
def test_un_conflit_est_aussi_un_ordre(nom):
    """`Conflicts=` dit à systemd d'arrêter l'autre unité, pas d'attendre qu'elle soit arrêtée : les deux se
    font en parallèle. Le visage peut donc réclamer l'écran KMS avant que la getty l'ait rendu."""
    u = lire(nom)["Unit"]
    ordonnees = set(unites(u.get("After"))) | set(unites(u.get("Before")))
    for adversaire in unites(u.get("Conflicts")):
        assert adversaire in ordonnees, f"{nom} : Conflicts={adversaire} sans After= ni Before="


@pytest.mark.parametrize("nom", UNITES)
def test_le_deployeur_sait_recopier_ce_que_l_unite_lance(nom):
    """Garde-fou : un programme du Pi lancé par systemd mais absent de la table du déployeur ne serait jamais
    mis à jour — on déploierait sans rien déployer, et sans le voir."""
    with open(os.path.join(DEPOT, "outils", "deployer_pi.sh"), encoding="utf-8") as f:
        deployeur = f.read()
    service = os.path.splitext(nom)[0]
    for programme in re.findall(r"\S+\.py", lire(nom)["Service"]["ExecStart"]):
        base = os.path.basename(programme)
        ligne = re.search(rf"^\s*\[(?:\S*/)?{re.escape(base)}\]=(.+)$", deployeur, re.M)
        assert ligne, f"{base} est lancé par {nom} mais absent de FICHIERS dans deployer_pi.sh"
        assert service in ligne.group(1), f"{base} change, mais le déployeur ne redémarre pas {service}"


@pytest.mark.parametrize("nom", UNITES)
def test_le_deployeur_installe_les_unites(nom):
    """Une unité corrigée dans le dépôt mais jamais recopiée ne corrige rien : le Pi garde l'ancienne et le dépôt
    promet un réglage que la machine n'a pas. Et une unité recopiée sans `daemon-reload` est là sans s'appliquer —
    `systemctl restart` relancerait l'ancienne définition, ce qui est exactement le genre de panne qu'on ne voit pas."""
    with open(os.path.join(DEPOT, "outils", "deployer_pi.sh"), encoding="utf-8") as f:
        deployeur = f.read()
    assert re.search(rf"^\s*\[pi/{re.escape(nom)}\]=", deployeur, re.M),         f"{nom} n'est pas dans FICHIERS : le déployeur ne la recopiera jamais"
    assert "daemon-reload" in deployeur, "les unités sont recopiées mais systemd ne les relit pas"


def test_le_deployeur_recopie_les_polices_dans_leur_sous_dossier():
    """Les polices sont le seul fichier du Pi qui ne vit pas à plat dans ~/kinectface.

    Oubliée de FICHIERS, une graisse ajoutée au dépôt n'arriverait jamais sur le Pi : carte.py retomberait sur
    DejaVu et la carte s'afficherait — en moins bien, sans rien signaler. Recopiée à plat, ce serait pareil,
    puisque carte.DOSSIERS ne regarde que ~/kinectface/polices."""
    with open(os.path.join(DEPOT, "outils", "deployer_pi.sh"), encoding="utf-8") as f:
        deployeur = f.read()
    dossier = os.path.join(DEPOT, "pi", "polices")
    for nom in sorted(os.listdir(dossier)):
        assert re.search(rf"^\s*\[pi/polices/{re.escape(nom)}\]=kinectface-face\s*$", deployeur, re.M), \
            f"pi/polices/{nom} est dans le dépôt mais absent de FICHIERS"
    assert "kinectface/polices/$(basename" in deployeur, "les polices seraient recopiées à plat"
    assert "mkdir -p kinectface/polices" in deployeur, "sans le dossier, la copie échoue"


def test_le_deployeur_verifie_la_copie_avant_de_redemarrer():
    """Une copie qui n'aboutit pas ne fait rien échouer : le fichier distant reste l'ancien, le déployeur annonce
    « copié », redémarre, et `systemctl is-active` répond `active` — sur l'ancien code. Vu le 21/09 en posant les
    unités à la main. Le seul garde-fou est de relire l'empreinte distante APRÈS la copie."""
    with open(os.path.join(DEPOT, "outils", "deployer_pi.sh"), encoding="utf-8") as f:
        deployeur = f.read()
    copie = deployeur.index("cat > $dst.nouveau")
    redemarrage = deployeur.index("systemctl restart")
    entre_les_deux = deployeur[copie:redemarrage]
    assert "sha256sum" in entre_les_deux, "rien ne relit l'empreinte distante entre la copie et le redémarrage"
