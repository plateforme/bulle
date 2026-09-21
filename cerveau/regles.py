"""Lecture de config/regles.yaml, rechargée automatiquement quand le fichier change (l'agent peut le modifier à chaud).

Porte aussi la lecture du jeton partagé — qui, lui, ne vient SURTOUT pas de regles.yaml (fichier versionné).
"""
import hmac, os, re, threading

import yaml

CHEMIN = os.environ.get("BULLE_REGLES", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "regles.yaml"))
_lock, _cache = threading.Lock(), {"mtime": None, "data": {}}


def tout():
    with _lock:
        try:
            m = os.path.getmtime(CHEMIN)
        except OSError:
            return _cache["data"]
        if m != _cache["mtime"]:
            with open(CHEMIN, encoding="utf-8") as f:
                _cache.update(mtime=m, data=yaml.safe_load(f) or {})
        return _cache["data"]


JETON_FICHIER = os.path.expanduser("~/.config/bulle/jeton")


def jeton():
    """Le secret partagé qui protège /ws, /suivi et /plan — chaîne vide s'il n'est pas configuré.

    Volontairement PAS dans regles.yaml : ce fichier est versionné et déployé sur le Pi. Il vient de la variable
    d'environnement BULLE_JETON (le cerveau la reçoit par l'EnvironmentFile de son unité systemd) ou, à défaut,
    de ~/.config/bulle/jeton — le même dossier que signal.env, et le seul chemin qui marche pour la boucle de
    nuit, dont l'unité systemd n'a pas d'EnvironmentFile.
    """
    j = os.environ.get("BULLE_JETON", "").strip()
    if j: return j
    try:
        with open(JETON_FICHIER, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def jeton_valide(presente):
    """Comparaison à temps constant : une comparaison naïve laisse deviner le jeton octet par octet."""
    attendu = jeton()
    presente = (presente or "").strip()
    if presente.lower().startswith("bearer "):
        presente = presente[7:].strip()
    return bool(attendu) and hmac.compare_digest(presente, attendu)


def get(section, cle, defaut=None):
    return (tout().get(section) or {}).get(cle, defaut)


def c(cle, defaut=None):
    """Raccourci pour la section « cerveau »."""
    return get("cerveau", cle, defaut)


def correspond(nom, motifs):
    """Nom exact ou préfixe « xxx* »."""
    return any(nom == m or (m.endswith("*") and nom.startswith(m[:-1])) for m in motifs or [])


def regex_nom():
    """(réveil n'importe où, apostrophe de début, apostrophe de fin, nom mal entendu en début)."""
    n = "(?:" + "|".join(c("nom_variantes", ["bulle"])) + ")"
    mal = "(?:" + "|".join(c("nom_mal_entendu", []) or ["(?!x)x"]) + ")"
    intro = r"^\W*(?:(?:hé|hey|eh|ok|dis|oh)\W+)?"
    return (re.compile(r"\b" + n + r"\b", re.I), re.compile(intro + n + r"\b\W*", re.I),
            re.compile(r"\W+" + n + r"\W*$", re.I), re.compile(intro + mal + r"\s*[,.!?]\s*", re.I))
